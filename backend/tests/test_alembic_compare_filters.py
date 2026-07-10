from __future__ import annotations

import pytest

from app.db.alembic_compare import include_object_for_dialect


@pytest.mark.parametrize(
    ("dialect_name", "table_name", "expected"),
    (
        ("sqlite", "search_index", False),
        ("sqlite", "search_index_data", False),
        ("sqlite", "vector_chunks", True),
        ("sqlite", "ordinary_table", True),
        ("postgresql", "vector_chunks", False),
        ("postgresql", "search_index", True),
        ("postgresql", "search_index_data", True),
        ("postgresql", "ordinary_table", True),
        ("mysql", "vector_chunks", True),
        ("mysql", "search_index", True),
    ),
)
def test_include_object_filter_is_exact_and_dialect_scoped(
    dialect_name: str,
    table_name: str,
    expected: bool,
) -> None:
    include_object = include_object_for_dialect(dialect_name)

    assert include_object(object(), table_name, "table", True, None) is expected


@pytest.mark.parametrize("dialect_name", ("sqlite", "postgresql"))
def test_include_object_filter_never_hides_metadata_or_non_table_objects(dialect_name: str) -> None:
    include_object = include_object_for_dialect(dialect_name)
    dialect_owned_name = "search_index" if dialect_name == "sqlite" else "vector_chunks"

    assert include_object(object(), dialect_owned_name, "table", False, None) is True
    assert include_object(object(), dialect_owned_name, "table", True, object()) is True
    assert include_object(object(), dialect_owned_name, "index", True, None) is True
