"""FastAPI application factory and entry point.

Run it with:

    uvicorn app.main:app --reload

``uvicorn`` imports this module, finds the ``app`` object, and serves it.
Importing this module also imports ``app.core.config``, which is what validates
the environment - so a misconfigured deployment fails at startup with a clear
message rather than during the first trade.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.router import api_router
from app.core.config import settings
from app.core.exceptions import register_exception_handlers
from app.core.logging_config import configure_logging
from app.core.middleware import RequestContextMiddleware
from app.db.database import check_database_connection
from app.dependencies.services import close_binance_client
from app.services.execution_queue import TradeExecutor, get_trade_executor

configure_logging()
logger = logging.getLogger(__name__)

DESCRIPTION = """
A single-service FastAPI backend that turns TradingView alerts into orders on
the **Binance Spot testnet**.

* `POST /auth/register` and `POST /auth/login` issue a JWT.
* `GET /trades` and `GET /trades/{id}` are protected and scoped to the caller.
* `POST /webhook/tradingview` is authenticated with a shared secret and is
  idempotent on `signal_id`.

**Safety:** this deployment refuses to start unless `BINANCE_BASE_URL` points at
a Binance *testnet* host. No real-money order can be placed.
"""


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Startup and shutdown work.

    Startup deliberately does *not* create tables - schema changes belong to
    Alembic, so that a running app can never silently reshape the database.
    """
    logger.info(
        "Starting %s (environment=%s, binance_host=%s, dry_run=%s, "
        "async_execution=%s)",
        settings.APP_NAME,
        settings.ENVIRONMENT,
        settings.binance_host,
        settings.BINANCE_DRY_RUN,
        settings.WEBHOOK_ASYNC_EXECUTION,
    )

    database_up = await check_database_connection()
    if database_up:
        logger.info("Database connection OK")
    else:
        # A warning, not a crash: the app can still serve /health and report
        # itself as not-ready while PostgreSQL comes up.
        logger.warning(
            "Database is NOT reachable at startup. Check DATABASE_URL and that "
            "PostgreSQL is running. /health/ready will report 'degraded'."
        )

    executor: TradeExecutor | None = None
    if settings.WEBHOOK_ASYNC_EXECUTION:
        executor = get_trade_executor()
        await executor.start()
        if database_up:
            # Anything committed but never sent in a previous run - e.g. the
            # process was killed between the PENDING commit and the exchange
            # call - is picked up here. Trades that *were* sent are left to
            # reconciliation, which queries rather than re-sends.
            await executor.resume_pending()
    else:
        logger.info("Inline webhook execution: the request waits for Binance")

    yield

    if executor is not None:
        await executor.stop()
    await close_binance_client()
    logger.info("Shutdown complete")


def create_app() -> FastAPI:
    """Build the application. A factory keeps tests free to build their own."""
    application = FastAPI(
        title=settings.APP_NAME,
        description=DESCRIPTION,
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    # Middleware first: it must wrap every request so each one gets an id.
    application.add_middleware(RequestContextMiddleware)

    # Then the handlers that give every error one consistent JSON shape.
    register_exception_handlers(application)

    application.include_router(api_router)
    return application


app = create_app()
