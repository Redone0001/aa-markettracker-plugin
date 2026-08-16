import logging

import requests
from django.conf import settings
from django.utils import timezone

from .models import DiscordMessage, DiscordWebhook
from .utils import resolve_ping_target, db_log, _chunked

logger = logging.getLogger(__name__)


def _iter_webhook_urls():
    for wh in DiscordWebhook.objects.all():
        url = (wh.url or "").strip()
        if url:
            yield url


def _get_config():
    return DiscordMessage.objects.first()


def _enabled(dm: DiscordMessage | None, field_name: str) -> bool:
    if not dm:
        return True
    return bool(getattr(dm, field_name, True))


def _get_ping_string(dm: DiscordMessage | None, which: str) -> str:
    if not dm:
        return ""

    if which == "items":
        choice = dm.item_ping_choice
        group = dm.item_ping_group
    elif which == "items_restocked":
        choice = dm.item_restocked_ping_choice
        group = dm.item_restocked_ping_group
    elif which == "contracts":
        choice = dm.contract_ping_choice
        group = dm.contract_ping_group
    elif which == "contracts_restocked":
        choice = dm.contract_restocked_ping_choice
        group = dm.contract_restocked_ping_group
    else:
        return ""

    if choice in ("here", "everyone"):
        return f"@{choice}"
    if group:
        return f"@{group.name}"
    return ""


def _post_embeds(embeds: list[dict], ping: str = ""):
    if not embeds:
        return

    payload = {
        "username": "Market Tracker",
        "avatar_url": "https://p7.hiclipart.com/preview/924/786/440/market-stall-marketplace-awning-marketplace.jpg",
        "content": ping or "",
        "embeds": embeds[:10],
    }
    headers = {"User-Agent": getattr(settings, "ESI_USER_AGENT", "MarketTracker/1.0")}

    for url in _iter_webhook_urls():
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=8)
            if resp.status_code >= 400:
                db_log(
                    level="ERROR",
                    source="discord",
                    event="webhook_http_error",
                    message=f"Discord webhook failed: {resp.status_code}",
                    data={
                        "url": url,
                        "status": resp.status_code,
                        "body": (resp.text or "")[:1000],
                        "embeds_count": len(payload["embeds"]),
                    },
                )
                resp.raise_for_status()
        except Exception as e:
            db_log(
                level="ERROR",
                source="discord",
                event="webhook_exception",
                message=str(e),
                data={"url": url},
            )
            logger.exception("[MarketTracker] Discord send failed for %s", url)


def send_items_alert(changed_items, location_name: str):
    dm = _get_config()
    if not _enabled(dm, "item_alert_enabled"):
        return

    filtered = [
        (item, old_s, new_s, percent, total, desired)
        for (item, old_s, new_s, percent, total, desired) in changed_items
        if new_s in ("YELLOW", "RED")
        and not (old_s == "RED" and new_s == "YELLOW")
    ]
    if not filtered:
        return

    header = (
        dm.item_alert_header
        if dm and dm.item_alert_header
        else "⚠️ MarketTracker Items"
    )

    fields = []
    for item, _old_status, new_status, percent, total, desired in filtered:
        name = str(getattr(item.item, "name", "Unknown"))[:256]
        value = f"**{new_status}** ({percent}%) – {total}/{desired}"
        fields.append({
            "name": name,
            "value": value[:1024],
            "inline": False,
        })

    ping = resolve_ping_target(_get_ping_string(dm, "items"))

    embeds = []
    for idx, chunk in enumerate(_chunked(fields, 25), start=1):
        embeds.append({
            "title": location_name + (f" (page {idx})" if len(fields) > 25 else ""),
            "description": header,
            "color": 0xFF0000,
            "fields": chunk,
            "timestamp": timezone.now().isoformat().replace("+00:00", "Z"),
        })

    first = True
    for embeds_chunk in _chunked(embeds, 10):
        _post_embeds(embeds_chunk, ping if first else "")
        first = False


def items_restocked_alert(changed_items, location_name: str):
    dm = _get_config()
    if not _enabled(dm, "item_restocked_alert_enabled"):
        return

    filtered = [
        (item, old_s, new_s, percent, total, desired)
        for (item, old_s, new_s, percent, total, desired) in changed_items
        if new_s == "OK" and old_s in ("YELLOW", "RED")
    ]
    if not filtered:
        return

    header = (
        dm.item_restocked_alert_header
        if dm and dm.item_restocked_alert_header
        else "👍 Items back in stock"
    )

    fields = []
    for item, old_status, _new_status, percent, total, desired in filtered:
        name = str(getattr(item.item, "name", "Unknown"))[:256]
        value = f"✅ **RESTOCKED** ({percent}%) – {total}/{desired} (before: {old_status})"
        fields.append({
            "name": name,
            "value": value[:1024],
            "inline": False,
        })

    ping = resolve_ping_target(_get_ping_string(dm, "items_restocked"))

    embeds = []
    for idx, chunk in enumerate(_chunked(fields, 25), start=1):
        embeds.append({
            "title": location_name + (f" (page {idx})" if len(fields) > 25 else ""),
            "description": header,
            "color": 0x00AA00,
            "fields": chunk,
            "timestamp": timezone.now().isoformat().replace("+00:00", "Z"),
        })

    first = True
    for embeds_chunk in _chunked(embeds, 10):
        _post_embeds(embeds_chunk, ping if first else "")
        first = False


def send_contracts_alert(changed_rows, location_name: str):
    dm = _get_config()
    if not _enabled(dm, "contract_alert_enabled"):
        return

    rows = [r for r in (changed_rows or []) if r.get("status") in ("YELLOW", "RED")]
    if not rows:
        return

    header = (
        dm.contract_alert_header
        if dm and dm.contract_alert_header
        else "📦 MarketTracker Contracts"
    )
    ping = resolve_ping_target(_get_ping_string(dm, "contracts"))

    embeds: list[dict] = []
    now_iso = timezone.now().isoformat().replace("+00:00", "Z")

    for idx, chunk in enumerate(_chunked(rows, 25), start=1):
        title = location_name
        if len(rows) > 25:
            title = f"{title} ({idx}/{(len(rows) + 24) // 25})"

        embed = {
            "title": title,
            "description": header,
            "color": 0xFF0000,
            "fields": [],
            "timestamp": now_iso,
        }

        for r in chunk:
            name = r.get("name") or "—"
            line = f"**{r.get('status')}** ({int(r.get('percent') or 0)}%) – {int(r.get('current') or 0)}/{int(r.get('desired') or 0)}"
            mp = r.get("min_price")
            if mp is not None:
                try:
                    line += f" | min: {float(mp):,.2f} ISK"
                except Exception:
                    pass
            embed["fields"].append({"name": name, "value": line, "inline": False})

        embeds.append(embed)

    first = True
    for emb_chunk in _chunked(embeds, 10):
        _post_embeds(list(emb_chunk), ping if first else "")
        first = False


def contracts_restocked_alert(changed_rows, location_name: str):
    dm = _get_config()
    if not _enabled(dm, "contract_restocked_alert_enabled"):
        return

    rows_ok = [
        r for r in (changed_rows or [])
        if r.get("status") == "OK" and (r.get("old_status") or "OK") != "OK"
    ]
    if not rows_ok:
        return

    header = (
        dm.contract_restocked_alert_header
        if dm and dm.contract_restocked_alert_header
        else "✅ Contracts restocked"
    )
    ping = resolve_ping_target(_get_ping_string(dm, "contracts_restocked"))

    embeds: list[dict] = []
    now_iso = timezone.now().isoformat().replace("+00:00", "Z")

    for idx, chunk in enumerate(_chunked(rows_ok, 25), start=1):
        title = location_name
        if len(rows_ok) > 25:
            title = f"{title} ({idx}/{(len(rows_ok) + 24) // 25})"

        embed = {
            "title": title,
            "description": header,
            "color": 0x008000,
            "fields": [],
            "timestamp": now_iso,
        }

        for r in chunk:
            name = r.get("name") or "—"
            line = f"✅ **RESTOCKED** ({int(r.get('percent') or 0)}%) – {int(r.get('current') or 0)}/{int(r.get('desired') or 0)}"
            old = r.get("old_status")
            if old and old != "OK":
                line += f" (before: {old})"

            mp = r.get("min_price")
            if mp is not None:
                try:
                    line += f" | min: {float(mp):,.2f} ISK"
                except Exception:
                    pass

            embed["fields"].append({"name": name, "value": line, "inline": False})

        embeds.append(embed)

    first = True
    for emb_chunk in _chunked(embeds, 10):
        _post_embeds(list(emb_chunk), ping if first else "")
        first = False