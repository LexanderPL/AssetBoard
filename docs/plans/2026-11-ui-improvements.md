# UI improvements plan — 2026-11-21

Status: **draft for discussion**. Baseline: `main` at `5ce53ac` (0.8.6), 2026-11-21. This file
is the durable checklist for the owner's UI improvement list. Update it as items are
implemented; a feature is not done until its item is checked and its acceptance tests pass.

Legend: `[ ]` not started · `[~]` in progress · `[x]` done · **Decision** = blocked on an owner
answer.

## Work packages

Small, reviewable PRs; keep unrelated items out of the same PR.

| WP | Items | Suggested branch / PR |
| --- | --- | --- |
| WP1 | R-1, R-2, R-3, R-6 | `reports-ui-polish` |
| WP2 | R-4, R-5 | `reports-analytics` |
| WP3 | T-1 | `transactions-emphasis` |
| WP4 | I-1 | `investment-price-history` |
| WP5 | P-1, P-2, P-5 | `profile-admin-icons` |
| WP6 | P-3, P-4 | `workspace-members-admin` |
| WP7 | W-1 | `workspace-country-defaults` |

## Reports

### [ ] R-1 — Report filters fill the panel and keep controls aligned

**Current.** The filter form is `grid gap-4 sm:grid-cols-4 sm:items-end` in
`src/assetboard/web/templates/reports/page.html` (current main line 67). `items-end` aligns the
bottom of each grid item. A field with helper text or an error is taller, so its control sits
higher than fields without one. The Categories multi-select summary and the Include saved
checkbox carry helper text; the Category trends window select does not, so they do not line up.

**Change.**
- Align items to the top (`items-start`), not the bottom.
- Use the full panel width: `grid-cols-1 sm:grid-cols-2 lg:grid-cols-4` with explicit spans.
  Proposed rows after R-6: From / To / Granularity / (Currency when shown); Categories (2 cols)
  / Include saved; submit row. Report year already lives in the Year overview header and stays
  there.
- Keep every control `w-full` and give each field wrapper `min-w-0`.
- Put helper text and errors below the control. Their presence must not move the control.

**Acceptance.**
- At 1280 px, labels and controls in one row share the same top edge whether or not a helper is
  present.
- A helper or field error appears below its control without moving the control or its row.
- The form fills the panel content width; no dead space to the right.
- At 390 px the form is one column and every control remains usable.
- Tests: web class/structure assertion plus a Chromium alignment check.

**Files.** `src/assetboard/web/templates/reports/page.html`,
`tests/web/test_reports_routes.py`, `tests/browser/test_f101_reports.py`.

### [ ] R-2 — Year overview heat uses the transactions red scale

**Current.** Cells use `.heat-1`…`.heat-4` (`src/assetboard/static/css/input.css:410-428`),
which are shades of the primary blue. The legend says “Darker cells mean more spending in that
month.” The Transactions page uses red shades for larger expenses, so the two pages encode the
same idea differently.

**Change.**
- Map the four steps to the existing transaction palette: `bg-danger-shade-1`,
  `bg-danger-shade-2`, `bg-danger-shade-3`, `bg-danger-tint` for the strongest step.
- Update the legend to “Stronger red means more spending in that month.”
- Remove the now-dead `.heat-*` rules and run `make css`.
- Keep zero/negative cells unshaded.

**Acceptance.**
- Heat steps are monotonic and zero stays unshaded.
- Light and dark theme contrast still passes `tests/unit/test_theme_tokens.py`.
- Tests: web classes, Chromium computed background, docs.

**Files.** `src/assetboard/web/reports.py` (`_heat_class`), `reports/page.html`,
`src/assetboard/static/css/input.css`, `src/assetboard/static/css/app.css`, `docs/reports.md`,
report tests.

### [ ] R-3 — Remove the “Show as table” feature from Category trends

**Current.** Category trends always renders a real table. With JavaScript the chart is drawn and
the table is made `sr-only`; the “Show as table” button reveals it. Without JavaScript the table
is visible. R-3 removes the user-facing toggle.

**Decision needed.** Two readings:
- **Recommended:** remove only the toggle; keep the table in the DOM as the accessible and
  no-JavaScript fallback. Charts must keep an accessible data table (UI guidelines).
- Remove the table entirely. This loses the no-JavaScript version and the accessible data table;
  it needs explicit approval.

**Change (recommended).**
- Delete the `data-chart-table-toggle` button in the Category trends section.
- Keep `data-chart-table`; `charts.js` still adds `sr-only` when it draws the chart.
- Do not change the Income vs expenses chart, which keeps its toggle.
- Update the “Show as table” browser test to assert the toggle is absent and the table is
  visually hidden with JavaScript but present for assistive technology.

**Files.** `reports/page.html`, `tests/browser/test_f101_reports.py`, `docs/reports.md`.

### [ ] R-4 — Year-over-year: honest first row, full years, savings column

**Current (after #421).**
- One row per year in the report range: Year, Income, Expenses, Income change (value and %),
  Expenses change (value and %). The footer repeats the range totals.
- `services/reports.py` includes the year before `start_year` so the first row can show a
  change. If that previous year has no data, `change = current - 0`, so the first row reports the
  whole amount as a change even though there is nothing to compare.
- The current year is clipped to today's month on both sides (`partial`); the table shows a
  “so far” badge and a “compared through {month}” note.
- “Expenses” is `expense/regular` only. Savings (`expense/savings`) exists in
  `PeriodTotals.saved` but is not shown. Investment-module purchases are not part of Reports.

**Change.**
1. **No comparison without a previous year.** Add `has_previous` to `YearOverYearRow`, true when
   the previous calendar year has any recorded income, expenses or savings. When false, show
   “—” for every change value and %, with the note “No data for {year}.” Do not show the full
   current amount as a change. Later rows keep their changes; a percentage is still “—” when
   that metric was zero in the previous year.
2. **Full calendar years.** Remove the current-month clipping and the `partial` badge/note.
   Current-year values cover January–December recorded data and are simply lower because some
   income and expenses are still to come. Add a neutral note: “The current year is still in
   progress; no projection is added.”
3. **Add Savings & investments.** Add `saved`, `saved_change` and `saved_change_tenths` to
   `YearOverYearRow`, and a `saved` total to `YearOverYearTable`. Add a Savings column and a
   Savings change group (value and %) to the table. The row then reads:
   `Income`, `Expenses`, `Savings & investments`, and the three change groups. This makes
   `income ≈ expenses + savings` visible.
4. **State the scope.** Add a note: “Expenses are regular spending; savings and investments are
   shown separately. Investment purchases recorded in Investments are not included.”
   `ledger_series` already accumulates `saved`; the “Include saved / invested” filter affects
   category series, not these totals, so YoY savings always show.
5. Keep the per-year links, the range totals footer, and the empty state. On narrow screens the
   wider table scrolls horizontally inside its panel.

**Decision needed.** Confirm the extra Savings columns (the table will have 10 columns and
scroll on phones). Should a “Net” column (income − expenses − savings) also be added, or is the
Savings column enough? Investment-module purchases currently stay out; including them would
need a second data source and is out of scope unless requested.

**Acceptance.**
- A range whose first year has no prior data shows no change/change % in that row.
- A first year that does have a prior year (range starts later) keeps its changes.
- Current-year values are not month-clipped; no “so far” badge, but the in-progress note is
  present.
- Savings totals and changes match `PeriodTotals.saved`; expenses never include savings.
- Domain golden tests: no prior year, prior year with zero income but expenses, full current
  year, savings-only year, totals.
- Web/browser/docs updated, including the Polish catalog and M10 spec.

**Files.** `src/assetboard/domain/reports.py` (`YearOverYearRow`, `YearOverYearTable`,
`year_over_year_table`), `src/assetboard/services/reports.py` (`yoy_monthly` read window),
`src/assetboard/web/reports.py` (`YearOverYearRowView`, `YearOverYearView`),
`reports/page.html`, `docs/reports.md`, `docs/specs/M10-reports.md`, report tests.

### [ ] R-5 — Planned vs actual becomes a planning score

**Current.** The section counts, per planned regular-expense category, how often actual spending
was strictly greater than the plan. It shows months planned, months over, and an over %.

**Proposed.**
- Rename the section to “Planning accuracy”.
- Add a headline score in percent (higher is better), for example:
  `score = max(0, 100 − Σ|actual − planned| / Σplanned × 100)`,
  rounded once to one decimal. It measures how close actual spending was to the plan, not just
  how often it went over.
- Show a progress bar plus the number; keep the category table below as the breakdown, with the
  existing “months over” detail.
- Note: only planned regular expenses count; income and savings are excluded.

**Decision needed.** Is absolute deviation the right score, or do you prefer the simpler
“100% − over-plan rate” (current over % inverted)? The absolute version also penalises being far
under plan, which is still a planning miss; the over-rate version only penalises overspending.

**Acceptance.**
- Domain golden tests for exact plan/actual combinations, zero planned total, and all-on-plan.
- Property test: score is between 0 and 100 and improves when deviations shrink.
- Web and browser assertions for the score, bar and empty state.
- Docs/spec updated.

**Files.** `src/assetboard/domain/reports.py` (`PlanAccuracy`), `src/assetboard/web/reports.py`,
`reports/page.html`, `docs/reports.md`, `docs/specs/M10-reports.md`, report tests.

### [ ] R-6 — Move “Category trends window” into the Category trends panel

**Current.** `trend` is a field in the shared filter form
(`reports/page.html:112-118`). The section only prints the resulting window.

**Change.**
- Remove the field from the top filter form.
- Add a small GET form in the Category trends panel header with hidden copies of all other
  filters (from, to, step, currency, categories, savings, year, yoy_category).
- Use the existing `data-autosubmit` behaviour on the select and a `<noscript>` submit button.
- Keep the URL/bookmark behaviour unchanged.

**Acceptance.**
- Changing the window keeps every other filter.
- The panel shows the active window.
- Without JavaScript the select still submits.
- Tests: web hidden-field preservation and browser autosubmit.

**Files.** `reports/page.html`, `tests/web/test_reports_routes.py`,
`tests/browser/test_f101_reports.py`, `docs/reports.md`.

## Transactions

### [ ] T-1 — Only genuinely large entries stand out

**Current.** Every amount is stored positive; `direction` decides red (expense) or green
(income). The tint is relative to the largest amount of the same direction and currency in the
month: under 25% is neutral, then four steps. A month with one entry makes that entry the
maximum, so it always gets the strongest tint. A month with only a few similar entries tints
most of them.

**Proposed.**
- Keep the red/green direction convention and the four-step scale.
- Add a “genuinely large” gate before shading:
  - require at least 3 entries of that direction and currency in the month;
  - compute their median absolute amount;
  - only entries at or above **2 × the median** are eligible for a tint;
  - then use the existing max-relative steps among eligible entries.
- Result: one or two entries, or a month of similar amounts, stay neutral; a clear outlier
  (rent, a bonus) stands out.
- Apply the same rule to expenses and income. Keep the per-user “Highlight large transactions”
  switch.

**Decision needed.** Is the median rule acceptable, or do you prefer “top 20% of entries” or an
absolute minimum amount? The median rule is the most direct reading of “only large transactions
stand out” and needs no per-currency configuration.

**Acceptance.**
- Unit/property tests: one entry → no tint; two entries → no tint; three similar → none; clear
  outlier → tinted; low values neutral.
- Web shading tests updated; the Chromium test no longer expects a single expense to be tinted.
- Docs (`docs/transactions.md`, `docs/accounts.md`) and profile help text updated if the rule
  wording changes.

**Files.** `src/assetboard/domain/shading.py`, `src/assetboard/web/ledger.py`,
`months/_ledger.html` (unchanged unless the mapping changes), `tests/unit/test_shading_domain.py`,
`tests/web/test_transaction_routes.py`, `tests/browser/test_f22_transactions.py`,
`docs/transactions.md`, `docs/accounts.md`.

## Investments

### [ ] I-1 — Price history shows the latest 10 points, older points expand

**Current.** `_prices_panel.html:94-130` renders every point in `prices.points` (104+ weekly
points in the demo, plus transaction-derived points). No limit or disclosure.

**Change.**
- Always show the latest 10 points, newest first.
- Put the older points in a `<details>` disclosure: “Show N older price points”.
- Keep edit/delete actions available in both parts.
- No service or query change; slice in the view/template. Define a named constant for 10.
- Update the Investments guide.

**Acceptance.**
- 0–10 points: one table, no disclosure.
- 11+ points: latest 10 visible, older rows expandable, count correct.
- Manual, provider and transaction-derived points all keep their source and actions.
- Web test with 12+ points; no-JS works.

**Files.** `src/assetboard/web/templates/investments/_prices_panel.html`,
`tests/web/test_investment_price_routes.py`, `docs/investments.md`.

## Profile, admin and navigation

### [ ] P-1 — Sidebar order rows align; Profile and Account security share a width

**Current.** The Taxes row in `account/profile.html:58-71` inserts a `w-full` helper note
before the arrow group, so the arrows wrap onto a second line. Other rows keep them on the same
line. Profile is `max-w-3xl`; Account security (`auth/password.html:6`) has no width cap and
fills the 6xl base container.

**Change.**
- Put the checkbox, label/note and arrows in a three-column row (or a flex row with the note
  inside a `min-w-0` middle column). The arrows stay on the same line as the label for every
  item, including Taxes.
- Keep long translated notes wrapping inside the middle column, not under the arrows.
- Unify Profile and Account security at the settings width `max-w-3xl space-y-6`. Account
  security keeps its two-column grid inside that width; the 2FA panel keeps `lg:col-span-2`.
- Update the M1 spec if it still describes a different width.

**Decision needed.** Confirm `max-w-3xl` is the wanted “standard”. The alternative is to widen
Profile to 6xl to match today's Account security; the UI guidelines currently say settings and
forms use `max-w-3xl`.

**Acceptance.**
- At 1280 px the Taxes arrows are on the same row as “Taxes”, vertically aligned with the other
  rows' arrows.
- At 390 px no horizontal overflow; the note wraps under the label.
- Profile and Account security have the same outer width class and pass a web assertion.
- Browser test for the Taxes row geometry.

**Files.** `src/assetboard/web/templates/account/profile.html`,
`src/assetboard/web/templates/auth/password.html`, `docs/specs/M1-identity-and-workspaces.md`,
`docs/ui-guidelines.md` if needed, `tests/web/test_profile_routes.py`,
`tests/browser/test_sidebar_customization.py`.

### [ ] P-2 — Instance settings panels share one standard width

**Current.** `admin/settings.html` mixes `max-w-xl` (main and demo panels) with `max-w-3xl`
(backups panel), left-aligned, so the right edges do not line up.

**Change.**
- Wrap the page in `<div class="max-w-3xl space-y-6">`.
- Make every panel full width of that wrapper (remove per-panel `max-w-xl`/`max-w-3xl` and
  `mt-6`).
- Keep the admin Users table as the standard full-width list (`max-w-6xl`), since lists keep the
  base width.

**Acceptance.**
- All panels on Instance settings share the same left and right edge at 1280 px and 390 px.
- The page passes an outer-width assertion.
- Tests updated; docs unchanged unless they state a different width.

**Files.** `src/assetboard/web/templates/admin/settings.html`,
`tests/web/test_admin_routes.py` (or a settings-specific test).

### [ ] P-3 — Delete deactivated users

**Current.** Admin actions are activate/deactivate/promote/demote/reset 2FA. There is no delete
path. A user row can be hard-deleted in principle: sessions, identities, tokens, 2FA,
dashboard preferences and notification channels cascade; audit/transactions/month states/import
batches/invitations set the user reference to null. `memberships.user_id` has no `ondelete`, so
any membership blocks deletion. Ownership is membership-based; there is no `Workspace.owner_id`.

**Proposed.**
- Add a `delete` admin action, offered only for an **inactive** user, behind the existing
  confirm-with-password/2FA flow and a danger confirmation.
- Refuse active users, self, demo users, and a user who is the **sole owner of any workspace**
  (list the workspace names). Otherwise remove their memberships and delete the user row.
- Audit `admin.user_deleted` with the number of memberships removed. Keep the audit actor and
  target id; the deleted user can display as “Deleted user”.
- No schema migration for a hard delete. All existing FKs already behave correctly.
- Add the action to the admin allowlist, the audit-data allowlist, the row menu, docs and tests.

**Decision needed.** Confirm the membership policy: remove memberships and block sole owners
(recommended), or refuse deletion whenever any membership exists (admin must remove the user
from each workspace first). Also confirm hard delete vs. an anonymised soft delete that keeps a
“Deleted user” row for audit display.

**Acceptance.**
- Active user: no Delete action and a forged POST is refused.
- Inactive user with no memberships: deleted; sessions/identities/tokens gone; audit row kept.
- Inactive user who is a sole owner: refused with a clear message; nothing deleted.
- Inactive user who shares a workspace: membership removed, workspace survives.
- CSRF and role checks in the service; tests for each case.
- Docs (`docs/accounts.md`, M1 spec) updated.

**Files.** `src/assetboard/services/admin_users.py`, `src/assetboard/web/admin_routes.py`,
`src/assetboard/web/templates/admin/users.html`, confirm template if it needs a delete tone,
`docs/accounts.md`, `docs/specs/M1-identity-and-workspaces.md`, admin tests.

### [ ] P-4 — “Workspace members” naming and discoverability

**Current.** The user-menu item, page title and heading say “Members”. The section heading says
“People in this workspace”. The header button says “Invite someone”. The page is separate from
Workspace settings.

**Change.**
- Rename the menu item, page title and page heading to **Workspace members**.
- Change the header button to **Invite**; use “Invite to this workspace” as the dialog heading.
- Add an owner-only **Members** panel or link at the bottom of Workspace settings (member count,
  pending invitations, “Manage members” link), so the workspace scope is obvious from settings.
- Keep the dedicated page for the member list, roles, removal and invitations. This avoids
  moving a large list and dialog into the settings page.

**Decision needed.** Is “rename + link/panel from settings” enough, or do you want tabs
(“General” / “Members”) inside Workspace settings? Tabs are a larger refactor and need a
settings/members route merge.

**Acceptance.**
- “Workspace members” appears in the user menu, page title and heading.
- The Invite button says “Invite”; the dialog still opens and works without JavaScript.
- Workspace settings links to the members page for owners only.
- English and Polish catalogs updated; header/menu/page tests updated.

**Files.** `base.html`, `workspace/members.html`, `workspace/settings.html`,
`services/workspaces.py` or the settings route for counts, locales, `docs/accounts.md`,
M1 spec, tests.

### [ ] P-5 — Nicer, larger sidebar icons

**Current.** Icons are Unicode glyphs in `SIDEBAR_GLYPHS` (`web/rendering.py:51-65`), rendered in
an unclassed `<span>` in `base.html:58`. They render at text size and vary by platform
(`⌂ ◷ ≡ ⌕ ▦ ◔ ↻ ¤ % ◇ ↗ ▤ ✦`).

**Proposed.**
- Replace the glyphs with a consistent inline SVG set: 20 × 20 (`h-5 w-5`), outline,
  `stroke-width="1.5"`, `aria-hidden="true"`, `shrink-0`.
- Use Heroicons (MIT) or Lucide (ISC) outline icons. Heroicons is the proposed default; both
  licences are AGPL-compatible.
- Add a `sidebar_icon(key)` macro or an icon-path map in `rendering.py`, keeping labels and keys
  unchanged.
- Add the upstream licence and a `vendor/README.md` entry (version, licence, source). If the
  paths are embedded rather than copied files, document the upstream version and licence in the
  same entry.
- Keep the 13-key coverage test and update exact-glyph assertions.

**Decision needed.** Approve a third-party icon set (Heroicons, MIT) with attribution, or keep
the Unicode glyphs and only make them larger (`text-lg`/fixed 20 px box)? The icon set is the
better visual result and avoids platform font differences.

**Acceptance.**
- Every sidebar destination has a distinct icon; all are 20 px and vertically centred with the
  label.
- No layout shift on phone/desktop; icons do not shrink when labels wrap.
- Licence/attribution complete; `tests/web/test_sidebar_routes.py`,
  `tests/web/test_navigation_routes.py`, `tests/web/test_reports_routes.py` updated.
- Browser screenshot check at 390 and 1440.

**Files.** `src/assetboard/web/rendering.py`, `base.html`, a new icon macro/template or map,
`src/assetboard/static/vendor/README.md` + licence, `docs/architecture.md` or
`docs/ui-guidelines.md`, tests.

## Workspace setup

### [ ] W-1 — Country first, then auto-set currency (and timezone) from it

**Current.** The shared `workspace_fields` macro
(`workspace/_fields.html`) orders: name → base currency → country → time zone → language.
Defaults: currency = instance default (EUR), country = not set, timezone = UTC. Used by both
first-run setup and Create workspace.

**Change.**
- Reorder: workspace name → **country** → base currency → time zone → language.
- Add a country-defaults helper:
  - **Currency:** Babel `get_territory_currencies(code, tender=True)`; pick the first current
    tender currency. No new dependency (Babel is already a runtime dependency).
  - **Timezone:** needs a decision. Options:
    1. **Recommended:** seed from the browser's IANA zone
       (`Intl.DateTimeFormat().resolvedOptions().timeZone`) on page load and only update it when
       the country changes if the user has not overridden it. No country→timezone table.
    2. Maintain a curated country→representative-timezone map (capital zone) for all countries.
       More accurate to the request but a new data file/maintenance burden and wrong for
       multi-timezone countries such as the US, Canada, Australia and Brazil.
    3. Add a new dependency (not recommended).
- JS lives in a small external module (`workspace-fields.js`) and is CSP-safe. Server-side, when
  a country is submitted with a blank currency, derive the currency; timezone falls back to UTC
  or the submitted value. This keeps crafted/no-JS submissions valid.
- Apply the same behaviour to first-run setup (`auth/setup.html`) and Create workspace
  (`workspace/new.html`), since both use the shared macro.
- Add a help line: “Picked from the country; change it if needed.”

**Decision needed.** Choose timezone option 1 or 2 (or say timezone can stay manual for now).
Also confirm the country should come before the workspace name or only before base currency.

**Acceptance.**
- Both forms show Country before Base currency.
- Selecting Poland sets PLN and a Europe/Warsaw default; United States sets USD; unknown/blank
  leaves the previous value or the instance default.
- First-run setup stores the chosen values; no-JS submission still works with the server
  fallbacks.
- Unit tests for country→currency (and timezone if mapped); web tests for order/defaults;
  Chromium test for the change event; docs updated.

**Files.** `workspace/_fields.html`, `auth/setup.html`, `workspace/new.html`,
`services/workspace_options.py`, `web/workspace_forms.py`, `web/auth_routes.py`,
`web/workspace_routes.py`, new `static/js/workspace-fields.js`, `base.html` script include if
needed, `docs/accounts.md`, M1 spec, tests.

## Answered questions

### Q-1 — How do I import a backup?

There are two different “backups” in AssetBoard:

**A. Workspace archive (ZIP) — restore in the UI**
1. Sign in as the workspace **owner**.
2. Create or use an **empty workspace** (restore refuses a non-empty one; untouched suggested
   categories are fine).
3. Open the user menu → **Export and import**.
4. Under **Import ledger data**, choose the ZIP, select **Restore into this workspace**, then
   **Validate upload**.
5. Review the restore plan; if the archive has `secrets.json.enc`, enter the **secrets
   passphrase** in the Apply form.
6. Click **Apply import**. It is all-or-nothing. Use **Roll back import** if needed.

A workspace archive can also be **merged** into an existing workspace: choose “Merge data into
this workspace” instead. Merge applies ledger data only, requires the same base currency, and
does not apply settings or closed months.

**B. Automatic database backup (`.db` from Instance settings) — restore with the CLI**
1. Stop the application first.
2. Run `assetboard restore <path-to-backup.db>` with the service's environment and data
   directory (native install: via `systemd-run`; container: copy the file in and run
   `python -m assetboard restore /data/restore.db`).
3. Copy the adjacent `.db.secret_key` file too when it exists; without it, 2FA secrets,
   notification URLs and provider credentials cannot be decrypted.
4. Start the application.

Full commands and caveats are in `docs/backup.md` §“Restore a backup”. The workspace archive is
the safer choice for moving one workspace between instances; the database restore replaces the
whole instance.

## Cross-cutting definition of done

For every item:
- `make check` green (ruff, format, pyright, i18n, licences, pytest).
- New user-facing strings in gettext; Polish catalog updated and `make i18n-check` 100%.
- Tests traceable to the acceptance criteria; browser tests where layout is the point.
- Docs/spec updated where behaviour or layout intent changed.
- Conventional Commits; one small PR per work package.
- Update this file's checkbox and add a short “as built” note when an item ships.

## Decisions requested

1. **R-3:** keep the accessible Category trends table but remove the toggle (recommended), or
   remove the table entirely?
2. **R-4:** add the Savings & investments columns (recommended) and hide all first-row changes;
   should a “Net” column (income − expenses − savings) also be added? Investment-module
   purchases stay out unless you want a second data source.
3. **R-5:** absolute-deviation planning score (recommended) or inverse over-plan rate?
4. **T-1:** median-based “2× the month's median, at least 3 entries” rule (recommended), or
   top-N/absolute rule?
5. **P-1:** unify Profile and Account security at `max-w-3xl` (recommended), or widen both to
   6xl?
6. **P-3:** remove memberships and block sole owners (recommended), or refuse any user with a
   membership? Hard delete (recommended) or anonymised soft delete?
7. **P-4:** rename + settings link/panel (recommended), or tabs inside Workspace settings?
8. **P-5:** Heroicons SVG set (recommended) or just larger Unicode glyphs?
9. **W-1:** timezone from the browser (recommended), curated country→timezone map, or leave
   timezone manual for now? Country before name or only before base currency?
