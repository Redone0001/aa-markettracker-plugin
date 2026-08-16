# AA MarketTracker 1.4.0 security and quality audit

Audit date: 2026-08-16  
Scope: the PyPI 1.4.0 source distribution reconstructed in this workspace  
Method: manual trust-boundary review, route/decorator analysis, secret scanning,
Bandit 1.9.4, Ruff 0.16.3, Python 3.11 compilation, and focused source-level
regression contracts.

## Executive assessment

Do not deploy this version unchanged. The code contains a practical Discord
webhook-secret disclosure chain, multiple missing or incorrect authorization
checks, state-changing GET endpoints, an unauthenticated task trigger, and an
admin-configured SSRF primitive. The implementation also has several material
correctness defects and no runnable, current integration-test setup.

No hardcoded live credentials or private keys were found. ESI access tokens are
placed in Authorization headers and are not directly written to logs by the
reviewed code. Those positive controls do not offset the authorization and
webhook-secret issues below.

### Remediation status

The findings below describe the reconstructed PyPI 1.4.0 baseline. The current
2.0 working tree contains remediations for SEC-01 through SEC-07 and an Alliance
Auth 5.2 migration:

- Discord targets are allowlisted, redirects are disabled, and tokens and
  response bodies are excluded from logs and display values.
- Diagnostics is superuser-only and task logs have automatic configurable
  retention.
- AllianceAuth permissions, selected-location scoping, POST requirements, CSRF
  forms, and enqueue throttling are enforced on the affected views.
- Select2 is served locally with a reviewed hash; shared jQuery and Chart.js
  assets reuse AA's versioned template bundles (including AA's SRI policy).
- A migration purges historical Discord task-log rows.
- ESI access now uses django-esi's generated client and AA `Token` objects rather
  than maintaining a second bearer-token and retry implementation.
- Character ownership and Discord roles use Alliance Auth's supported public
  APIs, and templates reuse AA's Bootstrap 5 and Chart.js bundles.

Deployment still requires rotating webhooks exposed by 1.4.0 and purging copies
from application logs, log aggregators, exports, and backups. The remediations
have regression coverage in minimal Alliance Auth 5.0.1 and 5.2.0 environments; a staging
deployment with the production database and service configuration is still
required before production rollout.

Current Python 3.11 result on both Alliance Auth 5.0.1 and 5.2.0: **70 passed**,
with no failures, errors, xfails, or pending model migrations. Django's system
checks and Ruff also pass. A clean database successfully applies the complete AA
and MarketTracker migration graph through migration 0017.

The score table below records the original PyPI 1.4.0 baseline, not the
remediated working tree.

| Baseline area | Assessment |
| --- | --- |
| Security posture | 3/10 — high risk until access controls and webhook handling are fixed |
| Correctness | 4/10 — several user-visible and destructive defects |
| Maintainability | 3/10 — very large functions, duplicated legacy paths, broad exception handling |
| Test readiness | 2/10 — published tests are stale and no test application/settings are supplied |
| Overall | 3/10 — prototype-quality, not production-ready as published |

## Security findings

### SEC-01 — Critical — Discord webhook token disclosure

The webhook URL contains the Discord webhook token. On an HTTP error or
exception, `_post_embeds()` writes the complete URL to `MTTaskLog` and the
application logger:

- [`markettracker/discord.py`](markettracker/discord.py#L68) stores `"url": url`
  in database log data at lines 78 and 91 and logs it again at line 93.
- [`markettracker/views.py`](markettracker/views.py#L1018) permits every
  authenticated user to open the diagnostics page.
- [`markettracker/templates/markettracker/diagnostics.html`](markettracker/templates/markettracker/diagnostics.html#L346)
  renders and offers one-click copying of the full log stream.
- [`markettracker/models.py`](markettracker/models.py#L27) includes the secret
  URL in `DiscordWebhook.__str__()`.
- [`markettracker/admin.py`](markettracker/admin.py#L17) displays and indexes
  the complete URL in the admin list.

Discord documents the token as a secure token and permits webhook operations
with the token without separate authentication. A user who reads one failed
webhook log can therefore impersonate or disrupt that webhook.

Immediate response:

1. Restrict diagnostics to superusers before exposing the app.
2. Redact webhook URLs everywhere, retaining at most a host and webhook ID.
3. Remove response bodies and secret-bearing URLs from persistent error data.
4. Purge existing Discord error rows from `MTTaskLog` and application logs.
5. Rotate every webhook whose URL may already have been logged.

Reference: [Discord Webhook Resource](https://docs.discord.com/developers/resources/webhook).

### SEC-02 — High — Authorization drift and insecure direct-object access

The model defines separate permissions for basic access, stock management, and
delivery management, and the templates hide links using those permissions.
Several views enforce only login or the weaker `basic_access` permission:

- [`markettracker/views.py`](markettracker/views.py#L486): `manage_stock_view`
  changes global items and contracts with `basic_access`, despite the UI using
  `can_manage_stocks`.
- [`markettracker/views.py`](markettracker/views.py#L346): any logged-in user can
  replace the global admin market character and demote the existing one.
- [`markettracker/views.py`](markettracker/views.py#L836): `admin_deliveries_view`
  exposes every user's deliveries to `basic_access` users, despite the UI using
  `can_manage_deliveries`.
- [`markettracker/views.py`](markettracker/views.py#L714) and
  [`markettracker/views.py`](markettracker/views.py#L867): delivery mutation
  views select rows by global primary key and use `basic_access`, allowing a
  lower-privileged user to finish or delete another user's delivery.
- [`markettracker/views.py`](markettracker/views.py#L132): item detail is public
  because the class has no login or permission mixin. It includes local order
  snapshots that may represent private structures.
- [`markettracker/views.py`](markettracker/views.py#L374), item/fitting search,
  and delivery-creation views require login but not `basic_access`.
- [`markettracker/views.py`](markettracker/views.py#L1018): diagnostics requires
  login only even though the template presents it as superuser-only.

Hiding links in templates is not an authorization control. Enforce the same
permission on every backing view and scope object lookups to the authorized
user or selected location.

Reference: [Django authentication decorators](https://docs.djangoproject.com/en/4.2/_modules/django/contrib/auth/decorators/).

### SEC-03 — High — Unauthenticated background-task trigger

[`markettracker/urls.py`](markettracker/urls.py#L43) binds a lambda that invokes
`refresh_contracts.delay()` to a public GET route. It has no authentication,
permission, method restriction, or normal view-level throttling. Repeated
requests can enqueue work and generate ESI traffic. The cache lock reduces some
overlap but is not an access control and is released after dispatch.

Replace the lambda with a named view requiring `login_required`,
`can_manage_stocks`, and `require_POST`; add rate limiting or an idempotent job
guard at the enqueue boundary.

### SEC-04 — High — State-changing GET endpoints and CSRF exposure

Refresh, finish, and delete views accept GET because they lack `require_POST`:

- `refresh_market_data` and `refresh_contracts_data`
- `delete_delivery` and `finish_delivery`
- `delete_contract_delivery` and `finish_contract_delivery`
- the inline contract-refresh route from SEC-03

An attacker can embed these URLs and cause a logged-in victim's browser to
perform the action. Django's CSRF middleware deliberately treats GET as safe,
so it cannot protect state changes implemented this way. Django explicitly
requires GET to be side-effect free.

Reference: [Django CSRF documentation](https://docs.djangoproject.com/en/4.2/ref/csrf/).

### SEC-05 — Medium — Privileged SSRF through arbitrary webhook URLs

[`markettracker/models.py`](markettracker/models.py#L17) accepts any URL and
[`markettracker/discord.py`](markettracker/discord.py#L68) sends a server-side
POST to it. `requests` follows redirects by default. An administrator or
compromised staff account can target internal services or a trusted-looking
URL that redirects to a private address.

Allow only HTTPS Discord webhook hosts and the expected `/api/webhooks/...`
path, reject credentials and nonstandard ports, disable redirects, and consider
an outbound network policy. OWASP recommends an allowlist when the intended
destinations are known and disabling redirect following.

Reference: [OWASP SSRF Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html).

### SEC-06 — Medium — Third-party JavaScript executes on authenticated pages

[`markettracker/templates/markettracker/base.html`](markettracker/templates/markettracker/base.html#L69)
loads jQuery using the floating `jquery@3` selector and Select2 from a release
candidate CDN URL. The item page loads Chart.js from the same CDN. None of the
resources use Subresource Integrity. A compromised CDN response executes with
the user's AllianceAuth session and can read page data or perform same-origin
actions.

Vendor the files locally, or pin exact immutable versions with integrity and
`crossorigin` attributes. A restrictive Content Security Policy should be an
additional control, not the only one.

### SEC-07 — Medium — Diagnostics exposes operational and private data broadly

Beyond the webhook token, diagnostics renders hostnames, task IDs, character
IDs, contract titles, status, prices, and arbitrary structured task data. The
page exposes up to 500 log records and 200 contract snapshots to any
authenticated account. Restrict it to superusers, minimize retained data, and
apply a retention policy.

## Correctness and data-integrity findings

### QUAL-01 — High — Cleanup command deletes pending deliveries

[`cleanup_deliveries.py`](markettracker/management/commands/cleanup_deliveries.py#L13)
claims to delete fulfilled deliveries, but filters only by age. Old `PENDING`
deliveries are deleted too. Filter explicitly on `status="FINISHED"`, cover
both delivery types if intended, and add a dry-run mode.

### QUAL-02 — High — Published tests target deleted APIs

[`markettracker/tests/test_tasks.py`](markettracker/tests/test_tasks.py#L5)
imports nonexistent `TrackedStructure`, `_fetch_region_orders`, and outdated
function signatures. The example test asserts only `True == True`. Even with a
configured AllianceAuth test project, this suite cannot validate the current
implementation.

### QUAL-03 — Medium — Delivery search uses invalid ORM fields

[`markettracker/views.py`](markettracker/views.py#L827) and line 858 filter
`contract__name` even though `ContractDelivery` has a `tracked_contract` field;
they also use `q` instead of `cq`. Supplying the contract search parameter will
raise `FieldError`.

### QUAL-04 — Medium — Multi-location delivery lookup is ambiguous

[`markettracker/views.py`](markettracker/views.py#L770) looks up a tracked item
only by item ID. Since the schema permits the same item at multiple locations,
`get_object_or_404()` can raise `MultipleObjectsReturned`. Include the selected
location or pass the tracked-item primary key.

### QUAL-05 — Medium — Dead legacy function contains undefined variables

Ruff reports `loc` and `changed_rows` as undefined at
[`markettracker/tasks.py`](markettracker/tasks.py#L1595). The affected 279-line
function is currently unused, but it is an unsafe maintenance trap and evidence
of incomplete migration to the per-location implementation.

### QUAL-06 — Medium — Runtime dependencies are not declared

[`pyproject.toml`](pyproject.toml) declares no `project.dependencies`, although
the package imports AllianceAuth, Django, Celery, ESI, EveUniverse, requests,
and optionally fittings. Installation is not reproducible and dependency
vulnerability auditing cannot determine the production dependency graph.

### QUAL-07 — Medium — Optional fittings handling is internally inconsistent

[`markettracker/models.py`](markettracker/models.py#L9) sets `Fitting = None`
when the fittings import fails, but later passes that value to
`models.ForeignKey`. Views and queries also dereference fitting relations
unconditionally. The package should either declare fittings as required or use
a string relation and guard all optional paths.

### QUAL-08 — Maintainability debt

- 5,405 Python lines are concentrated in `tasks.py` (1,896), `views.py`
  (1,076), and `utils.py` (706).
- The largest functions span 279, 261, 224, 207, and 192 lines.
- The source contains 60 broad `except Exception` handlers; many suppress
  failures completely.
- Legacy single-location and new per-location implementations duplicate fetch,
  matching, status, and alert logic.
- The README has an unclosed code fence and no configuration, migration,
  permission, Celery schedule, or test instructions.

Split orchestration from ESI clients, persistence, authorization, and status
calculation. Remove dead legacy paths and replace broad exception handling with
specific exceptions plus deliberate failure behavior.

## Static-tool results

- Bandit: 4,211 runtime lines scanned; 0 high, 5 medium, 19 low findings. The
  five medium findings are dynamic SQL identifier warnings. Under the current
  call graph, identifiers come from model metadata and a hex task suffix rather
  than HTTP input, so they are not treated as directly exploitable. Identifier
  validation should still be explicit.
- Ruff: 24 findings, including three undefined-name errors in a legacy task,
  unused imports/variables, and import/style defects.
- Hardcoded-secret scan: no live Discord webhook, private key, or obvious API
  credential found.
- Outbound request review: all direct `requests` calls set a timeout.

Static tools did not identify the principal authorization and secret-logging
findings; those required semantic review.

## Added audit and AA 5 tests

The security and quality contracts are in [`audit_tests`](audit_tests/README.md),
and the AA integration/provider tests are in `markettracker/tests`. Run:

```bash
pytest -q
```

The suite now covers authorization and HTTP-method contracts, webhook redaction
and allowlisting, dependency metadata, AA 5 template bundles, django-esi token
and pagination behavior, and a clean migrated database. The stale published
tests have been replaced with current integration tests.

## Recommended remediation order

1. Contain and rotate Discord webhook secrets; restrict diagnostics.
2. Apply the declared permissions consistently and scope all object lookups.
3. Convert all state changes to protected POST views; remove the public lambda.
4. Validate Discord destinations and disable redirects.
5. Fix destructive cleanup and the delivery/query correctness bugs.
6. Declare dependencies and build a minimal AllianceAuth test application.
7. Replace stale tests with request-level permission, CSRF, IDOR, Celery enqueue,
   webhook redaction, and cleanup-retention tests.
8. Remove legacy paths and break up the large task/view functions.

## Limitations

This was a source audit, not a penetration test of a deployed AllianceAuth
instance. No production settings, database, cache, Celery broker, OAuth client,
Discord webhook, or dependency lockfile were provided. Deployment-specific
headers, cookie flags, TLS, database permissions, broker exposure, and current
third-party dependency CVEs therefore remain unverified.
