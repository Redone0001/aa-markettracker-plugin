# ruff: noqa: F811
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from eveuniverse.models import EveType

from markettracker.models import (
    PriceHistoryRecording,
    PriceHistorySample,
    PriceHistorySettings,
    TrackedItem,
    TrackedItemGroup,
    TrackedLocation,
)
from markettracker.price_history import (
    HistoryMarket,
    purge_expired_history,
    record_price_history,
    record_price_history_safely,
)
from markettracker.sde_items import ensure_local_types
from markettracker.tests.test_doctrines import (  # noqa: F401
    add_order,
    doctrine_data,
    manager,
    market_user,
    rendered_pages,
)


@pytest.fixture
def history_data(doctrine_data):
    data = doctrine_data
    data.plan.is_shared = True
    data.plan.save(update_fields=["is_shared"])
    add_order(data, 0, 10, Decimal("100.90"))
    add_order(data, 1, 20, Decimal("20.75"))
    add_order(data, 3, 10_000, Decimal("0.12"))
    return data


@pytest.fixture
def history_user(market_user):
    market_user.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="markettracker", codename="can_manage_price_history"
        )
    )
    return market_user


@pytest.fixture
def history_viewer(market_user):
    market_user.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="markettracker", codename="can_view_price_history"
        )
    )
    return market_user


def enable(data, **source):
    PriceHistorySettings.objects.get_or_create(location=data.location)
    return PriceHistoryRecording.objects.create(location=data.location, is_enabled=True, **source)


def history_url(data, section="doctrines", **query):
    from urllib.parse import urlencode

    return (
        reverse("markettracker:price_history")
        + "?"
        + urlencode(
            {
                "loc": data.location.pk,
                "section": section,
                **query,
            }
        )
    )


def test_history_is_unused_until_explicitly_enabled(history_data):
    assert record_price_history(history_data.location.pk) == 0
    assert not PriceHistorySettings.objects.exists()
    assert not PriceHistoryRecording.objects.exists()
    assert not PriceHistorySample.objects.exists()


def test_fit_recording_stores_fit_cost_and_complete_fit_volume_only(history_data):
    data = history_data
    recording = enable(data, fit_target=data.plan.fit_targets.get())
    assert record_price_history(data.location.pk) == 1
    sample = recording.samples.get()
    assert sample.price == Decimal("262.40")
    assert sample.volume == 10
    assert PriceHistoryRecording.objects.count() == 1
    assert not PriceHistoryRecording.objects.filter(tracked_item__isnull=False).exists()
    assert record_price_history(data.location.pk) == 0


def test_item_samples_use_total_sell_volume_and_cheapest_positive_price(history_data):
    data = history_data
    item = data.location.tracked_items.get(item_id=data.types[1].pk)
    recording = enable(data, tracked_item=item)
    add_order(data, 1, 4, Decimal("9.99"))
    buy = add_order(data, 1, 500, Decimal("0.01"))
    buy.is_buy_order = True
    buy.save()
    add_order(data, 1, 100, 0)
    add_order(data, 1, 0, Decimal("0.50"))
    assert record_price_history(data.location.pk) == 1
    assert recording.samples.get().price == Decimal("9.99")
    assert recording.samples.get().volume == 24


def test_group_samples_do_not_enable_member_recordings(history_data):
    data = history_data
    ensure_local_types([data.types[2].pk])
    TrackedItem.objects.create(location=data.location, item_id=data.types[2].pk)
    group = TrackedItemGroup.objects.create(location=data.location, name="Shield alternatives")
    group.items.set(EveType.objects.filter(pk__in=[data.types[1].pk, data.types[2].pk]))
    add_order(data, 2, 5, Decimal("8.99"))
    recording = enable(data, item_group=group)
    assert record_price_history(data.location.pk) == 1
    assert recording.samples.get().price == Decimal("8.99")
    assert recording.samples.get().volume == 25
    assert PriceHistoryRecording.objects.count() == 1


def test_missing_component_records_null_fit_price_and_zero_volume(history_data):
    data = history_data
    recording = enable(data, fit_target=data.plan.fit_targets.get())
    data.location.tracked_items.get(item_id=data.types[1].pk).order_snapshots.all().delete()
    assert record_price_history(data.location.pk) == 1
    assert recording.samples.get().price is None
    assert recording.samples.get().volume == 0


def test_history_interval_pause_and_resume_preserve_rate_limit(history_data):
    data = history_data
    recording = enable(data, fit_target=data.plan.fit_targets.get())
    now = timezone.now()
    assert record_price_history(data.location.pk, now=now) == 1
    assert record_price_history(data.location.pk, now=now + timedelta(minutes=59)) == 0
    assert record_price_history(data.location.pk, now=now + timedelta(hours=1)) == 1
    recording.is_enabled = False
    recording.save(update_fields=["is_enabled"])
    assert record_price_history(data.location.pk, now=now + timedelta(hours=2)) == 0
    assert recording.samples.count() == 2
    recording.is_enabled = True
    recording.save(update_fields=["is_enabled"])
    config = PriceHistorySettings.objects.get(location=data.location)
    config.interval_hours = 6
    config.save()
    assert record_price_history(data.location.pk, now=now + timedelta(hours=2)) == 0
    assert record_price_history(data.location.pk, now=now + timedelta(hours=7)) == 1


def test_retention_also_expires_paused_and_inactive_locations(history_data):
    data = history_data
    recording = enable(data, fit_target=data.plan.fit_targets.get())
    now = timezone.now()
    PriceHistorySample.objects.create(
        recording=recording, observed_at=now - timedelta(days=91), price=1, volume=10
    )
    retained = PriceHistorySample.objects.create(
        recording=recording, observed_at=now - timedelta(days=89), price=2, volume=9
    )
    recording.is_enabled = False
    recording.save()
    data.location.is_active = False
    data.location.save()
    assert purge_expired_history(now=now) == 1
    assert list(recording.samples.all()) == [retained]
    assert PriceHistoryRecording.objects.filter(pk=recording.pk).exists()


def test_recording_source_is_validated_and_location_is_defended(history_data):
    data = history_data
    other = TrackedLocation.objects.create(name="Other market", location_id=99)
    item = data.location.tracked_items.first()
    invalid = PriceHistoryRecording(location=other, tracked_item=item, is_enabled=True)
    with pytest.raises(ValidationError, match="selected market location"):
        invalid.full_clean()
    PriceHistorySettings.objects.create(location=other)
    invalid.save()
    assert record_price_history(other.pk) == 0
    assert not PriceHistorySample.objects.exists()
    with transaction.atomic(), pytest.raises(IntegrityError):
        PriceHistoryRecording.objects.create(location=data.location)


def test_page_requires_new_permission_even_for_stock_managers(
    client, history_data, market_user, rendered_pages, settings
):
    settings.LOGIN_URL = "/login/"
    url = history_url(history_data)
    assert client.get(url).status_code == 302
    client.force_login(market_user)
    assert client.get(url).status_code == 403
    assert (
        client.post(
            url, {"action": "settings", "interval_hours": 1, "retention_days": 90}
        ).status_code
        == 403
    )
    market_user.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="markettracker", codename="can_manage_stocks"
        )
    )
    assert client.get(url).status_code == 403


def test_authorized_get_is_read_only_and_all_sections_render(
    client, history_data, history_user, rendered_pages
):
    client.force_login(history_user)
    for section in ("doctrines", "items", "groups"):
        response = client.get(history_url(history_data, section))
        assert response.status_code == 200
        assert b"Price history" in response.content
    assert not PriceHistorySettings.objects.exists()
    assert not PriceHistoryRecording.objects.exists()
    assert not PriceHistorySample.objects.exists()


def test_toggle_is_location_scoped_and_never_inherits_to_components(
    client, history_data, history_user, rendered_pages
):
    data = history_data
    client.force_login(history_user)
    target = data.plan.fit_targets.get()
    url = history_url(data)
    response = client.post(url, {"action": "toggle", "source_id": target.pk, "enabled": "1"})
    assert response.status_code == 302
    recording = PriceHistoryRecording.objects.get()
    assert recording.fit_target == target
    assert recording.is_enabled
    assert recording.last_recorded_at is None
    assert not recording.samples.exists()
    other = TrackedLocation.objects.create(name="Other market", location_id=99)
    foreign_url = reverse("markettracker:price_history") + f"?loc={other.pk}"
    assert (
        client.post(
            foreign_url, {"action": "toggle", "source_id": target.pk, "enabled": "1"}
        ).status_code
        == 404
    )
    assert client.post(url, {"action": "toggle", "source_id": target.pk}).status_code == 302
    recording.refresh_from_db()
    assert not recording.is_enabled


def test_history_role_does_not_expose_private_doctrines(
    client, history_data, history_user, rendered_pages
):
    data = history_data
    target = data.plan.fit_targets.get()
    recording = enable(data, fit_target=target)
    record_price_history(data.location.pk)
    data.plan.is_shared = False
    data.plan.save()
    client.force_login(history_user)
    assert data.fit.name.encode() not in client.get(history_url(data)).content
    assert client.get(history_url(data, record=target.pk)).status_code == 404
    assert (
        client.post(
            history_url(data), {"action": "toggle", "source_id": target.pk, "enabled": "1"}
        ).status_code
        == 404
    )
    assert recording.samples.count() == 1


def test_history_chart_and_sample_table_use_real_recorded_data(
    client, history_data, history_user, rendered_pages
):
    data = history_data
    enable(data, fit_target=data.plan.fit_targets.get())
    record_price_history(data.location.pk)
    client.force_login(history_user)
    response = client.get(history_url(data))
    assert response.status_code == 200
    assert response.context["history_data"][0]["price"] == "262.40"
    assert response.context["average_lowest_price"] == Decimal("262.40")
    assert response.context["history_data"][0]["volume"] == 10
    assert b'data-copy-price="262"' in response.content
    assert b"price-history-data" in response.content
    assert b"Sample history" not in response.content


@pytest.mark.parametrize("interval,retention", [(0, 90), (2, 90), (1, 0), (1, 10), ("bad", 90)])
def test_recording_settings_reject_unbounded_or_invalid_choices(
    client, history_data, history_user, rendered_pages, interval, retention
):
    client.force_login(history_user)
    response = client.post(
        history_url(history_data),
        {
            "action": "settings",
            "interval_hours": interval,
            "retention_days": retention,
        },
    )
    assert response.status_code == 400
    assert not PriceHistorySettings.objects.exists()


def test_saving_retention_cleans_expired_samples_immediately(
    client, history_data, history_user, rendered_pages
):
    data = history_data
    recording = enable(data, fit_target=data.plan.fit_targets.get())
    PriceHistorySample.objects.create(
        recording=recording, observed_at=timezone.now() - timedelta(days=40), price=1, volume=1
    )
    client.force_login(history_user)
    response = client.post(
        history_url(data), {"action": "settings", "interval_hours": 6, "retention_days": 30}
    )
    assert response.status_code == 302
    assert not recording.samples.exists()
    config = PriceHistorySettings.objects.get(location=data.location)
    assert (config.interval_hours, config.retention_days) == (6, 30)


def test_history_mutations_require_csrf(history_data, history_user):
    client = Client(enforce_csrf_checks=True)
    client.force_login(history_user)
    assert (
        client.post(
            history_url(history_data),
            {
                "action": "toggle",
                "source_id": history_data.plan.fit_targets.get().pk,
                "enabled": "1",
            },
        ).status_code
        == 403
    )
    assert not PriceHistoryRecording.objects.exists()


def test_successful_refresh_records_history_but_failed_import_does_not(history_data):
    from markettracker import tasks

    data = history_data
    cursor = MagicMock()
    cursor.fetchone.return_value = (10000,)
    with (
        patch.object(tasks, "connection") as connection,
        patch.object(tasks.MarketCharacter.objects, "get") as character,
        patch.object(tasks, "location_display_name", return_value="Test market"),
        patch.object(tasks, "_fetch_structure_orders_for_location", return_value={1}) as fetch,
        patch.object(tasks, "record_price_history_safely") as record,
        patch.object(tasks, "send_items_alert"),
        patch.object(tasks, "items_restocked_alert"),
    ):
        connection.cursor.return_value.__enter__.return_value = cursor
        character.return_value.token = object()
        tasks.fetch_market_data_for_location(123, data.location.pk)
        record.assert_called_once_with(data.location.pk)
        record.reset_mock()
        fetch.return_value = set()
        tasks.fetch_market_data_for_location(123, data.location.pk)
        record.assert_not_called()


def test_history_failure_is_isolated_from_market_refresh(history_data):
    with patch(
        "markettracker.price_history.record_price_history", side_effect=RuntimeError("write failed")
    ):
        assert record_price_history_safely(history_data.location.pk) == 0


def test_item_move_resets_history_instead_of_mixing_market_locations(history_data):
    from markettracker.tracked_item_moves import move_tracked_items

    data = history_data
    item = data.location.tracked_items.get(item_id=data.types[1].pk)
    recording = enable(data, tracked_item=item)
    record_price_history(data.location.pk)
    other = TrackedLocation.objects.create(name="Other market", location_id=99)
    move_tracked_items(TrackedItem.objects.filter(pk=item.pk), other)
    assert not PriceHistoryRecording.objects.filter(pk=recording.pk).exists()
    assert not PriceHistorySample.objects.exists()
    assert record_price_history(other.pk) == 0


def test_dispatcher_expires_history_even_without_a_market_character(history_data):
    from markettracker.tasks import fetch_market_data_auto

    recording = enable(history_data, fit_target=history_data.plan.fit_targets.get())
    PriceHistorySample.objects.create(
        recording=recording, observed_at=timezone.now() - timedelta(days=91), price=1, volume=1
    )
    fetch_market_data_auto()
    assert not recording.samples.exists()


@pytest.mark.parametrize("role", ["history_user", "history_viewer"])
def test_history_permission_also_requires_basic_access(
    client, history_data, rendered_pages, request, role
):
    user = request.getfixturevalue(role)
    user.user_permissions.remove(
        Permission.objects.get(content_type__app_label="markettracker", codename="basic_access")
    )
    client.force_login(user)
    assert client.get(history_url(history_data)).status_code == 403


@pytest.mark.parametrize("section", ["doctrines", "items", "groups"])
@pytest.mark.parametrize("recording_enabled", [True, False], ids=["recording", "paused"])
def test_viewers_can_read_history_without_management_controls(
    client, history_data, history_viewer, rendered_pages, section, recording_enabled
):
    data = history_data
    if section == "doctrines":
        source = data.plan.fit_targets.get()
        recording = enable(data, fit_target=source)
    elif section == "items":
        source = data.location.tracked_items.first()
        recording = enable(data, tracked_item=source)
    else:
        source = TrackedItemGroup.objects.create(location=data.location, name="History group")
        source.items.add(EveType.objects.get(pk=data.types[1].pk))
        recording = enable(data, item_group=source)
    recording.is_enabled = recording_enabled
    recording.save(update_fields=["is_enabled"])
    sample = PriceHistorySample.objects.create(
        recording=recording, observed_at=timezone.now(), price=Decimal("123.45"), volume=10
    )
    client.force_login(history_viewer)
    response = client.get(history_url(data, section, record=source.pk))
    assert response.status_code == 200
    assert not response.context["can_manage_price_history"]
    assert response.context["samples"] == [sample]
    assert response.context["history_data"][0]["price"] == "123.45"
    assert response.context["average_lowest_price"] == Decimal("123.45")
    assert b"data-history-location" in response.content
    assert b"data-history-toggle" not in response.content
    assert b'class="history-settings"' not in response.content
    assert b'name="action"' not in response.content
    assert b'name="source_id"' not in response.content
    assert b'class="history-record-status history-muted"' in response.content


@pytest.mark.parametrize(
    ("days", "expected"),
    [(7, Decimal("150.20")), (30, Decimal("200.30")), (90, Decimal("250.40"))],
)
def test_average_lowest_price_uses_the_selected_period_and_record(
    client, history_data, history_viewer, rendered_pages, days, expected
):
    data = history_data
    recording = enable(data, fit_target=data.plan.fit_targets.get())
    now = timezone.now()
    for age, price, volume in [
        (1, "100.10", 1),
        (3, "200.30", 1000),
        (10, "300.50", 10),
        (45, "400.70", 10),
        (100, "99999.00", 10),
        (2, None, 0),
    ]:
        PriceHistorySample.objects.create(
            recording=recording, observed_at=now - timedelta(days=age), price=price, volume=volume
        )
    other = enable(data, tracked_item=data.location.tracked_items.first())
    PriceHistorySample.objects.create(recording=other, observed_at=now, price="99999.00", volume=1)
    client.force_login(history_viewer)
    response = client.get(history_url(data, days=days))
    assert response.status_code == 200
    assert response.context["average_lowest_price"] == expected
    assert f"Average lowest price · {days} days".encode() in response.content
    average_readout = (
        response.content.decode().split('id="history-average-price">', 1)[1].split("</div>", 1)[0]
    )
    assert f'data-copy-price="{int(expected)}"' in average_readout


def test_average_lowest_price_includes_samples_beyond_the_displayed_latest_24(
    client, history_data, history_viewer, rendered_pages
):
    data = history_data
    recording = enable(data, fit_target=data.plan.fit_targets.get())
    now = timezone.now()
    PriceHistorySample.objects.bulk_create(
        [
            PriceHistorySample(
                recording=recording, observed_at=now - timedelta(hours=index), price=100, volume=1
            )
            for index in range(24)
        ]
        + [
            PriceHistorySample(
                recording=recording, observed_at=now - timedelta(days=6), price=350, volume=1
            )
        ]
    )
    client.force_login(history_viewer)
    response = client.get(history_url(data))
    assert response.status_code == 200
    assert len(response.context["samples"]) == 24
    assert response.context["sample_count"] == 25
    assert response.context["average_lowest_price"] == Decimal("110")


@pytest.mark.parametrize(
    ("prices", "expected"),
    [
        ([], None),
        ([None], None),
        ([Decimal("0.00")], Decimal("0.00")),
        ([Decimal("0.10"), Decimal("0.20")], Decimal("0.15")),
    ],
)
def test_average_lowest_price_handles_missing_prices_and_preserves_decimals(
    client, history_data, history_viewer, rendered_pages, prices, expected
):
    data = history_data
    recording = enable(data, fit_target=data.plan.fit_targets.get())
    now = timezone.now()
    PriceHistorySample.objects.bulk_create(
        [
            PriceHistorySample(
                recording=recording, observed_at=now - timedelta(hours=index), price=price, volume=1
            )
            for index, price in enumerate(prices)
        ]
    )
    client.force_login(history_viewer)
    response = client.get(history_url(data))
    assert response.status_code == 200
    assert response.context["average_lowest_price"] == expected
    average_readout = (
        response.content.decode().split('id="history-average-price">', 1)[1].split("</div>", 1)[0]
    )
    if expected is None:
        assert average_readout == "—"
    else:
        assert f'data-copy-price="{int(expected)}"' in average_readout


@pytest.mark.parametrize("action", ["toggle", "settings"])
@pytest.mark.parametrize("stock_manager", [True, False])
def test_viewers_cannot_change_recordings_or_settings_even_with_stock_management(
    client, history_data, history_viewer, rendered_pages, action, stock_manager
):
    data = history_data
    target = data.plan.fit_targets.get()
    recording = enable(data, fit_target=target)
    sample = PriceHistorySample.objects.create(
        recording=recording, observed_at=timezone.now() - timedelta(days=40), price=1, volume=1
    )
    if stock_manager:
        history_viewer.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="markettracker", codename="can_manage_stocks"
            )
        )
    client.force_login(history_viewer)
    response = client.post(
        history_url(data),
        {
            "action": action,
            "source_id": target.pk,
            "enabled": "0",
            "interval_hours": "6",
            "retention_days": "30",
        },
    )
    assert response.status_code == 403
    recording.refresh_from_db()
    config = PriceHistorySettings.objects.get(location=data.location)
    assert recording.is_enabled
    assert (config.interval_hours, config.retention_days) == (1, 90)
    assert list(recording.samples.all()) == [sample]


def test_manager_permission_includes_view_access(
    client, history_data, history_user, rendered_pages
):
    assert not history_user.has_perm("markettracker.can_view_price_history")
    client.force_login(history_user)
    response = client.get(history_url(history_data))
    assert response.status_code == 200
    assert response.context["can_manage_price_history"]
    assert b"data-history-toggle" in response.content
    assert b'class="history-settings"' in response.content


def test_viewers_do_not_gain_access_to_private_doctrine_history(
    client, history_data, history_viewer, rendered_pages
):
    data = history_data
    target = data.plan.fit_targets.get()
    enable(data, fit_target=target)
    record_price_history(data.location.pk)
    data.plan.is_shared = False
    data.plan.save()
    client.force_login(history_viewer)
    assert data.fit.name.encode() not in client.get(history_url(data)).content
    assert client.get(history_url(data, record=target.pk)).status_code == 404


@pytest.mark.parametrize("role", [None, "can_view_price_history", "can_manage_price_history"])
def test_navigation_shows_history_for_either_role_only(
    client, history_data, market_user, rendered_pages, settings, role
):
    templates = deepcopy(settings.TEMPLATES)
    templates[0]["OPTIONS"]["loaders"][0][1]["allianceauth/base-bs5.html"] = (
        "{% block header_nav_collapse_left %}{% endblock %}{% block content %}{% endblock %}"
    )
    settings.TEMPLATES = templates
    if role:
        market_user.user_permissions.add(
            Permission.objects.get(content_type__app_label="markettracker", codename=role)
        )
    client.force_login(market_user)
    response = client.get(reverse("markettracker:doctrine_list"), {"loc": history_data.location.pk})
    assert response.status_code == 200
    link = f'href="{reverse("markettracker:price_history")}"'.encode()
    assert (link in response.content) == bool(role)


def test_fit_quote_matches_existing_complete_fit_price(history_data):
    quote = HistoryMarket(history_data.location.pk).quote(
        "fit", history_data.plan.fit_targets.get()
    )
    assert quote == {"price": Decimal("262.40"), "volume": 10}


def test_thousand_item_recordings_are_batched_and_the_list_is_paginated(
    client, history_data, history_user, rendered_pages
):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    data = history_data
    existing = data.location.tracked_items.select_related("item").first()
    items = [
        EveType(
            id=90000 + i,
            name=f"History item {i}",
            eve_group=existing.item.eve_group,
            description="",
            published=True,
            enabled_sections=0,
        )
        for i in range(1000)
    ]
    EveType.objects.bulk_create(items)
    tracked = TrackedItem.objects.bulk_create(
        [TrackedItem(item=item, location=data.location) for item in items]
    )
    PriceHistorySettings.objects.create(location=data.location)
    PriceHistoryRecording.objects.bulk_create(
        [
            PriceHistoryRecording(location=data.location, tracked_item=item, is_enabled=True)
            for item in tracked
        ]
    )
    with CaptureQueriesContext(connection) as queries:
        assert record_price_history(data.location.pk) == 1000
    assert sum(query["sql"].lstrip().upper().startswith("SELECT") for query in queries) <= 10
    assert PriceHistorySample.objects.count() == 1000
    client.force_login(history_user)
    response = client.get(history_url(data, "items"))
    assert response.context["page_obj"].paginator.count == 1003
    assert len(response.context["rows"]) == 50
