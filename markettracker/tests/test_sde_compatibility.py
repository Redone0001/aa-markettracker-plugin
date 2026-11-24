"""Exercise the SDE import and ORM APIs used by MarketTracker."""

import json

import pytest
from eve_sde.models import DogmaAttribute, ItemCategory, ItemGroup, ItemType, TypeDogma
from eveuniverse.models import EveType

from markettracker.models import TrackedItem, TrackedItemGroup, TrackedLocation
from markettracker.sde_items import ensure_local_types
from markettracker.stat_groups import matching_type_ids, stat_choices
from markettracker.views import _filter_tracked_items_by_type


def write_jsonl(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


@pytest.fixture
def imported_sde(db, tmp_path):
    write_jsonl(tmp_path / "_sde.jsonl", [{"buildNumber": 1000}])
    write_jsonl(
        tmp_path / "categories.jsonl",
        [{"_key": 7, "name": {"en": "Module"}, "published": True}],
    )
    write_jsonl(
        tmp_path / "groups.jsonl",
        [{
            "_key": 77,
            "categoryID": 7,
            "name": {"en": "Test modules"},
            "published": True,
            "anchorable": False,
            "anchored": False,
            "fittableNonSingleton": False,
            "useBasePrice": False,
        }],
    )
    types = [
        {
            "_key": 9000 + index,
            "name": {"en": f"Module {index}"},
            "description": {"en": f"Description {index}"},
            "groupID": 77,
            "published": True,
            "mass": 100,
            "volume": 5,
            "capacity": 0,
            "packagedVolume": 2.5,
            "metaGroupID": meta_group,
            "metaLevel": meta_level,
        }
        for index, (meta_group, meta_level) in enumerate(
            [(1, 0), (1, 1), (2, 5), (4, 8), (6, 12)]
        )
    ]
    write_jsonl(tmp_path / "types.jsonl", types)
    write_jsonl(
        tmp_path / "dogmaAttributes.jsonl",
        [{"_key": 50, "name": "power", "displayName": {"en": "Powergrid"}}],
    )
    dogma = [
        {"_key": row["_key"], "dogmaAttributes": [{"attributeID": 50, "value": 10}]}
        for row in types
    ]
    write_jsonl(tmp_path / "typeDogma.jsonl", dogma)
    for model in [ItemCategory, ItemGroup, ItemType, DogmaAttribute, TypeDogma]:
        model.load_from_sde(str(tmp_path))
    return tmp_path, dogma


def test_imported_sde_types_materialize_names_relations_and_physical_stats(imported_sde):
    ensure_local_types(range(9000, 9005))

    assert EveType.objects.count() == 5
    item = EveType.objects.select_related("eve_group__eve_category").get(pk=9000)
    assert item.name == "Module 0"
    assert item.description == "Description 0"
    assert item.published
    assert item.eve_group_id == 77
    assert item.eve_group.name == "Test modules"
    assert item.eve_group.eve_category_id == 7
    assert item.eve_group.eve_category.name == "Module"
    assert (item.mass, item.volume, item.capacity, item.packaged_volume) == (100, 5, 0, 2.5)


def test_imported_sde_metadata_drives_module_filters(imported_sde):
    ensure_local_types(range(9000, 9005))
    location = TrackedLocation.objects.create(name="Filters", location_id=77)
    TrackedItem.objects.bulk_create(
        [TrackedItem(item_id=type_id, location=location) for type_id in range(9000, 9005)]
    )
    tracked = list(TrackedItem.objects.select_related("item__eve_group__eve_category"))

    for index, key in enumerate(["t1", "meta", "t2", "faction", "complex"]):
        assert [row.item_id for row in _filter_tracked_items_by_type(tracked, [key])] == [
            9000 + index
        ]


def test_dogma_reload_refreshes_matches_without_changing_saved_group_membership(imported_sde):
    folder, dogma = imported_sde
    ids = set(range(9000, 9005))
    ensure_local_types(ids)
    location = TrackedLocation.objects.create(name="Stats", location_id=77)
    group = TrackedItemGroup.objects.create(name="Saved matches", location=location)
    group.items.set(matching_type_ids(9000))
    assert set(group.items.values_list("pk", flat=True)) == ids
    assert ("50", "Powergrid") in stat_choices(9000)

    dogma[1]["dogmaAttributes"][0]["value"] = 20
    write_jsonl(folder / "typeDogma.jsonl", dogma)
    TypeDogma.load_from_sde(str(folder))

    assert TypeDogma.objects.count() == 5
    assert set(matching_type_ids(9000)) == ids - {9001}
    assert set(group.items.values_list("pk", flat=True)) == ids
