"""The trading workflow: signal in, trade row out.

The order of operations is chosen to survive partial failure, because
PostgreSQL and Binance are two independent systems and **no transaction spans
both of them**.

    validate business rules
        |
    INSERT trade as PENDING  (with its client_order_id)
        |
    COMMIT  <-- durable *before* any money moves
        |
    POST /api/v3/order to Binance
        |
    map the outcome onto a TradeStatus
        |
    COMMIT

Why the first commit matters: once Binance has been called, something exists in
the outside world that a database rollback cannot undo. Committing the PENDING
row first means that even if this process is killed mid-call, there is a
durable record - carrying the exact ``client_order_id`` that was sent - which
reconciliation can use later to ask Binance what actually happened.

**Two execution modes** (``WEBHOOK_ASYNC_EXECUTION``):

``True`` (default)
    ``process_signal`` returns as soon as the PENDING row is committed, and the
    exchange call is handed to the in-process executor
    (``app.services.execution_queue``). The webhook acknowledges in single-digit
    milliseconds; the trade is still tracked, still idempotent, still
    reconcilable.

``False``
    The exchange call happens inline and the caller waits for the final status.
    Kept because it is the honest baseline the benchmark compares against.

Either way the durability story is identical: the row is committed before
anything irreversible happens.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func as sa_func
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import (
    BusinessValidationError,
    ConfigurationError,
    DuplicateSignalError,
)
from app.db.models import Trade, TradeStatus, User
from app.schemas.webhook import TradingViewWebhookPayload
from app.services.binance_service import (
    BinanceClient,
    BinanceOrderResult,
    BinanceRateLimitError,
    BinanceRejectedError,
    BinanceUnavailableError,
    BinanceUncertainError,
)

logger = logging.getLogger(__name__)

#: Prefix for every client order id we generate, so orders placed by this
#: application are recognisable in the Binance UI.
CLIENT_ORDER_ID_PREFIX = "cat-"

#: Binance -> internal status mapping.
BINANCE_STATUS_MAP: dict[str, TradeStatus] = {
    "NEW": TradeStatus.NEW,
    "PARTIALLY_FILLED": TradeStatus.PARTIALLY_FILLED,
    "FILLED": TradeStatus.FILLED,
    "CANCELED": TradeStatus.CANCELLED,  # Binance spells it with one L
    "PENDING_CANCEL": TradeStatus.CANCELLED,
    "REJECTED": TradeStatus.REJECTED,
    "EXPIRED": TradeStatus.EXPIRED,
    "EXPIRED_IN_MATCH": TradeStatus.EXPIRED,
    # Returned only by our dry-run path (POST /api/v3/order/test).
    "DRY_RUN_VALIDATED": TradeStatus.FILLED,
}


def build_client_order_id(signal_id: str) -> str:
    """Derive a Binance ``newClientOrderId`` from a signal id.

    Deterministic on purpose: the same signal always produces the same client
    order id, which is exactly what makes recovery after a timeout safe - we
    can ask Binance about *this* id instead of guessing.

    A hash rather than the raw signal id because Binance limits the field to 36
    characters from a restricted alphabet, while ``signal_id`` is caller-
    supplied. ``cat-`` + 28 hex characters = 32 characters, well inside both.
    """
    digest = hashlib.sha256(signal_id.encode("utf-8")).hexdigest()[:28]
    return f"{CLIENT_ORDER_ID_PREFIX}{digest}"


# ---------------------------------------------------------------------------
# Webhook trade owner cache
# ---------------------------------------------------------------------------
# The owning account is fixed by configuration and effectively never changes,
# so looking it up on every webhook was a database round trip spent
# re-answering a constant. The id is cached for the process lifetime and
# re-resolved on a miss.
_owner_user_id: int | None = None


def reset_trade_owner_cache() -> None:
    """Forget the cached owner id. Used by tests, which rebuild the database."""
    global _owner_user_id
    _owner_user_id = None


class TradingService:
    """Coordinates validation, persistence and the Binance call.

    Constructed per request by ``app.dependencies.services.get_trading_service``
    with a database session and a Binance client. Injecting the client is what
    lets the test suite substitute a fake exchange without patching internals.
    """

    def __init__(self, db: AsyncSession, binance_client: BinanceClient) -> None:
        self.db = db
        self.binance = binance_client

    # ------------------------------------------------------------------
    # Business validation (distinct from Pydantic's schema validation)
    # ------------------------------------------------------------------

    def validate_signal(self, payload: TradingViewWebhookPayload) -> None:
        """Apply the rules that depend on *this deployment's* configuration.

        Pydantic already guaranteed the payload is a well-formed signal:
        required fields present, correct types, BUY/SELL only, quantity > 0,
        symbol shaped like a Binance pair. What it cannot know is which symbols
        this particular deployment is allowed to trade and how large an order
        it may place - that comes from configuration, so it is checked here.

        Deliberately not a coroutine: it is pure CPU work on data already in
        memory, so making it one would add scheduling overhead and buy nothing.
        """
        allowed = settings.allowed_symbols
        if payload.symbol not in allowed:
            raise BusinessValidationError(
                f"Symbol {payload.symbol} is not enabled for trading. "
                f"Allowed symbols: {', '.join(sorted(allowed))}."
            )

        minimum = Decimal(str(settings.MIN_ORDER_QUANTITY))
        maximum = Decimal(str(settings.MAX_ORDER_QUANTITY))

        if payload.quantity < minimum:
            raise BusinessValidationError(
                f"Quantity {payload.quantity} is below the minimum order size "
                f"of {minimum}."
            )
        if payload.quantity > maximum:
            raise BusinessValidationError(
                f"Quantity {payload.quantity} exceeds the maximum order size "
                f"of {maximum}. This is a safety limit, not a Binance limit."
            )

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def process_signal(self, payload: TradingViewWebhookPayload) -> Trade:
        """Handle one TradingView signal.

        Returns as soon as the trade is durable. Whether the exchange call has
        already happened depends on ``WEBHOOK_ASYNC_EXECUTION``; either way the
        returned ``Trade`` reflects everything known at that moment.
        """
        logger.info(
            "Processing signal_id=%s %s %s qty=%s",
            payload.signal_id,
            payload.action.value,
            payload.symbol,
            payload.quantity,
        )

        self.validate_signal(payload)

        owner_id = await self._resolve_trade_owner_id()
        trade = await self._create_pending_trade(payload, owner_id)

        if settings.WEBHOOK_ASYNC_EXECUTION:
            # Imported here rather than at module scope: the executor imports
            # this module for its submission logic.
            from app.services.execution_queue import get_trade_executor

            get_trade_executor().submit(trade.id)
            return trade

        claimed = await self.claim_for_submission(trade.id)
        if claimed is None:  # pragma: no cover - nothing else can hold it here
            return trade
        return await self.submit_to_binance(claimed)

    # ------------------------------------------------------------------
    # Step 1: durable PENDING record
    # ------------------------------------------------------------------

    async def _resolve_trade_owner_id(self) -> int:
        """Return the id of the account webhook trades are attributed to.

        The webhook is a machine-to-machine integration authenticated by a
        shared secret, not by a user's JWT, so the owning account comes from
        configuration (``WEBHOOK_TRADE_OWNER_EMAIL``). Every trade therefore
        has a real ``user_id``, and the ownership checks on ``GET /trades``
        work uniformly.

        Only the id is needed in order to insert a trade, so this selects an
        int rather than loading a whole ``User`` - and it is cached, because
        for a running process the answer is a constant.
        """
        global _owner_user_id
        if _owner_user_id is not None:
            return _owner_user_id

        email = settings.WEBHOOK_TRADE_OWNER_EMAIL
        result = await self.db.execute(select(User.id).where(User.email == email))
        owner_id = result.scalar_one_or_none()

        if owner_id is None:
            logger.error(
                "WEBHOOK_TRADE_OWNER_EMAIL points at an account that does not "
                "exist. Register it via POST /auth/register first."
            )
            raise ConfigurationError(
                "Webhook trade owner account is not registered on this server."
            )

        _owner_user_id = owner_id
        return owner_id

    async def _create_pending_trade(
        self, payload: TradingViewWebhookPayload, owner_id: int
    ) -> Trade:
        """Insert and commit the PENDING row, or detect a duplicate signal.

        **Insert first, ask questions only on failure.** An earlier version ran
        a "does this signal_id already exist?" SELECT before every insert. That
        query never provided the guarantee - two concurrent requests can both
        pass it, since nothing locks between the SELECT and the INSERT - so it
        was a database round trip per webhook spent on a check the UNIQUE
        constraint has to repeat anyway.

        Now the insert is attempted directly. The UNIQUE index on
        ``trades.signal_id`` decides the winner exactly as before; the loser
        gets an ``IntegrityError``, and only *then* is a query spent looking up
        the winning trade so the 409 can name it. The happy path is one
        statement; the duplicate path is unchanged from the caller's side.
        """
        trade = Trade(
            user_id=owner_id,
            signal_id=payload.signal_id,
            client_order_id=build_client_order_id(payload.signal_id),
            symbol=payload.symbol,
            side=payload.action,
            order_type="MARKET",
            quantity=payload.quantity,
            status=TradeStatus.PENDING,
        )
        self.db.add(trade)

        try:
            # COMMIT #1. From here on the trade is durable: a crash, a lost
            # response or a killed process can all be recovered from, because
            # the row (and its client_order_id) already exists on disk.
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            # Either a plain repeat or a lost race on the unique index.
            # Nothing was placed by us in either case.
            result = await self.db.execute(
                select(Trade.id).where(Trade.signal_id == payload.signal_id)
            )
            winner_id = result.scalar_one_or_none()
            logger.warning(
                "Duplicate signal rejected by the unique index: signal_id=%s "
                "(existing trade id=%s)",
                payload.signal_id,
                winner_id,
            )
            raise DuplicateSignalError(existing_trade_id=winner_id) from None

        # No refresh() here. Every server-generated column also carries a
        # Python-side default (see app/db/models.utcnow), so the ORM already
        # knows id, created_at and updated_at - another round trip saved, and
        # no risk of an implicit lazy load inside an async request.
        logger.info(
            "Trade created: id=%s signal_id=%s status=PENDING client_order_id=%s",
            trade.id,
            trade.signal_id,
            trade.client_order_id,
        )
        return trade

    # ------------------------------------------------------------------
    # Step 2: the external call
    # ------------------------------------------------------------------

    async def claim_for_submission(self, trade_id: int) -> Trade | None:
        """Take exclusive ownership of a trade before sending it to Binance.

        Returns the trade if this caller won the claim, ``None`` if somebody
        else already has it.

        This has to be atomic. Two executor workers can pull the same trade id
        off the queue, and a "load it, check the status, then send" sequence
        lets both of them read an unsubmitted PENDING row before either writes
        - the same time-of-check-to-time-of-use race that the signal_id UNIQUE
        constraint exists to prevent on the insert side. So the claim is a
        single conditional UPDATE and the winner is whoever the database says
        changed a row.

        Writing ``submitted_at`` *is* the claim, which is also what makes crash
        recovery unambiguous: ``submitted_at IS NULL`` means nothing was ever
        sent and re-submitting is safe.
        """
        now = datetime.now(UTC)
        result = await self.db.execute(
            update(Trade)
            .where(
                Trade.id == trade_id,
                Trade.status == TradeStatus.PENDING,
                Trade.submitted_at.is_(None),
            )
            .values(submitted_at=now, updated_at=now)
        )
        await self.db.commit()

        if result.rowcount != 1:
            return None

        # populate_existing so a stale copy in this session's identity map is
        # overwritten by what we just committed - in one query, not get()+refresh().
        return await self.db.get(Trade, trade_id, populate_existing=True)

    async def submit_to_binance(self, trade: Trade) -> Trade:
        """Send the order and record whatever came back.

        The caller must already hold the trade via :meth:`claim_for_submission`,
        which is what guarantees one order per signal even with several workers
        draining the queue.

        Every branch below ends with the trade in a status that honestly
        describes what we know - including "we do not know", which is a real
        and important answer.
        """
        try:
            result = await self.binance.place_market_order(
                symbol=trade.symbol,
                side=trade.side.value,
                quantity=trade.quantity,
                client_order_id=trade.client_order_id,
            )
        except BinanceRateLimitError as exc:
            # No order was created, but this is a transient condition: a human
            # or a scheduler may safely re-send the signal under a new id.
            return await self._finalise_failure(
                trade,
                TradeStatus.REJECTED,
                error_code=f"rate_limit:{exc.code}",
                error_message=exc.message,
            )
        except BinanceRejectedError as exc:
            # Binance understood and refused: bad symbol, insufficient balance,
            # LOT_SIZE filter, bad credentials. Terminal and unambiguous.
            return await self._finalise_failure(
                trade,
                TradeStatus.REJECTED,
                error_code=str(exc.code) if exc.code is not None else "rejected",
                error_message=exc.message,
            )
        except BinanceUnavailableError as exc:
            # We never connected, so no order can exist. Safe to call FAILED.
            return await self._finalise_failure(
                trade,
                TradeStatus.FAILED,
                error_code=str(exc.code) if exc.code is not None else "unavailable",
                error_message=exc.message,
            )
        except BinanceUncertainError as exc:
            # THE DANGEROUS CASE: timeout or HTTP 5xx. The order may be live.
            return await self._handle_uncertain_outcome(trade, exc)

        return await self._finalise_success(trade, result)

    # ------------------------------------------------------------------
    # Outcome handlers
    # ------------------------------------------------------------------

    async def _finalise_success(
        self, trade: Trade, result: BinanceOrderResult
    ) -> Trade:
        """Record a confirmed Binance response.

        If this commit fails, Binance has already executed something that no
        rollback can undo. That is logged at CRITICAL with the identifiers
        needed to recover, and the row stays in a reconcilable state so the
        reconciliation path can repair it. We do not pretend the two systems
        are transactional.
        """
        trade.binance_order_id = result.order_id
        trade.binance_status = result.status
        trade.executed_quantity = result.executed_quantity
        trade.cumulative_quote_quantity = result.cumulative_quote_quantity
        trade.status = BINANCE_STATUS_MAP.get(result.status, TradeStatus.NEW)
        trade.error_code = None
        trade.error_message = None

        try:
            # COMMIT #2.
            await self.db.commit()
        except SQLAlchemyError:
            await self.db.rollback()
            logger.critical(
                "BINANCE ORDER SUCCEEDED BUT DATABASE UPDATE FAILED. "
                "trade_id=%s signal_id=%s client_order_id=%s "
                "binance_order_id=%s binance_status=%s. The order is LIVE on "
                "the exchange; the row is still PENDING. Run reconciliation "
                "to repair it.",
                trade.id,
                trade.signal_id,
                trade.client_order_id,
                result.order_id,
                result.status,
            )
            raise

        logger.info(
            "Trade %s status PENDING -> %s (binance_order_id=%s)",
            trade.id,
            trade.status.value,
            trade.binance_order_id,
        )
        return trade

    async def _finalise_failure(
        self,
        trade: Trade,
        status: TradeStatus,
        *,
        error_code: str,
        error_message: str,
    ) -> Trade:
        previous = trade.status
        trade.status = status
        trade.error_code = error_code[:64]
        trade.error_message = error_message

        try:
            await self.db.commit()
        except SQLAlchemyError:
            await self.db.rollback()
            logger.critical(
                "Could not persist failure status for trade_id=%s signal_id=%s "
                "(no order was placed).",
                trade.id,
                trade.signal_id,
            )
            raise

        logger.warning(
            "Trade %s status %s -> %s (%s: %s)",
            trade.id,
            previous.value,
            status.value,
            error_code,
            error_message,
        )
        return trade

    async def _handle_uncertain_outcome(
        self, trade: Trade, exc: BinanceUncertainError
    ) -> Trade:
        """A timeout or 5xx: the order may or may not exist.

        The wrong move here is to retry the order - that is how one signal
        becomes two positions. The right move is to *ask*: mark the trade
        UNKNOWN, commit that fact, then immediately try one reconciliation
        query. If the query also fails, the trade simply stays UNKNOWN and the
        reconciliation job will pick it up later. Nothing is ever re-sent.
        """
        logger.error(
            "UNCERTAIN Binance outcome for trade_id=%s signal_id=%s "
            "client_order_id=%s (%s). Not retrying; reconciling instead.",
            trade.id,
            trade.signal_id,
            trade.client_order_id,
            exc.code,
        )

        trade.status = TradeStatus.UNKNOWN
        trade.error_code = str(exc.code) if exc.code is not None else "uncertain"
        trade.error_message = exc.message
        await self.db.commit()

        # Import here rather than at module scope: reconciliation imports this
        # module for its status map, so a top-level import would be circular.
        from app.services.reconciliation_service import reconcile_trade

        try:
            await reconcile_trade(self.db, self.binance, trade)
        except Exception:  # noqa: BLE001 - reconciliation is best-effort here
            logger.exception(
                "Immediate reconciliation failed for trade_id=%s; it remains "
                "UNKNOWN and will be retried by the reconciliation job.",
                trade.id,
            )

        return trade

    # ------------------------------------------------------------------
    # Queries used by the authenticated /trades endpoints
    # ------------------------------------------------------------------

    async def list_trades_for_user(
        self, user: User, *, limit: int, offset: int, status: TradeStatus | None
    ) -> tuple[list[Trade], int]:
        """Return one page of the caller's trades, newest first.

        Ownership is enforced in the WHERE clause rather than by filtering
        afterwards, so another user's rows are never even loaded.
        """
        conditions = [Trade.user_id == user.id]
        if status is not None:
            conditions.append(Trade.status == status)

        total_result = await self.db.execute(
            select(sa_func.count()).select_from(Trade).where(*conditions)
        )
        total = total_result.scalar_one()

        items_result = await self.db.execute(
            select(Trade)
            .where(*conditions)
            .order_by(Trade.created_at.desc(), Trade.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(items_result.scalars()), total

    async def get_trade_for_user(self, user: User, trade_id: int) -> Trade | None:
        """Fetch one trade *belonging to this user*.

        The ``user_id`` predicate is the authorization check. Returning None
        for both "does not exist" and "belongs to someone else" means the API
        answers 404 in both cases, so trade ids cannot be probed.
        """
        result = await self.db.execute(
            select(Trade).where(Trade.id == trade_id, Trade.user_id == user.id)
        )
        return result.scalar_one_or_none()


__all__ = [
    "BINANCE_STATUS_MAP",
    "TradingService",
    "build_client_order_id",
    "reset_trade_owner_cache",
]
