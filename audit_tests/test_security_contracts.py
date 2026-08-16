"""Dependency-free security contracts for the reconstructed PyPI source.

The strict xfails document confirmed vulnerabilities without pretending the
current implementation is secure.  When a vulnerability is fixed, pytest will
report XPASS as a failure so the xfail marker can be removed and the contract
kept as a normal regression test.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VIEWS = ROOT / "markettracker" / "views.py"
URLS = ROOT / "markettracker" / "urls.py"
DISCORD = ROOT / "markettracker" / "discord.py"
MODELS = ROOT / "markettracker" / "models.py"

KNOWN_VULNERABILITY = pytest.mark.xfail(
    strict=True,
    reason="Confirmed by the 2026-08-16 source audit; remove xfail after remediation",
)


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _function(path: Path, name: str) -> ast.FunctionDef:
    for node in _tree(path).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"Function {name!r} not found in {path}")


def _decorators(path: Path, name: str) -> list[str]:
    return [ast.unparse(node) for node in _function(path, name).decorator_list]


@pytest.mark.parametrize(
    ("view_name", "permission"),
    [
        pytest.param("list_items_view", "markettracker.basic_access", marks=KNOWN_VULNERABILITY),
        pytest.param("item_search", "markettracker.can_manage_stocks", marks=KNOWN_VULNERABILITY),
        pytest.param("fitting_search", "markettracker.can_manage_stocks", marks=KNOWN_VULNERABILITY),
        pytest.param("character_login_manage", "markettracker.can_manage_stocks", marks=KNOWN_VULNERABILITY),
        pytest.param("manage_stock_view", "markettracker.can_manage_stocks", marks=KNOWN_VULNERABILITY),
        pytest.param("refresh_market_data", "markettracker.can_manage_stocks", marks=KNOWN_VULNERABILITY),
        pytest.param("delete_trackeditem", "markettracker.can_manage_stocks", marks=KNOWN_VULNERABILITY),
        pytest.param("tracked_contract_delete", "markettracker.can_manage_stocks", marks=KNOWN_VULNERABILITY),
        pytest.param("admin_deliveries_view", "markettracker.can_manage_deliveries", marks=KNOWN_VULNERABILITY),
        pytest.param("delete_delivery", "markettracker.can_manage_deliveries", marks=KNOWN_VULNERABILITY),
        pytest.param("finish_delivery", "markettracker.can_manage_deliveries", marks=KNOWN_VULNERABILITY),
        pytest.param("delete_contract_delivery", "markettracker.can_manage_deliveries", marks=KNOWN_VULNERABILITY),
        pytest.param("finish_contract_delivery", "markettracker.can_manage_deliveries", marks=KNOWN_VULNERABILITY),
        ("contracts_list_view", "markettracker.basic_access"),
    ],
)
def test_sensitive_views_require_the_intended_permission(view_name: str, permission: str) -> None:
    decorators = _decorators(VIEWS, view_name)
    assert any("permission_required" in item and permission in item for item in decorators)


@pytest.mark.parametrize(
    "view_name",
    [
        pytest.param("refresh_market_data", marks=KNOWN_VULNERABILITY),
        pytest.param("refresh_contracts_data", marks=KNOWN_VULNERABILITY),
        pytest.param("delete_delivery", marks=KNOWN_VULNERABILITY),
        pytest.param("finish_delivery", marks=KNOWN_VULNERABILITY),
        pytest.param("delete_contract_delivery", marks=KNOWN_VULNERABILITY),
        pytest.param("finish_contract_delivery", marks=KNOWN_VULNERABILITY),
        "delete_trackeditem",
        "tracked_contract_delete",
    ],
)
def test_state_changing_views_reject_get(view_name: str) -> None:
    assert "require_POST" in _decorators(VIEWS, view_name)


@KNOWN_VULNERABILITY
def test_diagnostics_is_enforced_as_superuser_only() -> None:
    node = _function(VIEWS, "diagnostics_view")
    source = ast.unparse(node)
    decorators = [ast.unparse(item) for item in node.decorator_list]
    assert any("user_passes_test" in item and "superuser" in item for item in decorators) or (
        "request.user.is_superuser" in source and "PermissionDenied" in source
    )


@KNOWN_VULNERABILITY
def test_item_detail_requires_basic_access() -> None:
    item_detail = next(
        node
        for node in _tree(VIEWS).body
        if isinstance(node, ast.ClassDef) and node.name == "ItemPriceDetailView"
    )
    bases = {ast.unparse(base) for base in item_detail.bases}
    body = ast.unparse(item_detail)
    assert "LoginRequiredMixin" in bases
    assert "PermissionRequiredMixin" in bases
    assert "markettracker.basic_access" in body


@KNOWN_VULNERABILITY
def test_urls_do_not_dispatch_background_tasks_from_an_inline_lambda() -> None:
    assert not any(isinstance(node, ast.Lambda) for node in ast.walk(_tree(URLS)))


@KNOWN_VULNERABILITY
def test_webhook_failures_do_not_persist_or_log_the_secret_url() -> None:
    source = DISCORD.read_text(encoding="utf-8")
    assert '"url": url' not in source
    assert "Discord send failed for %s\", url" not in source


@KNOWN_VULNERABILITY
def test_discord_webhook_targets_use_an_allowlist() -> None:
    source = ast.get_source_segment(
        DISCORD.read_text(encoding="utf-8"),
        _function(DISCORD, "_iter_webhook_urls"),
    ) or ""
    assert "urlparse" in source
    assert "discord.com" in source


@KNOWN_VULNERABILITY
def test_discord_webhook_string_representation_masks_the_token() -> None:
    model = next(
        node
        for node in _tree(MODELS).body
        if isinstance(node, ast.ClassDef) and node.name == "DiscordWebhook"
    )
    string_method = next(
        node for node in model.body if isinstance(node, ast.FunctionDef) and node.name == "__str__"
    )
    assert ".url" not in ast.unparse(string_method)


def test_runtime_http_requests_set_timeouts() -> None:
    missing: list[str] = []
    for path in (ROOT / "markettracker").rglob("*.py"):
        if "migrations" in path.parts or "tests" in path.parts:
            continue
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if not isinstance(node.func.value, ast.Name) or node.func.value.id != "requests":
                continue
            if node.func.attr not in {"get", "post", "put", "patch", "delete", "request"}:
                continue
            if not any(keyword.arg == "timeout" for keyword in node.keywords):
                missing.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert missing == []


def test_no_csrf_exempt_views() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "markettracker").rglob("*.py")
        if "migrations" not in path.parts
    )
    assert "csrf_exempt" not in source


def test_no_hardcoded_live_webhook_or_private_key() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in ROOT.rglob("*")
        if path.is_file()
        and path.suffix in {".py", ".html", ".js", ".md", ".toml"}
        and ".venv" not in path.parts
        and "dist" not in path.parts
    )
    patterns = [
        r"https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9._-]{20,}",
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    ]
    assert not any(re.search(pattern, source) for pattern in patterns)
