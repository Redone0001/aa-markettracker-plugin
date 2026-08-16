import json
import logging
import random
import traceback

from celery import shared_task
from django.core.cache import cache
from django.db import connection
from django.db.models import Q
from django.utils import timezone as _tz
from django.utils.dateparse import parse_datetime
from esi.errors import TokenInvalidError
from esi.exceptions import (
    ESIBucketLimitException,
    ESIErrorLimitException,
    HTTPClientError,
    HTTPNotModified,
    HTTPServerError,
)
from esi.models import Token
from eveuniverse.models import EveType

from .discord import (
    contracts_restocked_alert,
    items_restocked_alert,
    send_contracts_alert,
    send_items_alert,
)
from .models import (
    ContractDelivery,
    ContractSnapshot,
    Delivery,
    MarketCharacter,
    MarketOrderSnapshot,
    MarketTrackingConfig,
    TrackedContract,
    TrackedItem,
)
from .providers import (
    get_character_contract_items,
    get_character_contracts,
    get_character_orders,
    get_region_orders,
    get_structure_orders,
)
from .utils import (
    _ctx,
    _location_name,
    _parse_esi_datetime,
    _task_suffix,
    contract_matches,
    db_log,
    fetch_contract_items,
    location_display_name,
)

logger = logging.getLogger(__name__)

MARKET_ORDERS_TABLE = MarketOrderSnapshot._meta.db_table
CONTRACTS_TABLE = ContractSnapshot._meta.db_table


# ========== DISPATCH TASKS ==========

@shared_task
def fetch_market_data_auto():
    """Dispatcher used by existing installations (keeps old task name).

    NEW:
    - schedules one task per active TrackedLocation (region/structure)
    - uses ONLY the admin MarketCharacter
    """
    mc = (
        MarketCharacter.objects
        .select_related("character", "token")
        .filter(type="admin")
        .first()
    )
    if not mc:
        logger.warning("[MarketTracker] No admin MarketCharacter found for auto refresh.")
        return

    lock_key = "mt:lock:fetch_market_data_auto"
    if cache.add(lock_key, "1", timeout=60 * 5) is False:
        logger.info("[MarketTracker] fetch_market_data_auto already running; skipping.")
        return

    try:
        from .models import TrackedLocation

        locations = list(TrackedLocation.objects.filter(is_active=True).order_by("-is_default", "id"))
        if not locations:
            logger.warning("[MarketTracker] No active TrackedLocation configured.")
            return

        spread = 2
        jitter = 3
        delay = 0

        for loc in locations:
            fetch_market_data_for_location.apply_async(
                args=[mc.character.character_id, int(loc.id)],
                countdown=delay + random.randint(0, jitter),
                priority=5,
            )
            delay += spread

    finally:
        cache.delete(lock_key)





@shared_task
def refresh_contracts():
    """Dispatcher (keeps old task name for existing installations)."""
    db_log(source="contracts", event="start", message="refresh_contracts dispatch", data=_ctx())

    lock_key = "mt:lock:refresh_contracts"
    if cache.add(lock_key, "1", timeout=60 * 10) is False:
        db_log(source="contracts", event="locked", message="refresh_contracts already running; skip", data=_ctx())
        return

    try:
        scope = "esi-contracts.read_character_contracts.v1"
        qs = (
            MarketCharacter.objects
            .select_related("token")
            .filter(token__scopes__name=scope)
            .distinct()
        )

        char_ids = [int(x) for x in qs.values_list("token__character_id", flat=True)]
        run_id = _task_suffix()

        db_log(
            source="contracts",
            event="dispatch_plan",
            data=_ctx({
                "run_id": run_id,
                "qs_count": len(char_ids),
                "char_ids": char_ids[:50],
            }),
        )

        if not char_ids:
            db_log(source="contracts", event="dispatch_empty", data=_ctx({"run_id": run_id}))
            return

        # ---- init barrier counter (MUST exist before first decr) ----
        barrier_left_key = f"mt:barrier:contracts:{run_id}:left"
        cache.set(barrier_left_key, len(char_ids), timeout=60 * 15)

        spread = 2
        jitter = 3
        delay = 0
        dispatched = 0

        for char_id in char_ids:
            refresh_contracts_for_character.apply_async(
                args=[char_id],
                kwargs={"force_refresh": False, "run_id": run_id},
                countdown=delay + random.randint(0, jitter),
                priority=5,
            )
            dispatched += 1
            delay += spread

        db_log(
            source="contracts",
            event="dispatch_done",
            data=_ctx({"run_id": run_id, "dispatched": dispatched}),
        )

    finally:
        cache.delete(lock_key)
        db_log(source="contracts", event="end", message="refresh_contracts dispatch end", data=_ctx())





# ========== MARKET (ITEMS) ==========

@shared_task
def fetch_market_data_for_location(character_id: int, location_pk: int):
    """
    Fetch market orders for ONE TrackedLocation (region/structure) and refresh snapshots for items in that location.
    This is safe for multi-location dispatch: we replace ONLY rows for items belonging to that location.
    """
    from .models import TrackedLocation  # local import for migrations safety

    ctx_base = {"character_id": int(character_id), "location_pk": int(location_pk)}

    loc = TrackedLocation.objects.filter(pk=location_pk, is_active=True).first()
    if not loc:
        db_log(level="WARN", source="items", event="location_missing", data=_ctx(ctx_base))
        return

    # global thresholds
    cfg = MarketTrackingConfig.objects.first()
    yellow_threshold = int(getattr(cfg, "yellow_threshold", 50) or 50)
    red_threshold = int(getattr(cfg, "red_threshold", 25) or 25)

    # token (structure needs token for /markets/structures/)
    try:
        mc = MarketCharacter.objects.get(character__character_id=character_id)
        token = mc.token
    except Exception as e:
        db_log(level="ERROR", source="items", event="token_refresh_failed", message=str(e), data=_ctx({**ctx_base}))
        return

    # per-location lock (prevents overlapping runs for same loc)
    lock_key = f"mt:lock:items:loc:{location_pk}"
    if cache.add(lock_key, "1", timeout=60 * 10) is False:
        db_log(source="items", event="locked", message="Location task already running; skipping", data=_ctx({**ctx_base}))
        return

    try:
        orig_table = MARKET_ORDERS_TABLE
        suffix = _task_suffix()
        tmp_table = f"{orig_table}_tmp_{suffix}"

        location_name = location_display_name(loc)

        db_log(
            source="items",
            event="loc_start",
            data=_ctx({
                **ctx_base,
                "scope": loc.scope,
                "location_id": int(loc.location_id),
                "tmp_table": tmp_table,
            }),
        )

        # create tmp table
        with connection.cursor() as cursor:
            cursor.execute(f"DROP TABLE IF EXISTS `{tmp_table}`;")
            cursor.execute(f"CREATE TABLE `{tmp_table}` LIKE `{orig_table}`;")

        # Import orders ONLY for items in this location
        if loc.scope == "region":
            seen_orders = _fetch_region_orders_sql_for_location(int(loc.location_id), int(loc.id), table_name=tmp_table)
        else:
            seen_orders = _fetch_structure_orders_for_location(
                int(loc.location_id), token, int(loc.id), table_name=tmp_table
            )

        if not seen_orders:
            db_log(level="WARN", source="items", event="import_empty", message="No orders imported for location", data=_ctx({**ctx_base}))
            with connection.cursor() as cursor:
                cursor.execute(f"DROP TABLE IF EXISTS `{tmp_table}`;")
            return

        # status calc (only items of this location)
        changed_statuses = []
        tracked_items = list(
            TrackedItem.objects.filter(location_id=int(loc.id)).select_related("item").all()
        )

        would_all_go_red = True
        any_desired_positive = False

        with connection.cursor() as cursor:
            for ti in tracked_items:
                cursor.execute(
                    f"SELECT COALESCE(SUM(volume_remain), 0) FROM `{tmp_table}` WHERE tracked_item_id = %s",
                    [ti.id],
                )
                total_volume = cursor.fetchone()[0] or 0

                desired = int(ti.desired_quantity or 0)
                if desired <= 0:
                    percentage = 100
                    new_status = "OK"
                else:
                    any_desired_positive = True
                    percentage = int((int(total_volume) / desired) * 100)
                    if percentage <= red_threshold:
                        new_status = "RED"
                    elif percentage <= yellow_threshold:
                        new_status = "YELLOW"
                    else:
                        new_status = "OK"

                if desired > 0:
                    if not (int(total_volume) == 0 and new_status == "RED"):
                        would_all_go_red = False

                old_status = ti.last_status or "OK"
                if new_status != old_status:
                    changed_statuses.append((ti, old_status, new_status, percentage, int(total_volume), desired))
                    ti.last_status = new_status
                    ti.save(update_fields=["last_status"])

        if any_desired_positive and would_all_go_red and changed_statuses:
            db_log(level="WARN", source="items", event="suspicious_all_zero", message="All monitored items went RED@0; rollback", data=_ctx({**ctx_base, "changed": len(changed_statuses)}))
            for (ti, old_s, _new_s, _p, _t, _d) in changed_statuses:
                TrackedItem.objects.filter(pk=ti.pk).update(last_status=old_s)

            with connection.cursor() as cursor:
                cursor.execute(f"DROP TABLE IF EXISTS `{tmp_table}`;")
            return

        # Replace ONLY rows for this location's tracked items
        tracked_ids = [ti.id for ti in tracked_items]
        if tracked_ids:
            with connection.cursor() as cursor:
                # delete existing rows for these tracked_item_ids
                # (safe even for big lists; MySQL IN size might be large, so chunk)
                CHUNK = 900
                for i in range(0, len(tracked_ids), CHUNK):
                    chunk = tracked_ids[i:i+CHUNK]
                    placeholders = ",".join(["%s"] * len(chunk))
                    cursor.execute(
                        f"DELETE FROM `{orig_table}` WHERE tracked_item_id IN ({placeholders})",
                        chunk,
                    )

                # insert fresh rows from tmp -> orig (no id column)
                cursor.execute(
                    f"""
                    INSERT INTO `{orig_table}`
                        (`order_id`, `tracked_item_id`, `structure_id`, `price`, `volume_remain`, `is_buy_order`, `issued`)
                    SELECT
                        `order_id`, `tracked_item_id`, `structure_id`, `price`, `volume_remain`, `is_buy_order`, `issued`
                    FROM `{tmp_table}`
                    """
                )

        # cleanup tmp
        with connection.cursor() as cursor:
            cursor.execute(f"DROP TABLE IF EXISTS `{tmp_table}`;")

        # alerts
        if changed_statuses:
            send_items_alert(changed_statuses, location_name)
            items_restocked_alert(changed_statuses, location_name)

        db_log(source="items", event="loc_end", data=_ctx({**ctx_base, "changed": len(changed_statuses)}))

    except Exception as e:
        db_log(level="ERROR", source="items", event="loc_exception", message=str(e), data=_ctx({**ctx_base, "traceback": traceback.format_exc()}))
        try:
            with connection.cursor() as cursor:
                cursor.execute(f"DROP TABLE IF EXISTS `{tmp_table}`;")
        except Exception:
            pass
    finally:
        cache.delete(lock_key)



@shared_task
def fetch_market_data(character_id: int):
    db_log(
        source="items",
        event="start",
        message="fetch_market_data start",
        data=_ctx({"character_id": character_id}),
    )

    config = MarketTrackingConfig.objects.first()
    if not config:
        db_log(
            level="WARN",
            source="items",
            event="no_config",
            message="No MarketTrackingConfig found",
            data=_ctx(),
        )
        return

    yellow_threshold = int(config.yellow_threshold or 50)
    red_threshold = int(config.red_threshold or 25)

    # --- token (structure mode needs token) ---
    try:
        mc = MarketCharacter.objects.get(character__character_id=character_id)
        token = mc.token
    except MarketCharacter.DoesNotExist:
        db_log(
            level="WARN",
            source="items",
            event="no_market_character",
            message=f"No MarketCharacter found for character_id={character_id}",
            data=_ctx({"character_id": character_id}),
        )
        return
    except Exception as e:
        db_log(
            level="ERROR",
            source="items",
            event="token_refresh_failed",
            message=str(e),
            data=_ctx({"character_id": character_id, "traceback": traceback.format_exc()}),
        )
        return

    orig_table = MARKET_ORDERS_TABLE
    suffix = _task_suffix()
    tmp_table = f"{orig_table}_tmp_{suffix}"
    old_table = f"{orig_table}_old_{suffix}"

    location_name = _location_name(config)

    db_log(
        source="items",
        event="tmp_plan",
        data=_ctx(
            {
                "orig_table": orig_table,
                "tmp_table": tmp_table,
                "old_table": old_table,
                "scope": config.scope,
                "location_id": config.location_id,
            }
        ),
    )

    # used for alerts
    changed_statuses: list[tuple] = []

    try:
        # --- prepare tmp table ---
        with connection.cursor() as cursor:
            cursor.execute(f"DROP TABLE IF EXISTS `{tmp_table}`;")
            cursor.execute(f"CREATE TABLE `{tmp_table}` LIKE `{orig_table}`;")

        db_log(
            source="items",
            event="tmp_ready",
            data=_ctx({"tmp_table": tmp_table, "orig_table": orig_table}),
        )

        # --- import orders into tmp table ---
        if config.scope == "region":
            seen_orders = _fetch_region_orders_sql(config.location_id, table_name=tmp_table)
        else:
            # django-esi handles authentication, caching, pagination, and rate limits.
            seen_orders = _fetch_structure_orders(
                config.location_id, token, table_name=tmp_table
            )

        db_log(
            source="items",
            event="orders_imported",
            data=_ctx(
                {
                    "seen_orders": len(seen_orders),
                    "scope": config.scope,
                    "location_id": config.location_id,
                }
            ),
        )

        if not seen_orders:
            db_log(
                level="WARN",
                source="items",
                event="import_empty_skip_swap",
                message="No orders imported, skipping atomic swap",
                data=_ctx({"tmp_table": tmp_table}),
            )
            # cleanup tmp
            with connection.cursor() as cursor:
                cursor.execute(f"DROP TABLE IF EXISTS `{tmp_table}`;")
            return

        # --- calculate statuses using tmp table (SQL SUM) ---
        tracked_items = list(TrackedItem.objects.select_related("item").all())
        tracked_count = len(tracked_items)

        db_log(
            source="items",
            event="status_calc_start",
            data=_ctx(
                {
                    "tracked_items": tracked_count,
                    "yellow_threshold": yellow_threshold,
                    "red_threshold": red_threshold,
                    "tmp_table": tmp_table,
                }
            ),
        )

        # also count how many items would be RED with total_volume=0
        # to detect a suspicious "all zero"
        would_all_go_red = True
        any_desired_positive = False

        with connection.cursor() as cursor:
            for ti in tracked_items:
                cursor.execute(
                    f"SELECT COALESCE(SUM(volume_remain), 0) "
                    f"FROM `{tmp_table}` WHERE tracked_item_id = %s",
                    [ti.id],
                )
                total_volume = cursor.fetchone()[0] or 0

                desired = int(ti.desired_quantity or 0)
                if desired <= 0:
                    # if desired=0 treat as OK (we do not monitor quantity)
                    percentage = 100
                    new_status = "OK"
                else:
                    any_desired_positive = True
                    percentage = int((int(total_volume) / desired) * 100)
                    if percentage <= red_threshold:
                        new_status = "RED"
                    elif percentage <= yellow_threshold:
                        new_status = "YELLOW"
                    else:
                        new_status = "OK"

                # suspicious check
                if desired > 0:
                    # detect "all-zero => RED" when desired>0 and total_volume == 0 with status RED
                    if not (int(total_volume) == 0 and new_status == "RED"):
                        would_all_go_red = False

                old_status = ti.last_status or "OK"
                if new_status != old_status:
                    # tuple shape matches discord.py:
                    # (i, old_s, new_s, p, t, d)
                    changed_statuses.append((ti, old_status, new_status, percentage, int(total_volume), desired))
                    ti.last_status = new_status
                    ti.save(update_fields=["last_status"])

        db_log(
            source="items",
            event="status_calc_done",
            data=_ctx({"tracked_items": tracked_count, "changed": len(changed_statuses)}),
        )

        # --- suspicious "all zero" protection  ---
        # If ALL monitored items (desired>0) end up RED because total_volume=0,
        # it almost always means: ESI failure / missing permissions / empty response / 403.
        if any_desired_positive and would_all_go_red and changed_statuses:
            db_log(
                level="WARN",
                source="items",
                event="suspicious_all_zero",
                message="Suspicious all-zero snapshot detected; skipping swap+alerts and rolling back statuses",
                data=_ctx({"changed": len(changed_statuses), "tmp_table": tmp_table}),
            )

            # rollback statuses
            for (ti, old_s, _new_s, _p, _t, _d) in changed_statuses:
                TrackedItem.objects.filter(pk=ti.pk).update(last_status=old_s)

            # cleanup tmp
            with connection.cursor() as cursor:
                cursor.execute(f"DROP TABLE IF EXISTS `{tmp_table}`;")

            return

        # --- atomic swap FIRST (so deliveries/alerts align with the new snapshot) ---
        with connection.cursor() as cursor:
            cursor.execute(f"DROP TABLE IF EXISTS `{old_table}`;")
            cursor.execute(
                f"RENAME TABLE `{orig_table}` TO `{old_table}`, `{tmp_table}` TO `{orig_table}`;"
            )
            cursor.execute(f"DROP TABLE IF EXISTS `{old_table}`;")

        db_log(
            source="items",
            event="swap_done",
            message="Atomic swap done",
            data=_ctx({"orig_table": orig_table, "tmp_table": tmp_table, "old_table": old_table}),
        )

        # --- deliveries / alerts AFTER swap ---
        if Delivery.objects.filter(status="PENDING").exists():
            _update_deliveries(config)


        if changed_statuses:
            send_items_alert(changed_statuses, location_name)
            items_restocked_alert(changed_statuses, location_name)

        if not changed_statuses:
            db_log(
                source="items",
                event="no_changes",
                message="No items status changes detected",
                data={
                    "tracked_count": tracked_count,
                    "snapshot_count": len(seen_orders),
                },
            )


    except Exception as e:
        db_log(
            level="ERROR",
            source="items",
            event="exception",
            message=str(e),
            data=_ctx(
                {
                    "traceback": traceback.format_exc(),
                    "tmp_table": tmp_table,
                    "orig_table": orig_table,
                }
            ),
        )
        # cleanup best-effort
        try:
            with connection.cursor() as cursor:
                cursor.execute(f"DROP TABLE IF EXISTS `{tmp_table}`;")
        except Exception:
            pass
        return

    finally:
        db_log(source="items", event="end", message="fetch_market_data end", data=_ctx())




def _fetch_region_orders_sql(region_id: int, *, table_name: str) -> set[int]:
    seen_orders: set[int] = set()
    tracked_items = list(TrackedItem.objects.select_related("item").all())

    for tracked in tracked_items:
        type_id = tracked.item.id
        data = get_region_orders(region_id, type_id, order_type="sell")
        sell_orders = [
            order
            for order in data
            if isinstance(order, dict) and not order.get("is_buy_order")
        ]
        seen_orders.update(_save_orders_sql(table_name, sell_orders, tracked, region_id))

    return seen_orders



def _fetch_structure_orders(structure_id: int, token: Token, table_name: str) -> set[int]:
    tracked_map = {
        int(t.item_id): t
        for t in TrackedItem.objects.select_related("item").all()
    }

    seen_orders: set[int] = set()
    orders_by_tracked: dict[int, list[dict]] = {}
    for order in get_structure_orders(structure_id, token):
        if not isinstance(order, dict) or order.get("is_buy_order"):
            continue

        type_id = order.get("type_id")
        if not type_id:
            continue

        tracked = tracked_map.get(int(type_id))
        if tracked:
            orders_by_tracked.setdefault(tracked.pk, []).append(order)

    for orders in orders_by_tracked.values():
        tracked = tracked_map[int(orders[0]["type_id"])]
        seen_orders.update(_save_orders_sql(table_name, orders, tracked, structure_id))

    return seen_orders


def _fetch_region_orders_sql_for_location(region_id: int, location_pk: int, *, table_name: str) -> set[int]:
    seen_orders: set[int] = set()
    tracked_items = list(
        TrackedItem.objects.filter(location_id=int(location_pk)).select_related("item").all()
    )

    for tracked in tracked_items:
        type_id = tracked.item.id
        data = get_region_orders(region_id, type_id, order_type="sell")
        sell_orders = [
            order
            for order in data
            if isinstance(order, dict) and not order.get("is_buy_order")
        ]
        seen_orders.update(_save_orders_sql(table_name, sell_orders, tracked, region_id))

    return seen_orders


def _fetch_structure_orders_for_location(
    structure_id: int, token: Token, location_pk: int, *, table_name: str
) -> set[int]:
    tracked_map = {
        int(t.item_id): t
        for t in TrackedItem.objects.filter(location_id=int(location_pk)).select_related("item").all()
    }

    seen_orders: set[int] = set()
    orders_by_tracked: dict[int, list[dict]] = {}
    for order in get_structure_orders(structure_id, token):
        if not isinstance(order, dict) or order.get("is_buy_order"):
            continue
        type_id = order.get("type_id")
        if not type_id:
            continue

        tracked = tracked_map.get(int(type_id))
        if tracked:
            orders_by_tracked.setdefault(tracked.pk, []).append(order)

    for orders in orders_by_tracked.values():
        tracked = tracked_map[int(orders[0]["type_id"])]
        seen_orders.update(_save_orders_sql(table_name, orders, tracked, structure_id))

    return seen_orders


def _save_orders_sql(
    table_name: str,
    orders: list[dict],
    tracked_item: TrackedItem,
    location_id: int,
) -> set[int]:
    if not orders:
        return set()

    now = _tz.now()
    rows = []
    order_ids = []

    for o in orders:
        if not isinstance(o, dict):
            continue
        oid = o.get("order_id")
        if not oid:
            continue

        oid = int(oid)
        order_ids.append(oid)

        issued = _parse_esi_datetime(o.get("issued")) or now


        rows.append((
            oid,                    # order_id
            tracked_item.id,        # tracked_item_id
            int(location_id),       # structure_id
            float(o.get("price") or 0),
            int(o.get("volume_remain") or 0),
            bool(o.get("is_buy_order", False)),
            issued,
        ))

    if not rows:
        return set()

    # WARNING: columns must match your table
    sql = f"""
        INSERT INTO `{table_name}`
            (`order_id`, `tracked_item_id`, `structure_id`, `price`, `volume_remain`, `is_buy_order`, `issued`)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            `tracked_item_id` = VALUES(`tracked_item_id`),
            `structure_id`    = VALUES(`structure_id`),
            `price`           = VALUES(`price`),
            `volume_remain`   = VALUES(`volume_remain`),
            `is_buy_order`    = VALUES(`is_buy_order`),
            `issued`          = VALUES(`issued`)
    """

    with connection.cursor() as cursor:
        cursor.executemany(sql, rows)

    return set(order_ids)


def _update_deliveries(config):
    """
    Update Delivery rows based on CURRENT market sell orders (volume_remain).
    Optimization:
    - If no PENDING deliveries -> do nothing (no token refresh).
    - For each delivery:
        * if delivery.character is set -> use ONLY that character's token(s)
        * else -> use tokens of the delivery.user (MarketCharacter only), not everyone
    """
    pending_qs = Delivery.objects.filter(status="PENDING").select_related("user", "character", "item")
    if not pending_qs.exists():
        logger.debug("[MarketTracker] No PENDING deliveries; skipping deliveries update.")
        return

    deliveries = list(pending_qs)
    mc_tokens_qs = (
        Token.objects.filter(
            marketcharacter__isnull=False,
            scopes__name="esi-markets.read_character_orders.v1",
        )
        .select_related()  # ok; token model
        .distinct()
    )
    all_tokens = list(mc_tokens_qs)

    if not all_tokens:
        logger.warning("[MarketTracker] No MarketTracker tokens with esi-markets.read_character_orders.v1; cannot update deliveries.")
        return


    def _tokens_for_delivery(d: Delivery) -> list[Token]:
        if d.character_id:
            char_id = getattr(d.character, "character_id", None)
            if not char_id:
                pass
            else:
                return [t for t in all_tokens if int(t.character_id) == int(char_id)]

        user_id = d.user_id
        user_char_ids = set(
            MarketCharacter.objects.filter(character__user_id=user_id)
            .values_list("token__character_id", flat=True)
        )
        if not user_char_ids:
            return []
        return [t for t in all_tokens if int(t.character_id) in user_char_ids]

    for d in deliveries:
        tokens = _tokens_for_delivery(d)
        if not tokens:
            logger.debug(
                "[MarketTracker] No usable tokens for delivery id=%s user_id=%s character_id=%s",
                d.id, d.user_id, d.character_id
            )
            continue

        total_delivered = 0
        for token in tokens:
            try:
                orders = get_character_orders(token.character_id, token)
                if config.scope == "region":
                    orders = [
                        order
                        for order in orders
                        if order.get("region_id") == config.location_id
                    ]
                else:
                    orders = [
                        order
                        for order in orders
                        if order.get("location_id") == config.location_id
                    ]

                delivered_from_orders = 0
                for o in orders:
                    # required keys
                    if "type_id" not in o or "issued" not in o:
                        continue
                    if int(o.get("type_id") or 0) != int(d.item_id):
                        continue
                    if o.get("is_buy_order", False):
                        continue

                    issued_dt = parse_datetime(o["issued"])
                    if issued_dt:
                        issued_dt = issued_dt.astimezone(_tz.utc)

                    # Only orders after declaring delivery
                    if issued_dt and issued_dt >= d.created_at:
                        delivered_from_orders += int(o.get("volume_remain", 0) or 0)

                total_delivered += delivered_from_orders

            except TokenInvalidError:
                logger.warning(
                    "[MarketTracker] Skipping invalid token for char %s (id=%s)",
                    token.character_id,
                    token.id,
                )
            except Exception:
                logger.exception("[MarketTracker] Orders fetch failed for char %s (delivery id=%s)", token.character_id, d.id)

        new_delivered = min(int(total_delivered), int(d.declared_quantity))
        new_status = "FINISHED" if new_delivered >= int(d.declared_quantity) else "PENDING"

        if new_delivered != d.delivered_quantity or new_status != d.status:
            d.delivered_quantity = new_delivered
            d.status = new_status
            d.save(update_fields=["delivered_quantity", "status", "updated_at"])



# ========== CONTRACTS ==========

@shared_task(bind=True, max_retries=5)
def refresh_contracts_for_character(
    self,
    character_id: int,
    force_refresh: bool = False,
    run_id: str | None = None,
):
    """
    Refresh the contract list for a single character.
    Behavior:
    - 304: do NOT modify snapshots list, BUT schedule items backfill if missing.
    - 200: snapshot list is authoritative for OUTSTANDING contracts for this character.
           We upsert seen ones and cleanup stale ones (were in DB but not returned).
    Barrier-based: last finished character triggers recalc.
    """
    ctx = {"character_id": int(character_id), "run_id": run_id}
    not_modified = False
    seen_outstanding_ids: set[int] = set()
    seen_outstanding_count = 0

    try:
        try:
            mc = MarketCharacter.objects.select_related("token").get(
                token__character_id=character_id
            )
        except MarketCharacter.DoesNotExist:
            db_log(source="contracts", event="no_marketcharacter", data=_ctx(ctx))
            return

        token = mc.token
        now = _tz.now()
        try:
            data = get_character_contracts(
                character_id, token, force_refresh=force_refresh
            )
        except HTTPNotModified:
            data = []
            not_modified = True
            db_log(source="contracts", event="not_modified", data=_ctx(ctx))
        except TokenInvalidError:
            db_log(level="WARN", source="contracts", event="token_invalid", data=_ctx(ctx))
            return
        except (ESIErrorLimitException, ESIBucketLimitException) as exc:
            wait_s = max(1, int(getattr(exc, "reset", 60) or 60))
            raise self.retry(countdown=wait_s, exc=exc) from exc
        except HTTPClientError as exc:
            if exc.status_code == 403:
                db_log(source="contracts", event="forbidden", data=_ctx(ctx))
                return
            if exc.status_code == 404:
                db_log(source="contracts", event="not_found", data=_ctx(ctx))
                return
            raise
        except HTTPServerError as exc:
            raise self.retry(countdown=60, exc=exc) from exc

        # django-esi's results() has already fetched and validated every page.
        for contract_data in data:
            if (contract_data.get("status") or "").lower() != "outstanding":
                continue

            contract_id = contract_data.get("contract_id")
            if not contract_id:
                continue
            contract_id = int(contract_id)

            seen_outstanding_ids.add(contract_id)
            seen_outstanding_count += 1

            ContractSnapshot.objects.update_or_create(
                contract_id=contract_id,
                defaults={
                    "owner_character_id": int(character_id),
                    "type": contract_data.get("type") or "",
                    "availability": contract_data.get("availability") or "",
                    "status": contract_data.get("status") or "",
                    "title": contract_data.get("title") or "",
                    "date_issued": _parse_esi_datetime(contract_data.get("date_issued")),
                    "date_expired": _parse_esi_datetime(contract_data.get("date_expired")),
                    "start_location_id": contract_data.get("start_location_id"),
                    "end_location_id": contract_data.get("end_location_id"),
                    "price": contract_data.get("price") or 0,
                    "reward": contract_data.get("reward") or 0,
                    "collateral": contract_data.get("collateral") or 0,
                    "volume": contract_data.get("volume") or 0,
                    "for_corporation": bool(
                        contract_data.get("for_corporation") or False
                    ),
                    "assignee_id": contract_data.get("assignee_id"),
                    "acceptor_id": contract_data.get("acceptor_id"),
                    "issuer_id": contract_data.get("issuer_id"),
                    "issuer_corporation_id": contract_data.get(
                        "issuer_corporation_id"
                    ),
                    "date_completed": _parse_esi_datetime(
                        contract_data.get("date_completed")
                    ),
                    "fetched_at": now,
                },
            )

        db_log(
            source="contracts",
            event="fetched",
            data=_ctx({**ctx, "contracts_seen": int(seen_outstanding_count), "not_modified": bool(not_modified)}),
        )

        # ---- cleanup only if we got an authoritative list (i.e. NOT 304) ----
        # We only cleanup outstanding snapshots for THIS owner_character_id.
        if not not_modified:
            # All outstanding in DB for this owner
            db_ids = set(
                ContractSnapshot.objects.filter(
                    owner_character_id=int(character_id),
                    status__iexact="outstanding",
                ).values_list("contract_id", flat=True)
            )

            stale = db_ids - seen_outstanding_ids

            if stale:
                # safest: delete stale outstanding snapshots for that owner
                # (alternatively: mark as completed/cancelled if you prefer)
                ContractSnapshot.objects.filter(
                    owner_character_id=int(character_id),
                    contract_id__in=list(stale),
                ).delete()

            db_log(
                source="contracts",
                event="cleanup_done",
                data=_ctx({**ctx, "db_outstanding": len(db_ids), "esi_outstanding": len(seen_outstanding_ids), "stale_deleted": len(stale)}),
            )

        # ---- items backfill (runs even on 304) ----
        # Option A (recommended): only title-matched doctrine needles to avoid big ESI burst
        needles = get_doctrine_needles()
        qs = ContractSnapshot.objects.filter(
            owner_character_id=int(character_id),
            status__iexact="outstanding",
            type__iexact="item_exchange",
        )

        if needles:
            title_q = Q()
            for n in needles[:100]:  # hard cap defensive
                title_q |= Q(title__icontains=n)
            qs = qs.filter(title_q)

        # missing items detection (supports JSONField or TextField variants)
        qs = qs.filter(
            Q(items__isnull=True) |
            Q(items=[]) |
            Q(items="[]") |
            Q(items="")
        ).values_list("contract_id", flat=True)[:200]

        missing_ids = list(qs)
        for cid in missing_ids:
            refresh_contract_items_for_contract.apply_async(
                args=[int(character_id), int(cid)],
                countdown=random.randint(0, 5),
                priority=5,
            )

        if missing_ids:
            db_log(
                source="contracts",
                event="items_backfill_scheduled",
                data=_ctx({**ctx, "count": len(missing_ids), "not_modified": bool(not_modified)}),
            )

    finally:
        if run_id:
            key = f"mt:barrier:contracts:{run_id}:left"
            try:
                left = cache.decr(key)
            except Exception:
                left = None

            db_log(
                source="contracts",
                event="barrier_tick",
                data=_ctx({**ctx, "left": left}),
            )

            if left == 0:
                lock = f"mt:lock:recalc_contracts:{run_id}"
                if cache.add(lock, "1", timeout=60 * 10):
                    db_log(
                        source="contracts",
                        event="barrier_release",
                        data=_ctx({**ctx, "action": "recalc"}),
                    )
                    recalc_contract_statuses.apply_async(priority=6)



@shared_task(bind=True, max_retries=5)
def refresh_contract_items_for_contract(self, character_id: int, contract_id: int):
    """Fetch and store items for a specific contract (single ESI call)."""
    ctx = {"character_id": int(character_id), "contract_id": int(contract_id)}
    try:
        mc = MarketCharacter.objects.select_related("token").get(token__character_id=character_id)
    except MarketCharacter.DoesNotExist:
        return

    token = mc.token
    try:
        items = get_character_contract_items(character_id, contract_id, token)
    except TokenInvalidError:
        return
    except (ESIErrorLimitException, ESIBucketLimitException) as exc:
        wait_s = max(1, int(getattr(exc, "reset", 60) or 60))
        raise self.retry(countdown=wait_s, exc=exc) from exc
    except HTTPClientError as exc:
        if exc.status_code == 403:
            db_log(source="contracts", event="items_403", data=_ctx(ctx))
            ContractSnapshot.objects.filter(contract_id=contract_id).update(items=[])
            return
        if exc.status_code == 404:
            ContractSnapshot.objects.filter(contract_id=contract_id).update(items=[])
            return
        raise
    except HTTPServerError as exc:
        raise self.retry(countdown=60, exc=exc) from exc

    ContractSnapshot.objects.filter(contract_id=contract_id).update(items=items)
    db_log(source="contracts", event="items_ok", data=_ctx({**ctx, "count": len(items)}))


@shared_task
def recalc_contract_statuses():
    """Recalculate contract statuses per-location + deliveries from DB only (no ESI calls)."""
    all_contracts = list(ContractSnapshot.objects.filter(status__iexact="outstanding"))
    cfg = MarketTrackingConfig.objects.first()
    yellow = int(getattr(cfg, "yellow_threshold", 50) or 50)
    red = int(getattr(cfg, "red_threshold", 25) or 25)

    summary = _recalculate_contract_statuses_and_alert_per_location(
        all_contracts,
        yellow,
        red,
        allow_esi_fetch=False
    )
    db_log(source="contracts", event="recalc_done", data=_ctx(summary or {}))


def get_doctrine_needles() -> list[str]:
    needles: set[str] = set()
    qs = TrackedContract.objects.filter(
        mode=TrackedContract.Mode.DOCTRINE,
        is_active=True,
    ).select_related("fitting")

    for tc in qs:
        needle = doctrine_needle_for_tc(tc)
        if needle:
            needles.add(needle.lower())

    return sorted(needles)



def doctrine_needle_for_tc(tc) -> str:
    """
    Returns doctrine needle used for title prefilter.
    Priority:
    1) tc.title_filter (manual)
    2) ship hull name from fitting.ship_type_id (EveType)
    3) fallback: fitting.name
    """
    needle = (tc.title_filter or "").strip()
    if needle:
        return needle

    fit = getattr(tc, "fitting", None)
    ship_type_id = getattr(fit, "ship_type_id", None) if fit else None
    if ship_type_id:
        try:
            return (EveType.objects.get(id=int(ship_type_id)).name or "").strip()
        except Exception:
            pass

    if fit:
        return (fit.name or "").strip()

    return ""


def _recalculate_contract_statuses_and_alert(all_contracts, yellow, red, allow_esi_fetch: bool = True):
    """
    Recalculate status of tracked contracts.

    Important behavior (NEW):
    - CUSTOM: works purely on titles/fields, no ESI.
    - DOCTRINE:
        * If items are missing AND allow_esi_fetch=False -> we DO NOT change status for this tracked contract.
          (Because we cannot reliably evaluate doctrine matching without items.)
        * If allow_esi_fetch=True, we may fetch items via ESI as a fallback (but you usually run with False).

    Returns a summary dict.
    """
    tracked_qs = TrackedContract.objects.select_related("fitting").filter(is_active=True)
    tracked_count = tracked_qs.count()
    snapshot_count = len(all_contracts)

    db_log(
        source="contracts",
        event="recalc_start",
        data=_ctx({
            "tracked_count": tracked_count,
            "snapshot_count": snapshot_count,
            "allow_esi_fetch": bool(allow_esi_fetch),
            "yellow": int(yellow),
            "red": int(red),
        }),
    )

    # Pre-filter: only doctrine-relevant contracts
    doctrine_contracts = [
        c for c in all_contracts
        if (getattr(c, "type", "") or "").lower() == "item_exchange"
        and (getattr(c, "status", "") or "").lower() == "outstanding"
    ]

    # ---- small helpers ----
    def _items_list(value):
        if not value:
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, list) else []
            except Exception:
                return []
        return []

    # cache per run to avoid refetch within a single recalc
    items_cache: dict[tuple[int, int], list] = {}

    fetch_attempts = 0
    fetch_success = 0
    fetch_skipped_has_items = 0
    fetch_skipped_cached = 0
    fetch_skipped_not_allowed = 0

    def _ensure_items(contract) -> bool:
        """
        Ensure contract.items is a non-empty list.
        - If items already present -> True
        - If allow_esi_fetch=False and items missing -> False (and DOCTRINE should not change status)
        - If allow_esi_fetch=True -> may call fetch_contract_items(...)
        """
        nonlocal fetch_attempts, fetch_success, fetch_skipped_has_items, fetch_skipped_cached, fetch_skipped_not_allowed

        cur = _items_list(getattr(contract, "items", None))
        if cur:
            contract.items = cur
            fetch_skipped_has_items += 1
            return True

        owner_id = int(getattr(contract, "owner_character_id", 0) or 0)
        cid = int(getattr(contract, "contract_id", 0) or 0)
        key = (owner_id, cid)

        if key in items_cache:
            contract.items = items_cache[key]
            fetch_skipped_cached += 1
            return bool(contract.items)

        if not allow_esi_fetch:
            # not allowed to hit ESI in recalc
            items_cache[key] = []
            contract.items = []
            fetch_skipped_not_allowed += 1
            return False

        # fallback: fetch from ESI (you probably don't use this path)
        fetch_attempts += 1
        try:
            items = fetch_contract_items(contract, None, owner_id) or []
            items = _items_list(items)
            items_cache[key] = items
            contract.items = items
            if items:
                fetch_success += 1
                return True
            return False
        except Exception:
            items_cache[key] = []
            contract.items = []
            return False

    def _title_contains(title: str, needle: str) -> bool:
        if not needle:
            return False
        return needle.lower() in (title or "").lower()

    # ---- main matching ----
    changed: list[dict] = []

    # diagnostics counters
    doctrine_tc_total = 0
    doctrine_tc_skipped_no_items = 0
    doctrine_candidates_total = 0
    doctrine_candidates_title_matched = 0
    doctrine_candidates_with_items = 0

    for tc in tracked_qs:
        matched_contracts = []

        if tc.mode == TrackedContract.Mode.CUSTOM:
            tf = (tc.title_filter or "").strip().lower()
            if not tf:
                # no title filter -> cannot match anything
                base_contracts = []
            else:
                base_contracts = [
                    c for c in all_contracts
                    if tf in ((getattr(c, "title", "") or "").lower())
                ]

            for c in base_contracts:
                ok, _reason = contract_matches(tc, c)
                if ok:
                    matched_contracts.append(c)

        elif tc.mode == TrackedContract.Mode.DOCTRINE:
            doctrine_tc_total += 1

            needle = doctrine_needle_for_tc(tc)

            if not needle:
                doctrine_tc_skipped_no_items += 1
                continue

            # candidates by title first (cheap)
            base_contracts = [c for c in doctrine_contracts if _title_contains(getattr(c, "title", ""), needle)]
            doctrine_candidates_total += len(doctrine_contracts)
            doctrine_candidates_title_matched += len(base_contracts)

            if not base_contracts:
                pass

            if not allow_esi_fetch:
                any_items_present = any(bool(_items_list(getattr(c, "items", None))) for c in base_contracts)
                if base_contracts and not any_items_present:
                    doctrine_tc_skipped_no_items += 1
                    continue

            for c in base_contracts:
                if not _ensure_items(c):
                    continue
                doctrine_candidates_with_items += 1

                ok, _reason = contract_matches(tc, c)
                if ok:
                    matched_contracts.append(c)

        else:
            # unknown mode
            continue

        current = len(matched_contracts)
        desired = int(tc.desired_quantity or 0)

        if desired <= 0:
            percent = 100
            new_status = "OK"
        else:
            percent = int((current / desired) * 100)
            if percent <= int(red):
                new_status = "RED"
            elif percent <= int(yellow):
                new_status = "YELLOW"
            else:
                new_status = "OK"

        old_status = tc.last_status or "OK"
        if old_status != new_status:
            tc.last_status = new_status
            tc.save(update_fields=["last_status"])

            if tc.mode == TrackedContract.Mode.DOCTRINE and tc.fitting:
                name = tc.fitting.name
            else:
                name = tc.title_filter or "—"

            prices = [float(getattr(m, "price", 0) or 0) for m in matched_contracts if getattr(m, "price", None)]
            min_price = min(prices) if prices else None

            changed.append({
                "tc_id": tc.id,
                "name": name,
                "status": new_status,
                "old_status": old_status,
                "current": current,
                "desired": desired,
                "percent": percent,
                "min_price": min_price,
            })

    db_log(
        source="contracts",
        event="alerts_summary",
        data=_ctx({
            "changed": len(changed),
            "changed_names": [c["name"] for c in changed[:10]],
            "tracked_count": tracked_count,
            "snapshot_count": snapshot_count,
            "doctrine_tc_total": doctrine_tc_total,
            "doctrine_tc_skipped_no_items": doctrine_tc_skipped_no_items,
            "doctrine_candidates_title_matched": doctrine_candidates_title_matched,
            "doctrine_candidates_with_items": doctrine_candidates_with_items,
            "fetch_attempts": fetch_attempts,
            "fetch_success": fetch_success,
            "fetch_skipped_has_items": fetch_skipped_has_items,
            "fetch_skipped_cached": fetch_skipped_cached,
            "fetch_skipped_not_allowed": fetch_skipped_not_allowed,
            "allow_esi_fetch": bool(allow_esi_fetch),
        }),
    )

    if not changed:
        db_log(
            source="contracts",
            event="no_changes",
            message="No contract status changes detected",
            data={
                "tracked_count": tracked_count,
                "snapshot_count": snapshot_count,
            },
        )
        return {"changed": 0}

    # keep your existing "suspicious_all_zero" protection if you still want it;
    # for now we keep it as-is.
    suspicious_all_zero = bool(changed) and all(
        int(c.get("desired") or 0) > 0
        and int(c.get("current") or 0) == 0
        and (c.get("status") == "RED")
        for c in changed
    )

    if suspicious_all_zero:
        # IMPORTANT: this can be legitimate (mass buyout).
        # We only log it, but we do NOT rollback statuses and we still send alerts.
        db_log(
            level="WARN",
            source="contracts",
            event="suspicious_all_zero",
            message="All changed tracked contracts are now at zero; NOT rolling back (could be legit buyout)",
            data=_ctx({
                "changed": len(changed),
                "tracked_count": tracked_count,
                "snapshot_count": snapshot_count,
                "changed_names": [c.get("name") for c in changed[:10]],
            }),
        )

    # send alerts
    config = MarketTrackingConfig.objects.first()
    location_name = _location_name(config) if config else "All locations"
    send_contracts_alert(changed, location_name)
    contracts_restocked_alert(changed, location_name)

    return {"changed": len(changed)}



def _update_contract_deliveries(all_contracts, allow_esi_fetch: bool = True):
    """
    Delivery auto completion.
    IMPORTANT: NO GLOBAL PRELOAD. We fetch items only for title-matched doctrine candidates.
    """
    deliveries = ContractDelivery.objects.select_related("tracked_contract__fitting").filter(status="PENDING")

    doctrine_contracts = [
        c for c in all_contracts
        if (c.type or "").lower() == "item_exchange"
        and (c.status or "").lower() == "outstanding"
    ]

    # cache per run to avoid refetch same contract in deliveries loop
    items_cache: dict[tuple[int, int], list] = {}

    def _items_list(value):
        if not value:
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, list) else []
            except Exception:
                return []
        return []

    def _ensure_items(contract) -> bool:
        cur = _items_list(contract.items)
        if cur:
            contract.items = cur
            return True
        
        if not allow_esi_fetch:
            contract.items = []
            items_cache[(int(contract.owner_character_id or 0), int(contract.contract_id or 0))] = []
            return False

        owner_id = int(contract.owner_character_id or 0)
        cid = int(contract.contract_id or 0)
        key = (owner_id, cid)

        if key in items_cache:
            contract.items = items_cache[key]
            return bool(contract.items)

        try:
            items = fetch_contract_items(contract, None, owner_id) or []
            items = _items_list(items)
            items_cache[key] = items
            contract.items = items
            return bool(items)
        except Exception:
            items_cache[key] = []
            contract.items = []
            return False

    for d in deliveries:
        tc = d.tracked_contract
        matched = 0

        if tc.mode == TrackedContract.Mode.CUSTOM:
            tf = (tc.title_filter or "").lower().strip()
            base = [c for c in all_contracts if tf and tf in (c.title or "").lower()]

        elif tc.mode == TrackedContract.Mode.DOCTRINE:
            # doctrine title needle: title_filter OR fitting.name (required)
            needle = (tc.title_filter or "").strip()
            if not needle and tc.fitting:
                needle = (tc.fitting.name or "").strip()

            if not needle:
                base = []  # IMPORTANT: no needle => no candidates => no ESI
            else:
                n = needle.lower()
                base = [c for c in doctrine_contracts if n in (c.title or "").lower()]

        else:
            base = []

        for c in base:
            if c.date_issued and c.date_issued < d.created_at:
                continue

            # in doctrine we must have items; fetch only for title-matched candidates
            if tc.mode == TrackedContract.Mode.DOCTRINE:
                if not _ensure_items(c):
                    continue

            ok, _ = contract_matches(tc, c)
            if ok:
                matched += 1

        d.delivered_quantity = min(matched, d.declared_quantity)
        if d.delivered_quantity >= d.declared_quantity:
            d.status = "FINISHED"
        d.save(update_fields=["delivered_quantity", "status"])



def _recalculate_contract_statuses_and_alert_per_location(all_contracts, yellow, red, allow_esi_fetch: bool = True):
    from .models import TrackedContractLocation
    from .utils import location_display_name

    tracked_loc_qs = (
        TrackedContractLocation.objects
        .filter(is_active=True, location__is_active=True)
        .select_related("tracked_contract", "tracked_contract__fitting", "location")
    )

    tracked_count = tracked_loc_qs.count()
    snapshot_count = len(all_contracts)

    db_log(
        source="contracts",
        event="recalc_start",
        data=_ctx({
            "tracked_loc_count": tracked_count,
            "snapshot_count": snapshot_count,
            "allow_esi_fetch": bool(allow_esi_fetch),
            "yellow": int(yellow),
            "red": int(red),
        }),
    )

    doctrine_contracts = [
        c for c in all_contracts
        if (getattr(c, "type", "") or "").lower() == "item_exchange"
        and (getattr(c, "status", "") or "").lower() == "outstanding"
    ]

    # helpers from your old code
    def _items_list(value):
        if not value:
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, list) else []
            except Exception:
                return []
        return []

    items_cache: dict[tuple[int, int], list] = {}

    def _ensure_items(contract) -> bool:
        cur = _items_list(getattr(contract, "items", None))
        if cur:
            contract.items = cur
            return True

        owner_id = int(getattr(contract, "owner_character_id", 0) or 0)
        cid = int(getattr(contract, "contract_id", 0) or 0)
        key = (owner_id, cid)

        if key in items_cache:
            contract.items = items_cache[key]
            return bool(contract.items)

        if not allow_esi_fetch:
            items_cache[key] = []
            contract.items = []
            return False

        try:
            items = fetch_contract_items(contract, None, owner_id) or []
            items = _items_list(items)
            items_cache[key] = items
            contract.items = items
            return bool(items)
        except Exception:
            items_cache[key] = []
            contract.items = []
            return False

    def _title_contains(title: str, needle: str) -> bool:
        if not needle:
            return False
        return needle.lower() in (title or "").lower()

    changed_global = []

    for tcl in tracked_loc_qs:
        tc = tcl.tracked_contract
        loc = tcl.location
        loc_target_id = int(loc.location_id)

        matched_contracts = []

        if tc.mode == TrackedContract.Mode.CUSTOM:
            tf = (tc.title_filter or "").strip().lower()
            base_contracts = [c for c in all_contracts if tf and tf in ((getattr(c, "title", "") or "").lower())]
            for c in base_contracts:
                ok, _reason = contract_matches(tc, c, location_id=loc_target_id)
                if ok:
                    matched_contracts.append(c)

        elif tc.mode == TrackedContract.Mode.DOCTRINE:
            needle = doctrine_needle_for_tc(tc)
            if not needle:
                continue

            base_contracts = [c for c in doctrine_contracts if _title_contains(getattr(c, "title", ""), needle)]

            # jeśli nie wolno ESI i nie ma items -> nie zmieniamy statusu
            if not allow_esi_fetch:
                any_items_present = any(bool(_items_list(getattr(c, "items", None))) for c in base_contracts)
                if base_contracts and not any_items_present:
                    continue

            for c in base_contracts:
                if not _ensure_items(c):
                    continue
                ok, _reason = contract_matches(tc, c, location_id=loc_target_id)
                if ok:
                    matched_contracts.append(c)

        else:
            continue

        current = len(matched_contracts)
        desired = int(tcl.desired_quantity or 0)

        if desired <= 0:
            percent = 100
            new_status = "OK"
        else:
            percent = int((current / desired) * 100)
            if percent <= int(red):
                new_status = "RED"
            elif percent <= int(yellow):
                new_status = "YELLOW"
            else:
                new_status = "OK"

        old_status = tcl.last_status or "OK"
        if old_status != new_status:
            tcl.last_status = new_status
            tcl.save(update_fields=["last_status"])

            # name for alert
            if tc.mode == TrackedContract.Mode.DOCTRINE and tc.fitting:
                name = tc.fitting.name
            else:
                name = tc.title_filter or "—"

            prices = [float(getattr(m, "price", 0) or 0) for m in matched_contracts if getattr(m, "price", None)]
            min_price = min(prices) if prices else None

            changed_global.append({
                "tc_id": tc.id,
                "tcl_id": tcl.id,
                "location": location_display_name(loc),
                "status": new_status,
                "old_status": old_status,
                "name": name,
                "current": current,
                "desired": desired,
                "percent": percent,
                "min_price": min_price,
            })

    if changed_global:
        # group by location name to satisfy legacy discord functions signature
        buckets: dict[str, list[dict]] = {}
        for row in changed_global:
            loc_name = (row.get("location") or "").strip() or "Unknown location"
            buckets.setdefault(loc_name, []).append(row)

        sent = 0
        for loc_name, rows in buckets.items():
            try:
                send_contracts_alert(rows, loc_name)
            except Exception as e:
                db_log(level="ERROR", source="contracts", event="discord_send_failed",
                       message=f"send_contracts_alert failed: {e}",
                       data=_ctx({"location_name": loc_name, "count": len(rows)}))

            try:
                contracts_restocked_alert(rows, loc_name)
            except Exception as e:
                db_log(level="ERROR", source="contracts", event="discord_send_failed",
                       message=f"contracts_restocked_alert failed: {e}",
                       data=_ctx({"location_name": loc_name, "count": len(rows)}))

            sent += len(rows)

        return {"changed": sent, "locations": len(buckets)}

    return {"changed": 0}
