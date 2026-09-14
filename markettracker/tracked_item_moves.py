"""Transactional operations for moving tracked items between locations."""

from dataclasses import dataclass

from django.db import transaction

from .models import MarketOrderSnapshot, TrackedItem, TrackedLocation


@dataclass(frozen=True)
class MoveTrackedItemsResult:
    moved_ids: tuple[int, ...]
    conflict_ids: tuple[int, ...]
    unchanged_ids: tuple[int, ...]
    deleted_snapshots: int

    @property
    def moved_count(self):
        return len(self.moved_ids)

    @property
    def conflict_count(self):
        return len(self.conflict_ids)

    @property
    def unchanged_count(self):
        return len(self.unchanged_ids)


@transaction.atomic
def move_tracked_items(queryset, target_location):
    """Move selected tracking rules and clear snapshots belonging to the old location."""
    target_location = TrackedLocation.objects.select_for_update().get(
        pk=target_location.pk
    )
    if not target_location.is_active:
        raise ValueError("Tracked items can only be moved to an active location.")

    selected_ids = list(queryset.values_list("pk", flat=True))
    selected_items = list(
        TrackedItem.objects.select_for_update()
        .filter(pk__in=selected_ids)
        .order_by("pk")
    )
    selected_item_ids = {tracked.item_id for tracked in selected_items}
    reserved_item_ids = set(
        TrackedItem.objects.select_for_update()
        .filter(location=target_location, item_id__in=selected_item_ids)
        .values_list("item_id", flat=True)
    )

    moved_ids = []
    conflict_ids = []
    unchanged_ids = []

    for tracked in selected_items:
        if tracked.location_id == target_location.pk:
            unchanged_ids.append(tracked.pk)
        elif tracked.item_id in reserved_item_ids:
            conflict_ids.append(tracked.pk)
        else:
            moved_ids.append(tracked.pk)
            reserved_item_ids.add(tracked.item_id)

    deleted_snapshots = 0
    if moved_ids:
        _, deleted_by_model = MarketOrderSnapshot.objects.filter(
            tracked_item_id__in=moved_ids
        ).delete()
        deleted_snapshots = deleted_by_model.get(
            MarketOrderSnapshot._meta.label,
            0,
        )
        TrackedItem.objects.filter(pk__in=moved_ids).update(
            location=target_location,
            last_status="OK",
        )

    return MoveTrackedItemsResult(
        moved_ids=tuple(moved_ids),
        conflict_ids=tuple(conflict_ids),
        unchanged_ids=tuple(unchanged_ids),
        deleted_snapshots=deleted_snapshots,
    )
