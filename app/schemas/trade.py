"""Response schemas for trades.

These are the only shapes a client ever sees. Building them from the ORM object
via ``from_attributes`` means new internal columns do not leak into the API
until they are added here on purpose.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from app.core.numbers import format_decimal
from app.db.models import OrderSide, TradeStatus


class TradeRead(BaseModel):
    """A single trade as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    signal_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    order_type: str
    # Serialised as a JSON string ("0.001") rather than a float, so no client
    # can lose precision by parsing it into a double.
    quantity: Decimal
    status: TradeStatus

    binance_order_id: int | None = None
    binance_status: str | None = None
    executed_quantity: Decimal | None = None
    cumulative_quote_quantity: Decimal | None = None

    error_code: str | None = None
    error_message: str | None = None

    submitted_at: datetime | None = None
    reconciled_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @field_serializer(
        "quantity", "executed_quantity", "cumulative_quote_quantity"
    )
    def _serialise_decimal(self, value: Decimal | None) -> str | None:
        """Emit "0.001", not "0.001000000000".

        NUMERIC(28, 12) pads on the way out of PostgreSQL. The padding is
        numerically meaningless and only makes the response harder to read.
        """
        return None if value is None else format_decimal(value)


class TradeListResponse(BaseModel):
    """Page of trades for ``GET /trades``."""

    items: list[TradeRead]
    total: int = Field(description="Total trades owned by the caller.")
    limit: int
    offset: int


class WebhookAcceptedResponse(BaseModel):
    """Result of processing one TradingView signal.

    ``trade.status`` carries the outcome. A signal that Binance rejected still
    returns 201 - the signal was accepted and recorded correctly, and the trade
    row says REJECTED. Only failures of *our* processing (bad secret, invalid
    payload, duplicate signal) return a 4xx.
    """

    detail: str
    trade: TradeRead
