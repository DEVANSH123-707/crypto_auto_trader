"""In-process trade executor: the exchange call, off the request path.

The webhook's job is to *accept* a signal durably. Placing the order is a
separate, slower step, and making the caller wait for a Binance round trip
(~200-400 ms) put the exchange's latency into our acknowledgement latency.

This module moves that step onto a small pool of asyncio tasks fed by a bounded
queue. It is deliberately the smallest thing that works:

* an ``asyncio.Queue`` of ``(trade_id, request_id)`` - no broker, no Redis,
  no Celery;
* N worker tasks started and stopped by the FastAPI lifespan;
* each worker opens its own ``AsyncSession``, because the request's session is
  already closed by the time the worker runs.

**Why a queue of ids and not of ORM objects.** A ``Trade`` instance belongs to the
session that loaded it. Passing the id and re-loading it in the worker's own
session keeps ownership unambiguous, and means the worker always acts on the
committed state of the row rather than an in-memory copy.

**Durability without a broker.** The queue is in memory, so a hard kill loses
its contents - but not the trades. Every queued trade was already committed as
PENDING before it was enqueued, and ``submitted_at IS NULL`` distinguishes
"never sent" from "sent, outcome unknown". On startup :meth:`resume_pending`
re-enqueues everything that was never sent, and reconciliation handles anything
that was. Losing the queue therefore costs latency, never correctness.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.logging_config import get_request_id, set_request_id
from app.db.database import AsyncSessionLocal
from app.db.models import Trade, TradeStatus
from app.services.binance_service import BinanceClient

logger = logging.getLogger(__name__)


class TradeExecutor:
    """A bounded queue of trade ids and the workers that drain it."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        binance_client_factory: Callable[[], BinanceClient] | None = None,
        worker_count: int | None = None,
        queue_size: int | None = None,
    ) -> None:
        # Both collaborators are injectable for the same reason the routes take
        # theirs from Depends(): the test suite swaps in a test database and a
        # fake exchange without patching anything inside this module.
        self._session_factory = session_factory or AsyncSessionLocal
        self._worker_count = worker_count or settings.EXECUTOR_WORKERS
        # (trade_id, request_id): the id of the webhook that created the trade
        # travels with it, so the executor's log lines carry the same
        # correlation id and one signal is still greppable end to end.
        self._queue: asyncio.Queue[tuple[int, str]] = asyncio.Queue(
            maxsize=queue_size or settings.EXECUTOR_QUEUE_SIZE
        )
        self._workers: list[asyncio.Task[None]] = []
        self._binance: BinanceClient | None = None
        self._binance_factory: Callable[[], BinanceClient] = (
            binance_client_factory or BinanceClient
        )
        self._running = False

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._workers = [
            asyncio.create_task(self._worker(index), name=f"trade-executor-{index}")
            for index in range(self._worker_count)
        ]
        logger.info(
            "Trade executor started (%s workers, queue size %s)",
            self._worker_count,
            self._queue.maxsize,
        )

    async def stop(self, *, drain_timeout: float = 10.0) -> None:
        """Finish the queued work if we can, then shut the workers down."""
        if not self._running:
            return
        self._running = False

        try:
            await asyncio.wait_for(self._queue.join(), timeout=drain_timeout)
        except TimeoutError:
            logger.warning(
                "Trade executor did not drain within %ss; %s item(s) left. They "
                "stay PENDING and are re-queued on the next startup.",
                drain_timeout,
                self._queue.qsize(),
            )

        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

        if self._binance is not None:
            await self._binance.close()
            self._binance = None

        logger.info("Trade executor stopped")

    # -- producing -------------------------------------------------------

    def submit(self, trade_id: int) -> bool:
        """Queue a committed PENDING trade for execution.

        Non-blocking by design: this runs inside the webhook request, and
        waiting for queue space would put backpressure straight back into the
        acknowledgement latency we are trying to protect.

        Returns False if the queue is full. That is not data loss - the trade
        is committed and unsubmitted, so :meth:`resume_pending` or a
        reconciliation run will still pick it up - but it does mean the system
        is accepting signals faster than it can place orders, which is worth a
        warning.
        """
        try:
            self._queue.put_nowait((trade_id, get_request_id()))
            return True
        except asyncio.QueueFull:
            logger.warning(
                "Trade executor queue is full (%s); trade_id=%s stays PENDING "
                "and will be submitted by the next resume/reconcile run.",
                self._queue.maxsize,
                trade_id,
            )
            return False

    async def resume_pending(self) -> int:
        """Re-queue trades that were committed but never sent to Binance.

        ``submitted_at IS NULL`` is the discriminator: it means no order was
        ever placed for this row, so submitting it now is safe. Trades that
        *were* sent and whose outcome is unknown are left to reconciliation,
        which queries rather than re-sends.
        """
        async with self._session_factory() as db:
            result = await db.execute(
                select(Trade.id)
                .where(
                    Trade.status == TradeStatus.PENDING,
                    Trade.submitted_at.is_(None),
                )
                .order_by(Trade.id)
                .limit(self._queue.maxsize)
            )
            trade_ids = list(result.scalars())

        for trade_id in trade_ids:
            self.submit(trade_id)

        if trade_ids:
            logger.info(
                "Re-queued %s unsubmitted PENDING trade(s) from a previous run",
                len(trade_ids),
            )
        return len(trade_ids)

    # -- consuming -------------------------------------------------------

    def _get_binance(self) -> BinanceClient:
        if self._binance is None:
            self._binance = self._binance_factory()
        return self._binance

    async def _worker(self, index: int) -> None:
        while True:
            trade_id, request_id = await self._queue.get()
            set_request_id(request_id)
            try:
                await self._execute(trade_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a worker must never die
                logger.exception(
                    "Trade executor worker %s failed on trade_id=%s; the trade "
                    "stays recoverable via reconciliation.",
                    index,
                    trade_id,
                )
            finally:
                self._queue.task_done()

    async def _execute(self, trade_id: int) -> None:
        """Place the order for one trade, in the worker's own session."""
        # Imported here to avoid a circular import at module load time.
        from app.services.trading_service import TradingService

        async with self._session_factory() as db:
            service = TradingService(db=db, binance_client=self._get_binance())

            # Take the trade atomically. Several workers can hold the same id -
            # the queue does not deduplicate, and resume_pending may re-add one
            # that is already queued - so "load it, check it, send it" would let
            # two workers both see an unsubmitted row and place two orders.
            # A conditional UPDATE lets the database pick exactly one winner.
            trade = await service.claim_for_submission(trade_id)

            if trade is None:
                logger.info(
                    "Executor: trade_id=%s was already claimed or is gone; "
                    "not submitting.",
                    trade_id,
                )
                return

            await service.submit_to_binance(trade)


# ---------------------------------------------------------------------------
# Process-wide instance
# ---------------------------------------------------------------------------
_executor: TradeExecutor | None = None


def get_trade_executor() -> TradeExecutor:
    global _executor
    if _executor is None:
        _executor = TradeExecutor()
    return _executor


def set_trade_executor(executor: TradeExecutor | None) -> None:
    """Replace the process-wide executor. Used by the test suite."""
    global _executor
    _executor = executor
