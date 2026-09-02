from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.http import HttpResponse
from django.template.loader import get_template
from django.urls import reverse
from django.utils import timezone
from eveuniverse.models import EveCategory, EveGroup, EveType

from markettracker.models import MarketOrderSnapshot, TrackedItem, TrackedLocation


@pytest.fixture
def status_user(db):
    user = get_user_model().objects.create_user(username="status-filter-user")
    permission = Permission.objects.get(
        content_type__app_label="markettracker",
        codename="basic_access",
    )
    user.user_permissions.add(permission)
    return user


@pytest.fixture
def status_location(db):
    return TrackedLocation.objects.create(
        name="Status filter test",
        scope=TrackedLocation.Scope.STRUCTURE,
        location_id=1_000_000_000_002,
        is_default=True,
        is_active=True,
    )


@pytest.fixture
def tracked_status_items(status_location):
    category = EveCategory.objects.create(id=98, name="Status items", published=True)
    group = EveGroup.objects.create(
        id=98,
        name="Status group",
        eve_category=category,
        published=True,
    )

    tracked_items = {}
    for offset, (expected_status, volume) in enumerate(
        (("RED", 20), ("YELLOW", 40), ("OK", 80)),
        start=1,
    ):
        item = EveType.objects.create(
            id=910_000 + offset,
            name=f"{expected_status} item",
            description="",
            eve_group=group,
            published=True,
            enabled_sections=0,
        )
        tracked = TrackedItem.objects.create(
            location=status_location,
            item=item,
            desired_quantity=100,
        )
        MarketOrderSnapshot.objects.create(
            tracked_item=tracked,
            order_id=920_000 + offset,
            structure_id=status_location.location_id,
            price=1_000_000,
            volume_remain=volume,
            issued=timezone.now(),
        )
        tracked_items[expected_status] = tracked

    return tracked_items


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("selected", "expected"),
    [
        (["red"], {"RED"}),
        (["yellow"], {"YELLOW"}),
        (["red", "yellow"], {"RED", "YELLOW"}),
        ([], {"RED", "YELLOW", "OK"}),
    ],
)
def test_list_items_accepts_multiple_status_filters(
    client,
    status_user,
    status_location,
    tracked_status_items,
    selected,
    expected,
):
    client.force_login(status_user)

    with patch("markettracker.views.location_display_name", return_value=status_location.name):
        with patch("markettracker.views.render", return_value=HttpResponse()) as render_mock:
            response = client.get(
                reverse("markettracker:list_items"),
                {"loc": status_location.pk, "status": selected},
            )

    assert response.status_code == 200
    context = render_mock.call_args.args[2]
    assert context["status_filters"] == tuple(selected)
    assert {entry["status"] for entry in context["items"]} == expected


def test_list_items_renders_red_and_yellow_as_checkboxes():
    source = get_template("markettracker/list_items.html").template.source

    assert source.count('name="status"') == 2
    assert 'value="red"' in source
    assert 'value="yellow"' in source
    assert '"red" in status_filters' in source
    assert '"yellow" in status_filters' in source


@pytest.mark.django_db
def test_list_items_preserves_item_type_exclusion_mode(
    client,
    status_user,
    status_location,
    tracked_status_items,
):
    client.force_login(status_user)

    with patch("markettracker.views.location_display_name", return_value=status_location.name):
        with patch("markettracker.views.render", return_value=HttpResponse()) as render_mock:
            response = client.get(
                reverse("markettracker:list_items"),
                {
                    "loc": status_location.pk,
                    "item_type": ["ship"],
                    "exclude_item_types": "1",
                },
            )

    assert response.status_code == 200
    context = render_mock.call_args.args[2]
    assert context["exclude_item_types"] is True
    assert [option["key"] for option in context["item_filters"] if option["selected"]] == [
        "ship"
    ]
