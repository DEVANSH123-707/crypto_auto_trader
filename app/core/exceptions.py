"""Application error types and the centralised exception handlers.

Design rule: **services raise, routes stay clean, handlers format.**

A service never builds an ``HTTPException`` and never knows about status codes
in the HTTP sense - it raises a domain error such as
:class:`DuplicateSignalError`. The handlers registered in
:func:`register_exception_handlers` turn those into a single, consistent JSON
envelope:

    {"error": {"code": "duplicate_signal",
               "message": "...",
               "details": [...],
               "request_id": "..."}}

Unexpected exceptions are logged with a full traceback server-side and reported
to the client as a bare 500 so internal details never leak.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging_config import get_request_id

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Domain errors
# ---------------------------------------------------------------------------


class AppError(Exception):
    """Base class for every error this application raises on purpose."""

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    error_code: str = "internal_error"
    message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        details: Any | None = None,
    ) -> None:
        self.message = message or self.__class__.message
        self.details = details
        super().__init__(self.message)


class AuthenticationError(AppError):
    """Caller is not authenticated (missing/invalid/expired credentials)."""

    status_code = status.HTTP_401_UNAUTHORIZED
    error_code = "authentication_failed"
    message = "Not authenticated."


class AuthorizationError(AppError):
    """Caller is authenticated but not allowed to touch this resource."""

    status_code = status.HTTP_403_FORBIDDEN
    error_code = "forbidden"
    message = "You do not have access to this resource."


class ResourceNotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    error_code = "not_found"
    message = "Resource not found."


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    error_code = "conflict"
    message = "The request conflicts with existing state."


class EmailAlreadyRegisteredError(ConflictError):
    error_code = "email_already_registered"
    message = "An account with this email already exists."


class DuplicateSignalError(ConflictError):
    """The webhook re-sent a signal_id we have already processed."""

    error_code = "duplicate_signal"
    message = "This signal has already been processed."

    def __init__(
        self, message: str | None = None, *, existing_trade_id: int | None = None
    ) -> None:
        super().__init__(message)
        self.existing_trade_id = existing_trade_id


class BusinessValidationError(AppError):
    """Payload is structurally valid but breaks a business rule.

    Kept separate from Pydantic's schema validation on purpose: "quantity must
    be a number" is a schema concern, "quantity must be below the per-order
    cap configured for this deployment" is a business concern.
    """

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    error_code = "business_validation_failed"
    message = "The request failed a business rule."


class ConfigurationError(AppError):
    """A deployment misconfiguration discovered at request time."""

    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    error_code = "configuration_error"
    message = "The server is misconfigured."


# ---------------------------------------------------------------------------
# Response envelope
# ---------------------------------------------------------------------------


def _error_response(
    status_code: int,
    error_code: str,
    message: str,
    details: Any | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "error": {
            "code": error_code,
            "message": message,
            "request_id": get_request_id(),
        }
    }
    if details is not None:
        body["error"]["details"] = details
    return JSONResponse(status_code=status_code, content=body, headers=headers)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def register_exception_handlers(app: FastAPI) -> None:
    """Attach every exception handler to ``app``."""

    @app.exception_handler(AppError)
    async def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        # 5xx is our bug; 4xx is the caller's. Log accordingly.
        if exc.status_code >= 500:
            logger.error("Application error: %s", exc.message, exc_info=exc)
        else:
            logger.info(
                "Request rejected (%s): %s", exc.error_code, exc.message
            )

        headers = None
        if isinstance(exc, AuthenticationError):
            # RFC 6750: a 401 must say which scheme the client should use.
            headers = {"WWW-Authenticate": "Bearer"}

        return _error_response(
            exc.status_code, exc.error_code, exc.message, exc.details, headers
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Pydantic/FastAPI request validation, including malformed JSON.

        The raw Pydantic error list contains an ``input`` field echoing the
        submitted value - which for /auth/register would be the plaintext
        password. Only location/message/type are forwarded to the client.
        """
        details = [
            {
                "field": ".".join(str(part) for part in error.get("loc", ())),
                "message": error.get("msg", "Invalid value"),
                "type": error.get("type", "value_error"),
            }
            for error in exc.errors()
        ]
        logger.info("Request validation failed: %s", details)
        return _error_response(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "validation_failed",
            "Request payload failed validation.",
            details,
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        _: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        """Keeps framework-raised errors (404 routing, 405, ...) on-format."""
        code = {
            status.HTTP_401_UNAUTHORIZED: "authentication_failed",
            status.HTTP_403_FORBIDDEN: "forbidden",
            status.HTTP_404_NOT_FOUND: "not_found",
            status.HTTP_405_METHOD_NOT_ALLOWED: "method_not_allowed",
        }.get(exc.status_code, "http_error")
        return _error_response(
            exc.status_code,
            code,
            str(exc.detail),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(SQLAlchemyError)
    async def handle_database_error(
        _: Request, exc: SQLAlchemyError
    ) -> JSONResponse:
        """Database failures: log everything, tell the client nothing.

        A driver error message can contain the connection string, table names
        and column values, so it is never forwarded.
        """
        logger.exception("Database error: %s", type(exc).__name__)
        return _error_response(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "database_unavailable",
            "A database error occurred. Please retry shortly.",
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception: %s", type(exc).__name__)
        return _error_response(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "internal_error",
            "An unexpected error occurred.",
        )
