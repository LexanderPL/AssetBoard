# AssetBoard deep audit — 2026-11-20

## 1. Metadata

| | |
|---|---|
| Audit date | 2026-11-20 |
| Code audited | `b68cc3f` (`origin/main`, includes #398/#400/#402) |
| Owner checkout | `chore/release-1.0.0` at `7cdf53f` — **behind main**; draft PR #397 is the v1.0 release PR |
| Latest tag | `v0.8.4`; **no `v1.0.0` tag** |
| Environment | Linux, Python 3.13, uv, SQLite/WAL; throwaway instance at `/tmp/assetboard-audit`, ports 8010 (demo data) and 8011 (fresh/Polish) |
| Browser | Chromium 1440×900 and 390×844, light/dark, 200% zoom proxy (720×450); English and Polish |
| Data | Synthetic demo data only; no owner data; owner’s port-8000 instance untouched |
| Network | No outbound calls: scheduler off, demo off on the fresh instance, providers/SMTP/OIDC/update check not configured |
| Tests | Last full gate at this commit: **6,352 passed, 42 skipped**. This audit did not rerun the full gate. Targeted suites in temporary copies: 355 ledger/months/occurrences/scheduled/income/taxes/recurrence and 292 import/archive tests, plus the net-worth/investment/loan/FIRE suites, passed. |
| Report file | This file only. No application code, templates, tests, migrations, config or other docs were changed. |

**Not dynamically tested:** live OIDC and proxy authentication (fakes/static only), SMTP and email reset/registration delivery, real price providers and notifications, PostgreSQL, upgrade from the previous tag (fresh install only), antivirus scanning, public demo mode, live 2FA enrollment, and a genuinely large database. Items depending on these are marked *static-only*.

**Release note:** `main` still says `Status: pre-alpha` in `README.md:3`; the release branch says 1.0. The release PR must merge current `main` (including #402) before it can be the 1.0 release candidate.

---

## 2. Executive summary

- AssetBoard is a mature, unusually well-disciplined 1.0 candidate. Money is integer minor units, domain math is pure, repositories are workspace-scoped, CSRF/Origin checks are systematic, and the UI has a real design system.
- The highest-risk correctness bug is **archive restore of fixed tax amounts in the wrong currency**: restoring a JPY/KWD archive into an empty EUR workspace can store 100×/1000× wrong tax amounts, or abort the restore. It is silent data corruption in a core portability flow.
- A second P1: **a current-balance loan becomes unbuildable after a later balance checkpoint**. The rate timeline starts at the latest checkpoint while the initial rate change is dated at the original balance date; the schedule, linked net-worth liability, property LTV and FIRE loan input fail.
- The highest-risk security gap is **backup creation/download without re-authentication or audit**. A stolen instance-admin session can download the complete database (all workspaces, password hashes, encrypted secrets) with one GET, and the action leaves no audit event.
- Closed months are not fully closed: **rescheduling an occurrence out of a closed month** and **skipping an income parent whose tax child is in a closed month** both mutate closed-month rows without an owner reopen.
- Installment plans drift: linked valuation ignores `as_of`, undo/delete of a posted installment does not restore the count, auto-post never decrements, and an overdue installment cannot be posted through “post next”.
- Polish 1.0 has a visible release blocker: the portfolio risk table calls `gettext("")`, which returns the whole `.po` header for Safe/Growth, producing a wall of `Project-Id-Version…` text. The initial suspicion about the 5+ plural form was checked and withdrawn: Polish 5+ quantity phrases take neuter singular, so “7 pozycji nie miało… nie zostało…” is correct.
- The Reports page is now the weakest page in the product: 7,153 px desktop / 7,526 px phone; on an empty workspace it renders **35 zero rows and 35 dash rows** instead of an empty state; the new net-worth/portfolio tables repeat the same “7 items had no value” warning 22 times; and the phone filters push the first chart below the first viewport.
- The Transactions page’s only “Add” control is at y=1,590 (desktop) / y=2,027 (phone), with no header action. The most common job in the app is below the fold on a populated month.
- Mobile tables are a cross-cutting weak point: the admin-users page scrolls horizontally at the page level (549 px content in a 390 px viewport), the Month summary hides the Difference column on phones, and several wide tables have no keyboard-focusable scroll region or visible hint.
- Demo data contradicts itself in ways a first-time user will notice: net-worth liabilities are €177,790 while the Loans page says €292,074.45; the demo creates tax rules and tax transactions but leaves the Taxes feature off, so the Tax page 404s and the income tax link is hidden.
- The “Other” category and the grouped “Other” donut slice have the same legend label and table name, distinguished only by colour — a direct violation of “do not use colour alone.”
- Charts clip the last x-axis label (`Nov 19, 2` on the Home net-worth widget and history page), including the new report series.
- The core flows themselves are strong: add/edit/delete/undo, amount parsing, month close/reopen, viewer read-only enforcement, IDOR scoping, CSRF/Origin, invitation acceptance, archive round-trip, backup creation, CSV export and the standard portfolio/loan/FIRE math passed. The current-balance checkpoint (COR-12), installment-plan lifecycle (COR-13–15), FIFO average cost (COR-16) and historical LTV (COR-17) are the exceptions.
- The biggest product gaps are **accounts/reconciliation**, **split transactions**, **bank CSV import with mapping** (already backlog #45), **multi-currency ledger entry**, and a **trash view** for soft-deleted rows.
- Do not rewrite the app. Fix the P1/P2 items above, add the mobile table pattern and the reports disclosures, then use the missing-capability table to plan the next minor.

---

## 3. Top 10 recommendations (impact ÷ effort)

1. **Make `gettext("")` return an empty string and stop translating empty subtitles** (S). One-line wrapper guard plus the portfolio call site; fixes the Polish risk-table release blocker.
2. **Pass the archive currency into `_restore_m3_configuration`** (S). Fixes silent 100×/1000× fixed-tax corruption on restore; add JPY and KWD tests.
3. **Require re-authentication and write an audit event for backup creation/download** (M). This is the only whole-database exfiltration path that does not ask for a password.
4. **Fix the current-balance loan checkpoint and the installment-plan lifecycle** (M): use the earliest checkpoint/loan start for the rate timeline and IO clock; make plan counts derived or symmetric across post/undo/delete/auto-post; allow overdue installments to post. This covers the P1 loan break and the three P2 plan drifts.
5. **Check both months on occurrence mutations** (S/M): reschedule must check the old due month; skip must check every child’s month; unskip must reject paused items and archived income sources.
6. **Turn the Reports page from a wall of tables into a report** (M): one empty state on an empty workspace, section-level warnings, table disclosures for the series, and collapsed filters on phones.
7. **Give Transactions a top “Add entry” control or a sticky entry row** (S/M). Keep the chronological row at the end, but make the primary action reachable in the first viewport.
8. **Adopt one mobile-table pattern** (M): card rows for the admin users table, a focusable scroll region with a visible hint for wide tables, and a compact Difference value under Actual on the Month summary.
9. **Reconcile the demo data and the “Other” labels** (S/M): link the mortgage/car loan to the Loans page (or use consistent balances), enable Taxes where tax data exists, and label the grouped slice “Other (combined)”.
10. **Fix Polish, accessibility, charts and release docs** (S/M): the risk-table header, a skip link, the confirm-dialog name, setup focus, the last x-axis label, the Month h1, the Reports screenshot, and merge main into the release PR.

---

## 4. Findings by category

### 4.1 Index

| ID | Sev. | Conf. | Area | Finding |
|---|---|---|---|---|
| COR-01 | P1 | High | Restore | Fixed tax amounts restored in the target’s stale currency (100×/1000× wrong or abort) |
| COR-02 | P2 | High | Closed months | Rescheduling an occurrence out of a closed month changes the closed month |
| COR-03 | P2 | High | Closed months | Skipping an income parent deletes tax children in a closed month |
| COR-04 | P2 | High | Recurring | Unskip resurrects pending dates for paused items and archived income sources |
| COR-05 | P3 | High | Domain | `SummaryBlock.difference` includes unplanned categories; the property test enshrines it |
| COR-06 | P3 | High | Month/Home | “No plan” bars are coloured as over plan |
| COR-07 | P3 | High | Ledger | HTMX delete offers Undo for a future-dated tombstone that undo always refuses |
| COR-08 | P2 | High | Demo | Net-worth liabilities (€177,790) do not reconcile with Loans (€292,074.45) |
| COR-09 | P2 | High | Demo | Demo creates tax rules/transactions but leaves Taxes off; the Tax page 404s |
| COR-10 | P2 | High | Restore | Soft-deleted rows and reopened month states block “restore into an empty workspace” |
| COR-11 | P2 | High | Charts | Category “Other” and grouped “Other” share a label; colour is the only distinction |
| COR-12 | P1 | High | Loans | Current-balance loan becomes unbuildable after a later balance checkpoint |
| COR-13 | P2 | High | Installments | Linked plan valuation ignores `as_of`; history is flat or zero |
| COR-14 | P2 | High | Installments | Undo/delete of a posted plan installment does not restore the count; auto-post never decrements |
| COR-15 | P2 | High | Installments | Overdue plan installment cannot be posted, or the next overdue row is skipped |
| COR-16 | P2 | High | Investments | FIFO `Holding.avg_cost` is 0 or stale until a sale |
| COR-17 | P2 | High | Loans/LTV | Historical property LTV ignores archive date and `estimate_between`; a test encodes the archive bug |
| COR-18 | P3 | High | Performance | `xirr()` returns an arbitrary 10% for same-date equal cashflows |
| COR-19 | P3 | Medium | Net worth | Zero-total categories are dropped; the test contradicts the spec |
| UX-01 | P1 | High | Transactions | The only Add control is at the bottom of a populated month; no header action |
| UX-02 | P2 | High | Month | The Difference column is hidden on phones; the variance amount disappears |
| UX-03 | P1 | High | Admin users | The page scrolls horizontally on phones; status/actions are cut off |
| UX-04 | P2 | High | Tables | Wide tables lack a keyboard-focusable scroll region or visible scroll hint |
| UX-05 | P2 | High | Upcoming | Skip/post reload to the top; the notice is visible but the user loses their place |
| UX-06 | P1 | High | Reports | Empty workspace renders 35 zero rows + 35 dash rows instead of an empty state |
| UX-07 | P1 | High | Reports | Phone filters (757 px) push the first chart to y=1,103 in an 844 px viewport |
| UX-08 | P2 | High | Reports | Net-worth rows repeat “7 items had no value” 22 times; the page grows by ~1,500 px |
| UX-09 | P2 | High | Reports | New series tables are always visible; every other report chart hides its table |
| UX-10 | P2 | High | Reports | 7,153 px desktop / 7,526 px phone; no section navigation |
| UX-11 | P2 | High | Charts | Last x-axis label is clipped (`Nov 19, 2`) on line charts |
| UX-12 | P2 | High | Page identity | Month’s h1 is the period (“November 2026”), not “Month” |
| UX-13 | P2 | High | Charts | Portfolio report “Gain” column does not follow the gain/loss colour convention |
| UX-14 | P3 | High | Empty Home | “Missing data — Nothing needs attention” on a brand-new workspace |
| UX-15 | P3 | High | Reports | Non-base-only/undated-only series vanish without the module warning |
| UX-16 | P3 | High | FIRE | 51-row year-by-year table with no collapse/pagination |
| UX-17 | P3 | High | Home | “Customise” is at the very bottom of a 3,792 px dashboard |
| A11Y-01 | P2 | High | Dialogs | The generic confirm dialog has no accessible name (`aria-labelledby`) |
| A11Y-02 | P2 | High | Navigation | No skip link; 15 tabs reach the first main-content link |
| A11Y-03 | P1 | High | Polish | Risk table renders the `.po` header for Safe/Growth |
| A11Y-05 | P3 | High | Setup | Wrong setup token leaves focus on `<body>`, not the invalid field |
| A11Y-06 | P3 | High | Setup/i18n | Setup “Language” sets the workspace only; the admin UI stays in browser language |
| SEC-01 | P1 | High | Backups | Backup creation/download bypass re-auth and audit; whole-DB exfiltration |
| SEC-02 | P2 | High | Auth | Successful sign-ins count toward the per-email throttle; 5 logins/hour lock the account |
| SEC-03 | P3 | High | Logging | Decoded newlines in the request path forge text-log lines |
| SEC-04 | P3 | High | Sessions | Expired/idle sessions are never pruned; metadata accumulates |
| SEC-05 | P3 | High | Demo | Demo import uploads have no demo restriction or aggregate quota |
| SEC-06 | P3 | High | Config | Broad `TRUSTED_PROXIES` CIDRs are accepted silently when proxy auth is off |
| SEC-07 | P3 | High | Config | Operator-supplied `SECRET_KEY` has no minimum length |
| SEC-08 | P3 | High | Admin | Demo reset/delete are unconfirmed, unlike the documented admin POST rule |
| OPS-01 | P2 | High | Home | Net-worth widget 500–600 ms; 12 separate widget requests per Home load |
| OPS-02 | P2 | High | Reports | Phase B reads all history: 42 SQL vs 6, 73 ms vs 9 ms on the demo |
| OPS-03 | P3 | High | Tests | Benchmark budgets allow 3–4× the specified performance |
| DOC-01 | P2 | High | Release | `main` README says “pre-alpha”; release branch is behind main; #397 must merge it |
| DOC-02 | P3 | High | Docs | Reports screenshot predates F10.4/F10.5; the new sections are missing |
| DOC-03 | P3 | High | Security docs | Checklist 5.2.6 says provider URLs are user-configured; they are hardcoded |
| DOC-04 | P3 | High | Security docs | Checklist 11.1.3/12.1.1 imply aggregate import quotas; only per-file caps exist |
| DOC-05 | P3 | High | Docs/code | `rendering.py` says hidden destinations remain reachable; Taxes 404s when off |
| DOC-06 | P3 | High | Docs | Backlog “Public read-only demo” differs from the shipped writable demo mode |
| PA-01 | P2 | High | Prior audit | Net-worth Add item dialog still exposes 14 fields; Save is below the scroll |
| PA-02 | P2 | High | Prior audit | Notification “Daily at”/“Remind days” are still always shown |
| PA-03 | P3 | High | Prior audit | Net-worth summary repeats assets/liabilities three times |
| PA-04 | P2 | High | Prior audit | “Breakdown”/“Value history” disclosures are 16 px high on phones |

### 4.2 Correctness and data integrity

#### COR-01 — P1 — Archive restore parses fixed tax amounts in the stale target currency

**Verified:** code review + temporary restore probe. **Route:** `POST /workspace/data` (restore mode). **Feature:** F4.4 / M3 tax versions.

**What happens.** `_apply_restore` changes the workspace’s base currency from the archive (`src/assetboard/services/import_archive.py:3155-3161`) and then calls `_restore_m3_configuration` (`:3197`). That function reads `currency = ctx.workspace.base_currency` (`:2456`). `Context.workspace` is an immutable `WorkspaceRef` built at request start, so it still holds the **target’s old currency**. Fixed tax versions are then parsed with it (`:2495-2499`).

A temporary restore probe confirmed that a JPY archive with a fixed tax of `1000` restored into an empty EUR workspace stores `fixed_minor=100000` — a 100× error. A KWD archive (three decimals) into a two-decimal target can raise `too_many_decimals` and abort the restore. The restore reports success in the first case.

**Why it matters.** This is silent data corruption in the documented backup/restore path. The user restores a backup and gets wrong tax payments for every period that uses the fixed version.

**Fix.** Pass `archive.rows.workspace["base_currency"]` (or re-read the workspace row after the settings update) into `_restore_m3_configuration` and use it for `fixed_amount`. Do not use `ctx.workspace` after changing the workspace settings.

**Tests.** Restore a JPY archive with a fixed tax version into an empty EUR workspace; assert the stored `fixed_minor` is unchanged. Add a KWD three-decimal case. Assert the restore plan/report shows the archive currency.

**Effort:** S. **Related:** M4 F4.4, M3 F3.3.

#### COR-02 — P2 — Rescheduling an occurrence out of a closed month changes the closed month

**Verified:** code review + temporary probe. **Route:** `POST /upcoming/{id}/reschedule`. **Feature:** F3.2 / F2.5.

`reschedule_occurrence` checks only the **new** date’s month (`src/assetboard/services/occurrences.py:414-435`, `_ensure_open(session, ctx, day)` at `:429`). It never checks the occurrence’s current `due_date` month. Post, skip, edit-amount and undo-post all check the month they touch; reschedule is the gap. A pending occurrence left in a month that was later closed can be moved to an open month, which updates/deletes the expected row in the closed month without a reopen. A temporary probe confirmed the reschedule succeeds.

**Fix.** Call `_ensure_open(session, ctx, occurrence.due_date)` before changing the row, and map the error to `month_closed`.

**Test.** Close the occurrence’s month, reschedule to an open month, assert `OccurrenceError("month_closed")` and no change to `due_date`/`rescheduled_at`.

**Effort:** S. **Related:** F2.5, F3.2.

#### COR-03 — P2 — Skipping an income parent deletes tax children in a closed month

**Verified:** code review + temporary probe. **Route:** `POST /upcoming/{id}/skip`. **Feature:** F3.2/F3.4.

`_skip_in_session` checks the parent’s month (`src/assetboard/services/occurrences.py:357`) but deletes every pending child without checking the child’s month (`:358-360`). With the parent due in January (open) and a VAT child due in February (closed), a temporary probe confirmed that skipping the parent deletes the February child. The closed month’s planned tax payment disappears.

**Fix.** Before deleting any child, call `_ensure_open(session, ctx, child.due_date)`; if any child month is closed, raise `OccurrenceError("month_closed")` and change nothing. Map the failure in the route.

**Test.** Parent open, child in a closed month; skip; assert the error and both rows unchanged.

**Effort:** S. **Related:** F3.2/F3.4, F2.5.

#### COR-04 — P2 — Unskip resurrects pending dates for paused items and archived income sources

**Verified:** code review + temporary probes. **Routes:** `POST /upcoming/{id}/unskip` and the ledger’s skipped-row Undo. **Feature:** F3.2.

`unskip_occurrence` rejects an archived **scheduled item** but not `item.paused_at` (`src/assetboard/services/occurrences.py:392-395`), and for income occurrences it does not check whether the income source is archived. `_sync_work_month` also never checks `source.archived_at` (`src/assetboard/services/income.py:496-551`). Probes confirmed that skip → pause → unskip and skip → archive source → unskip both return the occurrence to `pending`.

**Why it matters.** Paused rules and archived income sources are supposed to have no pending dates. Undo can bring one back on Upcoming/Month/Home, and a payment can be posted from an archived source while its category is still active.

**Fix.** In `unskip_occurrence`, reject when the item is archived **or paused**, when the income source is archived, and optionally when the category is archived. Keep the row skipped and show a clear message.

**Test.** Skip → pause → unskip and skip → archive source → unskip both fail with the row still `skipped`.

**Effort:** S. **Related:** F3.1/F3.2.

#### COR-05 — P3 — `SummaryBlock.difference` includes unplanned categories; the property test enshrines it

**Verified:** code review. **Feature:** F2.4.

`summarize_month` computes each block’s difference from the block totals (`src/assetboard/domain/summary.py:401-412`), including categories with actuals but no plan. The spec says a group’s total Difference adds only categories with a plan and is empty when none has one (M2 F2.4, owner 2026-11-01). The web view already implements the correct rule independently (`src/assetboard/web/plan.py:505-518`), and the property test asserts the wrong rule (`tests/unit/test_summary_domain.py:281`).

**Impact.** No visible impact today because the web view does not consume the field, but any future consumer gets a wrong total and the test protects it.

**Fix.** Sum only rows with `planned.amount_minor > 0` (or remove the field). Update the property/golden tests.

**Effort:** S. **Related:** F2.4.

#### COR-06 — P3 — “No plan” bars are coloured as over plan

**Verified:** code review. **Feature:** F2.4.

`_bar` sets `over = actual > planned` even when planned is zero (`src/assetboard/web/plan.py:298-306`), and the templates use `b.over` for the fill without gating on `has_plan` (`src/assetboard/web/templates/months/_plan.html:216,335`). A month with spending but no plan shows a red Out bar (or a green In bar) while the text says “(no plan)”.

**Fix.** Use `over = planned > 0 and actual > planned`, or gate the fill on `b.has_plan`.

**Test.** In the existing no-plan bar test, assert the actual fill is `fill-primary`, not `fill-danger`/`fill-success`.

**Effort:** S. **Related:** F2.4.

#### COR-07 — P3 — HTMX delete offers Undo for a future-dated tombstone that undo always refuses

**Verified:** code review. **Route:** `POST /m/{month}/transactions/{id}/delete`. **Feature:** F2.2/issue #164.

The HTMX delete response always builds an Undo toast from `deleted.undo_until` (`src/assetboard/web/transaction_routes.py:536-552`), including future-dated rows. `undo_delete` always raises `UndoFutureDated` for those (`src/assetboard/services/transactions.py:866-870`). The no-JS path uses `pending_undo`, which correctly returns `None`.

**Fix.** Suppress `toast_undo` when the deleted row’s date is after today (the route already has `row`), or expose an explicit `can_undo` from the service.

**Test.** Restore/create a future-dated actual row, delete over HTMX, assert no undo toast; the row stays soft-deleted.

**Effort:** S. **Related:** F2.2, issue #164.

#### COR-08 — P2 — Net-worth liabilities do not reconcile with the Loans page

**Verified:** browser + database. **Routes:** `/`, `/net-worth`, `/loans`.

The Home page shows **net worth liabilities €177,790.00** and a separate **Loans total €292,074.45**. The net-worth liabilities are manual items: Mortgage €176,920 (as of Oct 31) and Credit card €870 (`/net-worth?side=liability`). The Loans page has a car loan €14,583.33, a mortgage €277,491.12 and a renovation loan with no schedule. The net-worth mortgage is not linked to the loan; the car loan is not in net worth at all. The difference is €114,284.45.

**Why it matters.** The demo is the product’s first impression and the README/gallery screenshots show both widgets. A user sees two different liability totals on the same Home page and cannot tell which is right. The linked-valuation feature exists (`loan_balance` links) but the demo does not use it.

**Fix (demo data).** Link the net-worth Mortgage to the mortgage loan’s remaining principal, add the car loan as a linked liability (or use consistent manual balances), and either give the renovation loan a schedule or exclude it from both sides. Add a demo consistency test that the net-worth liabilities equal the linked loan balances (plus any manual liabilities) on the demo reference date.

**Effort:** S/M. **Related:** M5 F5.9, M7, F9.11a.

#### COR-09 — P2 — Demo creates tax data but leaves Taxes off; the Tax page 404s

**Verified:** browser + database. **Routes:** `/income`, `/taxes`, `/workspace/settings`.

The demo workspace has `taxes_enabled=0` (database), but the demo builder creates a tax rule, a tax-rule version, an income-source tax attachment and an “Income tax: Hourly consulting” transaction. `rendering.py:166` hides the Taxes nav item; `tax_routes._require_taxes` returns 404 (`src/assetboard/web/tax_routes.py:75-78`); the Income page hides the “Taxes on this income” link (`income/sources.html:181`). The user sees tax spending but no way to inspect the rule.

**Why it matters.** The demo guide (`docs/demo-mode.md:111`) promises an attached tax rule, and the ledger shows tax payments, but the feature is invisible. This is a credibility gap in the showcase.

**Fix.** Enable `taxes_enabled` for the demo workspace in the demo builder (or seed no tax data). Add a demo test that every feature with seeded data is visible, and that the Tax page renders.

**Effort:** S. **Related:** F9.11a, M3 F3.3/F3.4.

#### COR-10 — P2 — Soft-deleted rows and reopened month states block restore into an “empty” workspace

**Verified:** browser + service probe. **Route:** `/workspace/data` (restore mode). **Feature:** F4.4.

`restore_available` uses `workspace_is_empty`, whose `EMPTINESS_CHECKS` include `_has_transactions` and `_has_month_states` (`src/assetboard/services/archive.py:561-605`). `TransactionRepository.any_exists` explicitly includes soft-deleted rows (`src/assetboard/db/repositories/transaction.py:483-486`). A workspace with four soft-deleted test entries and one closed/reopened month has **0 active transactions** but restore is disabled; the radio is greyed out with only “Available only in an empty workspace” and no reason.

**Why it matters.** A user who tries the app, deletes a test entry and closes/reopens a month cannot restore a real archive into that workspace without creating a new one. The app offers no trash view or purge, so there is no visible way to become “empty” again.

**Fix.** Count only active rows (`deleted_at is null`) for restore availability, ignore month states with no closed month, or explain the exact blockers and offer a purge. If tombstones must block restore, say so in the UI and docs.

**Test.** A workspace with one soft-deleted transaction and an open month state still offers restore (or the UI lists the blocker); a workspace with an active transaction does not.

**Effort:** S/M. **Related:** F4.4, overview §3.9.

#### COR-11 — P2 — Category “Other” and grouped “Other” share a label; colour is the only distinction

**Verified:** browser + HTML. **Routes:** `/`, `/m/{month}`, report category donuts.

The suggested category “Other” and the grouped tail slice are both rendered with the name “Other” (`src/assetboard/web/charts.py:44`). The Home/Month donut legend shows “Other 2.5%” (the real category, red) and “Other 9.8%” (the grouped slice, grey); the accessible table has the same two names. The reports category chart uses “Other (combined)” for the grouped series, so the app is inconsistent with itself.

**Why it matters.** The user cannot tell which “Other” is the category and which is the group without relying on colour, which violates the “do not use colour alone” rule and makes the legend/table ambiguous.

**Fix.** Label the grouped slice “Other (combined)” or “Other categories” (and include the grouped count, e.g. “Other (7 categories)”) in the donut view, legend and accessible table. Keep the real category as “Other”. Reuse the report naming.

**Test.** A month with a category literally named “Other” plus a grouped tail renders two distinct labels in the legend and table.

**Effort:** S. **Related:** F2.7, F10.3.

#### COR-12 — P1 — A current-balance loan becomes unbuildable after a later balance checkpoint

**Verified:** code review + throwaway-workspace probe. **Routes:** `/loans/{id}`, `/loans/{id}/payments`, net worth, FIRE, property LTV. **Feature:** F7.5/F7.6.

`LoanView.balance_date` is the **latest** checkpoint (`src/assetboard/services/loans.py:697`), and both `_view_segments()` and `rate_timeline()` use `view.origination_date or view.balance_date` as the rate-timeline start (`:439`, `:456`). The initial fixed-rate `LoanRateChange` was created at the **original** balance date, so `build_rate_timeline()` raises `rate_change_before_start` (`src/assetboard/domain/loans.py:292-293`).

**Repro.** A current-balance fixed loan with balance date 2026-01-01; add a checkpoint 2026-06-01. `loan_schedule(...)` returns `error_code == "rate_change_before_start"`; `loan_balance_at(...)` returns `Unavailable`; a linked liability disappears from net worth, the FIRE loan resolver returns `SCHEDULE_ERROR`, and the property LTV is unavailable. The terms editor also refuses to edit a current-balance loan once it has more than one checkpoint (`services/loans.py:1500`). No test covers a current-balance loan with a second checkpoint.

**Why it matters.** Adding a bank-statement checkpoint is a headline F7.5 feature. For every current-balance fixed-rate loan it breaks the loan page, schedule, CSV, checkpoint variance, linked net-worth liability, property LTV and FIRE input.

**Fix.** Use the **earliest** checkpoint (or a stored loan start) as the rate-timeline start, keep `balance_date` as the latest for display/current balance, preserve the interest-only clock from the loan start, and relax the `len(checkpoints) != 1` terms-edit refusal. Add a regression test that creates a current-balance fixed loan, adds a later checkpoint, and asserts the schedule, checkpoint variance, `loan_balance_at`, terms editing and FIRE link still work.

**Effort:** M. **Related:** F7.5/F7.6, F5.9, F10.4, F8.

#### COR-13 — P2 — Installment-plan linked valuation ignores `as_of`; history is flat or zero

**Verified:** code + throwaway-workspace probe. **Routes:** linked `Installments` liability, net-worth history, property LTV. **Feature:** F7.6/F7.7.

The plan branch of `loan_balance_at` ignores `as_of` (`src/assetboard/services/loan_links.py:94-98`) and calls `plan_remaining_minor_in_session()`, which returns `installment × current remaining_count` (`src/assetboard/services/installment_plans.py:241-250`). A closed/archived plan returns 0 for every historical date. A 3 × 100,000 plan created in February with two installments posted in February and March shows 100,000 on 2026-02-28, 2026-03-31 and 2026-06-15; 2026-02-28 should be 200,000 and 2026-03-31 onwards 100,000. The net-worth trend and F10.4 report therefore show a flat, wrong liability.

**Fix.** Add an `as_of`-aware plan balance helper: derive the original count from `remaining_count + posted_count` (or store it), subtract posted occurrences with `due_date <= as_of`, clamp to zero and return 0 before creation/after closure. Keep `plan_remaining_minor_in_session()` for current UI/scheduling.

**Test.** Plan with posts before and after `as_of`, plus a closed plan; assert the linked value at each date and the resulting net-worth history.

**Effort:** M. **Related:** F7.6/F7.7, F5.9, F10.4.

#### COR-14 — P2 — Undo/delete of a posted plan installment does not restore the count; auto-post never decrements

**Verified:** code + throwaway-workspace probe. **Routes:** Upcoming/Month undo, ledger delete/restore. **Feature:** F7.7.

`apply_posted_occurrence()` decrements `remaining_count` on post (`src/assetboard/services/installment_plans.py:419`), but `undo_post()` (`src/assetboard/services/occurrences.py:491-520`) and `unlink_deleted_transaction()` (`:549-566`) only return the occurrence to pending; they never reverse the plan count. `relink_restored_transaction()` (`:577-603`) sets it back to posted without re-decrementing. The scheduler auto-post path also never calls `apply_posted_occurrence()` because it passes no context (`src/assetboard/services/occurrences.py:276-291`).

**Repro.** Plan `remaining_count=3`; post one → 2/1; undo → 2/0 (should be 3/0); re-post → 1/1 (should be 2/1). The plan liability, progress %, original count and payoff date are understated; repeated undo/re-post drifts the debt. The same understatement happens when the posted transaction is deleted from the ledger page, and an auto-posted plan never decrements.

**Fix.** Prefer deriving the count from posted occurrences; otherwise add symmetric `revert_posted_occurrence()`/`reapply_posted_occurrence()` calls from undo, unlink, relink and auto-post.

**Test.** post → undo → re-post; post → ledger delete → restore; auto-posted plan; assert count, progress and total remaining after each step.

**Effort:** M. **Related:** F7.7, F3.2.

#### COR-15 — P2 — Overdue plan installment cannot be posted, or the next overdue row is skipped

**Verified:** code + throwaway-workspace probe. **Route:** “Post next installment” / Upcoming posting. **Feature:** F7.7.

`apply_posted_occurrence()` computes `next_due` from the **posted occurrence’s due date** (`src/assetboard/services/installment_plans.py:420`) and calls `create_loan_schedule_item_in_session()`, which computes `next_due_on_or_after(today)` and raises `loan_paid_off` when `plan.end_date < next_due` (`:260-269`). The whole post transaction rolls back.

**Repro.** Plan `remaining_count=2`, payment day 5, first due 2026-02-05, created 2026-02-01; today 2026-03-10 (both February and March overdue). `post_next_installment()` (or posting the February occurrence) raises `LoanError("loan_paid_off")`; the plan stays at 2. With three installments, posting the first overdue row can skip the March occurrence because regeneration starts from today rather than the earliest pending due date.

**Fix.** Regenerate from the earliest pending/overdue occurrence (or from `posted_on` while preserving overdue rows), and do not treat a remaining count > 0 as paid off solely because the nominal next date after today is past `end_date`.

**Test.** Two/three installments all overdue; post the earliest twice; assert success, count decrement, remaining overdue row still postable, and final closure at 0.

**Effort:** M. **Related:** F7.7, F3.2.

#### COR-16 — P2 — FIFO `Holding.avg_cost` is 0 or stale until a sale

**Verified:** code + throwaway-workspace probe. **Route:** instrument page “Average purchase cost”. **Feature:** F6.2/F6.6.

On a buy, `average_cost` is only computed in the **average** branch (`src/assetboard/domain/holdings.py:384-397`). The FIFO branch appends a lot but leaves `average_cost` at its initial `Decimal(0)`; it is only recomputed on a sell (`:451-461`), and a later buy leaves the old value. Two buys of 10 @ 100 and 10 @ 200: average method 150.00, FIFO 0. A buy 10@100, sell 5, buy 10@200 leaves FIFO at 100 instead of 166.67.

**Impact.** The instrument history summary (`web/.../_instrument_history_body.html` uses `history.holding.avg_cost`) shows 0.00 or a stale average for every FIFO portfolio until a sale; the portfolio page’s average cost is calculated separately, so portfolio totals are unaffected.

**Fix.** For FIFO, compute `average_cost = cost_basis_base / quantity` at the end of `_compute_instrument_rows()`; keep the average method’s documented “unchanged by a partial sale” behavior.

**Test.** Buy-only FIFO avg cost, buy-sell-buy FIFO, split then buy, quantity 0.

**Effort:** S. **Related:** F6.2/F6.6.

#### COR-17 — P2 — Historical property LTV ignores the item’s archive date and `estimate_between`

**Verified:** code + existing test. **Routes:** `/loans/{id}?property_as_of=…`, `/loans/{id}/settings`. **Feature:** F7.6.

`loan_property_view()` treats `item.archived_at is not None` as archived for **every** `as_of` (`src/assetboard/services/loan_links.py:432-444`) instead of comparing the workspace-local archive date with `as_of`. It calls `current_values(...)` (`:458-465`), which calls `values_at()` with the default `backfill=False` (`src/assetboard/domain/networth_history.py:210-217`, `src/assetboard/services/networth.py:464-488`), so `estimate_between` is never applied to the historical property value. `tests/unit/test_loan_links.py:399-402` archives the property at the test clock (2026-03-01), requests 2026-01-01, and asserts `property_archived` — exactly opposite to F5.3 (“counts up to its archive date”). There is no test for `estimate_between=True`.

**Impact.** After archiving a property, all historical LTV/equity views become “unavailable”; with estimates on, historical LTV uses the last recorded value instead of the interpolated one, overstating collateral and understating LTV.

**Fix.** Compute the archive date with `archived_on(item.archived_at, timezone)` and return `property_archived` only when `archive_date <= as_of`; add a `backfill` flag to `current_values()` and use it for historical LTV (`as_of < today`). Update the test to archive after the requested date and add an estimate case.

**Test.** Property archived after `as_of` returns a value; archived on/before `as_of` is unavailable; `estimate_between` gives the expected interpolation at an arbitrary `as_of`.

**Effort:** M. **Related:** F7.6, F5.3/F5.6.

#### COR-18 — P3 — `xirr()` returns an arbitrary 10% for same-date equal cashflows

**Verified:** code. **Feature:** F6.7.

`xirr()` starts at `rate = 0.1` and returns it as soon as `abs(npv(0.1)) < tolerance` (`src/assetboard/domain/performance.py:485-489`). For a start and end value on the same date, `npv` is zero for every rate, so it returns 0.1 (10%). The performance table masks this because `days == 0` makes `period_return(..., 0) == 0` (`src/assetboard/services/performance.py:430-434`), but `PerformancePeriod.xirr` and direct callers still see 10%.

**Fix.** Detect “all cashflows on one date” before Newton and return `XirrResult(None, XirrFailure.NO_SOLUTION)` (or a defined 0 for an explicitly zero-day period).

**Test.** Two equal same-date flows; YTD period on 1 January; zero-day period.

**Effort:** S. **Related:** F6.7.

#### COR-19 — P3 — Zero-total categories are dropped; the test contradicts the spec

**Verified:** code + spec. **Feature:** F5.2.

The M5 spec says “Categories with no counted item are left out. **Zero-valued counted items remain in the table but have no share.**” `category_rows()` filters `amount != 0` (`src/assetboard/domain/networth_summary.py:101-106`), so a category containing only a zero-valued counted item is omitted. `tests/unit/test_networth_summary_domain.py:100-112` explicitly asserts that the zero category disappears — the opposite of the spec.

**Impact.** A zero-valued item (e.g. a written-off vehicle recorded at 0) remains in the Assets list but disappears from the Summary category table; the spec’s “no share” state is never shown. If the spec meant the item table, this is a spec/test mismatch to clarify.

**Fix.** Keep zero categories with `share_tenths=None` and update the test, or amend the spec to say zero-total categories are omitted.

**Test.** Zero-valued counted item alone and next to a real category.

**Effort:** S. **Related:** F5.2.

### 4.3 UI/UX

#### UX-01 — P1 — The only Add control on Transactions is at the bottom of a populated month

**Verified:** browser measurement. **Route:** `/transactions/{month}`.

At 1440×900, the expense entry row is at **y=1,590**; at 390×844 it is at **y=2,027**. The page has no `header_actions`; the first viewport contains rows but no way to add one. The income entry row is similarly below the fold. The UI guideline says the first viewport must contain the primary action (`docs/ui-guidelines.md:58-60`) and the interaction matrix puts “Add a ledger entry” at the end of the chronological table (`:199-201`). Both can be satisfied with a top “Add entry” control that focuses/scrolls to the existing row, or a sticky entry row.

**Why it matters.** Adding an entry is the most common job. On a populated month the user must scroll 1,590–2,027 px before they can start, which defeats the “spreadsheet-fast” principle.

**Fix.** Add a header action “Add entry” (and an income variant) that scrolls the entry row into view and focuses the Day field, or make the entry row sticky at the bottom of the table/viewport. Keep the chronological row at the end. Add a keyboard shortcut (e.g. `n`) if cheap.

**Test.** Browser test at 390 and 1440: the first viewport contains an “Add entry” control; activating it focuses `entry-expense-day` and the row is in view.

**Effort:** S/M. **Related:** F2.2, F9.9, UI guideline §2/§5.

#### UX-02 — P2 — The Difference column is hidden on phones; the variance amount disappears

**Verified:** browser + template. **Route:** `/m/{month}`.

`months/_summary.html:20` gives the Difference header `max-sm:hidden` and the cells are hidden with it. At 390 px the user sees Plan and Actual but not the variance; only the row tint and “over plan if everything pending arrives” text remain. The guideline says never hide critical columns without a visible hint (`docs/ui-guidelines.md:136-139`).

**Fix.** Show the difference as a small second line under Actual (e.g. “+€34.17” / “−€1,218.40”), or allow the table to scroll with a visible hint. Keep the desktop column.

**Test.** At 390, the Taxes row shows the difference amount (or a visible hint), not just a colour.

**Effort:** S. **Related:** F2.4/F9.9.

#### UX-03 — P1 — The admin users page scrolls horizontally on phones; status/actions are cut off

**Verified:** browser measurement + screenshot. **Route:** `/admin/users`.

At 390 px, `document.documentElement.scrollWidth` is **549** (and 526 in the first crawl) while `clientWidth` is 390. The table is 601 px wide inside a 358 px `overflow-x-auto` section; the row menu (`details[data-popover] .row-menu`) extends to x=758. The screenshot shows the Status and Actions columns cut off, with no visible scroll affordance. `admin/users.html:11-12` uses `panel overflow-x-auto` and a plain `sheet` table; `input.css:253-260` makes the row menu static under 1024 px but it still expands the table/page.

**Why it matters.** An instance administrator on a phone cannot see user status or reach the row actions without guessing that the page scrolls sideways; the page itself moves, not just the table.

**Fix.** Use `sheet-sm` card rows for the users table at phone width, or keep the table inside a constrained scroll region with `min-w-0` on the section and a visible “scroll sideways” hint. Ensure the row menu stays within the viewport (open left, or render it below the row). Add a 390 px test asserting `scrollWidth == clientWidth` and that the Status/Actions cells are reachable.

**Effort:** M. **Related:** UI guideline §4 tables, F1.5.

#### UX-04 — P2 — Wide tables lack a keyboard-focusable scroll region or visible hint

**Verified:** browser + templates. **Routes:** Transactions, History, Month summary, Reports series/year overview, Investments, FIRE.

The FIRE projection table does this correctly: `fire/_table.html:10` wraps it in `role="region" aria-label="Year-by-year projection" tabindex="0"`. The Transactions and History tables use `div.relative.mt-3.overflow-x-auto` around `min-w-[40rem]` tables (`months/_ledger.html:172-173`, `history/list.html:201-202`) with no `role`, `tabindex` or `aria-label`; the Month summary hides Difference instead; the report series tables are 625/471 px inside 358 px. A keyboard-only user cannot scroll these regions, and a touch user gets no hint that the Actions column exists off-screen.

**Fix.** Apply the FIRE pattern (focusable, labelled region plus a visible “Scroll sideways” hint) or card rows at phone width. Do not hide critical columns without an inline second line.

**Test.** At 390 and 720 (200%), every wide table has either card rows or a focusable region with an accessible name; keyboard Tab can enter it and Arrow keys scroll.

**Effort:** M. **Related:** UI guideline §4 tables, F9.9.

#### UX-05 — P2 — Skip/post reload to the top; the notice is visible but the user loses their place

**Verified:** browser measurement. **Route:** `/upcoming`.

Skipping an item at scrollY=947 reloads to scrollY=0; the “Skipped. It no longer counts as pending. Undo” notice is at the top. The action is a normal POST, not an HTMX swap, and there is no `data-keep-visible`. The prior audit’s “silent skip” is fixed (there is a notice), but the scroll loss remains. The UI guideline says preserve scroll and open disclosures across HTMX swaps and give feedback in the current viewport (`docs/ui-guidelines.md:21-22,144-153`).

**Fix.** Use an HTMX swap that replaces the row/list and keeps scroll, or redirect to the item’s anchor and restore scroll. Keep the notice in the list or a toast.

**Test.** Browser test: scroll to a lower item, skip, assert the page does not jump to 0 and the notice/undo is visible.

**Effort:** S/M. **Related:** F3.2, prior UI audit P0.

#### UX-06 — P1 — Empty workspace Reports renders 35 zero rows and 35 dash rows

**Verified:** browser + HTML. **Route:** `/reports` on an empty workspace.

The page is **4,267 px** tall. The “Income vs expenses” table has **35 rows of €0.00** and the “Savings rate” table has **35 rows of “—”**, despite the messages “No earned, spent or saved amounts in this range.” and “No savings rate for the charted entries.” The first chart is at y≈1,103 on phone. This is not an empty state; it looks like a broken report.

**Fix.** When the range has no ledger data, show one page-level `empty_state` (“No transactions in this range. Add an entry or change the years.”) and hide the zero/dash tables and charts. When there is partial data, keep the tables but put them behind “Show as table”.

**Test.** Empty workspace `/reports` has no table with more than a header/total row and shows an actionable empty state.

**Effort:** S/M. **Related:** F10.1/F10.2, UI guideline §4 empty states.

#### UX-07 — P1 — Phone filters (757 px) push the first chart below the viewport

**Verified:** browser measurement. **Route:** `/reports`.

At 390×844, the report filter form is **757 px tall** (from y=248 to y=1,005); the first chart starts at **y=1,103**. The first viewport contains only the title, workspace name and filters. The filter form is not a `data-filter-disclosure`, so `app.js:336-339` does not collapse it on phones. The guideline says filters belong below the title and must not push data off a phone screen (`docs/ui-guidelines.md:58-60,140-142`).

**Fix.** Put the filters behind the existing disclosure pattern (`data-filter-disclosure`) with an active-filter count, collapsed by default under 640 px; keep the primary “Show report” button visible. Add a 390 px test that the first chart/section is within the first viewport after load.

**Effort:** M. **Related:** F10.1, UI guideline §4 filters.

#### UX-08 — P2 — Net-worth rows repeat “7 items had no value” 22 times

**Verified:** browser + HTML. **Route:** `/reports`, “Net worth over time”.

The net-worth table has 24 rows; **22** of them contain the same “7 items had no value yet and were not counted.” warning inside the row header (the demo’s deliberate valuation gap). The section is 1,509 px tall; the table alone is 1,063 px; each row is 53 px instead of ~33 px. The same warning is also in the chart tooltips. The Summary page puts this warning in a collapsed per-point breakdown, not inline on every row.

**Fix.** Move the warning to a section-level note (once per distinct warning) or a per-row `<details>`/icon with the details, matching the Summary’s presentation. Keep the chart tooltips. If the warning changes between rows, show only the changes or a compact count.

**Test.** A net-worth report with a constant “no value” warning shows it once at section level (or once per changed row), not 22 times; the row header text is just the period.

**Effort:** S. **Related:** F10.4, M10 acceptance “same warning as the Summary”.

#### UX-09 — P2 — New series tables are always visible; every other report chart hides its table

**Verified:** browser + JS/template. **Route:** `/reports`.

The shared series macro renders `data-chart-table` but no `data-chart-table-toggle` (`src/assetboard/web/templates/reports/page.html:18-49`). `charts.js` only calls `revealReportTable` for `report-bars`/`report-category-bars`/`report-rate`/`report-trend` (`src/assetboard/static/js/charts.js:443-495`); the new sections use `line` and `performance`, so their 24-row tables stay visible. Every other report chart has a “Show as table” button and an `sr-only` table after the first draw.

**Fix.** Add the same toggle to the series macro and hide the table after draw (or use a `report-*` chart type), or wrap the table in a `<details>` with “Show as table”. This also removes ~1,900 px from the populated page and makes the two new sections consistent with the rest.

**Test.** After the chart draws, the series table is `sr-only` and a “Show as table” button is visible; without JS the table is visible.

**Effort:** S. **Related:** F10.4/F10.5, F10.1 chart conventions.

#### UX-10 — P2 — Reports is 7,153 px desktop / 7,526 px phone; no section navigation

**Verified:** browser measurement. **Route:** `/reports`.

The page stacks 10 sections (filters, income/expenses, savings rate, category over time, year overview, category trends, year-over-year, plan accuracy, net worth, portfolio). The new net-worth/portfolio sections are at y=4,315 and y=6,269 on desktop; on phone they are at y=4,740 and y=6,646. There is no in-page nav, sticky section menu or “jump to” control. The user who wants net worth must scroll past eight ledger sections.

**Fix.** Add a compact section nav (sticky on desktop, a select/scroll strip on phone) or reorder so the module-history sections sit near the top; combine with the table disclosures and empty-state fixes.

**Test.** A section nav lists all visible sections and each link focuses the section heading; on phone it fits one row or scrolls.

**Effort:** M. **Related:** F10.1–F10.5.

#### UX-11 — P2 — Last x-axis label is clipped on line charts

**Verified:** browser screenshots. **Routes:** Home net-worth widget, `/net-worth/history`, `/reports` net-worth/portfolio.

The Home net-worth widget shows `Nov 19, 2` at the right edge instead of `Nov 19, 2026`; the history page and the report series show the same clipping. The line options use `grid: { left: 8, right: 16, ... containLabel: true }` and `boundaryGap: false` (`src/assetboard/static/js/charts.js:126,234,311,390`). ECharts does not reserve space for the half-width of the last category label at the right edge.

**Fix.** Increase the right grid padding for line charts, set `axisLabel.alignMaxLabel: "right"` (ECharts 5) or add a small right boundary gap; test with long month names (`December 2026`, `grudzień 2026`).

**Test.** Visual/browser assertion that the last label is fully inside the canvas at 1440 and 390, in both locales.

**Effort:** S. **Related:** F5.9, F6.7, F10.4/F10.5.

#### UX-12 — P2 — Month’s h1 is the period, not “Month”

**Verified:** browser. **Route:** `/m/{month}`.

The h1 and document title are `November 2026` / `Listopad 2026`; the sidebar says “Month”. The guideline names the destination `Month` and puts the period in a subtitle or title block (`docs/ui-guidelines.md:40-49`). Home was fixed to `Home`; Month was not. A user cannot tell from the top of the page whether they are on Home or Month (the workspace name above the h1 can also be “Home” in the demo).

**Fix.** Use `Month` as the h1 and the period as a subtitle (`November 2026`), with document title `Month: November 2026 · AssetBoard` (or the existing `Transactions: November 2026` exception). Add a test asserting one h1 and the title.

**Effort:** S. **Related:** F2.3, UI guideline §2.

#### UX-13 — P2 — Portfolio report “Gain” column does not follow the gain/loss colour convention

**Verified:** browser + view model. **Route:** `/reports`, “Portfolio value over time”.

The report table’s “Gain” cells are plain text. The Performance page and portfolio holdings use `text-success-strong` for positive and `text-danger` for negative gains (`docs/ui-guidelines.md:132-134`). The series row model only carries formatted strings, so the sign is lost.

**Fix.** Carry the gain sign/amount in `ReportSeriesRow` (or a cell kind) and apply the same classes; keep zero neutral.

**Test.** A report with a positive and a negative gain renders the expected classes.

**Effort:** S. **Related:** F10.5, UI guideline §4 tables.

#### UX-14 — P3 — “Missing data — Nothing needs attention” on a brand-new workspace

**Verified:** browser. **Route:** `/` empty workspace.

The “Missing data” widget is shown on an empty workspace and says “Nothing needs attention.” There is no data to be missing, but the message implies everything is configured. Hide the widget when the workspace has no financial data, or change it to “Add an account, a value or an investment to see missing-data checks.”

**Effort:** S. **Related:** F9.1.

#### UX-15 — P3 — Non-base-only/undated-only series vanish without the module warning

**Verified:** code + tests. **Routes:** `/reports` net-worth/portfolio.

`net_worth_report` returns `None` when `not points or not data.has_values` (`src/assetboard/services/reports.py:359-389`). `has_values` is false when **all** items are non-base, so the section and any “non-base item” warning disappear. `portfolio_report` returns `None` when `not page.chart` (`:420-447`); `performance_page()` produces an empty chart when all live investment transactions are **undated**, so the section and the F6.7 undated note disappear. A user selecting a non-base currency, or a workspace whose portfolio was imported as undated legacy lots, sees nothing and cannot tell why; the Summary/Performance pages do show the warning.

**Fix.** Return the derived data with an explicit empty/notes state (or add a top-level report warning) when `has_recorded_values`/`undated_count` is non-zero, and render the existing Summary warning/undated note in the section. Keep the sections absent only when there is genuinely nothing to show.

**Test.** Only non-base net-worth items; only undated investment transactions; only excluded/no-value items; a non-base currency filter.

**Effort:** S. **Related:** F10.4/F10.5, F5.9/F6.7.

#### UX-16 — P3 — FIRE’s 51-row year-by-year table has no collapse or pagination

**Verified:** browser. **Route:** `/fire`.

The projection table is 51 rows (2026–2076) and dominates a 4,149 px desktop / 4,957 px phone page. It has a focusable scroll region (good), but no “show first 10 years / all years” or decade grouping. The FIRE page is a planning page; the table is useful but long.

**Fix.** Collapse to the first 10–15 years with a “Show all years” disclosure, or group by decade. Keep the chart and metrics visible.

**Effort:** S/M. **Related:** F8.

#### UX-17 — P3 — Home “Customise” is at the bottom of a 3,792 px dashboard

**Verified:** browser. **Route:** `/`.

The “Choose which widgets appear on Home and in what order. Customise” row is at the very bottom. The user must scroll the whole dashboard to change it. Put “Customise” in the user menu or a small header action, or make the footer sticky.

**Effort:** S. **Related:** F9.1.

### 4.4 Accessibility and i18n

#### A11Y-01 — P2 — The generic confirm dialog has no accessible name

**Verified:** template + browser. **All pages.**

`macros/components.html:114-126` renders `<dialog id="confirm-dialog" ... data-backdrop-static>` with an `<h2>` title but no `aria-labelledby` and no id on the heading. Every page-specific dialog (`item-dialog`, `channel-dialog`, `invite-dialog`, `rate-dialog`, etc.) has `aria-labelledby`; the shared confirm dialog does not. A screen reader announces an unnamed dialog.

**Fix.** Add `aria-labelledby="confirm-dialog-title"` and `id="confirm-dialog-title"` on the h2 (or pass an id). Add a test that every `<dialog>` has an accessible name.

**Effort:** S. **Related:** UI guideline §6, WCAG 4.1.2.

#### A11Y-02 — P2 — No skip link; 15 tabs reach the first main-content link

**Verified:** keyboard tab order. **All signed-in pages.**

Tab order on `/`: logo (1), 12 sidebar links (2–13), theme toggle (14), user menu (15), then the first main-content link (16). The `<main id="main-content">` already exists (`base.html:72`), but there is no “Skip to content” link and `main` has no `tabindex="-1"`.

**Fix.** Add a visually hidden “Skip to content” link as the first focusable element, targeting `#main-content`, and `tabindex="-1"` on `<main>`. Add a keyboard test.

**Effort:** S. **Related:** WCAG 2.4.1.

#### A11Y-03 — P1 — Polish risk table renders the whole `.po` header

**Verified:** browser + `gettext` probe. **Route:** `/investments/portfolio`, Polish.

`portfolio_routes.py:251` calls `gettext(RISK_TIER_SUBTITLES.get(row.tier, ""), locale)`. For Safe/Growth the subtitle is empty. `GNUTranslations.gettext("")` returns the catalog header; `assetboard.i18n.gettext("", "pl")` returns a **452-character** string starting `Project-Id-Version: AssetBoard…`. The risk table therefore renders the entire PO metadata as the subtitle for Safe and Growth; the phone page grows to 4,339 px and the table is unreadable. English is unaffected because `NullTranslations` returns `""`.

**Fix.** Make `assetboard.i18n.gettext` return `""` for an empty message (and `ngettext` handle empty), and guard the call site (`subtitle = gettext(subtitle, locale) if subtitle else ""`). Add a unit test for empty `gettext` in `pl` and a web test that the portfolio page does not contain `Project-Id-Version`.

**Effort:** S. **Related:** F6.11 risk tiers, i18n.

#### A11Y-04 — withdrawn — the 5+ plural form is correct

The initial audit suspected that “7 pozycji nie miało jeszcze wartości i nie zostało policzonych” was ungrammatical. Polish grammar for a 5+ quantity subject uses a **neuter singular** verb (`pięć osób przyszło`, `siedem pozycji miało`), so the rendered form is correct. No change is needed; the issue was withdrawn before implementation.

#### A11Y-05 — P3 — Wrong setup token leaves focus on `<body>`

**Verified:** browser. **Route:** `/setup`.

Submitting a wrong setup token re-renders the page with `aria-invalid="true"` on the token field and the error next to it, but `document.activeElement` is `<body>`. Other forms use `data-autofocus`; the setup template does not. A keyboard/screen-reader user must find the field again.

**Fix.** Add `data-autofocus`/`autofocus` to the first invalid setup field (token, then email, etc.) and let `app.js` focus it. Add a test asserting the active element after a refused setup.

**Effort:** S. **Related:** UI guideline §4 forms, F1.1.

#### A11Y-06 — P3 — Setup “Language” sets the workspace only; the admin UI stays in the browser language

**Verified:** browser. **Route:** `/setup`.

The setup form’s “Language” sits under the Workspace section and sets `workspace.locale`; the administrator’s profile locale remains `browser`. With a Polish workspace and an en-US browser, the first screen after setup is English (labels) while the currency formatting is Polish. A user choosing Polish may expect a Polish UI. The field is arguably labelled by its section, but the first-run experience is surprising.

**Fix.** Either set the administrator’s locale to the chosen language during setup, or rename the field “Workspace language (for categories and formatting)” and add a note that the interface follows the browser/profile language. Add a test for the chosen locale.

**Effort:** S. **Related:** F1.1, i18n.

### 4.5 Security and technical

#### SEC-01 — P1 — Backup creation/download bypass re-authentication and audit; whole-database exfiltration

**Verified:** code + UI + existing test. **Routes:** `POST /admin/backups/now`, `GET /admin/backups/{run_id}`.

`web/backup_routes.py:35-41` only checks `current.is_instance_admin`; it does not use `run_confirmed`, unlike other admin POSTs. `POST /admin/backups/now` creates a copy with no password (we clicked it in the UI; it succeeded). `GET /admin/backups/{run_id}` downloads the complete SQLite database: every workspace’s financial data, user emails, password hashes, external identities, audit IP prefixes and encrypted TOTP/notification/provider secrets. `BackupRun` has no actor column (`db/models/backups.py:19-50`) and there is no `backup.created`/`backup.downloaded` audit event. The existing test posts the route without `confirm_password` and downloads the DB (`tests/web/test_backup_routes.py:35-54`). `docs/backup.md:75-80` confirms the file contains all workspaces.

**Why it matters.** A stolen instance-admin session (shared browser, XSS-adjacent, stolen cookie) can exfiltrate the entire instance with one GET and no password, and the owner has no audit trail. The retention settings, by contrast, do ask for a password.

**Fix.** Require `run_confirmed` for “Back up now” and a fresh password/session confirmation for download (POST confirmation page or a short-lived signed download token). Record `backup.created` and `backup.downloaded` instance audit events with the actor. Add both routes to the authorization matrix. Wrap the demo reset/delete routes too (`admin_routes.py:653-691`), or correct the “every POST is confirmed” docstring.

**Test.** Admin POST without password → 401/403 and no file; with password → 303 + audit row. Download without confirmation → 401/403; after confirmation → 200 + audit row. Non-admin remains 403.

**Effort:** M. **Related:** F9.4, ASVS 3.7.1/4.1.3, `docs/security-checklist.md`.

#### SEC-02 — P2 — Successful sign-ins count toward the per-email throttle; five logins/hour lock the account

**Verified:** code + local observation. **Route:** `POST /login`.

`config.py:141` defaults `auth_account_free_failures=5`; `services/auth.py:1098-1102` deliberately keeps the successful attempt in the account bucket, and `:777-789` says every attempt counts, successful ones included. We hit “Too many attempts. Try again in 1 minute.” after four successful browser logins plus one failed attempt. The account bucket is per normalized email, window 60 minutes, and successful logins do not reset it. After the fifth attempt, the next attempt increments the count and backs off exponentially.

**Why it matters.** A household with a phone, a laptop and a few session expiries can lock the account for the rest of the hour; an attacker who knows the email can also burn four attempts and make the victim’s next successful login the fifth. The docs (`deployment.md:41-43`) document the behavior, but the user cost is high.

**Options.** (a) Count only failures in the account bucket while keeping the dummy-hash/timing-uniform response; (b) keep counting successes but exempt a successful login from the count when the request comes from an already-authenticated device/known session; (c) raise the free limit or shorten the window. The account bucket is not exposed, so counting successes adds little enumeration protection. Whichever is chosen, update `docs/deployment.md` and `docs/accounts.md`.

**Test.** Five successful logins in the window do not throttle the sixth; five failures still throttle; the response remains uniform for unknown emails.

**Effort:** S. **Related:** ADR-0012, ASVS 2.2.1.

#### SEC-03 — P3 — Decoded newlines in the request path forge text-log lines

**Verified:** local request + server log. **All routes.**

`GET /foo%0aINJECTED%20line` returns 303; the text log contains two physical lines:
```
INFO … path=/foo
INJECTED line status_code=303 …
```
`redact_path` only replaces invite/register/reset tokens (`middleware.py:141-144`); the request logger logs the decoded path with `%s` (`middleware.py:184-200`). The JSON formatter escapes newlines, but the text formatter (`logging.py:33-50`) does not; the native install default is text.

**Fix.** Strip/replace C0/C1/ANSI control characters in `redact_path` (e.g. `\xNN` escapes) before logging, or use JSON escaping in the text path.

**Test.** `tests/unit/test_logging.py`: request `/foo%0aINJECTED` and `%1b`, assert one record with an escaped sequence, not a second raw line.

**Effort:** S. **Related:** ASVS 7.1.2.

#### SEC-04 — P3 — Expired and idle-expired sessions are never pruned

**Verified:** code. **All sessions; public demo amplifies.**

`read_web_session` validates expiry but returns `None` without deleting the row (`services/auth.py:2782-2795`). `get_valid_session` deletes expired rows (`:1359-1375`) but is only used by unit tests, not the web path. No scheduled job prunes `user_sessions`; the recurring jobs cover imports, notifications, update checks and backups only. Rows retain the session hash, IP prefix and user agent indefinitely.

**Impact.** Database/disk growth and metadata retention after expiry; not an auth bypass. Public demo logins (20/hour/IP, unlimited IPs) amplify it.

**Fix.** Add a daily prune deleting rows where `expires_at <= now` or `last_seen_at + idle_timeout <= now`, with an index on `expires_at`; optionally delete on the expiry path. Remove or use the dead `get_valid_session` delete logic.

**Test.** Create a session, advance the clock past idle/absolute expiry, run the cleanup job, assert the row is gone; repeat for a demo session.

**Effort:** S. **Related:** ASVS 3.3.1, F9.4.

#### SEC-05 — P3 — Demo import uploads have no demo restriction or aggregate quota

**Verified:** code + docs. **Demo mode; P2 on a public instance.**

`workspace_data_import` requires only editor role (`web/workspace_routes.py:393-425`); `upload_import` has no `require_not_demo` (`services/imports.py:955-1008`); the demo context is `Role.OWNER` (`services/demo_mode.py:154-179`). The per-file cap is 20 MB (`config.py:117`), but there is no per-workspace byte/batch quota or per-session/IP upload limit. Files remain for at least 24 hours. An attacker with a demo session can fill the data volume or saturate the two import workers.

**Fix.** Call `require_not_demo(ctx)` for import mutations, or add a per-workspace pending-batch/byte quota and a per-session/IP upload rate limit; hide the import UI for demo users. Update `docs/security-checklist.md` 11.1.3/12.1.1 (they imply aggregate quotas that do not exist).

**Test.** Demo session POST import → 403 or quota enforced and no file remains; normal editor still works.

**Effort:** S/M. **Related:** F9.11b, ASVS 11.1.3/12.1.1.

#### SEC-06 — P3 — Broad `TRUSTED_PROXIES` CIDRs are accepted silently when proxy auth is off

**Verified:** `Settings` probe. **Deployment.**

`Settings(_env_file=None, trusted_proxies="0.0.0.0/0")` is accepted; `::/0` and `10.0.0.0/8` are accepted too. `PeerMiddleware` trusts forwarded headers from any address in the list (`middleware.py:333-341`). With proxy auth off, `_validate_proxy_auth` does not restrict the CIDRs (`config.py:489-518`) and the startup warning is conditional (`app.py:165-174`). An attacker in the trusted range can rotate `X-Forwarded-For` to bypass per-IP throttles, poison audit IP prefixes and change the scheme used by the Origin check. Account-level throttles still apply.

**Fix.** Reject `0.0.0.0/0` and `::/0` always; warn for any CIDR wider than /24 (IPv4) or /64 (IPv6) regardless of proxy auth. If trust-all is needed, require an explicit opt-in flag and document that direct access must be blocked.

**Test.** `Settings(trusted_proxies="0.0.0.0/0")` raises; `10.0.0.0/8` logs a warning without proxy auth; a spoofed `X-Forwarded-For` from an untrusted peer does not change the throttle key.

**Effort:** S. **Related:** ADR-0021, ASVS 5.2.6/12.6.1.

#### SEC-07 — P3 — Operator-supplied `SECRET_KEY` has no minimum length

**Verified:** code. **Deployment.**

`validate_secret_key` only rejects empty/blank values (`config.py:225-232`); `ensure_secret_key` returns a configured value unchanged (`config.py:620-658`). A short/guessable key weakens every derived key: CSRF, OIDC flow/link cookies, TOTP-secret encryption, notification-URL encryption and provider-key encryption. The generated default is strong; the risk is an operator-supplied placeholder.

**Fix.** Require at least 32 characters when a key is supplied, or emit a prominent startup warning and document the requirement. Reject known placeholders.

**Test.** `Settings(secret_key="short")` raises (or warns); `secret_key=None` still generates and persists a strong key.

**Effort:** S. **Related:** ADR-0012, ASVS 6.2.1.

#### SEC-08 — P3 — Demo reset/delete are unconfirmed, unlike the documented admin POST rule

**Verified:** code. **Routes:** `/admin/settings/demo/reset`, `/admin/settings/demo/delete`.

`web/admin_routes.py:653-691` handles the demo actions without `run_confirmed`, while the module docstring says every admin POST is confirmed. Demo-only impact, but the inconsistency matters because the reset replaces all demo data and the delete removes the workspace.

**Fix.** Wrap both in `run_confirmed` or correct the docstring and document the exception. Add a test.

**Effort:** S. **Related:** F9.11b, SEC-01.

### 4.6 Performance and operations

#### OPS-01 — P2 — Home’s net-worth widget takes 500–600 ms; 12 separate widget requests per load

**Measured:** server logs on the demo dataset. **Route:** `/`.

The Home page issues 12 `GET /dashboard/widgets/*` requests. The net-worth widget is consistently **500–600 ms** server time; portfolio and portfolio-history are 200–350 ms; the rest are 40–180 ms. Each widget opens its own session and recomputes its series; the net-worth widget calls the all-time derived history. On a larger valuation/price history this grows.

**Fix.** Cache the derived net-worth/portfolio series per workspace/date (invalidated by writes), or compute the Home widgets in one request/one session. At minimum, add a query-count/performance test with a populated net-worth and portfolio history (the existing 50k-ledger benchmark does not seed them).

**Test.** Benchmark Home with a large history; assert a budget for the net-worth widget and the total widget requests.

**Effort:** M. **Related:** F5.9, F6.7, F9.1.

#### OPS-02 — P2 — Reports Phase B reads all history: 42 SQL vs 6, 73 ms vs 9 ms

**Measured:** SQLAlchemy event count on the demo. **Route:** `/reports`.

With Phase B: **42 SQL / 72.8 ms**; without: **6 SQL / 9.2 ms**. `networth_history("all")` is 27 SQL / 63.7 ms; `performance_page("all")` is 9 SQL / 24.4 ms. The report range is 2024–2026, but both services load all history and then filter. The CSV route already passes `with_phase_b=False`; the page does not.

**Fix.** Add range-limited variants (or a `start`/`end` argument) to `networth_history`/`performance_page` so a short report range does not read all valuations/prices. Keep the all-time form for the module pages. Add a populated benchmark.

**Test.** A 1-year report on a 10-year history reads only the needed rows; query count stays bounded.

**Effort:** M. **Related:** F10.4/F10.5, F5.9/F6.7.

#### OPS-03 — P3 — Benchmark budgets allow 3–4× the specified performance

**Verified:** code + tests. **Features:** F6.7/F7.2.

F6.7 specifies “5 years × 30 instruments × weekly points computed in **< 500 ms**”; `tests/unit/test_performance_domain.py:617-638` asserts `< 2` seconds. F7.2 specifies “a 360-month schedule builds in **< 10 ms**”; `tests/unit/test_loans_schedule.py:586-602` asserts `< 0.030` seconds. The comments justify CI margin, but a 4×/3× regression passes the gate.

**Fix.** Tighten to the spec (with a separate CI-margin marker or a benchmark job if shared runners are noisy).

**Test.** The existing benchmark tests with tighter thresholds.

**Effort:** S. **Related:** F6.7, F7.2.

### 4.7 Documentation and onboarding

#### DOC-01 — P2 — `main` README says “pre-alpha”; release branch is behind main

**Verified:** `README.md:3` on `b68cc3f` says `Status: pre-alpha`; `7cdf53f` (release branch) says `Status: 1.0 (stable)`. The owner checkout is on `chore/release-1.0.0`; `origin/main` has #398/#400/#402 that the release branch does not. The release PR #397 is a draft based on older main.

**Why it matters.** The release candidate is not a single commit; the release PR must merge current main (including #402) and keep the 1.0 README status. Anyone reading main sees “pre-alpha.”

**Fix.** Merge `origin/main` into #397, resolve the README/version conflicts, confirm the 1.0 status and changelog, then run the release checklist. Do not tag until the owner lifts the hold.

**Effort:** S. **Related:** F9.8, `docs/release-checklist.md`.

#### DOC-02 — P3 — Reports screenshot predates F10.4/F10.5

**Verified:** git log. `docs/assets/screenshots/desktop/light/reports.jpg` and `phone/light/reports.jpg` were last updated in #396; `docs/reports.md` and `README.md` now describe net-worth/portfolio history. The screenshot does not show the new sections.

**Fix.** Regenerate the Reports screenshots (and the gallery entries) with `scripts/capture_screenshots.py` after #402; check the Polish/English images.

**Effort:** S. **Related:** F9.7, F10.4/F10.5.

#### DOC-03 — P3 — Security checklist 5.2.6 says provider URLs are user-configured; they are hardcoded

**Verified:** code. `docs/security-checklist.md:34,126` groups “notification and price-provider URLs” as user-configured. The built-in providers use hardcoded HTTPS hosts (`services/providers/nbp.py:24-29`, `ecb.py:18-19`, `coingecko.py:20-21`, `eodhd.py:23`, `goldapi.py:29-30`). The generic user-configured HTTP provider is post-1.0 backlog. The host-allow-list gap remains for notifications only.

**Fix.** Correct the checklist row; keep the notification SSRF gap. Mention the future generic provider.

**Effort:** S. **Related:** ADR-0021/0024.

#### DOC-04 — P3 — Security checklist 11.1.3/12.1.1 imply aggregate import quotas; only per-file caps exist

**Verified:** code + SEC-05. The checklist says “import sizes and archive quotas” and “large files … fill up storage”; the app has per-file `IMPORT_MAX_UPLOAD_BYTES`/archive member limits but no aggregate per-workspace byte/batch quota. A demo session can accumulate files until the daily reset.

**Fix.** Either add the quota (SEC-05) or narrow the checklist wording to per-file limits.

**Effort:** S. **Related:** SEC-05.

#### DOC-05 — P3 — `rendering.py` says hidden destinations remain reachable; Taxes 404s when off

**Verified:** code. `web/rendering.py:149` says “a hidden destination remains reachable by its address.” `tax_routes._require_taxes` raises `NotFound` when `taxes_enabled` is false (`tax_routes.py:75-78`). The docstring should except Taxes, or the route should render a “Taxes are off; enable them in Workspace settings” page.

**Fix.** Correct the docstring and add a test for the disabled-Taxes route; consider a helpful 403/404 instead of a bare 404.

**Effort:** S. **Related:** F3.3, M3.

#### DOC-06 — P3 — Backlog “Public read-only demo” differs from the shipped writable demo mode

**Verified:** `docs/specs/backlog-post-1.0.md:92-182` vs `docs/demo-mode.md`.

The backlog describes an instance-admin toggle, a separate read-only demo principal, scheduled reset, rate limits and a banner. The shipped demo mode is an operator environment variable, gives the demo user a normal owner session, allows imports/rollback/restore and is explicitly writable. The two are different products. The docs are honest about the current behavior, but the backlog item is now ambiguous.

**Fix (owner decision).** Either mark the backlog item as superseded by the shipped demo mode, or keep it as a future hardening of the public demo (read-only, instance-admin toggle, separate principal). Do not implement both without deciding which one is the public showcase.

**Effort:** S (docs) / M (implementation). **Related:** F9.11b.

### 4.8 Prior-audit items still open (not new discoveries)

| ID | Sev. | Item | Evidence / current state | Fix |
|---|---|---|---|---|
| PA-01 | P2 | Net-worth Add item dialog still has 14 fields; optional/link fields not grouped | `networth/_body.html:93` dialog; measured 916 px scroll height on a 390×844 phone; Save below the scroll. The prior audit asked for “More options” or a guided page. | Group Name/Category/Value/Currency/As-of; put note, acquired, notes, investable, estimate and link behind “More options”; hide value/currency when linked. |
| PA-02 | P2 | Notification “Daily at” and “Remind days before” always shown | `notifications/channels.html:95`; selecting “One message per reminder” still shows “Daily at”; unchecking “Items due soon” still shows the lead-days field. Prior audit P1. | Use `data-hidden-when`/server-side conditional rendering; test both modes. |
| PA-03 | P3 | Net-worth summary repeats assets/liabilities three times | `/net-worth/summary`: the `dl`, the sentence “Assets €…; liabilities €…”, and the bar labels. Prior audit P1. | Keep the `dl` and the bar labels or the sentence, not all three. |
| PA-04 | P2 | “Breakdown”/“Value history” disclosures are 16 px high on phones | Phone net-worth history/items; prior audit P1. | Give the summary a 44 px phone target (`.btn-sm`/padding), or move the history into a dialog. |

---

## 5. Missing capabilities

These are product gaps, not bugs. “Fit” says whether they belong in the current architecture and when they are worth planning.

| Capability | Who expects it | Why | Current workaround | Fit | Rough scope | Recommendation |
|---|---|---|---|---|---|---|
| **Accounts and reconciliation** | Every household with a checking/savings/credit-card account | The ledger has no account balance; users cannot reconcile a statement or see “what is in the bank”. `account_id` is reserved in the schema but no table exists. | Categories/treatments; mentally track balances; use net-worth manual items | High — the schema already reserves `account_id`; transfers become first-class | L | Next minor. This is the biggest table-stakes gap. |
| **Split transactions** | Households with mixed receipts (supermarket, home improvement) | One transaction has one category; a €120 shop may be €80 food + €40 household. | Create two entries; approximate with one category | High — add split lines under a transaction; keep the existing summary treatment | M | Next minor if users ask; do not block 1.0. |
| **Bank CSV import with column mapping** | Anyone leaving a spreadsheet/bank | The app imports only its archive CSV; bank files must be converted first. Backlog #45 already specifies detection, mapping, signed/debit-credit amounts and profiles. | Convert to the archive CSV manually | High — builds on M4's upload/dedupe/rollback | M | Next minor. This is the most valuable post-1.0 feature. |
| **Multi-currency ledger entry** | Expats, travelers, USD/PLN households | Ledger entries are base-currency only; net worth/investments support FX, but day-to-day USD income must be converted manually. | Convert to base currency before entry | High — schema carries `currency`; needs FX date policy and report conversion | L | After accounts/CSV import; decide the FX policy first. |
| **Trash / restore deleted rows** | Anyone who deletes after the 10 s undo | Soft-deleted rows are invisible; there is no trash view or purge. Restore-into-empty also counts tombstones (COR-10). | Database access; create a new workspace | High — soft delete already exists | M | Next minor; also fixes the restore blocker. |
| **Rules / auto-categorization** | Users importing the same merchants every month | Every imported row is categorized manually. | Category mapping during import; manual edit | Medium — a rule table (description → category) fits the import/service layer | M | After bank CSV import. |
| **Tags** | Power users who want cross-cutting labels | Categories are one-dimensional; “business”, “travel”, “reimbursable” cut across them. | Put keywords in the description; use categories | Medium — many-to-many table and filter UI | M | Later; not table stakes. |
| **Attachments / receipts** | Households with warranties/tax receipts | No way to attach a receipt image/PDF to a transaction. Receipt OCR is a non-goal; manual attachments are not. | External folders; notes | Medium — file storage under DATA_DIR, size/type limits, backup inclusion | M | Later; decide backup/export policy first. |
| **Saved filters / views** | Users who repeatedly filter History/Transactions | Filters are bookmarkable URLs, but there is no named saved view. | Browser bookmarks | Medium — store a name + query string per user/workspace | S/M | Later; low cost. |
| **API / webhooks / metrics** | Homelab users with Home Assistant, Grafana, Prometheus | No JSON API, webhook or metrics endpoint; the roadmap deferred the API. | Scrape HTML? Not practical; use the archive | Medium — read-only API/token + `/metrics` would fit | L | Post-1.0; needs the API security model (F9.5). |
| **Passkeys and admin MFA** | Security-conscious self-hosters | Password/TOTP only; instance-admin pages do not require MFA (known gap 4.3.1). | TOTP for the account; password for admin | Medium — WebAuthn is a new dependency; admin MFA is a policy | M | Next minor for admin MFA; passkeys later. |
| **Goals and debt-payoff planning** | savers paying down multiple loans | No “vacation fund” goal or snowball/avalanche comparison; loans have scenarios but no cross-loan planner. | Categories + FIRE + loan scenarios | Medium — goals on top of savings categories; payoff planner on loans | M | Later; quality-of-life. |
| **Public read-only demo** | Operators showcasing the app | The backlog describes a separate read-only demo; the shipped demo is writable and operator-only. | Use the shipped demo with the warning | Medium — needs a separate principal and reset policy | M | Decide with DOC-06 before building. |

**Deliberate non-goals (do not recommend):** bank sync/aggregation, receipt OCR, tax filing, advice, native mobile apps, built-in country tax tables. The PWA and the existing CSV/archive path cover the self-hosted use case.

---

## 6. Verified working

These passed in the browser, in code review, or in the existing/targeted tests. Keep them as regression checks.

**Core ledger**
- Add an expense and an income from the Transactions entry row; the new row appears in view, a visible status/toast appears, and focus returns to the Day field.
- Edit a transaction in place; Save updates the row and shows “Entry saved.”
- Delete a transaction; the Undo toast appears with a countdown; Undo restores the row (“Entry restored.”).
- Amount parsing accepts `1,234.56`, `1 234,56`, `12,5` and `1.234,56` in the English UI and stores the expected minor units.
- A future date creates a plan/occurrence, not an actual; the entry button switches to “Plan”.
- Month close shows a confirmation dialog; after acceptance the month is read-only, the Transactions page hides the entry forms, and a direct plan POST is refused. Reopen works and restores editing.
- The Month summary’s expense/income variance signs, savings rate (saved/earned), pending totals, transfer exclusion and “total money out” match the spec on the demo.
- The Month page’s “Future months stay open for planning” path has no Close button.

**Identity and roles**
- First-run setup with a wrong token shows the error next to the field and `aria-invalid` (focus is the exception, A11Y-05).
- Login, logout, session expiry and the “no workspace” 403 page work; a user with no membership can create a workspace.
- Invitation creation shows the link once; accepting as the invited email joins the workspace; the pending invitation list and expiry are visible.
- Admin account creation shows a one-time temporary password with a Copy button; the user must change it at first sign-in.
- Viewer role: write actions are hidden on Month, Transactions, Net worth and Categories; a direct plan POST returns 403; `/workspace/settings`, `/workspace/members`, `/workspace/data`, `/audit?scope=workspace` and `/admin/*` return 403.
- Cross-workspace IDOR probe: a viewer in Empty Home requesting a Home transaction/net-worth ID gets a 200 page with no ID/content leak; repositories return 404 semantics.
- CSRF: a plan POST without a token and with `Origin: https://evil.example` both return 403; a same-origin token succeeds. `safe_next` rejected `//evil.com`, `https://evil.com` and backslash forms.
- Security headers on `assetboard serve`: CSP (no inline/eval), `frame-ancestors 'none'`, `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy`, `Permissions-Policy`, `Cache-Control: no-store`, `Vary: Accept-Language`; the production CLI suppresses `Server`. (The screenshot harness’s `uvicorn.Config` does show `Server: uvicorn`; that is not the production path.)

**Reports and modules**
- Reports totals for the demo match the Month/Home figures; year/month granularity, category filter, top-8/Other, savings option and CSV download work. The year-overview CSV has the expected columns and amounts.
- F10.4/F10.5: the sections render only in the base currency and with data; year/month period selection, last point per period, “so far”, incomplete dashes, section notes and the CSV skip (`with_phase_b=False`) are covered by the new tests. The hidden non-base-only/undated-only states are UX-15.
- Net-worth linked investment items match the portfolio values; the summary and history use the derived history, not stored snapshots. The zero-total category rule is COR-19.
- Portfolio: holdings values/cost/gain are internally consistent (including the USD cash row and FX), the asset-class split matches, and the risk table’s numbers are correct in English. The instrument-page FIFO average cost is COR-16.
- Performance: the period table’s change, net flows, Δ value − net flows and XIRR are internally consistent; the all-time row’s “—” change % is expected with a zero opening value. The same-date XIRR edge is COR-18.
- Loans: the standard annuity/equal-principal amortization, rate changes, overpayment modes, exact final payment, tranches and scenario comparison passed the targeted review; the mortgage overview’s remaining principal, repaid principal, next-payment principal/interest/costs/overpayment, term and payoff date reconcile. Current-balance checkpoints and installment-plan lifecycle are COR-12–15; historical LTV is COR-17.
- FIRE: the metrics, year-by-year table, charts, sensitivity grid and “not advice” disclaimer render; the progress hint explains the denominator. The targeted review found the FIRE projection logic clean, but live-loan inputs depend on COR-12/COR-13 for current-balance and plan links.
- Investment income, asset classes, instruments, providers, transactions and the bulk-add dialog render without console/CSP errors.

**Data and operations**
- Workspace archive download produces a 40-member ZIP with manifest, SHA-256, workspace settings and CSVs; restore into a new empty workspace validates, shows a detailed plan, applies and reports “Applied”; the restored workspace settings/data match the archive.
- Backup “Back up now” creates a 3 MB manual copy, updates Last backup/Status/Size, and offers Download; the backup panel lists history. (The missing re-auth is SEC-01.)
- Health endpoints `/healthz` and `/readyz` return 200; the fresh instance applies migrations at startup.
- The empty workspace shows actionable empty states on Home, Month, Transactions, Upcoming, Recurring, Income, Net worth, Investments, Loans, FIRE, Categories and Notifications.
- Polish UI (after compiling `.mo`): page titles, navigation, Month, Reports, Investments, FIRE, Categories and settings are translated; seeded categories are Polish in a new Polish workspace; currency/date formatting follows the native-style rules. The exception is A11Y-03; the 5+ plural form is correct.
- Dark mode, 200% zoom (720×450 proxy) and 390 px layouts have no page-level horizontal overflow except the admin users page (UX-03); charts and tables render without console errors.
- No 404s/CSP violations on the reviewed pages other than the intentionally probed `/taxes` (disabled) and the audit’s wrong `/admin/backups` URL.

---

## 7. Open questions and assumptions

1. **v1.0 hold.** The release is intentionally paused. This audit assumes the release candidate is `origin/main` (`b68cc3f`) plus the 1.0 marker; the owner checkout is still `7cdf53f`. Do not tag or publish until the owner says so.
2. **Backup re-authentication.** Should “Back up now” and Download require the admin password (like retention) or a fresh session? The whole-database impact argues yes; the homelab convenience argues for a short-lived download token. Decide before 1.0.
3. **Login throttle.** The docs deliberately count successful sign-ins. Is the self-lockout cost acceptable, or should the account bucket count failures only? If changed, keep the uniform response/dummy hash.
4. **Public demo.** Is the shipped writable demo the intended public showcase, or is the backlog’s read-only demo still wanted? The current docs and backlog describe different products.
5. **Reports page.** Should the new series tables be collapsed, should the empty report be a single empty state, and should the phone filters collapse? The recommendations are compatible; the owner should choose the section order/navigation.
6. **Mobile tables.** Card rows vs a focusable scroll region vs hiding columns: the FIRE table is the best existing pattern. Decide one pattern before touching every table.
7. **Accounts/splits/multi-currency.** These are large product decisions; the missing-capabilities table gives a sequence, not a commitment.
8. **Demo data.** Link the mortgage/car loan to the Loans page and enable Taxes, or keep the manual/linked contrast as a teaching example? The current mismatch looks like a bug to a first-time user.
9. **`gettext("")`.** The wrapper fix is safe, but check any code that intentionally expects the catalog header (none found).
10. **Current-balance loan start.** Should the rate timeline and interest-only clock start at the earliest checkpoint or at a stored origination date? The earliest checkpoint is the least invasive fix for existing rows; a stored start would be cleaner long-term.
11. **Installment-plan counts.** Derive the count from posted occurrences (best) or keep the stored `remaining_count` and add symmetric hooks? The derived form also fixes auto-post, undo, delete and re-post drift.
12. **Zero-total categories.** Does F5.2 mean zero-valued counted items remain in the Summary category table (current spec wording) or are omitted (current code/test)? Pick one and update the other.
13. **Benchmark budgets.** Are the 500 ms/10 ms spec numbers hard budgets or CI targets? If hard, tighten the tests; if not, amend the specs and keep the margin.
14. **Not dynamically tested.** OIDC/proxy live, SMTP, providers, PostgreSQL, upgrade from the previous tag, antivirus, public demo and a large DB remain static-only or untested; mark them in the release checklist.

---

## 8. Suggested sequence

### Now — next patch (before or immediately after 1.0, owner’s call)

1. `gettext("")` guard + Polish risk subtitle (A11Y-03) — S.
2. Archive restore currency (COR-01) — S.
3. Backup re-auth/audit (SEC-01) — M.
4. Current-balance loan checkpoint (COR-12) — M.
5. Installment-plan lifecycle: `as_of`, undo/delete/auto-post counts, overdue posting (COR-13/14/15) — M.
6. FIFO average cost (COR-16) — S.
7. Closed-month occurrence checks (COR-02/03/04) — S/M.
8. Reports empty state, repeated warnings, table toggle, phone filters (UX-06/07/08/09) — M.
9. Transactions “Add entry”/sticky row (UX-01) — S/M.
10. Admin users mobile layout + one table pattern (UX-03/04) — M.
11. Demo reconciliation: loans/net worth, Taxes, “Other” labels (COR-08/09/11) — S/M.
12. Skip link, confirm dialog name, setup focus (A11Y-01/02/05) — S.
13. Chart label clipping, Month h1, Reports screenshot, README/release merge (UX-11/12, DOC-01/02) — S/M.

### Next — next minor

- Accounts/reconciliation (largest table-stakes gap).
- Bank CSV import with mapping (backlog #45).
- Split transactions.
- Trash view/purge (also unblocks COR-10).
- Historical property LTV archive/estimate fix (COR-17).
- Range-limited net-worth/portfolio reads and Home widget caching (OPS-01/02).
- Multi-currency ledger decision and, if approved, implementation.
- Rules/auto-categorization; saved filters.
- Same-date XIRR, zero-total categories, benchmark budgets (COR-18/19, OPS-03).

### Later — post-1.0

- Attachments/receipts, tags, goals/debt-payoff planner.
- Read-only API/webhooks/metrics (Home Assistant/Prometheus).
- Passkeys; admin MFA policy (known gap 4.3.1).
- Public read-only demo, if the owner still wants it after DOC-06.

### Do not

- Do not rewrite the app or replace the stack.
- Do not add bank sync, receipt OCR, tax filing, advice, native apps or built-in country tax tables (explicit non-goals).
- Do not tag/publish 1.0 while the owner’s hold is in place.
- Do not “fix” the login throttle by removing the uniform response/dummy hash; change the counting rule only.
- Do not add a second chart implementation for reports; reuse the existing chart builders (the current F10.4/F10.5 approach is right).

---

## 9. Appendix

### A. Route/flow coverage

| Area | Routes / flows exercised |
|---|---|
| Auth | `/login` (password, wrong password, throttle), `/setup` (wrong/correct token, validation), `/logout`, `/account/password`, `/account/totp` (page), no-workspace 403 |
| Workspace | switcher, `/workspaces/new`, members, invite/accept, roles (owner/viewer), non-member 403, IDOR probe |
| Ledger | `/m/{month}`, `/transactions/{month}`, add/edit/delete/undo, amount formats, future plan, close/reopen, direct POST, viewer read-only |
| Recurring | `/upcoming` (mark paid, skip, overflow menu, future default date), `/recurring` (pause, notices), scheduled form (static) |
| Income/tax | `/income`, income source table, work log form, tax page disabled, `/taxes` 404, tax transactions |
| Net worth | `/net-worth`, `/net-worth/summary`, `/net-worth?side=liability`, `/net-worth/history`, add dialog (fields), linked investments, value history |
| Investments | `/investments/portfolio`, `/investments/performance`, `/investments/income`, `/investments/instruments`, `/investments/classes`, `/investments/providers`, `/investments/transactions`, risk table (en/pl) |
| Loans | `/loans`, `/loans/installments`, `/loans/reference-rates`, mortgage overview, current-balance checkpoint probe, installment-plan post/undo/overdue probes, schedule/payments/scenarios tabs (static) |
| FIRE | `/fire` view, wizard empty, metrics, year table, charts, sensitivity, adjustments disclosure |
| Reports | `/reports` (populated/empty), filters, year/month, CSV, F10.4/F10.5 sections, chart/table |
| Data | `/workspace/data` export, restore into new empty workspace, restore disabled on tombstoned workspace, templates, recent import/rollback |
| Backups | `/admin/settings` backup panel, Back up now, download link, retention (static) |
| Admin | `/admin/users`, create account/temp password, `/admin/settings`, demo panel (static) |
| Account | `/account`, `/account/password`, `/account/totp`, language switch |
| UI system | dark/light, 390/720/1440, Polish, keyboard tab order, dialogs, toasts, charts, empty states |
| Ops | `/healthz`, `/readyz`, startup/migrations, security headers, log injection probe |

### B. Commands and artifacts

- `git -C /home/alex/code/AssetBoard worktree add --detach /tmp/assetboard-audit/repo b68cc3f`
- `uv sync --frozen`
- `make i18n-compile` (needed for the Polish runtime pass)
- `uv run python /tmp/assetboard-audit/serve.py` (demo instance, port 8010)
- `uv run assetboard serve --host 127.0.0.1 --port 8011` with a fresh data dir (setup/Polish instance)
- Playwright scripts: `crawl.py`, `a11y.py`, and ad-hoc measurement scripts under `/tmp/assetboard-audit`
- SQLAlchemy `before_cursor_execute` probe for query counts
- Targeted suites in a temporary copy: 355 ledger/months/occurrences/scheduled/income/taxes/recurrence; 292 import/archive
- Screenshots: `/tmp/assetboard-audit/artifacts/` (`desktop-light-*`, `phone-light-*`, `empty/`, `pl/`, `pl2/`, `pl-demo/`, `home-networth-widget.png`, `networth-history.png`, `performance-chart.png`, `login-390.png`)
- Archive: `/tmp/assetboard-audit/artifacts/archive.zip`; CSV: `year-overview.csv`

### C. Test results

- Last full gate at `b68cc3f`: **6,352 passed, 42 skipped** (prior session; not rerun here).
- Targeted correctness suites: 355 + 292 passed (temporary copy), plus the net-worth/investment/loan/FIRE suites.
- Browser: 96-page crawl, 4 viewports/themes, no console/CSP errors beyond the intentionally wrong routes; 41 browser tests were green at the last full run (prior session).
- Security: local CSRF/Origin/IDOR/header/log probes; no destructive or network tests.

### D. Note on the audit environment

The audit used a detached worktree at `b68cc3f`; the owner’s checkout at `/home/alex/code/AssetBoard` remains on `chore/release-1.0.0` (`7cdf53f`). The owner’s running instance on port 8000 was not touched. The temporary instances, data, screenshots and scripts live under `/tmp/assetboard-audit`. No real personal data was used. No application code was changed.

---

*No application code was changed. Await the owner’s approval before implementing.*
