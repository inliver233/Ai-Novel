"""Compatibility imports for legacy callers; implementations live in authentication."""

from app.services.authentication.passwords import (
    commit_user_creation,
    ensure_admin_user,
    hash_password,
    verify_password,
)

__all__ = ["commit_user_creation", "ensure_admin_user", "hash_password", "verify_password"]
