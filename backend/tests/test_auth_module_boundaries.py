from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

from app.api.router import api_router

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
_ROUTE_MODULES = (
    "auth_session.py",
    "auth_local.py",
    "auth_oidc_linuxdo.py",
    "auth_admin.py",
)

_EXPECTED_ROUTES = {
    ("GET", "/api/auth/user", "get_current_user_api_auth_user_get"),
    ("GET", "/api/auth/providers", "list_auth_providers_api_auth_providers_get"),
    ("POST", "/api/auth/local/login", "local_login_api_auth_local_login_post"),
    ("POST", "/api/auth/local/register", "local_register_api_auth_local_register_post"),
    ("GET", "/api/auth/oidc/linuxdo/start", "linuxdo_oidc_start_api_auth_oidc_linuxdo_start_get"),
    ("GET", "/api/auth/oidc/linuxdo/callback", "linuxdo_oidc_callback_api_auth_oidc_linuxdo_callback_get"),
    ("POST", "/api/auth/password/change", "change_password_api_auth_password_change_post"),
    (
        "POST",
        "/api/auth/admin/users/{target_user_id}/disable",
        "set_user_disabled_api_auth_admin_users__target_user_id__disable_post",
    ),
    ("GET", "/api/auth/admin/users", "list_users_api_auth_admin_users_get"),
    ("POST", "/api/auth/admin/users", "create_user_api_auth_admin_users_post"),
    (
        "POST",
        "/api/auth/admin/users/{target_user_id}/password/reset",
        "reset_user_password_api_auth_admin_users__target_user_id__password_reset_post",
    ),
    ("POST", "/api/auth/refresh", "refresh_session_api_auth_refresh_post"),
    ("POST", "/api/auth/logout", "logout_api_auth_logout_post"),
}


def test_auth_route_inventory_is_stable() -> None:
    actual = {
        (method, route.path, route.unique_id)
        for route in api_router.routes
        if route.path.startswith("/api/auth/")
        for method in route.methods
        if method not in {"HEAD", "OPTIONS"}
    }
    assert actual == _EXPECTED_ROUTES


def test_auth_routes_are_transport_only() -> None:
    route_dir = _BACKEND_ROOT / "app" / "api" / "routes"
    forbidden_imports = {"httpx", "sqlalchemy", "app.models"}
    for filename in _ROUTE_MODULES:
        tree = ast.parse((route_dir / filename).read_text(encoding="utf-8"))
        imports = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names} | {
            node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        }
        assert not any(
            name == prefix or name.startswith(prefix + ".") for name in imports for prefix in forbidden_imports
        )


def test_legacy_auth_module_is_only_a_compatibility_facade() -> None:
    source = (_BACKEND_ROOT / "app" / "api" / "routes" / "auth.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert not [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    assert len(source.splitlines()) < 40


def test_rate_limit_does_not_import_private_auth_session_symbols() -> None:
    # backend-api#4：限流模块必须走公开的密钥派生 API，不得引用 auth_session 私有符号。
    source_file = _BACKEND_ROOT / "app" / "services" / "authentication" / "rate_limit.py"
    tree = ast.parse(source_file.read_text(encoding="utf-8"))
    private_imports = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "app.core.auth_session"
        for alias in node.names
        if alias.name.startswith("_")
    ]
    assert private_imports == []


def test_auth_modules_import_fresh_in_any_order() -> None:
    modules = [
        "app.api.routes.auth_session",
        "app.api.routes.auth_local",
        "app.api.routes.auth_oidc_linuxdo",
        "app.api.routes.auth_admin",
        "app.api.routes.auth",
        "app.api.router",
    ]
    orders = (modules, list(reversed(modules)), modules[2:] + modules[:2])
    for order in orders:
        code = ";".join(f"import {module}" for module in order)
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=_BACKEND_ROOT, capture_output=True, text=True, timeout=30, check=False
        )
        assert result.returncode == 0, result.stderr
