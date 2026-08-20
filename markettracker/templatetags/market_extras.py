from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django import template

from markettracker.models import TrackedItem

register = template.Library()

_COMPACT_ISK_UNITS = (
    (Decimal("1"), ""),
    (Decimal("1000"), "K"),
    (Decimal("1000000"), "M"),
    (Decimal("1000000000"), "B"),
    (Decimal("1000000000000"), "T"),
)
_TWO_DECIMAL_PLACES = Decimal("0.01")


@register.filter
def compact_isk(value):
    """Format an ISK amount with a compact unit and two decimal places."""
    if value is None or value == "":
        return ""

    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return ""

    if not amount.is_finite():
        return ""

    unit_index = 0
    absolute_amount = abs(amount)
    for index, (divisor, _suffix) in enumerate(_COMPACT_ISK_UNITS[1:], start=1):
        if absolute_amount < divisor:
            break
        unit_index = index

    divisor, suffix = _COMPACT_ISK_UNITS[unit_index]
    compact_amount = (amount / divisor).quantize(
        _TWO_DECIMAL_PLACES,
        rounding=ROUND_HALF_UP,
    )

    # Avoid output such as 1000.00M when rounding reaches the next unit.
    if abs(compact_amount) >= 1000 and unit_index < len(_COMPACT_ISK_UNITS) - 1:
        unit_index += 1
        divisor, suffix = _COMPACT_ISK_UNITS[unit_index]
        compact_amount = (amount / divisor).quantize(
            _TWO_DECIMAL_PLACES,
            rounding=ROUND_HALF_UP,
        )

    return f"{compact_amount:.2f}{suffix} ISK"


@register.filter
def tracked_item(type_id):
    try:
        return TrackedItem.objects.select_related("item").get(item__id=type_id)
    except TrackedItem.DoesNotExist:
        return None
