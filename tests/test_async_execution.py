"""The queued execution path and concurrency safety at 50+ requests.

Two claims are under test here.

1. With ``WEBHOOK_ASYNC_EXECUTION`` on, the webhook acknowledges a signal as
   soon as the trade is durable, and the exchange call happens afterwards on
   the executor - without weakening idempotency, the PENDING state or
   recoverability.
2. 50 concurrent webhooks behave correctly: 50 distinct signals produce exactly
   50 trades, and 50 *identical* signals produce exactly one.
"""

from __future__ import annotations

import asyncio

import httpx2
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.db.models import Trade, TradeStatus
from app.services.execution_queue import TradeExecutor, set_trade_executor
from tests.conftest import TEST_WEBHOOK_SECRET, signal_payload
from tests.fakes import FakeBinanceClient

CONCURRENCY = 50


async def count_trades(db: AsyncSession) -> int:
    db.expire_all()
    result = await db.execute(select(func.count()).select_from(Trade))
    return result.scalar_one()


@pytest.fixture
def queued_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn on the queued execution path for this test."""
    monkeypatch.setattr(settings, "WEBHOOK_ASYNC_EXECUTION", True)


@pytest.fixture
def executor(
    session_factory: async_sessionmaker[AsyncSession],
    fake_binance: FakeBinanceClient,
) -> TradeExecutor:
    """A real executor wired to the test database and the fake exchange."""
    instance = TradeExecutor(
        session_factory=session_factory,
        binance_client_factory=lambda: fake_binance,
        worker_count=4,
        queue_size=200,
    )
    set_trade_executor(instance)
    yield instance
    set_trade_executor(None)


class TestQueuedExecution:
    async def test_webhook_returns_before_the_exchange_is_called(
        self,
        client: httpx2.AsyncClient,
        owner,
        webhook_headers: dict[str, str],
        queued_mode: None,
        executor: TradeExecutor,
        fake_binance: FakeBinanceClient,
    ):
        """202 with a PENDING trade, and Binance untouched at that moment."""
        # Workers are deliberately not started, so nothing can drain the queue.
        response = await client.post(
            "/webhook/tradingview", json=signal_payload(), headers=webhook_headers
        )

        assert response.status_code == 202
        assert response.json()["trade"]["status"] == "PENDING"
        assert fake_binance.place_order_calls == 0

    async def test_the_trade_is_durable_before_it_is_queued(
        self,
        client: httpx2.AsyncClient,
        owner,
        webhook_headers: dict[str, str],
        db_session: AsyncSession,
        queued_mode: None,
        executor: TradeExecutor,
    ):
        """The row exists on disk even though no order has been placed yet.

        This is what makes losing the in-memory queue survivable.
        """
        await client.post(
            "/webhook/tradingview", json=signal_payload(), headers=webhook_headers
        )

        result = await db_session.execute(
            select(Trade).where(Trade.signal_id == "tv-test-0001")
        )
        trade = result.scalar_one()
        assert trade.status is TradeStatus.PENDING
        assert trade.submitted_at is None  # nothing was ever sent
        assert trade.client_order_id  # ...but the exchange id is already fixed

    async def test_the_executor_completes_the_trade(
        self,
        client: httpx2.AsyncClient,
        owner,
        webhook_headers: dict[str, str],
        db_session: AsyncSession,
        queued_mode: None,
        executor: TradeExecutor,
        fake_binance: FakeBinanceClient,
    ):
        await executor.start()
        try:
            await client.post(
                "/webhook/tradingview",
                json=signal_payload(),
                headers=webhook_headers,
            )
            await asyncio.wait_for(executor._queue.join(), timeout=10)
        finally:
            await executor.stop()

        db_session.expire_all()
        result = await db_session.execute(
            select(Trade).where(Trade.signal_id == "tv-test-0001")
        )
        trade = result.scalar_one()
        assert trade.status is TradeStatus.FILLED
        assert trade.binance_order_id == fake_binance.next_order_id
        assert trade.submitted_at is not None
        assert fake_binance.place_order_calls == 1

    async def test_a_trade_is_never_submitted_twice(
        self,
        db_session: AsyncSession,
        owner,
        client: httpx2.AsyncClient,
        webhook_headers: dict[str, str],
        queued_mode: None,
        executor: TradeExecutor,
        fake_binance: FakeBinanceClient,
    ):
        """Enqueuing the same trade id repeatedly still places one order.

        The worker re-reads the committed row and skips anything that is no
        longer an unsubmitted PENDING trade.
        """
        response = await client.post(
            "/webhook/tradingview", json=signal_payload(), headers=webhook_headers
        )
        trade_id = response.json()["trade"]["id"]

        for _ in range(5):
            executor.submit(trade_id)

        await executor.start()
        try:
            await asyncio.wait_for(executor._queue.join(), timeout=10)
        finally:
            await executor.stop()

        assert fake_binance.place_order_calls == 1

    async def test_only_one_concurrent_claim_wins(
        self,
        client: httpx2.AsyncClient,
        owner,
        webhook_headers: dict[str, str],
        session_factory: async_sessionmaker[AsyncSession],
        queued_mode: None,
        executor: TradeExecutor,
        fake_binance: FakeBinanceClient,
    ):
        """The claim must be atomic, not read-then-write.

        Several workers can hold the same trade id. If claiming were "load it,
        check submitted_at, then write", they could all read an unsubmitted row
        before any of them committed - and place one order each.
        """
        from app.services.trading_service import TradingService

        response = await client.post(
            "/webhook/tradingview", json=signal_payload(), headers=webhook_headers
        )
        trade_id = response.json()["trade"]["id"]

        async def claim() -> bool:
            async with session_factory() as db:
                service = TradingService(db=db, binance_client=fake_binance)
                return await service.claim_for_submission(trade_id) is not None

        won = await asyncio.gather(*(claim() for _ in range(10)))

        assert sum(won) == 1, "exactly one claimer must win"

    async def test_resume_pending_requeues_unsubmitted_trades(
        self,
        client: httpx2.AsyncClient,
        owner,
        webhook_headers: dict[str, str],
        db_session: AsyncSession,
        queued_mode: None,
        executor: TradeExecutor,
        fake_binance: FakeBinanceClient,
    ):
        """Recovery after a crash that lost the in-memory queue.

        The signal was accepted and committed but never sent. On the next
        startup ``resume_pending`` finds it by ``submitted_at IS NULL``.
        """
        await client.post(
            "/webhook/tradingview", json=signal_payload(), headers=webhook_headers
        )
        # Simulate the process dying: drop the queued item on the floor.
        executor._queue.get_nowait()  # (trade_id, request_id)
        executor._queue.task_done()
        assert fake_binance.place_order_calls == 0

        requeued = await executor.resume_pending()
        assert requeued == 1

        await executor.start()
        try:
            await asyncio.wait_for(executor._queue.join(), timeout=10)
        finally:
            await executor.stop()

        assert fake_binance.place_order_calls == 1
        db_session.expire_all()
        result = await db_session.execute(
            select(Trade).where(Trade.signal_id == "tv-test-0001")
        )
        assert result.scalar_one().status is TradeStatus.FILLED

    async def test_resume_pending_ignores_already_submitted_trades(
        self,
        client: httpx2.AsyncClient,
        owner,
        webhook_headers: dict[str, str],
        queued_mode: None,
        executor: TradeExecutor,
        fake_binance: FakeBinanceClient,
    ):
        """A trade that *was* sent must never be re-sent - only reconciled."""
        await executor.start()
        try:
            await client.post(
                "/webhook/tradingview",
                json=signal_payload(),
                headers=webhook_headers,
            )
            await asyncio.wait_for(executor._queue.join(), timeout=10)

            assert await executor.resume_pending() == 0
        finally:
            await executor.stop()

        assert fake_binance.place_order_calls == 1

    async def test_a_full_queue_leaves_the_trade_recoverable(
        self,
        client: httpx2.AsyncClient,
        owner,
        webhook_headers: dict[str, str],
        db_session: AsyncSession,
        queued_mode: None,
        session_factory: async_sessionmaker[AsyncSession],
        fake_binance: FakeBinanceClient,
    ):
        """Backpressure must not lose a signal that was already accepted."""
        tiny = TradeExecutor(
            session_factory=session_factory,
            binance_client_factory=lambda: fake_binance,
            worker_count=1,
            queue_size=1,
        )
        set_trade_executor(tiny)
        try:
            tiny.submit(999_999)  # fill the single slot; workers are not running

            response = await client.post(
                "/webhook/tradingview",
                json=signal_payload(),
                headers=webhook_headers,
            )
            assert response.status_code == 202

            # Not queued, but committed - and therefore still recoverable.
            result = await db_session.execute(
                select(Trade).where(Trade.signal_id == "tv-test-0001")
            )
            trade = result.scalar_one()
            assert trade.status is TradeStatus.PENDING
            assert trade.submitted_at is None
        finally:
            set_trade_executor(None)


class TestConcurrencyAtFifty:
    async def test_fifty_distinct_signals_create_fifty_trades(
        self,
        client: httpx2.AsyncClient,
        owner,
        db_session: AsyncSession,
        queued_mode: None,
        executor: TradeExecutor,
        fake_binance: FakeBinanceClient,
    ):
        headers = {"X-Webhook-Secret": TEST_WEBHOOK_SECRET}

        await executor.start()
        try:
            responses = await asyncio.gather(
                *(
                    client.post(
                        "/webhook/tradingview",
                        json=signal_payload(signal_id=f"burst-{index:03d}"),
                        headers=headers,
                    )
                    for index in range(CONCURRENCY)
                )
            )
            await asyncio.wait_for(executor._queue.join(), timeout=30)
        finally:
            await executor.stop()

        assert [r.status_code for r in responses] == [202] * CONCURRENCY
        assert await count_trades(db_session) == CONCURRENCY
        assert fake_binance.place_order_calls == CONCURRENCY
        # Every order carried its own deterministic client id.
        assert len(set(fake_binance.submitted_client_order_ids)) == CONCURRENCY

    async def test_fifty_identical_signals_create_exactly_one_trade(
        self,
        client: httpx2.AsyncClient,
        owner,
        db_session: AsyncSession,
        queued_mode: None,
        executor: TradeExecutor,
        fake_binance: FakeBinanceClient,
    ):
        """The unique index is the only thing arbitrating this."""
        headers = {"X-Webhook-Secret": TEST_WEBHOOK_SECRET}
        payload = signal_payload(signal_id="fifty-way-race")

        await executor.start()
        try:
            responses = await asyncio.gather(
                *(
                    client.post(
                        "/webhook/tradingview", json=payload, headers=headers
                    )
                    for _ in range(CONCURRENCY)
                )
            )
            await asyncio.wait_for(executor._queue.join(), timeout=30)
        finally:
            await executor.stop()

        codes = sorted(r.status_code for r in responses)
        assert codes == [202] + [409] * (CONCURRENCY - 1)
        assert await count_trades(db_session) == 1
        assert fake_binance.place_order_calls == 1

    async def test_no_transaction_is_left_broken_by_the_race(
        self,
        client: httpx2.AsyncClient,
        owner,
        db_session: AsyncSession,
        queued_mode: None,
        executor: TradeExecutor,
    ):
        """After 50 colliding inserts the database must still be usable.

        A rolled-back IntegrityError that was not cleaned up would leave the
        connection in "current transaction is aborted" state and poison the
        pool for every later request.
        """
        headers = {"X-Webhook-Secret": TEST_WEBHOOK_SECRET}
        await asyncio.gather(
            *(
                client.post(
                    "/webhook/tradingview",
                    json=signal_payload(signal_id="poison-check"),
                    headers=headers,
                )
                for _ in range(CONCURRENCY)
            )
        )

        # The pool still works, and a fresh signal is still accepted.
        follow_up = await client.post(
            "/webhook/tradingview",
            json=signal_payload(signal_id="after-the-race"),
            headers=headers,
        )
        assert follow_up.status_code == 202
        assert await count_trades(db_session) == 2
