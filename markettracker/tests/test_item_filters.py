import pytest
from django.template.loader import get_template, render_to_string

from markettracker.item_filters import (
    item_filter_keys,
    matches_item_filters,
    normalize_item_filters,
)


@pytest.mark.parametrize(
    ("category_id", "meta_group_id", "meta_level", "expected"),
    [
        (7, 1, 0, {"t1"}),
        (7, 1, None, {"t1"}),
        (7, 1, 1, {"meta"}),
        (7, 2, 5, {"t2"}),
        (7, 4, 8, {"faction"}),
        (7, 6, 12, {"complex"}),
        (7, 5, 12, set()),  # Officer is deliberately not Complex/Deadspace.
        (7, 3, 6, set()),  # Storyline is deliberately not a named-meta module.
        (6, None, None, {"ship"}),
        (20, None, None, {"implant"}),
        (8, None, None, set()),
    ],
)
def test_item_filter_keys(category_id, meta_group_id, meta_level, expected):
    assert item_filter_keys(category_id, meta_group_id, meta_level) == expected


def test_multiple_item_filters_use_or_semantics():
    selected = ("t2", "faction", "complex", "ship", "implant")

    assert matches_item_filters(7, 2, 5, selected)
    assert matches_item_filters(7, 4, 8, selected)
    assert matches_item_filters(7, 6, 12, selected)
    assert matches_item_filters(6, None, None, selected)
    assert matches_item_filters(20, None, None, selected)
    assert not matches_item_filters(7, 1, 0, selected)


def test_no_item_filters_accepts_every_item():
    assert matches_item_filters(None, None, None, ())


def test_item_filter_normalization_removes_unknown_values_and_duplicates():
    values = ["implant", "complex", "unknown", "t1", "complex", "ship"]

    assert normalize_item_filters(values) == ("t1", "complex", "ship", "implant")


def test_item_filter_controls_render_all_multi_select_options():
    options = [
        {"key": key, "label": key, "selected": key in {"t2", "complex", "ship"}}
        for key in ("meta", "t1", "t2", "faction", "complex", "ship", "implant")
    ]

    html = render_to_string(
        "markettracker/includes/item_filter_controls.html",
        {
            "item_filters": options,
        },
    )

    assert html.count('name="item_type"') == 7
    assert 'value="t2"' in html
    assert 'value="complex"' in html
    assert 'value="ship"' in html
    assert 'value="implant"' in html
    assert html.count("checked") == 3
    assert "dropdown" not in html
    assert html.count("item-filter-btn") == 7


@pytest.mark.parametrize(
    "template_name",
    ["markettracker/list_items.html", "markettracker/manage_stock.html"],
)
def test_tracked_item_pages_include_item_filters(template_name):
    template = get_template(template_name)

    assert "markettracker/includes/item_filter_controls.html" in template.template.source


def test_item_filter_buttons_define_dark_theme_contrast():
    template = get_template("markettracker/base.html")
    source = template.template.source

    assert ':root[data-bs-theme="dark"] .item-filter-btn' in source
    assert "--bs-btn-color: #f8f9fa" in source
    assert "--bs-btn-bg: #343a40" in source
    assert "--bs-btn-border-color: #dee2e6" in source
