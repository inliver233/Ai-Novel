from __future__ import annotations

from typing import Annotated

from pydantic import BeforeValidator, Field

from app.core.auth_password import PASSWORD_MAX_LENGTH, PASSWORD_MIN_LENGTH
from app.schemas.base import RequestModel


def _blank_password_to_none(value: object) -> object:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return value


NewPassword = Annotated[
    str,
    Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH),
]
OptionalNewPassword = Annotated[NewPassword | None, BeforeValidator(_blank_password_to_none)]


class LocalLoginRequest(RequestModel):
    user_id: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)


class LocalRegisterRequest(RequestModel):
    user_id: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)
    display_name: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=255)


class ChangePasswordRequest(RequestModel):
    old_password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)
    new_password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)


class DisableUserRequest(RequestModel):
    disabled: bool = True


class AdminCreateUserRequest(RequestModel):
    user_id: str = Field(min_length=1, max_length=64)
    display_name: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=255)
    is_admin: bool = False
    password: OptionalNewPassword = None


class AdminResetPasswordRequest(RequestModel):
    new_password: OptionalNewPassword = None
