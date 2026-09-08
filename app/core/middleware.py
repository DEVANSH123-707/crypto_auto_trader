"""HTTP middleware: correlation ids and access logging."""

from __future__ import annotations

import logging
import time
import uuid

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging_config import set_request_id

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_HEADER_BYTES = REQUEST_ID_HEADER.lower().encode("latin-1")


class RequestContextMiddleware:
    """Give every request an id, log its outcome, echo the id back.

    Written as raw ASGI rather than as a ``BaseHTTPMiddleware`` subclass. The
    convenient base class wraps every request in an extra anyio task group and
    pipes the response through a memory stream, which costs real time per
    request and shows up at high concurrency. This does the same job by
    wrapping ``send``.

    Accepting an inbound ``X-Request-ID`` lets a reverse proxy or an upstream
    caller keep one id across systems; otherwise we mint a fresh one. The id
    goes into a ContextVar so every log line emitted while handling the request
    is stamped with it (see ``app.core.logging_config``).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        inbound = None
        for name, value in scope["headers"]:
            if name == _REQUEST_ID_HEADER_BYTES:
                inbound = value.decode("latin-1")
                break

        # Do not trust an arbitrary-length header value in our log format.
        request_id = (inbound or uuid.uuid4().hex)[:64]
        set_request_id(request_id)

        method: str = scope["method"]
        path: str = scope["path"]
        started = time.perf_counter()
        logger.info("--> %s %s", method, path)

        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message).append(REQUEST_ID_HEADER, request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # The exception handlers produce the response; this only records
            # timing for the failed request. Re-raise so they still run.
            elapsed_ms = (time.perf_counter() - started) * 1000
            logger.exception(
                "<-- %s %s failed after %.1fms", method, path, elapsed_ms
            )
            raise

        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.info("<-- %s %s %s (%.1fms)", method, path, status_code, elapsed_ms)
