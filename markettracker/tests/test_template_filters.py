import pytest

from markettracker.templatetags.market_extras import compact_isk, integer_isk


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0.00 ISK"),
        (123, "123.00 ISK"),
        (1_234, "1.23K ISK"),
        (6_000_000, "6.00M ISK"),
        (3_000_000_000, "3.00B ISK"),
        (4_500_000_000_000, "4.50T ISK"),
        (999_999_999, "1.00B ISK"),
        (-6_000_000, "-6.00M ISK"),
    ],
)
def test_compact_isk(value, expected):
    assert compact_isk(value) == expected


@pytest.mark.parametrize("value", [None, "", "not-a-number", float("inf")])
def test_compact_isk_returns_empty_string_for_missing_or_invalid_values(value):
    assert compact_isk(value) == ""


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("266621493.78", "266621493"),
        ("999.99", "999"),
        ("9007199254740993.99", "9007199254740993"),
        (0, "0"),
        (None, ""),
        (float("inf"), ""),
        ("invalid", ""),
    ],
)
def test_integer_isk_truncates_without_rounding_or_float_precision_loss(value, expected):
    assert integer_isk(value) == expected
