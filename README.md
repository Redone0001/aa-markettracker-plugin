# AA MarketTracker (AllianceAuth plugin)

An **AllianceAuth** plugin for tracking the EVE Online market, including market
orders, contracts, and stock levels. It integrates with ESI and supports optional
Discord notifications.

The plugin **does not require** the `structures` app. It uses a numeric
`location_id` (region or structure) for ESI requests.

## Features

- A list of tracked items with yellow and red stock thresholds.
- Market-order snapshots for regions and structures.
- Contract snapshots.
- Deliveries and basic delivery management.
- Discord webhooks with group mentions and embeds.
- Celery tasks for periodic data refreshes.

## Requirements

- Python `>=3.10,<3.15` (including Python 3.11)
- Alliance Auth `>=5.0,<6` (tested with 5.0.1 and 5.2.0)
- Django `>=5.2,<6`
- django-esi `>=9.4,<10`
- Celery `>=5.5,<6` and django-celery-beat `>=2.8`
- django-eveuniverse `>=2,<3`
- Fittings `>=2.3.2,<3`

Version 2.0 and later support Alliance Auth 5 only. Alliance Auth 4 installations
must upgrade Alliance Auth before installing this release.

## Installation

```bash
pip install aa-markettracker-plugin
```

Add the dependency apps and MarketTracker to the Alliance Auth project's
`local.py`. `modeltranslation` must be loaded before the other apps:

```python
if "modeltranslation" not in INSTALLED_APPS:
    INSTALLED_APPS.insert(0, "modeltranslation")

INSTALLED_APPS += [
    "eve_sde",
    "fittings",
    "eveuniverse",
    "markettracker",
]
```

Do not add an app twice if it is already enabled for another plugin. Then apply
the database migrations and collect static assets from the Alliance Auth project
directory:

```bash
python manage.py migrate
python manage.py collectstatic --noinput
```

Restart the AllianceAuth web and Celery services after the update.

## Upgrading from Alliance Auth 4 / MarketTracker 1.4

1. Back up the Alliance Auth database and `local.py`.
2. Follow the official Alliance Auth upgrade procedure and upgrade the site to
   a stable Alliance Auth 5 release.
3. Install MarketTracker 2.0 or later and add the apps shown above.
4. Run `python manage.py migrate` before restarting the web, worker, and beat
   services.
5. Run the webhook-rotation steps in the security notice below.

The upgrade migrations preserve the existing MarketTracker data model, repair
the historical `structure` to `location` transition, and work on both
MySQL/MariaDB and SQLite.

## Alliance Auth 5 integration

MarketTracker deliberately reuses Alliance Auth 5 facilities instead of
maintaining parallel implementations:

- Pages extend `allianceauth/base-bs5.html`; jQuery comes from the AA base
  template and charts use AA's `bundles/chart-js.html` bundle.
- ESI calls use django-esi's generated client with AA `Token` objects. This
  delegates token refresh, response caching, pagination, ESI compatibility-date
  handling, and rate-limit errors to the AA stack.
- Character links use Alliance Auth's ownership manager and preserve the ESI
  character owner hash.
- Discord group mentions use Alliance Auth's public Discord API when that
  service is enabled. Webhook notifications remain usable without enabling the
  AA Discord service.

## Periodic tasks (`local.py`)

Add the following schedules to your AllianceAuth project's `local.py`. They add
entries to the existing `CELERYBEAT_SCHEDULE`; do not replace the rest of that
setting.

Make sure `crontab` is imported:

```python
from celery.schedules import crontab
```

Then add these entries after `CELERYBEAT_SCHEDULE` has been initialized:

```python
CELERYBEAT_SCHEDULE["refresh_market_data"] = {
    "task": "markettracker.tasks.fetch_market_data_auto",
    "schedule": crontab(minute="*/30"),
}

CELERYBEAT_SCHEDULE["markettracker_fetch_contracts"] = {
    "task": "markettracker.tasks.refresh_contracts",
    "schedule": crontab(minute="*/30"),
}
```

Both tasks run every 30 minutes. Restart the AllianceAuth Celery Beat service
after changing `local.py` so that the new schedules are loaded.

## Permissions

Assign these permissions through Alliance Auth's normal group/state permission
management:

- `markettracker.basic_access` — view MarketTracker and manage personal
  deliveries.
- `markettracker.can_manage_stocks` — configure tracked stock, link the market
  character, and manually enqueue refreshes.
- `markettracker.can_manage_deliveries` — view and manage all deliveries.

The diagnostics page is restricted to Django superusers.

## Development and tests

Install the test dependencies and run the AA 5 integration suite with Python
3.11 or another supported Python version:

```bash
pip install -e ".[tests]"
pytest -q
ruff check .
python -m django check --settings=markettracker.tests.settings
python -m django makemigrations markettracker --check --dry-run \
    --settings=markettracker.tests.settings
```

## Security-related settings

MarketTracker task logs are retained for 14 days by default. Override the
retention period in `local.py` if needed:

```python
MARKETTRACKER_TASK_LOG_RETENTION_DAYS = 14
```

The application performs the retention cleanup opportunistically at most once
per day when it writes a task log.

## Security notice for upgrades from 1.4.0

Version 1.4.0 could write complete Discord webhook URLs to database and
application logs after a failed request. The included database migration removes
the affected MarketTracker database-log rows. It cannot remove copies from
external logging systems, backups, or log exports.

Rotate every Discord webhook that was configured in 1.4.0 and purge any external
logs that may contain its previous URL before deploying the remediated version.
New webhook configuration accepts canonical HTTPS `discord.com` webhook URLs
only; redirects, credentials, custom ports, query strings, and non-Discord hosts
are rejected.
