# ruff: noqa: F811
from copy import deepcopy
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.http import QueryDict
from django.urls import reverse

from markettracker.forms import TrackedItemGroupForm
from markettracker.item_groups import ensure_group_tracking, group_stock, update_group_statuses
from markettracker.models import MarketOrderSnapshot, TrackedItem, TrackedItemGroup, TrackedLocation
from markettracker.tests.test_status_filters import (  # noqa: F401
    status_location,
    tracked_status_items,
)


@pytest.fixture
def item_group(tracked_status_items, status_location):
    group = TrackedItemGroup.objects.create(
        name="Alternative modules", location=status_location, desired_quantity=100
    )
    group.items.set([tracked_status_items["RED"].item, tracked_status_items["YELLOW"].item])
    return group


@pytest.mark.django_db
def test_combined_sell_stock_and_location_isolation(item_group, tracked_status_items):
    # 20 + 40 exceeds yellow threshold, even though neither item does individually.
    other = TrackedLocation.objects.create(name="Elsewhere", scope="region", location_id=99)
    tracked = TrackedItem.objects.create(location=other, item=tracked_status_items["RED"].item)
    original = MarketOrderSnapshot.objects.first()
    for order_id, owner, buy in [(990001, tracked, False), (990002, tracked_status_items["RED"], True)]:
        MarketOrderSnapshot.objects.create(
            tracked_item=owner, order_id=order_id, structure_id=other.location_id,
            volume_remain=10000, price=1, issued=original.issued, is_buy_order=buy,
        )
    stock = group_stock(item_group)
    assert (stock["volume_remain"], stock["need"], stock["status"]) == (60, 40, "OK")
    item_group.desired_quantity = 240
    assert group_stock(item_group)["status"] == "RED"
    item_group.desired_quantity = 120
    assert group_stock(item_group)["status"] == "YELLOW"
    item_group.desired_quantity = 0
    assert group_stock(item_group)["status"] == "OK"


@pytest.mark.django_db
def test_group_status_changes_and_restock(item_group):
    item_group.desired_quantity = 300
    item_group.save()
    changes = update_group_statuses(item_group.location_id)
    assert len(changes) == 1
    assert changes[0][1:3] == ("OK", "RED")
    assert update_group_statuses(item_group.location_id) == []
    item_group.desired_quantity = 50
    item_group.save(update_fields=["desired_quantity"])
    assert update_group_statuses(item_group.location_id)[0][1:3] == ("RED", "OK")


@pytest.mark.django_db
def test_group_members_still_fetched_after_individual_tracking_removed(item_group):
    item = item_group.items.first()
    TrackedItem.objects.filter(location=item_group.location, item=item).delete()
    ensure_group_tracking(item_group.location_id)
    assert TrackedItem.objects.get(location=item_group.location, item=item).desired_quantity == 0


@pytest.mark.django_db
def test_group_form_validation(item_group):
    data = QueryDict(mutable=True)
    data.update(name="New group", desired_quantity=100)
    data.setlist("items", list(item_group.items.values_list("pk", flat=True)))
    form = TrackedItemGroupForm(data, location=item_group.location)
    assert form.is_valid(), form.errors
    data["name"] = item_group.name
    assert not TrackedItemGroupForm(data, location=item_group.location).is_valid()
    data["name"] = "New group"
    data["desired_quantity"] = -1
    assert not TrackedItemGroupForm(data, location=item_group.location).is_valid()
    data["desired_quantity"] = 100
    data.setlist("items", [])
    assert not TrackedItemGroupForm(data, location=item_group.location).is_valid()


@pytest.mark.django_db
def test_group_manage_and_display(client, item_group, settings):
    settings.STORAGES = {"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
                         "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}}
    templates = deepcopy(settings.TEMPLATES)
    templates[0]["APP_DIRS"] = False
    templates[0]["OPTIONS"]["loaders"] = [
        ("django.template.loaders.locmem.Loader", {
            "allianceauth/base-bs5.html": "{% block content %}{% endblock %}{% block extra_javascript %}{% endblock %}",
        }),
        "django.template.loaders.app_directories.Loader",
    ]
    settings.TEMPLATES = templates
    user = get_user_model().objects.create_superuser("group-manager", "manager@example.test", "password")
    client.force_login(user)
    url = reverse("markettracker:manage_stock") + f"?loc={item_group.location_id}"
    with patch("markettracker.views.location_display_name", return_value="Test location"):
        response = client.get(url)
        assert response.status_code == 200
        assert b"Alternative modules" in response.content
        response = client.post(url, {
            "group_save": "", "name": "New group", "desired_quantity": 100,
            "items": list(item_group.items.values_list("pk", flat=True)),
        })
        assert response.status_code == 302
        new_group = TrackedItemGroup.objects.get(name="New group")
        response = client.post(url, {
            "group_save": "", "group_id": new_group.pk, "name": "Renamed", "desired_quantity": 200,
            "items": list(new_group.items.values_list("pk", flat=True)),
        })
        assert response.status_code == 302
        new_group.refresh_from_db()
        assert new_group.name == "Renamed"
        response = client.get(reverse("markettracker:list_items"), {"loc": item_group.location_id, "q": "Alternative"})
        assert response.status_code == 200
        assert b"60 / 100" in response.content
        response = client.post(url, {"group_save": "", "name": "Invalid", "desired_quantity": -1})
        assert response.status_code == 200
        assert response.context["group_form"].errors
        response = client.post(url, {"group_delete": "", "group_id": new_group.pk})
        assert response.status_code == 302
        assert not TrackedItemGroup.objects.filter(pk=new_group.pk).exists()


@pytest.mark.django_db
def test_group_mutation_cannot_cross_locations(client, item_group):
    user = get_user_model().objects.create_superuser("group-admin", "admin@example.test", "password")
    client.force_login(user)
    other = TrackedLocation.objects.create(name="Other", scope="region", location_id=98)
    response = client.post(reverse("markettracker:manage_stock") + f"?loc={other.pk}", {
        "group_delete": "", "group_id": item_group.pk,
    })
    assert response.status_code == 404
    assert TrackedItemGroup.objects.filter(pk=item_group.pk).exists()
