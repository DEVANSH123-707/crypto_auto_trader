"""Logging setup and request-scoped correlation IDs.

Every log line carries a ``request_id``. The id is generated (or taken from an
inbound ``X-Request-ID`` header) by the middleware in ``app.core.middleware``,
stored in a :class:`contextvars.ContextVar`, and injected into every log record
by :class:`RequestIdFilter`.

That means one webhook call can be traced end to end - route, trading service,
Binance call, status update - by grepping a single id, without having to thread
a logger argument through every function.
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from typing import Final

from app.core.config import settings

# ``ContextVar`` values are isolated per asyncio task *and* per thread, so this
# stays correct whether a route is `async def` or a `def` running in FastAPI's
# threadpool.
_request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")

LOG_FORMAT: Final[str] = (
    "%(asctime)s %(levelname)-8s [%(request_id)s] %(name)s: %(message)s"
)


def set_request_id(request_id: str) -> None:
    _request_id_ctx.set(request_id)


def get_request_id() -> str:
    return _request_id_ctx.get()


class RequestIdFilter(logging.Filter):
    """Attach the current request id to every record.

    Implemented as a filter rather than a custom Logger so that records emitted
    by third-party libraries (uvicorn, sqlalchemy, httpx) also get the field and
    the formatter never raises a KeyError.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id()
        return True


def configure_logging() -> None:
    """Configure root logging once, at application startup."""
    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(RequestIdFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # uvicorn installs its own handlers; make them flow through ours so every
    # line has a request id and one consistent format.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True

    # SQLAlchemy echoes every statement at INFO when DB_ECHO is on; otherwise
    # keep it quiet so trading events stand out.
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if settings.DB_ECHO else logging.WARNING
    )
    # httpx2 logs one INFO line per request including the full URL. Binance
    # URLs contain the HMAC signature, so this is silenced deliberately.
    for http_logger in ("httpx2", "httpcore2", "httpx", "httpcore"):
        logging.getLogger(http_logger).setLevel(logging.WARNING)
