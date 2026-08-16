import json
import logging
import socket
import uuid
from datetime import timedelta

from celery import current_task
from django.conf import settings
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.utils import timezone as _tz
from django.utils.dateparse import parse_datetime
from esi.errors import TokenInvalidError
from esi.exceptions import HTTPClientError
from esi.models import Token
from eveuniverse.models import EveRegion
from requests.exceptions import HTTPError

from .models import (
    ContractSnapshot,
    MarketCharacter,
    MTTaskLog,
    TrackedContract,
    TrackedLocation,
)
from .providers import (
    get_character_contract_items,
    get_structure_info,
    post_universe_names,
)

logger = logging.getLogger(__name__)


def _aa_discord_role_for_group(group: Group):
    """Resolve an AA Discord role without making the Discord app mandatory."""
    try:
        from allianceauth.services.modules.discord.api import group_to_role

        return group_to_role(group)
    except (ImportError, RuntimeError):
        logger.debug("Alliance Auth's Discord service is not enabled")
    except HTTPError:
        logger.exception("Alliance Auth's Discord service rejected the role lookup")
    return None


def _chunked(seq, size: int):
    for index in range(0, len(seq), size):
        yield seq[index : index + size]


def _task_suffix() -> str:
    try:
        tid = getattr(current_task.request, "id", None) or uuid.uuid4().hex
    except Exception:
        tid = uuid.uuid4().hex
    return tid.replace("-", "")[:10]


def _ctx(extra: dict | None = None) -> dict:
    base = {
        "host": socket.gethostname(),
        "task_id": getattr(getattr(current_task, "request", None), "id", None),
        "ts": _tz.now().isoformat(),
    }
    if extra:
        base.update(extra)
    return base


def _cleanup_expired_task_logs() -> None:
    try:
        retention_days = int(getattr(settings, "MARKETTRACKER_TASK_LOG_RETENTION_DAYS", 14))
    except (TypeError, ValueError):
        retention_days = 14
    retention_days = max(1, retention_days)

    try:
        if cache.add("markettracker:task-log-retention", "running", timeout=86400):
            cutoff = _tz.now() - timedelta(days=retention_days)
            MTTaskLog.objects.filter(created__lt=cutoff).delete()
    except Exception:
        logger.exception("Failed to enforce MTTaskLog retention")
        try:
            cache.delete("markettracker:task-log-retention")
        except Exception:
            logger.exception("Failed to release MTTaskLog retention lock")


def db_log(level: str = "INFO", source: str = "contracts", event: str = "run", message: str = "", data: dict | None = None):
    try:
        MTTaskLog.objects.create(level=level, source=source, event=event, message=message or "", data=data or {})
    except Exception:
        logger.exception("Failed to write MTTaskLog")
        return
    _cleanup_expired_task_logs()


def _parse_esi_datetime(v):
    if not v:
        return None
    try:
        dt = parse_datetime(v)
        return dt
    except Exception:
        return None


def _location_name(config) -> str:
    try:
        if config.scope == "region":
            return EveRegion.objects.get(id=config.location_id).name

        # structure mode
        if TrackedLocation:
            loc = TrackedLocation.objects.filter(scope="structure", location_id=int(config.location_id)).first()
            if loc:
                return loc.name or str(config.location_id)

        return str(config.location_id)

    except Exception:
        return str(getattr(config, "location_id", "")) or ""

    

def location_display_name(loc) -> str:
    """
    Prefer real names:
    - region -> EveRegion.name
    - station -> EveStation.name
    - structure -> EveStructure.name (if present)
    If not present in DB, fallback to ESI /universe/names (public endpoint) + cache.
    Finally fallback to admin label.
    """
    if not loc:
        return ""

    scope = (getattr(loc, "scope", "") or "").lower()
    loc_id = int(getattr(loc, "location_id", 0) or 0)
    if not loc_id:
        return getattr(loc, "name", "") or ""

    # 1) Region via EveUniverse
    if scope == "region":
        try:
            from eveuniverse.models import EveRegion
            r = EveRegion.objects.filter(id=loc_id).only("name").first()
            if r and r.name:
                return r.name
        except Exception:
            pass

    # 2) Station
    try:
        from eveuniverse.models import EveStation
        s = EveStation.objects.filter(id=loc_id).only("name").first()
        if s and s.name:
            return s.name
    except Exception:
        pass

    # 3) Structure (may not exist in DB)
    try:
        from eveuniverse.models import EveStructure
        st = EveStructure.objects.filter(id=loc_id).only("name").first()
        if st and st.name:
            return st.name
    except Exception:
        pass

    # 4) ESI public lookup (cached)
    try:
        cache_key = f"mt:locname:{loc_id}"
        cached = cache.get(cache_key)
        if cached:
            return cached

        data = post_universe_names([loc_id])
        if data and isinstance(data, list):
            name = data[0].get("name")
            if name:
                cache.set(cache_key, name, 86400)  # 24h
                return name
    except Exception:
        pass

    # 5) Private structure name via authenticated ESI (admin token), cached
    try:
        if scope != "region":
            cache_key = f"mt:locname:priv:{loc_id}"
            cached = cache.get(cache_key)
            if cached:
                return cached

            mc = (
                MarketCharacter.objects.filter(type="admin")
                .select_related("token")
                .first()
            )
            if mc and mc.token:
                try:
                    data = get_structure_info(loc_id, mc.token)
                except TokenInvalidError:
                    data = None

                if data and isinstance(data, dict):
                    name = (data.get("name") or "").strip()
                    if name:
                        cache.set(cache_key, name, 86400)  # 24h
                        return name
    except Exception:
        pass


    return getattr(loc, "name", "") or str(loc_id)




def fetch_contract_items(contract_obj, _access_token_unused, char_id):
    """
    Lazy item snapshot.
    Fetches items once per contract if missing.
    403 is normal (token not allowed to view that contract's items).
    """

    existing = contract_obj.items
    if isinstance(existing, str):
        try:
            existing_parsed = json.loads(existing)
            if isinstance(existing_parsed, list) and len(existing_parsed) > 0:
                return existing_parsed
        except Exception:
            pass
    elif isinstance(existing, list) and len(existing) > 0:
        return existing


    if not char_id:
        logger.warning(
            "[Contracts] Cannot fetch items for contract %s: missing owner char_id",
            contract_obj.contract_id,
        )
        return []

    tokens = Token.objects.filter(
        character_id=char_id,
        scopes__name="esi-contracts.read_character_contracts.v1",
    )

    if not tokens.exists():
        logger.warning(
            "[Contracts] No contracts token for character %s (contract %s)",
            char_id,
            contract_obj.contract_id,
        )
        return []

    for token in tokens:
        try:
            items = get_character_contract_items(
                char_id, contract_obj.contract_id, token
            )
        except TokenInvalidError:
            logger.warning(
                "[Contracts] Invalid token for character %s (token id=%s)",
                char_id,
                token.id,
            )
            continue
        except HTTPClientError as exc:
            if exc.status_code == 403:
                logger.info(
                    "[Contracts] Items not accessible for contract %s with char %s (403).",
                    contract_obj.contract_id,
                    char_id,
                )
                return []
            if exc.status_code == 404:
                return []
            logger.warning(
                "[Contracts] ESI rejected contract %s items for char %s: HTTP %s",
                contract_obj.contract_id,
                char_id,
                exc.status_code,
            )
            continue
        except Exception as e:
            logger.exception(
                "[Contracts] Failed to load items for character %s (token id=%s): %s",
                char_id,
                token.id,
                e,
            )
            continue

        contract_obj.items = items
        contract_obj.save(update_fields=["items"])
        db_log(
            source="contracts",
            event="items_saved",
            data={
                "contract_id": contract_obj.contract_id,
                "owner_character_id": char_id,
            },
        )
        return items

    logger.warning(
        "[Contracts] Could not fetch items for contract %s (char %s) with any token",
        contract_obj.contract_id,
        char_id,
    )
    return []
 


def contract_matches(tc: TrackedContract, snap: ContractSnapshot, *, location_id: int | None = None) -> tuple[bool, str]:
    """
    Checks whether a snapshot contract matches the tracked contract.
    Returns: (ok, reason)
    reason is always a short string (for diagnostics).
    """

    if not tc.is_active:
        return False, "inactive"

    # We only track item_exchange outstanding
    if (snap.type or "").lower() != "item_exchange":
        return False, "type_mismatch"

    if (snap.status or "").lower() != "outstanding":
        return False, "status_mismatch"
    
    # Optional location gate (end_location_id is the "delivery" location for item_exchange)
    if location_id is not None:
        try:
            if int(getattr(snap, "end_location_id", 0) or 0) != int(location_id):
                return False, "location_mismatch"
        except Exception:
            return False, "location_mismatch"


    # price gate (applies to both modes if max_price set)
    if tc.max_price and float(tc.max_price) > 0:
        price = float(snap.price or 0)
        if price > float(tc.max_price):
            logger.debug(
                "[match] snap %s price %.2f > max %.2f",
                snap.contract_id, price, float(tc.max_price),
            )
            return False, "price_too_high"

    title = (snap.title or "").strip()

    # ----- CUSTOM -----
    if tc.mode == TrackedContract.Mode.CUSTOM:
        filt = (tc.title_filter or "").strip()
        if not filt:
            return False, "no_title_filter"

        if filt.lower() not in title.lower():
            logger.debug(
                "[match] snap %s title '%s' !contains '%s'",
                snap.contract_id, title, filt,
            )
            return False, "title_mismatch"

        return True, "ok"

    # ----- DOCTRINE -----
    if tc.mode == TrackedContract.Mode.DOCTRINE:
        fit = tc.fitting
        if not fit or not getattr(fit, "ship_type_id", None):
            return False, "no_fitting"

        items = snap.items or []
        if isinstance(items, str):
            try:
                items = json.loads(items)
            except Exception:
                items = []
        if not isinstance(items, list):
            items = []
        if not items:
            logger.debug("[match] snap %s has no items json", snap.contract_id)
            return False, "no_items"

        ship_type_id = int(fit.ship_type_id)

        # count items in contract
        contract_counts: dict[int, int] = {}
        for it in items:
            try:
                t_id = int(it.get("type_id"))
                qty = int(it.get("quantity") or 0)
            except (TypeError, ValueError):
                continue
            contract_counts[t_id] = contract_counts.get(t_id, 0) + qty

        # must include the ship hull
        if contract_counts.get(ship_type_id, 0) < 1:
            logger.debug(
                "[match] snap %s missing ship type_id=%s",
                snap.contract_id, ship_type_id,
            )
            return False, "ship_missing"

        # build required modules list
        required_items: dict[int, int] = {}
        for slot in ("high_slots", "mid_slots", "low_slots", "rigs", "subsystems"):
            for mod in getattr(fit, slot, []) or []:
                try:
                    t_id = int(mod.type_id)
                except (TypeError, ValueError):
                    continue
                required_items[t_id] = required_items.get(t_id, 0) + 1

        # verify required modules exist
        for t_id, req_qty in required_items.items():
            have_qty = contract_counts.get(t_id, 0)
            if have_qty < req_qty:
                logger.debug(
                    "[match] snap %s missing module type_id=%s (have %s, need %s)",
                    snap.contract_id, t_id, have_qty, req_qty,
                )
                return False, "module_missing"

        return True, "ok"

    return False, "mode_unknown"


def resolve_ping_target_from_config(config) -> str:
    """
    Pings for discord messages
    """
    if config.discord_ping_group:
        role = _aa_discord_role_for_group(config.discord_ping_group)
        role_id = role.id if role else None
        if role_id:
            return f"<@&{role_id}>"

        return f"@{config.discord_ping_group.name}"

    v = (config.discord_ping_group_text or "").strip()
    if v in {"here", "@here"}:
        return "@here"
    if v in {"everyone", "@everyone"}:
        return "@everyone"
    return ""

def resolve_ping_target(ping_value: str) -> str:
    if not ping_value:
        return ""
    if ping_value in ("@here", "@everyone"):
        return ping_value

    if ping_value.startswith("@"):
        group_name = ping_value[1:]
        try:
            group = Group.objects.get(name=group_name)
        except Group.DoesNotExist:
            return f"@{group_name}"

        discord_role = _aa_discord_role_for_group(group)

        if discord_role:
            return f"<@&{discord_role.id}>"
        return f"@{group_name}"

    return ""


def get_selected_location(request):
    """
    Returns (TrackedLocation instance or None).
    Selection:
    - ?loc=<pk> (GET) or loc in POST (hidden input)
    - else default
    - else first active
    """

    raw = None

    try:
        raw = (getattr(request, "GET", {}) or {}).get("loc")
    except Exception:
        raw = None

    if not raw:
        try:
            raw = (getattr(request, "POST", {}) or {}).get("loc")
        except Exception:
            raw = None

    if raw:
        try:
            pk = int(raw)
            loc = TrackedLocation.objects.filter(pk=pk, is_active=True).first()
            if loc:
                return loc
        except Exception:
            pass

    loc = TrackedLocation.objects.filter(is_default=True, is_active=True).first()
    if loc:
        return loc

    return TrackedLocation.objects.filter(is_active=True).first()
