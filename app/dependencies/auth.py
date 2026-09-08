"""The authentication dependency.

``get_current_user`` is the single place that turns an ``Authorization: Bearer
<jwt>`` header into a ``User`` row. Any route that declares it as a dependency
is automatically protected: FastAPI runs it before the handler, and if it
raises, the handler never executes.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AuthenticationError
from app.core.security import TokenError, decode_access_token
from app.db.database import get_db
from app.db.models import User
from app.services.auth_service import get_user_by_id

logger = logging.getLogger(__name__)

# auto_error=False so a missing header reaches our code as ``None`` instead of
# Starlette raising its own 403. That keeps every auth failure on the same
# error envelope and the same 401 status.
bearer_scheme = HTTPBearer(
    auto_error=False, description="Paste the JWT returned by POST /auth/login"
)


async def get_current_user(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """Resolve the caller's ``User`` from their bearer token.

    Steps, in order:

    1. read the ``Authorization: Bearer <token>`` header
    2. verify the token's HMAC signature with ``JWT_SECRET``
    3. verify it has not expired and is an access token
    4. read the ``sub`` claim and load that user
    5. confirm the account still exists and is active

    Every failure produces the same 401 so a caller cannot learn *why* a token
    was rejected.
    """
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("Missing bearer token.")

    if credentials.scheme.lower() != "bearer":
        raise AuthenticationError("Authorization scheme must be Bearer.")

    try:
        user_id = decode_access_token(credentials.credentials)
    except TokenError as exc:
        # Never log the token itself - it is a live credential.
        logger.warning("Rejected access token: %s", exc.reason)
        raise AuthenticationError(exc.reason) from None

    user = await get_user_by_id(db, user_id)
    if user is None:
        # Signature was valid, but the account is gone (deleted after issue).
        logger.warning("Valid token for missing user_id=%s", user_id)
        raise AuthenticationError("Invalid authentication token.")

    if not user.is_active:
        logger.warning("Token used for deactivated user_id=%s", user_id)
        raise AuthenticationError("This account is deactivated.")

    return user


#: Shorthand so routes read as ``current_user: CurrentUser``.
CurrentUser = Annotated[User, Depends(get_current_user)]
