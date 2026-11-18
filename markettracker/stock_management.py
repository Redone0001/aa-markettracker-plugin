"""Shared navigation and safe return state for stock-management screens."""

from django.http import QueryDict
from django.urls import reverse
from django.utils.translation import gettext as _

from .models import (
    MarketCharacter,
    TrackedContractLocation,
    TrackedDoctrine,
    TrackedItem,
    TrackedItemGroup,
    TrackedLocation,
)
from .utils import location_display_name

MANAGEMENT_SECTIONS = ("items", "groups", "doctrines", "contracts")
FILTER_KEYS = ("q", "cq", "item_type", "module", "exclude_item_types", "doctrine_only", "page")


def management_query(request, location, section):
    """Keep filters, never accept a redirect URL or a different location."""
    raw = request.POST.get("return_query", "") if request.method == "POST" else ""
    source = QueryDict(raw) if raw else request.GET
    query = QueryDict(mutable=True)
    for key in FILTER_KEYS:
        values = [value for value in source.getlist(key) if value]
        if values:
            query.setlist(key, values)
    if location:
        query["loc"] = str(location.pk)
    query["section"] = section if section in MANAGEMENT_SECTIONS else "items"
    return query


def management_return_url(request, location, section):
    query = management_query(request, location, section)
    route = "manage_doctrines" if query["section"] == "doctrines" else "manage_stock"
    return f"{reverse('markettracker:' + route)}?{query.urlencode()}"


def management_context(request, location, section):
    locations = list(TrackedLocation.objects.filter(is_active=True).order_by("-is_default", "name"))
    for entry in locations:
        entry.display_name = location_display_name(entry)
    counts = {key: 0 for key in MANAGEMENT_SECTIONS}
    if location:
        counts = {
            "items": TrackedItem.objects.filter(location=location).count(),
            "groups": TrackedItemGroup.objects.filter(location=location).count(),
            "doctrines": TrackedDoctrine.objects.filter(location=location).count(),
            "contracts": TrackedContractLocation.objects.filter(
                location=location, is_active=True
            ).count(),
        }
    tabs = []
    for key, label, icon in (
        ("items", _("Items"), "fa-cube"),
        ("groups", _("Groups"), "fa-layer-group"),
        ("doctrines", _("Doctrines"), "fa-folder-open"),
        ("contracts", _("Contracts"), "fa-file-contract"),
    ):
        route = "manage_doctrines" if key == "doctrines" else "manage_stock"
        tabs.append(
            {
                "key": key,
                "label": label,
                "icon": icon,
                "count": counts[key],
                "active": section == key,
                "url": f"{reverse('markettracker:' + route)}?loc={location.pk if location else ''}&section={key}",
            }
        )
    query = management_query(request, location, section)
    return {
        "management_section": section,
        "management_tabs": tabs,
        "management_counts": counts,
        "return_query": query.urlencode(),
        "return_url": management_return_url(request, location, section),
        "preserved_filters": [
            (key, value)
            for key, values in query.lists()
            if key not in {"loc", "section", "page"}
            for value in values
        ],
        "locations": locations,
        "selected_location": location,
        "location_title": location_display_name(location) if location else "",
        "market_character": MarketCharacter.objects.filter(type="admin")
        .select_related("character__character")
        .first(),
    }
