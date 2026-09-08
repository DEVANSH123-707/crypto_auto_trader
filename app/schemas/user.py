"""Pydantic schemas for user input and output.

The split between ``UserCreate`` and ``UserRead`` is deliberate: the request
model is the only place a password may appear, and the response model has no
``password_hash`` field at all, so a hash cannot leak by accident even if
someone returns the ORM object directly.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.core.security import BCRYPT_MAX_PASSWORD_BYTES, password_within_bcrypt_limit


class UserCreate(BaseModel):
    """Registration payload."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "email": "trader@example.com",
                "password": "correct-horse-battery-staple",
            }
        }
    )

    email: EmailStr
    password: str = Field(
        min_length=8,
        max_length=BCRYPT_MAX_PASSWORD_BYTES,
        description="At least 8 characters, at most 72 bytes (a bcrypt limit).",
    )

    @field_validator("email")
    @classmethod
    def _normalise_email(cls, value: str) -> str:
        # Store one canonical form so Alice@x.com and alice@x.com are the same
        # account and the UNIQUE constraint actually means something.
        return value.strip().lower()

    @field_validator("password")
    @classmethod
    def _fits_bcrypt(cls, value: str) -> str:
        # max_length counts characters; bcrypt counts bytes. A 30-character
        # emoji password can still be over 72 bytes.
        if not password_within_bcrypt_limit(value):
            raise ValueError(
                f"Password must be at most {BCRYPT_MAX_PASSWORD_BYTES} bytes "
                "when UTF-8 encoded"
            )
        return value


class UserRead(BaseModel):
    """Public representation of a user. Note: no password field of any kind."""

    # from_attributes lets FastAPI build this straight from the ORM object.
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr
    is_active: bool
    created_at: datetime
