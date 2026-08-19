import pytest
from django.template.loader import get_template, render_to_string

from markettracker.item_filters import (
    matches_module_filters,
    module_filter_keys,
    normalize_module_filters,
)


@pytest.mark.parametrize(
    ("meta_group_id", "meta_level", "expected"),
    [
        (1, 0, {"t1"}),
        (1, None, {"t1"}),
        (1, 1, {"meta"}),
        (2, 5, {"t2"}),
        (4, 8, {"faction"}),
        (6, 12, {"complex"}),
        (5, 12, set()),  # Officer is deliberately not Complex/Deadspace.
        (3, 6, set()),  # Storyline is deliberately not a named-meta module.
    ],
)
def test_module_filter_keys(meta_group_id, meta_level, expected):
    assert module_filter_keys(meta_group_id, meta_level) == expected


def test_multiple_module_filters_use_or_semantics():
    selected = ("t2", "faction", "complex")

    assert matches_module_filters(2, 5, selected)
    assert matches_module_filters(4, 8, selected)
    assert matches_module_filters(6, 12, selected)
    assert not matches_module_filters(1, 0, selected)


def test_no_module_filters_accepts_every_item():
    assert matches_module_filters(None, None, ())


def test_module_filter_normalization_removes_unknown_values_and_duplicates():
    values = ["complex", "unknown", "t1", "complex"]

    assert normalize_module_filters(values) == ("t1", "complex")


def test_module_filter_controls_render_all_multi_select_options():
    options = [
        {"key": key, "label": key, "selected": key in {"t2", "complex"}}
        for key in ("meta", "t1", "t2", "faction", "complex")
    ]

    html = render_to_string(
        "markettracker/includes/module_filter_controls.html",
        {
            "module_filters": options,
        },
    )

    assert html.count('name="module"') == 5
    assert 'value="t2"' in html
    assert 'value="complex"' in html
    assert html.count("checked") == 2
    assert "dropdown" not in html
    assert html.count("module-filter-btn") == 5


@pytest.mark.parametrize(
    "template_name",
    ["markettracker/list_items.html", "markettracker/manage_stock.html"],
)
def test_tracked_item_pages_include_module_filters(template_name):
    template = get_template(template_name)

    assert "markettracker/includes/module_filter_controls.html" in template.template.source


def test_module_filter_buttons_define_dark_theme_contrast():
    template = get_template("markettracker/base.html")
    source = template.template.source

    assert ':root[data-bs-theme="dark"] .module-filter-btn' in source
    assert "--bs-btn-color: #f8f9fa" in source
    assert "--bs-btn-bg: #343a40" in source
    assert "--bs-btn-border-color: #dee2e6" in source
