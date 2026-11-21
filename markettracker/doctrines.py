"""Doctrine requirements, provenance, and stock allocation (no live ESI calls)."""

from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction
from django.db.models import Prefetch

from .models import (
    DoctrineFitTarget,
    MarketOrderSnapshot,
    TrackedDoctrine,
    TrackedItem,
    TrackedItemGroup,
    TrackedLocation,
)
from .sde_items import ensure_local_types


@dataclass
class Requirement:
    key: tuple
    name: str
    type_ids: frozenset
    quantity: int
    original_id: int


def location_groups(location_id):
    # Prefer the most specific substitution group; ties use the oldest group.
    groups = list(
        TrackedItemGroup.objects.filter(location_id=location_id).prefetch_related("items")
    )
    groups.sort(key=lambda group: (len(group.items.all()), group.pk))
    mapping = {}
    for group in groups:
        ids = frozenset(item.pk for item in group.items.all())
        for type_id in ids:
            mapping.setdefault(type_id, (group, ids))
    return mapping


def fit_requirements(fit, groups):
    components = Counter({fit.ship_type_id: 1})
    names = {fit.ship_type_id: fit.ship_type.name}
    for item in fit.items.all():
        if item.quantity > 0:
            components[item.type_id] += item.quantity
            names[item.type_id] = item.type_fk.name
    requirements = {}
    for type_id, quantity in components.items():
        # Hulls remain exact; module/cargo/drone alternatives can use groups.
        group, ids = (
            groups.get(type_id, (None, frozenset({type_id})))
            if type_id != fit.ship_type_id
            else (None, frozenset({type_id}))
        )
        key = ("group", group.pk) if group else ("item", type_id)
        if key in requirements:
            requirements[key].quantity += quantity
        else:
            requirements[key] = Requirement(
                key, group.name if group else names[type_id], ids, quantity, type_id
            )
    return list(requirements.values())


def tracking_queryset(location_id=None):
    fits = (
        DoctrineFitTarget.objects.select_related("fitting__ship_type")
        .prefetch_related("fitting__items__type_fk")
        .order_by("fitting__name", "pk")
    )
    qs = TrackedDoctrine.objects.select_related("doctrine", "fitting__ship_type").prefetch_related(
        Prefetch("fit_targets", queryset=fits)
    )
    return qs.filter(location_id=location_id) if location_id is not None else qs


@transaction.atomic
def sync_doctrine_tracking(location_id=None):
    """Reconcile current fits, preserving manual rules and explicit group members."""
    locations = TrackedLocation.objects.select_for_update().all()
    if location_id is not None:
        locations = locations.filter(pk=location_id)
    for location in locations:
        plans = list(
            TrackedDoctrine.objects.filter(location=location)
            .select_related("doctrine", "fitting")
            .prefetch_related("doctrine__fittings", "fit_targets")
        )
        for plan in plans:
            fits = list(plan.doctrine.fittings.all()) if plan.doctrine_id else [plan.fitting]
            fit_ids = {fit.pk for fit in fits}
            existing_ids = {target.fitting_id for target in plan.fit_targets.all()}
            plan.fit_targets.exclude(fitting_id__in=fit_ids).delete()
            DoctrineFitTarget.objects.bulk_create(
                [
                    DoctrineFitTarget(
                        tracking=plan, fitting=fit, desired_quantity=plan.default_quantity
                    )
                    for fit in fits
                    if fit.pk not in existing_ids
                ]
            )
        groups = location_groups(location.pk)
        quantities, group_quantities = Counter(), Counter()
        sources = defaultdict(set)
        for plan in tracking_queryset(location.pk):
            for target in plan.fit_targets.all():
                if not target.desired_quantity:
                    continue
                for requirement in fit_requirements(target.fitting, groups):
                    amount = requirement.quantity * target.desired_quantity
                    if requirement.key[0] == "group":
                        group_quantities[requirement.key[1]] += amount
                    else:
                        quantities[requirement.original_id] += amount
                    for type_id in requirement.type_ids:
                        sources[type_id].add(target.pk)
        ensure_local_types(sources.keys())
        current = {row.item_id: row for row in TrackedItem.objects.filter(location=location)}
        TrackedItem.objects.bulk_create(
            [
                TrackedItem(location=location, item_id=type_id, manually_tracked=False)
                for type_id in sources
                if type_id not in current
            ],
            ignore_conflicts=True,
        )
        current = {row.item_id: row for row in TrackedItem.objects.filter(location=location)}
        through = TrackedItem.doctrine_fits.through
        through.objects.filter(trackeditem__location=location).delete()
        links = [
            through(trackeditem_id=current[type_id].pk, doctrinefittarget_id=target_id)
            for type_id, targets in sources.items()
            for target_id in targets
        ]
        through.objects.bulk_create(links, ignore_conflicts=True)
        updates = []
        for type_id, row in current.items():
            if row.doctrine_quantity != quantities[type_id]:
                row.doctrine_quantity = quantities[type_id]
                updates.append(row)
        TrackedItem.objects.bulk_update(updates, ["doctrine_quantity"])
        group_updates = []
        seen_groups = set()
        for group, _ in groups.values():
            if group.pk not in seen_groups:
                seen_groups.add(group.pk)
                if group.doctrine_quantity != group_quantities[group.pk]:
                    group.doctrine_quantity = group_quantities[group.pk]
                    group_updates.append(group)
        # Groups shadowed by more specific groups still need stale targets reset.
        TrackedItemGroup.objects.filter(location=location).exclude(pk__in=group_quantities).update(
            doctrine_quantity=0
        )
        TrackedItemGroup.objects.bulk_update(group_updates, ["doctrine_quantity"])
        if location.remove_unused_doctrine_items:
            TrackedItem.objects.filter(
                location=location, manually_tracked=False, doctrine_fits__isnull=True
            ).exclude(item__market_tracking_groups__location=location).delete()


def sell_orders(location_id):
    return list(
        MarketOrderSnapshot.objects.filter(
            tracked_item__location_id=location_id,
            is_buy_order=False,
            volume_remain__gt=0,
            price__gt=0,
        )
        .order_by("price", "order_id")
        .values_list("tracked_item__item_id", "volume_remain", "price")
    )


def allocate(requirements, orders, amount=1):
    """Min-cost max flow. Shared/overlapping alternatives cannot spend stock twice.

    Augment whole order stacks, so runtime depends on rows, never unit quantities.
    Returns fulfilled units, cost, selected type quantities, and unmet line amounts.
    """
    graph = defaultdict(list)

    def edge(start, end, capacity, cost=Decimal(0)):
        forward = [end, len(graph[end]), capacity, cost, capacity]
        backward = [start, len(graph[start]), 0, -cost, 0]
        graph[start].append(forward)
        graph[end].append(backward)
        return forward

    source, sink = ("source",), ("sink",)
    selected_edges, demand_edges = [], []
    eligible = (
        set().union(*(requirement.type_ids for requirement in requirements))
        if requirements
        else set()
    )
    # Combine identical price tiers, keeping graph size bounded by order data.
    tiers = Counter()
    demand_caps = Counter()
    for requirement in requirements:
        for type_id in requirement.type_ids:
            demand_caps[type_id] += requirement.quantity * amount
    for type_id, quantity, price in orders:
        if type_id in eligible and quantity > 0:
            tiers[type_id, Decimal(str(price))] += quantity
    admitted = Counter()
    for index, ((type_id, price), quantity) in enumerate(
        sorted(tiers.items(), key=lambda row: (row[0][1], row[0][0]))
    ):
        # Expensive stacks beyond the entire possible demand for this type
        # cannot improve any allocation. Avoid putting them in the graph.
        quantity = min(quantity, demand_caps[type_id] - admitted[type_id])
        if quantity <= 0:
            continue
        admitted[type_id] += quantity
        order_node, type_node = ("order", index), ("type", type_id)
        selected_edges.append((type_id, edge(source, order_node, quantity, price)))
        edge(order_node, type_node, quantity)
    demand = 0
    for index, requirement in enumerate(requirements):
        quantity = requirement.quantity * amount
        demand += quantity
        node = ("requirement", index)
        demand_edges.append(edge(node, sink, quantity))
        for type_id in requirement.type_ids:
            edge(("type", type_id), node, quantity)
    flow, cost = 0, Decimal(0)
    while flow < demand:
        distance, previous = {source: Decimal(0)}, {}
        queue, queued = deque([source]), {source}
        while queue:
            node = queue.popleft()
            queued.remove(node)
            for index, link in enumerate(graph[node]):
                end, _, capacity, price, _ = link
                if capacity and (end not in distance or distance[node] + price < distance[end]):
                    distance[end] = distance[node] + price
                    previous[end] = (node, index)
                    if end not in queued:
                        queue.append(end)
                        queued.add(end)
        if sink not in previous:
            break
        volume, node = demand - flow, sink
        while node != source:
            start, index = previous[node]
            volume = min(volume, graph[start][index][2])
            node = start
        node = sink
        while node != source:
            start, index = previous[node]
            link = graph[start][index]
            link[2] -= volume
            graph[node][link[1]][2] += volume
            node = start
        flow += volume
        cost += volume * distance[sink]
    selected = Counter()
    for type_id, link in selected_edges:
        selected[type_id] += link[4] - link[2]
    return flow, cost, +selected, [link[2] for link in demand_edges]


def available_fits(requirements, orders):
    if not requirements:
        return 0
    stock = Counter()
    for type_id, quantity, _ in orders:
        stock[type_id] += quantity
    capacity_orders = [(type_id, quantity, 0) for type_id, quantity in stock.items()]
    high = min(
        sum(stock[type_id] for type_id in req.type_ids) // req.quantity for req in requirements
    )
    low = 0
    units = sum(req.quantity for req in requirements)
    while low < high:
        middle = (low + high + 1) // 2
        if allocate(requirements, capacity_orders, middle)[0] == units * middle:
            low = middle
        else:
            high = middle - 1
    return low


def doctrine_stock(plan, groups, orders):
    rows = []
    stock = Counter()
    for type_id, quantity, _ in orders:
        stock[type_id] += quantity
    for target in plan.fit_targets.all():
        if not target.desired_quantity:
            continue
        requirements = fit_requirements(target.fitting, groups)
        count = available_fits(requirements, orders)
        _, cost, _, missing_one = allocate(requirements, orders)
        bottlenecks = []
        for req in requirements:
            available = sum(stock[type_id] for type_id in req.type_ids)
            needed = req.quantity * target.desired_quantity
            bottlenecks.append(
                {
                    "name": req.name,
                    "stock": available,
                    "required": needed,
                    "fits": available // req.quantity,
                    "need": max(needed - available, 0),
                }
            )
        bottlenecks.sort(key=lambda row: (row["fits"], row["name"]))
        rows.append(
            {
                "target": target,
                "available": count,
                "price": cost if not any(missing_one) else None,
                "bottlenecks": bottlenecks,
                "progress": min(count * 100 // target.desired_quantity, 100),
                "stock_state": _stock_state(count * 100 // target.desired_quantity),
                "missing_components": sum(line["need"] > 0 for line in bottlenecks),
            }
        )
    # Summarize the independent per-fit counts, weighted by their ship targets.
    # Surplus for one fit cannot offset another fit's shortage.
    target_ships = sum(row["target"].desired_quantity for row in rows)
    covered_ships = sum(min(row["available"], row["target"].desired_quantity) for row in rows)
    coverage = covered_ships * 100 // target_ships if target_ships else 100
    return {
        "plan": plan,
        "ships": rows,
        "coverage": coverage,
        "stock_state": _stock_state(coverage),
        "ready_fits": sum(row["available"] >= row["target"].desired_quantity for row in rows),
        "target_ships": target_ships,
    }


def _stock_state(percentage):
    if percentage >= 100:
        return "ready"
    return "critical" if percentage <= 25 else "low"


def multibuy(target, groups, orders, *, missing=False, amount=1):
    requirements = fit_requirements(target.fitting, groups)
    amount = target.desired_quantity if missing else amount
    _, _, selected, unmet = allocate(requirements, orders, amount)
    # Missing buys only the deficit to the seeding target; all buys a full fit.
    quantities = Counter() if missing else selected
    prices = {}
    for type_id, _, price in orders:
        prices[type_id] = min(prices.get(type_id, Decimal("Infinity")), Decimal(str(price)))
    unpriced = False
    for req, deficit in zip(requirements, unmet, strict=True):
        if deficit:
            chosen = min(
                req.type_ids,
                key=lambda tid: (prices.get(tid, Decimal("Infinity")), tid != req.original_id, tid),
            )
            quantities[chosen] += deficit
            unpriced |= chosen not in prices
    return quantities, unpriced
