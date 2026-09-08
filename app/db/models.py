"""SQLAlchemy ORM models: ``User`` and ``Trade``.

Written in SQLAlchemy 2.0 declarative style (``Mapped`` / ``mapped_column``),
which gives real type hints instead of untyped ``Column`` attributes.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


def utcnow() -> datetime:
    """Timezone-aware 'now', used as the Python-side column default.

    Every timestamp column carries **both** a Python-side ``default`` and a
    SQL ``server_default``. The server default is the backstop for rows
    inserted outside this application; the Python default is what lets the ORM
    know the value without asking the database for it after the INSERT.

    That second point matters twice over in an async application: it removes a
    database round trip from the request path, and it means reading
    ``trade.created_at`` after a commit can never trigger an implicit lazy
    load - which in async SQLAlchemy is not slow but a ``MissingGreenlet``
    error.
    """
    return datetime.now(UTC)


class OrderSide(enum.StrEnum):
    """Direction of a trade. Values match Binance's ``side`` parameter.

    ``StrEnum`` (Python 3.11+) means a member *is* its string, so it drops
    straight into a JSON response, a log line or a Binance query parameter.
    """

    BUY = "BUY"
    SELL = "SELL"


class TradeStatus(enum.StrEnum):
    """Lifecycle of a trade inside this system.

    Local-only states
        ``PENDING``   row committed, Binance has not been called yet
        ``UNKNOWN``   Binance was called but the outcome is genuinely unknown
                      (timeout or HTTP 5xx). Needs reconciliation - never retry.
        ``FAILED``    terminal: we know for certain no order exists

    States mirrored from Binance's ``status`` field
        ``NEW``, ``PARTIALLY_FILLED``, ``FILLED``, ``CANCELLED``,
        ``REJECTED``, ``EXPIRED``

    Only ``PENDING``, ``NEW``, ``PARTIALLY_FILLED`` and ``UNKNOWN`` are
    non-terminal; see :data:`TERMINAL_STATUSES`.
    """

    PENDING = "PENDING"
    UNKNOWN = "UNKNOWN"
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


#: Statuses that will never change again on their own.
TERMINAL_STATUSES: frozenset[TradeStatus] = frozenset(
    {
        TradeStatus.FILLED,
        TradeStatus.CANCELLED,
        TradeStatus.REJECTED,
        TradeStatus.EXPIRED,
        TradeStatus.FAILED,
    }
)

#: Statuses a background/manual reconciliation run should re-check.
RECONCILABLE_STATUSES: frozenset[TradeStatus] = frozenset(
    {
        TradeStatus.PENDING,
        TradeStatus.UNKNOWN,
        TradeStatus.NEW,
        TradeStatus.PARTIALLY_FILLED,
    }
)


class User(Base):
    """An account that can log in and read its own trades."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Unique + indexed: the index backs both the uniqueness guarantee and the
    # "find user by email" lookup that every login performs.
    email: Mapped[str] = mapped_column(
        String(320), unique=True, index=True, nullable=False
    )

    # Only ever a bcrypt hash. The plaintext password is never stored, never
    # logged, and never appears in any response schema.
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )

    trades: Mapped[list[Trade]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<User id={self.id} email={self.email!r}>"


class Trade(Base):
    """One trading signal and the order it produced (or failed to produce)."""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # ---- Idempotency ----------------------------------------------------
    # The heart of duplicate protection. TradingView may fire the same alert
    # more than once; this UNIQUE constraint is what makes a second insert
    # fail at the database level, so two concurrent requests cannot both
    # create a trade even if both passed the "does it exist?" check.
    signal_id: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )

    # Deterministically derived from signal_id and sent to Binance as
    # ``newClientOrderId``. Two things follow from that:
    #   * Binance itself rejects a duplicate submission (a second line of
    #     defence, independent of our database)
    #   * after a timeout we can ask Binance "did this order land?" by client
    #     id, without ever needing the exchange-assigned orderId
    client_order_id: Mapped[str] = mapped_column(
        String(36), unique=True, index=True, nullable=False
    )

    # ---- Order details --------------------------------------------------
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    side: Mapped[OrderSide] = mapped_column(
        # native_enum=False stores a VARCHAR with a CHECK constraint instead of
        # a PostgreSQL ENUM type. Adding a value later is then an ordinary
        # migration rather than an ALTER TYPE.
        SAEnum(
            OrderSide,
            native_enum=False,
            length=8,
            validate_strings=True,
            # Emit a CHECK constraint, so an out-of-range value is rejected by
            # PostgreSQL itself and not only by the Python layer.
            create_constraint=True,
        ),
        nullable=False,
    )
    order_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="MARKET", server_default="MARKET"
    )
    # Numeric, never float: binary floating point cannot represent 0.1 exactly
    # and quantities must round-trip byte for byte.
    quantity: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)

    status: Mapped[TradeStatus] = mapped_column(
        SAEnum(
            TradeStatus,
            native_enum=False,
            length=20,
            validate_strings=True,
            create_constraint=True,
        ),
        nullable=False,
        default=TradeStatus.PENDING,
        index=True,
    )

    # ---- Result from Binance -------------------------------------------
    # BigInteger: Binance order ids are 64-bit.
    binance_order_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, index=True
    )
    binance_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    executed_quantity: Mapped[Decimal | None] = mapped_column(
        Numeric(28, 12), nullable=True
    )
    # Total quote-asset spent/received, e.g. USDT for BTCUSDT.
    cumulative_quote_quantity: Mapped[Decimal | None] = mapped_column(
        Numeric(28, 12), nullable=True
    )

    # ---- Failure information -------------------------------------------
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ---- Timestamps -----------------------------------------------------
    #: When the order was handed to Binance (set just before the HTTP call).
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Last time reconciliation asked Binance about this trade.
    reconciled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=func.now(),
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )

    user: Mapped[User] = relationship(back_populates="trades")

    __table_args__ = (
        # "list my trades, newest first" - the query GET /trades runs.
        # PostgreSQL can scan a b-tree index backwards, so a plain ascending
        # composite index serves ORDER BY created_at DESC just as well.
        Index("ix_trades_user_id_created_at", "user_id", "created_at"),
        # "find everything still in flight" - the query reconciliation runs.
        Index("ix_trades_status_created_at", "status", "created_at"),
    )

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<Trade id={self.id} signal_id={self.signal_id!r} "
            f"{self.side.value} {self.quantity} {self.symbol} "
            f"status={self.status.value}>"
        )
