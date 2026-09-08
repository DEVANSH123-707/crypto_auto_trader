"""Shared pytest fixtures.

Two things have to happen before anything from ``app`` is imported:

1. the environment must be populated, because ``app.core.config`` validates it
   at import time and would otherwise refuse to load;
2. the database URL must point at a throwaway database.

That is why the ``os.environ`` block below sits at the very top of the file,
above the ``app`` imports. It is the one place in this project where import
order is load-bearing.

The suite is fully async: ``pytest-asyncio`` runs in ``auto`` mode (see
pyproject.toml), the app is driven through ``httpx2.ASGITransport`` rather than
a threaded test client, and every database fixture is an ``AsyncSession``.

By default it runs against a file-backed SQLite database via aiosqlite, so
``pytest`` works on a fresh clone with nothing installed but the requirements.
To run the identical suite against real PostgreSQL - which also makes the
concurrency tests a genuine race - set TEST_DATABASE_URL first:

    $env:TEST_DATABASE_URL="postgresql+asyncpg://user:pw@localhost:5432/crypto_trader_test"
    pytest
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

# --- Environment, before any `app` import ----------------------------------
_TEST_DB_DIR = Path(tempfile.mkdtemp(prefix="crypto_trader_tests_"))
_DEFAULT_TEST_DB_URL = f"sqlite+aiosqlite:///{_TEST_DB_DIR / 'test.db'}".replace(
    "\\", "/"
)

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", _DEFAULT_TEST_DB_URL)
IS_POSTGRES = TEST_DATABASE_URL.startswith("postgres")

TEST_WEBHOOK_SECRET = "test-webhook-secret-value"
TEST_OWNER_EMAIL = "webhook-owner@example.com"

os.environ.update(
    {
        "DATABASE_URL": TEST_DATABASE_URL,
        "JWT_SECRET": "test-jwt-secret-that-is-definitely-long-enough-123456",
        "JWT_ALGORITHM": "HS256",
        "ACCESS_TOKEN_EXPIRE_MINUTES": "60",
        "WEBHOOK_SECRET": TEST_WEBHOOK_SECRET,
        "WEBHOOK_TRADE_OWNER_EMAIL": TEST_OWNER_EMAIL,
        # Testnet placeholders. The fake exchange below means no HTTP request
        # is ever made, but configuration still has to validate.
        "BINANCE_API_KEY": "test-api-key",
        "BINANCE_SECRET_KEY": "test-secret-key",
        "BINANCE_BASE_URL": "https://testnet.binance.vision",
        "ALLOWED_SYMBOLS": "BTCUSDT,ETHUSDT",
        "MIN_ORDER_QUANTITY": "0.00001",
        "MAX_ORDER_QUANTITY": "1.0",
        "LOG_LEVEL": "WARNING",
        "ENVIRONMENT": "test",
        # Most tests assert on the *final* trade status in the webhook
        # response, so they run against inline execution. The queued path has
        # its own module (test_async_execution.py) which flips this on.
        "WEBHOOK_ASYNC_EXECUTION": "false",
    }
)

# --- Now it is safe to import the application ------------------------------
import httpx2  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db.database import Base, get_db  # noqa: E402
from app.db.models import User  # noqa: E402
from app.dependencies.services import get_binance_client  # noqa: E402
from app.main import app  # noqa: E402
from app.services.auth_service import register_user  # noqa: E402
from app.services.trading_service import reset_trade_owner_cache  # noqa: E402
from tests.fakes import FakeBinanceClient  # noqa: E402


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
    connect_args: dict[str, object] = {}
    if TEST_DATABASE_URL.startswith("sqlite"):
        # Concurrency tests drive many coroutines at the same database file;
        # a busy timeout makes SQLite wait for the write lock rather than
        # error out immediately.
        connect_args = {"check_same_thread": False, "timeout": 30}

    test_engine = create_async_engine(TEST_DATABASE_URL, connect_args=connect_args)

    if TEST_DATABASE_URL.startswith("sqlite"):
        # SQLite ignores FOREIGN KEY constraints unless asked not to.
        # PostgreSQL always enforces them; this makes the SQLite run behave the
        # same way, so a broken foreign key fails in tests, not in production.
        from sqlalchemy import event

        @event.listens_for(test_engine.sync_engine, "connect")
        def _fk_on(dbapi_connection, _record):  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    yield test_engine
    await test_engine.dispose()


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _fresh_schema(engine: AsyncEngine) -> AsyncIterator[None]:
    """Give every test an empty database.

    Slower than wrapping each test in a rollback, but far easier to reason
    about - and the trading code commits more than once on purpose, so a single
    surrounding transaction would not survive it anyway.
    """
    # The webhook owner id is cached for the process lifetime; every test
    # rebuilds the database, so the cache has to go with it.
    reset_trade_owner_cache()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest_asyncio.fixture(loop_scope="session")
async def db_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """A session for asserting directly against the database."""
    async with session_factory() as session:
        yield session


@pytest.fixture
def fake_binance() -> FakeBinanceClient:
    """The stand-in exchange. Every test gets a fresh one with no history."""
    return FakeBinanceClient()


@pytest_asyncio.fixture(loop_scope="session")
async def client(
    session_factory: async_sessionmaker[AsyncSession],
    fake_binance: FakeBinanceClient,
) -> AsyncIterator[httpx2.AsyncClient]:
    """An HTTP client wired to the test database and the fake exchange.

    Both substitutions go through ``app.dependency_overrides``, which is the
    payoff for building the database session and the Binance client as
    dependencies: no application code is patched, and nothing in ``app/`` knows
    the test suite exists.

    ``ASGITransport`` calls the app directly in this event loop - no socket, no
    background thread - so async fixtures and the app share one loop.
    """

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_binance_client] = lambda: fake_binance

    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as test_client:
        yield test_client

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Convenience fixtures
# ---------------------------------------------------------------------------

DEFAULT_PASSWORD = "correct-horse-battery-staple"


@pytest_asyncio.fixture(loop_scope="session")
async def user(db_session: AsyncSession) -> User:
    """A registered, non-owner user (used for authorization tests)."""
    created = await register_user(db_session, "alice@example.com", DEFAULT_PASSWORD)
    db_session.expunge_all()
    return created


@pytest_asyncio.fixture(loop_scope="session")
async def owner(db_session: AsyncSession) -> User:
    """The account WEBHOOK_TRADE_OWNER_EMAIL points at.

    Webhook-created trades belong to this user, so tests that check trade
    listing need it to exist.
    """
    created = await register_user(db_session, TEST_OWNER_EMAIL, DEFAULT_PASSWORD)
    db_session.expunge_all()
    return created


async def auth_headers_for(
    client: httpx2.AsyncClient, email: str, password: str
) -> dict[str, str]:
    """Log in and return an Authorization header."""
    response = await client.post(
        "/auth/login", json={"email": email, "password": password}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest_asyncio.fixture(loop_scope="session")
async def owner_headers(client: httpx2.AsyncClient, owner: User) -> dict[str, str]:
    return await auth_headers_for(client, TEST_OWNER_EMAIL, DEFAULT_PASSWORD)


@pytest_asyncio.fixture(loop_scope="session")
async def user_headers(client: httpx2.AsyncClient, user: User) -> dict[str, str]:
    return await auth_headers_for(client, "alice@example.com", DEFAULT_PASSWORD)


@pytest.fixture
def webhook_headers() -> dict[str, str]:
    return {"X-Webhook-Secret": TEST_WEBHOOK_SECRET}


def signal_payload(**overrides: object) -> dict[str, object]:
    """A valid webhook body, with any field overridden."""
    payload: dict[str, object] = {
        "signal_id": "tv-test-0001",
        "symbol": "BTCUSDT",
        "action": "BUY",
        "quantity": 0.001,
    }
    payload.update(overrides)
    return payload
