"""A fake Binance exchange for the automated test suite.

**No test ever places a real order, on the testnet or anywhere else.** This
class implements the same surface as
:class:`app.services.binance_service.BinanceClient` and is injected via
``app.dependency_overrides``, so the routes, the trading service and the
reconciliation service all run their real code paths against it.

It can be told to behave like every failure mode that matters:

    fake.mode = "reject"      Binance refuses the order (terminal)
    fake.mode = "timeout"     no response - outcome UNKNOWN
    fake.mode = "rate_limit"  HTTP 429
    fake.mode = "unavailable" never connected - definitely no order

``place_order_calls`` is the important counter: it is how the duplicate and
timeout tests prove that a second order was never sent.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any, Literal

from app.services.binance_service import (
    BinanceOrderResult,
    BinanceRateLimitError,
    BinanceRejectedError,
    BinanceUnavailableError,
    BinanceUncertainError,
)

Mode = Literal["success", "reject", "timeout", "rate_limit", "unavailable"]


class FakeBinanceClient:
    """Drop-in replacement for ``BinanceClient`` in tests."""

    def __init__(self, mode: Mode = "success", latency_seconds: float = 0.0) -> None:
        self.mode: Mode = mode

        #: Simulated exchange round-trip time. Left at zero for the test
        #: suite; the benchmark harness sets it to a realistic value so that
        #: inline vs queued execution can be compared honestly.
        self.latency_seconds = latency_seconds

        #: Number of times an order was submitted. Duplicate-protection tests
        #: assert this stays at 1 (or 0).
        self.place_order_calls = 0
        #: Number of reconciliation queries performed.
        self.get_order_calls = 0
        #: Every client order id ever submitted, in order.
        self.submitted_client_order_ids: list[str] = []

        #: What ``get_order`` should return during reconciliation:
        #:   "not_found" -> Binance has no such order (never placed)
        #:   "found"     -> returns an order in ``reconcile_status``
        #:   "error"     -> the query itself fails, so nothing is learned
        self.reconcile_result: Literal["not_found", "found", "error"] = "not_found"
        self.reconcile_status = "FILLED"

        #: Values used to build a successful order response.
        self.next_order_id = 555_000_111
        self.fill_status = "FILLED"

    # -- the BinanceClient surface ---------------------------------------

    async def place_market_order(
        self,
        *,
        symbol: str,
        side: str,
        quantity: Decimal,
        client_order_id: str,
    ) -> BinanceOrderResult:
        self.place_order_calls += 1
        self.submitted_client_order_ids.append(client_order_id)

        if self.latency_seconds:
            await asyncio.sleep(self.latency_seconds)

        if self.mode == "reject":
            raise BinanceRejectedError(
                "Account has insufficient balance for requested action.",
                code=-2010,
                status_code=400,
            )
        if self.mode == "rate_limit":
            raise BinanceRateLimitError(
                "Too many requests; current limit is 1200 request weight per minute.",
                code=-1003,
                status_code=429,
                retry_after_seconds=30,
            )
        if self.mode == "unavailable":
            raise BinanceUnavailableError(
                "Could not reach Binance testnet.", code="connection_error"
            )
        if self.mode == "timeout":
            raise BinanceUncertainError(
                "Binance did not respond in time; order status is unknown.",
                code="timeout",
            )

        payload: dict[str, Any] = {
            "symbol": symbol,
            "orderId": self.next_order_id,
            "clientOrderId": client_order_id,
            "transactTime": 1_757_000_000_000,
            "price": "0.00000000",
            "origQty": str(quantity),
            "executedQty": str(quantity),
            "cummulativeQuoteQty": "45.00000000",
            "status": self.fill_status,
            "timeInForce": "GTC",
            "type": "MARKET",
            "side": side,
        }
        return BinanceOrderResult.from_payload(payload)

    async def get_order(
        self, *, symbol: str, client_order_id: str
    ) -> BinanceOrderResult | None:
        self.get_order_calls += 1

        if self.latency_seconds:
            await asyncio.sleep(self.latency_seconds)

        if self.reconcile_result == "error":
            # "We could not ask" - must never be read as "there is no order".
            raise BinanceUncertainError(
                "Binance did not respond in time; order status is unknown.",
                code="timeout",
            )
        if self.reconcile_result == "not_found":
            return None

        return BinanceOrderResult.from_payload(
            {
                "symbol": symbol,
                "orderId": self.next_order_id,
                "clientOrderId": client_order_id,
                "price": "0.00000000",
                "origQty": "0.00100000",
                "executedQty": "0.00100000",
                "cummulativeQuoteQty": "45.00000000",
                "status": self.reconcile_status,
                "type": "MARKET",
                "side": "BUY",
            }
        )

    async def ping(self) -> bool:
        return self.mode != "unavailable"

    async def get_server_time(self) -> int:
        return 1_757_000_000_000

    async def close(self) -> None:
        """No connection pool to release."""

    async def __aenter__(self) -> FakeBinanceClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()
