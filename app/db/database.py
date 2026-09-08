"""Async SQLAlchemy engine, session factory and the ``get_db`` dependency.

Layering, from the bottom up:

    PostgreSQL server (a separate OS process, usually on port 5432)
        ^
        | TCP + the PostgreSQL wire protocol, spoken by asyncpg
        |
    AsyncEngine  - owns the connection pool
        ^
        | checks a pooled connection out for the duration of a transaction
        |
    AsyncSession - unit of work: identity map, change tracking, commit
        ^
        | one per HTTP request, provided by get_db()
        |
    FastAPI `async def` route / service code

Everything that touches the database is awaited. A request waiting on
PostgreSQL yields control back to the event loop, so a single worker can have
many requests in flight at once instead of occupying a thread each.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator

from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings

logger = logging.getLogger(__name__)

#: Seconds to wait for a TCP connection to PostgreSQL before giving up.
#: Without this, a database that is down (or a firewall that silently drops
#: packets rather than refusing the connection, which is the Windows default)
#: makes the application hang at startup instead of reporting a clear error.
CONNECT_TIMEOUT_SECONDS = 5


class Base(DeclarativeBase):
    """Declarative base every ORM model inherits from.

    ``Base.metadata`` is the in-Python catalogue of tables; Alembic diffs it
    against the live database to generate migrations.
    """


def is_postgres(database_url: str) -> bool:
    return make_url(database_url).get_backend_name() == "postgresql"


def build_engine_kwargs(database_url: str) -> dict[str, object]:
    """Engine options that only make sense for a real PostgreSQL server.

    SQLite (used by the test suite) has no network connection to time out and
    no server-side pool to size, so those options are omitted for it.
    """
    if not is_postgres(database_url):
        return {}
    return {
        "pool_size": settings.DB_POOL_SIZE,
        "max_overflow": settings.DB_MAX_OVERFLOW,
        "pool_timeout": settings.DB_POOL_TIMEOUT_SECONDS,
        # Sends a cheap liveness check before handing out a pooled connection,
        # so a connection dropped by a restart or idle timeout is replaced
        # transparently instead of surfacing as a random mid-request error.
        "pool_pre_ping": True,
        # asyncpg spells the connection timeout "timeout" (libpq's
        # "connect_timeout" is not one of its keywords).
        "connect_args": {"timeout": CONNECT_TIMEOUT_SECONDS},
    }


# Created once per process. Creating it does NOT connect - the pool opens
# connections lazily, which is why importing this module is safe even when
# PostgreSQL is down.
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DB_ECHO,
    **build_engine_kwargs(settings.DATABASE_URL),
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    autoflush=False,  # flush explicitly, so SQL happens where we expect it
    expire_on_commit=False,  # keep attributes readable (and lazy-load free)
    # after commit - important in async, where an
    # unexpected lazy load raises MissingGreenlet
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield a request-scoped async database session.

    Used as ``db: AsyncSession = Depends(get_db)``. FastAPI runs the code
    before ``yield`` when the request starts, injects the session into the
    handler, and runs the code after ``yield`` once the response has been
    produced - even if the handler raised.

    Committing is the *caller's* job (the service layer owns transaction
    boundaries, and trading needs separate commits at chosen points). This
    dependency only guarantees that a failed request never leaves a
    half-finished transaction behind and that the connection always goes back
    to the pool.
    """
    async with AsyncSessionLocal() as db:
        try:
            yield db
        except Exception:
            await db.rollback()
            raise


async def check_database_connection() -> bool:
    """Cheap readiness probe used by ``GET /health/ready``."""
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # noqa: BLE001 - health checks must never raise
        logger.warning("Database readiness check failed: %s", type(exc).__name__)
        return False
