from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import (
    auth_admin,
    auth_local,
    auth_oidc_linuxdo,
    auth_session,
    batch_generation,
    chapters,
    characters,
    detailed_outlines,
    entries,
    export,
    generation_runs,
    health,
    import_export,
    llm,
    llm_capabilities,
    llm_models,
    llm_preset,
    llm_profiles,
    llm_task_presets,
    mcp,
    memory,
    outline,
    outlines,
    projects,
    prompts,
    prompt_studio,
    search,
    settings,
    story_memory,
    vector,
    vector_rag_profiles,
    writing_styles,
)
from app.api.routes import outline_parse

api_router = APIRouter(prefix="/api")

api_router.include_router(health.router, tags=["health"])
api_router.include_router(auth_session.router, tags=["auth"])
api_router.include_router(auth_local.router, tags=["auth"])
api_router.include_router(auth_oidc_linuxdo.router, tags=["auth"])
api_router.include_router(auth_admin.router, tags=["auth"])
api_router.include_router(projects.router, tags=["projects"])
api_router.include_router(memory.router, tags=["memory"])
api_router.include_router(mcp.router, tags=["mcp"])
api_router.include_router(search.router, tags=["search"])
api_router.include_router(vector.router, tags=["vector"])
api_router.include_router(settings.router, tags=["settings"])
api_router.include_router(characters.router, tags=["characters"])
api_router.include_router(entries.router, tags=["entries"])
api_router.include_router(outline.router, tags=["outline"])
api_router.include_router(chapters.router, tags=["chapters"])
api_router.include_router(batch_generation.router, tags=["batch_generation"])
api_router.include_router(prompts.router, tags=["prompts"])
api_router.include_router(prompt_studio.router, tags=["prompt_studio"])
api_router.include_router(llm_preset.router, tags=["llm_preset"])
api_router.include_router(llm_task_presets.router, tags=["llm_task_presets"])
api_router.include_router(llm_capabilities.router, tags=["llm_capabilities"])
api_router.include_router(llm_models.router, tags=["llm_models"])
api_router.include_router(llm.router, tags=["llm"])
api_router.include_router(llm_profiles.router, tags=["llm_profiles"])
api_router.include_router(outlines.router, tags=["outlines"])
api_router.include_router(detailed_outlines.router, tags=["detailed_outlines"])
api_router.include_router(outline_parse.router, tags=["outline_parse"])
api_router.include_router(export.router, tags=["export"])
api_router.include_router(import_export.router, tags=["import_export"])
api_router.include_router(generation_runs.router, tags=["generation_runs"])
api_router.include_router(story_memory.router, tags=["story_memory"])
api_router.include_router(vector_rag_profiles.router, tags=["vector_rag_profiles"])
api_router.include_router(writing_styles.router, tags=["writing_styles"])
