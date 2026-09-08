"""Reconciliation: recovering trades whose real outcome was never recorded.

Covers both hard cases from the design:

* Binance timed out - did the order land?
* Binance succeeded but the database update failed - the row is still PENDING
  while a live order exists.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx2
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Trade, TradeStatus, User
from app.services.reconciliation_service import (
    find_reconcilable_trades,
    reconcile_pending_trades,
    reconcile_trade,
)
from tests.fakes import FakeBinanceClient


async def make_stuck_trade(
    db: AsyncSession,
    user: User,
    *,
    signal_id: str = "stuck-1",
    status: TradeStatus = TradeStatus.UNKNOWN,
    age_seconds: int = 3600,
) -> Trade:
    """A trade that was submitted but whose outcome was never recorded."""
    created = datetime.now(UTC) - timedelta(seconds=age_seconds)
    trade = Trade(
        user_id=user.id,
        signal_id=signal_id,
        client_order_id=f"cat-{signal_id}",
        symbol="BTCUSDT",
        side="BUY",
        quantity=Decimal("0.001"),
        status=status,
        submitted_at=created,
        created_at=created,
    )
    db.add(trade)
    await db.commit()
    await db.refresh(trade)
    return trade


class TestReconcileTrade:
    async def test_order_found_updates_the_trade(
        self, db_session: AsyncSession, user: User, fake_binance: FakeBinanceClient
    ):
        trade = await make_stuck_trade(db_session, user)
        fake_binance.reconcile_result = "found"
        fake_binance.reconcile_status = "FILLED"

        result = await reconcile_trade(db_session, fake_binance, trade)

        assert result is TradeStatus.FILLED
        assert trade.binance_order_id == fake_binance.next_order_id
        assert trade.executed_quantity == Decimal("0.001")
        assert trade.reconciled_at is not None
        assert trade.error_code is None

    async def test_order_not_found_becomes_failed(
        self, db_session: AsyncSession, user: User, fake_binance: FakeBinanceClient
    ):
        """Binance error -2013 is proof the submission never landed."""
        trade = await make_stuck_trade(db_session, user)
        fake_binance.reconcile_result = "not_found"

        result = await reconcile_trade(db_session, fake_binance, trade)

        assert result is TradeStatus.FAILED
        assert trade.error_code == "order_not_found"

    async def test_a_failed_query_changes_nothing(
        self, db_session: AsyncSession, user: User, fake_binance: FakeBinanceClient
    ):
        """"We could not ask" must never be recorded as "there is no order"."""
        trade = await make_stuck_trade(db_session, user)
        fake_binance.reconcile_result = "error"

        result = await reconcile_trade(db_session, fake_binance, trade)

        assert result is TradeStatus.UNKNOWN
        assert trade.status is TradeStatus.UNKNOWN
        assert trade.reconciled_at is None

    async def test_reconciliation_never_places_an_order(
        self, db_session: AsyncSession, user: User, fake_binance: FakeBinanceClient
    ):
        """It is read-only against the exchange, so it is safe to run twice."""
        trade = await make_stuck_trade(db_session, user)
        fake_binance.reconcile_result = "found"

        first = await reconcile_trade(db_session, fake_binance, trade)
        second = await reconcile_trade(db_session, fake_binance, trade)

        assert fake_binance.place_order_calls == 0
        assert first is second is TradeStatus.FILLED
        # The first run settled the trade, so the second had nothing to ask.
        assert fake_binance.get_order_calls == 1

    async def test_a_settled_trade_is_left_alone(
        self, db_session: AsyncSession, user: User, fake_binance: FakeBinanceClient
    ):
        """Re-querying a settled trade could only lose information."""
        trade = await make_stuck_trade(
            db_session, user, status=TradeStatus.REJECTED
        )
        trade.error_code = "-2010"
        await db_session.commit()
        fake_binance.reconcile_result = "not_found"

        result = await reconcile_trade(db_session, fake_binance, trade)

        assert result is TradeStatus.REJECTED
        assert trade.error_code == "-2010"  # not overwritten
        assert fake_binance.get_order_calls == 0

    async def test_a_cancelled_order_maps_to_cancelled(
        self, db_session: AsyncSession, user: User, fake_binance: FakeBinanceClient
    ):
        """Binance spells it CANCELED; our enum spells it CANCELLED."""
        trade = await make_stuck_trade(db_session, user)
        fake_binance.reconcile_result = "found"
        fake_binance.reconcile_status = "CANCELED"

        assert (
            await reconcile_trade(db_session, fake_binance, trade)
            is TradeStatus.CANCELLED
        )


class TestDatabaseFailedAfterBinanceSucceeded:
    async def test_a_stranded_pending_trade_is_repaired(
        self, db_session: AsyncSession, user: User, fake_binance: FakeBinanceClient
    ):
        """The scenario from section 12 of the design.

        Binance filled the order, then the second commit failed, so the row is
        still PENDING with no binance_order_id. Because the PENDING row - and
        crucially its client_order_id - was committed *before* the Binance
        call, reconciliation can still find the order and repair the row.
        """
        stranded = await make_stuck_trade(
            db_session, user, signal_id="stranded", status=TradeStatus.PENDING
        )
        assert stranded.binance_order_id is None

        fake_binance.reconcile_result = "found"
        fake_binance.reconcile_status = "FILLED"

        await reconcile_trade(db_session, fake_binance, stranded)

        assert stranded.status is TradeStatus.FILLED
        assert stranded.binance_order_id == fake_binance.next_order_id


class TestBatchReconciliation:
    async def test_only_in_flight_trades_are_selected(
        self, db_session: AsyncSession, user: User
    ):
        await make_stuck_trade(db_session, user, signal_id="a", status=TradeStatus.PENDING)
        await make_stuck_trade(db_session, user, signal_id="b", status=TradeStatus.UNKNOWN)
        await make_stuck_trade(db_session, user, signal_id="c", status=TradeStatus.FILLED)
        await make_stuck_trade(db_session, user, signal_id="d", status=TradeStatus.REJECTED)

        found = await find_reconcilable_trades(db_session, older_than_seconds=60)

        assert {trade.signal_id for trade in found} == {"a", "b"}

    async def test_recent_trades_are_left_alone(self, db_session: AsyncSession, user: User):
        """Do not race a request that is legitimately still waiting."""
        await make_stuck_trade(db_session, user, signal_id="fresh", age_seconds=0)

        assert await find_reconcilable_trades(db_session, older_than_seconds=60) == []

    async def test_batch_run_reports_what_it_changed(
        self, db_session: AsyncSession, user: User, fake_binance: FakeBinanceClient
    ):
        await make_stuck_trade(db_session, user, signal_id="a")
        await make_stuck_trade(db_session, user, signal_id="b")
        fake_binance.reconcile_result = "found"

        summary = await reconcile_pending_trades(db_session, fake_binance)

        assert summary == {"checked": 2, "changed": 2, "unchanged": 0}


class TestReconcileEndpoint:
    async def test_owner_can_reconcile_their_stuck_trade(
        self,
        client: httpx2.AsyncClient,
        db_session: AsyncSession,
        owner: User,
        owner_headers: dict[str, str],
        fake_binance: FakeBinanceClient,
    ):
        trade = await make_stuck_trade(db_session, owner, signal_id="via-api")
        fake_binance.reconcile_result = "found"
        fake_binance.reconcile_status = "FILLED"

        response = await client.post(
            f"/trades/{trade.id}/reconcile", headers=owner_headers
        )

        assert response.status_code == 200
        assert response.json()["status"] == "FILLED"
        assert fake_binance.place_order_calls == 0

    async def test_reconcile_requires_authentication(
        self, client: httpx2.AsyncClient, db_session: AsyncSession, owner: User
    ):
        trade = await make_stuck_trade(db_session, owner, signal_id="unauth")

        assert (await client.post(f"/trades/{trade.id}/reconcile")).status_code == 401
