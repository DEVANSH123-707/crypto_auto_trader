"""Registration and login logic.

Routes call these functions; the functions own the database work and raise
domain errors. No HTTP status codes appear here.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AuthenticationError, EmailAlreadyRegisteredError
from app.core.security import create_access_token, hash_password, verify_password
from app.db.models import User
from app.schemas.auth import TokenResponse

logger = logging.getLogger(__name__)

# bcrypt is deliberately slow (~250 ms at cost factor 12) and it is CPU-bound,
# not I/O-bound. Calling it directly inside an `async def` would block the
# event loop for that whole time and stall every other in-flight request, so
# both hashing calls are pushed onto a worker thread. This is the one place in
# the request path where threads are still the right tool: `await` only helps
# when there is I/O to wait on, and here there is none.


async def _hash_password(password: str) -> str:
    return await asyncio.to_thread(hash_password, password)


async def _verify_password(plain_password: str, password_hash: str) -> bool:
    return await asyncio.to_thread(verify_password, plain_password, password_hash)


async def get_user_by_email(db: AsyncSession, email: str) -> User | None:
    result = await db.execute(
        select(User).where(User.email == email.strip().lower())
    )
    return result.scalar_one_or_none()


async def get_user_by_id(db: AsyncSession, user_id: int) -> User | None:
    return await db.get(User, user_id)


async def register_user(db: AsyncSession, email: str, password: str) -> User:
    """Create a new account.

    Two layers guard against duplicate emails, and both are needed:

    * the pre-check produces a friendly 409 in the ordinary case
    * the ``IntegrityError`` catch covers the race where two registrations for
      the same address are in flight at once. The UNIQUE index on
      ``users.email`` is what actually enforces it; the pre-check is just a
      nicer error message.
    """
    email = email.strip().lower()

    if await get_user_by_email(db, email) is not None:
        logger.info("Registration rejected: email already registered")
        raise EmailAlreadyRegisteredError()

    user = User(email=email, password_hash=await _hash_password(password))
    db.add(user)

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        logger.info("Registration lost a race on the users.email unique index")
        raise EmailAlreadyRegisteredError() from None

    logger.info("User registered: id=%s", user.id)
    return user


async def authenticate_user(db: AsyncSession, email: str, password: str) -> User:
    """Verify credentials and return the user, or raise ``AuthenticationError``.

    "No such user" and "wrong password" produce the identical error, so the
    endpoint cannot be used to enumerate which addresses have accounts.
    """
    user = await get_user_by_email(db, email)

    if user is None or not await _verify_password(password, user.password_hash):
        # Log the reason for operators, return one generic message to callers.
        logger.warning(
            "Failed login attempt (%s)",
            "unknown email" if user is None else "bad password",
        )
        raise AuthenticationError("Incorrect email or password.")

    if not user.is_active:
        logger.warning("Login blocked for deactivated user id=%s", user.id)
        raise AuthenticationError("This account is deactivated.")

    logger.info("Successful login: user_id=%s", user.id)
    return user


def issue_access_token(user: User) -> TokenResponse:
    token, expires_in = create_access_token(user.id)
    return TokenResponse(access_token=token, expires_in=expires_in)
