"""Shared stock totals for named groups, scoped to their market location."""
from django.db.models import Sum

from .models import MarketOrderSnapshot, TrackedItem, TrackedItemGroup


def ensure_group_tracking(location_pk=None):
    groups = TrackedItemGroup.objects.prefetch_related("items")
    if location_pk is not None:
        groups = groups.filter(location_id=location_pk)
    for group in groups:
        for item in group.items.all():
            TrackedItem.objects.get_or_create(location_id=group.location_id, item=item)


def group_stock(group, yellow_threshold=50, red_threshold=25):
    total = MarketOrderSnapshot.objects.filter(
        tracked_item__location_id=group.location_id,
        tracked_item__item_id__in=group.items.values_list("pk", flat=True),
        is_buy_order=False,
    ).aggregate(total=Sum("volume_remain"))["total"] or 0
    desired = group.desired_quantity
    percentage = int(total / desired * 100) if desired else 100
    status = "OK"
    if desired:
        if percentage <= red_threshold:
            status = "RED"
        elif percentage <= yellow_threshold:
            status = "YELLOW"
    return {"group": group, "volume_remain": total, "desired_quantity": desired,
            "percentage": percentage, "status": status, "need": max(desired - total, 0)}


def update_group_statuses(location_pk=None, yellow_threshold=50, red_threshold=25):
    groups = TrackedItemGroup.objects.all()
    if location_pk is not None:
        groups = groups.filter(location_id=location_pk)
    changes = []
    for group in groups:
        stock = group_stock(group, yellow_threshold, red_threshold)
        if stock["status"] != group.last_status:
            changes.append((group, group.last_status, stock["status"], stock["percentage"],
                            stock["volume_remain"], group.desired_quantity))
            group.last_status = stock["status"]
            group.save(update_fields=["last_status"])
    return changes
