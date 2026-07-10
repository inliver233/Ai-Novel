from __future__ import annotations

import ast
from pathlib import Path
import subprocess
import sys

import pytest

from app.services import (
    vector_build,
    vector_chunk_builder,
    vector_rag_service,
    vector_retrieval,
    vector_storage,
    vector_types,
)

_STORAGE_REEXPORTS = (
    "_is_postgres",
    "_pgvector_ready",
    "_prefer_pgvector",
    "_safe_json_loads",
    "_pgvector_literal",
    "_rrf_contrib",
    "_rrf_score",
    "_default_chroma_persist_dir",
    "_vector_enabled_reason",
    "_normalize_kb_id",
    "_legacy_collection_name",
    "_hash_collection_name",
    "_chroma_collection_naming",
    "_migrate_chroma_collection",
    "_get_collection",
    "_pgvector_upsert_chunks",
    "_pgvector_delete_project",
    "_pgvector_hybrid_fetch",
    "_pgvector_hybrid_query",
    "ingest_chunks",
    "rebuild_project",
    "purge_project_vectors",
)


def test_vector_compatibility_exports_share_owner_identity() -> None:
    type_consumers = (
        vector_build,
        vector_chunk_builder,
        vector_rag_service,
        vector_retrieval,
        vector_storage,
    )
    for module in type_consumers:
        assert module.VectorSource is vector_types.VectorSource
        assert module._ALL_SOURCES is vector_types._ALL_SOURCES

    for module in (vector_build, vector_rag_service, vector_storage):
        assert module.VectorChunk is vector_types.VectorChunk

    for name in _STORAGE_REEXPORTS:
        owner = getattr(vector_storage, name)
        assert getattr(vector_build, name) is owner
        assert getattr(vector_rag_service, name) is owner

    assert "_PGVECTOR_READY_CACHE" not in vars(vector_build)
    assert "_PGVECTOR_READY_CACHE" not in vars(vector_rag_service)
    assert "_PGVECTOR_READY_CACHE" in vars(vector_storage)


def test_vector_build_contains_scheduler_but_no_storage_implementation() -> None:
    services_dir = Path(vector_build.__file__).resolve().parent
    build_tree = ast.parse(Path(vector_build.__file__).read_text(encoding="utf-8"))
    build_functions = {
        node.name
        for node in build_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert build_functions == {"schedule_vector_rebuild_task"}

    cache_owners: list[str] = []
    for path in services_dir.glob("vector_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            target = node.target if isinstance(node, ast.AnnAssign) else None
            if isinstance(target, ast.Name) and target.id == "_PGVECTOR_READY_CACHE":
                cache_owners.append(path.name)
    assert cache_owners == ["vector_storage.py"]


@pytest.mark.parametrize(
    "import_order",
    [
        (
            "vector_types",
            "vector_storage",
            "vector_build",
            "vector_chunk_builder",
            "vector_retrieval",
            "vector_rag_service",
        ),
        (
            "vector_rag_service",
            "vector_retrieval",
            "vector_chunk_builder",
            "vector_build",
            "vector_storage",
            "vector_types",
        ),
        (
            "vector_storage",
            "vector_rag_service",
            "vector_build",
            "vector_retrieval",
            "vector_chunk_builder",
            "vector_types",
        ),
        (
            "vector_retrieval",
            "vector_chunk_builder",
            "vector_build",
            "vector_rag_service",
            "vector_storage",
            "vector_types",
        ),
    ],
)
def test_vector_modules_are_import_order_independent_in_fresh_process(import_order: tuple[str, ...]) -> None:
    module_names = ",".join(import_order)
    script = f"""
import importlib
names = {module_names!r}.split(',')
loaded = {{name: importlib.import_module('app.services.' + name) for name in names}}
types = importlib.import_module('app.services.vector_types')
storage = importlib.import_module('app.services.vector_storage')
build = importlib.import_module('app.services.vector_build')
chunk_builder = importlib.import_module('app.services.vector_chunk_builder')
retrieval = importlib.import_module('app.services.vector_retrieval')
facade = importlib.import_module('app.services.vector_rag_service')
assert all(module._ALL_SOURCES is types._ALL_SOURCES for module in (storage, build, chunk_builder, retrieval, facade))
assert all(module.VectorSource is types.VectorSource for module in (storage, build, chunk_builder, retrieval, facade))
assert build.ingest_chunks is storage.ingest_chunks is facade.ingest_chunks
assert build._pgvector_hybrid_query is storage._pgvector_hybrid_query is facade._pgvector_hybrid_query
assert '_PGVECTOR_READY_CACHE' not in vars(build)
assert '_PGVECTOR_READY_CACHE' not in vars(facade)
assert '_PGVECTOR_READY_CACHE' in vars(storage)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
