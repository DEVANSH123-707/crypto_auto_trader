"""Authenticated trade routes.

Every endpoint here requires a valid JWT *and* enforces ownership: the user id
from the token is part of the SQL WHERE clause, so one user's rows are never
loaded while serving another user's request.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, status

from app.core.exceptions import ResourceNotFoundError
from app.db.models import TradeStatus
from app.dependencies.auth import CurrentUser
from app.dependencies.services import TradingServiceDep, get_binance_client
from app.schemas.trade import TradeListResponse, TradeRead
from app.services.binance_service import BinanceClient
from app.services.reconciliation_service import reconcile_trade

router = APIRouter(
    prefix="/trades",
    tags=["trades"],
    responses={401: {"description": "Missing, invalid or expired token"}},
)


@router.get(
    "",
    response_model=TradeListResponse,
    summary="List my trades",
)
async def list_trades(
    current_user: CurrentUser,
    service: TradingServiceDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    status_filter: Annotated[
        TradeStatus | None,
        Query(alias="status", description="Only return trades in this status."),
    ] = None,
) -> TradeListResponse:
    """Return the caller's trades, newest first."""
    items, total = await service.list_trades_for_user(
        current_user, limit=limit, offset=offset, status=status_filter
    )
    return TradeListResponse(
        items=[TradeRead.model_validate(trade) for trade in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{trade_id}",
    response_model=TradeRead,
    summary="Get one of my trades",
    responses={404: {"description": "No such trade for this user"}},
)
async def get_trade(
    current_user: CurrentUser,
    service: TradingServiceDep,
    trade_id: Annotated[int, Path(ge=1)],
) -> TradeRead:
    """Fetch a single trade by id.

    A trade that belongs to somebody else returns 404, not 403. Answering 403
    would confirm that the id exists, which lets an attacker map the table.
    """
    trade = await service.get_trade_for_user(current_user, trade_id)
    if trade is None:
        raise ResourceNotFoundError(f"Trade {trade_id} was not found.")
    return TradeRead.model_validate(trade)


@router.post(
    "/{trade_id}/reconcile",
    response_model=TradeRead,
    status_code=status.HTTP_200_OK,
    summary="Ask Binance what really happened to this trade",
    responses={404: {"description": "No such trade for this user"}},
)
async def reconcile(
    current_user: CurrentUser,
    service: TradingServiceDep,
    binance: Annotated[BinanceClient, Depends(get_binance_client)],
    trade_id: Annotated[int, Path(ge=1)],
) -> TradeRead:
    """Re-query Binance for this trade and update it with the real outcome.

    Safe to call at any time: it only ever *reads* from Binance, so it can
    never create a duplicate order. This is the manual counterpart to
    ``scripts/reconcile.py``, and the recovery path for a trade left UNKNOWN by
    a timeout or stuck PENDING by a failed database write.
    """
    trade = await service.get_trade_for_user(current_user, trade_id)
    if trade is None:
        raise ResourceNotFoundError(f"Trade {trade_id} was not found.")

    await reconcile_trade(service.db, binance, trade)
    return TradeRead.model_validate(trade)
