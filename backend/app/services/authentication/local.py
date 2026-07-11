from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import AppError, ok_payload
from app.db.utils import utc_now
from app.models.user import User
from app.models.user_password import UserPassword
from app.schemas.auth import ChangePasswordRequest, LocalLoginRequest, LocalRegisterRequest
from app.services.authentication.passwords import commit_user_creation, hash_password, verify_password
from app.services.authentication.session import login_response


def local_login(request: Request, db: Session, body: LocalLoginRequest) -> JSONResponse:
    user = db.get(User, body.user_id)
    pwd = db.get(UserPassword, body.user_id)
    if user is None or pwd is None:
        raise AppError.unauthorized("用户名或密码错误")
    if pwd.disabled_at is not None:
        raise AppError.unauthorized("账号已禁用")
    if not verify_password(body.password, pwd.password_hash):
        raise AppError.unauthorized("用户名或密码错误")
    return login_response(request, user)


def local_register(request: Request, db: Session, body: LocalRegisterRequest) -> JSONResponse:
    target_user_id = body.user_id.strip()
    if not target_user_id:
        raise AppError.validation("user_id 不能为空")
    admin_user_id = (settings.auth_admin_user_id or "").strip()
    if admin_user_id and target_user_id == admin_user_id:
        raise AppError.forbidden("该用户名已被系统保留，请联系管理员分配/重置")
    if db.get(User, target_user_id) is not None:
        raise AppError.conflict("用户已存在")
    user = User(
        id=target_user_id,
        email=(body.email or "").strip() or None,
        display_name=(body.display_name or "").strip() or target_user_id,
        is_admin=False,
    )
    db.add(user)
    db.add(
        UserPassword(
            user_id=target_user_id,
            password_hash=hash_password(body.password),
            password_updated_at=utc_now(),
            disabled_at=None,
        )
    )
    commit_user_creation(db)
    return login_response(request, user)


def change_password(request: Request, db: Session, user_id: str, body: ChangePasswordRequest) -> dict:
    pwd = db.get(UserPassword, user_id)
    if pwd is None or pwd.disabled_at is not None:
        raise AppError.unauthorized()
    if not verify_password(body.old_password, pwd.password_hash):
        raise AppError.unauthorized("旧密码错误")
    pwd.password_hash = hash_password(body.new_password)
    pwd.password_updated_at = utc_now()
    db.commit()
    return ok_payload(request_id=request.state.request_id, data={})
