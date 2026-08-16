"""Dependency-free security contracts for the reconstructed PyPI source."""

from __future__ import annotations

import ast
import hashlib
import re
from pathlib import Path

import pytest

from markettracker.security import (
    is_allowed_discord_webhook_url,
    redact_discord_webhook_url,
)

ROOT = Path(__file__).resolve().parents[1]
VIEWS = ROOT / "markettracker" / "views.py"
URLS = ROOT / "markettracker" / "urls.py"
DISCORD = ROOT / "markettracker" / "discord.py"
MODELS = ROOT / "markettracker" / "models.py"
VENDOR = ROOT / "markettracker" / "static" / "markettracker" / "vendor"

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
        ("list_items_view", "markettracker.basic_access"),
        ("character_login_list", "markettracker.basic_access"),
        ("create_delivery", "markettracker.basic_access"),
        ("create_contract_delivery", "markettracker.basic_access"),
        ("deliveries_list_view", "markettracker.basic_access"),
        ("item_search", "markettracker.can_manage_stocks"),
        ("fitting_search", "markettracker.can_manage_stocks"),
        ("character_login_manage", "markettracker.can_manage_stocks"),
        ("manage_stock_view", "markettracker.can_manage_stocks"),
        ("refresh_market_data", "markettracker.can_manage_stocks"),
        ("contract_errors_view", "markettracker.can_manage_stocks"),
        ("delete_trackeditem", "markettracker.can_manage_stocks"),
        ("tracked_contract_delete", "markettracker.can_manage_stocks"),
        ("tracked_contract_edit", "markettracker.can_manage_stocks"),
        ("refresh_contracts_data", "markettracker.can_manage_stocks"),
        ("admin_deliveries_view", "markettracker.can_manage_deliveries"),
        ("delete_delivery", "markettracker.can_manage_deliveries"),
        ("finish_delivery", "markettracker.can_manage_deliveries"),
        ("delete_contract_delivery", "markettracker.can_manage_deliveries"),
        ("finish_contract_delivery", "markettracker.can_manage_deliveries"),
        ("contracts_list_view", "markettracker.basic_access"),
    ],
)
def test_sensitive_views_require_the_intended_permission(view_name: str, permission: str) -> None:
    decorators = _decorators(VIEWS, view_name)
    assert any("permission_required" in item and permission in item for item in decorators)


@pytest.mark.parametrize(
    "view_name",
    [
        "refresh_market_data",
        "refresh_contracts_data",
        "delete_delivery",
        "finish_delivery",
        "delete_contract_delivery",
        "finish_contract_delivery",
        "delete_trackeditem",
        "tracked_contract_delete",
    ],
)
def test_state_changing_views_reject_get(view_name: str) -> None:
    assert "require_POST" in _decorators(VIEWS, view_name)


def test_diagnostics_is_enforced_as_superuser_only() -> None:
    node = _function(VIEWS, "diagnostics_view")
    source = ast.unparse(node)
    decorators = [ast.unparse(item) for item in node.decorator_list]
    assert any("user_passes_test" in item and "superuser" in item for item in decorators) or (
        "request.user.is_superuser" in source and "PermissionDenied" in source
    )


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


@pytest.mark.parametrize(
    "view_name",
    [
        "create_delivery",
        "create_contract_delivery",
        "delete_trackeditem",
        "tracked_contract_delete",
        "tracked_contract_edit",
    ],
)
def test_location_sensitive_objects_are_scoped_to_selected_location(view_name: str) -> None:
    source = ast.unparse(_function(VIEWS, view_name))
    assert "get_selected_location(request)" in source
    assert "location=loc" in source or "by_location__location=loc" in source


def test_urls_do_not_dispatch_background_tasks_from_an_inline_lambda() -> None:
    assert not any(isinstance(node, ast.Lambda) for node in ast.walk(_tree(URLS)))


def test_web_refresh_enqueues_are_rate_limited() -> None:
    helper = ast.unparse(_function(VIEWS, "_enqueue_refresh_once"))
    assert "cache.add" in helper
    assert "timeout=REFRESH_ENQUEUE_TTL" in helper


def test_delivery_actions_are_rendered_as_csrf_protected_forms() -> None:
    template = (
        ROOT / "markettracker" / "templates" / "markettracker" / "admin_deliveries.html"
    ).read_text(encoding="utf-8")
    for action in (
        "delete_delivery",
        "finish_delivery",
        "delete_contract_delivery",
        "finish_contract_delivery",
    ):
        assert f"action=\"{{% url 'markettracker:{action}'" in template
    assert template.count("{% csrf_token %}") >= 4


def test_webhook_failures_do_not_persist_or_log_the_secret_url() -> None:
    source = DISCORD.read_text(encoding="utf-8")
    assert '"url": url' not in source
    assert "Discord send failed for %s\", url" not in source
    assert "resp.text" not in source
    assert "allow_redirects=False" in source


@pytest.mark.parametrize(
    "url",
    [
        "http://discord.com/api/webhooks/123/token-value",
        "https://example.com/api/webhooks/123/token-value",
        "https://discord.com.evil.example/api/webhooks/123/token-value",
        "https://user@discord.com/api/webhooks/123/token-value",
        "https://discord.com:8443/api/webhooks/123/token-value",
        "https://discord.com/api/webhooks/123/token-value?wait=true",
        "https://discord.com/api/other/123/token-value",
    ],
)
def test_discord_webhook_targets_use_an_allowlist(url: str) -> None:
    assert not is_allowed_discord_webhook_url(url)


def test_official_discord_webhook_target_is_allowed_and_redacted() -> None:
    token = "a-secret-token-value"
    url = f"https://discord.com/api/webhooks/123456789/{token}"
    assert is_allowed_discord_webhook_url(url)
    assert token not in redact_discord_webhook_url(url)
    assert "123456789" in redact_discord_webhook_url(url)


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
        and not any(part.startswith(".venv") for part in path.parts)
        and "dist" not in path.parts
    )
    patterns = [
        r"https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9._-]{20,}",
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    ]
    assert not any(re.search(pattern, source) for pattern in patterns)


def test_authenticated_templates_do_not_load_script_or_css_cdns() -> None:
    templates = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "markettracker" / "templates").rglob("*.html")
    )
    for cdn in ("cdn.jsdelivr.net", "unpkg.com", "cdnjs.cloudflare.com"):
        assert cdn not in templates


@pytest.mark.parametrize(
    ("filename", "expected_sha256"),
    [
        (
            "select2-4.0.13.min.css",
            "15d6ad4dfdb43d0affad683e70029f97a8f8fc8637a28845009ee0542dccdf81",
        ),
        (
            "select2-4.0.13.min.js",
            "00501810e93307a8882a74d864e7547fd1458deea539361dc1124ac133799a4b",
        ),
    ],
)
def test_vendored_frontend_assets_match_reviewed_hashes(
    filename: str, expected_sha256: str
) -> None:
    assert hashlib.sha256((VENDOR / filename).read_bytes()).hexdigest() == expected_sha256


def test_operational_task_logs_have_automatic_retention() -> None:
    source = (ROOT / "markettracker" / "utils.py").read_text(encoding="utf-8")
    assert "MARKETTRACKER_TASK_LOG_RETENTION_DAYS" in source
    assert "MTTaskLog.objects.filter(created__lt=cutoff).delete()" in source
    assert "cache.add" in source


def test_upgrade_migration_purges_historical_discord_logs() -> None:
    migration = (
        ROOT
        / "markettracker"
        / "migrations"
        / "0016_purge_discord_webhook_logs.py"
    ).read_text(encoding="utf-8")
    assert 'filter(source="discord").delete()' in migration
