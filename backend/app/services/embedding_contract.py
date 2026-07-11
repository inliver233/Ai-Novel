from __future__ import annotations

from app.core.errors import AppError


def effective_vector_backend(*, configured_backend: str, dialect_name: str) -> str:
    configured = str(configured_backend or "auto").strip().lower() or "auto"
    if configured == "chroma":
        return "chroma"
    if configured == "pgvector":
        return "pgvector"
    return "pgvector" if str(dialect_name or "").strip().lower() == "postgresql" else "chroma"


def validate_embedding_expected_dimension(
    value: object,
    *,
    configured_backend: str,
    dialect_name: str,
) -> int:
    try:
        dimension = int(value)
    except (TypeError, ValueError) as exc:
        raise AppError.validation(
            "embedding expected_dimension 必须为整数",
            details={"field": "vector_embedding_expected_dimension"},
        ) from exc
    if dimension < 1 or dimension > 65535:
        raise AppError.validation(
            "embedding expected_dimension 必须在 1..65535 范围内",
            details={"field": "vector_embedding_expected_dimension", "value": dimension},
        )
    backend = effective_vector_backend(configured_backend=configured_backend, dialect_name=dialect_name)
    if backend == "pgvector" and dimension != 1536:
        raise AppError(
            code="PGVECTOR_DIMENSION_UNSUPPORTED",
            message="PostgreSQL pgvector 后端仅支持 1536 维 embedding",
            status_code=422,
            details={
                "expected_dimension": dimension,
                "supported_dimension": 1536,
                "effective_backend": backend,
            },
        )
    return dimension
