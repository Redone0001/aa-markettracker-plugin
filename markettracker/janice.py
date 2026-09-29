"""Batch Jita sell prices with cached fallback when Janice is unavailable."""
import logging
import math
import time

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)
URL = "https://janice.e-351.com/api/rest/v2/pricer"
FRESH_SECONDS = 900
RETRY_SECONDS = 60
PREFIX = "markettracker:janice:jita:v1:"


def get_jita_prices(type_ids):
    """Return {type_id: {price, stale}}; never discard a last good quote."""
    ids = sorted({int(type_id) for type_id in type_ids})
    keys = {type_id: f"{PREFIX}price:{type_id}" for type_id in ids}
    cached = cache.get_many(keys.values())
    now = time.time()
    quotes = {type_id: cached.get(key) for type_id, key in keys.items()}
    needs_refresh = any(not quote or now - quote["updated"] >= FRESH_SECONDS for quote in quotes.values())
    api_key = getattr(settings, "JANICE_API_KEY", "")
    if ids and api_key and needs_refresh and cache.add(f"{PREFIX}attempt", True, RETRY_SECONDS):
        try:
            response = requests.post(
                URL, params={"market": 2},
                headers={"X-ApiKey": api_key, "Content-Type": "text/plain", "Accept": "application/json"},
                data="\n".join(map(str, ids)), timeout=(3, 5), allow_redirects=False,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError("Expected a list of prices")
            updates = {}
            for row in payload:
                try:
                    type_id = int(row["itemType"]["eid"])
                    price = float(row["immediatePrices"]["sellPrice"])
                    if type_id not in keys or not math.isfinite(price) or price <= 0:
                        continue
                    quote = {"price": price, "updated": now}
                    quotes[type_id] = quote
                    updates[keys[type_id]] = quote
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue
            if updates:
                cache.set_many(updates, timeout=None)
        except (requests.RequestException, ValueError):
            # Do not log request headers, API keys, or response bodies.
            logger.warning("Janice Jita price refresh failed; using cached prices where available.")
    return {
        type_id: {
            "price": quote["price"] if quote else None,
            "stale": bool(quote and now - quote["updated"] >= FRESH_SECONDS),
        }
        for type_id, quote in quotes.items()
    }
