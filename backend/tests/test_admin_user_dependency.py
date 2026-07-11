from __future__ import annotations

import ast
from pathlib import Path
from typing import get_args
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.params import Depends
from sqlalchemy.orm import Session

from app.api.deps import AdminUserDep, get_admin_user
from app.api.routes import auth as auth_routes
from app.core.errors import AppError
from app.models.user import User
from app.models.user_activity_stat import UserActivityStat
from app.models.user_password import UserPassword
from app.models.user_usage_stat import UserUsageStat
from tests.support import (
    auth_cookies,
    create_tables,
    make_client,
    make_session_factory,
    make_sqlite_engine,
    make_test_app,
    seed_user,
)


BACKEND_ROOT = Path(__file__).resolve().parents[1]
AUTH_ROUTE_PATH = BACKEND_ROOT / "app" / "api" / "routes" / "auth.py"
ADMIN_HANDLERS = {"set_user_disabled", "list_users", "create_user", "reset_user_password"}
ADMIN_MODELS = [User, UserPassword, UserActivityStat, UserUsageStat]


def test_admin_user_dependency_alias_targets_get_admin_user() -> None:
    annotated_type, dependency = get_args(AdminUserDep)

    assert annotated_type is User
    assert isinstance(dependency, Depends)
    assert dependency.dependency is get_admin_user


def test_get_admin_user_returns_the_authenticated_admin() -> None:
    admin = User(id="admin", is_admin=True)
    db = Mock(spec=Session)
    db.get.return_value = admin

    assert get_admin_user(db, "admin") is admin
    db.get.assert_called_once_with(User, "admin")


@pytest.mark.parametrize("actor", [None, User(id="member", is_admin=False)])
def test_get_admin_user_preserves_forbidden_contract_for_missing_or_non_admin_actor(actor: User | None) -> None:
    db = Mock(spec=Session)
    db.get.return_value = actor

    with pytest.raises(AppError) as exc_info:
        get_admin_user(db, "member")

    error = exc_info.value
    assert (error.status_code, error.code, error.message, error.details) == (403, "FORBIDDEN", "无权限", {})
    db.get.assert_called_once_with(User, "member")


@pytest.mark.parametrize(
    ("method", "path", "json_body"),
    [
        ("get", "/api/auth/admin/users", None),
        ("post", "/api/auth/admin/users", {"user_id": "created-user", "password": "password123"}),
        ("post", "/api/auth/admin/users/victim/disable", {"disabled": True}),
        ("post", "/api/auth/admin/users/victim/password/reset", {"new_password": "new-password-123"}),
    ],
)
def test_each_admin_endpoint_resolves_admin_dependency_exactly_once(
    method: str,
    path: str,
    json_body: dict[str, object] | None,
) -> None:
    engine = make_sqlite_engine()
    factory = make_session_factory(engine)
    create_tables(engine, ADMIN_MODELS)
    seed_user(factory, user_id="victim")
    app = make_test_app(factory, [auth_routes])
    calls = 0

    def _override_admin_user() -> User:
        nonlocal calls
        calls += 1
        return User(id="admin", is_admin=True)

    app.dependency_overrides[get_admin_user] = _override_admin_user
    client = make_client(app)
    try:
        response = client.request(method, path, json=json_body)
    finally:
        client.close()
        engine.dispose()

    assert response.status_code == 200, response.text
    assert calls == 1


@pytest.mark.parametrize(
    ("method", "path", "json_body"),
    [
        ("get", "/api/auth/admin/users", None),
        ("post", "/api/auth/admin/users", {"user_id": "created-user", "password": "password123"}),
        ("post", "/api/auth/admin/users/victim/disable", {"disabled": True}),
        ("post", "/api/auth/admin/users/victim/password/reset", {"new_password": "new-password-123"}),
    ],
)
def test_each_admin_endpoint_preserves_unauthenticated_error_contract(
    method: str,
    path: str,
    json_body: dict[str, object] | None,
) -> None:
    engine = make_sqlite_engine()
    factory = make_session_factory(engine)
    app = make_test_app(factory, [auth_routes])
    client = make_client(app)
    try:
        response = client.request(method, path, json=json_body)
    finally:
        client.close()
        engine.dispose()

    assert response.status_code == 401
    assert response.json()["error"] == {"code": "UNAUTHORIZED", "message": "未登录", "details": {}}


@pytest.mark.parametrize(
    ("method", "path", "json_body"),
    [
        ("get", "/api/auth/admin/users", None),
        ("post", "/api/auth/admin/users", {"user_id": "created-user", "password": "password123"}),
        ("post", "/api/auth/admin/users/victim/disable", {"disabled": True}),
        ("post", "/api/auth/admin/users/victim/password/reset", {"new_password": "new-password-123"}),
    ],
)
def test_each_admin_endpoint_preserves_non_admin_error_contract(
    method: str,
    path: str,
    json_body: dict[str, object] | None,
) -> None:
    engine = make_sqlite_engine()
    factory = make_session_factory(engine)
    create_tables(engine, [User, UserPassword])
    seed_user(factory, user_id="member", is_admin=False)
    app = make_test_app(factory, [auth_routes])
    client = make_client(app)
    auth_cookies(client, "member")
    try:
        response = client.request(method, path, json=json_body)
    finally:
        client.close()
        engine.dispose()

    assert response.status_code == 403
    assert response.json()["error"] == {"code": "FORBIDDEN", "message": "无权限", "details": {}}


def test_admin_routes_use_dependency_injection_without_manual_guard_calls() -> None:
    tree = ast.parse(AUTH_ROUTE_PATH.read_text(encoding="utf-8"), filename=str(AUTH_ROUTE_PATH))
    functions = {node.name: node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}

    assert "_require_admin" not in functions
    assert ADMIN_HANDLERS <= functions.keys()

    for name in ADMIN_HANDLERS:
        handler = functions[name]
        admin_args = [
            arg
            for arg in handler.args.args
            if isinstance(arg.annotation, ast.Name) and arg.annotation.id == "AdminUserDep"
        ]
        assert [arg.arg for arg in admin_args] == ["_admin_user"]
        manual_guard_calls = [
            node.func.id
            for node in ast.walk(handler)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"get_admin_user", "_require_admin"}
        ]
        assert manual_guard_calls == []


def test_admin_route_openapi_paths_and_public_parameters_are_unchanged() -> None:
    app = FastAPI()
    app.include_router(auth_routes.router, prefix="/api")
    paths = app.openapi()["paths"]

    expected_operations = {
        ("/api/auth/admin/users", "get"): (
            "list_users_api_auth_admin_users_get",
            {("limit", "query"), ("cursor", "query"), ("q", "query"), ("online_only", "query")},
            None,
        ),
        ("/api/auth/admin/users", "post"): (
            "create_user_api_auth_admin_users_post",
            set(),
            "#/components/schemas/AdminCreateUserRequest",
        ),
        ("/api/auth/admin/users/{target_user_id}/disable", "post"): (
            "set_user_disabled_api_auth_admin_users__target_user_id__disable_post",
            {("target_user_id", "path")},
            "#/components/schemas/DisableUserRequest",
        ),
        ("/api/auth/admin/users/{target_user_id}/password/reset", "post"): (
            "reset_user_password_api_auth_admin_users__target_user_id__password_reset_post",
            {("target_user_id", "path")},
            "#/components/schemas/AdminResetPasswordRequest",
        ),
    }

    actual_admin_operations = {
        (path, method)
        for path, path_item in paths.items()
        if path.startswith("/api/auth/admin/")
        for method in path_item
    }
    assert actual_admin_operations == expected_operations.keys()

    for (path, method), (operation_id, parameters, request_schema_ref) in expected_operations.items():
        operation = paths[path][method]
        assert operation["operationId"] == operation_id
        assert {(item["name"], item["in"]) for item in operation.get("parameters", [])} == parameters
        if request_schema_ref is None:
            assert "requestBody" not in operation
        else:
            assert operation["requestBody"]["content"]["application/json"]["schema"]["$ref"] == request_schema_ref
