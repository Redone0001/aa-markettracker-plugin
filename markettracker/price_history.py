"""Opt-in, bounded local price/volume history. Never fetches ESI data."""

import logging
from collections import Counter
from datetime import timedelta
from decimal import Decimal
from functools import cached_property

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .doctrines import allocate, available_fits, fit_requirements, location_groups, sell_orders
from .models import PriceHistoryRecording, PriceHistorySample, PriceHistorySettings

logger = logging.getLogger(__name__)
SOURCE_FIELDS = {"fit": "fit_target", "item": "tracked_item", "group": "item_group"}


class HistoryMarket:
    """Reuse one location's market data across all selected records."""

    def __init__(self, location_id):
        self.location_id = location_id

    @cached_property
    def orders(self):
        return sell_orders(self.location_id)

    @cached_property
    def groups(self):
        return location_groups(self.location_id)

    @cached_property
    def totals(self):
        stock, prices = Counter(), {}
        for type_id, quantity, price in self.orders:
            stock[type_id] += quantity
            price = Decimal(str(price))
            prices[type_id] = min(prices.get(type_id, price), price)
        return stock, prices

    def quote(self, kind, source):
        if kind == "fit":
            requirements = fit_requirements(source.fitting, self.groups)
            count = available_fits(requirements, self.orders)
            _, cost, _, missing = allocate(requirements, self.orders)
            return {"price": None if any(missing) else cost, "volume": count}
        ids = {source.item_id} if kind == "item" else {item.pk for item in source.items.all()}
        stock, prices = self.totals
        return {
            "price": min((prices[type_id] for type_id in ids if type_id in prices), default=None),
            "volume": sum(stock[type_id] for type_id in ids),
        }


def visible_recordings(location_id, *, include_private=False):
    qs = PriceHistoryRecording.objects.filter(location_id=location_id)
    if not include_private:
        qs = qs.filter(Q(fit_target__isnull=True) | Q(fit_target__tracking__is_shared=True))
    return qs


def purge_expired_history(*, location_id=None, now=None):
    """Also expires paused records and inactive locations, without deleting opt-ins."""
    now = now or timezone.now()
    settings = PriceHistorySettings.objects.all()
    if location_id is not None:
        settings = settings.filter(location_id=location_id)
    deleted = 0
    for config in settings.iterator():
        count, _ = PriceHistorySample.objects.filter(
            recording__location_id=config.location_id,
            observed_at__lte=now - timedelta(days=config.retention_days),
        ).delete()
        deleted += count
    return deleted


@transaction.atomic
def record_price_history(location_id, *, now=None):
    """Sample only after a successful refresh; a location lock prevents duplicates."""
    now = now or timezone.now()
    config = (
        PriceHistorySettings.objects.select_for_update().filter(location_id=location_id).first()
    )
    if config is None:
        return 0
    purge_expired_history(location_id=location_id, now=now)
    due_before = now - timedelta(hours=config.interval_hours)
    recordings = list(
        PriceHistoryRecording.objects.filter(location_id=location_id, is_enabled=True)
        .filter(Q(last_recorded_at__isnull=True) | Q(last_recorded_at__lte=due_before))
        .select_related(
            "fit_target__tracking",
            "fit_target__fitting__ship_type",
            "tracked_item",
            "item_group",
        )
        .prefetch_related("fit_target__fitting__items__type_fk", "item_group__items")
    )
    if not recordings:
        return 0
    market = HistoryMarket(location_id)
    samples, updated = [], []
    for recording in recordings:
        for kind, field in SOURCE_FIELDS.items():
            if getattr(recording, field + "_id") is not None:
                source = getattr(recording, field)
                source_location = (
                    source.tracking.location_id if kind == "fit" else source.location_id
                )
                # Defensive scoping even if a recording was created outside the page.
                if source_location != location_id:
                    break
                samples.append(
                    PriceHistorySample(
                        recording=recording, observed_at=now, **market.quote(kind, source)
                    )
                )
                recording.last_recorded_at = now
                updated.append(recording)
                break
    PriceHistorySample.objects.bulk_create(samples, batch_size=500)
    PriceHistoryRecording.objects.bulk_update(updated, ["last_recorded_at"], batch_size=500)
    return len(samples)


def record_price_history_safely(location_id):
    """History failures must not interrupt an otherwise successful market refresh."""
    try:
        return record_price_history(location_id)
    except Exception:
        logger.exception("Unable to record price history for market location %s", location_id)
        return 0
