"""Shared stock totals for named groups, scoped to their market location."""

from django.db.models import Sum

from .models import MarketOrderSnapshot, TrackedItem, TrackedItemGroup


def ensure_group_tracking(location_pk=None):
    from .doctrines import sync_doctrine_tracking

    sync_doctrine_tracking(location_pk)
    groups = TrackedItemGroup.objects.prefetch_related("items")
    if location_pk is not None:
        groups = groups.filter(location_id=location_pk)
    pairs = {(group.location_id, item.pk) for group in groups for item in group.items.all()}
    if pairs:
        existing = set(
            TrackedItem.objects.filter(
                location_id__in={location_id for location_id, _ in pairs},
                item_id__in={item_id for _, item_id in pairs},
            ).values_list("location_id", "item_id")
        )
        TrackedItem.objects.bulk_create(
            [
                TrackedItem(location_id=location_id, item_id=item_id)
                for location_id, item_id in pairs - existing
            ],
            ignore_conflicts=True,
        )


def group_stock(group, yellow_threshold=50, red_threshold=25):
    total = (
        MarketOrderSnapshot.objects.filter(
            tracked_item__location_id=group.location_id,
            tracked_item__item_id__in=[item.pk for item in group.items.all()],
            is_buy_order=False,
        ).aggregate(total=Sum("volume_remain"))["total"]
        or 0
    )
    desired = group.effective_quantity
    percentage = int(total / desired * 100) if desired else 100
    status = "OK"
    if desired:
        if percentage <= red_threshold:
            status = "RED"
        elif percentage <= yellow_threshold:
            status = "YELLOW"
    return {
        "group": group,
        "volume_remain": total,
        "desired_quantity": desired,
        "percentage": percentage,
        "status": status,
        "need": max(desired - total, 0),
    }


def update_group_statuses(location_pk=None, yellow_threshold=50, red_threshold=25):
    groups = TrackedItemGroup.objects.prefetch_related("items")
    if location_pk is not None:
        groups = groups.filter(location_id=location_pk)
    changes = []
    for group in groups:
        stock = group_stock(group, yellow_threshold, red_threshold)
        if stock["status"] != group.last_status:
            changes.append(
                (
                    group,
                    group.last_status,
                    stock["status"],
                    stock["percentage"],
                    stock["volume_remain"],
                    group.effective_quantity,
                )
            )
            group.last_status = stock["status"]
            group.save(update_fields=["last_status"])
    return changes
