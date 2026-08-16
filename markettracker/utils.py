import logging
import socket
import time
import uuid
import json
import requests

from celery import current_task
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone as _tz
from django.utils.dateparse import parse_datetime
from esi.errors import TokenInvalidError
from esi.models import Token
from eveuniverse.models import EveRegion
from requests.exceptions import HTTPError, RequestException

from allianceauth.groupmanagement.models import Group
from allianceauth.services.modules.discord.models import DiscordUser

from .models import (
    ContractSnapshot,
    MTTaskLog,
    TrackedLocation,
    TrackedContract,
)

logger = logging.getLogger(__name__)

ESI_BASE_URL = "https://esi.evetech.net/latest"


# --- Global ESI cooldown (shared via Redis cache) ---
# When we hit 420/429 we set a short cooldown to prevent a retry storm.
MT_ESI_COOLDOWN_KEY = "mt-esi-cooldown-until"

def esi_set_cooldown(seconds: int) -> None:
    try:
        seconds = int(seconds or 0)
    except Exception:
        seconds = 0
    if seconds <= 0:
        seconds = 1
    cache.set(MT_ESI_COOLDOWN_KEY, 1, timeout=min(seconds, 600))


def esi_cooldown_active() -> bool:
    return bool(cache.get(MT_ESI_COOLDOWN_KEY, False))


def _chunked(seq, size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def esi_retry_wait_seconds(headers: dict) -> int:
    """Compute wait seconds from ESI headers (Retry-After / X-Esi-Error-Limit-Reset)."""
    try:
        ra = headers.get("Retry-After")
        if ra:
            return max(1, int(float(ra)))
    except Exception:
        pass
    try:
        reset = headers.get("X-Esi-Error-Limit-Reset")
        if reset:
            return max(1, int(float(reset))) + 1
    except Exception:
        pass
    return 10



def _fetch_character_orders(character_id, access_token, config):
    url = f"{ESI_BASE_URL}/characters/{character_id}/orders/"

    data, meta = esi_get_json(
        url,
        access_token=access_token,
        params=None,
        timeout=20,
        source="items",
        event="esi_character_orders_error",
        ctx={"character_id": character_id, "url": url},
        max_attempts=4,
    )
    orders = data or []

    if config.scope == "region":
        return [o for o in orders if o.get("region_id") == config.location_id]
    return [o for o in orders if o.get("location_id") == config.location_id]


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


def db_log(level: str = "INFO", source: str = "contracts", event: str = "run", message: str = "", data: dict | None = None):
    try:
        MTTaskLog.objects.create(level=level, source=source, event=event, message=message or "", data=data or {})
    except Exception:
        logger.exception("Failed to write MTTaskLog")


def esi_headers(access_token: str | None) -> dict:
    h = {"Accept": "application/json"}
    if access_token:
        h["Authorization"] = f"Bearer {access_token}"
    return h



def esi_get_json(
    url: str,
    *,
    access_token: str | None = None,
    params: dict | None = None,
    timeout: int = 15,
    cache_key: str | None = None,
    etag_key: str | None = None,
    max_attempts: int = 3,
    source: str = "esi",
    event: str = "esi_error",
    ctx: dict | None = None,
):
    """
    Safe ESI GET JSON with retries + optional ETag.
    - 304 -> returns cached payload (if present)
    - 304 without cached payload -> retry without If-None-Match
    Returns: (data_or_none, meta)
    meta: status_code, headers, attempts, error
    """
    params = params or {}
    meta = {"status_code": None, "headers": {}, "attempts": 0, "error": None}

    cached_payload = cache.get(cache_key) if cache_key else None
    etag = cache.get(etag_key) if etag_key else None

    headers = esi_headers(access_token)
    if etag:
        headers["If-None-Match"] = etag

    backoff = 1.0
    last_exc = None

    for attempt in range(1, max_attempts + 1):
        meta["attempts"] = attempt
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=timeout)
            meta["status_code"] = resp.status_code
            meta["headers"] = dict(resp.headers or {})

            # --- throttle / temporary errors ---
            if resp.status_code in (420, 429, 503):
                # best-effort wait
                retry_after = resp.headers.get("Retry-After")
                reset = resp.headers.get("X-Esi-Error-Limit-Reset")
                wait_s = None
                if retry_after:
                    try:
                        wait_s = float(retry_after)
                    except ValueError:
                        pass
                if wait_s is None and reset:
                    try:
                        wait_s = float(reset)
                    except ValueError:
                        pass
                if wait_s is None:
                    wait_s = backoff

                if attempt == max_attempts:
                    try:
                        db_log(level="ERROR", source=source, event=event,
                               message=f"{resp.status_code} for {resp.url}",
                               data=_ctx({**(ctx or {}), "attempts": attempt, "wait_s": wait_s}))
                    except Exception:
                        pass
                    return None, meta

                time.sleep(min(wait_s, 30.0))
                backoff = min(backoff * 2.0, 30.0)
                continue

            # --- ETag: 304 means "use cached" ---
            if resp.status_code == 304:
                if cached_payload is not None:
                    return cached_payload, meta

                # 304 but no cache -> retry without ETag once (same attempt)
                headers.pop("If-None-Match", None)
                resp = requests.get(url, headers=headers, params=params, timeout=timeout)
                meta["status_code"] = resp.status_code
                meta["headers"] = dict(resp.headers or {})

            resp.raise_for_status()
            data = resp.json()

            # store payload + ETag (optional)
            if cache_key:
                cache.set(cache_key, data, 3600)
            if etag_key:
                new_etag = resp.headers.get("ETag")
                if new_etag:
                    cache.set(etag_key, new_etag, 3600)

            return data, meta

        except Exception as e:
            last_exc = e
            meta["error"] = str(e)

            if attempt == max_attempts:
                try:
                    db_log(level="ERROR", source=source, event=event,
                           message=str(e),
                           data=_ctx({**(ctx or {}), "attempts": attempt}))
                except Exception:
                    pass
                return None, meta

            time.sleep(min(backoff, 10.0))
            backoff = min(backoff * 2.0, 10.0)

    # shouldn't happen, but keep it safe
    return None, meta


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

        url = f"{ESI_BASE_URL}/universe/names/"
        # public endpoint; POST with json list
        resp = requests.post(
            url,
            params={"datasource": "tranquility"},
            json=[loc_id],
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json() or []
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

            # Import here to avoid circular imports
            from .models import MarketCharacter
            from esi.errors import TokenInvalidError

            mc = (
                MarketCharacter.objects.filter(type="admin")
                .select_related("token")
                .first()
            )
            if mc and mc.token:
                try:
                    access = mc.token.valid_access_token()
                except TokenInvalidError:
                    access = None

                if access:
                    url = f"{ESI_BASE_URL}/universe/structures/{loc_id}/"
                    data, _meta = esi_get_json(
                        url,
                        access_token=access,
                        timeout=15,
                        source="esi",
                        event="esi_structure_name_error",
                        ctx={"structure_id": loc_id},
                        max_attempts=2,
                    )
                    if data and isinstance(data, dict):
                        nm = (data.get("name") or "").strip()
                        if nm:
                            cache.set(cache_key, nm, 86400)  # 24h
                            return nm
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

    url = f"{ESI_BASE_URL}/characters/{char_id}/contracts/{contract_obj.contract_id}/items/"

    for token in tokens:
        try:
            access_token = token.valid_access_token()
        except TokenInvalidError:
            logger.warning(
                "[Contracts] Invalid token for character %s (token id=%s)",
                char_id,
                token.id,
            )
            continue
        except Exception as e:
            logger.exception(
                "[Contracts] Token refresh failed for character %s (token id=%s): %s",
                char_id,
                token.id,
                e,
            )
            continue

        headers = {
            "User-Agent": getattr(settings, "ESI_USER_AGENT", "MarketTracker/1.0"),
            "Authorization": f"Bearer {access_token}",
        }

        try:
            resp = requests.get(
                url,
                headers=headers,
                params={"datasource": "tranquility"},
                timeout=10,
            )

            # 403 = this char/token can't access items for that contract; not retryable
            if resp.status_code == 403:
                logger.info(
                    "[Contracts] Items not accessible for contract %s with char %s (403).",
                    contract_obj.contract_id,
                    char_id,
                )
                return []

            resp.raise_for_status()

            items = resp.json() or []
            contract_obj.items = items
            contract_obj.save(update_fields=["items"])

            db_log(source="contracts", event="items_saved", data={
                "contract_id": contract_obj.contract_id,
                "owner_character_id": char_id,
            })


            return items

        except Exception as e:
            logger.error(
                "[Contracts] Failed to load items for contract %s with char %s (token id=%s): %s",
                contract_obj.contract_id,
                char_id,
                token.id,
                e,
            )
            continue

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
        try:
            mapping = DiscordUser.objects.group_to_role(group=config.discord_ping_group)
            role_id = mapping.get("id") if mapping else None
            if role_id:
                return f"<@&{role_id}>"
        except HTTPError:
            logger.exception("[MarketTracker] Discord service error when resolving group role")

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

        try:
            discord_group_info = DiscordUser.objects.group_to_role(group=group)
        except HTTPError:
            return f"@{group_name}"
        except Exception:
            return f"@{group_name}"

        if discord_group_info and "id" in discord_group_info:
            return f"<@&{discord_group_info['id']}>"
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

