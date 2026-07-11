from __future__ import annotations

import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from app.api.routes.auth import (
    AdminCreateUserRequest,
    AdminResetPasswordRequest,
    ChangePasswordRequest,
    LocalLoginRequest,
    LocalRegisterRequest,
    router as auth_router,
)
from app.core.auth_password import PASSWORD_MAX_LENGTH, PASSWORD_MIN_LENGTH


def _string_schema(schema: dict) -> dict:
    if schema.get("type") == "string":
        return schema
    return next(option for option in schema["anyOf"] if option.get("type") == "string")


@pytest.mark.parametrize(
    ("model", "payload", "field"),
    [
        (LocalRegisterRequest, {"user_id": "u1", "password": "x" * PASSWORD_MIN_LENGTH}, "password"),
        (
            ChangePasswordRequest,
            {"old_password": "x", "new_password": "x" * PASSWORD_MIN_LENGTH},
            "new_password",
        ),
        (AdminCreateUserRequest, {"user_id": "u1", "password": "x" * PASSWORD_MIN_LENGTH}, "password"),
        (AdminResetPasswordRequest, {"new_password": "x" * PASSWORD_MIN_LENGTH}, "new_password"),
    ],
)
def test_new_password_fields_share_minimum_and_maximum_boundaries(model, payload: dict, field: str) -> None:
    assert getattr(model.model_validate(payload), field) == "x" * PASSWORD_MIN_LENGTH

    max_payload = {**payload, field: "x" * PASSWORD_MAX_LENGTH}
    assert getattr(model.model_validate(max_payload), field) == "x" * PASSWORD_MAX_LENGTH

    with pytest.raises(ValidationError):
        model.model_validate({**payload, field: "x" * (PASSWORD_MIN_LENGTH - 1)})
    with pytest.raises(ValidationError):
        model.model_validate({**payload, field: "x" * (PASSWORD_MAX_LENGTH + 1)})


def test_login_and_old_password_keep_existing_minimum_contract() -> None:
    assert LocalLoginRequest.model_validate({"user_id": "u1", "password": "x"}).password == "x"
    assert ChangePasswordRequest.model_validate(
        {"old_password": "x", "new_password": "x" * PASSWORD_MIN_LENGTH}
    ).old_password == "x"


@pytest.mark.parametrize("blank_password", [None, "", " ", "        "])
def test_optional_admin_password_fields_preserve_generated_password_path(blank_password: str | None) -> None:
    assert AdminCreateUserRequest.model_validate({"user_id": "u1", "password": blank_password}).password is None
    assert AdminResetPasswordRequest.model_validate({"new_password": blank_password}).new_password is None


def test_openapi_documents_one_new_password_contract_without_tightening_credentials() -> None:
    app = FastAPI()
    app.include_router(auth_router, prefix="/api")
    schemas = app.openapi()["components"]["schemas"]

    new_password_fields = (
        ("LocalRegisterRequest", "password"),
        ("ChangePasswordRequest", "new_password"),
        ("AdminCreateUserRequest", "password"),
        ("AdminResetPasswordRequest", "new_password"),
    )
    for model_name, field_name in new_password_fields:
        field_schema = _string_schema(schemas[model_name]["properties"][field_name])
        assert field_schema["minLength"] == PASSWORD_MIN_LENGTH
        assert field_schema["maxLength"] == PASSWORD_MAX_LENGTH

    login_password = schemas["LocalLoginRequest"]["properties"]["password"]
    old_password = schemas["ChangePasswordRequest"]["properties"]["old_password"]
    assert login_password["minLength"] == 1
    assert old_password["minLength"] == 1
