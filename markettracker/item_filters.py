"""Classification helpers for the tracked-item module filters."""

MODULE_CATEGORY_ID = 7

TECH_I_META_GROUP_ID = 1
TECH_II_META_GROUP_ID = 2
FACTION_META_GROUP_ID = 4
DEADSPACE_META_GROUP_ID = 6

MODULE_FILTER_KEYS = ("meta", "t1", "t2", "faction", "complex")


def normalize_module_filters(values):
    """Return known filter keys once, in their display order."""
    requested = set(values)
    return tuple(key for key in MODULE_FILTER_KEYS if key in requested)


def module_filter_keys(meta_group_id, meta_level):
    """Classify one module using the meta data provided by the EVE SDE."""
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


def matches_module_filters(meta_group_id, meta_level, selected_filters):
    """Return whether an item matches any of the selected module filters."""
    selected = set(selected_filters)
    return not selected or bool(module_filter_keys(meta_group_id, meta_level) & selected)
