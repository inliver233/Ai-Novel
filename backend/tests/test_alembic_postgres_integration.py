from __future__ import annotations

import os
import threading
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from starlette.requests import Request
from alembic import command
from alembic.runtime.migration import MigrationContext
from sqlalchemy.orm import sessionmaker

from app.db.migrations import _alembic_config
from app.core.config import settings
from app.core.errors import AppError
from app.api.routes import batch_generation as batch_generation_routes
from app.models.batch_generation_task import BatchGenerationQuotaGuard, BatchGenerationTask
from app.models.chapter import Chapter
from app.models.llm_preset import LLMPreset
from app.models.outline import Outline
from app.models.project import Project
from app.models.prompt_preset import PromptPreset
from app.models.user import User
from app.schemas.batch_generation import BatchGenerationCreateRequest
from app.services.prompt_preset_defaults import sync_builtin_prompt_defaults
from app.services.batch_generation_quota import (
    enter_batch_generation_quota_admission,
    lock_and_enforce_batch_generation_quotas,
)
from scripts.check_alembic import (
    SchemaContractError,
    assert_schema_matches_metadata,
    check_empty_database,
    migration_head,
)


PRE_CLEANUP_REVISION = "9f3a7c2d1e4b"
HEAD_REVISION = "f3a1c7e9b2d4"
EXPECTED_DATABASE = "ainovel_schema_ci"
DESTRUCTIVE_SENTINEL = "I_UNDERSTAND_THIS_DROPS_PUBLIC_SCHEMA"
RETIRED_TABLES = {
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
}


def _validate_destructive_url(database_url: str, *, sentinel: str | None) -> None:
    url = sa.engine.make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise RuntimeError("TEST_POSTGRES_URL must be a PostgreSQL URL")
    if url.host not in {"localhost", "127.0.0.1", "::1"}:
        raise RuntimeError("destructive PostgreSQL integration requires a loopback host")
    if url.database != EXPECTED_DATABASE:
        raise RuntimeError(f"destructive PostgreSQL integration requires database {EXPECTED_DATABASE!r}")
    if sentinel != DESTRUCTIVE_SENTINEL:
        raise RuntimeError("destructive PostgreSQL integration sentinel is missing or incorrect")


def _postgres_url() -> str:
    enabled = os.getenv("AINOVEL_POSTGRES_INTEGRATION") == "1"
    database_url = os.getenv("TEST_POSTGRES_URL", "")
    if not enabled or not database_url:
        pytest.skip("set AINOVEL_POSTGRES_INTEGRATION=1 and TEST_POSTGRES_URL to run")
    try:
        _validate_destructive_url(
            database_url,
            sentinel=os.getenv("AINOVEL_POSTGRES_INTEGRATION_SENTINEL"),
        )
    except RuntimeError as exc:
        pytest.fail(str(exc))
    return database_url


@pytest.mark.parametrize(
    ("database_url", "sentinel", "message"),
    [
        (
            "postgresql+psycopg2://ainovel:ainovel@db.example/ainovel_schema_ci",
            DESTRUCTIVE_SENTINEL,
            "loopback host",
        ),
        (
            "postgresql+psycopg2://ainovel:ainovel@localhost/production",
            DESTRUCTIVE_SENTINEL,
            "requires database",
        ),
        (
            "postgresql+psycopg2://ainovel:ainovel@localhost/ainovel_schema_ci",
            "wrong",
            "sentinel",
        ),
    ],
)
def test_destructive_postgres_identity_fails_closed(
    database_url: str,
    sentinel: str,
    message: str,
) -> None:
    with pytest.raises(RuntimeError, match=message):
        _validate_destructive_url(database_url, sentinel=sentinel)


def _upgrade(database_url: str, revision: str) -> None:
    config = _alembic_config(database_url=database_url)
    previous_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        command.upgrade(config, revision)
    finally:
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url


def _alembic_check(database_url: str) -> None:
    config = _alembic_config(database_url=database_url)
    previous_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        command.check(config)
    finally:
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url


def _assert_postgres_quota_race(
    session_factory: sessionmaker,
    *,
    dimension: str,
    first: tuple[str, str, str],
    second: tuple[str, str, str],
    limits: tuple[int, int, int],
) -> None:
    with session_factory() as db:
        db.query(BatchGenerationTask).delete(synchronize_session=False)
        db.query(BatchGenerationQuotaGuard).delete(synchronize_session=False)
        db.commit()

    barrier = threading.Barrier(2)
    results: list[str] = []
    errors: list[BaseException] = []

    def _admit(index: int, values: tuple[str, str, str]) -> None:
        project_id, user_id, provider = values
        try:
            with session_factory() as db:
                barrier.wait(timeout=10)
                enter_batch_generation_quota_admission(db)
                lock_and_enforce_batch_generation_quotas(
                    db,
                    project_id=project_id,
                    user_id=user_id,
                    provider=provider,
                )
                db.add(
                    BatchGenerationTask(
                        id=f"postgres-{dimension}-{index}",
                        project_id=project_id,
                        outline_id=f"outline-{project_id}",
                        actor_user_id=user_id,
                        runtime_provider=provider,
                        status="queued",
                    )
                )
                db.commit()
                results.append("admitted")
        except AppError as exc:
            results.append(str(exc.details.get("quota")))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    project_limit, user_limit, provider_limit = limits
    with (
        patch.object(settings, "batch_generation_project_active_limit", project_limit),
        patch.object(settings, "batch_generation_user_active_limit", user_limit),
        patch.object(settings, "batch_generation_provider_active_limit", provider_limit),
    ):
        threads = [
            threading.Thread(target=_admit, args=(1, first)),
            threading.Thread(target=_admit, args=(2, second)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert sorted(results) == ["admitted", dimension]
    with session_factory() as db:
        assert db.query(BatchGenerationTask).count() == 1


def _assert_postgres_create_route_race(session_factory: sessionmaker) -> None:
    before_outline = threading.Barrier(2)
    after_chapter = threading.Barrier(2)
    results: list[str] = []
    errors: list[BaseException] = []
    original_ensure = batch_generation_routes.ensure_active_outline

    def _ensure_with_candidate(db, *, project):  # type: ignore[no-untyped-def]
        before_outline.wait(timeout=10)
        outline = original_ensure(db, project=project)
        chapter_id = f"chapter-{outline.id}"
        if db.get(Chapter, chapter_id) is None:
            db.add(
                Chapter(
                    id=chapter_id,
                    project_id=project.id,
                    outline_id=outline.id,
                    number=1,
                    title="Chapter",
                )
            )
            db.commit()
        after_chapter.wait(timeout=10)
        return outline

    class _NoopQueue:
        def enqueue_batch_generation_task(self, task_id: str) -> str:
            return task_id

    def _create(index: int) -> None:
        try:
            with session_factory() as db:
                request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
                request.state.request_id = f"postgres-create-{index}"
                batch_generation_routes.create_batch_generation_task(
                    request=request,
                    db=db,
                    user_id="quota-user-1",
                    project_id="quota-create-project",
                    body=BatchGenerationCreateRequest(count=1, include_existing=True),
                )
                results.append("admitted")
        except AppError as exc:
            results.append(str(exc.details.get("quota")))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    with (
        patch.object(batch_generation_routes, "ensure_active_outline", side_effect=_ensure_with_candidate),
        patch.object(batch_generation_routes, "get_task_queue", return_value=_NoopQueue()),
        patch.object(settings, "batch_generation_project_active_limit", 1),
        patch.object(settings, "batch_generation_user_active_limit", 10),
        patch.object(settings, "batch_generation_provider_active_limit", 10),
    ):
        threads = [threading.Thread(target=_create, args=(1,)), threading.Thread(target=_create, args=(2,))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert sorted(results) == ["admitted", "project"]
    with session_factory() as db:
        assert db.query(BatchGenerationTask).filter_by(project_id="quota-create-project").count() == 1


@pytest.mark.postgres_integration
def test_pgvector_cleanup_reconciliation_and_alembic_contract() -> None:
    """Exercise the real PostgreSQL-only DDL and the two current migrations."""
    database_url = _postgres_url()
    engine = sa.create_engine(database_url)
    destructive_target_verified = False
    try:
        # This test is destructive by design and only runs against the isolated
        # CI service when explicitly enabled.
        with engine.connect() as connection:
            actual_database = str(connection.exec_driver_sql("SELECT current_database()").scalar_one())
            if actual_database != EXPECTED_DATABASE:
                raise RuntimeError(
                    f"connected database {actual_database!r} does not match destructive target {EXPECTED_DATABASE!r}"
                )
            destructive_target_verified = True
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP SCHEMA IF EXISTS public CASCADE")
            connection.exec_driver_sql("CREATE SCHEMA public")
            connection.exec_driver_sql("DROP SCHEMA IF EXISTS rogue_contract CASCADE")
            connection.exec_driver_sql("CREATE SCHEMA rogue_contract")
            connection.exec_driver_sql("CREATE DOMAIN public.rogue_domain AS TEXT")

        with pytest.raises(SchemaContractError, match=r"non_public_schemas.*rogue_contract.*user_types.*rogue_domain"):
            check_empty_database(database_url)

        with engine.begin() as connection:
            connection.exec_driver_sql("DROP SCHEMA rogue_contract CASCADE")
            connection.exec_driver_sql("DROP SCHEMA public CASCADE")
            connection.exec_driver_sql("CREATE SCHEMA public")

        _upgrade(database_url, PRE_CLEANUP_REVISION)
        with engine.connect() as connection:
            tables = set(sa.inspect(connection).get_table_names())
            assert RETIRED_TABLES.issubset(tables)
            actor_user_id = next(
                column
                for column in sa.inspect(connection).get_columns("batch_generation_tasks")
                if column["name"] == "actor_user_id"
            )
            assert isinstance(actor_user_id["type"], sa.String)
            assert actor_user_id["type"].length == 36

        _upgrade(database_url, "head")
        with engine.connect() as connection:
            inspector = sa.inspect(connection)
            tables = set(inspector.get_table_names())
            assert tables.isdisjoint(RETIRED_TABLES)
            assert "vector_chunks" in tables
            actor_user_id = next(
                column
                for column in inspector.get_columns("batch_generation_tasks")
                if column["name"] == "actor_user_id"
            )
            assert actor_user_id["type"].length == 64
            runtime_provider = next(
                column
                for column in inspector.get_columns("batch_generation_tasks")
                if column["name"] == "runtime_provider"
            )
            assert runtime_provider["type"].length == 64
            assert "batch_generation_quota_guards" in tables
            batch_indexes = {str(index["name"]) for index in inspector.get_indexes("batch_generation_tasks")}
            assert {
                "ix_batch_generation_tasks_project_status",
                "ix_batch_generation_tasks_actor_status",
                "ix_batch_generation_tasks_provider_status",
            }.issubset(batch_indexes)
            is_admin = next(column for column in inspector.get_columns("users") if column["name"] == "is_admin")
            assert is_admin["default"] is not None
            current = tuple(MigrationContext.configure(connection).get_current_heads())
            assert current == (HEAD_REVISION,)
            assert migration_head(database_url) == HEAD_REVISION
            # Programmatic equivalent of ``alembic check`` with the same
            # compare-type/default and dialect-specific unmanaged-table filter.
            assert_schema_matches_metadata(connection)
        # Also execute Alembic's public command path so env.py itself is part
        # of the PostgreSQL contract, rather than only the shared primitives.
        _alembic_check(database_url)

        session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        with session_factory() as db:
            db.add(User(id="prompt-sync-user", display_name="Prompt Sync"))
            db.commit()
            db.add(
                Project(
                    id="prompt-sync-project",
                    owner_user_id="prompt-sync-user",
                    name="Prompt Sync Project",
                    genre=None,
                    logline=None,
                )
            )
            db.commit()

        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def _sync_concurrently() -> None:
            try:
                with session_factory() as db:
                    barrier.wait(timeout=10)
                    sync_builtin_prompt_defaults(db, project_id="prompt-sync-project")
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=_sync_concurrently), threading.Thread(target=_sync_concurrently)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert not errors
        assert all(not thread.is_alive() for thread in threads)
        with session_factory() as db:
            rows = (
                db.query(PromptPreset)
                .filter(PromptPreset.project_id == "prompt-sync-project")
                .all()
            )
            assert len(rows) == 6
            assert len({row.resource_key for row in rows}) == 6

        with session_factory() as db:
            db.add_all(
                [
                    User(id="quota-user-1", display_name="Quota User 1"),
                    User(id="quota-user-2", display_name="Quota User 2"),
                ]
            )
            db.commit()
            db.add_all(
                [
                    Project(
                        id="quota-project-1",
                        owner_user_id="quota-user-1",
                        name="Quota Project 1",
                        genre=None,
                        logline=None,
                    ),
                    Project(
                        id="quota-create-project",
                        owner_user_id="quota-user-1",
                        name="Quota Create Project",
                        genre=None,
                        logline=None,
                    ),
                    Project(
                        id="quota-project-2",
                        owner_user_id="quota-user-2",
                        name="Quota Project 2",
                        genre=None,
                        logline=None,
                    ),
                ]
            )
            db.commit()
            db.add(LLMPreset(project_id="quota-create-project", provider="openai", model="gpt-4o-mini"))
            db.commit()
            db.add_all(
                [
                    Outline(id="outline-quota-project-1", project_id="quota-project-1", title="Quota Outline 1"),
                    Outline(id="outline-quota-project-2", project_id="quota-project-2", title="Quota Outline 2"),
                ]
            )
            db.commit()

        _assert_postgres_create_route_race(session_factory)
        _assert_postgres_quota_race(
            session_factory,
            dimension="project",
            first=("quota-project-1", "quota-user-1", "openai"),
            second=("quota-project-1", "quota-user-2", "anthropic"),
            limits=(1, 10, 10),
        )
        _assert_postgres_quota_race(
            session_factory,
            dimension="user",
            first=("quota-project-1", "quota-user-1", "openai"),
            second=("quota-project-2", "quota-user-1", "anthropic"),
            limits=(10, 1, 10),
        )
        _assert_postgres_quota_race(
            session_factory,
            dimension="provider",
            first=("quota-project-1", "quota-user-1", "openai"),
            second=("quota-project-2", "quota-user-2", "openai"),
            limits=(10, 10, 1),
        )

        explain_cases = (
            (
                "ix_batch_generation_tasks_project_status",
                "project_id",
                "quota-project-1",
            ),
            (
                "ix_batch_generation_tasks_actor_status",
                "actor_user_id",
                "quota-user-1",
            ),
            (
                "ix_batch_generation_tasks_provider_status",
                "runtime_provider",
                "openai",
            ),
        )
        with engine.begin() as connection:
            connection.exec_driver_sql("SET LOCAL enable_seqscan = off")
            for index_name, column_name, scope_key in explain_cases:
                plan = connection.execute(
                    sa.text(
                        f"""
                        EXPLAIN (FORMAT JSON)
                        SELECT COUNT(*)
                        FROM batch_generation_tasks
                        WHERE {column_name} = :scope_key
                          AND status IN ('queued', 'running', 'paused')
                        """
                    ),
                    {"scope_key": scope_key},
                ).scalar_one()
                assert index_name in str(plan)
    finally:
        if destructive_target_verified:
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
                connection.exec_driver_sql("DROP SCHEMA IF EXISTS public CASCADE")
                connection.exec_driver_sql("DROP SCHEMA IF EXISTS rogue_contract CASCADE")
        engine.dispose()
