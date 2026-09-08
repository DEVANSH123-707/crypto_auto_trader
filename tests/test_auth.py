"""Registration, login and JWT verification."""

from __future__ import annotations

from datetime import timedelta

import httpx2
import jwt
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password, verify_password
from app.db.models import User
from tests.conftest import DEFAULT_PASSWORD


class TestRegistration:
    async def test_register_creates_user(self, client: httpx2.AsyncClient, db_session: AsyncSession):
        response = await client.post(
            "/auth/register",
            json={"email": "new@example.com", "password": DEFAULT_PASSWORD},
        )

        assert response.status_code == 201
        body = response.json()
        assert body["email"] == "new@example.com"
        assert body["is_active"] is True
        # The response must not carry the password in any form.
        assert "password" not in body
        assert "password_hash" not in body

        stored = (await db_session.execute(
            select(User).where(User.email == "new@example.com")
        )).scalar_one()
        assert stored.password_hash != DEFAULT_PASSWORD
        assert stored.password_hash.startswith("$2b$")

    async def test_duplicate_registration_is_rejected(self, client: httpx2.AsyncClient):
        payload = {"email": "dupe@example.com", "password": DEFAULT_PASSWORD}
        assert (await client.post("/auth/register", json=payload)).status_code == 201

        response = await client.post("/auth/register", json=payload)

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "email_already_registered"

    async def test_email_is_case_insensitive(self, client: httpx2.AsyncClient):
        await client.post(
            "/auth/register",
            json={"email": "Mixed@Example.COM", "password": DEFAULT_PASSWORD},
        )
        response = await client.post(
            "/auth/register",
            json={"email": "mixed@example.com", "password": DEFAULT_PASSWORD},
        )
        assert response.status_code == 409

    @pytest.mark.parametrize(
        ("payload", "bad_field"),
        [
            ({"email": "not-an-email", "password": DEFAULT_PASSWORD}, "email"),
            ({"email": "a@b.com", "password": "short"}, "password"),
            ({"password": DEFAULT_PASSWORD}, "email"),
            ({"email": "a@b.com"}, "password"),
            ({"email": "a@b.com", "password": 12345}, "password"),
        ],
    )
    async def test_invalid_registration_payloads(
        self, client: httpx2.AsyncClient, payload: dict, bad_field: str
    ):
        response = await client.post("/auth/register", json=payload)

        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == "validation_failed"
        assert any(bad_field in detail["field"] for detail in error["details"])

    async def test_validation_errors_never_echo_the_password(self, client: httpx2.AsyncClient):
        """Pydantic's raw error list contains the submitted value; ours must not."""
        secret = "hunter2-hunter2"
        response = await client.post(
            "/auth/register", json={"email": "bad", "password": secret}
        )

        assert response.status_code == 422
        assert secret not in response.text


class TestLogin:
    async def test_login_returns_a_usable_token(self, client: httpx2.AsyncClient, user: User):
        response = await client.post(
            "/auth/login",
            json={"email": "alice@example.com", "password": DEFAULT_PASSWORD},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["token_type"] == "bearer"
        assert body["expires_in"] > 0

        me = await client.get(
            "/auth/me",
            headers={"Authorization": f"Bearer {body['access_token']}"},
        )
        assert me.status_code == 200
        assert me.json()["email"] == "alice@example.com"

    async def test_wrong_password_is_rejected(self, client: httpx2.AsyncClient, user: User):
        response = await client.post(
            "/auth/login",
            json={"email": "alice@example.com", "password": "wrong-password"},
        )

        assert response.status_code == 401
        assert response.json()["error"]["message"] == "Incorrect email or password."

    async def test_unknown_email_looks_identical_to_a_wrong_password(
        self, client: httpx2.AsyncClient, user: User
    ):
        """Otherwise login becomes an account-enumeration oracle."""
        unknown = await client.post(
            "/auth/login",
            json={"email": "nobody@example.com", "password": DEFAULT_PASSWORD},
        )
        wrong = await client.post(
            "/auth/login",
            json={"email": "alice@example.com", "password": "wrong-password"},
        )

        assert unknown.status_code == wrong.status_code == 401

        def public_part(response) -> tuple[str, str]:
            error = response.json()["error"]
            return error["code"], error["message"]

        assert public_part(unknown) == public_part(wrong)


class TestProtectedEndpoints:
    async def test_missing_token_is_401(self, client: httpx2.AsyncClient):
        response = await client.get("/auth/me")

        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"

    async def test_malformed_token_is_401(self, client: httpx2.AsyncClient):
        response = await client.get(
            "/auth/me", headers={"Authorization": "Bearer not.a.jwt"}
        )
        assert response.status_code == 401

    async def test_token_signed_with_another_key_is_401(self, client: httpx2.AsyncClient, user: User):
        """A forged token must fail signature verification.

        The attacker's key is deliberately 32+ bytes: PyJWT refuses to sign
        HS256 with anything shorter, which is also why JWT_SECRET has a
        32-character minimum in config.
        """
        forged = jwt.encode(
            {"sub": str(user.id), "type": "access", "exp": 9_999_999_999},
            "an-attacker-chosen-secret-of-sufficient-length",
            algorithm="HS256",
        )
        response = await client.get(
            "/auth/me", headers={"Authorization": f"Bearer {forged}"}
        )
        assert response.status_code == 401

    async def test_expired_token_is_401(self, client: httpx2.AsyncClient, user: User):
        expired, _ = create_access_token(
            user.id, expires_delta=timedelta(minutes=-5)
        )

        response = await client.get(
            "/auth/me", headers={"Authorization": f"Bearer {expired}"}
        )

        assert response.status_code == 401
        assert response.json()["error"]["message"] == "Token has expired"

    async def test_wrong_scheme_is_401(self, client: httpx2.AsyncClient, user: User):
        token, _ = create_access_token(user.id)
        response = await client.get("/auth/me", headers={"Authorization": f"Basic {token}"})
        assert response.status_code == 401

    async def test_token_for_a_deleted_user_is_401(
        self, client: httpx2.AsyncClient, user: User, db_session: AsyncSession
    ):
        token, _ = create_access_token(user.id)
        await db_session.delete(await db_session.get(User, user.id))
        await db_session.commit()

        response = await client.get(
            "/auth/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 401


class TestPasswordHashing:
    async def test_hash_is_salted_and_verifiable(self):
        first = hash_password(DEFAULT_PASSWORD)
        second = hash_password(DEFAULT_PASSWORD)

        # Different salts -> different hashes for the same password.
        assert first != second
        assert verify_password(DEFAULT_PASSWORD, first)
        assert verify_password(DEFAULT_PASSWORD, second)
        assert not verify_password("something else", first)

    async def test_password_over_bcrypt_limit_is_rejected_at_the_edge(
        self, client: httpx2.AsyncClient
    ):
        """bcrypt silently truncates past 72 bytes, so we refuse instead."""
        response = await client.post(
            "/auth/register",
            json={"email": "long@example.com", "password": "x" * 100},
        )
        assert response.status_code == 422
