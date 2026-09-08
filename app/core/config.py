"""Application configuration.

All configuration comes from environment variables (loaded from ``.env`` during
local development). Nothing secret is ever hardcoded here.

Configuration is validated the moment this module is imported, which happens
during application startup. A missing or nonsensical value therefore stops the
process immediately with a readable error instead of blowing up later inside a
request.
"""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------------------
# Binance safety allowlist
# ---------------------------------------------------------------------------
# This project must never touch real-money Binance trading. The only hosts the
# application is allowed to talk to are the public Binance *testnet* hosts.
#
# There is deliberately NO environment flag to switch this off: pointing the
# application at production requires editing this constant, which is a visible,
# reviewable code change rather than an accidental .env typo.
ALLOWED_BINANCE_HOSTS: frozenset[str] = frozenset(
    {
        "testnet.binance.vision",  # Spot testnet (the one this project uses)
        "testnet.binancefuture.com",  # Futures testnet
    }
)

# Hosts that are explicitly called out so the error message can be loud and
# specific if someone pastes a production URL into their .env.
KNOWN_PRODUCTION_BINANCE_HOSTS: frozenset[str] = frozenset(
    {
        "api.binance.com",
        "api1.binance.com",
        "api2.binance.com",
        "api3.binance.com",
        "api4.binance.com",
        "api-gcp.binance.com",
        "fapi.binance.com",
        "dapi.binance.com",
        "api.binance.us",
    }
)

# Literal values shipped in .env.example. Refusing them stops a deployment
# that "works" while running on the example secret everyone can read on GitHub.
PLACEHOLDER_SECRETS: frozenset[str] = frozenset(
    {
        "replace_with_a_long_random_string_at_least_32_chars",
        "replace_with_a_random_string_at_least_16_chars",
        "change-me",
        "changeme",
        "replace-me",
        "your-secret-here",
        "string",
    }
)


# ---------------------------------------------------------------------------
# PostgreSQL URL normalisation
# ---------------------------------------------------------------------------
# Managed PostgreSQL providers hand out libpq-style URLs. SQLAlchemy passes any
# query parameter it does not recognise straight through to ``asyncpg.connect``
# as a keyword argument, and asyncpg does not speak libpq: a URL ending in
# ``?sslmode=require`` fails with
#
#     TypeError: connect() got an unexpected keyword argument 'sslmode'
#
# asyncpg spells the same thing ``ssl`` and accepts the identical set of values
# (disable / allow / prefer / require / verify-ca / verify-full), so the
# parameter is renamed rather than dropped - dropping it would silently
# downgrade a URL that explicitly asked for TLS.
LIBPQ_SSL_PARAM_RENAMES: dict[str, str] = {"sslmode": "ssl"}

#: libpq parameters asyncpg has no equivalent for. Kept, they would crash the
#: connection the same way; the TLS requirement is still carried by ``ssl``.
LIBPQ_PARAMS_ASYNCPG_IGNORES: frozenset[str] = frozenset({"channel_binding"})


def normalise_postgres_query_params(url: str) -> str:
    """Rewrite libpq-only query parameters into what asyncpg understands."""
    if "?" not in url:
        return url

    scheme, netloc, path, query, fragment = urlsplit(url)
    kept: list[tuple[str, str]] = []
    for key, value in parse_qsl(query, keep_blank_values=True):
        lowered = key.lower()
        if lowered in LIBPQ_PARAMS_ASYNCPG_IGNORES:
            continue
        kept.append((LIBPQ_SSL_PARAM_RENAMES.get(lowered, key), value))

    return urlunsplit((scheme, netloc, path, urlencode(kept), fragment))


class Settings(BaseSettings):
    """Typed, validated view of the process environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # -- Application ------------------------------------------------------
    APP_NAME: str = "Crypto Auto-Trading Backend"
    ENVIRONMENT: str = "local"
    LOG_LEVEL: str = "INFO"

    # -- Database ---------------------------------------------------------
    # e.g. postgresql+asyncpg://crypto_user:password@localhost:5432/crypto_trader
    DATABASE_URL: str
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_TIMEOUT_SECONDS: int = 30
    DB_ECHO: bool = False

    # -- JWT --------------------------------------------------------------
    JWT_SECRET: str = Field(min_length=32)
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(default=60, ge=1, le=60 * 24 * 7)

    # -- Webhook ----------------------------------------------------------
    WEBHOOK_SECRET: str = Field(min_length=16)
    # Trades created by the machine-to-machine webhook are attributed to this
    # user account. See PROJECT_IMPLEMENTATION_NOTES.md for the reasoning.
    WEBHOOK_TRADE_OWNER_EMAIL: str

    # -- Binance (TESTNET ONLY) -------------------------------------------
    BINANCE_API_KEY: str
    BINANCE_SECRET_KEY: str
    BINANCE_BASE_URL: str = "https://testnet.binance.vision"
    BINANCE_TIMEOUT_SECONDS: float = Field(default=10.0, gt=0, le=60)
    BINANCE_RECV_WINDOW_MS: int = Field(default=5000, ge=1000, le=60000)
    # When true the Binance service calls POST /api/v3/order/test, which
    # validates an order without ever creating one. Useful as a first smoke
    # test against the testnet before letting the app place real testnet orders.
    BINANCE_DRY_RUN: bool = False

    # -- Webhook execution model ------------------------------------------
    # True  : the webhook commits the PENDING trade and hands the exchange call
    #         to the in-process executor, acknowledging in a few milliseconds.
    # False : the exchange call happens inline and the caller waits for the
    #         final status. This is the baseline the benchmark compares against.
    # Idempotency, the PENDING state, reconciliation and recovery behave
    # identically in both modes.
    WEBHOOK_ASYNC_EXECUTION: bool = True
    #: Concurrent exchange calls in flight. Kept modest so a burst of signals
    #: cannot stampede Binance into rate-limiting us.
    EXECUTOR_WORKERS: int = Field(default=8, ge=1, le=64)
    #: Bounded, so a runaway producer surfaces as a warning rather than
    #: unbounded memory growth.
    EXECUTOR_QUEUE_SIZE: int = Field(default=1000, ge=1)

    # -- Trading business rules -------------------------------------------
    ALLOWED_SYMBOLS: str = "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT"
    MIN_ORDER_QUANTITY: float = Field(default=0.00001, gt=0)
    MAX_ORDER_QUANTITY: float = Field(default=1.0, gt=0)

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("DATABASE_URL")
    @classmethod
    def _normalise_database_url(cls, value: str) -> str:
        """Force every PostgreSQL URL onto the one driver this project ships.

        The application talks to PostgreSQL asynchronously, which needs an
        async driver - here, asyncpg. A bare ``postgresql://`` URL would send
        SQLAlchemy looking for psycopg2, and a synchronous driver cannot back
        an async engine at all, so any PostgreSQL dialect is rewritten to
        ``postgresql+asyncpg://``.

        (asyncpg rather than psycopg's async mode for one concrete reason:
        psycopg refuses to run on Windows' default ProactorEventLoop, which is
        the loop uvicorn gets. asyncpg works on it unchanged.)

        Managed hosts (Render, Heroku, Neon) also hand out ``postgres://`` and
        append libpq query parameters, so the URL's query string is translated
        into asyncpg's spelling too - see
        :func:`normalise_postgres_query_params`.
        """
        value = value.strip()
        if not value:
            raise ValueError("DATABASE_URL must not be empty")

        for prefix in ("postgresql+psycopg2://", "postgresql+psycopg://"):
            if value.startswith(prefix):
                value = "postgresql+asyncpg://" + value[len(prefix) :]
                break
        else:
            if value.startswith("postgresql://"):
                value = value.replace("postgresql://", "postgresql+asyncpg://", 1)
            elif value.startswith("postgres://"):
                value = value.replace("postgres://", "postgresql+asyncpg://", 1)

        if value.startswith("postgresql+asyncpg://"):
            value = normalise_postgres_query_params(value)
        return value

    @field_validator("JWT_SECRET", "WEBHOOK_SECRET")
    @classmethod
    def _reject_placeholder_secrets(cls, value: str, info: ValidationInfo) -> str:
        if value.strip().lower() in PLACEHOLDER_SECRETS:
            raise ValueError(
                f"{info.field_name} still contains a placeholder value. "
                "Generate a real secret with: "
                "python -c \"import secrets; print(secrets.token_urlsafe(48))\""
            )
        return value

    @field_validator("BINANCE_BASE_URL")
    @classmethod
    def _enforce_testnet_only(cls, value: str) -> str:
        """Hard safety gate: only Binance testnet hosts are accepted."""
        value = value.strip().rstrip("/")
        parsed = urlparse(value)

        if parsed.scheme not in {"http", "https"}:
            raise ValueError(
                f"BINANCE_BASE_URL must be an absolute http(s) URL, got {value!r}"
            )

        host = (parsed.hostname or "").lower()

        if host in KNOWN_PRODUCTION_BINANCE_HOSTS:
            raise ValueError(
                f"REFUSING TO START: {host!r} is a real-money Binance endpoint. "
                "This project is testnet-only. Use "
                "BINANCE_BASE_URL=https://testnet.binance.vision"
            )

        if host not in ALLOWED_BINANCE_HOSTS:
            allowed = ", ".join(sorted(ALLOWED_BINANCE_HOSTS))
            raise ValueError(
                f"REFUSING TO START: BINANCE_BASE_URL host {host!r} is not an "
                f"allowed Binance testnet host. Allowed hosts: {allowed}"
            )

        return value

    @field_validator("WEBHOOK_TRADE_OWNER_EMAIL")
    @classmethod
    def _normalise_owner_email(cls, value: str) -> str:
        value = value.strip().lower()
        if "@" not in value:
            raise ValueError("WEBHOOK_TRADE_OWNER_EMAIL must be an email address")
        return value

    # ------------------------------------------------------------------
    # Derived helpers
    # ------------------------------------------------------------------

    @property
    def allowed_symbols(self) -> frozenset[str]:
        """Uppercase set of symbols this deployment is permitted to trade."""
        return frozenset(
            part.strip().upper()
            for part in self.ALLOWED_SYMBOLS.split(",")
            if part.strip()
        )

    @property
    def binance_host(self) -> str:
        return (urlparse(self.BINANCE_BASE_URL).hostname or "").lower()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings object (parsed exactly once)."""
    return Settings()  # type: ignore[call-arg]  # values are read from the environment


# Imported directly by the rest of the application. Importing this module is
# what triggers configuration validation at startup.
settings = get_settings()
