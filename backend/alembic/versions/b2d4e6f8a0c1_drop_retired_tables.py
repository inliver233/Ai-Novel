"""drop retired tables after verified archival

Revision ID: b2d4e6f8a0c1
Revises: 9f3a7c2d1e4b
Create Date: 2026-07-11 00:00:00.000000

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "b2d4e6f8a0c1"
down_revision = "9f3a7c2d1e4b"
branch_labels = None
depends_on = None


# This immutable list is intentionally local to the migration. Widening a
# destructive historical migration because application code changed later
# would be unsafe.
RETIRED_TABLES = (
    "entities",
    "relations",
    "events",
    "foreshadows",
    "evidence",
    "memory_change_sets",
    "memory_change_set_items",
    "memory_tasks",
    "project_tables",
    "project_table_rows",
    "worldbook_entries",
    "glossary_terms",
    "plot_analysis",
    "fractal_memory",
)

_CREATE_ORDER = RETIRED_TABLES
_DROP_ORDER = tuple(reversed(_CREATE_ORDER))


def _quoted_table_list(bind: sa.Connection, table_names: tuple[str, ...]) -> str:
    quote = bind.dialect.identifier_preparer.quote
    return ", ".join(quote(name) for name in table_names)


def _lock_and_require_empty() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect not in {"sqlite", "postgresql"}:
        raise RuntimeError(
            "retired-table cleanup supports only SQLite and PostgreSQL; "
            f"refusing destructive migration on dialect={dialect!r}"
        )

    present = set(sa.inspect(bind).get_table_names())
    missing = sorted(set(RETIRED_TABLES) - present)
    if missing:
        raise RuntimeError(
            "retired-table cleanup expected the complete legacy schema but tables are missing; "
            f"refusing a partial destructive migration: {missing}"
        )

    if dialect == "postgresql":
        # One statement lets PostgreSQL acquire all locks together and keeps
        # the count check and DDL in Alembic's surrounding transaction.
        bind.exec_driver_sql(
            f"LOCK TABLE {_quoted_table_list(bind, tuple(sorted(RETIRED_TABLES)))} IN ACCESS EXCLUSIVE MODE"
        )
    else:
        # SQLite has database-level writer locking rather than LOCK TABLE.
        # This no-op write acquires the RESERVED writer lock before any count,
        # preventing a concurrent writer from racing the preflight and DROP.
        bind.exec_driver_sql("UPDATE alembic_version SET version_num = version_num")

    non_empty: list[tuple[str, int]] = []
    quote = bind.dialect.identifier_preparer.quote
    for table_name in RETIRED_TABLES:
        row_count = int(bind.exec_driver_sql(f"SELECT COUNT(*) FROM {quote(table_name)}").scalar_one())
        if row_count:
            non_empty.append((table_name, row_count))

    if not non_empty:
        return

    counts = ", ".join(f"{name}={count}" for name, count in non_empty)
    raise RuntimeError(
        "retired-table cleanup found data and aborted before dropping any table "
        f"({counts}). Preserve it first: for SQLite run "
        "`python scripts/archive_retired_tables.py --database-url <source-url> archive --output <archive-dir>` "
        "then `python scripts/archive_retired_tables.py --database-url <source-url> purge --archive <archive-dir> "
        "--confirm PURGE_RETIRED_TABLE_DATA`; for PostgreSQL run "
        "`pg_dump --format=custom --file=<archive.dump> --table=entities --table=relations --table=events "
        "--table=foreshadows --table=evidence --table=memory_change_sets --table=memory_change_set_items "
        "--table=memory_tasks --table=project_tables --table=project_table_rows --table=worldbook_entries "
        "--table=glossary_terms --table=plot_analysis --table=fractal_memory <database>` and verify the dump, then "
        "explicitly purge the archived retired rows. Retry the migration only after all 14 tables are empty."
    )


def upgrade() -> None:
    _lock_and_require_empty()
    for table_name in _DROP_ORDER:
        op.drop_table(table_name)


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    op.create_table(
        "entities",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("summary_md", sa.Text(), nullable=True),
        sa.Column("attributes_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "entity_type", "name", name="uq_entities_project_type_name"),
    )
    op.create_index("ix_entities_project_id", "entities", ["project_id"], unique=False)
    op.create_index("ix_entities_project_id_entity_type", "entities", ["project_id", "entity_type"], unique=False)
    op.create_index(
        "ix_entities_project_id_deleted_at_updated_at",
        "entities",
        ["project_id", "deleted_at", "updated_at"],
        unique=False,
    )

    op.create_table(
        "relations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("from_entity_id", sa.String(length=36), nullable=False),
        sa.Column("to_entity_id", sa.String(length=36), nullable=False),
        sa.Column("relation_type", sa.String(length=64), nullable=False),
        sa.Column("description_md", sa.Text(), nullable=True),
        sa.Column("attributes_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["from_entity_id"], ["entities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["to_entity_id"], ["entities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id",
            "from_entity_id",
            "to_entity_id",
            "relation_type",
            name="uq_relations_project_from_to_type",
        ),
    )
    op.create_index("ix_relations_project_id", "relations", ["project_id"], unique=False)
    op.create_index(
        "ix_relations_project_id_from_entity_id",
        "relations",
        ["project_id", "from_entity_id"],
        unique=False,
    )
    op.create_index(
        "ix_relations_project_id_to_entity_id",
        "relations",
        ["project_id", "to_entity_id"],
        unique=False,
    )
    op.create_index(
        "ix_relations_project_id_deleted_at_updated_at",
        "relations",
        ["project_id", "deleted_at", "updated_at"],
        unique=False,
    )

    op.create_table(
        "events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("chapter_id", sa.String(length=36), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("content_md", sa.Text(), nullable=False),
        sa.Column("attributes_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["chapter_id"], ["chapters.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_events_project_id", "events", ["project_id"], unique=False)
    op.create_index("ix_events_project_id_chapter_id", "events", ["project_id", "chapter_id"], unique=False)
    op.create_index(
        "ix_events_project_id_deleted_at_updated_at",
        "events",
        ["project_id", "deleted_at", "updated_at"],
        unique=False,
    )

    op.create_table(
        "foreshadows",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("chapter_id", sa.String(length=36), nullable=True),
        sa.Column("resolved_at_chapter_id", sa.String(length=36), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("content_md", sa.Text(), nullable=False),
        sa.Column("resolved", sa.Integer(), nullable=False),
        sa.Column("attributes_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["chapter_id"], ["chapters.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["resolved_at_chapter_id"], ["chapters.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_foreshadows_project_id", "foreshadows", ["project_id"], unique=False)
    op.create_index("ix_foreshadows_project_id_resolved", "foreshadows", ["project_id", "resolved"], unique=False)
    op.create_index(
        "ix_foreshadows_project_id_deleted_at_updated_at",
        "foreshadows",
        ["project_id", "deleted_at", "updated_at"],
        unique=False,
    )

    op.create_table(
        "evidence",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=True),
        sa.Column("quote_md", sa.Text(), nullable=False),
        sa.Column("attributes_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_evidence_project_id", "evidence", ["project_id"], unique=False)
    op.create_index(
        "ix_evidence_project_id_source", "evidence", ["project_id", "source_type", "source_id"], unique=False
    )
    op.create_index(
        "ix_evidence_project_id_deleted_at_created_at",
        "evidence",
        ["project_id", "deleted_at", "created_at"],
        unique=False,
    )

    op.create_table(
        "memory_change_sets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("actor_user_id", sa.String(length=64), nullable=True),
        sa.Column("generation_run_id", sa.String(length=36), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("summary_md", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rolled_back_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('proposed','applied','rolled_back','failed')", name="ck_memory_change_sets_status"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["generation_run_id"], ["generation_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "idempotency_key", name="uq_memory_change_sets_project_idempotency_key"),
    )
    op.create_index("ix_memory_change_sets_project_id", "memory_change_sets", ["project_id"], unique=False)
    op.create_index(
        "ix_memory_change_sets_project_id_status",
        "memory_change_sets",
        ["project_id", "status"],
        unique=False,
    )

    op.create_table(
        "memory_change_set_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("change_set_id", sa.String(length=36), nullable=False),
        sa.Column("item_index", sa.Integer(), nullable=False),
        sa.Column("target_table", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.String(length=64), nullable=True),
        sa.Column("op", sa.String(length=16), nullable=False),
        sa.Column("before_json", sa.Text(), nullable=True),
        sa.Column("after_json", sa.Text(), nullable=True),
        sa.Column("evidence_ids_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("op IN ('upsert','delete')", name="ck_memory_change_set_items_op"),
        sa.CheckConstraint(
            "target_table IN ('entities','relations','events','foreshadows','evidence','project_table_rows')",
            name="ck_memory_change_set_items_target_table",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["change_set_id"], ["memory_change_sets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("change_set_id", "item_index", name="uq_memory_change_set_items_change_set_index"),
    )
    op.create_index("ix_memory_change_set_items_project_id", "memory_change_set_items", ["project_id"], unique=False)
    op.create_index(
        "ix_memory_change_set_items_change_set_id", "memory_change_set_items", ["change_set_id"], unique=False
    )
    op.create_index(
        "ix_memory_change_set_items_project_target",
        "memory_change_set_items",
        ["project_id", "target_table"],
        unique=False,
    )

    op.create_table(
        "memory_tasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("change_set_id", sa.String(length=36), nullable=False),
        sa.Column("actor_user_id", sa.String(length=36), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="queued"),
        sa.Column("params_json", sa.Text(), nullable=True),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("error_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('queued','running','succeeded','failed')", name="ck_memory_tasks_status"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["change_set_id"], ["memory_change_sets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("change_set_id", "kind", name="uq_memory_tasks_change_set_kind"),
    )
    op.create_index("ix_memory_tasks_project_id", "memory_tasks", ["project_id"], unique=False)
    op.create_index("ix_memory_tasks_change_set_id", "memory_tasks", ["change_set_id"], unique=False)
    op.create_index("ix_memory_tasks_status", "memory_tasks", ["status"], unique=False)
    op.create_index("ix_memory_tasks_kind", "memory_tasks", ["kind"], unique=False)

    op.create_table(
        "project_tables",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("table_key", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("schema_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "auto_update_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true() if dialect == "sqlite" else None,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "table_key", name="uq_project_tables_project_id_table_key"),
    )
    op.create_index("ix_project_tables_project_id", "project_tables", ["project_id"], unique=False)
    op.create_index(
        "ix_project_tables_project_id_table_key", "project_tables", ["project_id", "table_key"], unique=False
    )

    op.create_table(
        "project_table_rows",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("table_id", sa.String(length=36), nullable=False),
        sa.Column("row_index", sa.Integer(), nullable=False),
        sa.Column("data_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["table_id"], ["project_tables.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_project_table_rows_project_id", "project_table_rows", ["project_id"], unique=False)
    op.create_index("ix_project_table_rows_table_id", "project_table_rows", ["table_id"], unique=False)
    op.create_index(
        "ix_project_table_rows_table_id_row_index",
        "project_table_rows",
        ["table_id", "row_index"],
        unique=False,
    )

    op.create_table(
        "worldbook_entries",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("content_md", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("constant", sa.Boolean(), nullable=False),
        sa.Column("keywords_json", sa.Text(), nullable=True),
        sa.Column("exclude_recursion", sa.Boolean(), nullable=False),
        sa.Column("prevent_recursion", sa.Boolean(), nullable=False),
        sa.Column("char_limit", sa.Integer(), nullable=False),
        sa.Column("priority", sa.String(length=32), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_worldbook_entries_project_id_constant_enabled",
        "worldbook_entries",
        ["project_id", "constant", "enabled"],
        unique=False,
    )
    op.create_index(
        "ix_worldbook_entries_project_id_enabled",
        "worldbook_entries",
        ["project_id", "enabled"],
        unique=False,
    )

    op.create_table(
        "glossary_terms",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("term", sa.String(length=255), nullable=False),
        sa.Column("aliases_json", sa.Text(), nullable=False),
        sa.Column("sources_json", sa.Text(), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("enabled", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "term", name="uq_glossary_terms_project_term"),
    )
    op.create_index("ix_glossary_terms_project_id", "glossary_terms", ["project_id"], unique=False)
    op.create_index("ix_glossary_terms_term", "glossary_terms", ["term"], unique=False)

    op.create_table(
        "plot_analysis",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("chapter_id", sa.String(length=36), nullable=False),
        sa.Column("analysis_json", sa.Text(), nullable=False),
        sa.Column("overall_quality_score", sa.Float(), nullable=True),
        sa.Column("coherence_score", sa.Float(), nullable=True),
        sa.Column("engagement_score", sa.Float(), nullable=True),
        sa.Column("pacing_score", sa.Float(), nullable=True),
        sa.Column("analysis_report_md", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["chapter_id"], ["chapters.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chapter_id", name="uq_plot_analysis_chapter_id"),
    )
    op.create_index(
        "ix_plot_analysis_project_id_chapter_id", "plot_analysis", ["project_id", "chapter_id"], unique=False
    )
    op.create_index(
        "ix_plot_analysis_project_id_created_at", "plot_analysis", ["project_id", "created_at"], unique=False
    )

    op.create_table(
        "fractal_memory",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("config_json", sa.Text(), nullable=True),
        sa.Column("scenes_json", sa.Text(), nullable=False),
        sa.Column("arcs_json", sa.Text(), nullable=False),
        sa.Column("sagas_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", name="uq_fractal_memory_project_id"),
    )
    op.create_index("ix_fractal_memory_project_id", "fractal_memory", ["project_id"], unique=False)
