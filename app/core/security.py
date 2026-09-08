"""Password hashing and JWT creation/verification.

Two independent concerns live here, both purely cryptographic:

* password hashing with bcrypt (adaptive, salted, slow by design)
* signing and verifying JSON Web Tokens with the ``JWT_SECRET`` from config

Nothing in this module touches the database or FastAPI. That keeps it trivially
unit-testable and makes the dependency direction obvious: routes -> services ->
security, never the other way around.
"""

from __future__ import annotations

import hmac
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import bcrypt
import jwt
from jwt import ExpiredSignatureError, InvalidTokenError

from app.core.config import settings

logger = logging.getLogger(__name__)

# bcrypt only ever looks at the first 72 *bytes* of the password. Anything
# longer is silently truncated, which would mean two different long passwords
# could authenticate each other. We reject over-long passwords instead.
BCRYPT_MAX_PASSWORD_BYTES: Final[int] = 72

TOKEN_TYPE_ACCESS: Final[str] = "access"


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------


def password_within_bcrypt_limit(password: str) -> bool:
    """True when the password fits in bcrypt's 72-byte input window."""
    return len(password.encode("utf-8")) <= BCRYPT_MAX_PASSWORD_BYTES


def hash_password(password: str) -> str:
    """Return a salted bcrypt hash of ``password``.

    The returned string embeds the algorithm, cost factor and salt, so nothing
    else needs to be stored alongside it.
    """
    if not password_within_bcrypt_limit(password):
        raise ValueError(
            f"Password must be at most {BCRYPT_MAX_PASSWORD_BYTES} bytes"
        )
    salt = bcrypt.gensalt()  # default cost factor (12 rounds)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain_password: str, password_hash: str) -> bool:
    """Constant-time check of a plaintext password against a stored hash."""
    if not password_within_bcrypt_limit(plain_password):
        # Cannot match anything we would ever have stored.
        return False
    try:
        return bcrypt.checkpw(
            plain_password.encode("utf-8"), password_hash.encode("utf-8")
        )
    except ValueError:
        # Malformed/corrupted hash in the database - treat as a failed login
        # rather than a 500, but make it visible in the logs.
        logger.warning("Stored password hash is not a valid bcrypt hash")
        return False


# ---------------------------------------------------------------------------
# JSON Web Tokens
# ---------------------------------------------------------------------------


def create_access_token(
    user_id: int, expires_delta: timedelta | None = None
) -> tuple[str, int]:
    """Create a signed access token for ``user_id``.

    Returns ``(token, expires_in_seconds)`` so the login response can tell the
    client how long the token is good for without decoding it.
    """
    lifetime = expires_delta or timedelta(
        minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
    )
    issued_at = datetime.now(UTC)
    expires_at = issued_at + lifetime

    payload: dict[str, Any] = {
        "sub": str(user_id),  # RFC 7519 says "sub" must be a string
        "type": TOKEN_TYPE_ACCESS,
        "iat": issued_at,
        "exp": expires_at,
        "jti": uuid.uuid4().hex,  # unique id, useful for future revocation
    }

    token = jwt.encode(
        payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM
    )
    return token, int(lifetime.total_seconds())


class TokenError(Exception):
    """Raised when a token cannot be trusted. Carries a safe public reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def decode_access_token(token: str) -> int:
    """Verify ``token`` and return the user id it refers to.

    Verifies the HMAC signature, the expiry, and that the token is an access
    token with a usable subject. Raises :class:`TokenError` otherwise - the
    caller turns that into a 401.
    """
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
            options={"require": ["exp", "sub"]},
        )
    except ExpiredSignatureError:
        raise TokenError("Token has expired") from None
    except InvalidTokenError:
        # Bad signature, wrong algorithm, malformed token, missing claims...
        # All of them collapse to the same public message so a caller cannot
        # probe the token format.
        raise TokenError("Invalid authentication token") from None

    if payload.get("type") != TOKEN_TYPE_ACCESS:
        raise TokenError("Invalid authentication token")

    subject = payload.get("sub")
    try:
        return int(subject)
    except (TypeError, ValueError):
        raise TokenError("Invalid authentication token") from None


# ---------------------------------------------------------------------------
# Shared-secret comparison (used by the TradingView webhook)
# ---------------------------------------------------------------------------


def secrets_match(provided: str | None, expected: str) -> bool:
    """Constant-time comparison so the secret cannot be guessed by timing."""
    if not provided:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))
