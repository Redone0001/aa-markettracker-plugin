"""Parsing and persistence helpers for tracked-item bulk imports."""

import re
from collections import OrderedDict
from dataclasses import dataclass

from django.db import transaction
from django.db.models.functions import Lower
from eveuniverse.models import EveType

from .models import TrackedItem

MAX_BULK_IMPORT_LINES = 500
MAX_DESIRED_QUANTITY = 2_147_483_647
MAX_ITEM_NAME_LENGTH = 255

EXCLUDED_GROUP_IDS = [6, 1, 14]
EXCLUDED_CATEGORIES = ["Blueprint", "SKINs"]

_X_QUANTITY_RE = re.compile(
    r"^(?P<name>.+?)\s+[xX×]\s*(?P<quantity>[0-9][0-9,_]*)$"
)
_COLUMN_QUANTITY_RE = re.compile(
    r"^(?P<name>.+?)(?:\t+| {2,})(?P<quantity>[0-9][0-9,_]*)$"
)
_INVALID_X_QUANTITY_RE = re.compile(r"\s+[xX×]\s*\S+\s*$")


@dataclass(frozen=True)
class BulkItemEntry:
    name: str
    quantity: int
    line_number: int


@dataclass(frozen=True)
class BulkImportResult:
    added: int
    updated: int
    skipped: int
    unknown_names: tuple[str, ...]


class BulkImportError(ValueError):
    """Raised when parsed quantities cannot be stored safely."""


def _clean_item_name(name):
    return " ".join(name.split())


def _parse_quantity(raw_quantity):
    normalized = raw_quantity.replace(",", "").replace("_", "")
    if len(normalized) > 10:
        raise ValueError
    return int(normalized)


def parse_bulk_item_list(raw_text):
    """Parse supported list formats into entries and human-readable errors."""
    entries = []
    errors = []
    non_empty_lines = 0

    for line_number, raw_line in enumerate((raw_text or "").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue

        non_empty_lines += 1
        if non_empty_lines > MAX_BULK_IMPORT_LINES:
            errors.append(
                f"The list contains more than {MAX_BULK_IMPORT_LINES} non-empty lines."
            )
            break

        match = _X_QUANTITY_RE.fullmatch(line)
        if match is None:
            match = _COLUMN_QUANTITY_RE.fullmatch(raw_line.rstrip())

        if match is not None:
            name = _clean_item_name(match.group("name"))
            try:
                quantity = _parse_quantity(match.group("quantity"))
            except ValueError:
                errors.append(f"Line {line_number}: quantity is too large.")
                continue
        else:
            if _INVALID_X_QUANTITY_RE.search(line):
                errors.append(f"Line {line_number}: invalid quantity in {line!r}.")
                continue
            name = _clean_item_name(line)
            quantity = 1

        if not name:
            errors.append(f"Line {line_number}: item name is missing.")
        elif len(name) > MAX_ITEM_NAME_LENGTH:
            errors.append(
                f"Line {line_number}: item name exceeds {MAX_ITEM_NAME_LENGTH} characters."
            )
        elif quantity < 1:
            errors.append(f"Line {line_number}: quantity must be at least 1.")
        else:
            entries.append(
                BulkItemEntry(name=name, quantity=quantity, line_number=line_number)
            )

    if not entries and not errors:
        errors.append("Enter at least one item.")

    return entries, errors


def _aggregate_entries(entries, multiplier):
    aggregated = OrderedDict()
    for entry in entries:
        key = entry.name.lower()
        if key not in aggregated:
            aggregated[key] = {"name": entry.name, "quantity": 0}
        aggregated[key]["quantity"] += entry.quantity

    for value in aggregated.values():
        value["quantity"] *= multiplier
        if value["quantity"] > MAX_DESIRED_QUANTITY:
            raise BulkImportError(
                f"The resulting quantity for {value['name']!r} exceeds "
                f"{MAX_DESIRED_QUANTITY:,}."
            )
    return aggregated


@transaction.atomic
def import_tracked_items(location, entries, multiplier=1, overwrite_amount=False):
    """Create or optionally update tracked items for one location."""
    if multiplier < 1:
        raise BulkImportError("Multiplier must be at least 1.")

    requested = _aggregate_entries(entries, multiplier)
    candidates = (
        EveType.objects
        .filter(published=True, name__isnull=False)
        .exclude(eve_group_id__in=EXCLUDED_GROUP_IDS)
        .exclude(eve_group__eve_category__name__in=EXCLUDED_CATEGORIES)
        .annotate(bulk_name_lower=Lower("name"))
        .filter(bulk_name_lower__in=requested.keys())
        .order_by("id")
    )

    candidates_by_name = {}
    ambiguous_names = set()
    for item in candidates:
        key = item.name.lower()
        if key in candidates_by_name:
            ambiguous_names.add(key)
        else:
            candidates_by_name[key] = item

    for key in ambiguous_names:
        candidates_by_name.pop(key, None)

    resolved = {
        candidates_by_name[key].id: (candidates_by_name[key], value["quantity"])
        for key, value in requested.items()
        if key in candidates_by_name
    }
    unknown_names = tuple(
        value["name"] for key, value in requested.items() if key not in candidates_by_name
    )

    existing = {
        tracked.item_id: tracked
        for tracked in TrackedItem.objects.select_for_update().filter(
            location=location,
            item_id__in=resolved.keys(),
        )
    }
    additions = []
    updates = []
    skipped = 0

    for item_id, (item, desired_quantity) in resolved.items():
        tracked = existing.get(item_id)
        if tracked is None:
            additions.append(
                TrackedItem(
                    item=item,
                    location=location,
                    desired_quantity=desired_quantity,
                )
            )
        elif overwrite_amount and tracked.desired_quantity != desired_quantity:
            tracked.desired_quantity = desired_quantity
            updates.append(tracked)
        else:
            skipped += 1

    TrackedItem.objects.bulk_create(additions)
    if updates:
        TrackedItem.objects.bulk_update(updates, ["desired_quantity"])

    return BulkImportResult(
        added=len(additions),
        updated=len(updates),
        skipped=skipped,
        unknown_names=unknown_names,
    )
