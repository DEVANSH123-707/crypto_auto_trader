"""Unit tests for the real Binance client.

These exercise ``BinanceClient`` itself - signing, quantity formatting, and the
translation of each HTTP outcome into the right exception. No network calls are
made: httpx's ``MockTransport`` answers every request in-process, so the client
code runs for real against scripted responses.
"""

from __future__ import annotations

import hashlib
import hmac
from decimal import Decimal

import httpx2
import pytest

from app.core.numbers import format_decimal
from app.services.binance_service import (
    BinanceAuthError,
    BinanceClient,
    BinanceRateLimitError,
    BinanceRejectedError,
    BinanceUnavailableError,
    BinanceUncertainError,
)

API_KEY = "test-api-key"
SECRET_KEY = "test-secret-key"

FILLED_ORDER = {
    "symbol": "BTCUSDT",
    "orderId": 28,
    "clientOrderId": "cat-abc123",
    "transactTime": 1_757_000_000_000,
    "price": "0.00000000",
    "origQty": "0.00100000",
    "executedQty": "0.00100000",
    "cummulativeQuoteQty": "45.00000000",
    "status": "FILLED",
    "type": "MARKET",
    "side": "BUY",
}


def build_client(handler) -> BinanceClient:
    """A real BinanceClient whose transport is scripted."""
    client = BinanceClient(
        api_key=API_KEY,
        secret_key=SECRET_KEY,
        base_url="https://testnet.binance.vision",
        dry_run=False,
    )
    client._client = httpx2.AsyncClient(
        base_url="https://testnet.binance.vision",
        transport=httpx2.MockTransport(handler),
        headers={"X-MBX-APIKEY": API_KEY},
    )
    return client


def respond(status_code: int, json_body: dict | None = None, **kwargs):
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status_code, json=json_body or {}, **kwargs)

    return handler


class TestSigning:
    async def test_request_is_signed_and_carries_the_api_key(self):
        captured: dict[str, httpx2.Request] = {}

        def handler(request: httpx2.Request) -> httpx2.Response:
            captured["request"] = request
            return httpx2.Response(200, json=FILLED_ORDER)

        async with build_client(handler) as client:
            await client.place_market_order(
                symbol="BTCUSDT",
                side="BUY",
                quantity=Decimal("0.001"),
                client_order_id="cat-abc123",
            )

        request = captured["request"]
        assert request.headers["X-MBX-APIKEY"] == API_KEY

        params = request.url.params
        assert params["symbol"] == "BTCUSDT"
        assert params["type"] == "MARKET"
        assert params["newClientOrderId"] == "cat-abc123"
        assert "timestamp" in params and "recvWindow" in params

        # Recompute the HMAC over everything except the signature itself.
        query = str(request.url.query, "utf-8")
        payload, _, signature = query.rpartition("&signature=")
        expected = hmac.new(
            SECRET_KEY.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        assert signature == expected

    async def test_the_secret_key_never_appears_in_the_request(self):
        captured: dict[str, httpx2.Request] = {}

        def handler(request: httpx2.Request) -> httpx2.Response:
            captured["request"] = request
            return httpx2.Response(200, json=FILLED_ORDER)

        async with build_client(handler) as client:
            await client.place_market_order(
                symbol="BTCUSDT",
                side="BUY",
                quantity=Decimal("0.001"),
                client_order_id="cat-abc123",
            )

        request = captured["request"]
        assert SECRET_KEY not in str(request.url)
        assert SECRET_KEY not in str(dict(request.headers))


class TestQuantityFormatting:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (Decimal("0.001"), "0.001"),
            (Decimal("0.00100000"), "0.001"),
            (Decimal("1"), "1"),
            (Decimal("1.50"), "1.5"),
            (Decimal("0.000000010"), "0.00000001"),
            (Decimal("100"), "100"),
        ],
    )
    async def test_plain_decimal_notation_only(self, value: Decimal, expected: str):
        """Binance rejects scientific notation like 1E-3."""
        formatted = format_decimal(value)

        assert formatted == expected
        assert "E" not in formatted and "e" not in formatted


class TestResponseHandling:
    async def test_a_filled_order_is_normalised(self):
        async with build_client(respond(200, FILLED_ORDER)) as client:
            result = await client.place_market_order(
                symbol="BTCUSDT",
                side="BUY",
                quantity=Decimal("0.001"),
                client_order_id="cat-abc123",
            )

        assert result.order_id == 28
        assert result.status == "FILLED"
        assert result.executed_quantity == Decimal("0.00100000")
        assert result.cumulative_quote_quantity == Decimal("45.00000000")

    async def test_a_business_rejection_raises_rejected(self):
        handler = respond(400, {"code": -1121, "msg": "Invalid symbol."})

        async with build_client(handler) as client:
            with pytest.raises( BinanceRejectedError ) as caught:
                await client.place_market_order(
                    symbol="NOPEUSDT",
                    side="BUY",
                    quantity=Decimal("1"),
                    client_order_id="cat-x",
                )

        assert caught.value.code == -1121
        assert caught.value.message == "Invalid symbol."

    async def test_bad_credentials_raise_auth_error(self):
        handler = respond(401, {"code": -2015, "msg": "Invalid API-key."})

        async with build_client(handler) as client:
            with pytest.raises(BinanceAuthError):
                await client.place_market_order(
                    symbol="BTCUSDT",
                    side="BUY",
                    quantity=Decimal("1"),
                    client_order_id="cat-x",
                )

    async def test_rate_limit_raises_with_retry_after(self):
        handler = respond(
            429,
            {"code": -1003, "msg": "Too many requests."},
            headers={"Retry-After": "30"},
        )

        async with build_client(handler) as client:
            with pytest.raises( BinanceRateLimitError ) as caught:
                await client.place_market_order(
                    symbol="BTCUSDT",
                    side="BUY",
                    quantity=Decimal("1"),
                    client_order_id="cat-x",
                )

        assert caught.value.retry_after_seconds == 30

    async def test_http_500_is_uncertain_not_failed(self):
        """Binance docs: on 5xx the execution status is UNKNOWN."""
        async with build_client(respond(500, {})) as client:
            with pytest.raises( BinanceUncertainError ):
                await client.place_market_order(
                    symbol="BTCUSDT",
                    side="BUY",
                    quantity=Decimal("1"),
                    client_order_id="cat-x",
                )

    async def test_a_read_timeout_is_uncertain(self):
        def handler(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ReadTimeout("timed out", request=request)

        async with build_client(handler) as client:
            with pytest.raises(BinanceUncertainError):
                await client.place_market_order(
                    symbol="BTCUSDT",
                    side="BUY",
                    quantity=Decimal("1"),
                    client_order_id="cat-x",
                )

    async def test_a_connect_error_is_unavailable_not_uncertain(self):
        """Nothing was ever sent, so the order definitively does not exist."""

        def handler(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ConnectError("connection refused", request=request)

        async with build_client(handler) as client:
            with pytest.raises(BinanceUnavailableError):
                await client.place_market_order(
                    symbol="BTCUSDT",
                    side="BUY",
                    quantity=Decimal("1"),
                    client_order_id="cat-x",
                )


class TestGetOrder:
    async def test_found_order_is_returned(self):
        async with build_client(respond(200, FILLED_ORDER)) as client:
            result = await client.get_order(symbol="BTCUSDT", client_order_id="cat-abc123")

        assert result is not None
        assert result.status == "FILLED"

    async def test_order_does_not_exist_returns_none(self):
        """-2013 is the proof that a submission never landed."""
        handler = respond(400, {"code": -2013, "msg": "Order does not exist."})

        async with build_client(handler) as client:
            assert await client.get_order(symbol="BTCUSDT", client_order_id="cat-x") is None

    async def test_other_errors_still_raise(self):
        """"Could not ask" must never be reported as "no such order"."""
        handler = respond(400, {"code": -1121, "msg": "Invalid symbol."})

        async with build_client(handler) as client:
            with pytest.raises(BinanceRejectedError):
                await client.get_order(symbol="NOPEUSDT", client_order_id="cat-x")

    async def test_it_queries_by_orig_client_order_id(self):
        captured: dict[str, httpx2.Request] = {}

        def handler(request: httpx2.Request) -> httpx2.Response:
            captured["request"] = request
            return httpx2.Response(200, json=FILLED_ORDER)

        async with build_client(handler) as client:
            await client.get_order(symbol="BTCUSDT", client_order_id="cat-abc123")

        params = captured["request"].url.params
        assert params["origClientOrderId"] == "cat-abc123"
        assert captured["request"].url.path == "/api/v3/order"


class TestDryRun:
    async def test_dry_run_uses_the_test_endpoint_and_creates_nothing(self):
        captured: dict[str, httpx2.Request] = {}

        def handler(request: httpx2.Request) -> httpx2.Response:
            captured["request"] = request
            return httpx2.Response(200, json={})

        client = build_client(handler)
        client._dry_run = True
        async with client:
            result = await client.place_market_order(
                symbol="BTCUSDT",
                side="BUY",
                quantity=Decimal("0.001"),
                client_order_id="cat-dry",
            )

        assert captured["request"].url.path == "/api/v3/order/test"
        assert result.order_id is None
        assert result.status == "DRY_RUN_VALIDATED"
