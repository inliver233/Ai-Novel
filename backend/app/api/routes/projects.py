from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from pydantic import Field
from sqlalchemy import case, func, select

from app.api.deps import (
    DbDep,
    UserIdDep,
    require_outline_viewer,
    require_owned_llm_profile,
    require_project_owner,
    require_project_viewer,
)
from app.core.errors import AppError, ok_payload
from app.core.logging import exception_log_fields, log_event
from app.db.utils import new_id
from app.llm.utils import default_max_tokens
from app.models.chapter import Chapter
from app.models.character import Character
from app.models.knowledge_base import KnowledgeBase
from app.models.llm_profile import LLMProfile
from app.models.llm_preset import LLMPreset
from app.models.outline import Outline
from app.models.project import Project
from app.models.project_membership import ProjectMembership
from app.models.project_settings import ProjectSettings
from app.models.user import User
from app.schemas.projects import ProjectCreate, ProjectOut, ProjectUpdate
from app.schemas.base import RequestModel
from app.services.import_export_service import import_project_bundle
from app.services.llm_profile_template import apply_profile_template_to_llm_row, normalize_base_url_for_provider
from app.services.prompt_preset_defaults import stage_missing_builtin_prompt_defaults
from app.services.vector_kb_service import ensure_default_kb
from app.services.vector_rag_service import purge_project_vectors

router = APIRouter()
logger = logging.getLogger("ainovel")

PROJECTS_SUMMARY_OUTLINE_MAX_CHARS = 2048


class ProjectMembershipCreate(RequestModel):
    user_id: str = Field(min_length=1, max_length=64)
    role: str = Field(min_length=1, max_length=16)


class ProjectMembershipUpdateRole(RequestModel):
    role: str = Field(min_length=1, max_length=16)


class ProjectBundleImportRequest(RequestModel):
    bundle: dict = Field(default_factory=dict)
    rebuild_vectors: bool = False


def _normalize_membership_role(raw: str) -> str:
    role = (raw or "").strip().lower()
    if role not in ("viewer", "editor"):
        raise AppError.validation("role 必须为 viewer 或 editor")
    return role


def _membership_public(*, membership: ProjectMembership, user: User | None) -> dict:
    return {
        "project_id": membership.project_id,
        "user": {
            "id": membership.user_id,
            "display_name": getattr(user, "display_name", None),
            "is_admin": bool(getattr(user, "is_admin", False)),
        },
        "role": membership.role,
        "created_at": membership.created_at,
        "updated_at": membership.updated_at,
    }


@router.get("/projects")
def list_projects(request: Request, db: DbDep, user_id: UserIdDep) -> dict:
    request_id = request.state.request_id

    owned = select(Project.id).where(Project.owner_user_id == user_id)
    member = select(ProjectMembership.project_id).where(ProjectMembership.user_id == user_id)
    project_ids = db.execute(owned.union(member)).scalars().all()
    projects: list[Project] = []
    if project_ids:
        projects = (
            db.execute(select(Project).where(Project.id.in_(project_ids)).order_by(Project.created_at.desc()))
            .scalars()
            .all()
        )
    return ok_payload(
        request_id=request_id, data={"projects": [ProjectOut.model_validate(p).model_dump() for p in projects]}
    )


@router.get("/projects/summary")
def list_projects_summary(request: Request, db: DbDep, user_id: UserIdDep) -> dict:
    request_id = request.state.request_id
    owned = select(Project.id).where(Project.owner_user_id == user_id)
    member = select(ProjectMembership.project_id).where(ProjectMembership.user_id == user_id)
    project_ids = db.execute(owned.union(member)).scalars().all()
    projects: list[Project] = []
    if project_ids:
        projects = (
            db.execute(select(Project).where(Project.id.in_(project_ids)).order_by(Project.created_at.desc()))
            .scalars()
            .all()
        )
    if not projects:
        return ok_payload(request_id=request_id, data={"items": []})

    project_ids = [p.id for p in projects]

    settings_rows = (
        db.execute(select(ProjectSettings).where(ProjectSettings.project_id.in_(project_ids))).scalars().all()
    )
    settings_by_project_id = {r.project_id: r for r in settings_rows}

    character_count_rows = db.execute(
        select(Character.project_id, func.count(Character.id))
        .where(Character.project_id.in_(project_ids))
        .group_by(Character.project_id)
    ).all()
    character_count_by_project_id = {str(project_id): int(count or 0) for project_id, count in character_count_rows}

    active_outline_ids = [p.active_outline_id for p in projects if p.active_outline_id]
    outline_by_id: dict[str, Outline] = {}
    if active_outline_ids:
        outlines = db.execute(select(Outline).where(Outline.id.in_(active_outline_ids))).scalars().all()
        outline_by_id = {o.id: o for o in outlines}

    chapter_stats_rows = db.execute(
        select(
            Chapter.project_id,
            func.count(Chapter.id),
            func.sum(case((Chapter.status == "done", 1), else_=0)),
        )
        .where(Chapter.project_id.in_(project_ids))
        .group_by(Chapter.project_id)
    ).all()
    chapter_stats_by_project_id: dict[str, tuple[int, int]] = {
        str(project_id): (int(total or 0), int(done or 0)) for project_id, total, done in chapter_stats_rows
    }

    presets = db.execute(select(LLMPreset).where(LLMPreset.project_id.in_(project_ids))).scalars().all()
    preset_by_project_id = {p.project_id: p for p in presets}

    profile_ids = sorted({p.llm_profile_id for p in projects if p.llm_profile_id})
    profile_has_key_by_id: dict[str, bool] = {}
    if profile_ids:
        profile_rows = db.execute(
            select(LLMProfile.id, LLMProfile.api_key_ciphertext)
            .where(LLMProfile.owner_user_id == user_id, LLMProfile.id.in_(profile_ids))
            .order_by(LLMProfile.updated_at.desc())
        ).all()
        profile_has_key_by_id = {str(pid): bool(ciphertext) for pid, ciphertext in profile_rows}

    items: list[dict] = []
    for project in projects:
        settings = settings_by_project_id.get(project.id)
        characters_count = int(character_count_by_project_id.get(project.id, 0))

        outline_content_md = ""
        outline_content_len = 0
        outline_content_truncated = False
        if project.active_outline_id:
            outline = outline_by_id.get(project.active_outline_id)
            full_outline = (outline.content_md or "") if outline is not None else ""
            outline_content_len = len(full_outline)
            outline_content_truncated = outline_content_len > PROJECTS_SUMMARY_OUTLINE_MAX_CHARS
            outline_content_md = (
                full_outline[:PROJECTS_SUMMARY_OUTLINE_MAX_CHARS] if outline_content_truncated else full_outline
            )

        chapters_total, chapters_done = chapter_stats_by_project_id.get(project.id, (0, 0))

        preset = preset_by_project_id.get(project.id)
        llm_preset_out = None
        if preset is not None:
            llm_preset_out = {"provider": preset.provider, "model": preset.model}

        llm_profile_has_api_key = False
        if project.llm_profile_id:
            llm_profile_has_api_key = bool(profile_has_key_by_id.get(project.llm_profile_id, False))

        settings_out = None
        if settings is not None:
            settings_out = {
                "project_id": settings.project_id,
                "world_setting": settings.world_setting or "",
                "style_guide": settings.style_guide or "",
                "constraints": settings.constraints or "",
            }

        items.append(
            {
                "project": ProjectOut.model_validate(project).model_dump(),
                "settings": settings_out,
                "characters_count": characters_count,
                "outline_content_md": outline_content_md,
                "outline_content_len": outline_content_len,
                "outline_content_truncated": outline_content_truncated,
                "chapters_total": chapters_total,
                "chapters_done": chapters_done,
                "llm_preset": llm_preset_out,
                "llm_profile_has_api_key": llm_profile_has_api_key,
            }
        )

    return ok_payload(request_id=request_id, data={"items": items})


@router.post("/projects")
def create_project(request: Request, db: DbDep, user_id: UserIdDep, body: ProjectCreate) -> dict:
    request_id = request.state.request_id
    project = Project(
        id=new_id(),
        owner_user_id=user_id,
        name=body.name,
        genre=body.genre,
        logline=body.logline,
    )
    try:
        db.add(project)
        db.flush()
        db.add(ProjectMembership(project_id=project.id, user_id=user_id, role="owner"))
        stage_missing_builtin_prompt_defaults(db, project_id=project.id)
        ensure_default_kb(db, project_id=project.id, commit=False)
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(project)

    return ok_payload(request_id=request_id, data={"project": ProjectOut.model_validate(project).model_dump()})


@router.post("/projects/import_bundle")
def import_project_bundle_endpoint(
    request: Request, db: DbDep, user_id: UserIdDep, body: ProjectBundleImportRequest
) -> dict:
    request_id = request.state.request_id

    result = import_project_bundle(
        db, owner_user_id=user_id, bundle=body.bundle, rebuild_vectors=bool(body.rebuild_vectors)
    )
    if not bool(result.get("ok")):
        raise AppError.validation(details={"reason": "import_bundle_failed", **result})
    return ok_payload(request_id=request_id, data={"result": result})


@router.get("/projects/{project_id}")
def get_project(request: Request, db: DbDep, user_id: UserIdDep, project_id: str) -> dict:
    request_id = request.state.request_id
    project = require_project_viewer(db, project_id=project_id, user_id=user_id)
    return ok_payload(request_id=request_id, data={"project": ProjectOut.model_validate(project).model_dump()})


@router.get("/projects/{project_id}/memberships")
def list_project_memberships(request: Request, db: DbDep, user_id: UserIdDep, project_id: str) -> dict:
    request_id = request.state.request_id
    require_project_owner(db, project_id=project_id, user_id=user_id)

    rows = db.execute(
        select(ProjectMembership, User)
        .join(User, User.id == ProjectMembership.user_id)
        .where(ProjectMembership.project_id == project_id)
    ).all()
    memberships = [_membership_public(membership=m, user=u) for m, u in rows]
    memberships.sort(key=lambda x: str(x.get("user", {}).get("id") or ""))
    return ok_payload(request_id=request_id, data={"memberships": memberships})


@router.post("/projects/{project_id}/memberships")
def add_project_membership(
    request: Request, db: DbDep, user_id: UserIdDep, project_id: str, body: ProjectMembershipCreate
) -> dict:
    request_id = request.state.request_id
    project = require_project_owner(db, project_id=project_id, user_id=user_id)

    target_user_id = body.user_id.strip()
    if not target_user_id:
        raise AppError.validation("user_id 不能为空")
    if target_user_id == project.owner_user_id:
        raise AppError.validation("不可修改 owner membership")
    role = _normalize_membership_role(body.role)

    target_user = db.get(User, target_user_id)
    if target_user is None:
        raise AppError.not_found("用户不存在")

    exists = db.get(ProjectMembership, (project_id, target_user_id))
    if exists is not None:
        raise AppError.conflict("membership 已存在")

    membership = ProjectMembership(project_id=project_id, user_id=target_user_id, role=role)
    db.add(membership)
    db.commit()
    db.refresh(membership)

    return ok_payload(
        request_id=request_id, data={"membership": _membership_public(membership=membership, user=target_user)}
    )


@router.put("/projects/{project_id}/memberships/{target_user_id}")
def update_project_membership_role(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    project_id: str,
    target_user_id: str,
    body: ProjectMembershipUpdateRole,
) -> dict:
    request_id = request.state.request_id
    project = require_project_owner(db, project_id=project_id, user_id=user_id)

    if target_user_id == project.owner_user_id:
        raise AppError.validation("不可修改 owner membership")

    membership = db.get(ProjectMembership, (project_id, target_user_id))
    if membership is None:
        raise AppError.not_found("membership 不存在")

    membership.role = _normalize_membership_role(body.role)
    db.commit()

    target_user = db.get(User, target_user_id)
    return ok_payload(
        request_id=request_id, data={"membership": _membership_public(membership=membership, user=target_user)}
    )


@router.delete("/projects/{project_id}/memberships/{target_user_id}")
def remove_project_membership(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    project_id: str,
    target_user_id: str,
) -> dict:
    request_id = request.state.request_id
    project = require_project_owner(db, project_id=project_id, user_id=user_id)

    if target_user_id == project.owner_user_id:
        raise AppError.validation("不可移除 owner membership")

    membership = db.get(ProjectMembership, (project_id, target_user_id))
    if membership is None:
        raise AppError.not_found("membership 不存在")

    db.delete(membership)
    db.commit()

    return ok_payload(request_id=request_id, data={})


@router.put("/projects/{project_id}")
def update_project(request: Request, db: DbDep, user_id: UserIdDep, project_id: str, body: ProjectUpdate) -> dict:
    request_id = request.state.request_id
    project = require_project_owner(db, project_id=project_id, user_id=user_id)

    if body.name is not None:
        project.name = body.name
    if body.genre is not None:
        project.genre = body.genre
    if body.logline is not None:
        project.logline = body.logline

    if "active_outline_id" in body.model_fields_set:
        if body.active_outline_id is None:
            project.active_outline_id = None
        else:
            outline = require_outline_viewer(db, outline_id=body.active_outline_id, user_id=user_id)
            if outline.project_id != project_id:
                raise AppError.validation("active_outline_id 不属于当前项目")
            project.active_outline_id = outline.id

    if "llm_profile_id" in body.model_fields_set:
        if body.llm_profile_id is None:
            project.llm_profile_id = None
        else:
            profile = require_owned_llm_profile(db, profile_id=body.llm_profile_id, user_id=user_id)
            project.llm_profile_id = profile.id

            preset = db.get(LLMPreset, project_id)
            if preset is None:
                preset = LLMPreset(
                    project_id=project_id,
                    provider=profile.provider,
                    base_url=normalize_base_url_for_provider(profile.provider, profile.base_url),
                    model=profile.model,
                    temperature=0.7,
                    top_p=1.0,
                    max_tokens=default_max_tokens(profile.provider, profile.model),
                    presence_penalty=0.0,
                    frequency_penalty=0.0,
                    top_k=None,
                    stop_json="[]",
                    timeout_seconds=1200,
                    extra_json="{}",
                )
                db.add(preset)

            apply_profile_template_to_llm_row(row=preset, profile=profile)

    db.commit()
    db.refresh(project)
    return ok_payload(request_id=request_id, data={"project": ProjectOut.model_validate(project).model_dump()})


@router.delete("/projects/{project_id}")
def delete_project(request: Request, db: DbDep, user_id: UserIdDep, project_id: str) -> dict:
    request_id = request.state.request_id
    project = require_project_owner(db, project_id=project_id, user_id=user_id)

    try:
        purge_out = purge_project_vectors(project_id=project_id)
        log_event(logger, "info", event="PROJECT", action="delete_purge", project_id=project_id, vector=purge_out)
    except Exception as exc:  # pragma: no cover - best-effort purge
        log_event(
            logger,
            "warning",
            event="PROJECT",
            action="delete_purge",
            project_id=project_id,
            **exception_log_fields(exc),
        )

    db.delete(project)
    db.commit()
    return ok_payload(request_id=request_id, data={})
