from __future__ import annotations

from typing import Annotated, Literal, TYPE_CHECKING

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.db.session import get_db
from app.models.chapter import Chapter
from app.models.character import Character
from app.models.generation_run import GenerationRun
from app.models.llm_profile import LLMProfile
from app.models.outline import Outline
from app.models.project import Project
from app.models.project_membership import ProjectMembership
from app.models.user import User
from app.db.datetime_compat import coerce_utc_datetime

if TYPE_CHECKING:
    # Entry 采用惰性运行时导入（见 require_entry_*），此处仅用于类型注解解析，
    # 不在运行期执行，避免循环导入。
    from app.models.entry import Entry

LOCAL_USER_ID = "local-user"


def get_current_user_id(request: Request) -> str:
    user_id = getattr(request.state, "user_id", None)
    if isinstance(user_id, str) and user_id:
        return user_id
    raise AppError.unauthorized()


def get_authenticated_user_id(request: Request) -> str:
    user_id = getattr(request.state, "authenticated_user_id", None)
    if isinstance(user_id, str) and user_id:
        return user_id
    raise AppError.unauthorized()


DbDep = Annotated[Session, Depends(get_db)]


def load_enabled_user(db: Session, *, user_id: str) -> User:
    user = db.get(User, user_id)
    if user is None or user.disabled_at is not None:
        raise AppError(code="ACCOUNT_DISABLED", message="账号已禁用", status_code=401)
    return user


def get_enabled_current_user_id(request: Request, db: DbDep) -> str:
    user = load_enabled_user(db, user_id=get_current_user_id(request))
    _require_current_session(request, user)
    return str(user.id)


def get_enabled_authenticated_user_id(request: Request, db: DbDep) -> str:
    user = load_enabled_user(db, user_id=get_authenticated_user_id(request))
    _require_current_session(request, user)
    return str(user.id)


def _require_current_session(request: Request, user: User) -> None:
    cookie_version = getattr(request.state, "session_version", None)
    if cookie_version is not None:
        if int(cookie_version) != int(user.session_version or 0):
            raise AppError(code="ACCOUNT_DISABLED", message="账号已禁用", status_code=401)
        return
    invalid_before = coerce_utc_datetime(user.session_invalid_before)
    if invalid_before is None:
        return
    issued_at = coerce_utc_datetime(getattr(request.state, "session_issued_at", None))
    if issued_at is None or issued_at <= invalid_before:
        raise AppError(code="ACCOUNT_DISABLED", message="账号已禁用", status_code=401)


UserIdDep = Annotated[str, Depends(get_enabled_current_user_id)]
AuthenticatedUserIdDep = Annotated[str, Depends(get_enabled_authenticated_user_id)]


def get_admin_user(db: DbDep, user_id: AuthenticatedUserIdDep) -> User:
    user = db.get(User, user_id)
    if user is None or not user.is_admin:
        raise AppError.forbidden()
    return user


AdminUserDep = Annotated[User, Depends(get_admin_user)]


ProjectRole = Literal["viewer", "editor", "owner"]
_ROLE_RANK: dict[str, int] = {"viewer": 1, "editor": 2, "owner": 3}


def _project_role(db: Session, *, project: Project, user_id: str) -> ProjectRole | None:
    if project.owner_user_id == user_id:
        return "owner"
    membership = db.get(ProjectMembership, (project.id, user_id))
    if membership is None:
        return None
    role = str(membership.role or "").strip().lower()
    return role if role in _ROLE_RANK else None


def require_project_access(db: Session, *, project_id: str, user_id: str, min_role: ProjectRole) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise AppError.not_found()

    role = _project_role(db, project=project, user_id=user_id)
    if role is None:
        # Fail-closed to reduce resource existence leaks across projects.
        raise AppError.not_found()

    if _ROLE_RANK[role] < _ROLE_RANK[min_role]:
        raise AppError.forbidden()

    return project


def require_project_viewer(db: Session, *, project_id: str, user_id: str) -> Project:
    return require_project_access(db, project_id=project_id, user_id=user_id, min_role="viewer")


def require_project_editor(db: Session, *, project_id: str, user_id: str) -> Project:
    return require_project_access(db, project_id=project_id, user_id=user_id, min_role="editor")


def require_project_owner(db: Session, *, project_id: str, user_id: str) -> Project:
    return require_project_access(db, project_id=project_id, user_id=user_id, min_role="owner")


def require_character_viewer(db: Session, *, character_id: str, user_id: str) -> Character:
    character = db.get(Character, character_id)
    if character is None:
        raise AppError.not_found()
    require_project_viewer(db, project_id=character.project_id, user_id=user_id)
    return character


def require_character_editor(db: Session, *, character_id: str, user_id: str) -> Character:
    character = db.get(Character, character_id)
    if character is None:
        raise AppError.not_found()
    require_project_editor(db, project_id=character.project_id, user_id=user_id)
    return character


def require_entry_viewer(db: Session, *, entry_id: str, user_id: str) -> "Entry":
    from app.models.entry import Entry

    entry = db.get(Entry, entry_id)
    if entry is None:
        raise AppError.not_found()
    require_project_viewer(db, project_id=entry.project_id, user_id=user_id)
    return entry


def require_entry_editor(db: Session, *, entry_id: str, user_id: str) -> "Entry":
    from app.models.entry import Entry

    entry = db.get(Entry, entry_id)
    if entry is None:
        raise AppError.not_found()
    require_project_editor(db, project_id=entry.project_id, user_id=user_id)
    return entry


def require_chapter_viewer(db: Session, *, chapter_id: str, user_id: str) -> Chapter:
    chapter = db.get(Chapter, chapter_id)
    if chapter is None:
        raise AppError.not_found()
    require_project_viewer(db, project_id=chapter.project_id, user_id=user_id)
    return chapter


def require_chapter_editor(db: Session, *, chapter_id: str, user_id: str) -> Chapter:
    chapter = db.get(Chapter, chapter_id)
    if chapter is None:
        raise AppError.not_found()
    require_project_editor(db, project_id=chapter.project_id, user_id=user_id)
    return chapter


def require_outline_viewer(db: Session, *, outline_id: str, user_id: str) -> Outline:
    outline = db.get(Outline, outline_id)
    if outline is None:
        raise AppError.not_found()
    require_project_viewer(db, project_id=outline.project_id, user_id=user_id)
    return outline


def require_outline_editor(db: Session, *, outline_id: str, user_id: str) -> Outline:
    outline = db.get(Outline, outline_id)
    if outline is None:
        raise AppError.not_found()
    require_project_editor(db, project_id=outline.project_id, user_id=user_id)
    return outline


def require_owned_llm_profile(db: Session, *, profile_id: str, user_id: str) -> LLMProfile:
    profile = db.get(LLMProfile, profile_id)
    if profile is None or profile.owner_user_id != user_id:
        raise AppError.not_found()
    return profile


def require_generation_run_viewer(db: Session, *, run_id: str, user_id: str) -> GenerationRun:
    run = db.get(GenerationRun, run_id)
    if run is None:
        raise AppError.not_found()
    require_project_viewer(db, project_id=run.project_id, user_id=user_id)
    return run


def require_generation_run_editor(db: Session, *, run_id: str, user_id: str) -> GenerationRun:
    run = db.get(GenerationRun, run_id)
    if run is None:
        raise AppError.not_found()
    require_project_editor(db, project_id=run.project_id, user_id=user_id)
    return run


# Backward-compatible alias (owner-only).
def require_owned_project(db: Session, *, project_id: str, user_id: str) -> Project:
    return require_project_owner(db, project_id=project_id, user_id=user_id)


def require_owned_character(db: Session, *, character_id: str, user_id: str) -> Character:
    return require_character_editor(db, character_id=character_id, user_id=user_id)


def require_owned_chapter(db: Session, *, chapter_id: str, user_id: str) -> Chapter:
    return require_chapter_editor(db, chapter_id=chapter_id, user_id=user_id)


def require_owned_outline(db: Session, *, outline_id: str, user_id: str) -> Outline:
    return require_outline_editor(db, outline_id=outline_id, user_id=user_id)


def require_owned_generation_run(db: Session, *, run_id: str, user_id: str) -> GenerationRun:
    return require_generation_run_editor(db, run_id=run_id, user_id=user_id)
