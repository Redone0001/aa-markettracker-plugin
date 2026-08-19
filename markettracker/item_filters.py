"""Classification helpers for the tracked-item type filters."""

SHIP_CATEGORY_ID = 6
MODULE_CATEGORY_ID = 7
IMPLANT_CATEGORY_ID = 20

TECH_I_META_GROUP_ID = 1
TECH_II_META_GROUP_ID = 2
FACTION_META_GROUP_ID = 4
DEADSPACE_META_GROUP_ID = 6

ITEM_FILTER_KEYS = ("meta", "t1", "t2", "faction", "complex", "ship", "implant")


def normalize_item_filters(values):
    """Return known filter keys once, in their display order."""
    requested = set(values)
    return tuple(key for key in ITEM_FILTER_KEYS if key in requested)


def item_filter_keys(category_id, meta_group_id, meta_level):
    """Classify an item using its EVE category and SDE meta data."""
    if category_id == SHIP_CATEGORY_ID:
        return frozenset({"ship"})
    if category_id == IMPLANT_CATEGORY_ID:
        return frozenset({"implant"})
    if category_id != MODULE_CATEGORY_ID:
        return frozenset()

    if meta_group_id == TECH_I_META_GROUP_ID:
        if meta_level is not None and meta_level > 0:
            return frozenset({"meta"})
        return frozenset({"t1"})
    if meta_group_id == TECH_II_META_GROUP_ID:
        return frozenset({"t2"})
    if meta_group_id == FACTION_META_GROUP_ID:
        return frozenset({"faction"})
    if meta_group_id == DEADSPACE_META_GROUP_ID:
        return frozenset({"complex"})
    return frozenset()


def matches_item_filters(category_id, meta_group_id, meta_level, selected_filters):
    """Return whether an item matches any selected item-type filter."""
    selected = set(selected_filters)
    return not selected or bool(
        item_filter_keys(category_id, meta_group_id, meta_level) & selected
    )
