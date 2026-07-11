from __future__ import annotations

import unittest
from datetime import timedelta
from unittest.mock import patch
from typing import Generator

import pytest

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.api.routes.auth import router as auth_router
from app.core.auth_session import decode_session_cookie, encode_session_cookie
from app.core.config import settings
from app.core.errors import AppError
from app.db.session import get_db
from app.db.utils import utc_now
from app.main import app_error_handler, auth_session_middleware, validation_error_handler
from app.models.user import User
from app.models.user_password import UserPassword
from app.services.auth_service import hash_password


def _make_test_app(SessionLocal: sessionmaker) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def _request_id_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.request_id = "rid-test"
        return await call_next(request)

    app.middleware("http")(auth_session_middleware)
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.include_router(auth_router, prefix="/api")

    def _override_get_db() -> Generator[Session, None, None]:
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    return app


class TestAuthSessionCookie(unittest.TestCase):
    def test_encode_decode_roundtrip(self) -> None:
        now = utc_now()
        value = encode_session_cookie(user_id="u1", expires_at=now + timedelta(seconds=123))
        session = decode_session_cookie(value, now=now)
        self.assertIsNotNone(session)
        assert session is not None
        self.assertEqual(session.user_id, "u1")

    def test_decode_rejects_expired(self) -> None:
        now = utc_now()
        value = encode_session_cookie(user_id="u1", expires_at=now - timedelta(seconds=1))
        self.assertIsNone(decode_session_cookie(value, now=now))

    def test_decode_rejects_tampering(self) -> None:
        now = utc_now()
        value = encode_session_cookie(user_id="u1", expires_at=now + timedelta(seconds=60))
        parts = value.split(".")
        self.assertEqual(len(parts), 3)
        payload_b64 = parts[1]
        tampered_payload_b64 = payload_b64[:-1] + ("A" if payload_b64[-1] != "A" else "B")
        tampered = ".".join([parts[0], tampered_payload_b64, parts[2]])
        self.assertIsNone(decode_session_cookie(tampered, now=now))


class TestAuthEndpoints(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.addCleanup(engine.dispose)
        User.__table__.create(engine)
        UserPassword.__table__.create(engine)
        self.SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        self.app = _make_test_app(self.SessionLocal)

    def _seed_user(self, *, user_id: str, password: str, is_admin: bool = False, disabled: bool = False) -> None:
        with self.SessionLocal() as db:
            user = User(id=user_id, display_name=user_id, is_admin=is_admin)
            db.add(user)
            db.add(
                UserPassword(
                    user_id=user_id,
                    password_hash=hash_password(password),
                    disabled_at=utc_now() if disabled else None,
                )
            )
            db.commit()

    def test_auth_user_returns_401_when_not_logged_in(self) -> None:
        client = TestClient(self.app)
        resp = client.get("/api/auth/user")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error"]["code"], "UNAUTHORIZED")

    def test_login_then_auth_user(self) -> None:
        self._seed_user(user_id="u1", password="password123")
        client = TestClient(self.app)
        resp = client.post("/api/auth/local/login", json={"user_id": "u1", "password": "password123"})
        self.assertEqual(resp.status_code, 200)
        self.assertIsNotNone(client.cookies.get(settings.auth_cookie_user_id_name))
        self.assertIsNotNone(client.cookies.get(settings.auth_cookie_expire_at_name))

        resp2 = client.get("/api/auth/user")
        self.assertEqual(resp2.status_code, 200)
        data = resp2.json()["data"]
        self.assertEqual(data["user"]["id"], "u1")
        self.assertIn("session", data)

    def test_login_rejects_wrong_password_without_hash_leak(self) -> None:
        self._seed_user(user_id="u1", password="password123")
        client = TestClient(self.app)
        resp = client.post("/api/auth/local/login", json={"user_id": "u1", "password": "wrong-password"})
        self.assertEqual(resp.status_code, 401)
        body = resp.json()
        self.assertEqual(body["error"]["code"], "UNAUTHORIZED")
        self.assertNotIn("bcrypt", str(body))
        self.assertNotIn("$2", str(body))

    def test_disabled_user_cannot_login(self) -> None:
        self._seed_user(user_id="u1", password="password123", disabled=True)
        client = TestClient(self.app)
        resp = client.post("/api/auth/local/login", json={"user_id": "u1", "password": "password123"})
        self.assertEqual(resp.status_code, 401)

    def test_register_then_auth_user(self) -> None:
        client = TestClient(self.app)
        resp = client.post("/api/auth/local/register", json={"user_id": "u2", "password": "password123"})
        self.assertEqual(resp.status_code, 200)
        self.assertIsNotNone(client.cookies.get(settings.auth_cookie_user_id_name))
        self.assertIsNotNone(client.cookies.get(settings.auth_cookie_expire_at_name))

        resp2 = client.get("/api/auth/user")
        self.assertEqual(resp2.status_code, 200)
        data = resp2.json()["data"]
        self.assertEqual(data["user"]["id"], "u2")

    def test_register_rejects_existing_user(self) -> None:
        self._seed_user(user_id="u1", password="password123")
        client = TestClient(self.app)
        resp = client.post("/api/auth/local/register", json={"user_id": "u1", "password": "password123"})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["code"], "CONFLICT")

    def test_register_rejects_reserved_admin_user_id(self) -> None:
        # Admin-id reservation is opt-in via settings.auth_admin_user_id
        # (auth.py:350-352). Patch it to the id being registered so the
        # reservation fires and registration is forbidden.
        client = TestClient(self.app)
        with patch.object(settings, "auth_admin_user_id", "admin"):
            resp = client.post("/api/auth/local/register", json={"user_id": "admin", "password": "password123"})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "FORBIDDEN")

    def test_register_rejects_short_password(self) -> None:
        client = TestClient(self.app)
        resp = client.post("/api/auth/local/register", json={"user_id": "u2", "password": "short"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "VALIDATION_ERROR")
        with self.SessionLocal() as db:
            self.assertIsNone(db.get(User, "u2"))
            self.assertIsNone(db.get(UserPassword, "u2"))

    def test_change_password_rejects_short_new_password_without_updating_hash(self) -> None:
        self._seed_user(user_id="u1", password="password123")
        client = TestClient(self.app)
        login = client.post("/api/auth/local/login", json={"user_id": "u1", "password": "password123"})
        self.assertEqual(login.status_code, 200)

        with self.SessionLocal() as db:
            original_hash = db.get(UserPassword, "u1").password_hash

        resp = client.post(
            "/api/auth/password/change",
            json={"old_password": "password123", "new_password": "short"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "VALIDATION_ERROR")
        with self.SessionLocal() as db:
            self.assertEqual(db.get(UserPassword, "u1").password_hash, original_hash)

    def test_change_password(self) -> None:
        self._seed_user(user_id="u1", password="password123")
        client = TestClient(self.app)
        resp = client.post("/api/auth/local/login", json={"user_id": "u1", "password": "password123"})
        self.assertEqual(resp.status_code, 200)

        resp2 = client.post(
            "/api/auth/password/change",
            json={"old_password": "password123", "new_password": "new-password-123"},
        )
        self.assertEqual(resp2.status_code, 200)

        client.post("/api/auth/logout")
        resp_old = client.post("/api/auth/local/login", json={"user_id": "u1", "password": "password123"})
        self.assertEqual(resp_old.status_code, 401)
        resp_new = client.post("/api/auth/local/login", json={"user_id": "u1", "password": "new-password-123"})
        self.assertEqual(resp_new.status_code, 200)

    def test_admin_can_disable_user(self) -> None:
        self._seed_user(user_id="admin", password="admin-password-123", is_admin=True)
        self._seed_user(user_id="u1", password="password123")

        client = TestClient(self.app)
        resp = client.post("/api/auth/local/login", json={"user_id": "admin", "password": "admin-password-123"})
        self.assertEqual(resp.status_code, 200)

        resp2 = client.post("/api/auth/admin/users/u1/disable", json={"disabled": True})
        self.assertEqual(resp2.status_code, 200)

        client.post("/api/auth/logout")
        resp3 = client.post("/api/auth/local/login", json={"user_id": "u1", "password": "password123"})
        self.assertEqual(resp3.status_code, 401)

    def test_refresh_extends_when_near_expiry(self) -> None:
        client = TestClient(self.app)
        now = utc_now()
        near_exp = now + timedelta(seconds=max(1, settings.auth_refresh_threshold_seconds - 1))
        client.cookies.set(settings.auth_cookie_user_id_name, encode_session_cookie(user_id="u1", expires_at=near_exp))

        resp = client.post("/api/auth/refresh")
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()["data"]
        self.assertTrue(payload["refreshed"])
        self.assertGreater(payload["session"]["expire_at"], int(near_exp.timestamp()))


    def test_login_sets_secure_cookie_flags_in_prod(self) -> None:
        self._seed_user(user_id="u1", password="password123")
        client = TestClient(self.app)

        with patch.object(settings, "app_env", "prod"), patch.object(settings, "auth_cookie_samesite", "strict"):
            resp = client.post("/api/auth/local/login", json={"user_id": "u1", "password": "password123"})

        self.assertEqual(resp.status_code, 200)
        cookie_headers = resp.headers.get_list("set-cookie")
        self.assertGreaterEqual(len(cookie_headers), 2)
        for header in cookie_headers:
            lowered = header.lower()
            self.assertIn("httponly", lowered)
            self.assertIn("secure", lowered)
            self.assertIn("samesite=strict", lowered)


if __name__ == "__main__":
    unittest.main()
