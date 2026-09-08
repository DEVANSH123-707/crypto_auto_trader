"""Schemas for the login exchange."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class LoginRequest(BaseModel):
    """Credentials posted to ``POST /auth/login``."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "email": "trader@example.com",
                "password": "correct-horse-battery-staple",
            }
        }
    )

    email: EmailStr
    # No length constraints here on purpose: a login attempt should not reveal
    # anything about the password policy, and every wrong password produces the
    # same 401 regardless of shape.
    password: str = Field(min_length=1)

    @field_validator("email")
    @classmethod
    def _normalise_email(cls, value: str) -> str:
        return value.strip().lower()


class TokenResponse(BaseModel):
    """A signed access token, in the shape RFC 6750 clients expect."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Token lifetime in seconds.")
