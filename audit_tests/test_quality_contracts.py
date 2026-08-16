"""Source-level quality contracts that do not require AllianceAuth or Django."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import tomllib

ROOT = Path(__file__).resolve().parents[1]
KNOWN_DEFECT = pytest.mark.xfail(
    strict=True,
    reason="Confirmed by the 2026-08-16 source audit; remove xfail after remediation",
)


def _defined_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }


@KNOWN_DEFECT
def test_existing_task_tests_reference_current_symbols() -> None:
    tests = ast.parse(
        (ROOT / "markettracker" / "tests" / "test_tasks.py").read_text(encoding="utf-8")
    )
    model_names = _defined_names(ROOT / "markettracker" / "models.py")
    task_names = _defined_names(ROOT / "markettracker" / "tasks.py")
    missing: list[str] = []
    for node in tests.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        available = model_names if node.module == "markettracker.models" else task_names
        if node.module not in {"markettracker.models", "markettracker.tasks"}:
            continue
        missing.extend(alias.name for alias in node.names if alias.name not in available)
    assert missing == []


@KNOWN_DEFECT
def test_runtime_dependencies_are_declared_in_project_metadata() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = "\n".join(metadata["project"].get("dependencies", [])).lower()
    for required in ("allianceauth", "django", "celery", "eveuniverse", "requests"):
        assert required in dependencies


@KNOWN_DEFECT
def test_readme_markdown_fences_are_balanced() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert readme.count("```") % 2 == 0


@KNOWN_DEFECT
def test_delivery_cleanup_only_deletes_finished_rows() -> None:
    source = (
        ROOT / "markettracker" / "management" / "commands" / "cleanup_deliveries.py"
    ).read_text(encoding="utf-8")
    assert 'status="FINISHED"' in source or "status='FINISHED'" in source


def test_all_python_source_compiles_under_supported_python() -> None:
    for path in (ROOT / "markettracker").rglob("*.py"):
        compile(path.read_text(encoding="utf-8"), str(path), "exec")

