from pathlib import Path

import pytest
import tomllib
from django.template.loader import get_template

from markettracker import __version__, auth_hooks
from markettracker.models import TrackedLocation

ROOT = Path(__file__).resolve().parents[2]


def test_package_is_marked_as_a_beta_release():
    assert __version__ == "2.0.0b1"


def test_project_metadata_targets_alliance_auth_5():
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = metadata["project"]["dependencies"]

    assert "allianceauth>=5.0,<6" in dependencies
    assert "django>=5.2,<6" in dependencies
    assert "django-esi>=9.4,<10" in dependencies
    assert "django-eveonline-sde>=0.1,<1" in dependencies


def test_markettracker_base_extends_aa5_bootstrap_template():
    template = get_template("markettracker/base.html")

    assert template.origin.name.endswith("markettracker/templates/markettracker/base.html")
    assert get_template("allianceauth/base-bs5.html")


def test_aa5_chart_bundle_is_available():
    assert get_template("bundles/chart-js.html")


def test_menu_hook_uses_community_app_order_range():
    menu_item = auth_hooks.MarketTrackerMenuItem()

    assert menu_item.order >= 1000


@pytest.mark.django_db
def test_markettracker_schema_migrates_cleanly_on_aa5():
    location = TrackedLocation.objects.create(
        name="The Forge",
        scope=TrackedLocation.Scope.REGION,
        location_id=10999999,
        is_default=True,
    )

    assert location.pk is not None
