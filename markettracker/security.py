"""Security helpers that do not depend on Django runtime state."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

DISCORD_WEBHOOK_HOSTS = frozenset(
    {
        "discord.com",
        "canary.discord.com",
        "ptb.discord.com",
    }
)
DISCORD_WEBHOOK_PATH = re.compile(
    r"/api/webhooks/(?P<webhook_id>[0-9]+)/(?P<token>[A-Za-z0-9._-]+)"
)


def _parsed_discord_webhook(url: str):
    """Return parsed URL and webhook ID when *url* is an allowed Discord target."""
    try:
        parsed = urlsplit((url or "").strip())
        match = DISCORD_WEBHOOK_PATH.fullmatch(parsed.path)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in DISCORD_WEBHOOK_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.query
            or parsed.fragment
            or match is None
        ):
            return None
    except (TypeError, ValueError):
        return None
    return parsed, match.group("webhook_id")


def is_allowed_discord_webhook_url(url: str) -> bool:
    """Return whether *url* is an HTTPS Discord webhook URL on the allowlist."""
    return _parsed_discord_webhook(url) is not None


def redact_discord_webhook_url(url: str) -> str:
    """Return a log-safe endpoint label without the Discord webhook token."""
    result = _parsed_discord_webhook(url)
    if result is None:
        return "invalid Discord webhook"
    parsed, webhook_id = result
    return f"{parsed.hostname}/api/webhooks/{webhook_id}/[REDACTED]"
