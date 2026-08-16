from django.db import migrations, models
import django.db.models.deletion


TABLE = "markettracker_trackeditem"
REF_TABLE = "markettracker_trackedlocation"
REF_COL = "id"
LEGACY_COLS = ("item_id", "structure_id")


def _column_exists(cursor, table: str, col: str) -> bool:
    cursor.execute(
        """
        SELECT COUNT(*)
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
          AND COLUMN_NAME = %s
        """,
        [table, col],
    )
    return int(cursor.fetchone()[0] or 0) > 0


def _find_unique_index_by_cols(cursor, table: str, cols: tuple[str, ...]) -> str | None:
    # Find UNIQUE index exactly on cols in order
    cursor.execute(
        """
        SELECT INDEX_NAME
        FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
          AND NON_UNIQUE = 0
        GROUP BY INDEX_NAME
        HAVING GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) = %s
        LIMIT 1
        """,
        [table, ",".join(cols)],
    )
    row = cursor.fetchone()
    return row[0] if row else None


def _find_fk_name(cursor, table: str, col: str, ref_table: str, ref_col: str) -> str | None:
    cursor.execute(
        """
        SELECT CONSTRAINT_NAME
        FROM information_schema.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
          AND COLUMN_NAME = %s
          AND REFERENCED_TABLE_NAME = %s
          AND REFERENCED_COLUMN_NAME = %s
        LIMIT 1
        """,
        [table, col, ref_table, ref_col],
    )
    row = cursor.fetchone()
    return row[0] if row else None


def _get_ref_rules(cursor, fk_name: str) -> tuple[str, str]:
    # returns (UPDATE_RULE, DELETE_RULE)
    cursor.execute(
        """
        SELECT UPDATE_RULE, DELETE_RULE
        FROM information_schema.REFERENTIAL_CONSTRAINTS
        WHERE CONSTRAINT_SCHEMA = DATABASE()
          AND CONSTRAINT_NAME = %s
        LIMIT 1
        """,
        [fk_name],
    )
    row = cursor.fetchone()
    if not row:
        return ("RESTRICT", "RESTRICT")
    return (row[0] or "RESTRICT", row[1] or "RESTRICT")


def _has_any_index_on_column(cursor, table: str, col: str) -> bool:
    cursor.execute(f"SHOW INDEX FROM `{table}`;")
    rows = cursor.fetchall() or []
    # rows: (Table, Non_unique, Key_name, Seq_in_index, Column_name, ...)
    for r in rows:
        try:
            if r[4] == col:
                return True
        except Exception:
            continue
    return False


def drop_legacy_unique_if_exists(apps, schema_editor):
    """
    MySQL/MariaDB:
    drop legacy UNIQUE(item_id, structure_id) if it exists.

    Some installs cannot drop it because MariaDB requires that index for an FK on structure_id.
    We therefore:
    - drop ANY FK constraints on structure_id (whatever they reference / whatever their names are)
    - then drop the unique index if possible
    - if still not possible -> skip (non-fatal)
    """
    table = "markettracker_trackeditem"
    cols = ("item_id", "structure_id")

    with schema_editor.connection.cursor() as cursor:
        # 1) find legacy UNIQUE index name by columns
        cursor.execute(
            """
            SELECT INDEX_NAME
            FROM information_schema.STATISTICS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = %s
              AND NON_UNIQUE = 0
            GROUP BY INDEX_NAME
            HAVING GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) = %s
            LIMIT 1
            """,
            [table, ",".join(cols)],
        )
        row = cursor.fetchone()
        if not row:
            return
        idx_name = row[0]

        # 2) drop ALL foreign keys that use structure_id (names vary!)
        cursor.execute(
            """
            SELECT DISTINCT CONSTRAINT_NAME
            FROM information_schema.KEY_COLUMN_USAGE
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = %s
              AND COLUMN_NAME = 'structure_id'
              AND REFERENCED_TABLE_NAME IS NOT NULL
            """,
            [table],
        )
        fk_names = [r[0] for r in (cursor.fetchall() or []) if r and r[0]]
        for fk in fk_names:
            try:
                cursor.execute(f"ALTER TABLE `{table}` DROP FOREIGN KEY `{fk}`;")
            except Exception:
                # ignore, best-effort
                pass

        # 3) ensure there is at least a plain index on structure_id (optional, but helps)
        try:
            cursor.execute(f"SHOW INDEX FROM `{table}`;")
            rows = cursor.fetchall() or []
            has_plain = any((len(r) > 4 and r[4] == "structure_id") for r in rows)
            if not has_plain:
                cursor.execute(f"CREATE INDEX `mt_trackeditem_structure_id_idx` ON `{table}` (`structure_id`);")
        except Exception:
            pass

        # 4) try to drop legacy unique index
        try:
            cursor.execute(f"ALTER TABLE `{table}` DROP INDEX `{idx_name}`;")
        except Exception:
            # non-fatal: if MariaDB still refuses, we skip.
            # New logic uses location_id unique anyway.
            return



def ensure_location_column(apps, schema_editor):
    """
    Ensure location_id column exists on markettracker_trackeditem.
    If it already exists -> noop.
    If missing -> add it as nullable BIGINT to allow backfill.
    Adds an index and FK for location_id if missing.
    """
    with schema_editor.connection.cursor() as cursor:
        if not _column_exists(cursor, TABLE, "location_id"):
            cursor.execute(f"ALTER TABLE `{TABLE}` ADD COLUMN `location_id` bigint NULL;")

        # Ensure index exists
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM information_schema.STATISTICS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = %s
              AND INDEX_NAME = 'mt_trackeditem_location_id_idx'
            """,
            [TABLE],
        )
        idx_exists = int(cursor.fetchone()[0] or 0) > 0
        if not idx_exists:
            cursor.execute(f"CREATE INDEX `mt_trackeditem_location_id_idx` ON `{TABLE}` (`location_id`);")

        # Ensure FK exists (best-effort)
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM information_schema.KEY_COLUMN_USAGE
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = %s
              AND COLUMN_NAME = 'location_id'
              AND REFERENCED_TABLE_NAME IS NOT NULL
            """,
            [TABLE],
        )
        fk_exists = int(cursor.fetchone()[0] or 0) > 0
        if not fk_exists:
            cursor.execute(
                f"""
                ALTER TABLE `{TABLE}`
                ADD CONSTRAINT `mt_trackeditem_location_fk`
                FOREIGN KEY (`location_id`)
                REFERENCES `{REF_TABLE}` (`{REF_COL}`)
                ON DELETE CASCADE
                """
            )


def ensure_new_unique_if_missing(apps, schema_editor):
    """
    Ensure UNIQUE(item_id, location_id) exists on markettracker_trackeditem.
    """
    cols = ("item_id", "location_id")
    with schema_editor.connection.cursor() as cursor:
        if not _column_exists(cursor, TABLE, "location_id"):
            return

        cursor.execute(
            """
            SELECT COUNT(*)
            FROM (
                SELECT INDEX_NAME
                FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = %s
                  AND NON_UNIQUE = 0
                GROUP BY INDEX_NAME
                HAVING GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) = %s
            ) t
            """,
            [TABLE, ",".join(cols)],
        )
        exists = int(cursor.fetchone()[0] or 0) > 0
        if exists:
            return

        cursor.execute(
            f"CREATE UNIQUE INDEX `mt_trackeditem_item_location_uniq` ON `{TABLE}` (`item_id`, `location_id`);"
        )


def forwards(apps, schema_editor):
    TrackedLocation = apps.get_model("markettracker", "TrackedLocation")

    default_loc = TrackedLocation.objects.filter(is_default=True, is_active=True).first()
    if not default_loc:
        default_loc = TrackedLocation.objects.filter(is_active=True).first()
    if not default_loc:
        return

    default_id = int(default_loc.pk)

    with schema_editor.connection.cursor() as cursor:
        has_structure_id = _column_exists(cursor, TABLE, "structure_id")
        has_location_id = _column_exists(cursor, TABLE, "location_id")

        if not has_location_id:
            cursor.execute(f"ALTER TABLE `{TABLE}` ADD COLUMN `location_id` bigint NULL;")

        if has_structure_id:
            cursor.execute(
                f"""
                UPDATE `{TABLE}`
                SET `location_id` = COALESCE(`location_id`, `structure_id`, %s)
                """,
                [default_id],
            )
        else:
            cursor.execute(
                f"""
                UPDATE `{TABLE}`
                SET `location_id` = COALESCE(`location_id`, %s)
                """,
                [default_id],
            )


def backwards(apps, schema_editor):
    return


class Migration(migrations.Migration):

    dependencies = [
        ("markettracker", "0011_trackedlocation_rename_and_seed"),
    ]

    operations = [
        # A) drop legacy unique safely (MariaDB FK-safe)
        migrations.RunPython(drop_legacy_unique_if_exists, migrations.RunPython.noop),

        # B) ensure DB column + FK (safe)
        migrations.RunPython(ensure_location_column, migrations.RunPython.noop),

        # C) update Django state (no DB changes here)
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.AddField(
                    model_name="trackeditem",
                    name="location",
                    field=models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="tracked_items",
                        to="markettracker.trackedlocation",
                    ),
                ),
            ],
        ),

        # D) backfill (SQL-only)
        migrations.RunPython(forwards, migrations.RunPython.noop),

        # E) make non-null at ORM/state level
        migrations.AlterField(
            model_name="trackeditem",
            name="location",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="tracked_items",
                to="markettracker.trackedlocation",
            ),
        ),

        # F) ensure new unique exists (safe)
        migrations.RunPython(ensure_new_unique_if_missing, migrations.RunPython.noop),
    ]
