import requests
from django.core.cache import cache

from .utils import (
    esi_cooldown_active,
    esi_retry_wait_seconds,
    esi_set_cooldown,
)

ESI_BASE = "https://esi.evetech.net/latest"
COMMON = {"datasource": "tranquility"}


def _public_get_json(url: str, *, params: dict, timeout: int = 15) -> tuple[list | dict, dict]:
    """Public GET wrapper with cooldown handling. Returns (json, meta)."""
    meta = {"status_code": None, "headers": {}}

    if esi_cooldown_active():
        raise RuntimeError("ESI cooldown active")

    r = requests.get(url, params=params, timeout=timeout)
    meta["status_code"] = r.status_code
    meta["headers"] = dict(r.headers or {})

    if r.status_code in (420, 429, 503):
        wait_s = esi_retry_wait_seconds(meta["headers"])
        esi_set_cooldown(wait_s)
        raise RuntimeError(f"ESI throttled ({r.status_code}), wait {wait_s}s")

    r.raise_for_status()
    return r.json(), meta


def get_market_history(region_id: int, type_id: int):
    """Daily market history for a type in a region."""
    url = f"{ESI_BASE}/markets/{int(region_id)}/history/"
    data, _ = _public_get_json(url, params={**COMMON, "type_id": int(type_id)}, timeout=15)
    return data


def get_type_info(type_id: int, language: str = "en"):
    url = f"{ESI_BASE}/universe/types/{int(type_id)}/"
    data, _ = _public_get_json(url, params={**COMMON, "language": language}, timeout=15)
    return data


def get_type_name(type_id: int, language: str = "en") -> str:
    info = get_type_info(type_id, language=language) or {}
    return (info.get("name") or "").strip()


def get_best_prices(region_id: int, type_id: int, max_pages: int = 10):
    """Best buy/sell in a region for a type, scanning region orders pages.

    Note: region orders can be huge (e.g. Jita), so we:
    - use a short lock to prevent thundering herd
    - allow caller-side caching (views already cache for 60s)
    """
    cache_key = f"mt:best_prices:{region_id}:{type_id}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    region_id = int(region_id)
    type_id = int(type_id)

    lock = f"mt:lock:best_prices:{region_id}:{type_id}"
    if cache.add(lock, "1", timeout=30) is False:
        # another request is already computing this; return "unknown" and let caller use cache/fallback
        return {"sell": None, "buy": None}

    try:
        url = f"{ESI_BASE}/markets/{region_id}/orders/"
        params_base = {**COMMON, "type_id": type_id}

        def _scan(order_type: str) -> float | None:
            best = None
            page = 1
            while True:
                params = {**params_base, "order_type": order_type, "page": page}
                data, meta = _public_get_json(url, params=params, timeout=15)
                if not isinstance(data, list) or not data:
                    break

                if order_type == "sell":
                    for o in data:
                        p = o.get("price")
                        if p is None:
                            continue
                        best = p if best is None else min(best, p)
                else:  # buy
                    for o in data:
                        p = o.get("price")
                        if p is None:
                            continue
                        best = p if best is None else max(best, p)

                xpages = int(meta["headers"].get("X-Pages", "1") or "1")
                if page >= xpages or page >= max_pages:
                    break
                page += 1
            return best
        result = {"sell": _scan("sell"), "buy": _scan("buy")}
        cache.set(cache_key, result, timeout=60)
        return result
    finally:
        cache.delete(lock)