"""Regression coverage for LinuxDo OIDC token responses without an access token."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from app.api.routes import auth as auth_routes
from app.core.config import settings
from app.services.authentication import oidc_client

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
    client.cookies.set(auth_routes._LINUXDO_OIDC_STATE_COOKIE, "state-fixed")
    client.cookies.set(auth_routes._LINUXDO_OIDC_VERIFIER_COOKIE, "verifier-fixed")
    client.cookies.set(auth_routes._LINUXDO_OIDC_NEXT_COOKIE, "/projects/p1")

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
            auth_routes._LINUXDO_OIDC_STATE_COOKIE,
            auth_routes._LINUXDO_OIDC_VERIFIER_COOKIE,
            auth_routes._LINUXDO_OIDC_NEXT_COOKIE,
        ):
            assert any(
                header.startswith(f"{cookie_name}=") and "Max-Age=0" in header
                for header in set_cookie_headers
            )

        assert settings.auth_cookie_user_id_name not in response.cookies
        assert settings.auth_cookie_expire_at_name not in response.cookies
    finally:
        engine.dispose()
