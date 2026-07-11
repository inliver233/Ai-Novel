from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_APP_ERROR_HEADER_ALLOWLIST = {
    "retry-after": "Retry-After",
    "www-authenticate": "WWW-Authenticate",
}


@dataclass(slots=True)
class AppError(Exception):
    code: str
    message: str
    status_code: int = 400
    details: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Avoid zero-arg super() edge cases in production; keep Exception args stable for str(err).
        Exception.__init__(self, self.message)

    @staticmethod
    def unauthorized(message: str = "未登录", *, details: dict[str, Any] | None = None) -> "AppError":
        return AppError(code="UNAUTHORIZED", message=message, status_code=401, details=details or {})

    @staticmethod
    def forbidden(message: str = "无权限", *, details: dict[str, Any] | None = None) -> "AppError":
        return AppError(code="FORBIDDEN", message=message, status_code=403, details=details or {})

    @staticmethod
    def not_found(message: str = "资源不存在", *, details: dict[str, Any] | None = None) -> "AppError":
        return AppError(code="NOT_FOUND", message=message, status_code=404, details=details or {})

    @staticmethod
    def conflict(message: str = "资源冲突", *, details: dict[str, Any] | None = None) -> "AppError":
        return AppError(code="CONFLICT", message=message, status_code=409, details=details or {})

    @staticmethod
    def validation(message: str = "参数错误", *, details: dict[str, Any] | None = None) -> "AppError":
        return AppError(code="VALIDATION_ERROR", message=message, status_code=400, details=details or {})


def error_payload(*, request_id: str, code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {"code": code, "message": message, "details": details or {}},
        "request_id": request_id,
    }


def ok_payload(*, request_id: str, data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data, "request_id": request_id}


def safe_app_error_headers(headers: dict[str, str]) -> dict[str, str]:
    """Allow only explicitly supported, bounded, non-control response headers."""

    safe: dict[str, str] = {}
    for key, value in headers.items():
        name = str(key).strip()
        raw_value = str(value).strip()
        canonical_name = _APP_ERROR_HEADER_ALLOWLIST.get(name.lower())
        if canonical_name is None:
            continue
        if not raw_value or len(raw_value) > 1024:
            continue
        if any(ord(char) < 0x20 or ord(char) > 0x7E for char in name + raw_value):
            continue
        safe[canonical_name] = raw_value
    return safe
