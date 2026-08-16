from django.db import migrations

TABLE = "markettracker_trackeditem"
BAD_NAME_EXACT = "markettracker_trackeditem_item_id_structure_id_2ea624df_uniq"
BAD_NAME_LIKE = "%item_id_structure_id%"
NEW_INDEX = "mt_trackeditem_item_location_uniq"


def _get_unique_indexes(cursor):
    cursor.execute(
        """
        SELECT DISTINCT index_name
        FROM information_schema.statistics
        WHERE table_schema = DATABASE()
          AND table_name = %s
          AND non_unique = 0
        """,
        [TABLE],
    )
    return [r[0] for r in cursor.fetchall()]


def _index_exists(cursor, name: str) -> bool:
    cursor.execute(
        """
        SELECT COUNT(1)
        FROM information_schema.statistics
        WHERE table_schema = DATABASE()
          AND table_name = %s
          AND index_name = %s
        """,
        [TABLE, name],
    )
    return int(cursor.fetchone()[0] or 0) > 0


def _drop_index_if_exists(cursor, name: str):
    if _index_exists(cursor, name):
        cursor.execute(f"DROP INDEX `{name}` ON `{TABLE}`;")


def _drop_indexes_like(cursor, like_pattern: str):
    cursor.execute(
        """
        SELECT DISTINCT index_name
        FROM information_schema.statistics
        WHERE table_schema = DATABASE()
          AND table_name = %s
          AND non_unique = 0
          AND index_name LIKE %s
        """,
        [TABLE, like_pattern],
    )
    for (idx,) in cursor.fetchall():
        cursor.execute(f"DROP INDEX `{idx}` ON `{TABLE}`;")


def forwards(apps, schema_editor):
    if schema_editor.connection.vendor != "mysql":
        return

    with schema_editor.connection.cursor() as cursor:
        # 1) Drop the known bad index name if it exists
        _drop_index_if_exists(cursor, BAD_NAME_EXACT)

        # 2) Drop any other UNIQUE index that contains item_id_structure_id in its name
        _drop_indexes_like(cursor, BAD_NAME_LIKE)

        # 3) Ensure the new UNIQUE index exists (item_id, location_id)
        if not _index_exists(cursor, NEW_INDEX):
            cursor.execute(
                f"CREATE UNIQUE INDEX `{NEW_INDEX}` ON `{TABLE}` (`item_id`, `location_id`);"
            )


def backwards(apps, schema_editor):
    if schema_editor.connection.vendor != "mysql":
        return

    with schema_editor.connection.cursor() as cursor:
        # reverse just removes the new index (we do NOT recreate old broken one)
        _drop_index_if_exists(cursor, NEW_INDEX)


class Migration(migrations.Migration):
    dependencies = [
        ("markettracker", "0013_contracts_per_location"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
