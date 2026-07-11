"""Shared database and administrator bootstrap entry point.

Both the FastAPI lifespan and the container entrypoint delegate to this module
so schema migration and administrator initialization cannot drift apart.
"""

from __future__ import annotations

import logging
import os

from app.core.auth_password import PASSWORD_MIN_LENGTH
from app.core.config import settings
from app.core.errors import AppError
from app.core.logging import configure_logging, log_event
from app.db.migrations import ensure_db_schema
from app.db.session import SessionLocal
from app.services.auth_service import ensure_admin_user

logger = logging.getLogger("ainovel")


def _env_truthy(name: str) -> bool | None:
    """Parse the existing bootstrap boolean environment convention."""
    raw = str(os.getenv(name) or "").strip().lower()
    if not raw:
        return None
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return None


def _web_concurrency() -> int:
    """Return the configured web worker count, preserving legacy fallback."""
    raw = str(os.getenv("WEB_CONCURRENCY") or "").strip()
    if not raw:
        return 1
    try:
        value = int(raw)
    except Exception:
        return 1
    return 1 if value <= 0 else value


def should_bootstrap_in_app() -> bool:
    """Return whether the FastAPI process should run bootstrap on startup.

    ``AINOVEL_BOOTSTRAP_DONE`` remains the highest-priority marker set by the
    container entrypoint. ``AINOVEL_BOOTSTRAP_IN_APP`` remains an explicit
    override. Without either, only a single-worker development process runs
    bootstrap in-app, matching the previous lifespan policy.
    """
    if _env_truthy("AINOVEL_BOOTSTRAP_DONE") is True:
        return False

    override = _env_truthy("AINOVEL_BOOTSTRAP_IN_APP")
    if override is not None:
        return override

    if settings.app_env != "dev":
        return False

    return _web_concurrency() <= 1


def ensure_configured_admin_user() -> None:
    """Create the configured administrator with the existing dev fail-soft."""
    db = SessionLocal()
    try:
        ensure_admin_user(db)
    except AppError as exc:
        raw = (settings.auth_admin_password or "").strip()
        if settings.app_env == "dev" and exc.code == "VALIDATION_ERROR" and raw and len(raw) < PASSWORD_MIN_LENGTH:
            log_event(
                logger,
                "warning",
                event="AUTH_ADMIN_BOOTSTRAP",
                action="skipped",
                reason="invalid_password",
                admin_user_id=settings.auth_admin_user_id,
                password_length=len(raw),
                min_password_length=PASSWORD_MIN_LENGTH,
                message=f"AUTH_ADMIN_PASSWORD 无效（长度 < {PASSWORD_MIN_LENGTH}），跳过 admin bootstrap（dev only）",
            )
            return
        raise
    finally:
        db.close()


def run_bootstrap() -> None:
    """Apply the schema before initializing the configured administrator."""
    ensure_db_schema()
    ensure_configured_admin_user()


def bootstrap_in_app() -> bool:
    """Run bootstrap for the FastAPI process when the shared policy allows it."""
    if not should_bootstrap_in_app():
        return False
    run_bootstrap()
    return True


def main() -> None:
    """Run the unconditional bootstrap command used by the entrypoint."""
    configure_logging()
    run_bootstrap()


if __name__ == "__main__":
    main()
