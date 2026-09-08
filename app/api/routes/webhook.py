"""The TradingView webhook.

Authentication here is a shared secret, not a JWT: the caller is a machine
(TradingView's alert server), not a person, so there is nobody to log in.

**How to send the secret.** Preferred, and what any HTTP client should use:

    X-Webhook-Secret: <the value of WEBHOOK_SECRET>

TradingView's own alert webhooks cannot set custom request headers - the alert
dialog only lets you choose a URL and a message body. For that integration the
same secret may instead be included as a ``secret`` field inside the JSON body.
Both are compared in constant time; the body field is excluded from every
response schema and is never logged.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Header, status

from app.core.config import settings
from app.core.exceptions import AuthenticationError
from app.core.security import secrets_match
from app.dependencies.services import TradingServiceDep
from app.schemas.trade import TradeRead, WebhookAcceptedResponse
from app.schemas.webhook import TradingViewWebhookPayload

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["webhook"])


def _authenticate_webhook(header_secret: str | None, body_secret: str | None) -> None:
    """Accept the request only if it carries the configured shared secret."""
    if secrets_match(header_secret, settings.WEBHOOK_SECRET):
        return
    if secrets_match(body_secret, settings.WEBHOOK_SECRET):
        logger.debug("Webhook authenticated via body secret (TradingView mode)")
        return

    # Never echo what was received - that would put a guessed secret in the logs.
    logger.warning(
        "Webhook rejected: %s",
        "no secret supplied"
        if not (header_secret or body_secret)
        else "incorrect secret",
    )
    raise AuthenticationError("Invalid or missing webhook secret.")


@router.post(
    "/tradingview",
    response_model=WebhookAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Receive a TradingView alert and place a testnet order",
    responses={
        401: {"description": "Missing or invalid webhook secret"},
        409: {"description": "This signal_id was already processed"},
        422: {"description": "Payload failed schema or business validation"},
    },
)
async def tradingview_webhook(
    payload: TradingViewWebhookPayload,
    service: TradingServiceDep,
    x_webhook_secret: Annotated[
        str | None,
        Header(
            alias="X-Webhook-Secret",
            description="Shared secret. Required unless sent in the JSON body.",
        ),
    ] = None,
) -> WebhookAcceptedResponse:
    """Process one trading signal.

    The route does three things and no more: authenticate the caller, log that
    the signal arrived, and hand it to the trading service. Validation,
    duplicate detection, persistence and the Binance call all live in
    ``app.services.trading_service``.

    **On the status code.** 202 Accepted, not 201 Created: with
    ``WEBHOOK_ASYNC_EXECUTION`` on, the signal has been accepted and durably
    recorded but the order has not necessarily reached the exchange yet.
    ``trade.status`` carries whatever is known at the moment of the reply -
    usually ``PENDING``, and in inline mode the final status.

    A signal Binance refuses is still a 202: the webhook did its job and the
    trade row records ``REJECTED``. Returning an error would invite TradingView
    to re-send an alert that will fail identically. Only failures of *our*
    processing - a bad secret (401), an invalid payload (422), or a duplicate
    signal (409) - return 4xx.
    """
    _authenticate_webhook(x_webhook_secret, payload.secret)

    logger.info(
        "Webhook received: signal_id=%s %s %s qty=%s",
        payload.signal_id,
        payload.action.value,
        payload.symbol,
        payload.quantity,
    )

    trade = await service.process_signal(payload)

    return WebhookAcceptedResponse(
        detail=f"Signal accepted; trade is {trade.status.value}.",
        trade=TradeRead.model_validate(trade),
    )
