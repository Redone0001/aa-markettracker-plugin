from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.messages import get_messages
from django.http import HttpResponse
from django.urls import reverse
from django.utils import timezone
from eve_sde.models import ItemCategory, ItemGroup, ItemType
from eveuniverse.models import EveType
from fittings.models import Doctrine, Fitting, FittingItem

from markettracker.bulk_import import import_tracked_items, parse_bulk_item_list
from markettracker.doctrines import (
    Requirement,
    allocate,
    available_fits,
    doctrine_stock,
    location_groups,
    multibuy,
    sell_orders,
    sync_doctrine_tracking,
    tracking_queryset,
)
from markettracker.item_groups import ensure_group_tracking, group_stock
from markettracker.models import (
    MarketOrderSnapshot,
    TrackedDoctrine,
    TrackedItem,
    TrackedItemGroup,
    TrackedLocation,
)


def req(ids, quantity=1):
    return Requirement(("test", min(ids)), "component", frozenset(ids), quantity, min(ids))


def test_overlapping_alternatives_do_not_double_spend_stock():
    requirements = [req([1, 2]), req([1, 3])]
    assert available_fits(requirements, [(1, 1, 5)]) == 0
    assert available_fits(requirements, [(1, 2, 5)]) == 1
    flow, cost, selected, unmet = allocate(requirements, [(1, 1, 5), (2, 1, 2)])
    assert (flow, cost, selected, unmet) == (2, 7, {1: 1, 2: 1}, [0, 0])


def test_cheapest_orders_respect_depth_and_can_reroute_scarce_alternatives():
    requirements = [req([1, 2]), req([1])]
    flow, cost, selected, unmet = allocate(requirements, [(1, 1, 1), (2, 1, 8), (1, 20, 100)])
    assert (flow, cost, selected, unmet) == (2, Decimal(9), {1: 1, 2: 1}, [0, 0])
    assert allocate([req([1, 2], 3)], [(1, 1, 1), (2, 2, 2), (1, 10, 50)])[1] == 5


def test_large_ammo_quantities_do_not_iterate_per_unit():
    assert available_fits([req([1], 1_000_000)], [(1, 100_000_000, 1)]) == 100
    assert allocate([req([1], 10_000_000)], [(1, 100_000_000, 0)])[0] == 10_000_000


@pytest.fixture
def doctrine_data(db):
    category = ItemCategory.objects.create(id=77, name="Doctrine test", published=True)
    group = ItemGroup.objects.create(
        id=77, name="Doctrine types", category=category, published=True
    )
    types = [
        ItemType.objects.create(id=7000 + i, name=name, group=group, published=True)
        for i, name in enumerate(["Hull", "Module A", "Module B", "Ammo", "Other hull"])
    ]
    fit = Fitting.objects.create(name="Main fit", ship_type=types[0], ship_type_type_id=types[0].pk)
    module = FittingItem.objects.create(
        fit=fit, type_fk=types[1], type_id=types[1].pk, quantity=2, flag="HiSlot0"
    )
    FittingItem.objects.create(
        fit=fit, type_fk=types[3], type_id=types[3].pk, quantity=1000, flag="Cargo"
    )
    doctrine = Doctrine.objects.create(name="Test doctrine", description="")
    doctrine.fittings.add(fit)
    location = TrackedLocation.objects.create(
        name="Test market", scope="structure", location_id=100_000_000_001, is_default=True
    )
    plan = TrackedDoctrine.objects.create(location=location, doctrine=doctrine, default_quantity=10)
    sync_doctrine_tracking(location.pk)
    return SimpleNamespace(
        location=location, doctrine=doctrine, fit=fit, module=module, types=types, plan=plan
    )


@pytest.fixture
def rendered_pages(settings):
    settings.STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    templates = deepcopy(settings.TEMPLATES)
    templates[0]["APP_DIRS"] = False
    templates[0]["OPTIONS"]["loaders"] = [
        (
            "django.template.loaders.locmem.Loader",
            {
                "allianceauth/base-bs5.html": "{% block content %}{% endblock %}{% block extra_javascript %}{% endblock %}"
            },
        ),
        "django.template.loaders.app_directories.Loader",
    ]
    settings.TEMPLATES = templates
    with (
        patch("markettracker.doctrine_views.location_display_name", return_value="Test market"),
        patch("markettracker.views.location_display_name", return_value="Test market"),
    ):
        yield


@pytest.fixture
def market_user(db):
    user = get_user_model().objects.create_user(username="doctrine-user")
    user.user_permissions.add(
        Permission.objects.get(content_type__app_label="markettracker", codename="basic_access")
    )
    return user


@pytest.fixture
def manager(db):
    return get_user_model().objects.create_superuser(
        "doctrine-manager", "manager@example.test", "password"
    )


def add_order(data, type_index, quantity, price=1):
    tracked = TrackedItem.objects.get(location=data.location, item_id=data.types[type_index].pk)
    return MarketOrderSnapshot.objects.create(
        tracked_item=tracked,
        order_id=99000 + MarketOrderSnapshot.objects.count(),
        structure_id=data.location.location_id,
        price=price,
        volume_remain=quantity,
        issued=timezone.now(),
    )


def test_sync_materializes_sde_types_and_preserves_custom_ship_targets(doctrine_data):
    data = doctrine_data
    target = data.plan.fit_targets.get()
    assert data.location.tracked_items.count() == 3
    assert not TrackedItem.objects.filter(location=data.location, manually_tracked=True).exists()
    assert (
        TrackedItem.objects.get(location=data.location, item_id=data.types[3].pk).effective_quantity
        == 10_000
    )
    target.desired_quantity = 3
    target.save()
    data.module.quantity = 4
    data.module.save()
    sync_doctrine_tracking(data.location.pk)
    assert (
        TrackedItem.objects.get(location=data.location, item_id=data.types[1].pk).doctrine_quantity
        == 12
    )
    assert data.plan.fit_targets.get().desired_quantity == 3
    assert TrackedItem.doctrine_fits.through.objects.count() == 3


def test_membership_changes_and_cleanup_preserve_manual_and_group_items(doctrine_data):
    data = doctrine_data
    data.location.remove_unused_doctrine_items = True
    data.location.save()
    # Explicitly promoting ammo to manual preserves it after the doctrine is removed.
    entries, _ = parse_bulk_item_list("Ammo x42")
    import_tracked_items(data.location, entries, overwrite_amount=True)
    group = TrackedItemGroup.objects.create(name="Preserved group", location=data.location)
    group.items.add(EveType.objects.get(pk=data.types[1].pk))
    data.doctrine.fittings.clear()
    sync_doctrine_tracking(data.location.pk)
    assert not TrackedItem.objects.filter(location=data.location, item_id=data.types[0].pk).exists()
    assert (
        TrackedItem.objects.get(location=data.location, item_id=data.types[3].pk).effective_quantity
        == 42
    )
    assert TrackedItem.objects.filter(location=data.location, item_id=data.types[1].pk).exists()
    assert not data.plan.fit_targets.exists()


def test_cleanup_disabled_and_last_source_removed(doctrine_data):
    data = doctrine_data
    standalone = TrackedDoctrine.objects.create(
        location=data.location, fitting=data.fit, default_quantity=5
    )
    data.plan.delete()
    sync_doctrine_tracking(data.location.pk)
    assert (
        TrackedItem.objects.get(location=data.location, item_id=data.types[0].pk).doctrine_quantity
        == 5
    )
    standalone.delete()
    sync_doctrine_tracking(data.location.pk)
    assert data.location.tracked_items.count() == 3
    assert (
        not TrackedItem.objects.filter(location=data.location).exclude(doctrine_quantity=0).exists()
    )
    data.location.remove_unused_doctrine_items = True
    data.location.save()
    sync_doctrine_tracking(data.location.pk)
    assert not data.location.tracked_items.exists()


def test_new_doctrine_fit_inherits_default_and_zero_target_pauses(doctrine_data):
    data = doctrine_data
    new_fit = Fitting.objects.create(
        name="Support", ship_type=data.types[4], ship_type_type_id=data.types[4].pk
    )
    data.doctrine.fittings.add(new_fit)
    ensure_group_tracking(data.location.pk)
    target = data.plan.fit_targets.get(fitting=new_fit)
    assert target.desired_quantity == 10
    target.desired_quantity = 0
    target.save()
    sync_doctrine_tracking(data.location.pk)
    assert (
        TrackedItem.objects.get(location=data.location, item_id=data.types[4].pk).doctrine_quantity
        == 0
    )


def test_group_substitutions_set_combined_targets_without_multiplying_individual_targets(
    doctrine_data,
):
    data = doctrine_data
    from markettracker.sde_items import ensure_local_types

    ensure_local_types([data.types[2].pk])
    group = TrackedItemGroup.objects.create(name="Interchangeable", location=data.location)
    group.items.set(EveType.objects.filter(pk__in=[data.types[1].pk, data.types[2].pk]))
    sync_doctrine_tracking(data.location.pk)
    group.refresh_from_db()
    assert group.doctrine_quantity == 20
    assert (
        TrackedItem.objects.get(location=data.location, item_id=data.types[1].pk).doctrine_quantity
        == 0
    )
    assert (
        TrackedItem.objects.get(
            location=data.location, item_id=data.types[2].pk
        ).doctrine_fits.count()
        == 1
    )
    add_order(data, 0, 10, 100)
    add_order(data, 1, 1, 30)
    add_order(data, 2, 19, 10)
    add_order(data, 3, 10_000, 1)
    assert group_stock(group)["desired_quantity"] == 20
    stock = doctrine_stock(
        tracking_queryset(data.location.pk).get(),
        location_groups(data.location.pk),
        sell_orders(data.location.pk),
    )
    assert stock["coverage"] == 100
    assert stock["ships"][0]["available"] == 10
    assert stock["ships"][0]["price"] == 1120
    quantities, unpriced = multibuy(
        data.plan.fit_targets.get(),
        location_groups(data.location.pk),
        sell_orders(data.location.pk),
    )
    assert quantities[data.types[2].pk] == 2
    assert not unpriced


def test_doctrine_coverage_accounts_for_modules_shared_by_different_ships(doctrine_data):
    data = doctrine_data
    second = Fitting.objects.create(
        name="Second fit", ship_type=data.types[4], ship_type_type_id=data.types[4].pk
    )
    FittingItem.objects.create(
        fit=second, type_fk=data.types[1], type_id=data.types[1].pk, quantity=2
    )
    data.doctrine.fittings.add(second)
    sync_doctrine_tracking(data.location.pk)
    for index, quantity in [(0, 10), (4, 10), (1, 20), (3, 10_000)]:
        add_order(data, index, quantity)
    stock = doctrine_stock(
        tracking_queryset(data.location.pk).get(), {}, sell_orders(data.location.pk)
    )
    assert stock["coverage"] == 50
    assert [ship["available"] for ship in stock["ships"]] == [10, 10]


def test_buy_missing_and_location_isolation(doctrine_data):
    data = doctrine_data
    add_order(data, 0, 9)
    add_order(data, 1, 10)
    add_order(data, 3, 12_000)
    other = TrackedLocation.objects.create(name="Elsewhere", location_id=99)
    tracked = TrackedItem.objects.create(location=other, item_id=data.types[1].pk)
    MarketOrderSnapshot.objects.create(
        tracked_item=tracked,
        order_id=123456,
        structure_id=99,
        price=0.1,
        volume_remain=99_999,
        issued=timezone.now(),
    )
    quantities, _ = multibuy(
        data.plan.fit_targets.get(), {}, sell_orders(data.location.pk), missing=True
    )
    assert quantities == {data.types[0].pk: 1, data.types[1].pk: 10}
    add_order(data, 0, 1)
    add_order(data, 1, 10)
    assert not multibuy(
        data.plan.fit_targets.get(), {}, sell_orders(data.location.pk), missing=True
    )[0]


def test_private_doctrine_routes_and_multibuy_are_protected(
    client, doctrine_data, market_user, rendered_pages
):
    data = doctrine_data
    client.force_login(market_user)
    target = data.plan.fit_targets.get()
    list_url = reverse("markettracker:doctrine_list") + f"?loc={data.location.pk}"
    page = client.get(list_url)
    assert data.doctrine.name not in page.content.decode()
    buy_url = (
        reverse("markettracker:doctrine_multibuy", args=[target.pk]) + f"?loc={data.location.pk}"
    )
    assert client.post(buy_url, {"mode": "all"}).status_code == 404
    assert client.get(reverse("markettracker:manage_doctrines")).status_code == 403
    data.plan.is_shared = True
    data.plan.save()
    page = client.get(list_url)
    assert data.doctrine.name in page.content.decode()
    assert '<details class="card mb-3">' in page.content.decode()
    result = client.post(buy_url, {"mode": "all", "amount": 2})
    assert result.status_code == 200
    assert "Hull\t2" in result.json()["text"]
    assert "Module A\t4" in result.json()["text"]
    assert result.json()["unpriced"]
    assert client.get(buy_url).status_code == 405
    assert client.post(buy_url, {"amount": 0}).status_code == 400
    other = TrackedLocation.objects.create(name="Elsewhere", location_id=123)
    assert (
        client.post(
            reverse("markettracker:doctrine_multibuy", args=[target.pk]) + f"?loc={other.pk}",
            {"mode": "all"},
        ).status_code
        == 404
    )


def test_manager_can_create_edit_targets_and_remove_tracking(
    client, doctrine_data, manager, rendered_pages
):
    data = doctrine_data
    client.force_login(manager)
    url = reverse("markettracker:manage_doctrines") + f"?loc={data.location.pk}"
    assert client.get(url + f"&edit={data.plan.pk}").status_code == 200
    response = client.post(url, {"save": "1", "fitting": data.fit.pk, "default_quantity": 2})
    assert response.status_code == 302
    plan = TrackedDoctrine.objects.get(fitting=data.fit)
    target = plan.fit_targets.get()
    form_data = {
        "plan_id": plan.pk,
        "save_targets": "1",
        "form-TOTAL_FORMS": "1",
        "form-INITIAL_FORMS": "1",
        "form-0-id": target.pk,
        "form-0-desired_quantity": "7",
    }
    assert client.post(url, form_data).status_code == 302
    target.refresh_from_db()
    assert target.desired_quantity == 7
    # Tampering with another plan's target is rejected.
    form_data["form-0-id"] = data.plan.fit_targets.get().pk
    assert client.post(url, form_data).status_code == 400
    assert client.post(url, {"delete": "1", "plan_id": plan.pk}).status_code == 302
    assert not TrackedDoctrine.objects.filter(pk=plan.pk).exists()


def test_doctrine_item_filters_and_price_sorting(client, doctrine_data, manager):
    data = doctrine_data
    client.force_login(manager)
    manual = (
        TrackedItem.objects.create(location=data.location, item_id=data.types[4].pk)
        if EveType.objects.filter(pk=data.types[4].pk).exists()
        else None
    )
    if manual is None:
        from markettracker.sde_items import ensure_local_types

        ensure_local_types([data.types[4].pk])
        manual = TrackedItem.objects.create(location=data.location, item_id=data.types[4].pk)
    add_order(data, 0, 1, 100)
    add_order(data, 1, 1, 200)
    for direction, expected in [("asc", [0, 1, 3]), ("desc", [1, 0, 3])]:
        with (
            patch("markettracker.views.location_display_name", return_value="Market"),
            patch("markettracker.views.render", return_value=HttpResponse()) as renderer,
        ):
            client.get(
                reverse("markettracker:list_items"),
                {
                    "loc": data.location.pk,
                    "doctrine_only": "yes",
                    "sort": "price",
                    "direction": direction,
                },
            )
        rows = renderer.call_args.args[2]["items"]
        assert [row["item"].pk for row in rows] == [data.types[i].pk for i in expected]
    with (
        patch("markettracker.views.location_display_name", return_value="Market"),
        patch("markettracker.views.render", return_value=HttpResponse()) as renderer,
    ):
        client.get(
            reverse("markettracker:list_items"), {"loc": data.location.pk, "doctrine_only": "no"}
        )
    assert [row["item"].pk for row in renderer.call_args.args[2]["items"]] == [manual.item_id]


def test_sync_failure_rolls_back_prior_tracking(doctrine_data):
    data = doctrine_data
    data.module.type_id = 999_999_999
    data.module.save()
    with pytest.raises(ValueError, match="Missing local SDE types"):
        sync_doctrine_tracking(data.location.pk)
    assert data.location.tracked_items.count() == 3
    assert TrackedItem.doctrine_fits.through.objects.count() == 3


def test_failed_new_tracking_remains_a_retryable_form(
    client, doctrine_data, manager, rendered_pages
):
    data = doctrine_data
    client.force_login(manager)
    url = reverse("markettracker:manage_doctrines") + f"?loc={data.location.pk}"
    with patch(
        "markettracker.doctrine_views.sync_doctrine_tracking",
        side_effect=ValueError("Missing local SDE types"),
    ):
        response = client.post(url, {"save": "1", "fitting": data.fit.pk, "default_quantity": 2})
    assert response.status_code == 200
    assert any(
        "Missing local SDE types" in str(message) for message in get_messages(response.wsgi_request)
    )
    assert not TrackedDoctrine.objects.filter(fitting=data.fit).exists()
    assert b'name="plan_id"' not in response.content.split(b"Tracked doctrines and fits")[0]
    assert (
        client.post(url, {"save": "1", "fitting": data.fit.pk, "default_quantity": 2}).status_code
        == 302
    )


def test_moving_a_rule_does_not_transfer_location_scoped_doctrine_targets(doctrine_data):
    from markettracker.tracked_item_moves import move_tracked_items

    data = doctrine_data
    destination = TrackedLocation.objects.create(name="Other market", location_id=987)
    row = TrackedItem.objects.get(location=data.location, item_id=data.types[0].pk)
    move_tracked_items(TrackedItem.objects.filter(pk=row.pk), destination)
    row.refresh_from_db()
    assert row.location == destination
    assert row.doctrine_quantity == 0
    assert not row.doctrine_fits.exists()
    assert row.manually_tracked
    sync_doctrine_tracking(data.location.pk)
    assert (
        TrackedItem.objects.get(location=data.location, item_id=data.types[0].pk).doctrine_quantity
        == 10
    )


def test_allocation_matches_exhaustive_small_market_scenarios():
    from collections import Counter
    from itertools import product
    from random import Random

    random = Random(712)
    for _ in range(20):
        inventory = {
            type_id: (random.randint(0, 2), random.randint(1, 5)) for type_id in range(1, 4)
        }
        requirements = [
            req(random.sample([1, 2, 3], random.randint(1, 3)), random.randint(1, 2))
            for _ in range(3)
        ]
        slots = [
            tuple(requirement.type_ids) + (None,)
            for requirement in requirements
            for _ in range(requirement.quantity)
        ]
        best = (0, 0)
        for choice in product(*slots):
            used = Counter(type_id for type_id in choice if type_id is not None)
            if any(quantity > inventory[type_id][0] for type_id, quantity in used.items()):
                continue
            score = (
                sum(used.values()),
                -sum(inventory[type_id][1] * quantity for type_id, quantity in used.items()),
            )
            best = max(best, score)
        orders = [(type_id, quantity, price) for type_id, (quantity, price) in inventory.items()]
        flow, cost, _, _ = allocate(requirements, orders)
        assert (flow, -cost) == best
