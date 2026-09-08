"""The protected /trades endpoints: authentication *and* authorization.

Authentication answers "who are you?". Authorization answers "may you see
this?". A valid token is not enough - these tests prove one user cannot read
another user's trades.
"""

from __future__ import annotations

from decimal import Decimal

import httpx2
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Trade, TradeStatus, User


async def make_trade(
    db: AsyncSession, user: User, signal_id: str, **overrides
) -> Trade:
    fields: dict = {
        "user_id": user.id,
        "signal_id": signal_id,
        "client_order_id": f"cat-{signal_id}",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "quantity": Decimal("0.001"),
        "status": TradeStatus.FILLED,
    }
    fields.update(overrides)
    trade = Trade(**fields)
    db.add(trade)
    await db.commit()
    await db.refresh(trade)
    return trade


class TestAuthenticationRequired:
    async def test_listing_without_a_token_is_401(self, client: httpx2.AsyncClient):
        assert (await client.get("/trades")).status_code == 401

    async def test_fetching_without_a_token_is_401(self, client: httpx2.AsyncClient):
        assert (await client.get("/trades/1")).status_code == 401


class TestListingOwnTrades:
    async def test_returns_only_my_trades(
        self,
        client: httpx2.AsyncClient,
        db_session: AsyncSession,
        owner: User,
        user: User,
        owner_headers: dict[str, str],
    ):
        await make_trade(db_session, owner, "mine-1")
        await make_trade(db_session, owner, "mine-2")
        await make_trade(db_session, user, "someone-elses")

        response = await client.get("/trades", headers=owner_headers)

        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 2
        assert {item["signal_id"] for item in body["items"]} == {"mine-1", "mine-2"}

    async def test_response_never_exposes_internal_fields(
        self,
        client: httpx2.AsyncClient,
        db_session: AsyncSession,
        owner: User,
        owner_headers: dict[str, str],
    ):
        await make_trade(db_session, owner, "mine-1")

        item = (await client.get("/trades", headers=owner_headers)).json()["items"][0]

        assert "password_hash" not in item
        assert "user_id" not in item  # the caller already knows who they are
        expected = {
            "id",
            "signal_id",
            "client_order_id",
            "symbol",
            "side",
            "order_type",
            "quantity",
            "status",
            "binance_order_id",
            "binance_status",
            "executed_quantity",
            "cumulative_quote_quantity",
            "error_code",
            "error_message",
            "submitted_at",
            "reconciled_at",
            "created_at",
            "updated_at",
        }
        assert set(item) == expected

    async def test_pagination(
        self,
        client: httpx2.AsyncClient,
        db_session: AsyncSession,
        owner: User,
        owner_headers: dict[str, str],
    ):
        for index in range(5):
            await make_trade(db_session, owner, f"sig-{index}")

        page = (await client.get("/trades?limit=2&offset=2", headers=owner_headers)).json()

        assert page["total"] == 5
        assert len(page["items"]) == 2
        assert page["limit"] == 2
        assert page["offset"] == 2

    async def test_status_filter(
        self,
        client: httpx2.AsyncClient,
        db_session: AsyncSession,
        owner: User,
        owner_headers: dict[str, str],
    ):
        await make_trade(db_session, owner, "filled-1")
        await make_trade(db_session, owner, "failed-1", status=TradeStatus.FAILED)

        response = await client.get("/trades?status=FAILED", headers=owner_headers)

        body = response.json()
        assert body["total"] == 1
        assert body["items"][0]["signal_id"] == "failed-1"

    async def test_invalid_status_filter_is_422(
        self, client: httpx2.AsyncClient, owner_headers: dict[str, str]
    ):
        response = await client.get("/trades?status=NOPE", headers=owner_headers)
        assert response.status_code == 422

    async def test_empty_list_for_a_user_with_no_trades(
        self, client: httpx2.AsyncClient, user_headers: dict[str, str]
    ):
        body = (await client.get("/trades", headers=user_headers)).json()
        assert body == {"items": [], "total": 0, "limit": 50, "offset": 0}


class TestAuthorization:
    async def test_cannot_read_another_users_trade(
        self,
        client: httpx2.AsyncClient,
        db_session: AsyncSession,
        owner: User,
        user: User,
        user_headers: dict[str, str],
    ):
        """The whole point: a valid token for Alice must not open Bob's trade."""
        someone_elses = await make_trade(db_session, owner, "not-yours")

        response = await client.get(f"/trades/{someone_elses.id}", headers=user_headers)

        # 404 rather than 403: a 403 would confirm the id exists, letting an
        # attacker enumerate the table.
        assert response.status_code == 404

    async def test_can_read_my_own_trade(
        self,
        client: httpx2.AsyncClient,
        db_session: AsyncSession,
        owner: User,
        owner_headers: dict[str, str],
    ):
        mine = await make_trade(db_session, owner, "mine")

        response = await client.get(f"/trades/{mine.id}", headers=owner_headers)

        assert response.status_code == 200
        assert response.json()["signal_id"] == "mine"

    async def test_missing_trade_is_404(
        self, client: httpx2.AsyncClient, owner_headers: dict[str, str]
    ):
        assert (await client.get("/trades/999999", headers=owner_headers)).status_code == 404

    async def test_cannot_reconcile_another_users_trade(
        self,
        client: httpx2.AsyncClient,
        db_session: AsyncSession,
        owner: User,
        user_headers: dict[str, str],
    ):
        someone_elses = await make_trade(db_session, owner, "not-yours")

        response = await client.post(
            f"/trades/{someone_elses.id}/reconcile", headers=user_headers
        )

        assert response.status_code == 404
