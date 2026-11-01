# M9 — Dashboard, reports, backup/export, API and hardening → v1.0

**Goal:** make the app pleasant to live in and safe to expose in a homelab: an at-a-glance dashboard, reports, full export and backups, a read/write API with tokens, security hardening, a phone-ready installable web app, documentation and a polished release.

**Exit criteria:** a new user can install from the compose file, import their history in the documented archive format of M4 (or start fresh), and use every module; backups and restores are tested; a security checklist is completed; docs cover install, reverse proxies, SSO, backup/restore and upgrade; `v1.0.0` is tagged and the multi-arch image is published.

Every agent prompt starts with: *"Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go)."*

**Data model (sketch):** `ApiToken(id, workspace_id, user_id, name, token_hash, scopes[read|write], last_used_at, expires_at?, revoked_at?)`; `BackupRun(id, started_at, finished_at, status, path, size_bytes, error?)`; `DashboardPreference(user_id, workspace_id, hidden_cards JSON, card_order JSON)`.

---

## F9.1 Dashboard

**After F2.7 (owner, 2026-10-25):** the dashboard is the Home page (`/`) built in F2.7, which
already shows the current month (flow bar, category donuts) and upcoming items. F9.1 adds the
remaining module cards, hiding/reordering and privacy mode to that page; the "This month" and
"Upcoming" cards are F2.7's blocks, not new ones.

**Acceptance**
- Cards (each links to its module and can be hidden/reordered per user): **This month** (planned vs actual, savings rate, forecast); **Upcoming 30 days** (with overdue highlighted and one-click confirm); **Net worth** (value + 12-month trend); **Portfolio** (value, gain %, allocation mini-donut); **Loans** (remaining principal, payoff date, % repaid); **FIRE** (progress, FIRE age of the default scenario); **Missing data** (stale prices, missing work logs, imports planned but not applied).
- Cards degrade gracefully when a module has no data ("Set up investments →").
- Each card is its own HTMX fragment (lazy-loaded) so a slow card never blocks the page; every figure comes from existing services (no duplicated math).
- Privacy mode toggle (hide amounts everywhere, persisted per user).

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F9.1 from docs/specs/M9-dashboard-reports-hardening.md: dashboard with lazy-loaded card fragments calling existing services,
per-user hide/reorder preferences (model + migration), graceful empty states, privacy mode. No new finance math.
Tests: each card renders with data and without; preferences persist per user and workspace.
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
- Round-trip test over a fully populated synthetic workspace (the F9.7 demo builder, touching
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
- `assetboard demo-seed` CLI creates a realistic synthetic workspace (12+ months of ledger data, recurring items incl. a yearly insurance, a variable income source with tax rules, assets and debts, a small portfolio with an 80/20 fund, a mortgage, a FIRE scenario); used for screenshots and by tests.
- Screenshots (light/dark, desktop/mobile) generated by a Playwright script against the demo data; README updated.
- Polish translation completeness check (percentage reported in CI; v1.0 requires ≥ 95% for user-facing strings).

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F9.7 from docs/specs/M9-dashboard-reports-hardening.md: documentation set under docs/, the assetboard demo-seed CLI using the same builders as tests,
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
