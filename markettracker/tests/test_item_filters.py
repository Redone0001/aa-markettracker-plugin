import pytest

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
