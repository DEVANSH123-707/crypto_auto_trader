"""The trading workflow: every Binance outcome, and duplicate protection.

No test here touches the network. ``FakeBinanceClient`` is injected through
``app.dependency_overrides``, so the routes and services run their real code.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import httpx2
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Trade, TradeStatus
from app.services.trading_service import build_client_order_id
from tests.conftest import TEST_WEBHOOK_SECRET, signal_payload
from tests.fakes import FakeBinanceClient

#: How many identical signals to fire at once in the race test.
CONCURRENT_DUPLICATES = 8


async def fetch_trade(db: AsyncSession, signal_id: str) -> Trade:
    db.expire_all()  # do not serve a stale copy from the identity map
    result = await db.execute(select(Trade).where(Trade.signal_id == signal_id))
    return result.scalar_one()


class TestSuccessfulTrade:
    async def test_filled_order_is_recorded(
        self,
        client: httpx2.AsyncClient,
        owner,
        webhook_headers: dict[str, str],
        db_session: AsyncSession,
        fake_binance: FakeBinanceClient,
    ):
        response = await client.post(
            "/webhook/tradingview", json=signal_payload(), headers=webhook_headers
        )

        assert response.status_code == 202
        body = response.json()["trade"]
        assert body["status"] == "FILLED"
        assert body["binance_order_id"] == fake_binance.next_order_id
        assert body["quantity"] == "0.001"
        assert body["error_code"] is None

        trade = await fetch_trade(db_session, "tv-test-0001")
        assert trade.status is TradeStatus.FILLED
        assert trade.executed_quantity == Decimal("0.001")
        assert trade.submitted_at is not None
        assert trade.user_id == owner.id

    async def test_binance_receives_the_derived_client_order_id(
        self,
        client: httpx2.AsyncClient,
        owner,
        webhook_headers: dict[str, str],
        fake_binance: FakeBinanceClient,
    ):
        """The id must be derived from the signal, not random.

        That is what makes a post-timeout lookup possible.
        """
        await client.post(
            "/webhook/tradingview", json=signal_payload(), headers=webhook_headers
        )

        expected = build_client_order_id("tv-test-0001")
        assert fake_binance.submitted_client_order_ids == [expected]
        assert len(expected) <= 36

    async def test_partial_fill_is_recorded_as_partially_filled(
        self,
        client: httpx2.AsyncClient,
        owner,
        webhook_headers: dict[str, str],
        fake_binance: FakeBinanceClient,
    ):
        fake_binance.fill_status = "PARTIALLY_FILLED"

        response = await client.post(
            "/webhook/tradingview", json=signal_payload(), headers=webhook_headers
        )

        assert response.json()["trade"]["status"] == "PARTIALLY_FILLED"


class TestBinanceFailures:
    async def test_rejection_is_recorded_and_still_returns_202(
        self,
        client: httpx2.AsyncClient,
        owner,
        webhook_headers: dict[str, str],
        db_session: AsyncSession,
        fake_binance: FakeBinanceClient,
    ):
        """Binance refusing the order is a trade outcome, not a webhook error.

        Returning 4xx here would make TradingView re-send an alert that is
        guaranteed to fail the same way.
        """
        fake_binance.mode = "reject"

        response = await client.post(
            "/webhook/tradingview", json=signal_payload(), headers=webhook_headers
        )

        assert response.status_code == 202
        body = response.json()["trade"]
        assert body["status"] == "REJECTED"
        assert body["error_code"] == "-2010"
        assert "insufficient balance" in body["error_message"]

        assert (await fetch_trade(db_session, "tv-test-0001")).status is TradeStatus.REJECTED

    async def test_rate_limit_is_recorded_as_rejected(
        self,
        client: httpx2.AsyncClient,
        owner,
        webhook_headers: dict[str, str],
        db_session: AsyncSession,
        fake_binance: FakeBinanceClient,
    ):
        fake_binance.mode = "rate_limit"

        response = await client.post(
            "/webhook/tradingview", json=signal_payload(), headers=webhook_headers
        )

        trade = await fetch_trade(db_session, "tv-test-0001")
        assert trade.status is TradeStatus.REJECTED
        assert trade.error_code.startswith("rate_limit:")
        assert response.status_code == 202

    async def test_connection_failure_is_recorded_as_failed(
        self,
        client: httpx2.AsyncClient,
        owner,
        webhook_headers: dict[str, str],
        db_session: AsyncSession,
        fake_binance: FakeBinanceClient,
    ):
        """Never connected, so no order can exist - FAILED is a known fact."""
        fake_binance.mode = "unavailable"

        await client.post(
            "/webhook/tradingview", json=signal_payload(), headers=webhook_headers
        )

        trade = await fetch_trade(db_session, "tv-test-0001")
        assert trade.status is TradeStatus.FAILED
        assert trade.binance_order_id is None

    async def test_a_failed_binance_call_still_leaves_a_trade_row(
        self, client: httpx2.AsyncClient, owner, webhook_headers: dict[str, str], db_session
    ):
        """The PENDING row is committed before Binance is called, so every
        attempt is trackable even when the call blows up."""
        await client.post(
            "/webhook/tradingview", json=signal_payload(), headers=webhook_headers
        )
        assert (
            (await db_session.execute(select(func.count()).select_from(Trade))).scalar_one()
            == 1
        )


class TestBinanceTimeout:
    """The dangerous case: the order may or may not exist."""

    async def test_timeout_becomes_unknown_when_it_cannot_be_resolved(
        self,
        client: httpx2.AsyncClient,
        owner,
        webhook_headers: dict[str, str],
        db_session: AsyncSession,
        fake_binance: FakeBinanceClient,
    ):
        fake_binance.mode = "timeout"
        fake_binance.reconcile_result = "error"  # follow-up query also fails

        response = await client.post(
            "/webhook/tradingview", json=signal_payload(), headers=webhook_headers
        )

        assert response.status_code == 202
        assert response.json()["trade"]["status"] == "UNKNOWN"

        trade = await fetch_trade(db_session, "tv-test-0001")
        assert trade.status is TradeStatus.UNKNOWN
        # It must NOT be guessed into a terminal state.
        assert not trade.is_terminal

    async def test_timeout_never_retries_the_order(
        self,
        client: httpx2.AsyncClient,
        owner,
        webhook_headers: dict[str, str],
        fake_binance: FakeBinanceClient,
    ):
        """A blind retry is how one signal becomes two positions."""
        fake_binance.mode = "timeout"
        fake_binance.reconcile_result = "error"

        await client.post(
            "/webhook/tradingview", json=signal_payload(), headers=webhook_headers
        )

        assert fake_binance.place_order_calls == 1
        # It asked instead of retrying.
        assert fake_binance.get_order_calls == 1

    async def test_timeout_resolves_when_the_order_did_land(
        self,
        client: httpx2.AsyncClient,
        owner,
        webhook_headers: dict[str, str],
        db_session: AsyncSession,
        fake_binance: FakeBinanceClient,
    ):
        """The response was lost but the order executed - reconciliation finds it."""
        fake_binance.mode = "timeout"
        fake_binance.reconcile_result = "found"
        fake_binance.reconcile_status = "FILLED"

        response = await client.post(
            "/webhook/tradingview", json=signal_payload(), headers=webhook_headers
        )

        assert response.json()["trade"]["status"] == "FILLED"
        trade = await fetch_trade(db_session, "tv-test-0001")
        assert trade.status is TradeStatus.FILLED
        assert trade.binance_order_id == fake_binance.next_order_id
        assert trade.reconciled_at is not None
        assert fake_binance.place_order_calls == 1

    async def test_timeout_resolves_to_failed_when_the_order_never_landed(
        self,
        client: httpx2.AsyncClient,
        owner,
        webhook_headers: dict[str, str],
        db_session: AsyncSession,
        fake_binance: FakeBinanceClient,
    ):
        """Binance says -2013 "order does not exist" - now FAILED is a fact."""
        fake_binance.mode = "timeout"
        fake_binance.reconcile_result = "not_found"

        response = await client.post(
            "/webhook/tradingview", json=signal_payload(), headers=webhook_headers
        )

        assert response.json()["trade"]["status"] == "FAILED"
        trade = await fetch_trade(db_session, "tv-test-0001")
        assert trade.error_code == "order_not_found"


class TestDuplicateProtection:
    async def test_resending_the_same_signal_returns_409(
        self, client: httpx2.AsyncClient, owner, webhook_headers: dict[str, str]
    ):
        first = await client.post(
            "/webhook/tradingview", json=signal_payload(), headers=webhook_headers
        )
        second = await client.post(
            "/webhook/tradingview", json=signal_payload(), headers=webhook_headers
        )

        assert first.status_code == 202
        assert second.status_code == 409
        assert second.json()["error"]["code"] == "duplicate_signal"

    async def test_a_duplicate_never_reaches_binance(
        self,
        client: httpx2.AsyncClient,
        owner,
        webhook_headers: dict[str, str],
        fake_binance: FakeBinanceClient,
    ):
        for _ in range(4):
            await client.post(
                "/webhook/tradingview",
                json=signal_payload(),
                headers=webhook_headers,
            )

        assert fake_binance.place_order_calls == 1

    async def test_only_one_row_exists_after_repeated_signals(
        self, client: httpx2.AsyncClient, owner, webhook_headers: dict[str, str], db_session
    ):
        for _ in range(4):
            await client.post(
                "/webhook/tradingview",
                json=signal_payload(),
                headers=webhook_headers,
            )

        assert (
            (await db_session.execute(select(func.count()).select_from(Trade))).scalar_one()
            == 1
        )

    async def test_a_different_signal_id_is_a_different_trade(
        self,
        client: httpx2.AsyncClient,
        owner,
        webhook_headers: dict[str, str],
        fake_binance: FakeBinanceClient,
    ):
        await client.post(
            "/webhook/tradingview",
            json=signal_payload(signal_id="sig-a"),
            headers=webhook_headers,
        )
        second = await client.post(
            "/webhook/tradingview",
            json=signal_payload(signal_id="sig-b"),
            headers=webhook_headers,
        )

        assert second.status_code == 202
        assert fake_binance.place_order_calls == 2

    async def test_a_failed_trade_still_blocks_a_resend_of_the_same_signal(
        self,
        client: httpx2.AsyncClient,
        owner,
        webhook_headers: dict[str, str],
        fake_binance: FakeBinanceClient,
    ):
        """Idempotency is about the *signal*, not about success.

        Re-sending after a failure must not place an order, because the first
        attempt's outcome may still be unresolved.
        """
        fake_binance.mode = "unavailable"
        await client.post(
            "/webhook/tradingview", json=signal_payload(), headers=webhook_headers
        )

        fake_binance.mode = "success"
        retry = await client.post(
            "/webhook/tradingview", json=signal_payload(), headers=webhook_headers
        )

        assert retry.status_code == 409
        assert fake_binance.place_order_calls == 1


class TestConcurrentDuplicateProtection:
    async def test_concurrent_identical_signals_create_exactly_one_trade(
        self,
        client: httpx2.AsyncClient,
        owner,
        db_session: AsyncSession,
        fake_binance: FakeBinanceClient,
    ):
        """Requests racing on the same signal_id.

        There is no lock between "does this exist?" and the INSERT, so nothing
        in Python decides this. The UNIQUE index on ``trades.signal_id`` picks
        the winner; every loser gets an ``IntegrityError`` that becomes a 409.

        ``asyncio.gather`` releases all of them into the app at once. On SQLite
        the writes still serialise at the file lock, so there it is a smoke
        test; against PostgreSQL (TEST_DATABASE_URL) it is a genuine race.
        """
        payload = signal_payload(signal_id="race-condition-signal")
        headers = {"X-Webhook-Secret": TEST_WEBHOOK_SECRET}

        responses = await asyncio.gather(
            *(
                client.post(
                    "/webhook/tradingview", json=payload, headers=headers
                )
                for _ in range(CONCURRENT_DUPLICATES)
            )
        )
        results = [response.status_code for response in responses]

        assert sorted(results) == [202] + [409] * (CONCURRENT_DUPLICATES - 1)
        assert fake_binance.place_order_calls == 1

        db_session.expire_all()
        assert (
            (await db_session.execute(
                select(func.count())
                .select_from(Trade)
                .where(Trade.signal_id == "race-condition-signal")
            )).scalar_one()
            == 1
        )


class TestUniqueConstraintAtTheDatabaseLevel:
    async def test_inserting_a_duplicate_signal_id_raises(
        self, db_session: AsyncSession, owner, session_factory: async_sessionmaker[AsyncSession]
    ):
        """Proves the guarantee lives in PostgreSQL, not just in Python."""
        def make(signal_id: str, client_order_id: str) -> Trade:
            return Trade(
                user_id=owner.id,
                signal_id=signal_id,
                client_order_id=client_order_id,
                symbol="BTCUSDT",
                side="BUY",
                quantity=Decimal("0.001"),
                status=TradeStatus.PENDING,
            )

        db_session.add(make("same-signal", "cat-aaa"))
        await db_session.commit()

        other = session_factory()
        try:
            other.add(make("same-signal", "cat-bbb"))
            with pytest.raises(IntegrityError):
                await other.commit()
        finally:
            await other.rollback()
            await other.close()
