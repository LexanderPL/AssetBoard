# M9 — Dashboard, reports, backup/export, API and hardening → v1.0

**Goal:** make the app pleasant to live in and safe to expose in a homelab: an at-a-glance dashboard, reports, full export and backups, a read/write API with tokens, security hardening, a phone-ready installable web app, documentation and a polished release.

**Exit criteria:** a new user can install from the compose file, import their history in the documented archive format of M4 (or start fresh), and use every module; backups and restores are tested; a security checklist is completed; docs cover install, reverse proxies, SSO, backup/restore and upgrade; `v1.0.0` is tagged and the multi-arch image is published.

Every agent prompt starts with: *"Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go)."*

**Data model (sketch):** `ApiToken(id, workspace_id, user_id, name, token_hash, scopes[read|write], last_used_at, expires_at?, revoked_at?)`; `BackupRun(id, started_at, finished_at, status, path, size_bytes, error?)`; `DashboardPreference(user_id, workspace_id, widgets JSON, customised_at?)`, where `widgets` is the ordered list of enabled widget keys (F9.1).

---

## F9.1 Dashboard

**After F2.7 (owner, 2026-10-25):** the dashboard is the Home page (`/`) built in F2.7, which
already shows the current month (flow bar, category donuts) and upcoming items. F9.1 adds the
remaining module cards, hiding/reordering and privacy mode to that page; the "This month" and
"Upcoming" cards are F2.7's blocks, not new ones.

**Configurable widgets (owner, 2026-11-02, pre-1.0):** every user chooses what their Home page
shows. Home is built from **widgets** picked from a catalogue: charts for the current month,
upcoming payments, portfolio changes and the other module summaries below. Each user adds, removes
and orders them. A widget is the same thing as a "card" in the rest of this spec.

**Acceptance**
- **Widget catalogue.** Each widget links to its module.
  - **This month: in vs out** (F2.7's flow bar);
  - **This month: expenses by category** and **This month: income by category** (F2.7's donuts,
    one widget each);
  - **This month: planned vs actual** (planned vs actual, savings rate, forecast);
  - **Upcoming payments** (F2.7's upcoming block; overdue highlighted, one-click confirm; it uses
    the workspace's Upcoming window setting);
  - **Net worth** (value + 12-month trend);
  - **Portfolio** (value, gain %, allocation mini-donut);
  - **Portfolio changes** (change in value since the previous day and since the start of the
    month, in money and %, plus the largest movers);
  - **Loans** (remaining principal, payoff date, % repaid);
  - **FIRE** (progress, FIRE age of the default scenario);
  - **Missing data** (stale prices, missing work logs, imports planned but not applied).
- **Modules that ship later.** A widget whose module has not shipped yet is not in the catalogue.
  One whose module has shipped but has no data shows the empty state ("Set up investments →").
  Later modules add their widgets to the same catalogue (a registry, like `EMPTINESS_CHECKS`).
- **Customise Home.** A "Customise" control on Home opens the picker:
  - a checkbox per widget;
  - move up/down buttons, so reordering works without drag and drop, by keyboard and on a phone;
  - "Reset to default".
  Changes apply on save. It works without JavaScript beyond the CSP-safe Alpine build.
- **Defaults.** A user with no saved preference sees what Home shows today (This month: in vs out,
  both category donuts, Upcoming payments), followed by each shipped module's widget. Widgets that
  arrive with a later release appear for users who never customised Home. For users who did, they
  are available in the picker but stay off until chosen.
- **Storage.** Preferences are stored per user and workspace (`DashboardPreference`): the ordered
  list of enabled widgets. An unknown widget key in stored preferences is ignored, so a removed
  widget never breaks Home.
- **Loading.** Each widget is its own HTMX fragment (lazy-loaded), so a slow widget never blocks
  the page. Every figure comes from existing services (no duplicated math). On a phone, widgets
  stack in the chosen order in one column.
- **Privacy mode.** A toggle hides amounts everywhere; it is persisted per user.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F9.1 from docs/specs/M9-dashboard-reports-hardening.md: Home built from a widget registry (catalogue), lazy-loaded
widget fragments calling existing services, a "Customise" picker (enable, order with up/down buttons, reset to default)
stored per user and workspace (model + migration), defaults for users who never customised, graceful
empty states, privacy mode. No new finance math.
Tests: each widget renders with data and without; preferences persist per user and workspace; unknown stored widget keys are
ignored; defaults and newly shipped widgets follow the rules above; reordering works without JavaScript.
```

---

## F9.2 Reports

**Changed (owner, 2026-10-25):** reports have their own milestone,
[M10-reports.md](M10-reports.md), whose Phase A can ship before M5/M6. "Income vs expenses vs
saved per month and per year" and the savings-rate trend moved to F10.1 and F10.2, and
expenses by category over time to F10.3. The reports left here are added as sections of M10's
`/reports` page, with its filters and aggregation rules.

**Acceptance**
- **Year overview:** grid of categories × months (actual), totals per row/column, year selector; heat-shading; CSV export.
- **Category trends:** rolling 3/12-month averages on top of F10.3's category series.
- **Year-over-year** comparison for a category or total.
- **Planned vs actual accuracy:** how often each category exceeded its plan.
- All reports are pure query + domain functions, paginated/streamed where large, respect closed/open months equally, and exclude `transfer` categories.
- Date-range and category filters are URL-addressable (shareable between workspace members).

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F9.2 from docs/specs/M9-dashboard-reports-hardening.md: the four remaining reports as sections of M10's /reports page, with domain aggregation functions, URL-addressable filters, CSV export.
Property tests: grid totals equal transaction sums; treatment rules respected. Include an EXPLAIN-based index review in the PR.
```

---

## F9.3 Archive completeness, secrets and final round-trip

The workspace archive format, export, import and restore are built in M4
(docs/specs/M4-ledger-export-import.md, ADR-0016) and extended by M3 and M5–M8. This feature
closes the gaps before 1.0.

**Acceptance**
- The M4 archive coverage test passes with every workspace-owned model either archived or
  excluded with a reason; the exclusion list is reviewed and documented in
  docs/archive-format.md.
- **Secrets** (notification URLs, price-provider keys, API tokens) stay out of the archive
  unless the owner ticks "include secrets"; they are then written to a separate member encrypted
  with a passphrase (algorithm and any new dependency justified in the PR, per AGENTS.md) and
  restored only with that passphrase. A minor format version per ADR-0016.
- Round-trip test over a fully populated synthetic workspace (the F9.11a demo builder, touching
  every model): export → restore into an empty workspace → export gives byte-identical data
  files (ignoring `exported_at` and the app version in the manifest).
- Golden archives of every released format version restore on the current version.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F9.3 from docs/specs/M9-dashboard-reports-hardening.md: review the archive coverage and exclusions, the optional
passphrase-encrypted secrets member, the full round-trip test over the demo builder (fail if a new model is not covered),
and the golden-archive regression tests for every released format version.
```

---

## F9.4 Backups and restore

**Acceptance**
- Scheduled online backup of the SQLite database using the SQLite backup API (or `VACUUM INTO`), written to `DATA_DIR/backups/` with retention (default: 7 daily, 4 weekly, 6 monthly); `BackupRun` records; failures notify via the notification channels (M3).
- Settings page: backup status, "Back up now", download a backup, retention settings.
- Restore procedure documented and **tested** (CLI `assetboard restore <file>` with integrity check, schema-version check, and automatic pre-restore backup).
- For PostgreSQL deployments (if configured later) the page explains using `pg_dump`; the feature is disabled gracefully.
- Docs: `docs/backup.md` includes advice for volume snapshots and off-site copies.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F9.4 from docs/specs/M9-dashboard-reports-hardening.md: SQLite online backup job with retention, BackupRun model + migration, settings UI,
CLI restore with integrity/schema checks and a pre-restore backup, failure notifications, docs/backup.md.
Tests: backup of a DB under concurrent writes is consistent; restore round-trip; retention pruning with the fixed Clock.
```

---

## F9.5 REST API and tokens

**Acceptance**
- Versioned `/api/v1` JSON API over the existing services (no logic duplicated): transactions, categories, month summary, scheduled occurrences (list/confirm/skip), net-worth summary, portfolio summary, loan summary, FIRE results.
- API tokens: created in the UI, shown once, stored hashed, scoped `read` or `write`, optional expiry, revocable; `Authorization: Bearer`; last-used tracking; per-token rate limit.
- OpenAPI docs served at `/api/docs` (can be disabled by setting).
- Every endpoint enforces workspace scoping and roles via the same services; tenant-isolation tests extended to the API layer.
- Idempotency key support on `POST /transactions` (header) to make automations (Home Assistant, scripts, shortcuts) safe to retry.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F9.5 from docs/specs/M9-dashboard-reports-hardening.md: /api/v1 routers delegating to services, ApiToken model + migration, bearer auth with scopes,
rate limiting, OpenAPI docs toggle, idempotency keys on POST /transactions, tokens UI. Extend the tenant-isolation tests to the API.
Include a docs/api.md with curl examples.
```

---

## F9.6 Security hardening

**Acceptance**
- Written checklist `docs/security-checklist.md` (based on OWASP ASVS level 1 categories) with each item marked done/not-applicable and a link to the test or code.
- CSRF coverage test (every non-GET HTML route rejects missing tokens); authorization matrix test (role × route); session fixation and logout tests; secure cookie flags per `BASE_URL` scheme; CSP and security headers verified by tests; no stack traces in production responses.
- Login/API rate limiting verified; password-reset tokens single-use and expiring.
- `pip-audit` and container image scan (e.g., Trivy) in CI as non-blocking → blocking for high severity; SBOM generated on release.
- Container: non-root, read-only root filesystem tested, no shell tools beyond what's needed, dropped capabilities documented in the compose example.
- Dependency and license review: all runtime deps compatible with AGPL-3.0-or-later; `THIRD_PARTY_LICENSES` generated.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F9.6 from docs/specs/M9-dashboard-reports-hardening.md: write docs/security-checklist.md, add the listed automated tests (CSRF coverage,
role x route authorization matrix, session tests, header tests), CI jobs for pip-audit and image scanning, SBOM and third-party license generation,
compose hardening options. List every gap you find rather than hiding it.
```

---

## F9.7 Documentation and demo data

**Acceptance**
- User docs: install (compose, env reference), first run, reverse proxies (Traefik, Caddy, nginx examples), OIDC setup, proxy-auth, notifications (Apprise examples), price providers, importing from a spreadsheet (links M4's docs/import-export.md and docs/archive-format.md), backup/restore, upgrading, troubleshooting.
- The demo builder and the `assetboard demo-seed` CLI are delivered early by **F9.11a** (before M7). By v1.0 the builder also covers every later module (loans and the mortgage from M7, a FIRE scenario from M8); it is used for screenshots and by tests.
- Screenshots (light/dark, desktop/mobile) generated by a Playwright script against the demo data; README updated.
- Polish translation completeness check (percentage reported in CI; v1.0 requires ≥ 95% for user-facing strings).

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F9.7 from docs/specs/M9-dashboard-reports-hardening.md: documentation set under docs/, screenshots from the F9.11a demo-seed CLI,
a Playwright screenshot script, and a CI check reporting translation completeness.
```

---

## F9.9 Phone layout pass

**Added by the owner (2026-11-01).** Before v1.0, every page is usable on a phone at 360–430 px
width without horizontal page scroll. The collapsible phone menu (#195) is the first step. The
web UI stays the only client: native apps remain a non-goal (overview §6); F9.10 makes the web
app installable instead.

**Acceptance**
- **Month summary table fits on a phone, with no sideways scroll and no cards.** Below `sm`:
  - category names wrap instead of pushing the amount columns off screen;
  - the planned-value input shrinks to about 7 digits;
  - amounts drop the currency symbol (the section header shows the currency once);
  - "+ X pending" shortens to "+X" (screen-reader text keeps the full wording);
  - font and cell padding drop one step.

  Category, Planned, Actual and Difference stay visible and editable at 390 px with the demo
  data's longest category name and a six-digit amount.
  If that still does not fit, the fallback is to show Difference as a small coloured line under
  Actual on phones (like the pending line) instead of its own column. Record the choice in the PR.
- **Transactions entry row on a phone:** tap targets of at least 44 px; amount fields open the
  numeric keypad (`inputmode="decimal"`, which still accepts the locale's separator); the row
  does not overflow.
- **Upcoming:** confirming or skipping an occurrence works with one tap on a phone.
- **Every other page has been checked at 390 px:** Home, History, Recurring, Income (incl. the
  work calendar), Taxes, Categories, settings and auth pages. Each overflow found is fixed or
  listed as a follow-up in the PR.
- **Browser tests at a 390 × 844 viewport assert that the page width does not exceed the
  viewport** on Home, Month, Transactions, History, Upcoming, Recurring and Income.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F9.9 from docs/specs/M9-dashboard-reports-hardening.md: phone layout of the Month summary
table (fit, no horizontal scroll), the Transactions entry row and Upcoming actions, a 390 px review
of every page, and browser tests asserting no horizontal overflow at a phone viewport.
```

---

## F9.10 Installable web app (PWA)

**Added by the owner (2026-11-01).** AssetBoard can be added to a phone's home screen and opens
like an app (own icon, no browser bars). It works on Android and on iOS 16.4+. This is the
answer to "should there be a mobile app": a native app stays a non-goal unless a need the web
cannot meet comes up (home-screen widget, camera receipts, offline sync). Phone notifications
keep going through the existing Apprise channels (F3.6); web push is out of scope.

**Acceptance**
- **Web app manifest** (`manifest.webmanifest`):
  - name from `branding.APP_NAME`;
  - `start_url` and `scope` respect the app's root path;
  - `display: standalone`;
  - theme and background colours from the design tokens, light and dark.

  It is linked from `base.html` and `auth_base.html`.
- **Icons:**
  - PNG 192 and 512 px, each also as a maskable variant;
  - a 180 px `apple-touch-icon`;
  - all generated from the existing SVG favicon by a script that is committed, with the
    outputs committed too.

  No new runtime dependency.
- **Service worker, minimal and privacy-first:**
  - it caches only versioned static assets (CSS, JS, vendored libraries, icons) and one
    offline page;
  - it never caches HTML pages, HTMX fragments, exports or any response carrying workspace data;
  - when offline, a navigation shows the offline page ("You are offline"), which is
    translatable and contains no data;
  - a new release replaces the cache (cache name carries the app version);
  - it is served with a scope that matches the root path and is allowed by the CSP. Registration
    lives in `app.js`, with no inline script.
- **Standalone mode:**
  - no dead ends: every page reachable from the menu, a back path on every page;
  - sign-in, sign-out and OIDC redirects (including Authelia behind a tunnel) return into the
    installed app;
  - the update notice and version still show.
- **Docs:** `docs/` gains "Install on your phone" (Android and iOS steps, and HTTPS being
  required, as it already is for OIDC).
- **Tests:**
  - the manifest is served with the right content type and fields;
  - the service worker's caching rules (a static asset is cached; an HTML page is not; a
    workspace fragment is not);
  - a browser test that registers the worker and loads the offline page.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F9.10 from docs/specs/M9-dashboard-reports-hardening.md: web app manifest, generated icons,
a minimal static-only service worker with an offline page, standalone-mode checks incl. OIDC, user
docs and tests. No new runtime dependency; nothing with workspace data may be cached.
```

---

## F9.11 Demo data builder and demo mode (pulled forward: before M7)

**Added by the owner (2026-11-09).** A public "Try the demo" button that signs a visitor into a
shared sample workspace showing every feature, refreshed every day. The instance owner turns it on
with an environment variable; it is off by default. It pulls the F9.7 demo builder forward, so
every later milestone (M7 loans, M8 FIRE, …) extends the same builder, and the F9.3 round-trip
test uses it.

**Owner decisions (2026-11-09):** one shared demo workspace (visitors see each other's changes
until the next reset); reset **daily**; data in the instance's default language and currency;
imports, exports and archive features are allowed in the demo.

Two PRs, in this order: **F9.11a** builder, then **F9.11b** demo mode.

### F9.11a Demo workspace builder

**Acceptance**
- A builder fills an **empty** workspace with a realistic dataset relative to a reference date `T`
  (the reset date), in a locale and base currency. It is split into a **pure** plan
  (`domain/demo.py`: what to create, from `T`, locale and currency, using a `random.Random`
  seeded from `T`'s ISO date; no I/O, no clock) and a writer (`services/demo.py`) that applies
  the plan **through the existing services** with a `Context` whose clock is fixed, so all
  validation, locks and audit apply. No raw inserts except where no service exists, and then
  only through the repositories.
- **Deterministic:** the same `T`, locale and base currency give the same data (compared through
  the archive export, ignoring ids, `exported_at` and the app version).
- **Amounts** are defined once in EUR-scale and converted with a fixed factor table (EUR 1,
  PLN 4.3, USD 1.1, GBP 0.85, CHF 0.95, other currencies 1), rounded to plausible values
  (whole units for bills, 2 decimals for groceries). Names come from the gettext catalog (en/pl)
  in the workspace locale. All data is synthetic.
- **Contents.** `M0` is the month of `T`; `M-1…M-3` the three previous months. Nothing is dated
  after `T` except planned values, scheduled occurrences and bond maturity.
  - **Ledger:** the suggested categories; 40–60 transactions per full month `M-3…M-1`
    (groceries, transport, eating out, etc., with varied descriptions so autocomplete has
    material) and month `M0` up to `T`; planned values for all four months with some lines over
    and some under plan; `M-3` closed (F2.5).
  - **Recurring and income:** a monthly salary; rent; a variable utility bill; two
    subscriptions; internet; a yearly insurance due in a later month; a one-time planned item;
    a phone bought in 12 installments as a scheduled item with an end (until F7.7 replaces it
    with an installment plan). Past occurrences paid and posted; in `M0` some paid, some pending
    and one overdue. Every scheduled-item kind and occurrence state that exists is used at
    least once.
  - **Variable income and tax:** an hourly income source with work logs for `M-3…M0`, the work
    calendar (F3.7) filled for those months, and at least one tax rule applied to it so the month
    page shows gross, tax and net.
  - **Net worth:** the suggested categories; a current account, savings, an emergency fund,
    a car (declining value), an apartment (Real estate, F5.7), a mortgage as a manual liability
    (linked to a loan once F7.6 lands) and a credit card. Monthly valuations for 12 months with
    one gap so the F5.6 estimate shows; monthly snapshots for 12 months.
  - **Investments:** the suggested asset classes with risk tiers; instruments: a global ETF in
    the base currency (monthly buys for 24 months), an 80/20 fund (allocation split), a US stock
    in USD (a few buys, one sell with realized P&L, quarterly dividends with 15% withholding,
    some recorded as linked income, a 2-for-1 split about 8 months before `T`, one buy settled in
    the base currency, F6.2a), gold (metal, oz), a crypto coin, a bond-model instrument with
    terms and a rate schedule, and a currency (cash) holding. Price history and FX rates for
    24 months as **manual** points from a seeded random walk (weekly; the last point within 3
    days of `T`, plus one older point so a stale badge shows). A saved risk target policy
    (fixed mode). The portfolio linked into net worth (F6.8). No price provider is enabled.
  - **Data page:** one completed CSV transactions import (a generated file run through the real
    import path, so history and rollback show).
  - **Not seeded:** notification channels, price-provider keys, invitations and other members.
- **Coverage test:** after a build, every workspace-owned model (the tenant-coverage registry)
  has at least one row, unless it is listed in `DEMO_EXCLUDED_MODELS` with a reason (e.g.
  notification channels, provider errors). A new model without demo rows or an exclusion fails
  the test, so M7, M8 and later features must extend the builder in their own PRs.
- **CLI:** `assetboard demo-seed --workspace <id> [--date YYYY-MM-DD]` fills an existing empty
  workspace (refused otherwise); used for screenshots (F9.7) and manual testing.
- Builds in under 10 s on CI hardware (a test asserts a generous upper bound).
- The F9.3 round-trip test and the F9.7 screenshot script use this builder.

**Tests:** plan determinism (same inputs, same plan; a different `T` shifts dates); amount
conversion table; writer over en and pl and two currencies; coverage test; every month `M-3…M0`
has transactions and planned values, `M0` has paid, pending and overdue occurrences; holdings
compute without oversell; the split and dividends appear; the archive export of a built
workspace restores into an empty one; CLI refuses a non-empty workspace.

### F9.11b Demo mode: login button, restrictions, daily reset

**Acceptance**
- **Configuration** (env, documented in the env reference and `deploy/` env example):
  `ASSETBOARD_DEMO_ENABLED` (bool, default `false`), `ASSETBOARD_DEMO_RESET_TIME` (`HH:MM`,
  default `03:00`), `ASSETBOARD_DEMO_TIMEZONE` (IANA, default `UTC`; also the demo workspace's
  timezone), `ASSETBOARD_DEMO_IP_LIMIT` (demo sign-ins per IP per hour, default 20). Only the
  person who runs the instance can turn demo mode on; there is no in-app switch. At startup with
  demo mode on, a WARNING says that anyone who can reach the instance can sign in to the demo
  workspace.
- **Demo user and workspace.** On startup with demo mode on, the app creates (idempotently) a
  user flagged `is_demo` (display name "Demo", email on the reserved `.invalid` domain, no
  password hash, never instance admin) and a workspace flagged `is_demo` (name "Demo" in the
  default locale, default currency and locale, the demo timezone) with the demo user as its only
  member (owner). A migration adds the two flags. The demo user cannot sign in with a password,
  password reset, OIDC, proxy auth or invitations; real users are never added to the demo
  workspace, and instance admins do not see it in their switcher.
- **Login page:** while demo mode is on, a secondary **Try the demo** button appears below the
  password form and any SSO buttons, with the hint "Shared sample workspace, resets daily at
  {time}". It is a CSRF-protected POST that creates a normal session for the demo user (no
  two-step sign-in or TOTP), rate-limited per IP with the existing throttle store; a refusal
  shows the usual throttle message. With demo mode off the route returns 404 and the button is
  absent.
- **Banner:** every page of a demo session shows a non-dismissable banner: "Demo workspace:
  shared with other visitors and reset every day at {time} ({timezone}). Don't enter real data."
- **Restrictions**, enforced in the service layer (a `DemoRestricted` error rendered as a 403
  page with a translated message; the UI hides the controls):
  - account: no changes to password, email, display name, language, TOTP, linked SSO accounts
    or profile fields other than those the demo needs (`birth_year` is seeded, not editable);
    no account deletion;
  - workspaces: no creating, renaming, deleting or leaving workspaces; no invitations or
    member/role changes;
  - outside calls: no notification channels or test sends; no price-provider configuration or
    refresh; the scheduler skips demo workspaces for notifications, price refresh and FX
    backfill; no email is ever sent for the demo user;
  - allowed: everything else, including CSV imports and rollback, archive export, archive merge,
    and restore (only reachable if the visitor empties the workspace), within the existing
    upload limits.
- **Daily reset:** an APScheduler job at the reset time (demo timezone), and at startup when the
  last reset is older than 24 hours or the builder's `DEMO_DATASET_VERSION` changed. The reset
  takes the workspace lock, deletes all workspace-owned rows of the demo workspace in FK-safe
  order (driven by the tenant-coverage registry, not a hand-kept list) and its stored import
  files, then runs the F9.11a builder with `T` = today in the demo timezone, all in one
  transaction; on failure it rolls back, logs the error class and keeps the old data. Demo
  sessions stay valid (the workspace id does not change). It records `last_reset_at`,
  `last_reset_error?` and the dataset version in instance settings and an instance-level audit
  event `demo.reset`.
- **Instance admin page:** a "Demo mode" panel (only while on): status, last reset and its
  result, next reset, and a CSRF-protected **Reset now** button (instance admin only).
- **Turning it off:** the button and route disappear, demo sessions are revoked at startup, and
  the demo workspace and user stay (deactivated) until an instance admin deletes the workspace.
- Docs: a "Demo mode" section in the install/config docs (what it exposes, the warning, how to
  reset); en/pl strings, pl without fuzzy entries.

**Tests:** settings parsing; startup creates the demo user and workspace once; the button only
with demo mode on; demo login creates a session and lands on Home; per-IP limit; password,
OIDC, reset and invitation paths refuse the demo user; each restriction raises `DemoRestricted`
from the service (table-driven) and the UI hides the control; scheduler jobs skip the demo
workspace (fake providers and notifier record no calls); reset replaces the data, keeps the
workspace id and sessions, is atomic on a forced failure, and runs at startup when stale or
versioned; admin reset-now (CSRF, instance admin only); banner on every demo page; turning
demo mode off revokes sessions; tenant isolation (the demo user sees only the demo workspace).

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F9.11a from docs/specs/M9-dashboard-reports-hardening.md: the pure demo plan, the service writer, the coverage
test and the demo-seed CLI. Then, in a separate PR, F9.11b: demo settings, demo user/workspace, the login button,
service-layer restrictions, the banner, the daily reset job and the admin panel. No new runtime dependency.
```

---

## F9.8 Release v1.0

**Acceptance**
- F9.9 (phone layout pass) and F9.10 (installable web app) are done.
- Release checklist executed and recorded in `docs/release-checklist.md`: fresh-install test from the published image; upgrade test from the previous tag with a populated DB; backup/restore verified; migrations forward-only tested on a copy of demo data; changelog reviewed.
- Tag `v1.0.0`; release-please PR merged; multi-arch image published to GHCR with `latest`, `1`, `1.0`, `1.0.0`; GitHub release notes with upgrade notes.
- Decide and apply the final project name (if renamed): package, env prefix, CLI, image, docs, via `branding.py` plus a documented migration path for existing data directories and env vars (old prefix accepted with a deprecation warning for one minor version).

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F9.8 from docs/specs/M9-dashboard-reports-hardening.md: write docs/release-checklist.md and scripted checks for fresh-install and upgrade tests
(compose-based, run in CI on tags), and, if a rename is decided, the rename migration with deprecation handling for the old env prefix.
Do not tag or publish anything yourself; produce the checklist and scripts.
```
