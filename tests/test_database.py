"""Model-level tests: persistence, relationships and constraints."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.core.security import verify_password
from app.db.models import Trade, TradeStatus, User
from app.services.auth_service import register_user
from tests.conftest import DEFAULT_PASSWORD


class TestUser:
    async def test_user_is_persisted_with_a_hash_not_a_password(self, db_session: AsyncSession):
        user = await register_user(db_session, "persist@example.com", DEFAULT_PASSWORD)

        stored = (await db_session.execute(
            select(User).where(User.email == "persist@example.com")
        )).scalar_one()

        assert stored.id == user.id
        assert stored.password_hash != DEFAULT_PASSWORD
        assert verify_password(DEFAULT_PASSWORD, stored.password_hash)
        assert stored.created_at is not None
        assert stored.is_active is True

    async def test_duplicate_email_violates_the_unique_index(
        self, db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ):
        db_session.add(User(email="dupe@example.com", password_hash="x"))
        await db_session.commit()

        other = session_factory()
        try:
            other.add(User(email="dupe@example.com", password_hash="y"))
            with pytest.raises(IntegrityError):
                await other.commit()
        finally:
            await other.rollback()
            await other.close()


class TestTrade:
    async def test_trade_is_persisted_with_its_decimal_intact(
        self, db_session: AsyncSession, user: User
    ):
        db_session.add(
            Trade(
                user_id=user.id,
                signal_id="sig-1",
                client_order_id="cat-1",
                symbol="BTCUSDT",
                side="BUY",
                quantity=Decimal("0.000123456789"),
                status=TradeStatus.PENDING,
            )
        )
        await db_session.commit()
        db_session.expire_all()

        trade = (await db_session.execute(
            select(Trade).where(Trade.signal_id == "sig-1")
        )).scalar_one()

        assert trade.quantity == Decimal("0.000123456789")
        assert trade.status is TradeStatus.PENDING
        assert trade.order_type == "MARKET"
        assert trade.created_at is not None
        assert trade.binance_order_id is None

    async def test_user_trade_relationship_works_in_both_directions(
        self, db_session: AsyncSession, user: User
    ):
        trade = Trade(
            user_id=user.id,
            signal_id="rel-1",
            client_order_id="cat-rel-1",
            symbol="BTCUSDT",
            side="SELL",
            quantity=Decimal("0.5"),
            status=TradeStatus.FILLED,
        )
        db_session.add(trade)
        await db_session.commit()
        db_session.expire_all()

        # Relationships must be loaded explicitly in async SQLAlchemy: an
        # implicit lazy load has no greenlet to run its IO on and raises
        # MissingGreenlet rather than silently emitting a query.
        reloaded = (
            await db_session.execute(
                select(User)
                .where(User.id == user.id)
                .options(selectinload(User.trades).selectinload(Trade.user))
            )
        ).scalar_one()
        assert [t.signal_id for t in reloaded.trades] == ["rel-1"]
        assert reloaded.trades[0].user.email == user.email

    async def test_signal_id_is_unique(
        self, db_session: AsyncSession, user: User, session_factory: async_sessionmaker[AsyncSession]
    ):
        """The database-level guarantee behind duplicate protection."""
        db_session.add(
            Trade(
                user_id=user.id,
                signal_id="only-once",
                client_order_id="cat-a",
                symbol="BTCUSDT",
                side="BUY",
                quantity=Decimal("1"),
                status=TradeStatus.PENDING,
            )
        )
        await db_session.commit()

        other = session_factory()
        try:
            other.add(
                Trade(
                    user_id=user.id,
                    signal_id="only-once",
                    client_order_id="cat-b",
                    symbol="ETHUSDT",
                    side="SELL",
                    quantity=Decimal("2"),
                    status=TradeStatus.PENDING,
                )
            )
            with pytest.raises(IntegrityError):
                await other.commit()
        finally:
            await other.rollback()
            await other.close()

    async def test_client_order_id_is_unique(
        self, db_session: AsyncSession, user: User, session_factory: async_sessionmaker[AsyncSession]
    ):
        """Second line of defence: the id we send to Binance is unique too."""
        db_session.add(
            Trade(
                user_id=user.id,
                signal_id="sig-x",
                client_order_id="cat-shared",
                symbol="BTCUSDT",
                side="BUY",
                quantity=Decimal("1"),
                status=TradeStatus.PENDING,
            )
        )
        await db_session.commit()

        other = session_factory()
        try:
            other.add(
                Trade(
                    user_id=user.id,
                    signal_id="sig-y",
                    client_order_id="cat-shared",
                    symbol="BTCUSDT",
                    side="BUY",
                    quantity=Decimal("1"),
                    status=TradeStatus.PENDING,
                )
            )
            with pytest.raises(IntegrityError):
                await other.commit()
        finally:
            await other.rollback()
            await other.close()

    async def test_trade_requires_an_existing_user(self, db_session: AsyncSession):
        """The foreign key rejects an orphan trade."""
        db_session.add(
            Trade(
                user_id=999_999,
                signal_id="orphan",
                client_order_id="cat-orphan",
                symbol="BTCUSDT",
                side="BUY",
                quantity=Decimal("1"),
                status=TradeStatus.PENDING,
            )
        )
        with pytest.raises(IntegrityError):
            await db_session.commit()
        await db_session.rollback()

    async def test_deleting_a_user_cascades_to_their_trades(
        self, db_session: AsyncSession, user: User
    ):
        db_session.add(
            Trade(
                user_id=user.id,
                signal_id="cascade-1",
                client_order_id="cat-cascade",
                symbol="BTCUSDT",
                side="BUY",
                quantity=Decimal("1"),
                status=TradeStatus.PENDING,
            )
        )
        await db_session.commit()

        await db_session.delete(await db_session.get(User, user.id))
        await db_session.commit()

        assert (await db_session.execute(select(Trade))).scalars().all() == []


class TestTradeStatusModel:
    async def test_terminal_statuses_are_marked_terminal(self):
        terminal = {
            TradeStatus.FILLED,
            TradeStatus.CANCELLED,
            TradeStatus.REJECTED,
            TradeStatus.EXPIRED,
            TradeStatus.FAILED,
        }
        in_flight = {
            TradeStatus.PENDING,
            TradeStatus.UNKNOWN,
            TradeStatus.NEW,
            TradeStatus.PARTIALLY_FILLED,
        }

        # Every status is classified exactly once.
        assert terminal | in_flight == set(TradeStatus)
        assert not terminal & in_flight

        trade = Trade(status=TradeStatus.UNKNOWN)
        assert not trade.is_terminal
        trade.status = TradeStatus.FILLED
        assert trade.is_terminal
