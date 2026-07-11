from __future__ import annotations

import unittest
from typing import Generator
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from fastapi import FastAPI, Request
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.api.routes.auth import router as auth_router
from app.core.config import settings
from app.core.errors import AppError
from app.db.session import get_db
from app.main import app_error_handler, auth_session_middleware
from app.models.auth_external_account import AuthExternalAccount
from app.models.user import User
from app.models.user_password import UserPassword
from app.services.authentication import linuxdo, oidc_client

_LINUXDO_OIDC_NEXT_COOKIE = linuxdo._NEXT_COOKIE
_LINUXDO_OIDC_STATE_COOKIE = linuxdo._STATE_COOKIE
_LINUXDO_OIDC_VERIFIER_COOKIE = linuxdo._VERIFIER_COOKIE


def _make_test_app(SessionLocal: sessionmaker) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def _request_id_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.request_id = "rid-test"
        return await call_next(request)

    app.middleware("http")(auth_session_middleware)
    app.add_exception_handler(AppError, app_error_handler)
    app.include_router(auth_router, prefix="/api")

    def _override_get_db() -> Generator[Session, None, None]:
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    return app


class TestLinuxDoOidcEndpoints(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON;")
            finally:
                cursor.close()

        self.addCleanup(engine.dispose)
        User.__table__.create(engine)
        UserPassword.__table__.create(engine)
        AuthExternalAccount.__table__.create(engine)
        self.SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        self.app = _make_test_app(self.SessionLocal)

    def test_providers_linuxdo_disabled_by_default(self) -> None:
        client = TestClient(self.app)
        resp = client.get("/api/auth/providers")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertTrue(bool(data["local"]["enabled"]))
        self.assertFalse(bool(data["linuxdo"]["enabled"]))

    def test_oidc_start_requires_config(self) -> None:
        client = TestClient(self.app)
        resp = client.get("/api/auth/oidc/linuxdo/start", follow_redirects=False)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "OIDC_NOT_CONFIGURED")

    def test_oidc_start_redirects_and_sets_cookies_when_enabled(self) -> None:
        client = TestClient(self.app)
        with (
            patch.object(settings, "linuxdo_oidc_client_id", "cid"),
            patch.object(settings, "linuxdo_oidc_client_secret", "sec"),
            patch(
                "app.services.authentication.oidc_client.get_linuxdo_discovery",
                return_value={
                    "authorization_endpoint": "https://connect.linux.do/oauth2/authorize",
                    "token_endpoint": "https://connect.linux.do/oauth2/token",
                    "userinfo_endpoint": "https://connect.linux.do/api/user",
                    "issuer": "https://connect.linux.do/",
                },
            ),
        ):
            resp = client.get("/api/auth/oidc/linuxdo/start?next=%2Fprojects%2Fp1", follow_redirects=False)

        self.assertEqual(resp.status_code, 302)
        self.assertIn("https://connect.linux.do/oauth2/authorize", resp.headers.get("location") or "")
        self.assertIsNotNone(client.cookies.get(_LINUXDO_OIDC_STATE_COOKIE))
        self.assertIsNotNone(client.cookies.get(_LINUXDO_OIDC_VERIFIER_COOKIE))
        self.assertEqual(str(client.cookies.get(_LINUXDO_OIDC_NEXT_COOKIE) or "").strip().strip('"'), "/projects/p1")

    def test_oidc_start_then_callback_reuses_cached_discovery(self) -> None:
        client = TestClient(self.app)
        discovery_url = "https://flow.example/.well-known/openid-configuration"
        discovery_calls = 0

        def fake_fetch(url: str) -> dict[str, str]:
            nonlocal discovery_calls
            self.assertEqual(url, discovery_url)
            discovery_calls += 1
            return {
                "authorization_endpoint": "https://flow.example/authorize",
                "token_endpoint": "https://flow.example/token",
                "userinfo_endpoint": "https://flow.example/userinfo",
                "issuer": "https://flow.example",
            }

        with oidc_client._DISCOVERY_STATE_LOCK:
            oidc_client._DISCOVERY_CACHE.pop(discovery_url, None)
        self.addCleanup(lambda: oidc_client._DISCOVERY_CACHE.pop(discovery_url, None))

        with (
            patch.object(settings, "linuxdo_oidc_client_id", "cid"),
            patch.object(settings, "linuxdo_oidc_client_secret", "sec"),
            patch.object(settings, "linuxdo_oidc_discovery_url", discovery_url),
            patch.object(settings, "linuxdo_oidc_discovery_ttl_seconds", 300),
            patch.object(oidc_client, "_fetch_linuxdo_discovery", side_effect=fake_fetch),
            patch.object(oidc_client, "exchange_linuxdo_code_for_token", return_value={"access_token": "at-123"}),
            patch.object(
                oidc_client,
                "fetch_linuxdo_userinfo",
                return_value={
                    "sub": "sub-flow",
                    "login": "flow-user",
                    "username": "flow-user",
                    "name": "Flow User",
                },
            ),
        ):
            start_response = client.get("/api/auth/oidc/linuxdo/start", follow_redirects=False)
            state = parse_qs(urlsplit(start_response.headers["location"]).query)["state"][0]
            callback_response = client.get(
                "/api/auth/oidc/linuxdo/callback",
                params={"code": "code123", "state": state},
                follow_redirects=False,
            )

        self.assertEqual(start_response.status_code, 302)
        self.assertEqual(callback_response.status_code, 302)
        self.assertEqual(discovery_calls, 1)
        with self.SessionLocal() as db:
            self.assertIsNotNone(db.get(AuthExternalAccount, ("linuxdo", "sub-flow")))

    def test_oidc_callback_creates_user_and_sets_session_cookie(self) -> None:
        client = TestClient(self.app)
        client.cookies.set(_LINUXDO_OIDC_STATE_COOKIE, "state1")
        client.cookies.set(_LINUXDO_OIDC_VERIFIER_COOKIE, "verifier1")
        client.cookies.set(_LINUXDO_OIDC_NEXT_COOKIE, "/")

        with (
            patch.object(settings, "linuxdo_oidc_client_id", "cid"),
            patch.object(settings, "linuxdo_oidc_client_secret", "sec"),
            patch(
                "app.services.authentication.oidc_client.get_linuxdo_discovery",
                return_value={
                    "authorization_endpoint": "https://connect.linux.do/oauth2/authorize",
                    "token_endpoint": "https://connect.linux.do/oauth2/token",
                    "userinfo_endpoint": "https://connect.linux.do/api/user",
                    "issuer": "https://connect.linux.do/",
                },
            ),
            patch(
                "app.services.authentication.oidc_client.exchange_linuxdo_code_for_token",
                return_value={"access_token": "at-123"},
            ),
            patch(
                "app.services.authentication.oidc_client.fetch_linuxdo_userinfo",
                return_value={
                    "sub": "sub-123",
                    "login": "alice",
                    "username": "alice",
                    "name": "Alice",
                    "email": "alice@example.com",
                    "avatar_url": "https://example.com/avatar.png",
                },
            ),
        ):
            resp = client.get("/api/auth/oidc/linuxdo/callback?code=code123&state=state1", follow_redirects=False)

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers.get("location"), "/")
        self.assertIsNotNone(client.cookies.get(settings.auth_cookie_user_id_name))

        with self.SessionLocal() as db:
            user = db.get(User, "linuxdo_alice")
            self.assertIsNotNone(user)
            assert user is not None
            ext = db.get(AuthExternalAccount, ("linuxdo", "sub-123"))
            self.assertIsNotNone(ext)
            assert ext is not None
            self.assertEqual(ext.user_id, user.id)

    def test_oidc_callback_is_idempotent_when_commit_hits_integrity_error(self) -> None:
        with self.SessionLocal() as db:
            user = User(id="linuxdo_alice", email=None, display_name=None, is_admin=False)
            ext = AuthExternalAccount(
                provider="linuxdo",
                subject="sub-123",
                user_id="linuxdo_alice",
                username=None,
                email=None,
                avatar_url=None,
            )
            db.add(user)
            db.flush([user])
            db.add(ext)
            db.commit()

        client = TestClient(self.app)
        client.cookies.set(_LINUXDO_OIDC_STATE_COOKIE, "state1")
        client.cookies.set(_LINUXDO_OIDC_VERIFIER_COOKIE, "verifier1")
        client.cookies.set(_LINUXDO_OIDC_NEXT_COOKIE, "/")

        with (
            patch.object(settings, "linuxdo_oidc_client_id", "cid"),
            patch.object(settings, "linuxdo_oidc_client_secret", "sec"),
            patch(
                "app.services.authentication.oidc_client.get_linuxdo_discovery",
                return_value={
                    "authorization_endpoint": "https://connect.linux.do/oauth2/authorize",
                    "token_endpoint": "https://connect.linux.do/oauth2/token",
                    "userinfo_endpoint": "https://connect.linux.do/api/user",
                    "issuer": "https://connect.linux.do/",
                },
            ),
            patch(
                "app.services.authentication.oidc_client.exchange_linuxdo_code_for_token",
                return_value={"access_token": "at-123"},
            ),
            patch(
                "app.services.authentication.oidc_client.fetch_linuxdo_userinfo",
                return_value={
                    "sub": "sub-123",
                    "login": "alice",
                    "username": "alice",
                    "name": "Alice",
                    "email": "alice@example.com",
                    "avatar_url": "https://example.com/avatar.png",
                },
            ),
        ):
            original_commit = Session.commit
            calls: dict[str, int] = {"n": 0}

            def flaky_commit(self: Session) -> None:  # type: ignore[no-untyped-def]
                calls["n"] += 1
                if calls["n"] == 1:
                    raise IntegrityError(
                        "INSERT INTO auth_external_accounts ...",
                        {},
                        Exception("duplicate key value violates unique constraint"),
                    )
                return original_commit(self)

            with patch.object(Session, "commit", new=flaky_commit):
                resp = client.get("/api/auth/oidc/linuxdo/callback?code=code123&state=state1", follow_redirects=False)

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers.get("location"), "/")
        self.assertIsNotNone(client.cookies.get(settings.auth_cookie_user_id_name))

    def test_oidc_callback_next_path_is_fail_closed(self) -> None:
        client = TestClient(self.app)
        client.cookies.set(_LINUXDO_OIDC_STATE_COOKIE, "state1")
        client.cookies.set(_LINUXDO_OIDC_VERIFIER_COOKIE, "verifier1")
        client.cookies.set(_LINUXDO_OIDC_NEXT_COOKIE, "https://evil.example/")

        with (
            patch.object(settings, "linuxdo_oidc_client_id", "cid"),
            patch.object(settings, "linuxdo_oidc_client_secret", "sec"),
            patch(
                "app.services.authentication.oidc_client.get_linuxdo_discovery",
                return_value={
                    "authorization_endpoint": "https://connect.linux.do/oauth2/authorize",
                    "token_endpoint": "https://connect.linux.do/oauth2/token",
                    "userinfo_endpoint": "https://connect.linux.do/api/user",
                    "issuer": "https://connect.linux.do/",
                },
            ),
            patch(
                "app.services.authentication.oidc_client.exchange_linuxdo_code_for_token",
                return_value={"access_token": "at-123"},
            ),
            patch(
                "app.services.authentication.oidc_client.fetch_linuxdo_userinfo",
                return_value={
                    "sub": "sub-123",
                    "login": "alice",
                    "username": "alice",
                    "name": "Alice",
                    "email": "alice@example.com",
                    "avatar_url": "https://example.com/avatar.png",
                },
            ),
        ):
            resp = client.get("/api/auth/oidc/linuxdo/callback?code=code123&state=state1", follow_redirects=False)

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers.get("location"), "/")

    def test_oidc_callback_recovers_from_conflicting_user_id_insert(self) -> None:
        with self.SessionLocal() as db:
            db.add(User(id="linuxdo_alice", email=None, display_name="existing", is_admin=False))
            db.commit()

        client = TestClient(self.app)
        client.cookies.set(_LINUXDO_OIDC_STATE_COOKIE, "state1")
        client.cookies.set(_LINUXDO_OIDC_VERIFIER_COOKIE, "verifier1")
        client.cookies.set(_LINUXDO_OIDC_NEXT_COOKIE, "/")

        with (
            patch.object(settings, "linuxdo_oidc_client_id", "cid"),
            patch.object(settings, "linuxdo_oidc_client_secret", "sec"),
            patch(
                "app.services.authentication.oidc_client.get_linuxdo_discovery",
                return_value={
                    "authorization_endpoint": "https://connect.linux.do/oauth2/authorize",
                    "token_endpoint": "https://connect.linux.do/oauth2/token",
                    "userinfo_endpoint": "https://connect.linux.do/api/user",
                    "issuer": "https://connect.linux.do/",
                },
            ),
            patch(
                "app.services.authentication.oidc_client.exchange_linuxdo_code_for_token",
                return_value={"access_token": "at-123"},
            ),
            patch(
                "app.services.authentication.oidc_client.fetch_linuxdo_userinfo",
                return_value={
                    "sub": "sub-123",
                    "login": "alice",
                    "username": "alice",
                    "name": "Alice",
                    "email": "alice@example.com",
                    "avatar_url": "https://example.com/avatar.png",
                },
            ),
            patch(
                "app.services.authentication.linuxdo.suggest_user_id",
                return_value="linuxdo_alice",
            ),
        ):
            resp = client.get("/api/auth/oidc/linuxdo/callback?code=code123&state=state1", follow_redirects=False)

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers.get("location"), "/")
        self.assertIsNotNone(client.cookies.get(settings.auth_cookie_user_id_name))

        with self.SessionLocal() as db:
            ext = db.get(AuthExternalAccount, ("linuxdo", "sub-123"))
            self.assertIsNotNone(ext)
            assert ext is not None
            self.assertNotEqual(ext.user_id, "linuxdo_alice")
            self.assertIsNotNone(db.get(User, str(ext.user_id)))


if __name__ == "__main__":
    unittest.main()
