"""Health endpoints, the error envelope, and the Binance testnet safety gate."""

from __future__ import annotations

import httpx2
import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.services.binance_service import BinanceClient


def valid_env(**overrides: str) -> dict[str, str]:
    """A complete, valid configuration; override one key to test one rule."""
    env = {
        "DATABASE_URL": "postgresql+psycopg://u:p@localhost:5432/db",
        "JWT_SECRET": "a" * 40,
        "WEBHOOK_SECRET": "b" * 20,
        "WEBHOOK_TRADE_OWNER_EMAIL": "owner@example.com",
        "BINANCE_API_KEY": "key",
        "BINANCE_SECRET_KEY": "secret",
        "BINANCE_BASE_URL": "https://testnet.binance.vision",
    }
    env.update(overrides)
    return env


class TestHealth:
    async def test_liveness_needs_no_dependencies(self, client: httpx2.AsyncClient):
        response = await client.get("/health")

        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    async def test_readiness_reports_the_database(self, client: httpx2.AsyncClient):
        response = await client.get("/health/ready")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ready"
        assert body["database"] == "up"
        assert body["binance_host"] == "testnet.binance.vision"


class TestErrorEnvelope:
    async def test_every_error_carries_a_correlation_id(self, client: httpx2.AsyncClient):
        response = await client.get("/trades")  # 401, no token

        error = response.json()["error"]
        assert set(error) >= {"code", "message", "request_id"}
        assert error["request_id"] == response.headers["X-Request-ID"]

    async def test_an_inbound_request_id_is_preserved(self, client: httpx2.AsyncClient):
        response = await client.get("/health", headers={"X-Request-ID": "trace-me-123"})

        assert response.headers["X-Request-ID"] == "trace-me-123"

    async def test_unknown_route_is_a_clean_404(self, client: httpx2.AsyncClient):
        response = await client.get("/does-not-exist")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    async def test_wrong_method_is_405(self, client: httpx2.AsyncClient):
        response = await client.get("/webhook/tradingview")

        assert response.status_code == 405
        assert "error" in response.json()


class TestBinanceSafetyGate:
    """Making a real-money order require a deliberate code change."""

    async def test_testnet_url_is_accepted(self):
        settings = Settings(_env_file=None, **valid_env())
        assert settings.binance_host == "testnet.binance.vision"

    @pytest.mark.parametrize(
        "production_url",
        [
            "https://api.binance.com",
            "https://api1.binance.com",
            "https://fapi.binance.com",
            "https://api.binance.us",
        ],
    )
    async def test_production_urls_are_refused(self, production_url: str):
        with pytest.raises(ValidationError) as caught:
            Settings(_env_file=None, **valid_env(BINANCE_BASE_URL=production_url))

        assert "REFUSING TO START" in str(caught.value)

    async def test_an_arbitrary_host_is_refused(self):
        with pytest.raises(ValidationError):
            Settings(
                _env_file=None,
                **valid_env(BINANCE_BASE_URL="https://evil.example.com"),
            )

    async def test_the_client_refuses_a_non_testnet_url_at_runtime(self):
        """Second gate, right next to the code that sends orders."""
        with pytest.raises(RuntimeError, match="not a Binance testnet host"):
            BinanceClient(base_url="https://api.binance.com")


class TestConfigurationValidation:
    async def test_a_short_jwt_secret_is_refused(self):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **valid_env(JWT_SECRET="too-short"))

    async def test_the_env_example_placeholder_is_refused(self):
        """Running on the secret that ships in .env.example must not be possible."""
        with pytest.raises(ValidationError, match="placeholder"):
            Settings(
                _env_file=None,
                **valid_env(
                    JWT_SECRET="REPLACE_WITH_A_LONG_RANDOM_STRING_AT_LEAST_32_CHARS"
                ),
            )

    async def test_a_missing_required_value_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        env = valid_env()
        del env["DATABASE_URL"]
        # conftest put DATABASE_URL in os.environ; remove it so the field is
        # genuinely absent rather than falling back to the process environment.
        monkeypatch.delenv("DATABASE_URL", raising=False)

        with pytest.raises(ValidationError, match="DATABASE_URL"):
            Settings(_env_file=None, **env)

    async def test_a_bare_postgresql_url_is_rewritten_for_asyncpg(self):
        """A sync driver cannot back an async engine, so the URL is rewritten."""
        settings = Settings(
            _env_file=None,
            **valid_env(DATABASE_URL="postgresql://u:p@localhost:5432/db"),
        )
        assert settings.DATABASE_URL.startswith("postgresql+asyncpg://")

    async def test_allowed_symbols_are_parsed_into_a_set(self):
        settings = Settings(
            _env_file=None, **valid_env(ALLOWED_SYMBOLS="btcusdt, ETHUSDT ")
        )
        assert settings.allowed_symbols == {"BTCUSDT", "ETHUSDT"}
