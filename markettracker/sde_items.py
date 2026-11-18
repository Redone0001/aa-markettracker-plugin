"""Materialize local SDE types without one ESI request per item."""

from eve_sde.models import ItemType
from eveuniverse.models import EveCategory, EveGroup, EveType


def ensure_local_types(type_ids):
    missing = set(type_ids) - set(
        EveType.objects.filter(pk__in=type_ids).values_list("pk", flat=True)
    )
    rows = list(ItemType.objects.filter(pk__in=missing).select_related("group__category"))
    categories, groups, types = {}, {}, []
    for row in rows:
        group = row.group
        if group is None or group.category is None:
            raise ValueError(f"SDE category/group data is missing for {row.name} ({row.pk}).")
        category = group.category
        categories[category.pk] = EveCategory(
            id=category.pk, name=category.name, published=category.published
        )
        groups[group.pk] = EveGroup(
            id=group.pk, name=group.name, published=group.published, eve_category_id=category.pk
        )
        types.append(
            EveType(
                id=row.pk,
                name=row.name,
                description=row.description or "",
                published=row.published,
                eve_group_id=group.pk,
                volume=row.volume,
                mass=row.mass,
                capacity=row.capacity,
                packaged_volume=row.packaged_volume,
                enabled_sections=0,
            )
        )
    EveCategory.objects.bulk_create(categories.values(), ignore_conflicts=True)
    EveGroup.objects.bulk_create(groups.values(), ignore_conflicts=True)
    EveType.objects.bulk_create(types, ignore_conflicts=True)
    unresolved = missing - {row.pk for row in rows}
    if unresolved:
        raise ValueError("Missing local SDE types: " + ", ".join(map(str, sorted(unresolved))))
