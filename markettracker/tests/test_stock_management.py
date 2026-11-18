# ruff: noqa: F811
import re
from html import unescape
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from django.urls import reverse
from eveuniverse.models import EveType

from markettracker.models import (
    TrackedContract,
    TrackedContractLocation,
    TrackedItem,
    TrackedLocation,
)
from markettracker.tests.test_doctrines import (  # noqa: F401
    doctrine_data,
    manager,
    rendered_pages,
)


@pytest.fixture(autouse=True)
def local_management_names():
    with patch(
        "markettracker.stock_management.location_display_name",
        side_effect=lambda location: location.name if location else "",
    ):
        yield


def test_invalid_item_target_keeps_values_errors_and_location(
    client, doctrine_data, manager, rendered_pages
):
    data = doctrine_data
    client.force_login(manager)
    item = TrackedItem.objects.get(location=data.location, item_id=data.types[0].pk)
    url = reverse("markettracker:manage_stock") + f"?loc={data.location.pk}&section=items&q=Hull"
    response = client.post(
        url, {"edit": "1", "item_id": item.pk, "item": item.item_id, "desired_quantity": "-2"}
    )
    assert response.status_code == 200
    form = response.context["form"]
    assert form.is_bound
    assert form["desired_quantity"].value() == "-2"
    assert "desired_quantity" in form.errors
    assert response.context["selected_location"].pk == data.location.pk
    assert parse_qs(urlsplit(response.context["return_url"]).query)["q"] == ["Hull"]
    item.refresh_from_db()
    assert item.desired_quantity == 0


def test_duplicate_manual_item_is_a_validation_error(
    client, doctrine_data, manager, rendered_pages
):
    data = doctrine_data
    client.force_login(manager)
    response = client.post(
        reverse("markettracker:manage_stock") + f"?loc={data.location.pk}",
        {"add": "1", "item": data.types[0].pk, "desired_quantity": "12"},
    )
    assert response.status_code == 200
    assert "already tracked" in str(response.context["form"].errors)
    assert TrackedItem.objects.filter(location=data.location, item_id=data.types[0].pk).count() == 1


def test_edit_returns_to_the_same_list_without_trusting_redirect_input(
    client, doctrine_data, manager
):
    data = doctrine_data
    client.force_login(manager)
    item = TrackedItem.objects.get(location=data.location, item_id=data.types[0].pk)
    other = TrackedLocation.objects.create(name="Elsewhere", location_id=987)
    response = client.post(
        reverse("markettracker:manage_stock") + f"?loc={data.location.pk}",
        {
            "edit": "1",
            "item_id": item.pk,
            "item": item.item_id,
            "desired_quantity": "13",
            "return_query": f"loc={other.pk}&section=contracts&q=Hull&page=2&next=https%3A%2F%2Fevil.example",
        },
    )
    assert response.status_code == 302
    url = urlsplit(response.url)
    assert not url.netloc
    assert url.path == reverse("markettracker:manage_stock")
    query = parse_qs(url.query)
    assert query == {
        "loc": [str(data.location.pk)],
        "section": ["items"],
        "q": ["Hull"],
        "page": ["2"],
    }
    item.refresh_from_db()
    assert item.desired_quantity == 13 and item.manually_tracked


def test_contracts_show_and_edit_the_location_target(
    client, doctrine_data, manager, rendered_pages
):
    data = doctrine_data
    client.force_login(manager)
    contract = TrackedContract.objects.create(title_filter="Ready Guardian", desired_quantity=99)
    TrackedContractLocation.objects.create(
        tracked_contract=contract, location=data.location, desired_quantity=7
    )
    url = reverse("markettracker:manage_stock") + f"?loc={data.location.pk}&section=contracts"
    response = client.get(url, {"loc": data.location.pk, "section": "contracts", "cq": "Guardian"})
    assert response.status_code == 200
    text = " ".join(unescape(re.sub(r"<[^>]+>", " ", response.content.decode())).split())
    assert "Ready Guardian Title match 7 No limit Edit Remove" in text
    assert 'name="loc"' in response.content.decode()
    response = client.get(url + f"&tc_edit={contract.pk}")
    assert response.context["tc_form"]["desired_quantity"].value() == 7
    response = client.post(
        url,
        {
            "tc_edit_submit": "1",
            "tc_id": contract.pk,
            "mode": "custom",
            "title_filter": "",
            "max_price": "123",
            "desired_quantity": "41",
        },
    )
    assert response.status_code == 200
    assert response.context["tc_form"].is_bound
    assert response.context["tc_form"]["desired_quantity"].value() == "41"
    assert response.context["tc_form"].non_field_errors()
    assert (
        TrackedContractLocation.objects.get(
            tracked_contract=contract, location=data.location
        ).desired_quantity
        == 7
    )


def test_contract_edits_are_scoped_to_the_selected_market(client, doctrine_data, manager):
    data = doctrine_data
    client.force_login(manager)
    other = TrackedLocation.objects.create(name="Other", location_id=987)
    contract = TrackedContract.objects.create(title_filter="Elsewhere")
    TrackedContractLocation.objects.create(
        tracked_contract=contract, location=other, desired_quantity=9
    )
    url = reverse("markettracker:manage_stock") + f"?loc={data.location.pk}&section=contracts"
    assert client.get(url + f"&tc_edit={contract.pk}").status_code == 404
    assert (
        client.post(
            url,
            {
                "tc_edit_submit": "1",
                "tc_id": contract.pk,
                "mode": "custom",
                "title_filter": "Forged",
                "max_price": "0",
                "desired_quantity": "3",
            },
        ).status_code
        == 404
    )
    contract.refresh_from_db()
    assert contract.title_filter == "Elsewhere"


def test_management_paginates_items_instead_of_nesting_scroll_areas(
    client, doctrine_data, manager, rendered_pages
):
    data = doctrine_data
    client.force_login(manager)
    existing = TrackedItem.objects.get(location=data.location, item_id=data.types[0].pk)
    items = [
        EveType(
            id=90000 + i,
            name=f"Extra item {i:02}",
            eve_group=existing.item.eve_group,
            description="",
            published=True,
            enabled_sections=0,
        )
        for i in range(60)
    ]
    EveType.objects.bulk_create(items)
    TrackedItem.objects.bulk_create(
        [TrackedItem(item=item, location=data.location, desired_quantity=1) for item in items]
    )
    url = reverse("markettracker:manage_stock")
    response = client.get(url, {"loc": data.location.pk, "section": "items"})
    assert response.context["page_obj"].paginator.count == 63
    assert len(response.context["tracked_items"]) == 50
    response = client.get(url, {"loc": data.location.pk, "section": "items", "page": 2})
    assert len(response.context["tracked_items"]) == 13


def test_no_locations_displays_setup_state_without_a_redirect_loop(client, manager, rendered_pages):
    client.force_login(manager)
    TrackedLocation.objects.all().delete()
    response = client.get(reverse("markettracker:manage_stock"))
    assert response.status_code == 200
    assert b"Set up a market location" in response.content
