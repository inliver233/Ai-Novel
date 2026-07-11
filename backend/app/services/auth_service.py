from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.auth_password import PASSWORD_MIN_LENGTH
from app.core.config import settings
from app.core.errors import AppError
from app.db.utils import utc_now
from app.models.user import User
from app.models.user_password import UserPassword


def hash_password(password: str) -> str:
    raw = (password or "").strip()
    if len(raw) < PASSWORD_MIN_LENGTH:
        raise AppError.validation(f"密码长度至少 {PASSWORD_MIN_LENGTH} 位")

    import bcrypt

    salt = bcrypt.gensalt(rounds=settings.auth_bcrypt_rounds)
    hashed = bcrypt.hashpw(raw.encode("utf-8"), salt)
    return hashed.decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    raw = (password or "").strip()
    if not raw:
        return False
    if not password_hash:
        return False
    try:
        import bcrypt

        return bcrypt.checkpw(raw.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False


def commit_user_creation(db: Session) -> None:
    """Commit a newly created user and translate uniqueness races safely."""
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise AppError.conflict("用户已存在") from None


def ensure_admin_user(db: Session) -> None:
    admin_user_id = settings.auth_admin_user_id
    admin_password = settings.auth_admin_password
    if not admin_user_id or not admin_password:
        return

    user = db.get(User, admin_user_id)
    if user is None:
        user = User(
            id=admin_user_id,
            email=settings.auth_admin_email,
            display_name=settings.auth_admin_display_name or "管理员",
            is_admin=True,
        )
        db.add(user)
    else:
        user.is_admin = True
        if settings.auth_admin_email and not user.email:
            user.email = settings.auth_admin_email
        if settings.auth_admin_display_name and not user.display_name:
            user.display_name = settings.auth_admin_display_name

    pwd = db.get(UserPassword, admin_user_id)
    if pwd is None:
        pwd = UserPassword(
            user_id=admin_user_id,
            password_hash=hash_password(admin_password),
            password_updated_at=utc_now(),
            disabled_at=None,
        )
        db.add(pwd)

    db.commit()
