from unittest.mock import MagicMock, patch

import pytest

from markettracker.models import MarketOrderSnapshot, TrackedItem, TrackedStructure
from markettracker.tasks import _fetch_region_orders, _fetch_structure_orders


@pytest.mark.django_db
def test_fetch_region_orders_saves_orders():
    # GIVEN: one tracked item (Tritanium) in DB
    tracked_item = TrackedItem.objects.create(
        item_id=34,  # Tritanium type_id
        desired_quantity=100
    )

    fake_order = {
        "order_id": 12345,
        "type_id": 34,
        "price": 5.5,
        "volume_remain": 1000,
        "is_buy_order": False,
        "issued": "2025-07-30T00:00:00Z",
        "location_id": 60003760
    }

    fake_headers = {"X-Pages": "1"}

    # WHEN: ESI returns one page with one order
    with patch("markettracker.tasks.esi.client.Market.get_markets_region_id_orders") as mock_esi:
        mock_result = MagicMock()
        mock_result.result.return_value = [fake_order]
        mock_result.headers = fake_headers
        mock_esi.return_value = mock_result

        _fetch_region_orders(10000002)  # The Forge region

    # THEN: order snapshot exists in DB
    snapshot = MarketOrderSnapshot.objects.filter(order_id=12345).first()
    assert snapshot is not None
    assert snapshot.price == 5.5
    assert snapshot.volume_remain == 1000
    assert snapshot.tracked_item == tracked_item


@pytest.mark.django_db
def test_fetch_structure_orders_saves_orders():
    # GIVEN: one tracked item linked to structure
    structure = TrackedStructure.objects.create(structure_id=1020001234567, name="Test Structure")
    tracked_item = TrackedItem.objects.create(
        item_id=35,  # Pyerite type_id
        desired_quantity=500,
        structure=structure
    )

    fake_order = {
        "order_id": 54321,
        "type_id": 35,
        "price": 10.0,
        "volume_remain": 200,
        "is_buy_order": False,
        "issued": "2025-07-30T00:00:00Z",
        "location_id": structure.structure_id
    }

    fake_headers = {"X-Pages": "1"}

    # WHEN: ESI returns one page with one order
    with patch("markettracker.tasks.esi.client.Market.get_markets_structures_structure_id") as mock_esi:
        mock_result = MagicMock()
        mock_result.result.return_value = [fake_order]
        mock_result.headers = fake_headers
        mock_esi.return_value = mock_result

        _fetch_structure_orders(structure.structure_id, access_token="dummy_access_token")


    # THEN: order snapshot exists in DB
    snapshot = MarketOrderSnapshot.objects.filter(order_id=54321).first()
    assert snapshot is not None
    assert snapshot.price == 10.0
    assert snapshot.volume_remain == 200
    assert snapshot.tracked_item == tracked_item
