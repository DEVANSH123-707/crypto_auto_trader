"""Authentication routes.

Routes stay thin: validate (Pydantic does it), delegate to the service, shape
the response. All the interesting logic lives in ``app.services.auth_service``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.dependencies.auth import CurrentUser
from app.schemas.auth import LoginRequest, TokenResponse
from app.schemas.user import UserCreate, UserRead
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])

DbSession = Annotated[AsyncSession, Depends(get_db)]


@router.post(
    "/register",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account",
    responses={409: {"description": "Email already registered"}},
)
async def register(payload: UserCreate, db: DbSession) -> UserRead:
    """Register a new user.

    The password is hashed with bcrypt before it touches the database, and the
    response model has no password field, so neither the plaintext nor the hash
    can leave the process.
    """
    user = await auth_service.register_user(db, payload.email, payload.password)
    return UserRead.model_validate(user)


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Exchange credentials for a JWT",
    responses={401: {"description": "Incorrect email or password"}},
)
async def login(payload: LoginRequest, db: DbSession) -> TokenResponse:
    """Verify credentials and return a signed access token.

    Send the token on protected requests as ``Authorization: Bearer <token>``.
    In Swagger UI, click **Authorize** and paste the token value.
    """
    user = await auth_service.authenticate_user(db, payload.email, payload.password)
    return auth_service.issue_access_token(user)


@router.get(
    "/me",
    response_model=UserRead,
    summary="Who am I?",
    responses={401: {"description": "Missing, invalid or expired token"}},
)
async def read_current_user(current_user: CurrentUser) -> UserRead:
    """Smallest possible protected endpoint - handy for checking a token."""
    return UserRead.model_validate(current_user)
