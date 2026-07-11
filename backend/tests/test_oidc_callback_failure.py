"""Regression coverage for LinuxDo OIDC token responses without an access token."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from app.api.routes import auth as auth_routes
from app.core.config import settings
from app.db.utils import utc_now
from app.models.auth_external_account import AuthExternalAccount
from app.models.user import User
from app.services.authentication import linuxdo, oidc_client

from tests.support import create_tables, make_session_factory, make_sqlite_engine, make_test_app


@pytest.mark.parametrize(
    "token_response",
    [
        {},
        {"access_token": None},
        {"access_token": "   "},
        {"error": "invalid_grant", "error_description": "expired code"},
    ],
    ids=["missing", "null", "blank", "provider-error"],
)
def test_linuxdo_oidc_callback_missing_access_token_returns_error(
    monkeypatch: pytest.MonkeyPatch,
    token_response: dict[str, object],
) -> None:
    engine = make_sqlite_engine()
    factory = make_session_factory(engine)
    create_tables(engine)
    app = make_test_app(factory, [auth_routes])
    monkeypatch.setattr(settings, "linuxdo_oidc_client_id", "test-client-id")
    monkeypatch.setattr(settings, "linuxdo_oidc_client_secret", "test-client-secret")

    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set(linuxdo._STATE_COOKIE, "state-fixed")
    client.cookies.set(linuxdo._VERIFIER_COOKIE, "verifier-fixed")
    client.cookies.set(linuxdo._NEXT_COOKIE, "/projects/p1")

    fake_discovery = {
        "authorization_endpoint": "https://connect.linux.do/oauth2/authorize",
        "token_endpoint": "https://connect.linux.do/oauth2/token",
        "userinfo_endpoint": "https://connect.linux.do/api/user",
        "issuer": "https://connect.linux.do",
    }
    try:
        with (
            patch.object(oidc_client, "get_linuxdo_discovery", return_value=fake_discovery),
            patch.object(oidc_client, "exchange_linuxdo_code_for_token", return_value=token_response),
            patch.object(oidc_client, "fetch_linuxdo_userinfo") as fetch_userinfo,
        ):
            response = client.get(
                "/api/auth/oidc/linuxdo/callback",
                params={"code": "fake-code", "state": "state-fixed"},
                follow_redirects=False,
            )

        fetch_userinfo.assert_not_called()
        assert response.status_code == 302

        location = urlsplit(response.headers["location"])
        query = parse_qs(location.query)
        assert location.path == "/login"
        assert query["next"] == ["/projects/p1"]
        assert query["oidc_error"] == ["OIDC_TOKEN_MISSING"]
        assert query["request_id"] == ["rid-test"]

        set_cookie_headers = response.headers.get_list("set-cookie")
        for cookie_name in (
            linuxdo._STATE_COOKIE,
            linuxdo._VERIFIER_COOKIE,
            linuxdo._NEXT_COOKIE,
        ):
            assert any(header.startswith(f"{cookie_name}=") and "Max-Age=0" in header for header in set_cookie_headers)

        assert settings.auth_cookie_user_id_name not in response.cookies
        assert settings.auth_cookie_expire_at_name not in response.cookies
    finally:
        engine.dispose()


def test_linuxdo_oidc_callback_refuses_disabled_existing_user(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = make_sqlite_engine()
    factory = make_session_factory(engine)
    create_tables(engine)
    with factory() as db:
        db.add(User(id="oidc-user", display_name="OIDC", disabled_at=utc_now()))
        db.commit()
        db.add(AuthExternalAccount(provider="linuxdo", subject="subject-1", user_id="oidc-user"))
        db.commit()
    app = make_test_app(factory, [auth_routes])
    monkeypatch.setattr(settings, "linuxdo_oidc_client_id", "test-client-id")
    monkeypatch.setattr(settings, "linuxdo_oidc_client_secret", "test-client-secret")
    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set(linuxdo._STATE_COOKIE, "state-fixed")
    client.cookies.set(linuxdo._VERIFIER_COOKIE, "verifier-fixed")
    client.cookies.set(settings.auth_cookie_user_id_name, "old-session", domain="testserver.local", path="/")
    client.cookies.set(settings.auth_cookie_expire_at_name, "123", domain="testserver.local", path="/")
    try:
        with (
            patch.object(
                oidc_client,
                "get_linuxdo_discovery",
                return_value={
                    "authorization_endpoint": "https://example/authorize",
                    "token_endpoint": "https://example/token",
                    "userinfo_endpoint": "https://example/user",
                    "issuer": "https://example",
                },
            ),
            patch.object(oidc_client, "exchange_linuxdo_code_for_token", return_value={"access_token": "token"}),
            patch.object(oidc_client, "fetch_linuxdo_userinfo", return_value={"sub": "subject-1", "name": "OIDC"}),
        ):
            response = client.get(
                "/api/auth/oidc/linuxdo/callback",
                params={"code": "code", "state": "state-fixed"},
                follow_redirects=False,
            )
        assert response.status_code == 302
        assert parse_qs(urlsplit(response.headers["location"]).query)["oidc_error"] == ["ACCOUNT_DISABLED"]
        set_cookie_headers = response.headers.get_list("set-cookie")
        for cookie_name in (settings.auth_cookie_user_id_name, settings.auth_cookie_expire_at_name):
            assert any(header.startswith(f"{cookie_name}=") and "Max-Age=0" in header for header in set_cookie_headers)
            assert client.cookies.get(cookie_name) is None
    finally:
        engine.dispose()
