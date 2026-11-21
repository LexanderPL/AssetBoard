# M9 — Dashboard, reports, backup/export and hardening → v1.0

**Goal:** make the app pleasant to live in and safe to expose in a homelab: an at-a-glance dashboard, reports, full export and backups, security hardening, a phone-ready installable web app, documentation and a polished release. The read/write API and tokens (F9.5) are deferred after v1.0 (owner, 2026-11-17); v1.0 ships without it.

**Exit criteria:** a new user can install from the compose file, import their history in the documented archive format of M4 (or start fresh), and use every module; backups and restores are tested; a security checklist is completed; docs cover install, reverse proxies, SSO, backup/restore and upgrade; `v1.0.0` is tagged and the multi-arch image is published.

Every agent prompt starts with: *"Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go)."*

**Data model (sketch):** `ApiToken(...)` is deferred with F9.5; `BackupRun(id, started_at, finished_at, status, path, size_bytes, error?)`; `DashboardPreference(user_id, workspace_id, widgets JSON, upcoming_limit, portfolio_range, net_worth_range, customised_at?)`, where `widgets` is the ordered list of enabled widget keys (F9.1).

**Scope decisions (owner, 2026-11-17):** F9.1 does **not** implement privacy mode; the bullet is removed from its acceptance. F9.5 (REST API and tokens) is deferred after v1.0 and is not part of the v1.0 release. The rest of M9 is unchanged.

---

## F9.1 Dashboard

**After F2.7 (owner, 2026-10-25):** the dashboard is the Home page (`/`) built in F2.7, which
already shows the current month (flow bar, category donuts) and upcoming items. F9.1 adds the
remaining module cards and hiding/reordering to that page; the "This month" and
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
  - **Net worth** (value + trend over the chosen range);
  - **Portfolio** (value, gain %, allocation mini-donut);
  - **Portfolio changes** (change in value since the previous day and since the start of the
    month, in money and %, plus the largest movers);
  - **Portfolio value over time** (value and invested capital over the chosen range);
  - **Loans** (remaining principal, payoff date, % repaid);
  - **FIRE** (progress, FIRE age of the default scenario);
  - **Missing data** (stale prices, missing work logs, imports planned but not applied).
- **Modules that ship later.** A widget whose module has not shipped yet is not in the catalogue.
  One whose module has shipped but has no data shows the empty state ("Set up investments →").
  Later modules add their widgets to the same catalogue (a registry, like `EMPTINESS_CHECKS`).
- **Customise Home.** A "Customise" control on Home opens the picker:
  - a checkbox per widget;
  - ↑/↓ buttons, so reordering works without drag and drop, by keyboard and on a phone;
  - per-widget settings: how many Upcoming payments to show (1–50, default 10), the Net worth
    range (1Y, 3Y, 5Y, All; default 1Y) and the Portfolio value-history range (1M, 3M, 6M, 1Y,
    All; default 1Y);
  - "Reset to default".
  Changes apply on save. It works without JavaScript beyond the CSP-safe Alpine build.
- **Defaults.** A user with no saved preference sees what Home shows today (This month: in vs out,
  both category donuts, Upcoming payments), followed by each shipped module's widget. Widgets that
  arrive with a later release appear for users who never customised Home. For users who did, they
  are available in the picker but stay off until chosen.
- **Storage.** Preferences are stored per user and workspace (`DashboardPreference`): the ordered
  list of enabled widgets and the widget settings. An unknown widget key in stored preferences is
  ignored, so a removed widget never breaks Home.
- **Loading.** Each widget is its own HTMX fragment (lazy-loaded), so a slow widget never blocks
  the page. Every figure comes from existing services (no duplicated math). On a phone, widgets
  stack in the chosen order in one column.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F9.1 from docs/specs/M9-dashboard-reports-hardening.md: Home built from a widget registry (catalogue), lazy-loaded
widget fragments calling existing services, a "Customise" picker (enable, order with up/down buttons, reset to default)
stored per user and workspace (model + migration), defaults for users who never customised, graceful
empty states. No new finance math.
Tests: each widget renders with data and without; preferences persist per user and workspace; unknown stored widget keys are
ignored; defaults and newly shipped widgets follow the rules above; reordering works without JavaScript.
```

**As built (F9.1, 2026-11-17)**

- Home is a shell of ordered widget placeholders; each widget is its own lazy HTMX fragment
  (`GET /dashboard/widgets/{key}`). The widgets and their keys are:
  `month-in-out`, `month-expenses`, `month-income`, `month-plan`, `upcoming`, `net-worth`,
  `portfolio`, `portfolio-changes`, `portfolio-history`, `loans`, `fire`, `missing-data`.
- `DashboardPreference` stores the ordered enabled keys per (workspace, user). No row means the
  default catalogue (all shipped widgets). A saved row keeps later widgets off until chosen;
  unknown or corrupt stored keys are ignored.
- The same row stores the Upcoming limit (1–50, default 10), the Net worth range (1Y, 3Y, 5Y,
  All; default 1Y) and the Portfolio history range (1M, 3M, 6M, 1Y, All; default 1Y), all chosen
  in Customise Home.
- `GET /dashboard/customise` is a plain form: checkboxes, ↑/↓ buttons, the three widget settings,
  Save and Reset. With JavaScript `static/js/customise.js` moves the rows in place, updates the
  arrow disabled states and the hidden order field; nothing is written until Save. Without
  JavaScript each arrow rerenders the draft on the server, and Save still applies it.
- Save and Reset sit in one horizontal action row and share one form; Reset posts to its own
  route through `formaction`.
- The old Home KPI block is split into "This month: in vs out" (the flow bar) and "This month:
  planned vs actual" (the plan bars, savings rate and forecast), so the two widgets do not repeat
  figures. The Upcoming block keeps its existing two-step confirm form.
- Net worth, portfolio, loans and FIRE widgets reuse the existing module services; the portfolio
  changes widget reuses F6.7's historical valuation and computes only differences and ratios.
  The loans widget shows up to three active loans with `LoanListFigures.payoff_date`.
- The Portfolio widget's pie uses the same chart height as the expense/income donuts (the macro's
  `canvas_size` parameter leaves the portfolio page's compact donut unchanged). The separate
  Portfolio value-over-time widget draws the line chart, reusing
  `services.performance.performance_page` in chart-only mode and
  `web.charts.build_performance_chart`; no valuation maths is duplicated. The Net worth widget
  reuses `services.networth_history` with the saved range.
- The Customise entry point sits in the Home header action row (F11.9), not at the bottom of the page.
- Missing data composes the existing portfolio price read (stale or absent prices and FX
  rates), `income.missing_work_logs` and planned import batches (the last only for editors and
  owners).
- Privacy mode was removed from F9.1 by the owner on 2026-11-17 and is not implemented.

---

## F9.2 Reports

**Changed (owner, 2026-10-25):** reports have their own milestone,
[M10-reports.md](M10-reports.md), whose Phase A can ship before M5/M6. "Income vs expenses vs
saved per month and per year" and the savings-rate trend moved to F10.1 and F10.2, and
expenses by category over time to F10.3. The reports left here are added as sections of M10's
`/reports` page, with its filters and aggregation rules.

**Acceptance**
- **Year overview:** grid of categories × months (actual), totals per row/column, year selector; heat-shading.
- **Category trends:** rolling 3/12-month averages on top of F10.3's category series.
- **Year-over-year:** one row per year in the range, with income, expenses and separate value/percent changes against the previous year.
- **Planned vs actual accuracy:** how often each category exceeded its plan.
- All reports are pure query + domain functions, paginated/streamed where large, respect closed/open months equally, and exclude `transfer` categories.
- Date-range and category filters are URL-addressable (shareable between workspace members).

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F9.2 from docs/specs/M9-dashboard-reports-hardening.md: the four remaining reports as sections of M10's /reports page, with domain aggregation functions, URL-addressable filters and linked periods.
Property tests: grid totals equal transaction sums; treatment rules respected. Include an EXPLAIN-based index review in the PR.
```

**As built (F9.2):**
- The four sections are additional panels on M10's `/reports` page, using the same shared
  filters and treatment rules. A `year` query parameter (default: the latest data year inside the
  range, capped at the current year) drives the year grid; `trend=3|12` (default 3) chooses the
  rolling window. Unknown years (not 1900–2199) and unknown/other-workspace category ids are
  ignored; a year outside the report range is still shown when explicitly requested.
- **Year overview:** `domain.reports.year_grid` is F10.3's `category_series` for one calendar
  year, so the grid is one row per eligible category with spending in that year (plus explicitly selected zero rows) and twelve month columns, with row/column/
  grand totals. It follows the category filter and the savings option; transfers, income and
  withdrawals never count. Heat shading is four CSS classes over the primary-soft token, not
  inline styles. The report-year selector sits inside the Year overview panel and offers the
  years with transactions inside the selected range plus an explicitly requested year.
- **Category trends:** `domain.reports.rolling_category_averages` averages the displayed F10.3
  series over a trailing 3- or 12-month window, from exact integer sums, and leaves the first
  `window - 1` points empty (a gap, not zero). The chart reuses the report line module; the
  table is the accessible version. Yearly granularity shows a prompt to choose monthly.
- **Year-over-year:** `domain.reports.year_over_year_table` builds one row per year in the range
  from a monthly totals series, with separate income and expense changes. The first year is
  compared with the year before the range (read but not shown); the current year is compared with
  the same months of the previous year. Percent changes are `None` when the previous figure was
  zero. The table always uses the whole ledger.
- **Planned vs actual accuracy:** `domain.reports.plan_accuracy` counts planned months and
  months whose actual was **strictly greater** than the plan per planned regular expense
  category, with a footer over category-months. The service reads the raw `BudgetLine` rows for
  the range in one scoped query (`BudgetLineRepository.for_range`) and counts them in Python;
  transfers, income, savings, categories without plans and other-currency plans are excluded.
  Open and closed months count alike.
- **One read:** the service materialises the one column-only transaction stream once, then runs
  the additional pure aggregations over it; the budget lines are a second scoped read. The
  existing `ix_transactions_workspace_date` index serves the transaction range and the
  `(workspace, month, category)` unique index serves the budget-line range; no migration or new
  index was needed. No SQL aggregate or `DecimalType` ordering is introduced.
- User guide: [Reports](../reports.md). No migration and no new runtime dependency.

---

## F9.3 Archive completeness, secrets and final round-trip

The workspace archive format, export, import and restore are built in M4
(docs/specs/M4-ledger-export-import.md, ADR-0016) and extended by M3 and M5–M8. This feature
closes the gaps before 1.0.

**Acceptance**
- The M4 archive coverage test passes with every workspace-owned model either archived or
  excluded with a reason; the exclusion list is reviewed and documented in
  docs/archive-format.md.
- **Secrets** (notification URLs and price-provider keys; API tokens when F9.5 ships) stay out of the archive
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


**As built (format 2.4):**
- `tests/unit/test_archive_coverage.py` asserts every workspace-owned model is in
  `ARCHIVE_INCLUDED_MODELS`, `ARCHIVE_SECRET_MODELS` or `ARCHIVE_EXCLUDED_MODELS`, with no
  overlap and a non-empty reason for every exclusion. `FireLoanSetting` was the missing model
  found while adding the test. Notification channels are conditional secrets; notification
  logs, provider error logs, audit history, imports/mappings, membership data and per-user UI
  preferences stay excluded with documented reasons in `docs/archive-format.md`.
- The optional `secrets.json.enc` member is written only when an owner ticks **Include
  notification URLs and provider API keys** and supplies a passphrase of at least 12 characters.
  It is a compact JSON envelope (PBKDF2-HMAC-SHA256, 600 000 iterations, 16-byte salt, Fernet)
  listed in the manifest and checksummed. The payload holds the exporting owner's
  notification-channel configuration and provider settings including API keys; other members'
  channels stay private. No new dependency was added: both primitives come
  from the already-vendored `cryptography` package.
- Restore decrypts the member at apply time, after the plan has shown `secrets_present`.
  Notification channels are restored for the restoring owner (per-user ownership is not
  archived) and provider settings are re-encrypted with the target instance secret. A missing
  or wrong passphrase raises a translated 409 and the whole restore transaction rolls back, so
  the workspace stays empty. Merge ignores the member and reports `secrets_restore_only`.
- A full demo-builder round trip into a fresh database now compares every required data file
  byte-for-byte (the manifest is excluded because `exported_at` and the app version differ).
  The parameterized golden-archive test restores every fixture from 1.0 through 2.4.

---

## F9.4 Backups and restore

**Acceptance**
- Scheduled online backup of the SQLite database using the SQLite backup API (or `VACUUM INTO`), written to `DATA_DIR/backups/` with retention (default: 7 daily, 4 weekly, 6 monthly); `BackupRun` records; failures notify via the notification channels (M3).
- Settings page: backup status, "Back up now", download a backup, retention settings.
- Manual backup creation and download require the administrator's password (or a fresh external sign-in) and record instance-level `backup.created` / `backup.downloaded` audit events. Demo reset and delete use the same confirmation.
- Restore procedure documented and **tested** (CLI `assetboard restore <file>` with integrity check, schema-version check, and automatic pre-restore backup).
- For PostgreSQL deployments (if configured later) the page explains using `pg_dump`; the feature is disabled gracefully.
- Docs: `docs/backup.md` includes advice for volume snapshots and off-site copies.

**Implementation notes (F9.4).** One scheduled copy runs per day at 03:30 UTC (rather than a
separate daily/weekly/monthly file): retention keeps the newest scheduled copy of each of the
configured number of days, ISO weeks and months, so one file can be the representative of
several buckets. Manual and pre-restore copies are never pruned automatically. `BackupRun` is
instance-level (no `workspace_id`), so tenant isolation and the demo builder do not apply; the
table is documented as instance-level in `docs/architecture.md`. The backup directory also
holds a `<name>.secret_key` sidecar when the key is stored in `DATA_DIR`, so an automatic
restore keeps encrypted values readable; the download button serves the database file only.
The source is validated as a complete AssetBoard database before copying, so a missing or
empty database cannot produce a "successful" empty backup. Backup writers share a lock (thread
and file), which keeps concurrent manual runs from colliding; the daily tick skips a copy made
within the previous hour to avoid a startup/cron duplicate. A damaged live database still gets
a raw pre-restore copy before a restore, so recovery is never blocked by its own safety net,
and the CLI refuses the default relative `./data` target unless `--data-dir`,
`--database-url` or `ASSETBOARD_DATA_DIR` names the live instance.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F9.4 from docs/specs/M9-dashboard-reports-hardening.md: SQLite online backup job with retention, BackupRun model + migration, settings UI,
CLI restore with integrity/schema checks and a pre-restore backup, failure notifications, docs/backup.md.
Tests: backup of a DB under concurrent writes is consistent; restore round-trip; retention pruning with the fixed Clock.
```

---

## F9.5 REST API and tokens

**Deferred (owner, 2026-11-17).** Not part of v1.0. The acceptance below is kept for the
after-1.0 API work.

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
- Login rate limiting verified; password-reset tokens single-use and expiring.
- `pip-audit` and container image scan (e.g., Trivy) in CI as non-blocking → blocking for high severity; SBOM generated on release.
- Container: non-root, read-only root filesystem tested, no shell tools beyond what's needed, dropped capabilities documented in the compose example.
- Dependency and license review: all runtime deps compatible with AGPL-3.0-or-later; `THIRD_PARTY_LICENSES` generated.

**As built (2026-11-18):** the checklist covers all 127 ASVS 4.0.3 Level 1 requirements
(87 done, 27 not applicable, 13 gaps with follow-ups). The gaps are password length limits
(8/1024 versus 12/128), no strength meter, no sign-in breach re-check, no outbound notices for
authentication changes, temporary-password expiry, MFA not required for instance-admin pages,
user-configured notification/provider hosts (ADR-0021 accepted risk), invite/reset tokens in
URLs (ADR-0013 accepted trade-off), no antivirus scan, outbound host allow-listing, and HSTS
without `includeSubDomains`. `pip-audit` and Trivy report first and then block high/critical;
releases carry a CycloneDX SBOM and `THIRD_PARTY_LICENSES`. The 500 page test, cookie/session,
CSRF, header and role-matrix tests live in `tests/web/test_security_hardening.py`,
`tests/web/test_authorization_matrix.py` and the existing feature suites.

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

**As built (2026-11-19):** the README now indexes install, first run, reverse proxies (with
nginx/Caddy/Traefik examples), OIDC, proxy auth, notifications, price providers, imports,
backup/restore and upgrading, and links a new `docs/troubleshooting.md`. The demo builder
(`DEMO_DATASET_VERSION` 3) creates one live FIRE plan, every adjustment kind and an ignored
loan, so all workspace-owned models except the documented operational/user-what-if exclusions
are covered. `scripts/capture_screenshots.py` generates 40 light/dark desktop/phone images under
`docs/assets/screenshots/`, referenced from README and the user guides. `make i18n-check`
reports the Polish percentage, fails below 95% or on missing/fuzzy entries, and runs in
`make check` and CI.

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

**As built (2026-11-17):** with the demo data's longest category name and six-digit planned and
actual amounts, the four columns do not fit at 390 px, so the documented fallback is used: below
`sm`, Difference is a small coloured line under Actual (like the pending line) and the table
keeps Category, Plan and Actual. The Summary heading shows the currency once and the table's
amounts lose it below `sm`; the KPI and bar captions above the table keep their fully formatted
amounts. The Transactions table keeps its existing in-panel horizontal scroll for the data
columns, while the entry row's controls fit their cells and the page itself never scrolls
sideways. On phones, Upcoming's primary action posts the prefilled date and amount in one tap
(with the same Undo as the dialog) and Skip is a direct button; the editable dialog remains from
`sm` up.

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

**As built (F9.10, 2026-11-18)**

- `GET /manifest.webmanifest` builds the manifest from `branding.APP_NAME`, the ASGI root path
  (`start_url`, `scope` and `id`) and the design tokens (`primary` for `theme_color`, `canvas`
  for `background_color`). The dark tokens travel in the draft `color_scheme_dark` manifest
  member, with `<meta name="theme-color" media="(prefers-color-scheme: …)">` in both page heads
  for current browsers. Both heads link the manifest and the 180 px `apple-touch-icon`.
- The icons are generated from `static/img/favicon.svg` by
  `scripts/generate_pwa_icons.py` (`make icons`) with Playwright's Chromium; the five PNGs are
  committed. No runtime dependency was added.
- `GET /service-worker.js` serves the worker from the app root, so its scope matches the root
  path. It is registered by `app.js` with the running version in `?v=`; the cache name carries
  that version. Only versioned `/static/` assets and `GET /offline` are cached. Navigations are
  network-first and fall back to the translated, data-free offline page; fragments, exports and
  every other workspace response are network-only. `worker-src 'self'` was added to the CSP.
- With AssetBoard at the origin root, sign-in, sign-out and OIDC redirects already stay on the
  app's URLs (OIDC uses a local `next`), so they return to the installed app. A subpath (ASGI
  root path) is honoured by the manifest, worker and offline page, but not yet by the auth
  redirects, so the guide scopes installation to a root deployment; that is a recorded follow-up.
  A real install and an Authelia/OIDC round trip on physical Android and iOS devices cannot be
  automated here; the [Install on your phone](../install-on-your-phone.md) guide covers the
  manual check, and the Chromium test covers the phone viewport, manifest, registration, cache
  rules, the offline page and its working retry. The update notice and version are served in the
  (never cached) HTML.

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
    one gap so the F5.6 estimate shows; no stored snapshots (F5.9 derives the chart from the
    valuations).
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
  takes the instance lock and the workspace lock, deletes all workspace-owned rows of the demo
  workspace in FK-safe order (driven by the tenant-coverage registry, not a hand-kept list) and
  its stored import files, then runs the F9.11a builder with `T` = today in the demo timezone, all
  in one transaction; on failure it rolls back, logs the error class and keeps the old data. The
  workspace row, its membership and demo sessions stay untouched (the workspace id does not
  change). Stored upload files are removed only after the transaction commits. It records
  `last_reset_at`, `last_reset_error?` and the dataset version in instance settings and an
  instance-level audit event `demo.reset` with the reason (`startup`, `scheduled` or `manual`),
  status and version.
- **Instance admin page:** a "Demo mode" panel (only while on): status, last reset and its
  result, next reset, and a CSRF-protected **Reset now** button (instance admin only).
- **Turning it off:** the button and route disappear, demo sessions are revoked at startup, and
  the demo workspace and user stay (deactivated) until an instance admin deletes the workspace.
  While demo mode is off and a demo workspace still exists, the panel stays visible and offers a
  CSRF-protected **Delete demo data** button (instance admin only) that revokes demo sessions,
  removes the demo workspace with all its rows and the demo user, and records the instance-level
  audit event `demo.deleted`.
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

## F9.12 Sidebar customization

**Added by the owner (2026-11-17).** Every member can choose which pages the main sidebar shows
and in which order, for their own account. It is navigation only: a hidden page stays reachable
by its address, and no permission, report or feature gate changes. The preference is per user,
not per workspace, so switching workspaces keeps the same order; pages a workspace does not
enable (today Taxes) are still hidden by that feature gate.

**Acceptance**
- The canonical keys, in the current sidebar order, are `home`, `month`, `transactions`,
  `history`, `upcoming`, `recurring`, `income`, `taxes`, `net_worth`, `investments`, `loans`,
  `fire`. A frozen `SidebarItem(key, visible)` and `DEFAULT_SIDEBAR` (all visible, canonical
  order) live in the pure `domain/navigation.py`.
- `normalize_sidebar(raw)` accepts only a list of mappings with a known string `key` and a
  boolean `visible`; unknown keys and malformed entries are ignored, a duplicated key keeps its
  first entry, Home is forced first and visible, and missing known keys are appended visible in
  canonical order. It never raises on bad stored data. `move_sidebar(items, key, direction)`
  moves one place up or down; Home, boundaries and unknown keys are no-ops.
  `serialize_sidebar(items)` returns the JSON-ready `{"key", "visible"}` list.
- `User.sidebar_items` is nullable JSON (`users.sidebar_items`; migration
  `0065_f912_sidebar_items`, reversible). `NULL` is "never customised": canonical order, every
  destination visible. Saving the canonical list stores `NULL` again; Reset stores `NULL`.
  A destination added in a later release therefore appears at its canonical position for
  never-customised users and appended visible for customised users.
- `WebSession.sidebar_items` carries the stored value from the same user row already loaded for
  the session (no extra query). `WorkspaceNav.sidebar` holds the normalised tuple;
  `workspace_nav(..., sidebar_items=None)` normalises it, and `web.context.current_context`
  passes the session's value. `UserRef`, `Context`, tenant isolation, archive, demo seeding and
  permissions are unchanged: `users` is not workspace-owned.
- **Profile customiser.** `/account` has a Sidebar panel below "Your details", available to
  every role and even without a workspace. It is one ordered list of every key: a hidden
  `item` field in displayed order, a `visible` checkbox whose value is the key, the translated
  label, and Move up / Move down buttons. Home is first, checked and disabled, labelled
  "Always shown", with no move buttons; the first non-Home Up and the last Down are disabled.
  Hidden rows stay in place, muted. The Taxes row explains that it appears only where the
  workspace enables Taxes. Help text says some pages depend on the workspace feature and that
  Save sidebar applies the changes. Demo sessions see the existing "Not available in the demo
  workspace." read-only state.
  With JavaScript, the arrows move the row in place and never submit: the page does not reload
  and nothing is stored until **Save sidebar** is pressed. Focus stays with the moved row; when
  the arrow just used becomes disabled at a boundary, focus moves to the other arrow of that
  row. Save sidebar and Reset to default share one action row; Reset submits the same form to
  its own route with `formaction`, so it is not a second form below Save. Without JavaScript
  the arrows stay submit buttons and the server applies the move immediately; a
  `data-sidebar-nojs-note` note says so and the script removes it when the local reorder is
  available. Stable hooks: `data-sidebar-settings`, `data-sidebar-list`, `data-sidebar-item`,
  `data-sidebar-actions`, `data-sidebar-save`, `data-sidebar-reset`, `data-sidebar-move`,
  `data-sidebar-nojs-note`.
- **Routes.** `POST /account/sidebar` (`account_sidebar`) and
  `POST /account/sidebar/reset` (`account_sidebar_reset`), both CSRF-protected by the account
  router. A successful save or reset redirects (303) to
  `scoped_url(request, "account") + "?sidebar=saved#sidebar-settings"` (or `reset`), and
  `GET /account` shows the translated status inside the Sidebar panel (`role="status"`), not
  the OIDC notice; unknown values are ignored. A demo session is refused by the service with
  `DemoRestricted` (403) before any write. The services are `services/navigation.py`
  (`save_sidebar`, `reset_sidebar`): normalise the posted order, apply
  `<key>:up`/`<key>:down` if present (the no-JavaScript fallback), then write the ORM attribute
  in `run_immediate`; malformed or unknown move values are ignored. A normal JavaScript save
  posts the row order in the `item` fields and has no `move` value.
- **Rendering.** `base.html` renders one loop over request-aware links built from
  `WorkspaceNav.sidebar`; desktop and mobile share it. The existing hooks, glyphs, order,
  `aria-current` rules, Upcoming overdue badge and `ngettext` text are preserved. Home always
  appears; without a workspace Context only Home appears; Taxes appears only when
  `WorkspaceNav.current.taxes_enabled`. All URL logic stays in `web/rendering.py` (no duplicate
  helpers, no circular import). Hidden destinations remain reachable by URL.
- **Docs and i18n.** This section, `docs/accounts.md`, the profile help and all new strings in
  gettext; Polish is complete without fuzzy or empty entries.
- **Tests.** Domain rules (default, custom order/visibility, hidden position, Home forced,
  unknown/duplicate/missing keys, malformed fallback, move boundaries/Home, serialise
  round-trip, property invariants); service (NULL default, custom save, canonical save stores
  NULL, reset, per-user isolation, demo refusal before writing, malformed move ignored); web
  (panel state, save/move/reset with CSRF, hidden link absent but URL 200, current marker and
  overdue badge after reorder, Taxes gate and saved position, no-workspace profile, Polish
  labels, demo read-only/403, unknown and missing stored keys); browser (1440 px and 390 px,
  arrow click and keyboard move reorder in place with no navigation or write, focus stays in
  the moved row at the boundaries, Save sidebar persists, Save and Reset share one visual row,
  checkbox + save, reset, both navs same order, no horizontal overflow, and a
  JavaScript-disabled context that can still save and move).
- No new runtime dependency. No link or control is added to Workspace settings → Menu, and the
  user menu/workspace switcher is unchanged.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F9.12 from docs/specs/M9-dashboard-reports-hardening.md: the pure sidebar model,
User.sidebar_items and the reversible migration, the session/workspace-nav plumbing, the
Profile customiser with save/move/reset, the shared request-aware sidebar loop, docs and
tests. It is navigation only: no permission, archive, demo-seed or tenant-registry change.
No new runtime dependency.
```

**As built (2026-11-17).** Implemented as specified above: `domain/navigation.py`,
`services/navigation.py`, migration `0065_f912_sidebar_items`, `User.sidebar_items`,
`WebSession.sidebar_items`, `WorkspaceNav.sidebar`, `sidebar_links`/`sidebar_labels` in
`web/rendering.py`, the Profile panel, both account routes, and `tests/unit/test_navigation_*`
plus `tests/web/test_sidebar_routes.py` and `tests/browser/test_sidebar_customization.py`.
The panel's submit button reads "Save sidebar", so it is distinct from the profile form's own
"Save" button on the same page, and it shares one action row with "Reset to default". With
JavaScript (`static/js/sidebar.js`), the arrows move the list rows in place, without navigation
or a write; only Save sidebar stores the order, and focus stays in the moved row (on the other
arrow when the used arrow becomes disabled at a boundary). Without JavaScript the arrows remain
submit buttons and apply the move on the server, as the `data-sidebar-nojs-note` note says. A
test keeps `SIDEBAR_LABELS`, `SIDEBAR_GLYPHS` and the link rules in step with `CANONICAL_KEYS`,
and an unhandled canonical key raises instead of silently disappearing. No follow-ups beyond
the existing feature gate for Taxes.

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

**As built (2026-11-19).** [docs/release-checklist.md](../release-checklist.md) is the ordered
pre-tag and post-publish checklist, including the operator-only tag/publish step. The repeatable
parts are `scripts/release_checks.sh` (`fresh-install`, `upgrade`, `backup-restore`,
`verify-tags`; `all` runs the first three) and `scripts/verify_release_tags.py` for the
`docker/metadata-action` output. The Docker workflow verifies the four stable tags before
pushing and runs the published-image checks after a tag; the release workflow verifies that
every file built by `deploy/build-release.sh`, including the CycloneDX SBOM, is attached to the
GitHub release. The app name is already **AssetBoard**, so no rename migration or old-prefix
deprecation was needed. F9.7 (docs, screenshots and the i18n completeness report) is a separate
pre-tag dependency and is not implemented here. Nothing in this change creates a tag, publishes
an image or records a check as passed; those remain operator actions.
