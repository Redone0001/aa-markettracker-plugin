from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.http import HttpResponse
from django.urls import reverse
from eveuniverse.models import EveCategory, EveGroup, EveType

from markettracker.bulk_import import (
    MAX_BULK_IMPORT_LINES,
    BulkImportError,
    import_tracked_items,
    parse_bulk_item_list,
)
from markettracker.forms import BulkTrackedItemForm
from markettracker.models import TrackedItem, TrackedLocation


@pytest.fixture
def eve_group(db):
    category = EveCategory.objects.create(id=97, name="Test items", published=True)
    return EveGroup.objects.create(
        id=97,
        name="Test group",
        eve_category=category,
        published=True,
    )


@pytest.fixture
def eve_types(eve_group):
    return {
        name: EveType.objects.create(
            id=type_id,
            name=name,
            description="",
            eve_group=eve_group,
            published=True,
            enabled_sections=0,
        )
        for type_id, name in (
            (900_001, "Machariel"),
            (900_002, "Barrage L"),
            (900_003, "Navy Cap Booster 800"),
        )
    }


@pytest.fixture
def location(db):
    return TrackedLocation.objects.create(
        name="Bulk import test",
        scope=TrackedLocation.Scope.STRUCTURE,
        location_id=1_000_000_000_001,
        is_default=True,
        is_active=True,
    )


def test_parser_accepts_x_columns_and_missing_quantities():
    entries, errors = parse_bulk_item_list(
        "\n".join(
            (
                "Machariel x1",
                "Barrage L\t1,628",
                "Navy Cap Booster 800    12",
                "Medium Capacitor Booster II    ",
            )
        )
    )

    assert errors == []
    assert [(entry.name, entry.quantity) for entry in entries] == [
        ("Machariel", 1),
        ("Barrage L", 1628),
        ("Navy Cap Booster 800", 12),
        ("Medium Capacitor Booster II", 1),
    ]


@pytest.mark.parametrize("line", ["Machariel x0", "Machariel x-2", "Machariel xmany"])
def test_parser_rejects_invalid_x_quantities(line):
    entries, errors = parse_bulk_item_list(line)

    assert entries == []
    assert errors and errors[0].startswith("Line 1:")


def test_parser_limits_the_number_of_lines():
    text = "\n".join(f"Item {number}" for number in range(MAX_BULK_IMPORT_LINES + 1))

    _, errors = parse_bulk_item_list(text)

    assert errors == [
        f"The list contains more than {MAX_BULK_IMPORT_LINES} non-empty lines."
    ]


def test_parser_rejects_unreasonably_large_quantities_without_crashing():
    entries, errors = parse_bulk_item_list(f"Machariel x{'9' * 5_000}")

    assert entries == []
    assert errors == ["Line 1: quantity is too large."]


def test_bulk_form_defaults_multiplier_to_one():
    form = BulkTrackedItemForm()

    assert form.fields["multiplier"].initial == 1
    assert not form.fields["overwrite_amount"].required


@pytest.mark.django_db
def test_import_adds_new_items_and_preserves_existing_amounts(location, eve_types):
    TrackedItem.objects.create(
        location=location,
        item=eve_types["Machariel"],
        desired_quantity=20,
    )
    entries, errors = parse_bulk_item_list("Machariel x2\nBarrage L x4")
    assert errors == []

    result = import_tracked_items(location, entries, multiplier=3, overwrite_amount=False)

    assert result.added == 1
    assert result.updated == 0
    assert result.skipped == 1
    assert TrackedItem.objects.get(location=location, item=eve_types["Machariel"]).desired_quantity == 20
    assert TrackedItem.objects.get(location=location, item=eve_types["Barrage L"]).desired_quantity == 12


@pytest.mark.django_db
def test_import_overwrites_existing_amount_and_aggregates_duplicates(location, eve_types):
    tracked = TrackedItem.objects.create(
        location=location,
        item=eve_types["Machariel"],
        desired_quantity=20,
    )
    entries, errors = parse_bulk_item_list("Machariel x2\nmachariel x3")
    assert errors == []

    result = import_tracked_items(location, entries, multiplier=2, overwrite_amount=True)

    tracked.refresh_from_db()
    assert tracked.desired_quantity == 10
    assert result.added == 0
    assert result.updated == 1
    assert result.skipped == 0


@pytest.mark.django_db
def test_import_reports_unknown_names_but_adds_known_items(location, eve_types):
    entries, errors = parse_bulk_item_list("barrage l x5\nDefinitely Not An Eve Item x2")
    assert errors == []

    result = import_tracked_items(location, entries)

    assert result.added == 1
    assert result.unknown_names == ("Definitely Not An Eve Item",)
    assert TrackedItem.objects.get(location=location).item == eve_types["Barrage L"]


@pytest.mark.django_db
def test_import_rejects_quantities_larger_than_database_integer(location, eve_types):
    entries, errors = parse_bulk_item_list("Machariel x2,147,483,647")
    assert errors == []

    with pytest.raises(BulkImportError, match="exceeds"):
        import_tracked_items(location, entries, multiplier=2)

    assert not TrackedItem.objects.filter(location=location).exists()


@pytest.mark.django_db
def test_manage_stock_bulk_import_requires_permission(client, location, eve_types):
    user = get_user_model().objects.create_user(username="no-bulk-permission")
    client.force_login(user)

    response = client.post(
        f"{reverse('markettracker:manage_stock')}?loc={location.id}",
        {"bulk_import": "1", "items": "Machariel x1", "multiplier": "1"},
    )

    assert response.status_code == 403
    assert not TrackedItem.objects.filter(location=location).exists()


@pytest.mark.django_db
def test_manage_stock_bulk_import_uses_selected_location(client, location, eve_types):
    user = get_user_model().objects.create_user(username="bulk-manager")
    permission = Permission.objects.get(
        content_type__app_label="markettracker",
        codename="can_manage_stocks",
    )
    user.user_permissions.add(permission)
    client.force_login(user)

    response = client.post(
        f"{reverse('markettracker:manage_stock')}?loc={location.id}",
        {
            "bulk_import": "1",
            "items": "Machariel x2",
            "multiplier": "4",
            "overwrite_amount": "on",
        },
    )

    assert response.status_code == 302
    tracked = TrackedItem.objects.get(location=location, item=eve_types["Machariel"])
    assert tracked.desired_quantity == 8


@pytest.mark.django_db
def test_manage_stock_keeps_invalid_bulk_list_visible(client, location, eve_types):
    user = get_user_model().objects.create_user(username="bulk-manager-invalid")
    permission = Permission.objects.get(
        content_type__app_label="markettracker",
        codename="can_manage_stocks",
    )
    user.user_permissions.add(permission)
    client.force_login(user)

    with patch("markettracker.views.render", return_value=HttpResponse()) as render_mock:
        response = client.post(
            f"{reverse('markettracker:manage_stock')}?loc={location.id}",
            {"bulk_import": "1", "items": "Machariel x0", "multiplier": "1"},
        )

    assert response.status_code == 200
    bulk_form = render_mock.call_args.args[2]["bulk_import_form"]
    assert "quantity must be at least 1" in str(bulk_form.errors)
    assert bulk_form["items"].value() == "Machariel x0"
    assert not TrackedItem.objects.filter(location=location).exists()
