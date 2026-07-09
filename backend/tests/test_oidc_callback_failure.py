"""LinuxDo OIDC 回调缺失 access_token 的 known_issue 测试。

H6（项目情况完全分析.md）：``app/api/routes/auth.py:460-462``，LinuxDo OIDC 回调在
token 交换成功但响应**缺少 access_token** 时（如 IdP 返回 ``{}`` 或仅返回错误字段），
直接 ``return response``——``response`` 是 line 432 预建的**成功重定向**
（302 → next_path），既未调用 ``_fail`` 报错，也未走 ``_linuxdo_fetch_userinfo``。
结果：用户实际未登录，却被导向成功页，无错误提示、无日志。

正确行为：缺少 access_token 应返回**错误**（400 + ok=False 信封），而非成功重定向；
且**不应设置会话 cookie**。当前实现有 bug，故本测试标 ``known_issue``，必须红。

载体：``make_test_app(factory, [auth_routes])`` + ``create_tables`` 全建活表；
patch ``_linuxdo_discovery``（避免真实 HTTP）与 ``_linuxdo_exchange_code_for_token``
（返回不含 access_token 的 dict）；``TestClient(raise_server_exceptions=False)``
以拿到错误响应而非异常抛出；``follow_redirects=False`` 捕获原始 302 而非跟随后的 404。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from app.api.routes import auth as auth_routes
from app.core.config import settings

from tests.support import (
    create_tables,
    make_session_factory,
    make_sqlite_engine,
    make_test_app,
)


# H6: OIDC 回调缺 access_token 应返回错误，当前 bug 返回成功重定向（302）
@pytest.mark.known_issue
def test_linuxdo_oidc_callback_missing_access_token_returns_error() -> None:
    engine = make_sqlite_engine()
    factory = make_session_factory(engine)
    create_tables(engine)  # OIDC 回调可能写 user / auth_external_accounts，全建活表
    app = make_test_app(factory, [auth_routes])

    # 临时开启 OIDC（client_id/secret 非空），finally 还原
    old_id = settings.linuxdo_oidc_client_id
    old_secret = settings.linuxdo_oidc_client_secret
    settings.linuxdo_oidc_client_id = "test-client-id"
    settings.linuxdo_oidc_client_secret = "test-client-secret"
    try:
        client = TestClient(app, raise_server_exceptions=False)
        # mock discovery（避免真实 HTTP）+ token 交换返回不含 access_token 的 dict
        fake_discovery = {
            "authorization_endpoint": "https://connect.linux.do/oauth2/authorize",
            "token_endpoint": "https://connect.linux.do/oauth2/token",
            "userinfo_endpoint": "https://connect.linux.do/api/user",
            "issuer": "https://connect.linux.do",
        }
        with patch.object(auth_routes, "_linuxdo_discovery", return_value=fake_discovery), patch.object(
            auth_routes, "_linuxdo_exchange_code_for_token", return_value={}
        ):
            resp = client.get(
                "/api/auth/oidc/linuxdo/callback",
                params={"code": "fake-code", "state": "state-fixed"},
                cookies={
                    "oidc_linuxdo_state": "state-fixed",
                    "oidc_linuxdo_verifier": "verifier-fixed",
                    "oidc_linuxdo_next": "/",
                },
                follow_redirects=False,
            )

        # 正确行为：缺少 access_token 应返回错误（400 + ok=False 信封）。
        # 当前 bug：直接 return response（line 462）→ 302 成功重定向到 next_path，无错误。
        assert resp.status_code == 400, f"期望 400 错误，实际 {resp.status_code}（成功重定向即 bug）"
        body = resp.json()
        assert body.get("ok") is False

        # 不应设置会话 cookie（用户实际未登录）
        assert "user_id" not in resp.cookies
        assert "session_expire_at" not in resp.cookies
    finally:
        settings.linuxdo_oidc_client_id = old_id
        settings.linuxdo_oidc_client_secret = old_secret
