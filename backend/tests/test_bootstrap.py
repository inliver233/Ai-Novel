from __future__ import annotations

import ast
import asyncio
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from unittest.mock import MagicMock, call, patch

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from app import bootstrap
from app import main as app_main
from app.core.errors import AppError
from app.db import migrations

_BACKEND_DIR = Path(__file__).resolve().parents[1]
_MAIN_FILE = _BACKEND_DIR / "app" / "main.py"
_BOOTSTRAP_FILE = _BACKEND_DIR / "app" / "bootstrap.py"
_ENTRYPOINT_FILE = _BACKEND_DIR / "scripts" / "entrypoint.sh"


def _current_alembic_head(database_url: str) -> str:
    config = migrations._alembic_config(database_url=database_url)
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head is not None
    return head


def _bootstrap_subprocess_env(database_path: Path, **overrides: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "APP_ENV": "dev",
            "DATABASE_URL": f"sqlite:///{database_path.as_posix()}",
            "AUTH_ADMIN_USER_ID": "",
            "AUTH_ADMIN_PASSWORD": "",
            "AUTH_DEV_FALLBACK_USER_ID": "",
            "SECRET_ENCRYPTION_KEY": "7fBl3GuKPdb-zsPc0uPXZ8xJnDPbazidy-_BsW8owGM=",
        }
    )
    env.update(overrides)
    return env


def _run_bootstrap_subprocess(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "app.bootstrap"],
        cwd=_BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _database_revision(database_path: Path) -> str:
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        with engine.connect() as connection:
            return connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    finally:
        engine.dispose()


@pytest.mark.parametrize("value", ["1", "true", "TRUE", " yes ", "On"])
def test_env_truthy_accepts_existing_true_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("BOOTSTRAP_TEST_FLAG", value)
    assert bootstrap._env_truthy("BOOTSTRAP_TEST_FLAG") is True


@pytest.mark.parametrize("value", ["0", "false", "FALSE", " no ", "Off"])
def test_env_truthy_accepts_existing_false_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("BOOTSTRAP_TEST_FLAG", value)
    assert bootstrap._env_truthy("BOOTSTRAP_TEST_FLAG") is False


@pytest.mark.parametrize("value", [None, "", "maybe"])
def test_env_truthy_preserves_unset_or_invalid_fallback(
    monkeypatch: pytest.MonkeyPatch,
    value: str | None,
) -> None:
    if value is None:
        monkeypatch.delenv("BOOTSTRAP_TEST_FLAG", raising=False)
    else:
        monkeypatch.setenv("BOOTSTRAP_TEST_FLAG", value)
    assert bootstrap._env_truthy("BOOTSTRAP_TEST_FLAG") is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, 1), ("", 1), ("invalid", 1), ("0", 1), ("-2", 1), ("1", 1), ("4", 4)],
)
def test_web_concurrency_preserves_existing_normalization(
    monkeypatch: pytest.MonkeyPatch,
    value: str | None,
    expected: int,
) -> None:
    if value is None:
        monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    else:
        monkeypatch.setenv("WEB_CONCURRENCY", value)
    assert bootstrap._web_concurrency() == expected


def _set_policy_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    done: str | None = None,
    in_app: str | None = None,
    workers: str | None = None,
) -> None:
    values = {
        "AINOVEL_BOOTSTRAP_DONE": done,
        "AINOVEL_BOOTSTRAP_IN_APP": in_app,
        "WEB_CONCURRENCY": workers,
    }
    for name, value in values.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def test_done_marker_has_precedence_over_in_app_override(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_policy_env(monkeypatch, done="true", in_app="true", workers="1")
    monkeypatch.setattr(bootstrap.settings, "app_env", "dev")
    assert bootstrap.should_bootstrap_in_app() is False


@pytest.mark.parametrize(("override", "expected"), [("true", True), ("false", False)])
def test_in_app_override_is_preserved_in_all_environments(
    monkeypatch: pytest.MonkeyPatch,
    override: str,
    expected: bool,
) -> None:
    _set_policy_env(monkeypatch, in_app=override, workers="8")
    monkeypatch.setattr(bootstrap.settings, "app_env", "prod")
    assert bootstrap.should_bootstrap_in_app() is expected


@pytest.mark.parametrize(
    ("app_env", "workers", "expected"),
    [("prod", "1", False), ("dev", "1", True), ("dev", "2", False), ("dev", "invalid", True)],
)
def test_default_in_app_policy_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
    app_env: str,
    workers: str,
    expected: bool,
) -> None:
    _set_policy_env(monkeypatch, workers=workers)
    monkeypatch.setattr(bootstrap.settings, "app_env", app_env)
    assert bootstrap.should_bootstrap_in_app() is expected


def test_run_bootstrap_orders_schema_before_admin() -> None:
    events = MagicMock()
    with (
        patch.object(bootstrap, "ensure_db_schema", side_effect=lambda: events("schema")),
        patch.object(bootstrap, "ensure_configured_admin_user", side_effect=lambda: events("admin")),
    ):
        bootstrap.run_bootstrap()
    assert events.call_args_list == [call("schema"), call("admin")]


def test_run_bootstrap_does_not_attempt_admin_when_schema_fails() -> None:
    failure = RuntimeError("migration failed")
    with (
        patch.object(bootstrap, "ensure_db_schema", side_effect=failure),
        patch.object(bootstrap, "ensure_configured_admin_user") as ensure_admin,
        pytest.raises(RuntimeError, match="migration failed"),
    ):
        bootstrap.run_bootstrap()
    ensure_admin.assert_not_called()


@pytest.mark.parametrize("allowed", [False, True])
def test_bootstrap_in_app_uses_shared_policy(allowed: bool) -> None:
    with (
        patch.object(bootstrap, "should_bootstrap_in_app", return_value=allowed),
        patch.object(bootstrap, "run_bootstrap") as run_bootstrap,
    ):
        assert bootstrap.bootstrap_in_app() is allowed
    assert run_bootstrap.call_count == int(allowed)


def test_admin_bootstrap_closes_session_on_unexpected_error() -> None:
    dummy_db = MagicMock()
    with (
        patch.object(bootstrap, "SessionLocal", return_value=dummy_db),
        patch.object(bootstrap, "ensure_admin_user", side_effect=RuntimeError("admin failed")),
        pytest.raises(RuntimeError, match="admin failed"),
    ):
        bootstrap.ensure_configured_admin_user()
    dummy_db.close.assert_called_once_with()


def test_admin_bootstrap_does_not_hide_other_dev_validation_errors() -> None:
    dummy_db = MagicMock()
    with (
        patch.object(bootstrap, "SessionLocal", return_value=dummy_db),
        patch.object(bootstrap, "ensure_admin_user", side_effect=AppError.validation("other")),
        patch.object(bootstrap.settings, "app_env", "dev"),
        patch.object(bootstrap.settings, "auth_admin_password", "long-enough"),
        pytest.raises(AppError),
    ):
        bootstrap.ensure_configured_admin_user()
    dummy_db.close.assert_called_once_with()


def test_cli_configures_logging_before_running_bootstrap() -> None:
    events = MagicMock()
    with (
        patch.object(bootstrap, "configure_logging", side_effect=lambda: events("logging")),
        patch.object(bootstrap, "run_bootstrap", side_effect=lambda: events("bootstrap")),
    ):
        bootstrap.main()
    assert events.call_args_list == [call("logging"), call("bootstrap")]


def test_lifespan_runs_shared_bootstrap_once_in_complete_startup_shutdown_order() -> None:
    events = MagicMock()
    watchdog_handle = object()

    def start_watchdog() -> object:
        events("watchdog_start")
        return watchdog_handle

    async def exercise_lifespan() -> None:
        async with app_main.lifespan(app_main.app):
            events("serving")

    with (
        patch.object(app_main, "configure_logging", side_effect=lambda: events("logging")),
        patch.object(app_main, "bootstrap_in_app", side_effect=lambda: events("bootstrap")),
        patch.object(app_main, "_warn_sqlite_single_worker", side_effect=lambda: events("sqlite_warning")),
        patch.object(app_main, "_ensure_local_user", side_effect=lambda: events("local_user")),
        patch.object(app_main, "start_project_task_watchdog", side_effect=start_watchdog),
        patch.object(
            app_main,
            "stop_project_task_watchdog",
            side_effect=lambda handle: events("watchdog_stop", handle),
        ),
        patch.object(app_main, "close_llm_http_client", side_effect=lambda: events("llm_close")),
    ):
        asyncio.run(exercise_lifespan())

    assert events.call_args_list == [
        call("logging"),
        call("bootstrap"),
        call("sqlite_warning"),
        call("local_user"),
        call("watchdog_start"),
        call("serving"),
        call("watchdog_stop", watchdog_handle),
        call("llm_close"),
    ]


def test_lifespan_bootstrap_failure_prevents_application_startup() -> None:
    events = MagicMock()

    def fail_bootstrap() -> None:
        events("bootstrap")
        raise RuntimeError("bootstrap failed")

    async def exercise_lifespan() -> None:
        async with app_main.lifespan(app_main.app):
            events("serving")

    with (
        patch.object(app_main, "configure_logging", side_effect=lambda: events("logging")),
        patch.object(app_main, "bootstrap_in_app", side_effect=fail_bootstrap),
        patch.object(app_main, "_warn_sqlite_single_worker") as warn_sqlite,
        patch.object(app_main, "_ensure_local_user") as ensure_local_user,
        patch.object(app_main, "start_project_task_watchdog") as start_watchdog,
        patch.object(app_main, "stop_project_task_watchdog") as stop_watchdog,
        patch.object(app_main, "close_llm_http_client") as close_llm,
        pytest.raises(RuntimeError, match="bootstrap failed"),
    ):
        asyncio.run(exercise_lifespan())

    assert events.call_args_list == [call("logging"), call("bootstrap")]
    warn_sqlite.assert_not_called()
    ensure_local_user.assert_not_called()
    start_watchdog.assert_not_called()
    stop_watchdog.assert_not_called()
    close_llm.assert_not_called()


def test_python_module_cli_bootstraps_fresh_sqlite_database(tmp_path: Path) -> None:
    database_path = tmp_path / "bootstrap.db"
    env = _bootstrap_subprocess_env(
        database_path,
        # The CLI is intentionally unconditional; this marker only controls in-app bootstrap.
        AINOVEL_BOOTSTRAP_DONE="1",
    )
    completed = _run_bootstrap_subprocess(env)
    assert completed.returncode == 0, completed.stderr or completed.stdout

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        assert "users" in inspect(engine).get_table_names()
    finally:
        engine.dispose()
    assert _database_revision(database_path) == _current_alembic_head(env["DATABASE_URL"])


def test_dev_short_admin_password_cli_fails_soft_without_persisting_or_leaking(tmp_path: Path) -> None:
    database_path = tmp_path / "dev-short-password.db"
    raw_password = "leakme"
    admin_user_id = "short-password-admin"
    env = _bootstrap_subprocess_env(
        database_path,
        APP_ENV="dev",
        AUTH_ADMIN_USER_ID=admin_user_id,
        AUTH_ADMIN_PASSWORD=raw_password,
    )
    completed = _run_bootstrap_subprocess(env)
    combined_output = completed.stdout + completed.stderr

    assert completed.returncode == 0, combined_output
    assert _database_revision(database_path) == _current_alembic_head(env["DATABASE_URL"])

    engine = create_engine(env["DATABASE_URL"])
    try:
        with engine.connect() as connection:
            admin_count = connection.execute(
                text("SELECT COUNT(*) FROM users WHERE id = :user_id"),
                {"user_id": admin_user_id},
            ).scalar_one()
        assert admin_count == 0
    finally:
        engine.dispose()

    warning_events: list[dict[str, object]] = []
    for raw_line in combined_output.splitlines():
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if payload.get("event") == "AUTH_ADMIN_BOOTSTRAP":
            warning_events.append(payload)
    assert len(warning_events) == 1
    warning = warning_events[0]
    assert warning["level"] == "warning"
    assert warning["event"] == "AUTH_ADMIN_BOOTSTRAP"
    assert warning["action"] == "skipped"
    assert warning["reason"] == "invalid_password"
    assert warning["admin_user_id"] == admin_user_id
    assert warning["password_length"] == len(raw_password)
    assert warning["min_password_length"] == 8
    assert raw_password not in combined_output


def test_prod_short_admin_password_cli_fails_during_config_before_creating_database(tmp_path: Path) -> None:
    database_path = tmp_path / "prod-short-password.db"
    raw_password = "leakme"
    admin_user_id = "prod-short-password-admin"
    env = _bootstrap_subprocess_env(
        database_path,
        APP_ENV="prod",
        CORS_ORIGINS="https://example.test",
        AUTH_ADMIN_USER_ID=admin_user_id,
        AUTH_ADMIN_PASSWORD=raw_password,
    )
    completed = _run_bootstrap_subprocess(env)
    combined_output = completed.stdout + completed.stderr

    assert completed.returncode != 0
    assert "AUTH_ADMIN_PASSWORD must not use weak or default credentials" in combined_output
    assert not database_path.exists()


def test_invalid_prod_configuration_cli_fails_before_creating_database(tmp_path: Path) -> None:
    database_path = tmp_path / "invalid-prod-config.db"
    env = _bootstrap_subprocess_env(
        database_path,
        APP_ENV="prod",
        CORS_ORIGINS="https://example.test",
        SECRET_ENCRYPTION_KEY="",
    )
    completed = _run_bootstrap_subprocess(env)
    assert completed.returncode != 0
    assert "SECRET_ENCRYPTION_KEY" in completed.stderr
    assert not database_path.exists()


def test_bootstrap_logic_has_one_python_owner() -> None:
    main_tree = ast.parse(_MAIN_FILE.read_text(encoding="utf-8"))
    bootstrap_tree = ast.parse(_BOOTSTRAP_FILE.read_text(encoding="utf-8"))
    main_functions = {node.name for node in main_tree.body if isinstance(node, ast.FunctionDef)}
    bootstrap_functions = {node.name for node in bootstrap_tree.body if isinstance(node, ast.FunctionDef)}

    retired_main_functions = {"_env_truthy", "_web_concurrency", "_should_bootstrap_in_app", "_ensure_admin_user"}
    assert retired_main_functions.isdisjoint(main_functions)
    assert {
        "_env_truthy",
        "_web_concurrency",
        "should_bootstrap_in_app",
        "ensure_configured_admin_user",
        "run_bootstrap",
        "bootstrap_in_app",
        "main",
    }.issubset(bootstrap_functions)

    main_source = _MAIN_FILE.read_text(encoding="utf-8")
    assert "from app.db.migrations import ensure_db_schema" not in main_source
    assert "from app.services.auth_service import ensure_admin_user" not in main_source
    bootstrap_calls = [
        node
        for node in ast.walk(main_tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "bootstrap_in_app"
    ]
    assert len(bootstrap_calls) == 1


def test_entrypoint_delegates_bootstrap_and_sets_marker_after_success() -> None:
    source = _ENTRYPOINT_FILE.read_text(encoding="utf-8")
    command = "python -m app.bootstrap"
    marker = "export AINOVEL_BOOTSTRAP_DONE=1"

    assert source.count(command) == 1
    assert source.index(command) < source.index(marker)
    assert "from app.db.migrations import ensure_db_schema" not in source
    assert "from app.services.auth_service import ensure_admin_user" not in source
    assert "AUTH_ADMIN_BOOTSTRAP" not in source
    assert "RUN_DB_BOOTSTRAP" in source
    assert "SHOULD_BOOTSTRAP_DB" in source


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8", newline="\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX sh is required")
def test_entrypoint_exports_done_only_after_successful_shared_cli(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    trace = tmp_path / "trace"
    launched = tmp_path / "launched"
    fake_python = bin_dir / "python"
    final_command = tmp_path / "final-command"
    _write_executable(fake_python, '#!/bin/sh\nprintf "%s|%s\\n" "${AINOVEL_BOOTSTRAP_DONE:-}" "$*" >> "$TRACE"\n')
    _write_executable(
        final_command,
        '#!/bin/sh\nprintf "%s" "${AINOVEL_BOOTSTRAP_DONE:-}" > "$LAUNCHED"\n',
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
            "TRACE": str(trace),
            "LAUNCHED": str(launched),
            "SECRET_ENCRYPTION_KEY": "already-configured",
            "WAIT_FOR_DB": "0",
            "RUN_DB_BOOTSTRAP": "true",
        }
    )
    completed = subprocess.run(
        ["sh", str(_ENTRYPOINT_FILE), str(final_command)],
        cwd=_BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert trace.read_text(encoding="utf-8").splitlines() == ["|-m app.bootstrap"]
    assert launched.read_text(encoding="utf-8") == "1"


@pytest.mark.parametrize(
    ("case", "run_override", "expected_trace", "expected_command_marker"),
    [
        (
            "default_no_args",
            None,
            [
                "|-m app.bootstrap",
                "1|-m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --no-proxy-headers",
            ],
            None,
        ),
        ("custom_command", None, [], ""),
        (
            "run_false_no_args",
            "false",
            ["|-m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --no-proxy-headers"],
            None,
        ),
    ],
)
@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX sh is required")
def test_entrypoint_preserves_default_command_and_run_false_matrix(
    tmp_path: Path,
    case: str,
    run_override: str | None,
    expected_trace: list[str],
    expected_command_marker: str | None,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    trace = tmp_path / "trace"
    launched = tmp_path / "launched"
    fake_python = bin_dir / "python"
    final_command = tmp_path / "final-command"
    _write_executable(fake_python, '#!/bin/sh\nprintf "%s|%s\\n" "${AINOVEL_BOOTSTRAP_DONE:-}" "$*" >> "$TRACE"\n')
    _write_executable(
        final_command,
        '#!/bin/sh\nprintf "%s" "${AINOVEL_BOOTSTRAP_DONE:-}" > "$LAUNCHED"\n',
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
            "TRACE": str(trace),
            "LAUNCHED": str(launched),
            "SECRET_ENCRYPTION_KEY": "already-configured",
            "WAIT_FOR_DB": "0",
            "DATABASE_URL": "postgresql://unused",
            "HOST": "0.0.0.0",
            "PORT": "8000",
            "WEB_CONCURRENCY": "1",
        }
    )
    env.pop("AINOVEL_BOOTSTRAP_DONE", None)
    if run_override is None:
        env.pop("RUN_DB_BOOTSTRAP", None)
    else:
        env["RUN_DB_BOOTSTRAP"] = run_override

    arguments = ["sh", str(_ENTRYPOINT_FILE)]
    if case == "custom_command":
        arguments.append(str(final_command))
    completed = subprocess.run(
        arguments,
        cwd=_BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    actual_trace = trace.read_text(encoding="utf-8").splitlines() if trace.exists() else []
    assert actual_trace == expected_trace
    if expected_command_marker is None:
        assert not launched.exists()
    else:
        assert launched.read_text(encoding="utf-8") == expected_command_marker


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX sh is required")
def test_entrypoint_does_not_set_marker_or_launch_command_after_cli_failure(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launched = tmp_path / "launched"
    fake_python = bin_dir / "python"
    final_command = tmp_path / "final-command"
    _write_executable(fake_python, "#!/bin/sh\nexit 23\n")
    _write_executable(final_command, f'#!/bin/sh\nprintf launched > "{launched}"\n')
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
            "SECRET_ENCRYPTION_KEY": "already-configured",
            "WAIT_FOR_DB": "0",
            "RUN_DB_BOOTSTRAP": "true",
        }
    )
    completed = subprocess.run(
        ["sh", str(_ENTRYPOINT_FILE), str(final_command)],
        cwd=_BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 23
    assert not launched.exists()
