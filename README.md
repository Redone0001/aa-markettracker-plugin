# AA MarketTracker (AllianceAuth plugin)

An **AllianceAuth** plugin for tracking the EVE Online market, including market
orders, contracts, and stock levels. It integrates with ESI and supports optional
Discord notifications.

The plugin **does not require** the `structures` app. It uses a numeric
`location_id` (region or structure) for ESI requests.

## Features

- A list of tracked items with yellow and red stock thresholds, text search,
  and multi-select item filters for Meta, Tech I, Tech II, Faction, and
  Complex (Deadspace) modules, plus ships and implants.
- Named item groups with one shared quantity target per location, combined
  sell-order stock, and group stock alerts.
- Bulk tracked-item imports with quantity multipliers and optional overwrites.
- Market-order snapshots for regions and structures.
- Opt-in local price and volume history for fits, individual items, and item groups.
- Contract snapshots.
- Deliveries and basic delivery management.
- Discord webhooks with group mentions and embeds.
- Celery tasks for periodic data refreshes.

## Requirements

- Python `>=3.10,<3.15` (including Python 3.11)
- Alliance Auth `>=5.0,<6` (tested with 5.0.1 and 5.2.0)
- Django `>=5.2,<6`
- django-esi `>=9.4,<11`
- django-eveonline-sde `>=0.1,<1`
- Celery `>=5.5,<6` and django-celery-beat `>=2.8`
- django-eveuniverse `>=2,<3`
- Fittings `>=2.3.2,<3`

Version 2.0 and later support Alliance Auth 5 only. Alliance Auth 4 installations
must upgrade Alliance Auth before installing this release.

Django-ESI 10.0.0 is tested with django-eveuniverse 2.2.0. When upgrading to
Django-ESI 10, also upgrade django-eveuniverse: version 2.1.0 requires Django-ESI
below 10.

## Installation

```bash
pip install aa-markettracker-plugin
```

### Install the Alliance Auth 5 beta branch

Until the 2.0 release is published on PyPI, install or reinstall the current
Alliance Auth 5 beta directly from the `aa5-upgrade` branch:

```bash
python -m pip install --force-reinstall --no-deps --no-cache-dir "git+https://github.com/Redone0001/aa-markettracker-plugin.git@aa5-upgrade"
```

The `--no-deps` option deliberately prevents pip from changing the Alliance
Auth environment. Use this command only after the dependencies listed above
have been installed in the active Alliance Auth virtual environment.

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
- `markettracker.can_view_price_history` — browse recorded prices and volume.
- `markettracker.can_manage_price_history` — browse history, select records to
  capture, and configure sampling and retention. Includes view access.
  Both history permissions require `basic_access`; assign them to separate
  viewer and manager groups/roles as appropriate.
- `markettracker.can_move_tracked_items` — view tracked items in Django admin
  and bulk move them between active market locations.
- `markettracker.can_manage_deliveries` — view and manage all deliveries.

The diagnostics page is restricted to Django superusers.

### Local price and volume history

Open **Price history** to switch between **Doctrines**, **Items**, and **Item
groups**, select individual records, and inspect their lowest price and
available volume over 7, 30, or 90 days. Viewers need
`markettracker.can_view_price_history`. Managers need
`markettracker.can_manage_price_history`, which also grants viewing access.
Only managers see recording toggles and sampling/retention settings; requests
to change them are rejected for viewers. Stock-management permission alone
does not grant history access. Both roles see shared doctrines; viewing or
configuring private doctrine history additionally requires `can_manage_stocks`.

Recording is off by default. Merely opening the page creates no settings,
recording rules, or samples. Each enabled fitting records its cheapest complete
fit price and complete fits available, evaluated independently against local
stock and substitution groups. It does not record each component. Item groups
record the lowest member sell price and total member sell volume, without
enabling history for their members. Individual items record the lowest sell
price and total sell volume. Only positive-price, nonempty local sell orders
count; unavailable fits/items record a missing price and zero volume.

Sampling follows successful market refreshes, at most once per hour by default.
Per-location settings offer 1, 6, or 24 hours between samples and retention of
30, 90 (default), or 365 days. No additional sampling schedule or live ESI call
is needed. Failed/empty imports do not create history. The existing
`fetch_market_data_auto` dispatcher expires old samples, including paused
recordings and inactive locations; saving a shorter retention also cleans up
expired samples immediately. Keep that dispatcher scheduled for expiry even
when recording is paused. Pausing preserves existing samples until expiry.
Removing a tracked source removes its history; moving an item rule to another
market also clears its old history and opt-in so prices from different markets
cannot be mixed.

Prices in the doctrine market, item list, and history page are clickable. They
copy the exact whole ISK amount with decimals truncated, no separators, and no
compact suffix: `266,621,493.78` copies as `266621493`. Historical prices keep
their decimals; truncation only affects clipboard text.

After upgrading, run `python manage.py migrate` and
`python manage.py collectstatic --noinput`, restart web and Celery services,
then grant `basic_access` plus `can_view_price_history` to viewer groups or
`can_manage_price_history` to manager groups. Existing managers retain their
access; the migration does not automatically grant view access to other users
or enable any recordings.

### Moving tracked items between locations

Users with `markettracker.can_move_tracked_items` can open **Django admin →
Market Tracker → Tracked items**, select multiple rows, and choose **Move
selected tracked items to another location** from the Actions menu.

The move resets each moved item's status and removes its fetched market-order
snapshots from the old location. The next scheduled market refresh populates
fresh data for the destination. If the destination already tracks an item, that
row is skipped instead of overwriting or merging it.

After installing the version that introduces this permission, run:

```bash
python manage.py migrate markettracker
```

Then assign `markettracker.can_move_tracked_items` through Alliance Auth's group
or state permission management. Django superusers receive it automatically.

## Bulk tracked-item import

Users with `markettracker.can_manage_stocks` can open **Manage Stock → Items → Import items**
and paste one item per line. Supported formats
include:

```text
Machariel x1
Barrage L x1000
Void S    1628
```

The third example uses a tab or at least two spaces before the quantity. A line
without a quantity defaults to `1`. Duplicate names are combined before the
multiplier is applied. Existing desired quantities remain unchanged unless
**Overwrite amount** is selected. Names that do not exactly match an eligible,
published item in the local EVE Universe database are skipped and reported.

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

## Tracking item groups

In **Manage Stock**, select a location and open **Groups → Add group**.
Enter a name, select the items, and set a shared desired quantity. For example,
a target of 100 with 30 units of one item and 50 of another shows 80 / 100,
with 20 still needed. Only sell orders at the selected location count.

The market list displays each group’s combined stock and applies the existing
red/yellow thresholds to its shared target. Groups use the existing item alert
and restock notification settings. A target of zero disables shortage monitoring.
Existing individual targets remain independent; newly selected items are fetched
with an individual target of zero. Editing or deleting a group does not remove
individual tracking rules. An item can belong to more than one group.

After updating an existing installation, apply the new database migration with
`python manage.py migrate` and restart the application and Celery workers.

## Jita prices and markup

Set `JANICE_API_KEY = 'your-key'` in Alliance Auth's `local.py` to enable the
**Jita sell price (markup %)** column on the item list. The key stays on the
server. Prices come from Janice's [batch pricing API](https://janice.e-351.com/api/rest/docs/index.html)
(`POST /api/rest/v2/pricer`, market 2), using immediate sell prices. All tracked
item types at the selected location are requested together in one call.

Markup is `(local minimum sell price / Jita sell price - 1) × 100`:
100 ISK in Jita and 120 ISK locally means +20%. Use the minimum and maximum
markup fields independently or together; bounds are inclusive and can be
negative. These filters apply to individual items, not group stock totals.
Items missing either price are excluded only while a markup filter is active.

Prices are refreshed on demand after 15 minutes. API errors, timeouts, missing
items, and invalid prices retain the last good cached value (labelled **Cached**
when older than 15 minutes). Without a previous value, the column shows **No data**.
Refresh attempts are throttled to once per minute across locations. Last good
prices live in Django's cache until eviction or cache clearing; no API key or
an unavailable cache entry simply means no new reference price. No migration
is needed for this feature.

## Doctrine and fitting seeding

Open **Manage Stock → Doctrines → Track doctrine / fit**, select a market
location, and choose either a doctrine from Fittings or a standalone fitting. Set a default
ship target, save, then adjust each fitting's ship target. New fits added to a
tracked doctrine inherit the default target; existing overrides are preserved.
A ship target of zero pauses tracking for that fit.

Tracking is private by default. **Share with market members** explicitly exposes
the selected doctrine/fit and its contents to everyone with
`markettracker.basic_access`, even if its Fittings categories restrict access.
Stock managers can see private tracking. Unselected doctrines are never added
to the doctrine market view.

**Doctrines** shows collapsed doctrine entries with market coverage, expandable
ships and bottlenecks, the number of complete fits available, and the cheapest
complete-fit price in compact ISK. Only sell orders at the selected tracked
location count. The price consumes the cheapest order stacks first, respecting
available quantities; it is unavailable if a complete fit cannot be assembled.
Compact ship rows inherit the active Alliance Auth theme. Click a fit name or
its components icon to see bottlenecks and change the Buy all quantity.
Coverage adds up each fit's available ships, capped at its target, and divides
by the total ships targeted, rounded down to a whole percentage. For example,
264 available ships against 265 targeted gives 99% coverage. Surplus ships for
one fit cannot offset a shortage in another. Paused fits with a zero target are
excluded. Each fit is evaluated independently against local stock, so shared
stock can contribute to several fits' counts and to the summary. Within a fit,
overlapping substitution groups cannot spend the same stock twice. These
figures are not stock reservations across fits or doctrines.

A module, drone, charge, or cargo item in an existing item group can use any
member of that group. Quantities for multiple components using the same group
are combined. Hulls remain exact. If an item belongs to several groups, the
smallest group is used; ties prefer the oldest group. Group names appear in the
component breakdown so admins can check which alternatives are being used.

**Buy all** copies an EVE Multibuy list for the selected number of complete fits
(default: one). **Buy missing** copies the component shortages against that
ship's configured market target. Both choose the cheapest local alternatives;
if a component has no priced alternative, its original fitting item is used
and the page reports the missing price. Paste the list into EVE to buy it.
These buttons do not place orders or read a pilot's assets.

Fitting contents, doctrine membership, and derived item/group targets synchronize
before each existing market-order refresh. **Sync now** applies source changes
immediately without fetching ESI orders. No additional periodic task is needed.
Required hulls and components are copied from the installed SDE into the local
EVE Universe tables in batches, without one ESI request per type.

Manual targets remain independent: an item's/group's effective minimum is the
larger of its manual target and the summed doctrine target. Alternative groups
receive one combined doctrine target, rather than that target for every member.
Existing tracking rules remain manual. Explicit item edits and bulk imports
also preserve manual tracking. Moving an item rule between locations moves its
manual target only; doctrine targets stay with their configured location and
recreate any required source items on the next synchronization.

The per-location **Automatic item cleanup** setting defaults to off. When enabled,
synchronization removes only automatically added items with no remaining
tracked doctrine/fit source and no item-group membership at that location.
Manual items remain, including manual targets of zero. The item list and stock
management list have a **Part of a doctrine / fit: Yes / No** filter. Click item
list column headings to sort names, stock, targets, minimum prices, Jita prices,
or markup in either direction; missing prices stay last and filters are retained.

## Create groups by matching stats

In **Manage Stock → Groups → Add group**, choose **Match item stats**,
select a reference item, and choose the stats to compare. All stats are selected
by default. Matching compares mass, volume, capacity, and the reference item's
SDE dogma attributes exactly. With all stats selected, extra/missing attributes
also prevent a match. Selecting a subset ignores other attributes. An empty
selection also means all stats.

Candidates are published items in the reference item's SDE group. Matches can
include items not previously loaded into EVE Universe. Matching uses the local
SDE, never per-item ESI calls, and is performed only when the admin saves the
group. Stored membership is reused for market requests and doctrine calculations;
save the group again to refresh it after an SDE update. Matches are capped at
1,000 members, and missing reference/dogma data produces a validation message.
This avoids repeated full-universe comparisons and keeps ordinary market views
independent of stat-matching work.

After updating to this release, run `python manage.py migrate`,
`python manage.py collectstatic --noinput`, and restart the web and Celery
services before configuring doctrine tracking.


## Stock management workspace

**Manage Stock** uses one location selector and four sections: **Items**,
**Groups**, **Doctrines**, and **Contracts**. Each section has its own list and
consistent add/edit screens. Save, cancel, and remove actions return to the same
location and filtered list. Lists use pages of 50 rows instead of nested scroll
areas. **View market** opens the corresponding market view for that location.

The Items and Groups lists distinguish **Manual target**, **Doctrine demand**,
and **Effective target**. Group members with no individual minimum show
**Shared**, linking to the group settings. Contract targets always show the
quantity configured for the selected location.

Use **Items → Import items** for bulk imports. In the Doctrines section, choose
**Edit targets** to manage tracking settings and per-fit quantities side by side;
automatic cleanup is in the collapsed settings below the doctrine list.

**Market connection** contains character linking and refresh actions for all
active markets. It is available to stock managers; refresh buttons become
available after an admin market character is connected. Validation errors keep
submitted values visible so they can be corrected without restarting the form.
