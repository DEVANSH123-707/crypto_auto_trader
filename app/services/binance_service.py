"""Binance Spot **TESTNET** REST client.

This module is the only place in the project that knows what Binance's HTTP API
looks like. It speaks HTTP and HMAC; it knows nothing about our database, our
ORM models or FastAPI. The trading service receives normalised results
(:class:`BinanceOrderResult`) or typed exceptions, so swapping the exchange
would not ripple outwards.

API reference (Spot testnet):
    base URL       https://testnet.binance.vision
    place order    POST /api/v3/order          (SIGNED)
    validate only  POST /api/v3/order/test     (SIGNED, creates nothing)
    query order    GET  /api/v3/order          (SIGNED)
    server time    GET  /api/v3/time           (public)

Signing: every SIGNED request carries the API key in the ``X-MBX-APIKEY``
header and a ``signature`` parameter that is the hex HMAC-SHA256 of the query
string, keyed by the API secret.

The error taxonomy is the important part of this file. Binance failures split
into three categories that must be handled *differently*:

* :class:`BinanceRejectedError` - Binance definitively refused the order.
  No order exists. Safe and correct to record as terminal.
* :class:`BinanceUnavailableError` - the request definitively never reached
  order placement (connection refused, DNS failure). Safe to record as failed.
* :class:`BinanceUncertainError` - a timeout or an HTTP 5xx. **The order may
  have been placed.** Binance's own documentation says of 5xx responses: "It is
  important to NOT treat this as a failure operation; the execution status is
  UNKNOWN and could have been a success." Never retried blindly; resolved by
  querying the order instead.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final
from urllib.parse import urlencode

import httpx2

from app.core.config import ALLOWED_BINANCE_HOSTS, settings
from app.core.numbers import format_decimal

logger = logging.getLogger(__name__)

# Binance's "order does not exist" code. During reconciliation this is the
# proof that a submission never landed.
ORDER_DOES_NOT_EXIST_CODE: Final[int] = -2013

#: Binance order statuses, mapped onto our TradeStatus by the trading service.
BINANCE_STATUS_FILLED: Final[str] = "FILLED"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BinanceError(Exception):
    """Base class for anything the Binance client raises."""

    def __init__(
        self,
        message: str,
        *,
        code: str | int | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


class BinanceRejectedError(BinanceError):
    """Binance understood the request and refused it (HTTP 4xx + error code).

    Examples: invalid symbol (-1121), insufficient balance (-2010), quantity
    below the symbol's LOT_SIZE filter (-1013). The order does not exist and
    never will; retrying the identical request would fail identically.
    """


class BinanceRateLimitError(BinanceRejectedError):
    """HTTP 429 (rate limit) or 418 (IP auto-ban).

    A subclass of "rejected" because no order was created, but distinct so the
    caller can back off rather than treat it as a permanent business failure.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str | int | None = None,
        status_code: int | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message, code=code, status_code=status_code)
        self.retry_after_seconds = retry_after_seconds


class BinanceAuthError(BinanceRejectedError):
    """HTTP 401 or a signature/API-key error code. A configuration problem."""


class BinanceUnavailableError(BinanceError):
    """The request provably never reached Binance's matching engine.

    Raised only for failures that happen *before* any bytes could have been
    acted upon: DNS failure, connection refused, TLS handshake failure.
    """


class BinanceUncertainError(BinanceError):
    """The outcome is genuinely unknown - the order may or may not exist.

    Raised for read timeouts (the request was sent, the response was lost) and
    for HTTP 5xx. The caller must NOT retry the order; it must reconcile.
    """


# ---------------------------------------------------------------------------
# Normalised result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BinanceOrderResult:
    """Exchange-agnostic view of an order, returned to the trading service."""

    order_id: int | None
    client_order_id: str
    symbol: str
    side: str
    status: str
    executed_quantity: Decimal
    cumulative_quote_quantity: Decimal
    raw: dict[str, Any]

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> BinanceOrderResult:
        def _decimal(key: str) -> Decimal:
            try:
                return Decimal(str(payload.get(key, "0") or "0"))
            except Exception:  # noqa: BLE001 - never fail on a cosmetic field
                return Decimal("0")

        raw_order_id = payload.get("orderId")
        return cls(
            order_id=int(raw_order_id) if raw_order_id is not None else None,
            client_order_id=str(payload.get("clientOrderId", "")),
            symbol=str(payload.get("symbol", "")),
            side=str(payload.get("side", "")),
            status=str(payload.get("status", "")),
            executed_quantity=_decimal("executedQty"),
            # Binance really does spell it with two m's.
            cumulative_quote_quantity=_decimal("cummulativeQuoteQty"),
            raw=payload,
        )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class BinanceClient:
    """Thin, **async** Binance Spot testnet client.

    Built on ``httpx2.AsyncClient``, so waiting on the exchange yields control
    back to the event loop instead of occupying a thread. One shared instance
    per process reuses its connection pool, which also avoids a TLS handshake
    on every order.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        secret_key: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
        recv_window_ms: int | None = None,
        dry_run: bool | None = None,
    ) -> None:
        self._api_key = api_key or settings.BINANCE_API_KEY
        self._secret_key = secret_key or settings.BINANCE_SECRET_KEY
        self._base_url = (base_url or settings.BINANCE_BASE_URL).rstrip("/")
        self._timeout = timeout_seconds or settings.BINANCE_TIMEOUT_SECONDS
        self._recv_window = recv_window_ms or settings.BINANCE_RECV_WINDOW_MS
        self._dry_run = (
            settings.BINANCE_DRY_RUN if dry_run is None else dry_run
        )

        self._assert_testnet()

        self._client = httpx2.AsyncClient(
            base_url=self._base_url,
            timeout=httpx2.Timeout(self._timeout),
            headers={
                "X-MBX-APIKEY": self._api_key,
                "User-Agent": "crypto-auto-trader/1.0",
            },
        )

    # -- safety ----------------------------------------------------------

    def _assert_testnet(self) -> None:
        """Second, runtime line of defence against real-money trading.

        Configuration already refuses to load a non-testnet URL. This check
        runs again here, right next to the code that sends orders, so the
        guarantee holds even if a client were constructed with an explicit
        ``base_url`` argument in some future code path.
        """
        host = httpx2.URL(self._base_url).host.lower()
        if host not in ALLOWED_BINANCE_HOSTS:
            raise RuntimeError(
                f"BinanceClient refused to start: {host!r} is not a Binance "
                f"testnet host. Allowed: {sorted(ALLOWED_BINANCE_HOSTS)}"
            )

    # -- plumbing --------------------------------------------------------

    def _sign(self, params: dict[str, Any]) -> str:
        """Return the hex HMAC-SHA256 of the encoded query string."""
        query = urlencode(params)
        return hmac.new(
            self._secret_key.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _signed_params(self, params: dict[str, Any]) -> dict[str, Any]:
        signed = {
            **params,
            "timestamp": int(time.time() * 1000),
            "recvWindow": self._recv_window,
        }
        # The signature covers every other parameter, so it is added last.
        signed["signature"] = self._sign(signed)
        return signed

    async def _request(
        self, method: str, path: str, params: dict[str, Any], *, signed: bool
    ) -> dict[str, Any]:
        """Perform one Binance call and translate every failure mode.

        ``params`` is never logged: for a signed request it contains the HMAC
        signature, and the API key travels in a header set at construction
        time. Only the method, path and outcome are logged.
        """
        request_params = self._signed_params(params) if signed else dict(params)

        try:
            response = await self._client.request(
                method, path, params=request_params
            )
        except (httpx2.ConnectError, httpx2.ConnectTimeout) as exc:
            # No connection was ever established, so no order can exist.
            logger.warning("Binance unreachable on %s %s: %s", method, path, exc)
            raise BinanceUnavailableError(
                "Could not reach Binance testnet.", code="connection_error"
            ) from exc
        except httpx2.TimeoutException as exc:
            # Read/write/pool timeout: the request was (or may have been) sent
            # and the response was lost. This is the dangerous case.
            logger.error(
                "Binance TIMEOUT on %s %s - outcome UNKNOWN, will not retry",
                method,
                path,
            )
            raise BinanceUncertainError(
                "Binance did not respond in time; order status is unknown.",
                code="timeout",
            ) from exc
        except httpx2.HTTPError as exc:
            logger.error("Binance transport error on %s %s: %s", method, path, exc)
            raise BinanceUncertainError(
                "Binance request failed in transport; order status is unknown.",
                code="transport_error",
            ) from exc

        return self._handle_response(response, method, path)

    def _handle_response(
        self, response: httpx2.Response, method: str, path: str
    ) -> dict[str, Any]:
        status_code = response.status_code

        if status_code >= 500:
            # Binance docs: "It is important to NOT treat this as a failure
            # operation; the execution status is UNKNOWN."
            logger.error(
                "Binance HTTP %s on %s %s - outcome UNKNOWN", status_code, method, path
            )
            raise BinanceUncertainError(
                "Binance returned a server error; order status is unknown.",
                code="server_error",
                status_code=status_code,
            )

        if status_code in (429, 418):
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            logger.warning(
                "Binance rate limit HTTP %s on %s %s (retry after %ss)",
                status_code,
                method,
                path,
                retry_after,
            )
            raise BinanceRateLimitError(
                "Binance rate limit exceeded.",
                code=_error_code(response),
                status_code=status_code,
                retry_after_seconds=retry_after,
            )

        if status_code >= 400:
            code, message = _error_code(response), _error_message(response)
            logger.warning(
                "Binance rejected %s %s: HTTP %s code=%s msg=%s",
                method,
                path,
                status_code,
                code,
                message,
            )
            error_cls = (
                BinanceAuthError
                if status_code in (401, 403) or code in (-2014, -2015, -1022)
                else BinanceRejectedError
            )
            raise error_cls(message, code=code, status_code=status_code)

        try:
            payload = response.json()
        except ValueError as exc:
            logger.error("Binance returned non-JSON on %s %s", method, path)
            raise BinanceUncertainError(
                "Binance returned an unreadable response.", code="bad_payload"
            ) from exc

        return payload if isinstance(payload, dict) else {"result": payload}

    # -- public API ------------------------------------------------------

    async def ping(self) -> bool:
        """Public connectivity check. Never raises."""
        try:
            await self._request("GET", "/api/v3/ping", {}, signed=False)
            return True
        except BinanceError:
            return False

    async def get_server_time(self) -> int:
        """Milliseconds since epoch, per Binance. Useful for clock-skew debugging."""
        payload = await self._request("GET", "/api/v3/time", {}, signed=False)
        return int(payload["serverTime"])

    async def place_market_order(
        self,
        *,
        symbol: str,
        side: str,
        quantity: Decimal,
        client_order_id: str,
    ) -> BinanceOrderResult:
        """Place a MARKET order on the Binance **testnet**.

        ``client_order_id`` is sent as ``newClientOrderId``. It is derived
        deterministically from the signal id, which gives two guarantees:

        1. Binance refuses a second live order with the same id, so a duplicate
           submission cannot become a duplicate order even if our own database
           check were bypassed.
        2. After a timeout the order can be located by ``origClientOrderId``
           without ever having seen the exchange-assigned id.

        Raises one of the ``Binance*Error`` types; the caller maps each to a
        trade status.
        """
        self._assert_testnet()

        params: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            # Plain notation, no trailing zeros - Binance rejects "1E-3".
            "quantity": format_decimal(quantity),
            "newClientOrderId": client_order_id,
            # Ask for the full order object rather than the terse ACK form.
            "newOrderRespType": "FULL",
        }

        path = "/api/v3/order/test" if self._dry_run else "/api/v3/order"
        logger.info(
            "Binance order request: %s %s %s qty=%s client_order_id=%s%s",
            path,
            side,
            symbol,
            params["quantity"],
            client_order_id,
            " [DRY RUN]" if self._dry_run else "",
        )

        payload = await self._request("POST", path, params, signed=True)

        if self._dry_run:
            # POST /api/v3/order/test validates the order and returns {}.
            # Synthesise a result so the caller's code path is identical.
            logger.info(
                "Binance DRY RUN accepted order client_order_id=%s "
                "(no order was created)",
                client_order_id,
            )
            return BinanceOrderResult(
                order_id=None,
                client_order_id=client_order_id,
                symbol=symbol,
                side=side,
                status="DRY_RUN_VALIDATED",
                executed_quantity=Decimal("0"),
                cumulative_quote_quantity=Decimal("0"),
                raw=payload,
            )

        result = BinanceOrderResult.from_payload(payload)
        logger.info(
            "Binance order accepted: order_id=%s status=%s executed=%s",
            result.order_id,
            result.status,
            result.executed_quantity,
        )
        return result

    async def get_order(
        self, *, symbol: str, client_order_id: str
    ) -> BinanceOrderResult | None:
        """Look up an order by its client id. This is the reconciliation call.

        Returns ``None`` when Binance answers "order does not exist" (-2013),
        which is the definitive proof that a submission never landed. Any other
        failure raises, because "we could not ask" must never be mistaken for
        "there is no order".
        """
        try:
            payload = await self._request(
                "GET",
                "/api/v3/order",
                {"symbol": symbol, "origClientOrderId": client_order_id},
                signed=True,
            )
        except BinanceRejectedError as exc:
            if exc.code == ORDER_DOES_NOT_EXIST_CODE:
                logger.info(
                    "Binance has no order for client_order_id=%s (never placed)",
                    client_order_id,
                )
                return None
            raise

        return BinanceOrderResult.from_payload(payload)

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> BinanceClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _error_body(response: httpx2.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _error_code(response: httpx2.Response) -> int | str | None:
    return _error_body(response).get("code")


def _error_message(response: httpx2.Response) -> str:
    """Binance's human-readable reason, or a safe fallback.

    Binance error messages describe the order ("Filter failure: LOT_SIZE"),
    never our credentials, so they are safe to surface to the API caller and
    genuinely useful when debugging a strategy.
    """
    message = _error_body(response).get("msg")
    if isinstance(message, str) and message:
        return message
    return f"Binance returned HTTP {response.status_code}."


def _parse_retry_after(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None
