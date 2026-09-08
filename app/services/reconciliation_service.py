"""Reconciliation: repairing trades whose real outcome we never learned.

This is the answer to the two hardest questions in the project.

**"Binance timed out. Did the order go through?"**
    We do not know, and we must not guess. Retrying could open a second
    position; assuming failure could leave a live position untracked. So we
    *ask*: query the order by the ``client_order_id`` we already committed
    before sending it.

**"Binance succeeded but the database update failed. Now what?"**
    The PENDING row is still on disk with the right ``client_order_id``,
    because it was committed *before* the Binance call. Reconciliation finds
    it, asks Binance, and writes the real outcome.

Reconciliation is a pure read-then-update. It never places an order, so
running it twice is harmless.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import RECONCILABLE_STATUSES, Trade, TradeStatus
from app.services.binance_service import (
    BinanceClient,
    BinanceError,
    BinanceRejectedError,
)

logger = logging.getLogger(__name__)


async def reconcile_trade(
    db: AsyncSession, binance: BinanceClient, trade: Trade
) -> TradeStatus:
    """Ask Binance what really happened to ``trade`` and record the answer.

    Three possible answers:

    * **the order exists** - copy its real status/ids onto the trade.
    * **the order does not exist** (Binance error -2013) - the submission never
      landed. The trade becomes FAILED, which is now a *known* fact rather than
      an assumption. Re-sending is safe from here, because the deterministic
      client order id means an accidental resend of the same signal would still
      collide at Binance.
    * **we could not ask** (network/exchange trouble) - nothing is changed. The
      trade stays reconcilable and the next run tries again. "Could not ask" is
      never allowed to look like "there is no order".

    A trade that has already settled is left completely alone. Re-querying it
    could only overwrite a precise outcome (say REJECTED with Binance's
    "insufficient balance") with a vaguer one, and there is nothing to learn.

    Returns the trade's status after the attempt.
    """
    if trade.is_terminal:
        logger.info(
            "Trade_id=%s is already terminal (%s); nothing to reconcile.",
            trade.id,
            trade.status.value,
        )
        return trade.status

    logger.info(
        "Reconciling trade_id=%s signal_id=%s client_order_id=%s (status=%s)",
        trade.id,
        trade.signal_id,
        trade.client_order_id,
        trade.status.value,
    )

    try:
        result = await binance.get_order(
            symbol=trade.symbol, client_order_id=trade.client_order_id
        )
    except BinanceRejectedError as exc:
        # e.g. bad API credentials, or the symbol was delisted. We still do not
        # know the order's fate, so the trade is left alone.
        logger.warning(
            "Reconciliation query rejected for trade_id=%s (code=%s): %s",
            trade.id,
            exc.code,
            exc.message,
        )
        return trade.status
    except BinanceError as exc:
        logger.warning(
            "Reconciliation query failed for trade_id=%s (%s). Leaving status "
            "as %s; will retry later.",
            trade.id,
            exc.code,
            trade.status.value,
        )
        return trade.status

    now = datetime.now(UTC)
    previous = trade.status

    if result is None:
        # Binance is certain there is no such order. Safe to close the loop.
        trade.status = TradeStatus.FAILED
        trade.error_code = "order_not_found"
        trade.error_message = (
            "Binance has no order with this client order id; the submission "
            "never reached the exchange."
        )
    else:
        # Import here to avoid a circular import at module load time.
        from app.services.trading_service import BINANCE_STATUS_MAP

        trade.binance_order_id = result.order_id
        trade.binance_status = result.status
        trade.executed_quantity = result.executed_quantity
        trade.cumulative_quote_quantity = result.cumulative_quote_quantity
        trade.status = BINANCE_STATUS_MAP.get(result.status, TradeStatus.NEW)
        if trade.status not in {TradeStatus.REJECTED, TradeStatus.FAILED}:
            trade.error_code = None
            trade.error_message = None

    trade.reconciled_at = now
    await db.commit()

    logger.info(
        "Reconciled trade_id=%s: %s -> %s (binance_order_id=%s)",
        trade.id,
        previous.value,
        trade.status.value,
        trade.binance_order_id,
    )
    return trade.status


async def find_reconcilable_trades(
    db: AsyncSession, *, older_than_seconds: int = 60, limit: int = 100
) -> list[Trade]:
    """Trades that are still in flight and old enough to be worth checking.

    The age filter keeps the job from racing requests that are legitimately
    still waiting on Binance right now.
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=older_than_seconds)
    result = await db.execute(
        select(Trade)
        .where(
            Trade.status.in_(RECONCILABLE_STATUSES),
            Trade.created_at < cutoff,
        )
        .order_by(Trade.created_at)
        .limit(limit)
    )
    return list(result.scalars())


async def reconcile_pending_trades(
    db: AsyncSession,
    binance: BinanceClient,
    *,
    older_than_seconds: int = 60,
    limit: int = 100,
) -> dict[str, int]:
    """Reconcile every in-flight trade. Entry point for ``scripts/reconcile.py``.

    Kept as a plain function taking a session and a client so it can be driven
    from a cron job, a management command, or a test - no web request needed.
    """
    trades = await find_reconcilable_trades(
        db, older_than_seconds=older_than_seconds, limit=limit
    )
    logger.info("Reconciliation run: %s trade(s) to check", len(trades))

    summary: dict[str, int] = {"checked": 0, "changed": 0, "unchanged": 0}
    for trade in trades:
        before = trade.status
        after = await reconcile_trade(db, binance, trade)
        summary["checked"] += 1
        summary["changed" if after != before else "unchanged"] += 1

    logger.info("Reconciliation run complete: %s", summary)
    return summary
