"""Service-construction dependencies.

Building services through ``Depends`` rather than instantiating them inside a
route has one concrete payoff: the test suite replaces the Binance client with
``app.dependency_overrides[get_binance_client] = ...`` and every route, service
and code path underneath it transparently talks to a fake exchange. No
monkeypatching of module internals, and the production code has no idea tests
exist.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.services.binance_service import BinanceClient
from app.services.trading_service import TradingService

logger = logging.getLogger(__name__)

# One client per process: httpx2.AsyncClient keeps a connection pool, and
# reusing it avoids a TLS handshake on every order. Created lazily so importing
# this module never opens a socket.
_binance_client: BinanceClient | None = None


def get_binance_client() -> BinanceClient:
    """Return the shared Binance testnet client."""
    global _binance_client
    if _binance_client is None:
        _binance_client = BinanceClient()
        logger.info("Binance client initialised")
    return _binance_client


async def close_binance_client() -> None:
    """Release the shared client's connection pool at shutdown."""
    global _binance_client
    if _binance_client is not None:
        await _binance_client.close()
        _binance_client = None


async def get_trading_service(
    db: Annotated[AsyncSession, Depends(get_db)],
    binance: Annotated[BinanceClient, Depends(get_binance_client)],
) -> AsyncGenerator[TradingService, None]:
    """Build a request-scoped :class:`TradingService`."""
    yield TradingService(db=db, binance_client=binance)


TradingServiceDep = Annotated[TradingService, Depends(get_trading_service)]
