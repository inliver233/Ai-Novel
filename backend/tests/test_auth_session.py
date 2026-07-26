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
from app.schemas.auth import DisableUserRequest
from app.services.auth_service import hash_password
from app.services.authentication import admin as auth_admin


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

    def test_decode_rejects_legacy_v1_cookie(self) -> None:
        value = encode_session_cookie(user_id="u1", expires_at=utc_now() + timedelta(minutes=5))
        self.assertIsNone(decode_session_cookie("v1." + value.split(".", 1)[1]))

    def test_decode_rejects_legacy_v2_cookie(self) -> None:
        value = encode_session_cookie(user_id="u1", expires_at=utc_now() + timedelta(minutes=5))
        self.assertIsNone(decode_session_cookie("v2." + value.split(".", 1)[1]))

    def test_legacy_v2_cookie_signed_with_raw_fernet_key_is_rejected(self) -> None:
        # backend-core#8：旧派生直接用 Fernet 密钥原文做 HMAC。域分离后该签名必须失效。
        import base64
        import hashlib
        import hmac
        import json
        from datetime import timezone

        now = utc_now()
        exp_ts = int((now + timedelta(minutes=5)).astimezone(timezone.utc).timestamp())
        payload = json.dumps(
            {"uid": "u1", "exp": exp_ts, "iat": int(now.timestamp() * 1_000_000), "sv": 0},
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        raw_key = base64.urlsafe_b64decode(settings.secret_encryption_key.encode("ascii"))
        sig = hmac.new(raw_key, payload, hashlib.sha256).digest()

        def b64url(data: bytes) -> str:
            return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

        legacy = f"v2.{b64url(payload)}.{b64url(sig)}"
        self.assertIsNone(decode_session_cookie(legacy, now=now))

    def test_rotating_master_key_invalidates_existing_sessions(self) -> None:
        from cryptography.fernet import Fernet

        now = utc_now()
        key_a = Fernet.generate_key().decode("ascii")
        key_b = Fernet.generate_key().decode("ascii")
        with patch.object(settings, "auth_session_signing_key", None):
            with patch.object(settings, "secret_encryption_key", key_a):
                value = encode_session_cookie(user_id="u1", expires_at=now + timedelta(minutes=5))
                self.assertIsNotNone(decode_session_cookie(value, now=now))
            with patch.object(settings, "secret_encryption_key", key_b):
                self.assertIsNone(decode_session_cookie(value, now=now))


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
            user = User(
                id=user_id,
                display_name=user_id,
                is_admin=is_admin,
                disabled_at=utc_now() if disabled else None,
            )
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

    def test_signed_cookie_for_missing_user_is_revoked(self) -> None:
        client = TestClient(self.app)
        expires_at = utc_now() + timedelta(minutes=5)
        client.cookies.set(
            settings.auth_cookie_user_id_name,
            encode_session_cookie(user_id="missing-user", expires_at=expires_at),
        )
        response = client.get("/api/auth/user")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "ACCOUNT_DISABLED")
        self.assertIn(settings.auth_cookie_user_id_name, response.headers.get("set-cookie", ""))

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

    def test_admin_can_disable_and_enable_oidc_only_user(self) -> None:
        with self.SessionLocal() as db:
            db.add(User(id="oidc-only", display_name="OIDC Only"))
            db.commit()
            request = type("Request", (), {"state": type("State", (), {"request_id": "rid"})()})()
            auth_admin.set_user_disabled(request, db, "oidc-only", DisableUserRequest(disabled=True))
            user = db.get(User, "oidc-only")
            assert user is not None and user.disabled_at is not None
            assert user.session_version == 1
            invalid_before = user.session_invalid_before
            auth_admin.set_user_disabled(request, db, "oidc-only", DisableUserRequest(disabled=False))
            db.refresh(user)
            assert user.disabled_at is None
            assert user.session_invalid_before == invalid_before

    def test_disabling_user_revokes_existing_cookie_and_requires_new_login(self) -> None:
        self._seed_user(user_id="u1", password="password123")
        client = TestClient(self.app)
        login = client.post("/api/auth/local/login", json={"user_id": "u1", "password": "password123"})
        self.assertEqual(login.status_code, 200)
        old_cookie = client.cookies.get(settings.auth_cookie_user_id_name)
        self.assertTrue(old_cookie)
        with self.SessionLocal() as db:
            user = db.get(User, "u1")
            assert user is not None
            disabled_at = utc_now()
            user.disabled_at = disabled_at
            user.session_invalid_before = disabled_at
            user.session_version += 1
            db.commit()
            self.assertEqual(user.session_version, 1)

        revoked = client.get("/api/auth/user")
        self.assertEqual(revoked.status_code, 401)
        self.assertEqual(revoked.json()["error"]["code"], "ACCOUNT_DISABLED")
        set_cookie = revoked.headers.get("set-cookie", "")
        self.assertIn(settings.auth_cookie_user_id_name, set_cookie)
        self.assertIn(settings.auth_cookie_expire_at_name, set_cookie)

        with self.SessionLocal() as db:
            user = db.get(User, "u1")
            assert user is not None
            user.disabled_at = None
            db.commit()
        client.cookies.set(settings.auth_cookie_user_id_name, str(old_cookie))
        still_revoked = client.post("/api/auth/refresh")
        self.assertEqual(still_revoked.status_code, 401)
        self.assertEqual(still_revoked.json()["error"]["code"], "ACCOUNT_DISABLED")
        client = TestClient(self.app)
        relogin = client.post("/api/auth/local/login", json={"user_id": "u1", "password": "password123"})
        self.assertEqual(relogin.status_code, 200)
        self.assertEqual(client.get("/api/auth/user").status_code, 200)

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
        # The registration conflict pre-check normally returns before commit. Patch it
        # to the id being registered so the
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
        self._seed_user(user_id="u1", password="password123")
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

        with (
            patch.object(settings, "app_env", "prod"),
            patch.object(settings, "auth_cookie_samesite", "strict"),
            patch("app.services.authentication.local.enforce_auth_rate_limit"),
        ):
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
