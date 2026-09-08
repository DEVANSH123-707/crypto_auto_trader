"""Schema for the inbound TradingView webhook payload.

This layer answers exactly one question: *is this JSON structurally a trading
signal?* Field presence, types, ranges and format live here.

It deliberately does **not** answer "is this symbol one we are allowed to
trade?" or "is this quantity within the per-order cap?" - those are business
rules that depend on deployment configuration and belong in the trading
service. See ``app/services/trading_service.py``.
"""

from __future__ import annotations

import re
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.db.models import OrderSide

#: Binance spot symbols are uppercase alphanumerics, e.g. BTCUSDT.
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{5,20}$")

#: Conservative: printable, URL-safe-ish characters only. The signal id also
#: seeds the Binance client order id, and it ends up in log lines.
SIGNAL_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


class TradingViewWebhookPayload(BaseModel):
    """The JSON body TradingView posts to ``/webhook/tradingview``."""

    model_config = ConfigDict(
        # Reject unknown keys: a typo like "quantiy" should be a loud 422, not
        # a silently dropped field followed by a wrong-sized order.
        extra="forbid",
        json_schema_extra={
            "example": {
                "signal_id": "tv-btc-20260908-0930",
                "symbol": "BTCUSDT",
                "action": "BUY",
                "quantity": 0.001,
            }
        },
    )

    signal_id: str = Field(
        min_length=1,
        max_length=64,
        description=(
            "Unique id for this alert. Re-sending the same signal_id never "
            "places a second order."
        ),
    )
    symbol: str = Field(description="Binance trading pair, e.g. BTCUSDT.")
    action: OrderSide = Field(description="BUY or SELL.")
    # Decimal, not float: 0.001 has no exact binary representation and an
    # order quantity must survive the round trip unchanged.
    quantity: Decimal = Field(
        gt=0,
        description="Base-asset quantity. Must be greater than zero.",
    )

    #: Optional fallback for the shared secret.
    #:
    #: TradingView's alert webhooks send a POST body and cannot set custom
    #: request headers, so an integration that cannot use the
    #: ``X-Webhook-Secret`` header may put the same secret here instead. It is
    #: excluded from every response schema and never logged.
    secret: str | None = Field(
        default=None,
        exclude=True,
        description=(
            "Shared secret, as an alternative to the X-Webhook-Secret header "
            "(TradingView alerts cannot send custom headers)."
        ),
    )

    @field_validator("signal_id")
    @classmethod
    def _validate_signal_id(cls, value: str) -> str:
        value = value.strip()
        if not SIGNAL_ID_PATTERN.fullmatch(value):
            raise ValueError(
                "signal_id may only contain letters, digits, '.', ':', '_' "
                "and '-' (max 64 characters)"
            )
        return value

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        value = value.strip().upper()
        if not SYMBOL_PATTERN.fullmatch(value):
            raise ValueError(
                "symbol must be 5-20 uppercase letters/digits, e.g. BTCUSDT"
            )
        return value

    @field_validator("action", mode="before")
    @classmethod
    def _normalise_action(cls, value: object) -> object:
        # TradingView strategy alerts commonly emit lowercase "buy"/"sell".
        if isinstance(value, str):
            return value.strip().upper()
        return value

    @field_validator("quantity")
    @classmethod
    def _validate_quantity(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("quantity must be a finite number")
        # 12 decimal places matches the Numeric(28, 12) column; more precision
        # than that would be silently rounded on insert.
        if -value.as_tuple().exponent > 12:
            raise ValueError("quantity supports at most 12 decimal places")
        return value
