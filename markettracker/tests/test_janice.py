# ruff: noqa: F811
from unittest.mock import Mock, patch

import pytest
import requests
from django.core.cache import cache
from django.http import HttpResponse
from django.urls import reverse

from markettracker.forms import MarkupFilterForm
from markettracker.janice import PREFIX, get_jita_prices
from markettracker.tests.test_status_filters import (  # noqa: F401
    status_location,
    status_user,
    tracked_status_items,
)


@pytest.fixture(autouse=True)
def janice_settings(settings):
    settings.JANICE_API_KEY = "test-key"
    cache.clear()
    yield
    cache.clear()


def response_row(type_id, price):
    return {"itemType": {"eid": type_id}, "immediatePrices": {"sellPrice": price}}


def test_batch_request_and_cache():
    response = Mock()
    response.json.return_value = [response_row(34, 5), response_row(35, 10)]
    with patch("markettracker.janice.requests.post", return_value=response) as post:
        assert get_jita_prices([35, 34, 35]) == {
            34: {"price": 5, "stale": False}, 35: {"price": 10, "stale": False},
        }
        get_jita_prices([34, 35])
    assert post.call_count == 1
    assert post.call_args.kwargs["data"] == "34\n35"
    assert post.call_args.kwargs["params"] == {"market": 2}
    assert post.call_args.kwargs["headers"]["X-ApiKey"] == "test-key"
    assert post.call_args.kwargs["allow_redirects"] is False


@pytest.mark.parametrize("failure", [requests.Timeout(), requests.HTTPError(), ValueError("Invalid JSON")])
def test_failure_keeps_old_quote_and_throttles_retries(failure):
    cache.set(f"{PREFIX}price:34", {"price": 5, "updated": 0}, timeout=None)
    with patch("markettracker.janice.requests.post", side_effect=failure) as post:
        prices = get_jita_prices([34, 35])
        assert prices == {34: {"price": 5, "stale": True}, 35: {"price": None, "stale": False}}
        assert get_jita_prices([34, 35]) == prices
        assert post.call_count == 1


def test_partial_or_invalid_quotes_preserve_previous_prices():
    cache.set(f"{PREFIX}price:34", {"price": 5, "updated": 0}, timeout=None)
    response = Mock()
    response.json.return_value = [
        response_row(34, 0), response_row(35, 10), response_row(36, float("nan")),
        response_row(37, -1), response_row(38, float("inf")), {}, None,
    ]
    with patch("markettracker.janice.requests.post", return_value=response):
        prices = get_jita_prices([34, 35, 36, 37, 38])
    assert prices[34] == {"price": 5, "stale": True}
    assert prices[35] == {"price": 10, "stale": False}
    assert all(prices[item]["price"] is None for item in [36, 37, 38])


def test_missing_key_and_empty_items_do_not_call_api(settings):
    settings.JANICE_API_KEY = ""
    with patch("markettracker.janice.requests.post") as post:
        assert get_jita_prices([34])[34]["price"] is None
        assert get_jita_prices([]) == {}
        post.assert_not_called()


@pytest.mark.parametrize("data", [
    {"markup_min": "NaN"}, {"markup_max": "Infinity"}, {"markup_min": "bad"},
    {"markup_min": 20, "markup_max": 10},
])
def test_invalid_markup_filters(data):
    assert not MarkupFilterForm(data).is_valid()


@pytest.mark.django_db
@pytest.mark.parametrize("filters, expected", [
    ({}, {"RED", "YELLOW", "OK"}),
    ({"markup_min": "25", "markup_max": "25"}, {"RED"}),
    ({"markup_max": "-10"}, {"YELLOW"}),
    ({"markup_min": "0"}, {"RED"}),
    ({"markup_min": "bad"}, {"RED", "YELLOW", "OK"}),
])
def test_list_markup_filter(client, status_user, status_location, tracked_status_items, filters, expected):
    client.force_login(status_user)
    quotes = {
        tracked_status_items["RED"].item_id: {"price": 800000, "stale": False},
        tracked_status_items["YELLOW"].item_id: {"price": 2000000, "stale": True},
        tracked_status_items["OK"].item_id: {"price": None, "stale": False},
    }
    with (
        patch("markettracker.views.get_jita_prices", return_value=quotes) as fetch,
        patch("markettracker.views.location_display_name", return_value="Test"),
        patch("markettracker.views.render", return_value=HttpResponse()) as render,
    ):
        response = client.get(reverse("markettracker:list_items"), {"loc": status_location.pk, **filters})
        assert response.status_code == 200
        assert set(fetch.call_args.args[0]) == set(quotes)
    rows = render.call_args.args[2]["items"]
    assert {row["item"].name.split()[0] for row in rows} == expected
    for row in rows:
        if row["item"].name == "RED item":
            assert row["markup"] == 25
        if row["item"].name == "YELLOW item":
            assert row["markup"] == -50
            assert row["jita_stale"] is True
