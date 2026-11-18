"""Explicit, group-scoped stat comparisons against the installed SDE."""

from collections import defaultdict

from eve_sde.models import ItemType, TypeDogma

PHYSICAL_STATS = ("mass", "volume", "capacity")
MAX_STAT_GROUP_MEMBERS = 1000


def stat_choices(reference_id):
    if not ItemType.objects.filter(pk=reference_id).exists():
        return []
    choices = [(field, field.capitalize()) for field in PHYSICAL_STATS]
    choices.extend(
        (str(row.dogma_attribute_id), row.dogma_attribute.display_name or row.dogma_attribute.name)
        for row in TypeDogma.objects.filter(
            item_type_id=reference_id, dogma_attribute__isnull=False
        )
        .select_related("dogma_attribute")
        .order_by("dogma_attribute_id")
    )
    return choices


def matching_type_ids(reference_id, selected_stats=()):
    try:
        reference = ItemType.objects.get(pk=reference_id)
    except ItemType.DoesNotExist as error:
        raise ValueError(
            "The reference item is missing from the local SDE. Update the SDE first."
        ) from error
    if reference.group_id is None:
        raise ValueError("The reference item has no SDE group. Update the local SDE first.")
    candidates = ItemType.objects.filter(group_id=reference.group_id, published=True)
    selected = set(selected_stats)
    if selected and set(PHYSICAL_STATS).issubset(selected):
        all_keys = set(PHYSICAL_STATS) | {
            str(key)
            for key in TypeDogma.objects.filter(
                item_type_id=reference_id, dogma_attribute__isnull=False
            ).values_list("dogma_attribute_id", flat=True)
        }
        if selected == all_keys:
            selected.clear()
    for field in PHYSICAL_STATS:
        if not selected or field in selected:
            candidates = candidates.filter(**{field: getattr(reference, field)})
    ids = list(candidates.values_list("pk", flat=True))
    # One indexed, group-scoped dogma query, never a full-universe comparison.
    dogma = defaultdict(dict)
    rows = TypeDogma.objects.filter(
        item_type_id__in=set(ids) | {reference_id}, dogma_attribute__isnull=False
    )
    if selected:
        rows = rows.filter(dogma_attribute_id__in=[int(key) for key in selected if key.isdigit()])
    for type_id, attribute_id, value in rows.values_list(
        "item_type_id", "dogma_attribute_id", "value"
    ):
        dogma[type_id][attribute_id] = value
    if not dogma[reference_id] and (not selected or any(key.isdigit() for key in selected)):
        raise ValueError(
            "No dogma stats are loaded for this reference item. Update the local SDE first."
        )
    matches = [type_id for type_id in ids if dogma[type_id] == dogma[reference_id]]
    if len(matches) > MAX_STAT_GROUP_MEMBERS:
        raise ValueError("More than 1,000 items match. Select more stats to narrow the group.")
    if not matches:
        raise ValueError("No published items match the selected stats.")
    return matches
