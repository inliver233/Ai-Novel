from __future__ import annotations

from collections.abc import Callable
from typing import Any


IncludeObject = Callable[[Any, str | None, str, bool, Any], bool]

_SQLITE_UNMANAGED_TABLES = frozenset(
    {
        "search_index",
        "search_index_config",
        "search_index_content",
        "search_index_data",
        "search_index_docsize",
        "search_index_idx",
    }
)
_POSTGRESQL_UNMANAGED_TABLES = frozenset({"vector_chunks"})


def include_object_for_dialect(dialect_name: str) -> IncludeObject:
    """Return an autogenerate filter for dialect-owned raw-DDL tables."""
    if dialect_name == "sqlite":
        unmanaged_tables = _SQLITE_UNMANAGED_TABLES
    elif dialect_name == "postgresql":
        unmanaged_tables = _POSTGRESQL_UNMANAGED_TABLES
    else:
        unmanaged_tables = frozenset()

    def include_object(_object: Any, name: str | None, type_: str, reflected: bool, compare_to: Any) -> bool:
        # Only suppress reflected tables that have no metadata counterpart.
        # SQLite FTS5 owns search_index and its shadow tables; PostgreSQL's
        # vector_chunks is maintained by pgvector-aware raw SQL migrations.
        # Keeping both exact and dialect-scoped prevents a same-named normal
        # table on the other dialect from escaping drift detection.
        return not (type_ == "table" and reflected and compare_to is None and name in unmanaged_tables)

    return include_object
