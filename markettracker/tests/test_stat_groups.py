import pytest
from django.http import QueryDict
from eve_sde.models import DogmaAttribute, ItemCategory, ItemGroup, ItemType, TypeDogma

from markettracker.forms import TrackedItemGroupForm
from markettracker.models import TrackedItemGroup, TrackedLocation
from markettracker.sde_items import ensure_local_types
from markettracker.stat_groups import matching_type_ids


@pytest.fixture
def stat_data(db):
    category = ItemCategory.objects.create(id=88, name="Modules", published=True)
    group = ItemGroup.objects.create(id=88, name="Propulsion", category=category, published=True)
    other_group = ItemGroup.objects.create(id=89, name="Weapons", category=category, published=True)
    types = []
    for index, item_group in enumerate([group, group, group, group, other_group]):
        types.append(
            ItemType.objects.create(
                id=8000 + index,
                name=f"Stat module {index}",
                group=item_group,
                published=True,
                mass=1,
                volume=5,
                capacity=0,
            )
        )
    power = DogmaAttribute.objects.create(id=50, name="Power", display_name="Powergrid")
    cpu = DogmaAttribute.objects.create(id=51, name="CPU")
    for index, item in enumerate(types):
        TypeDogma.objects.create(
            item_type=item, dogma_attribute=power, value=10 if index != 2 else 20
        )
        TypeDogma.objects.create(item_type=item, dogma_attribute=cpu, value=5 if index != 3 else 15)
    return types, power, cpu


def test_all_stats_and_subset_matching_are_group_scoped(stat_data, django_assert_num_queries):
    types, power, _ = stat_data
    with django_assert_num_queries(3):
        assert set(matching_type_ids(types[0].pk)) == {types[0].pk, types[1].pk}
    assert set(matching_type_ids(types[0].pk, [str(power.pk)])) == {types[i].pk for i in [0, 1, 3]}
    assert set(matching_type_ids(types[0].pk, ["mass"])) == {item.pk for item in types[:4]}


def test_missing_and_extra_dogma_are_not_silently_equal(stat_data):
    types, power, _ = stat_data
    TypeDogma.objects.filter(item_type=types[1], dogma_attribute=power).delete()
    extra = DogmaAttribute.objects.create(id=52, name="Extra stat")
    TypeDogma.objects.create(item_type=types[3], dogma_attribute=extra, value=7)
    assert matching_type_ids(types[0].pk) == [types[0].pk]
    assert types[1].pk not in matching_type_ids(types[0].pk, [str(power.pk)])


def test_empty_dogma_reports_missing_sde_instead_of_grouping_every_item(stat_data):
    types, _, _ = stat_data
    TypeDogma.objects.all().delete()
    with pytest.raises(ValueError, match="No dogma stats"):
        matching_type_ids(types[0].pk)


def test_stat_group_form_materializes_all_matching_sde_items_and_saves_snapshot(stat_data):
    types, power, _ = stat_data
    ensure_local_types([types[0].pk])
    location = TrackedLocation.objects.create(name="Stats", location_id=77)
    data = QueryDict(mutable=True)
    data.update(
        name="Stat equivalents",
        group_mode="stats",
        stats_reference=types[0].pk,
        desired_quantity=100,
    )
    data.setlist("matching_stats", [str(power.pk)])
    form = TrackedItemGroupForm(data, location=location)
    assert form.is_valid(), form.errors
    group = form.save()
    assert set(group.items.values_list("pk", flat=True)) == {types[i].pk for i in [0, 1, 3]}
    assert group.matching_stats == [str(power.pk)]
    # An SDE edit doesn't trigger any background universe-wide recomputation.
    TypeDogma.objects.filter(item_type=types[3], dogma_attribute=power).update(value=999)
    assert group.items.count() == 3
    form = TrackedItemGroupForm(data, instance=group, location=location)
    assert form.is_valid(), form.errors
    group = form.save()
    assert group.items.count() == 2
    data["group_mode"] = "manual"
    data.setlist("items", [str(types[0].pk)])
    form = TrackedItemGroupForm(data, instance=group, location=location)
    assert form.is_valid(), form.errors
    group = form.save()
    assert group.stats_reference is None
    assert group.matching_stats == []


def test_invalid_stat_selection_and_reference_are_rejected(stat_data):
    types, _, _ = stat_data
    location = TrackedLocation.objects.create(name="Stats", location_id=77)
    data = QueryDict(mutable=True)
    data.update(name="Invalid", group_mode="stats", desired_quantity=100)
    form = TrackedItemGroupForm(data, location=location)
    assert not form.is_valid()
    assert "stats_reference" in form.errors
    ensure_local_types([types[0].pk])
    data["stats_reference"] = types[0].pk
    data.setlist("matching_stats", ["999999"])
    form = TrackedItemGroupForm(data, location=location)
    assert not form.is_valid()
    assert "matching_stats" in form.errors
    assert not TrackedItemGroup.objects.exists()


def test_selecting_every_checkbox_still_compares_extra_candidate_stats(stat_data):
    from markettracker.stat_groups import stat_choices

    types, _, _ = stat_data
    extra = DogmaAttribute.objects.create(id=52, name="Extra stat")
    TypeDogma.objects.create(item_type=types[1], dogma_attribute=extra, value=5)
    keys = [key for key, _ in stat_choices(types[0].pk)]
    assert matching_type_ids(types[0].pk, keys) == [types[0].pk]


def test_reference_missing_from_local_sde_is_a_useful_validation_error(db):
    with pytest.raises(ValueError, match="missing from the local SDE"):
        matching_type_ids(9999999)


def test_all_stats_persist_as_all_when_the_sde_adds_new_attributes(stat_data):
    from markettracker.stat_groups import stat_choices

    types, _, _ = stat_data
    ensure_local_types([types[0].pk])
    location = TrackedLocation.objects.create(name="All stats", location_id=77)
    data = QueryDict(mutable=True)
    data.update(
        name="All stats", group_mode="stats", stats_reference=types[0].pk, desired_quantity=10
    )
    data.setlist("matching_stats", [key for key, _ in stat_choices(types[0].pk)])
    form = TrackedItemGroupForm(data, location=location)
    assert form.is_valid(), form.errors
    group = form.save()
    assert group.matching_stats == []
    extra = DogmaAttribute.objects.create(id=52, name="New stat")
    TypeDogma.objects.create(item_type=types[0], dogma_attribute=extra, value=7)
    edited = TrackedItemGroupForm(instance=group, location=location)
    assert str(extra.pk) in edited.initial["matching_stats"]
