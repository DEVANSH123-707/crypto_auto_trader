"""Webhook authentication and the two layers of validation.

Schema validation (Pydantic) and business validation (the trading service) are
tested separately, because they answer different questions and fail for
different reasons.
"""

from __future__ import annotations

import httpx2
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Trade
from tests.conftest import TEST_WEBHOOK_SECRET, signal_payload
from tests.fakes import FakeBinanceClient


async def trade_count(db: AsyncSession) -> int:
    result = await db.execute(select(func.count()).select_from(Trade))
    return result.scalar_one()


class TestWebhookAuthentication:
    async def test_correct_header_secret_is_accepted(
        self, client: httpx2.AsyncClient, owner, webhook_headers: dict[str, str]
    ):
        response = await client.post(
            "/webhook/tradingview", json=signal_payload(), headers=webhook_headers
        )
        assert response.status_code == 202

    async def test_secret_in_the_body_is_accepted(self, client: httpx2.AsyncClient, owner):
        """TradingView alerts cannot send custom headers, so the body works too."""
        response = await client.post(
            "/webhook/tradingview",
            json=signal_payload(secret=TEST_WEBHOOK_SECRET),
            # Deliberately no X-Webhook-Secret header.
        )
        assert response.status_code == 202

    async def test_secret_is_never_echoed_back(self, client: httpx2.AsyncClient, owner):
        response = await client.post(
            "/webhook/tradingview",
            json=signal_payload(secret=TEST_WEBHOOK_SECRET),
        )
        assert TEST_WEBHOOK_SECRET not in response.text

    async def test_missing_secret_is_rejected(
        self, client: httpx2.AsyncClient, owner, db_session: AsyncSession
    ):
        response = await client.post("/webhook/tradingview", json=signal_payload())

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "authentication_failed"
        assert await trade_count(db_session) == 0

    async def test_wrong_secret_is_rejected(
        self, client: httpx2.AsyncClient, owner, db_session: AsyncSession
    ):
        response = await client.post(
            "/webhook/tradingview",
            json=signal_payload(),
            headers={"X-Webhook-Secret": "definitely-not-the-secret"},
        )

        assert response.status_code == 401
        assert await trade_count(db_session) == 0

    async def test_authentication_happens_before_any_order(
        self, client: httpx2.AsyncClient, owner, fake_binance: FakeBinanceClient
    ):
        await client.post(
            "/webhook/tradingview",
            json=signal_payload(),
            headers={"X-Webhook-Secret": "wrong"},
        )
        assert fake_binance.place_order_calls == 0


class TestSchemaValidation:
    """Layer 1: is this JSON structurally a trading signal?"""

    async def test_malformed_json_is_422_not_500(
        self, client: httpx2.AsyncClient, webhook_headers: dict[str, str]
    ):
        response = await client.post(
            "/webhook/tradingview",
            content=b'{"signal_id": "x", "symbol": ',  # truncated JSON
            headers={**webhook_headers, "Content-Type": "application/json"},
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_failed"

    @pytest.mark.parametrize(
        "missing",
        ["signal_id", "symbol", "action", "quantity"],
    )
    async def test_missing_required_field(
        self, client: httpx2.AsyncClient, webhook_headers: dict[str, str], missing: str
    ):
        payload = signal_payload()
        del payload[missing]

        response = await client.post(
            "/webhook/tradingview", json=payload, headers=webhook_headers
        )

        assert response.status_code == 422
        details = response.json()["error"]["details"]
        assert any(missing in detail["field"] for detail in details)

    @pytest.mark.parametrize(
        "payload",
        [
            signal_payload(quantity="not-a-number"),
            signal_payload(quantity=0),
            signal_payload(quantity=-0.5),
            signal_payload(action="HOLD"),
            signal_payload(action=""),
            signal_payload(symbol="BTC/USDT"),
            signal_payload(symbol="btc"),
            signal_payload(signal_id=""),
            signal_payload(signal_id="has spaces"),
            signal_payload(signal_id="x" * 65),
        ],
        ids=[
            "quantity-not-numeric",
            "quantity-zero",
            "quantity-negative",
            "action-unsupported",
            "action-empty",
            "symbol-has-slash",
            "symbol-too-short",
            "signal-id-empty",
            "signal-id-has-spaces",
            "signal-id-too-long",
        ],
    )
    async def test_invalid_values_are_rejected(
        self, client: httpx2.AsyncClient, webhook_headers: dict[str, str], payload: dict
    ):
        response = await client.post(
            "/webhook/tradingview", json=payload, headers=webhook_headers
        )
        assert response.status_code == 422

    async def test_unknown_field_is_rejected(
        self, client: httpx2.AsyncClient, webhook_headers: dict[str, str]
    ):
        """A typo like "quantiy" must be loud, not silently dropped."""
        payload = signal_payload()
        payload["quantiy"] = 5
        response = await client.post(
            "/webhook/tradingview", json=payload, headers=webhook_headers
        )
        assert response.status_code == 422

    async def test_lowercase_action_is_normalised(
        self, client: httpx2.AsyncClient, owner, webhook_headers: dict[str, str]
    ):
        """TradingView strategy alerts commonly emit lowercase "buy"."""
        response = await client.post(
            "/webhook/tradingview",
            json=signal_payload(action="sell", symbol="ethusdt"),
            headers=webhook_headers,
        )

        assert response.status_code == 202
        trade = response.json()["trade"]
        assert trade["side"] == "SELL"
        assert trade["symbol"] == "ETHUSDT"

    async def test_quantity_precision_survives_the_round_trip(
        self, client: httpx2.AsyncClient, owner, webhook_headers: dict[str, str]
    ):
        """Decimal, not float: 0.000123456789 must come back unchanged."""
        response = await client.post(
            "/webhook/tradingview",
            json=signal_payload(quantity="0.000123456789"),
            headers=webhook_headers,
        )

        assert response.status_code == 202
        assert response.json()["trade"]["quantity"] == "0.000123456789"


class TestBusinessValidation:
    """Layer 2: rules that depend on this deployment's configuration."""

    async def test_symbol_outside_the_allowlist_is_rejected(
        self,
        client: httpx2.AsyncClient,
        owner,
        webhook_headers: dict[str, str],
        db_session: AsyncSession,
        fake_binance: FakeBinanceClient,
    ):
        # DOGEUSDT is a perfectly valid Binance symbol - it is just not one
        # this deployment has enabled, which Pydantic cannot know.
        response = await client.post(
            "/webhook/tradingview",
            json=signal_payload(symbol="DOGEUSDT"),
            headers=webhook_headers,
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "business_validation_failed"
        assert await trade_count(db_session) == 0
        assert fake_binance.place_order_calls == 0

    async def test_quantity_above_the_safety_cap_is_rejected(
        self,
        client: httpx2.AsyncClient,
        owner,
        webhook_headers: dict[str, str],
        fake_binance: FakeBinanceClient,
    ):
        response = await client.post(
            "/webhook/tradingview",
            json=signal_payload(quantity=999),
            headers=webhook_headers,
        )

        assert response.status_code == 422
        assert "maximum order size" in response.json()["error"]["message"]
        assert fake_binance.place_order_calls == 0

    async def test_quantity_below_the_minimum_is_rejected(
        self, client: httpx2.AsyncClient, owner, webhook_headers: dict[str, str]
    ):
        response = await client.post(
            "/webhook/tradingview",
            json=signal_payload(quantity="0.000000001"),
            headers=webhook_headers,
        )

        assert response.status_code == 422
        assert "minimum order size" in response.json()["error"]["message"]

    async def test_business_validation_runs_before_the_trade_is_created(
        self, client: httpx2.AsyncClient, owner, webhook_headers: dict[str, str], db_session
    ):
        """A rejected signal must leave no row behind to block a later retry."""
        await client.post(
            "/webhook/tradingview",
            json=signal_payload(symbol="DOGEUSDT"),
            headers=webhook_headers,
        )
        assert await trade_count(db_session) == 0
