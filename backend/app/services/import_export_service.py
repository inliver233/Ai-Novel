from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from collections import Counter
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import SessionLocal
from app.db.utils import new_id
from app.models.character import Character
from app.models.chapter import Chapter
from app.models.knowledge_base import KnowledgeBase
from app.models.llm_preset import LLMPreset
from app.models.outline import Outline
from app.models.project import Project
from app.models.project_membership import ProjectMembership
from app.models.project_source_document import ProjectSourceDocument, ProjectSourceDocumentChunk
from app.models.project_settings import ProjectSettings
from app.models.prompt_block import PromptBlock
from app.models.prompt_preset import PromptPreset
from app.models.story_memory import StoryMemory
from app.services.vector_embedding_overrides import vector_embedding_overrides
from app.services.vector_rag_service import VectorChunk, ingest_chunks, purge_project_vectors


_TOKEN_RE = re.compile(r"[A-Za-z0-9\u4e00-\u9fff]{2,}")


def _chunk_text(text: str, *, chunk_size: int, overlap: int) -> list[str]:
    s = (text or "").strip()
    if not s:
        return []
    if chunk_size <= 0:
        return [s]

    out: list[str] = []
    start = 0
    overlap = max(0, min(int(overlap), int(chunk_size) - 1)) if chunk_size > 1 else 0
    while start < len(s):
        end = min(len(s), start + chunk_size)
        piece = s[start:end].strip()
        if piece:
            out.append(piece)
        if end >= len(s):
            break
        start = max(0, end - overlap)
    return out


def _base_filename(name: str) -> str:
    raw = str(name or "").strip()
    if not raw:
        return "import"
    if "." in raw:
        raw = raw.rsplit(".", 1)[0]
    raw = raw.strip()
    return raw[:80] if raw else "import"


def _extract_keywords(text: str, *, limit: int) -> list[str]:
    tokens = [t.lower() for t in _TOKEN_RE.findall(text or "") if t]
    if not tokens:
        return []
    counts = Counter(tokens)
    out: list[str] = []
    for token, _ in counts.most_common(max(1, int(limit)) * 3):
        if len(out) >= int(limit):
            break
        if token in {"the", "and", "that", "with", "this", "from", "were", "have", "has"}:
            continue
        out.append(token[:64])
    return out



def _build_story_memory_proposal(*, filename: str, content_text: str) -> dict[str, Any]:
    title = _base_filename(filename)[:255]
    summary = (content_text or "").strip()
    if len(summary) > 800:
        summary = summary[:800].rstrip() + "…"
    return {
        "schema_version": "story_memory_import_v1",
        "memories": [
            {
                "memory_type": "import_summary",
                "title": title,
                "content": summary,
                "importance_score": 0.6,
                "story_timeline": 0,
                "is_foreshadow": 0,
            }
        ],
    }


def run_import_task(task_id: str) -> None:
    """
    ProjectSourceDocument import worker.

    The task_id is the ProjectSourceDocument.id (so it can be scheduled via TaskQueue without a separate task table).
    """

    doc_id = str(task_id or "").strip()
    if not doc_id:
        return

    db = SessionLocal()
    try:
        doc = db.get(ProjectSourceDocument, doc_id)
        if doc is None:
            return

        project_id = str(doc.project_id)
        filename = str(doc.filename or "").strip()
        content_text = str(doc.content_text or "")

        doc.status = "running"
        doc.progress = 0
        doc.progress_message = "切分文本..."
        doc.error_message = None
        db.commit()

        chunk_size = int(getattr(settings, "vector_chunk_size", 800) or 800)
        overlap = int(getattr(settings, "vector_chunk_overlap", 120) or 120)
        chunks = _chunk_text(content_text, chunk_size=chunk_size, overlap=overlap)

        db.execute(delete(ProjectSourceDocumentChunk).where(ProjectSourceDocumentChunk.document_id == doc_id))
        db.flush()

        rows: list[ProjectSourceDocumentChunk] = []
        vector_chunks: list[VectorChunk] = []
        for idx, chunk in enumerate(chunks):
            vector_chunk_id = f"source_doc:{doc_id}:{idx}"
            rows.append(
                ProjectSourceDocumentChunk(
                    id=new_id(),
                    document_id=doc_id,
                    chunk_index=int(idx),
                    content_text=chunk,
                    vector_chunk_id=vector_chunk_id,
                )
            )
            vector_chunks.append(
                VectorChunk(
                    id=vector_chunk_id,
                    text=chunk,
                    metadata={
                        "project_id": project_id,
                        # NOTE: reuse existing VectorSource label to avoid protocol breakage.
                        "source": "chapter",
                        "source_id": doc_id,
                        "title": filename,
                        "chunk_index": int(idx),
                    },
                )
            )

        if rows:
            db.add_all(rows)

        doc.chunk_count = int(len(chunks))
        doc.progress = 35
        doc.progress_message = f"已切分 {len(chunks)} 个 chunk"

        story_memory_proposal = _build_story_memory_proposal(filename=filename, content_text=content_text)
        doc.story_memory_proposal_json = json.dumps(story_memory_proposal, ensure_ascii=False)

        embedding = vector_embedding_overrides(db.get(ProjectSettings, project_id))
        kb_id = str(doc.kb_id or "").strip() or None

        db.commit()
    except Exception as exc:
        try:
            doc = db.get(ProjectSourceDocument, doc_id)
            if doc is not None:
                doc.status = "failed"
                doc.progress_message = "导入失败"
                doc.error_message = f"{type(exc).__name__}"
                db.commit()
        except Exception:
            pass
        return
    finally:
        db.close()

    # external calls (embedding/vector) must happen without holding DB transactions.
    ingest_result: dict[str, Any] = {}
    try:
        ingest_result = ingest_chunks(project_id=project_id, kb_id=kb_id, chunks=vector_chunks, embedding=embedding)
    except Exception as exc:
        ingest_result = {"enabled": False, "skipped": True, "disabled_reason": "error", "error_type": type(exc).__name__}

    db2 = SessionLocal()
    try:
        doc2 = db2.get(ProjectSourceDocument, doc_id)
        if doc2 is None:
            return
        doc2.vector_ingest_result_json = json.dumps(ingest_result, ensure_ascii=False)
        doc2.progress = 100
        doc2.progress_message = "完成"
        doc2.status = "done"
        db2.commit()
    finally:
        db2.close()


def retry_import_task(*, project_id: str, document_id: str) -> dict[str, Any]:
    """
    Best-effort cleanup for retries:
    - delete previous chunks
    - purge vectors for the document kb_id (if any)
    """

    doc_id = str(document_id or "").strip()
    if not doc_id:
        return {"ok": False, "reason": "document_id_missing"}

    kb_id: str | None = None
    db = SessionLocal()
    try:
        doc = db.get(ProjectSourceDocument, doc_id)
        if doc is None or str(doc.project_id) != str(project_id):
            return {"ok": False, "reason": "not_found"}
        kb_id = str(doc.kb_id or "").strip() or None

        doc.status = "queued"
        doc.progress = 0
        doc.progress_message = "queued"
        doc.error_message = None
        doc.vector_ingest_result_json = None
        db.execute(delete(ProjectSourceDocumentChunk).where(ProjectSourceDocumentChunk.document_id == doc_id))
        db.commit()
    finally:
        db.close()

    purge_out: dict[str, Any] = {}
    if kb_id:
        try:
            purge_out = purge_project_vectors(project_id=str(project_id), kb_id=kb_id)
        except Exception as exc:
            purge_out = {"enabled": True, "skipped": True, "deleted": False, "error_type": type(exc).__name__}

    return {"ok": True, "purge": purge_out, "kb_id": kb_id}


def _dt_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    dt = value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _safe_json_list(raw: str | None) -> list:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except Exception:
        return []
    return value if isinstance(value, list) else []


def _safe_json_dict(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def export_project_bundle(db: Session, *, project_id: str) -> dict[str, Any]:
    project_id_norm = str(project_id or "").strip()
    project = db.get(Project, project_id_norm)
    if project is None:
        return {"schema_version": "project_bundle_v1", "error": "project_not_found"}

    settings_row = db.get(ProjectSettings, project_id_norm)
    llm_preset = db.get(LLMPreset, project_id_norm)

    outlines = db.execute(select(Outline).where(Outline.project_id == project_id_norm).order_by(Outline.updated_at.desc())).scalars().all()
    chapters = db.execute(select(Chapter).where(Chapter.project_id == project_id_norm).order_by(Chapter.updated_at.desc())).scalars().all()
    characters = (
        db.execute(select(Character).where(Character.project_id == project_id_norm).order_by(Character.updated_at.desc()))
        .scalars()
        .all()
    )

    prompt_presets = (
        db.execute(select(PromptPreset).where(PromptPreset.project_id == project_id_norm).order_by(PromptPreset.updated_at.desc()))
        .scalars()
        .all()
    )
    blocks_by_preset: dict[str, list[PromptBlock]] = {}
    for preset in prompt_presets:
        blocks = (
            db.execute(select(PromptBlock).where(PromptBlock.preset_id == preset.id).order_by(PromptBlock.injection_order.asc()))
            .scalars()
            .all()
        )
        blocks_by_preset[preset.id] = blocks

    story_memories = (
        db.execute(select(StoryMemory).where(StoryMemory.project_id == project_id_norm).order_by(StoryMemory.updated_at.desc()))
        .scalars()
        .all()
    )

    knowledge_bases = (
        db.execute(select(KnowledgeBase).where(KnowledgeBase.project_id == project_id_norm).order_by(KnowledgeBase.order_index.asc()))
        .scalars()
        .all()
    )

    source_docs = (
        db.execute(select(ProjectSourceDocument).where(ProjectSourceDocument.project_id == project_id_norm).order_by(ProjectSourceDocument.updated_at.desc()))
        .scalars()
        .all()
    )

    return {
        "schema_version": "project_bundle_v1",
        "exported_at": _dt_iso(getattr(project, "updated_at", None)),
        "project": {
            "id": project.id,
            "name": project.name,
            "genre": project.genre,
            "logline": project.logline,
            "active_outline_id": project.active_outline_id,
            "created_at": _dt_iso(getattr(project, "created_at", None)),
            "updated_at": _dt_iso(getattr(project, "updated_at", None)),
        },
        "settings": {
            "world_setting": (settings_row.world_setting if settings_row else "") or "",
            "style_guide": (settings_row.style_guide if settings_row else "") or "",
            "constraints": (settings_row.constraints if settings_row else "") or "",
            "auto_update_worldbook_enabled": bool(getattr(settings_row, "auto_update_worldbook_enabled", True)) if settings_row else True,
            "auto_update_characters_enabled": bool(getattr(settings_row, "auto_update_characters_enabled", True)) if settings_row else True,
            "auto_update_story_memory_enabled": bool(getattr(settings_row, "auto_update_story_memory_enabled", True)) if settings_row else True,
            "auto_update_graph_enabled": bool(getattr(settings_row, "auto_update_graph_enabled", True)) if settings_row else True,
            "auto_update_vector_enabled": bool(getattr(settings_row, "auto_update_vector_enabled", True)) if settings_row else True,
            "auto_update_search_enabled": bool(getattr(settings_row, "auto_update_search_enabled", True)) if settings_row else True,
            "auto_update_fractal_enabled": bool(getattr(settings_row, "auto_update_fractal_enabled", True)) if settings_row else True,
            "auto_update_tables_enabled": bool(getattr(settings_row, "auto_update_tables_enabled", True)) if settings_row else True,
            "vector_embedding": {
                "provider": getattr(settings_row, "vector_embedding_provider", None) if settings_row else None,
                "base_url": getattr(settings_row, "vector_embedding_base_url", None) if settings_row else None,
                "model": getattr(settings_row, "vector_embedding_model", None) if settings_row else None,
                "azure_deployment": getattr(settings_row, "vector_embedding_azure_deployment", None) if settings_row else None,
                "azure_api_version": getattr(settings_row, "vector_embedding_azure_api_version", None) if settings_row else None,
                "sentence_transformers_model": getattr(settings_row, "vector_embedding_sentence_transformers_model", None) if settings_row else None,
                "has_api_key": bool(getattr(settings_row, "vector_embedding_api_key_ciphertext", None) or "") if settings_row else False,
                "masked_api_key": getattr(settings_row, "vector_embedding_api_key_masked", None) if settings_row else "",
            },
            "vector_rerank": {
                "enabled": getattr(settings_row, "vector_rerank_enabled", None) if settings_row else None,
                "method": getattr(settings_row, "vector_rerank_method", None) if settings_row else None,
                "top_k": getattr(settings_row, "vector_rerank_top_k", None) if settings_row else None,
            },
            "query_preprocessing_json": getattr(settings_row, "query_preprocessing_json", None) if settings_row else None,
            "context_optimizer_enabled": bool(getattr(settings_row, "context_optimizer_enabled", False)) if settings_row else False,
        },
        "llm_preset": (
            {
                "provider": llm_preset.provider,
                "base_url": llm_preset.base_url,
                "model": llm_preset.model,
                "temperature": llm_preset.temperature,
                "top_p": llm_preset.top_p,
                "max_tokens": llm_preset.max_tokens,
                "presence_penalty": llm_preset.presence_penalty,
                "frequency_penalty": llm_preset.frequency_penalty,
                "top_k": llm_preset.top_k,
                "stop_json": llm_preset.stop_json,
                "timeout_seconds": llm_preset.timeout_seconds,
            }
            if llm_preset is not None
            else None
        ),
        "outlines": [
            {
                "id": o.id,
                "title": o.title,
                "content_md": o.content_md,
                "structure_json": o.structure_json,
                "created_at": _dt_iso(getattr(o, "created_at", None)),
                "updated_at": _dt_iso(getattr(o, "updated_at", None)),
            }
            for o in outlines
        ],
        "chapters": [
            {
                "id": ch.id,
                "outline_id": ch.outline_id,
                "number": ch.number,
                "title": ch.title,
                "plan": ch.plan,
                "content_md": ch.content_md,
                "summary": ch.summary,
                "status": ch.status,
                "updated_at": _dt_iso(getattr(ch, "updated_at", None)),
            }
            for ch in chapters
        ],
        "characters": [{"id": c.id, "name": c.name, "role": c.role, "profile": c.profile, "notes": c.notes} for c in characters],
        "prompt_presets": {
            "schema_version": "prompt_presets_export_all_v1",
            "presets": [
                {
                    "preset": {
                        "name": p.name,
                        "category": p.category,
                        "scope": p.scope,
                        "version": p.version,
                        "resource_key": p.resource_key,
                        "active_for": _safe_json_list(p.active_for_json),
                    },
                    "blocks": [
                        {
                            "identifier": b.identifier,
                            "name": b.name,
                            "role": b.role,
                            "enabled": bool(b.enabled),
                            "template": b.template,
                            "marker_key": b.marker_key,
                            "injection_position": b.injection_position,
                            "injection_depth": b.injection_depth,
                            "injection_order": b.injection_order,
                            "triggers": _safe_json_list(b.triggers_json),
                            "forbid_overrides": bool(b.forbid_overrides),
                            "budget": _safe_json_dict(b.budget_json),
                            "cache": _safe_json_dict(b.cache_json),
                        }
                        for b in (blocks_by_preset.get(p.id) or [])
                    ],
                }
                for p in prompt_presets
            ],
        },
        "story_memory": {
            "schema_version": "story_memory_export_v1",
            "memories": [
                {
                    "id": m.id,
                    "chapter_id": m.chapter_id,
                    "memory_type": m.memory_type,
                    "title": m.title,
                    "content": m.content,
                    "full_context_md": m.full_context_md,
                    "importance_score": float(m.importance_score),
                    "tags_json": m.tags_json,
                    "story_timeline": int(m.story_timeline),
                    "text_position": int(m.text_position),
                    "text_length": int(m.text_length),
                    "is_foreshadow": int(m.is_foreshadow),
                    "foreshadow_resolved_at_chapter_id": m.foreshadow_resolved_at_chapter_id,
                    "metadata_json": m.metadata_json,
                }
                for m in story_memories
            ],
        },
        "knowledge_bases": {
            "schema_version": "knowledge_bases_export_v1",
            "kbs": [
                {
                    "kb_id": kb.kb_id,
                    "name": kb.name,
                    "enabled": bool(kb.enabled),
                    "weight": float(kb.weight),
                    "order": int(kb.order_index),
                    "priority_group": str(getattr(kb, "priority_group", "normal") or "normal"),
                }
                for kb in knowledge_bases
            ],
        },
        "source_documents": {
            "schema_version": "project_source_documents_export_v1",
            "docs": [
                {
                    "id": d.id,
                    "filename": d.filename,
                    "content_type": d.content_type,
                    "content_text": d.content_text,
                    "kb_id": d.kb_id,
                    "story_memory_proposal_json": d.story_memory_proposal_json,
                }
                for d in source_docs
            ],
        },
    }


def import_project_bundle(
    db: Session,
    *,
    owner_user_id: str,
    bundle: dict[str, Any],
    rebuild_vectors: bool = False,
) -> dict[str, Any]:
    schema = str(bundle.get("schema_version") or "").strip()
    if schema != "project_bundle_v1":
        return {"ok": False, "reason": "unsupported_schema_version", "schema_version": schema}

    project_in = bundle.get("project")
    project_obj = project_in if isinstance(project_in, dict) else {}
    name = str(project_obj.get("name") or "").strip() or "Imported Project"
    genre = str(project_obj.get("genre") or "").strip() or None
    logline = str(project_obj.get("logline") or "").strip() or None
    active_outline_id = str(project_obj.get("active_outline_id") or "").strip() or None

    new_project_id = new_id()
    db.add(Project(id=new_project_id, owner_user_id=str(owner_user_id), name=name[:255], genre=genre, logline=logline))
    db.add(ProjectMembership(project_id=new_project_id, user_id=str(owner_user_id), role="owner"))
    db.flush()

    report: dict[str, Any] = {"created": {}, "warnings": []}

    settings_in = bundle.get("settings")
    settings_obj = settings_in if isinstance(settings_in, dict) else {}
    settings_row = ProjectSettings(project_id=new_project_id)
    settings_row.world_setting = str(settings_obj.get("world_setting") or "") or None
    settings_row.style_guide = str(settings_obj.get("style_guide") or "") or None
    settings_row.constraints = str(settings_obj.get("constraints") or "") or None

    embedding_in = settings_obj.get("vector_embedding")
    embedding_obj = embedding_in if isinstance(embedding_in, dict) else {}
    settings_row.vector_embedding_provider = str(embedding_obj.get("provider") or "") or None
    settings_row.vector_embedding_base_url = str(embedding_obj.get("base_url") or "") or None
    settings_row.vector_embedding_model = str(embedding_obj.get("model") or "") or None
    settings_row.vector_embedding_azure_deployment = str(embedding_obj.get("azure_deployment") or "") or None
    settings_row.vector_embedding_azure_api_version = str(embedding_obj.get("azure_api_version") or "") or None
    settings_row.vector_embedding_sentence_transformers_model = str(embedding_obj.get("sentence_transformers_model") or "") or None

    has_key = bool(embedding_obj.get("has_api_key"))
    settings_row.vector_embedding_api_key_ciphertext = None
    settings_row.vector_embedding_api_key_masked = str(embedding_obj.get("masked_api_key") or "") if has_key else None
    if has_key:
        report["warnings"].append("api_key_not_imported")

    rerank_in = settings_obj.get("vector_rerank")
    rerank_obj = rerank_in if isinstance(rerank_in, dict) else {}
    settings_row.vector_rerank_enabled = rerank_obj.get("enabled")
    settings_row.vector_rerank_method = str(rerank_obj.get("method") or "") or None
    settings_row.vector_rerank_top_k = int(rerank_obj.get("top_k")) if isinstance(rerank_obj.get("top_k"), int) else None

    settings_row.query_preprocessing_json = str(settings_obj.get("query_preprocessing_json") or "") or None
    settings_row.context_optimizer_enabled = bool(settings_obj.get("context_optimizer_enabled", False))
    settings_row.auto_update_worldbook_enabled = bool(settings_obj.get("auto_update_worldbook_enabled", True))
    settings_row.auto_update_characters_enabled = bool(settings_obj.get("auto_update_characters_enabled", True))
    settings_row.auto_update_story_memory_enabled = bool(settings_obj.get("auto_update_story_memory_enabled", True))
    settings_row.auto_update_graph_enabled = bool(settings_obj.get("auto_update_graph_enabled", True))
    settings_row.auto_update_vector_enabled = bool(settings_obj.get("auto_update_vector_enabled", True))
    settings_row.auto_update_search_enabled = bool(settings_obj.get("auto_update_search_enabled", True))
    settings_row.auto_update_fractal_enabled = bool(settings_obj.get("auto_update_fractal_enabled", True))
    settings_row.auto_update_tables_enabled = bool(settings_obj.get("auto_update_tables_enabled", True))

    db.add(settings_row)
    report["created"]["project_settings"] = 1

    llm_in = bundle.get("llm_preset")
    llm_obj = llm_in if isinstance(llm_in, dict) else {}
    if llm_obj:
        db.add(
            LLMPreset(
                project_id=new_project_id,
                provider=str(llm_obj.get("provider") or "openai"),
                base_url=str(llm_obj.get("base_url") or "") or None,
                model=str(llm_obj.get("model") or "gpt-4o-mini"),
                temperature=llm_obj.get("temperature"),
                top_p=llm_obj.get("top_p"),
                max_tokens=llm_obj.get("max_tokens"),
                presence_penalty=llm_obj.get("presence_penalty"),
                frequency_penalty=llm_obj.get("frequency_penalty"),
                top_k=llm_obj.get("top_k"),
                stop_json=str(llm_obj.get("stop_json") or "") or None,
                timeout_seconds=llm_obj.get("timeout_seconds"),
            )
        )
        report["created"]["llm_preset"] = 1

    outline_id_map: dict[str, str] = {}
    outlines_in = bundle.get("outlines")
    outlines_list = outlines_in if isinstance(outlines_in, list) else []
    for o in outlines_list:
        if not isinstance(o, dict):
            continue
        old_id = str(o.get("id") or "").strip()
        new_outline_id = new_id()
        outline_id_map[old_id] = new_outline_id
        db.add(
            Outline(
                id=new_outline_id,
                project_id=new_project_id,
                title=str(o.get("title") or "Outline")[:255],
                content_md=str(o.get("content_md") or "") or None,
                structure_json=str(o.get("structure_json") or "") or None,
            )
        )
    report["created"]["outlines"] = len(outline_id_map)

    chapter_id_map: dict[str, str] = {}
    chapters_in = bundle.get("chapters")
    chapters_list = chapters_in if isinstance(chapters_in, list) else []
    for ch in chapters_list:
        if not isinstance(ch, dict):
            continue
        old_id = str(ch.get("id") or "").strip()
        old_outline_id = str(ch.get("outline_id") or "").strip()
        outline_new = outline_id_map.get(old_outline_id)
        if not outline_new:
            continue
        new_ch_id = new_id()
        chapter_id_map[old_id] = new_ch_id
        try:
            number = int(ch.get("number") or 0)
        except Exception:
            number = 0
        db.add(
            Chapter(
                id=new_ch_id,
                project_id=new_project_id,
                outline_id=outline_new,
                number=max(0, int(number)),
                title=str(ch.get("title") or "") or None,
                plan=str(ch.get("plan") or "") or None,
                content_md=str(ch.get("content_md") or "") or None,
                summary=str(ch.get("summary") or "") or None,
                status=str(ch.get("status") or "planned")[:32],
            )
        )
    report["created"]["chapters"] = len(chapter_id_map)

    if active_outline_id:
        mapped = outline_id_map.get(active_outline_id)
        if mapped:
            project_row = db.get(Project, new_project_id)
            if project_row is not None:
                project_row.active_outline_id = mapped

    characters_in = bundle.get("characters")
    characters_list = characters_in if isinstance(characters_in, list) else []
    created_chars = 0
    for c in characters_list:
        if not isinstance(c, dict):
            continue
        created_chars += 1
        db.add(
            Character(
                id=new_id(),
                project_id=new_project_id,
                name=str(c.get("name") or "")[:255] or "角色",
                role=str(c.get("role") or "") or None,
                profile=str(c.get("profile") or "") or None,
                notes=str(c.get("notes") or "") or None,
            )
        )
    report["created"]["characters"] = created_chars

    presets_in = bundle.get("prompt_presets")
    presets_obj = presets_in if isinstance(presets_in, dict) else {}
    presets_list = presets_obj.get("presets")
    presets_items = presets_list if isinstance(presets_list, list) else []
    created_presets = 0
    created_blocks = 0
    for item in presets_items:
        if not isinstance(item, dict):
            continue
        preset_in = item.get("preset")
        preset_obj = preset_in if isinstance(preset_in, dict) else {}
        blocks_in = item.get("blocks")
        blocks_list = blocks_in if isinstance(blocks_in, list) else []

        preset_id = new_id()
        created_presets += 1
        db.add(
            PromptPreset(
                id=preset_id,
                project_id=new_project_id,
                name=str(preset_obj.get("name") or "")[:255] or "Preset",
                category=str(preset_obj.get("category") or "")[:64] or None,
                scope=str(preset_obj.get("scope") or "project")[:32] or "project",
                version=int(preset_obj.get("version") or 1),
                resource_key=str(preset_obj.get("resource_key") or "") or None,
                active_for_json=json.dumps(preset_obj.get("active_for") or [], ensure_ascii=False),
            )
        )
        db.flush()

        for b in blocks_list:
            if not isinstance(b, dict):
                continue
            created_blocks += 1
            db.add(
                PromptBlock(
                    id=new_id(),
                    preset_id=preset_id,
                    identifier=str(b.get("identifier") or "")[:255],
                    name=str(b.get("name") or "")[:255],
                    role=str(b.get("role") or "user")[:32],
                    enabled=bool(b.get("enabled", True)),
                    template=str(b.get("template") or ""),
                    marker_key=str(b.get("marker_key") or "") or None,
                    injection_position=str(b.get("injection_position") or "relative")[:32],
                    injection_depth=b.get("injection_depth"),
                    injection_order=int(b.get("injection_order") or 0),
                    triggers_json=json.dumps(b.get("triggers") or [], ensure_ascii=False),
                    forbid_overrides=bool(b.get("forbid_overrides", False)),
                    budget_json=json.dumps(b.get("budget") or {}, ensure_ascii=False) if b.get("budget") is not None else None,
                    cache_json=json.dumps(b.get("cache") or {}, ensure_ascii=False) if b.get("cache") is not None else None,
                )
            )
    report["created"]["prompt_presets"] = created_presets
    report["created"]["prompt_blocks"] = created_blocks

    sm2_in = bundle.get("story_memory")
    sm2_obj = sm2_in if isinstance(sm2_in, dict) else {}
    memories_in = sm2_obj.get("memories")
    memories_list = memories_in if isinstance(memories_in, list) else []
    created_story_memories = 0
    for m in memories_list:
        if not isinstance(m, dict):
            continue
        ch_old = str(m.get("chapter_id") or "").strip() or None
        resolved_old = str(m.get("foreshadow_resolved_at_chapter_id") or "").strip() or None
        created_story_memories += 1
        db.add(
            StoryMemory(
                id=new_id(),
                project_id=new_project_id,
                chapter_id=chapter_id_map.get(ch_old) if ch_old else None,
                memory_type=str(m.get("memory_type") or "note")[:64] or "note",
                title=str(m.get("title") or "")[:255] or None,
                content=str(m.get("content") or ""),
                full_context_md=str(m.get("full_context_md") or "") or None,
                importance_score=float(m.get("importance_score") or 0.0),
                tags_json=str(m.get("tags_json") or "") or None,
                story_timeline=int(m.get("story_timeline") or 0),
                text_position=int(m.get("text_position") or -1),
                text_length=int(m.get("text_length") or 0),
                is_foreshadow=int(m.get("is_foreshadow") or 0),
                foreshadow_resolved_at_chapter_id=chapter_id_map.get(resolved_old) if resolved_old else None,
                metadata_json=str(m.get("metadata_json") or "") or None,
            )
        )
    report["created"]["story_memories"] = created_story_memories

    kb_in = bundle.get("knowledge_bases")
    kb_obj = kb_in if isinstance(kb_in, dict) else {}
    kbs_in = kb_obj.get("kbs")
    kbs_list = kbs_in if isinstance(kbs_in, list) else []
    created_kbs = 0
    for kb in kbs_list:
        if not isinstance(kb, dict):
            continue
        created_kbs += 1
        kb_id = str(kb.get("kb_id") or "").strip() or "default"
        db.add(
            KnowledgeBase(
                id=new_id(),
                project_id=new_project_id,
                kb_id=kb_id[:64],
                name=str(kb.get("name") or kb_id)[:255] or kb_id,
                enabled=bool(kb.get("enabled", True)),
                weight=float(kb.get("weight") or 1.0),
                order_index=int(kb.get("order") or 0),
                priority_group=str(kb.get("priority_group") or "normal")[:16] or "normal",
            )
        )
    report["created"]["knowledge_bases"] = created_kbs

    docs_in = bundle.get("source_documents")
    docs_obj = docs_in if isinstance(docs_in, dict) else {}
    docs_list = docs_obj.get("docs")
    docs_items = docs_list if isinstance(docs_list, list) else []
    created_docs = 0
    for d in docs_items:
        if not isinstance(d, dict):
            continue
        created_docs += 1
        kb_id = str(d.get("kb_id") or "").strip() or None
        db.add(
            ProjectSourceDocument(
                id=new_id(),
                project_id=new_project_id,
                actor_user_id=str(owner_user_id),
                filename=str(d.get("filename") or "")[:255],
                content_type=str(d.get("content_type") or "txt")[:32] or "txt",
                content_text=str(d.get("content_text") or ""),
                status="done",
                progress=100,
                progress_message="imported",
                chunk_count=0,
                kb_id=kb_id[:64] if kb_id else None,
                vector_ingest_result_json=None,
                story_memory_proposal_json=str(d.get("story_memory_proposal_json") or "") or None,
                error_message=None,
            )
        )
    report["created"]["source_documents"] = created_docs

    db.commit()

    from app.services.prompt_presets import ensure_default_chapter_preset, ensure_default_outline_preset
    from app.services.vector_kb_service import ensure_default_kb as ensure_default_vector_kb

    ensure_default_outline_preset(db, project_id=new_project_id, activate=True)
    ensure_default_chapter_preset(db, project_id=new_project_id, activate=True)
    ensure_default_vector_kb(db, project_id=new_project_id)
    db.commit()

    vector_rebuild_result: dict[str, Any] | None = None
    if rebuild_vectors:
        from app.services.vector_rag_service import build_project_chunks, rebuild_project

        db2 = SessionLocal()
        try:
            chunks = build_project_chunks(db=db2, project_id=new_project_id, sources=["outline", "chapter"])
            embedding = vector_embedding_overrides(db2.get(ProjectSettings, new_project_id))
            selected_kbs = [str(k.get("kb_id") or "").strip() for k in kbs_list if isinstance(k, dict)] or ["default"]
        finally:
            db2.close()

        per_kb: dict[str, dict[str, Any]] = {}
        for kid in selected_kbs:
            if not kid:
                continue
            try:
                per_kb[kid] = rebuild_project(project_id=new_project_id, kb_id=kid, chunks=chunks, embedding=embedding)
            except Exception as exc:  # pragma: no cover - env dependent
                per_kb[kid] = {"enabled": False, "skipped": True, "disabled_reason": "error", "error_type": type(exc).__name__}
        vector_rebuild_result = {"kbs": {"selected": selected_kbs, "per_kb": per_kb}}

    return {"ok": True, "project_id": new_project_id, "report": report, "vector_rebuild": vector_rebuild_result}
