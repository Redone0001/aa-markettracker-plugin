from unittest.mock import patch

import pytest
from django.contrib import admin
from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied
from django.template.loader import get_template
from django.test import RequestFactory
from django.utils import timezone
from eveuniverse.models import EveCategory, EveGroup, EveType

from markettracker.admin import TrackedItemAdmin
from markettracker.models import MarketOrderSnapshot, TrackedItem, TrackedLocation
from markettracker.tracked_item_moves import move_tracked_items


@pytest.fixture
def move_locations(db):
    source = TrackedLocation.objects.create(
        name="Move source",
        scope=TrackedLocation.Scope.STRUCTURE,
        location_id=1_000_000_000_010,
        is_active=True,
    )
    target = TrackedLocation.objects.create(
        name="Move target",
        scope=TrackedLocation.Scope.STRUCTURE,
        location_id=1_000_000_000_011,
        is_active=True,
    )
    return source, target


@pytest.fixture
def move_item_types(db):
    category = EveCategory.objects.create(id=99, name="Move items", published=True)
    group = EveGroup.objects.create(
        id=99,
        name="Move group",
        eve_category=category,
        published=True,
    )
    return [
        EveType.objects.create(
            id=930_000 + offset,
            name=f"Move item {offset}",
            description="",
            eve_group=group,
            published=True,
            enabled_sections=0,
        )
        for offset in range(1, 5)
    ]


@pytest.fixture
def move_user(db):
    user = get_user_model().objects.create_user(
        username="tracked-item-mover",
        is_staff=True,
    )
    permission = Permission.objects.get(
        content_type__app_label="markettracker",
        content_type__model="trackeditem",
        codename="can_move_tracked_items",
    )
    user.user_permissions.add(permission)
    return user


def _create_snapshot(tracked_item, order_id):
    return MarketOrderSnapshot.objects.create(
        tracked_item=tracked_item,
        order_id=order_id,
        structure_id=tracked_item.location.location_id,
        price=1_000_000,
        volume_remain=10,
        issued=timezone.now(),
    )


@pytest.mark.django_db
def test_bulk_move_clears_only_moved_snapshots_and_skips_conflicts(
    move_locations,
    move_item_types,
):
    source, target = move_locations
    moved = TrackedItem.objects.create(
        item=move_item_types[0],
        location=source,
        desired_quantity=20,
        last_status="RED",
    )
    conflict = TrackedItem.objects.create(
        item=move_item_types[1],
        location=source,
        desired_quantity=30,
        last_status="YELLOW",
    )
    TrackedItem.objects.create(
        item=move_item_types[1],
        location=target,
        desired_quantity=40,
    )
    unchanged = TrackedItem.objects.create(
        item=move_item_types[2],
        location=target,
        desired_quantity=50,
    )
    moved_snapshot = _create_snapshot(moved, 940_001)
    conflict_snapshot = _create_snapshot(conflict, 940_002)
    unchanged_snapshot = _create_snapshot(unchanged, 940_003)

    result = move_tracked_items(
        TrackedItem.objects.filter(pk__in=(moved.pk, conflict.pk, unchanged.pk)),
        target,
    )

    moved.refresh_from_db()
    conflict.refresh_from_db()
    unchanged.refresh_from_db()
    assert moved.location == target
    assert moved.last_status == "OK"
    assert conflict.location == source
    assert unchanged.location == target
    assert result.moved_ids == (moved.pk,)
    assert result.conflict_ids == (conflict.pk,)
    assert result.unchanged_ids == (unchanged.pk,)
    assert result.deleted_snapshots == 1
    assert not MarketOrderSnapshot.objects.filter(pk=moved_snapshot.pk).exists()
    assert MarketOrderSnapshot.objects.filter(pk=conflict_snapshot.pk).exists()
    assert MarketOrderSnapshot.objects.filter(pk=unchanged_snapshot.pk).exists()


@pytest.mark.django_db
def test_bulk_move_skips_duplicate_selected_items_from_different_sources(
    move_locations,
    move_item_types,
):
    first_source, target = move_locations
    second_source = TrackedLocation.objects.create(
        name="Second move source",
        scope=TrackedLocation.Scope.STRUCTURE,
        location_id=1_000_000_000_012,
        is_active=True,
    )
    first = TrackedItem.objects.create(
        item=move_item_types[3],
        location=first_source,
    )
    second = TrackedItem.objects.create(
        item=move_item_types[3],
        location=second_source,
    )

    result = move_tracked_items(
        TrackedItem.objects.filter(pk__in=(first.pk, second.pk)),
        target,
    )

    assert result.moved_count == 1
    assert result.conflict_count == 1
    assert TrackedItem.objects.filter(
        item=move_item_types[3],
        location=target,
    ).count() == 1


@pytest.mark.django_db
def test_bulk_move_rejects_an_inactive_destination(
    move_locations,
    move_item_types,
):
    source, target = move_locations
    target.is_active = False
    target.save(update_fields=["is_active"])
    tracked = TrackedItem.objects.create(
        item=move_item_types[0],
        location=source,
    )

    with pytest.raises(ValueError, match="active location"):
        move_tracked_items(TrackedItem.objects.filter(pk=tracked.pk), target)

    tracked.refresh_from_db()
    assert tracked.location == source


@pytest.mark.django_db
def test_tracked_item_admin_requires_dedicated_move_permission(move_user):
    model_admin = admin.site._registry[TrackedItem]
    allowed_request = RequestFactory().get("/admin/")
    allowed_request.user = move_user
    denied_request = RequestFactory().get("/admin/")
    denied_request.user = get_user_model().objects.create_user(
        username="tracked-item-viewer",
        is_staff=True,
    )

    assert isinstance(model_admin, TrackedItemAdmin)
    assert model_admin.has_module_permission(allowed_request)
    assert model_admin.has_view_permission(allowed_request)
    assert model_admin.has_move_tracked_items_permission(allowed_request)
    assert not model_admin.has_add_permission(allowed_request)
    assert not model_admin.has_change_permission(allowed_request)
    assert not model_admin.has_delete_permission(allowed_request)
    assert "move_to_location" in model_admin.get_actions(allowed_request)
    assert not model_admin.has_module_permission(denied_request)
    assert "move_to_location" not in model_admin.get_actions(denied_request)


@pytest.mark.django_db
def test_tracked_item_admin_action_moves_selected_items(
    move_user,
    move_locations,
    move_item_types,
):
    source, target = move_locations
    tracked = TrackedItem.objects.create(
        item=move_item_types[0],
        location=source,
        last_status="RED",
    )
    request = RequestFactory().post(
        "/admin/markettracker/trackeditem/",
        {
            ACTION_CHECKBOX_NAME: [str(tracked.pk)],
            "action": "move_to_location",
            "apply": "1",
            "target_location": str(target.pk),
        },
    )
    request.user = move_user
    model_admin = admin.site._registry[TrackedItem]

    with patch.object(model_admin, "log_change") as log_change:
        with patch.object(model_admin, "message_user") as message_user:
            response = model_admin.move_to_location(
                request,
                TrackedItem.objects.filter(pk=tracked.pk),
            )

    tracked.refresh_from_db()
    assert response is None
    assert tracked.location == target
    assert tracked.last_status == "OK"
    log_change.assert_called_once()
    message_user.assert_called_once()


@pytest.mark.django_db
def test_tracked_item_admin_action_rejects_user_without_permission(
    move_locations,
):
    source, _target = move_locations
    request = RequestFactory().post("/admin/markettracker/trackeditem/")
    request.user = get_user_model().objects.create_user(
        username="unauthorized-mover",
        is_staff=True,
    )
    model_admin = admin.site._registry[TrackedItem]

    with pytest.raises(PermissionDenied):
        model_admin.move_to_location(request, TrackedItem.objects.none())


def test_move_location_admin_template_is_available():
    template = get_template("admin/markettracker/trackeditem/move_location.html")

    assert template.origin.name.endswith("trackeditem/move_location.html")
