from django.core.cache import cache

from .providers import (
    get_market_history as esi_get_market_history,
)
from .providers import (
    get_region_orders,
)
from .providers import (
    get_type_info as esi_get_type_info,
)


def get_market_history(region_id: int, type_id: int):
    """Daily market history for a type in a region."""
    return esi_get_market_history(int(region_id), int(type_id))


def get_type_info(type_id: int, language: str = "en"):
    del language  # ESI returns localized English fields by default.
    return esi_get_type_info(int(type_id))


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
    _ = max_pages  # retained for backward-compatible callers

    lock = f"mt:lock:best_prices:{region_id}:{type_id}"
    if cache.add(lock, "1", timeout=30) is False:
        # another request is already computing this; return "unknown" and let caller use cache/fallback
        return {"sell": None, "buy": None}

    try:
        def _scan(order_type: str) -> float | None:
            best = None
            for order in get_region_orders(region_id, type_id, order_type=order_type):
                price = order.get("price")
                if price is None:
                    continue
                if order_type == "sell":
                    best = price if best is None else min(best, price)
                else:
                    best = price if best is None else max(best, price)
            return best
        result = {"sell": _scan("sell"), "buy": _scan("buy")}
        cache.set(cache_key, result, timeout=60)
        return result
    finally:
        cache.delete(lock)
