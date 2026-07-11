from __future__ import annotations

from typing import Annotated

from pydantic import BeforeValidator, Field

from app.core.auth_password import PASSWORD_MAX_LENGTH, PASSWORD_MIN_LENGTH


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
