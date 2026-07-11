from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

from app.services.prompt_management import app as prompt_app


BACKEND_ROOT = Path(__file__).resolve().parents[1]
ROUTES_DIR = BACKEND_ROOT / "app" / "api" / "routes"
PROMPT_SERVICE_DIR = BACKEND_ROOT / "app" / "services" / "prompt_management"

RETIRED_ROUTE_SUPPORT_MODULES = {
    "memory_route_helpers.py",
    "memory_route_models.py",
    "memory_route_story_helpers.py",
    "memory_route_story_mappers.py",
    "prompt_route_helpers.py",
    "prompt_route_import_export.py",
    "prompt_route_mappers.py",
    "prompt_route_models.py",
    "prompt_route_preview.py",
}


def _imports(path: Path) -> list[ast.ImportFrom]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]


def test_every_route_module_defines_an_api_router() -> None:
    offenders = []
    for path in ROUTES_DIR.glob("*.py"):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        defines_router = any(
            isinstance(node, (ast.Assign, ast.AnnAssign))
            and any(
                isinstance(target, ast.Name) and target.id == "router"
                for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            )
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "APIRouter"
            for node in tree.body
        )
        if not defines_router:
            offenders.append(path.name)
    assert offenders == []


def test_routes_directory_contains_no_retired_support_modules() -> None:
    assert RETIRED_ROUTE_SUPPORT_MODULES.isdisjoint(path.name for path in ROUTES_DIR.glob("*.py"))


def test_prompt_route_depends_only_on_public_application_facade() -> None:
    prompt_imports = [node for node in _imports(ROUTES_DIR / "prompts.py") if node.module and "prompt_management" in node.module]
    assert len(prompt_imports) == 1
    assert prompt_imports[0].module == "app.services.prompt_management.app"
    assert all(alias.name in prompt_app.__all__ for alias in prompt_imports[0].names)


def test_prompt_service_modules_do_not_import_route_modules() -> None:
    offenders = []
    for path in PROMPT_SERVICE_DIR.glob("*.py"):
        for node in _imports(path):
            if node.module and node.module.startswith("app.api.routes"):
                offenders.append((path.name, node.module))
    assert offenders == []


def test_routes_do_not_bypass_prompt_application_facade() -> None:
    offenders = []
    for path in ROUTES_DIR.glob("*.py"):
        for node in _imports(path):
            if node.module and node.module.startswith("app.services.prompt_management."):
                if node.module != "app.services.prompt_management.app":
                    offenders.append((path.name, node.module))
    assert offenders == []


def test_prompt_internal_modules_define_only_package_private_symbols() -> None:
    offenders = []
    for path in PROMPT_SERVICE_DIR.glob("*.py"):
        if path.name in {"app.py", "__init__.py"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not node.name.startswith("_"):
                offenders.append((path.name, node.name))
    assert offenders == []


def test_memory_route_uses_schema_and_public_import_service() -> None:
    modules = {node.module for node in _imports(ROUTES_DIR / "memory.py")}
    assert "app.schemas.story_memory_import" in modules
    assert "app.services.story_memory_import_service" in modules
    assert not any(module and "memory_route_" in module for module in modules)


def test_fresh_process_imports_routes_and_service_facades() -> None:
    modules = (
        "app.api.routes.memory",
        "app.api.routes.prompts",
        "app.schemas.story_memory_import",
        "app.services.story_memory_import_service",
        "app.services.prompt_management.app",
    )
    code = "; ".join(f"import {module}" for module in modules)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
