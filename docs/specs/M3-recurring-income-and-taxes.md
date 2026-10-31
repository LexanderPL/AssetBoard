# M3 — Recurring items, variable income, taxes and reminders

**Goal:** never forget the yearly car insurance, never retype fixed income, support income that depends on days worked, and optionally model taxes on income.

**Exit criteria:** recurring items generate upcoming occurrences you can confirm in one click; a variable income source computes expected income from logged work units with an optional payment lag; optional user-defined tax rules (percent or fixed) produce a breakdown and, if desired, planned tax-payment expenses; reminders reach you through Apprise channels.

Every agent prompt starts with: *"Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go)."*

Implementation order: F3.1 → F3.2 → F3.3 → F3.4 → F3.5 → F3.6 → (F3.7 stretch).

**Design decisions in this milestone**
- Planned things (occurrences) and real things (transactions) are separate tables. An occurrence becomes a transaction only when confirmed (or auto-posted).
- Editing a rule only regenerates **pending** future occurrences; posted or skipped ones are never touched.
- Taxes are optional and user-defined. The app never ships tax rates as facts and shows a "not tax advice" notice on the tax screens.

**Data model (sketch)**
- `ScheduledItem(id, workspace_id, name, direction, category_id, amount_mode[fixed|estimate], amount, currency, start_date, end_date?, max_count?, freq[weekly|monthly|yearly], interval, weekdays?, month_day (1..31 | -1 last day | 'first_working_day' | 'last_working_day'), month (yearly), weekend_policy[none|previous_working_day|next_working_day], lead_days, auto_post, paused_at?, notes)`
- `ScheduledOccurrence(id, workspace_id, scheduled_item_id?, income_source_id?, parent_occurrence_id?, direction, category_id, due_date, expected_minor, currency, amount_state[estimated|confirmed], status[pending|posted|skipped], transaction_id?, previous_transaction_id?, label, generated_by[rule|income|tax], created_at)`; unique `(scheduled_item_id, due_date)`; check: exactly one of `scheduled_item_id` / `income_source_id` is set (tax payments additionally set `parent_occurrence_id`).
- `TaxRule(id, workspace_id, name, effect[added|withheld], base[net|gross], notes, archived_at)`; `TaxRuleVersion(tax_rule_id, effective_from, mode[percent|fixed], rate_bp?, fixed_minor?, currency?)`.
- `IncomeSource(id, workspace_id, name, category_id, kind[fixed|variable], fixed_amount?, rate?, unit_label, pay_day (1..31 | -1), pay_lag_months, expected_units_mode[working_days|fixed|average|manual], expected_units_value?, expected_units_window?, record_as[cash_received|net|take_home], auto_post, active_from, active_to?)`
- `IncomeSourceTaxRule(income_source_id, tax_rule_id, position, generate_payment, payment_category_id?, due_day, due_month_offset)`
- `WorkLog(income_source_id, work_month, units Decimal, note)`; unique `(income_source_id, work_month)`.
- `NotificationChannel(id, workspace_id, user_id, name, apprise_url_encrypted, enabled, digest_time, default_lead_days)`; `NotificationLog(id, channel_id, occurrence_id?, kind, sent_on, status, error?)`; unique `(channel_id, occurrence_id, kind, sent_on)` (as built: `(channel_id, kind, subject_key, sent_on)`, see F3.6).

---

## F3.1 Recurrence engine and scheduled items

**Story:** I define "car insurance, yearly on 14 March, about 1 800 PLN" once and it shows up every year.

**Acceptance**
- Pure domain module `recurrence.py`: `occurrences(rule, from_date, to_date, holidays) -> list[date]`; may wrap `dateutil.rrule` (justify the dependency) but the stored model is the structured fields above.
- Supports weekly (weekdays), monthly (day 1–31, last day, first/last working day), yearly (month + day), `interval`, `start_date`, `end_date`, `max_count`.
- Short months clamp (day 31 → last day of month); yearly 29 Feb → 28 Feb in non-leap years.
- Weekend policy `previous_working_day` / `next_working_day` using workspace country holidays (python-holidays; justify the dependency); `none` leaves the date.
- Scheduled item CRUD with live preview of the next 6 due dates; pause/resume; archive.
- Generator service (idempotent): materializes occurrences for the next **13 months** (setting), never duplicates (`unique(scheduled_item_id, due_date)`), by default does not create occurrences before today unless the user ticks "include past occurrences".
- Rule edit → delete and regenerate **pending future** occurrences only.
- APScheduler job runs the generator daily at 03:00 workspace time and on every rule change.

**F3.1 implementation details**
- Scheduled dates use the existing 1900–2199 range. Start/end are inclusive nominal-date limits;
  query windows are inclusive effective-date limits. Weekly intervals start on Monday. Maximum
  count applies before shifting; shifted duplicates merge. Holiday adjustments may cross the
  nominal start/end boundary.
- No workspace country means weekends only. National/observed holidays are used without a
  subdivision. Unsupported countries are refused only for working-day rules; changing the
  workspace country to one without holiday data is refused while working-day items exist.
  A country change regenerates working-day items immediately.
- The horizon is today through today plus `RECURRENCE_HORIZON_MONTHS` calendar months (default
  13), inclusive. Backfill is an explicit creation option. Edits never backfill past dates.
- Every generator run reconciles pending rows from today onward with the current rule and
  calendar (stale pending rows are deleted), so calendar changes never duplicate.
- Pause removes pending dates from today onward; archive/delete remove all pending dates;
  resume generates forward. History is posted/skipped rows only. Delete physically removes an
  item only without remaining history, otherwise archives it.
- Rows in closed months (F2.5) are never created, deleted or changed by any of the above.
- Items whose category is archived generate nothing; their pending rows from today onward are
  removed on the next run.
- Fields needed by F3.2/F3.4/F3.6 (posting, income/tax links and reminders) are added with those
  features. F3.1 persists only rule-generated occurrences, with no transaction side effects.
- Startup catches up the forward horizon; job definitions reconcile new workspaces/time-zone
  changes every minute. See [user guide](../recurring.md) and [ADR-0018](../adr/0018-recurring-items.md).

**As built: one-time items (owner decisions of 2026-10-26):**
- Frequency `once` ("One time"): exactly one occurrence on `start_date`, shifted by
  `weekend_policy` with the usual holiday handling. Interval, weekdays, month day, month, end
  date and maximum count are ignored and stored normalised (interval 1, end date and count
  null, weekdays empty, month day and month 1). The form hides those fields (a small
  `data-hidden-when` progressive-disclosure handler) and explains that they are ignored.
- The horizon rule is unchanged: the occurrence is generated once its date enters the
  horizon. A past date follows "include past occurrences". A date edit regenerates the pending
  occurrence like any rule edit. Once the item has a kept row (posted, skipped, overdue or in a
  closed month), a date edit adds no second occurrence; move an overdue one from Upcoming.
- With its occurrence posted or skipped the item is shown as "Done" (no pause/resume; archive
  as usual). The list shows "One time on <date>" (the nominal date).
- The pending occurrence feeds the month's Expected and Forecast; Planned (the budget) is not
  changed.
- Migration `0023_one_time_items` widens `ck_scheduled_items_freq_valid`. Its downgrade keeps
  each one-time item as `monthly` on the day of its date with `max_count = 1` (same single
  date), so no data is lost.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F3.1 from docs/specs/M3-recurring-income-and-taxes.md: pure recurrence domain module with the listed options,
ScheduledItem and ScheduledOccurrence models + migration, CRUD UI with next-6-dates preview, idempotent generator service
(13-month horizon), APScheduler daily job, regeneration of pending future occurrences on edit.
Domain tests (table-driven + hypothesis): day-31 clamping, 29 Feb yearly, last/first working day, weekend policies with a fake holiday set,
interval/end_date/max_count, determinism. Services use the injected Clock only.
```

---

## F3.2 Upcoming inbox and occurrence workflow

**Story:** I see what's due and overdue and confirm, skip or move items in one click.

**Acceptance**
- "Upcoming" page: overdue (highlighted), next 30 days (setting), grouped by week; filters by direction/category.
- Actions per occurrence: **Mark paid/received** (dialog prefilled with expected amount and due date; editable) creates a `Transaction` (`source=recurring`) linked to the occurrence and sets `status=posted`; **Skip**; **Reschedule this one** (changes only this occurrence's date); **Edit amount** (this occurrence only); **Edit rule**.
- Undo of "mark paid" within 10 seconds deletes the created transaction and restores `pending`.
- `auto_post=true` items post automatically on the due date (job runs shortly after midnight workspace time) with `source=recurring_auto`; the transaction is clearly marked and editable.
- Posting into a closed month is blocked with a clear message (the user must reopen or reschedule).
- A posted occurrence cannot be changed by rule edits.

**Implementation details**
- Occurrences retain a `nominal_date` identity separate from their effective `due_date`; rescheduling
  one occurrence never changes the recurring rule or its nominal key. Shifted duplicate nominal
  dates continue to merge. Regeneration keeps a rescheduled pending occurrence while its nominal
  date still belongs to the rule, even when that date is before today (an overdue item moved
  forward).
- `generated_by`, `transaction_id`, per-occurrence amount overrides and reschedule timestamps are
  stored on the occurrence. `ScheduledItem.auto_post` defaults to false.
- Undo follows the ledger's soft-delete and audit conventions: the linked transaction is soft
  deleted and the occurrence returns to pending, within the existing 10-second undo window.
- Deleting a posted occurrence's transaction from the ledger (month page soft delete) returns the
  occurrence to `pending` in the same write: `transaction_id` is cleared, its nominal date,
  reschedule and amount override are kept, and `scheduled_occurrence.unposted` is audited. The
  delete needs the transaction's month and the occurrence's due month open (`MonthClosed`
  otherwise), as for an undo. Deleting an already deleted transaction changes nothing.
  Restoring the transaction (ledger undo within 10 seconds) re-links it
  (`scheduled_occurrence.relinked`) only if the occurrence is still pending, unlinked and in an
  open month; otherwise it stays a plain transaction.
- An occurrence returned to pending (undo or ledger delete) remembers the transaction in
  `previous_transaction_id`. Auto-post only touches occurrences that were never posted
  (`previous_transaction_id IS NULL`), so a deleted auto-posted transaction is not recreated by
  the next run; the user posts it by hand.
- The inbox window defaults to 30 days and is configurable by the operator with
  `ASSETBOARD_UPCOMING_DAYS` (1–365).
- Auto-post jobs run at 00:05 in each workspace time zone, use the injected Clock, and leave
  occurrences pending when their due month is closed. Paused or archived items are not posted.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F3.2 from docs/specs/M3-recurring-income-and-taxes.md: Upcoming page, occurrence actions (mark paid -> Transaction,
skip, reschedule, edit amount), undo window, auto-post job, closed-month protection in the service layer.
Tests: posting creates exactly one linked transaction (idempotent on double submit), undo restores pending, auto-post respects the Clock,
posted occurrences are immune to rule edits.
```

---

## F3.3 Tax rules (optional modelling)

**Story:** I can define taxes or levies on my income — name, percent or fixed value — see a breakdown, and optionally have the payments planned for me.

**Concepts**
- A **tax rule** has a name (VAT, income tax, social contributions, whatever the user calls it), an **effect** and a **value** that can change over time (versions with `effective_from`).
- `effect=added`: added on top of the net amount (VAT-style); the amount is passed through to a tax office. `effect=withheld`: deducted from what the user keeps (income-tax/contribution-style).
- `base`: `net` or `gross` (`gross = net + added taxes`). `added` rules always use `net`; `withheld` rules may use either.
- Value is a `PercentOrFixed`: percent (basis points) or fixed Money per income occurrence.
- Rule version is chosen by the occurrence's pay date.

**Pure function** `calculate_taxes(net, rules, record_as) -> TaxBreakdown`
```
added_i    = round_half_up(rate_i * net)            or fixed_i
gross      = net + sum(added_i)
withheld_j = round_half_up(rate_j * (net | gross))  or fixed_j
take_home  = net - sum(withheld_j)                  # added taxes are pass-through
ledger_amount = gross (cash_received) | net (net) | take_home (take_home)
```
Which taxes get a planned payment expense (`generate_payment = true`):

| `record_as` | `added` taxes | `withheld` taxes |
|---|---|---|
| `cash_received` (gross) | payment expense | payment expense |
| `net` | no ledger effect (pass-through) | payment expense |
| `take_home` | no ledger effect | no ledger effect (already deducted) |

Payment due date = `due_day` of the month `pay_date.month + due_month_offset` (clamped to month length).

**Acceptance**
- Tax rule CRUD with versions (add a new version instead of editing history), archive; notice "Rates are entered by you; this is not tax advice."
- **Calculator preview**: enter a net amount and (optionally) pick an income source → shows the breakdown table (net, each added tax, gross, each withheld tax, take-home, ledger amount, planned payments).
- Golden test: net 23 370.00 PLN; VAT 23% added → 5 375.10; gross 28 745.10; income tax 12% withheld on net → 2 804.40; fixed levy 1 000.00 withheld → total withheld 3 804.40; take-home 19 565.60. Ledger amounts: `cash_received` 28 745.10 with payments 5 375.10 + 2 804.40 + 1 000.00; `net` 23 370.00 with payments 2 804.40 + 1 000.00; `take_home` 19 565.60 with no payments.
- **Invariant (property test):** when every rule has `generate_payment`, `ledger_amount − sum(payments) == take_home` for all three `record_as` modes and any inputs.
- Rounding is half-up per rule; amounts are `Money`; no floats.

**F3.3 implementation details**
- F3.3's calculator accepts a manual net amount and `record_as`. Selecting an income source and
  attaching rules is deferred to F3.4, where `IncomeSourceTaxRule` is introduced.
- Fixed values use the workspace base currency; no currency conversion is performed.
- The workspace base currency is locked while any fixed tax version exists; percentage-only
  versions do not lock it.
- Rate versions are append-only and a newly added version must have an `effective_from` later than
  the latest existing version. Rule effect and base are fixed at creation; create a separate rule
  to model a different effect or base. Archived rules are excluded from new calculator previews.
- If no version is effective on the selected date, the rule is omitted from the calculation and
  named in the preview warning.

**As built: tax redesign ([ADR-0022](../adr/0022-tax-kinds-and-payment-schedule.md); owner
mockup of 2026-10-31).** This supersedes the effect/`record_as` model above.
- `TaxRule.kind` is `added` | `self_paid` | `payer_withheld` ("Who pays it to the tax office?").
  `calculate_taxes(net, rules)` has no `record_as`: received (the occurrence's expected amount,
  `ledger_amount`) = `net + added − payer_withheld`; payments = `added + self_paid` where
  `generate_payment`; take-home = `net − self_paid − payer_withheld`. The invariant
  `received − sum(payments) == take_home` holds when every rule generates payments.
- Golden test (same numbers, by kind): income tax and levy `self_paid` → received 28 745.10,
  payments 5 375.10 + 2 804.40 + 1 000.00; income tax `payer_withheld` → 25 940.70, payments
  5 375.10 + 1 000.00; both `payer_withheld` → 24 940.70, payment 5 375.10.
- The rule carries the payment schedule: `payment_day`, `payment_timing` (`next_month`,
  `same_month`, `after_quarter` = the month after the pay date's calendar quarter) and
  `payment_category_id` (required unless `payer_withheld`). Attachment `due_day`,
  `due_month_offset` and `payment_category_id` are nullable overrides.
- The first version is effective from 1900-01-01; kind and base are editable until a posted
  income occurrence of a source using the rule exists (`kind_locked`).
- The calculator page is replaced by the example panel of the new/edit tax page (10 000 net,
  this tax only); `/taxes/calculator` redirects to `/taxes`. The warning box is replaced by an
  intro line; `users.tax_warning_dismissed` stays but is unused.
- `workspaces.taxes_enabled` (Workspace settings → Menu, owners) hides the Taxes menu item, the
  tax pages (404) and the tax parts of income; it cannot be turned off while a non-archived
  income source has a tax attached.
- "Other taxes" are `scheduled_items.is_tax` recurring items created on the Taxes page.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F3.3 from docs/specs/M3-recurring-income-and-taxes.md: shared PercentOrFixed value object (domain, reusable later for loan costs),
TaxRule/TaxRuleVersion models + migration, pure calculate_taxes() exactly per the formulas and the record_as table in the spec,
CRUD UI with versions, and the calculator preview page. Do NOT attach rules to income sources yet (that is F3.4).
Tests: the golden example from the spec, the take-home invariant with hypothesis, rounding edge cases (half-up), fixed rules, version selection by date.
```

---

## F3.4 Income sources and work logs (variable income)

**Story:** My income depends on the days I worked. I log worked days; the app computes what I'll be paid and when, with taxes if I want them.

**Concepts**
- `kind=fixed`: a fixed net amount per pay date (salary).
- `kind=variable`: `rate × units` where units come from `WorkLog` (default unit label "day"; also hours, items, custom).
- `pay_lag_months`: pay month = work month + lag (e.g., work in August, paid in September → lag 1). Occurrence is created on the pay date.
- Expected units before logging: `working_days` (Mon–Fri minus public holidays of the workspace country in the work month), `fixed` value, `average` of the last N logged months, or `manual` (occurrence stays "estimated: 0" until logged).
- `amount_state`: `estimated` until a `WorkLog` exists for the work month, then `confirmed` (and recalculated).
- Taxes: attach any number of tax rules (ordered), with `generate_payment`, payment category and due settings per attachment; `record_as` decides the ledger amount (see F3.3 table). Without attached rules the source works as plain income.

**As built: tax redesign (ADR-0022).** `record_as` was removed: each attached tax's kind decides
its effect on the expected amount and whether a payment is planned; payment dates come from the
rule's schedule unless the attachment overrides them. The income page ticks the workspace's taxes
("Taxes on this income") with an optional per-source payment day; tax parts are hidden while
Taxes are off. The golden example below now reads "VAT 23% added" (no `record_as`). The startup
catch-up recalculates every pending income occurrence before generating.

**As built: Income page redesign (owner mockup, 2026-10-31).** The page is called "Income": one
card per source with a summary line, tax chips, the next three payments as a table and a
"why no tax payment" note while Taxes are on. New sources are created with the four-step
"Add income" form at `/income/new` (server-side steps with hidden fields, no JavaScript needed;
the taxes step only while Taxes are on), which ticks the chosen taxes in the same transaction.
Editing moved to `/income/{id}/edit` (one page, same fields; archive is there too). The payday is
entered as "day N of the next month / same month / two months later" (pay lag 1/0/2; older
sources with a longer lag keep it). The old one-form `POST /income` still works.

**Acceptance**
- Income source CRUD (fixed and variable) with a live "next occurrences" preview including tax breakdown.
- Logging units for a work month (month-level number, decimals allowed e.g., 7.5) creates/updates the occurrence of the corresponding pay month; only **pending** occurrences are recalculated; posted ones are left alone.
- Generated tax-payment occurrences (`generated_by=tax`, `parent_occurrence_id` set) follow the parent: recalculated when units change, removed if the parent is skipped.
- Golden example (test): variable source, rate 1 230.00/day, lag 1, pay day 10; work log Aug-2026 = 19 days; VAT 23% added, `record_as=cash_received` → occurrence 2026-09-10 expects 28 745.10 (net 23 370.00 + VAT 5 375.10), confirmed; tax-payment occurrences for the configured rules appear in the configured months.
- Confirming (F3.2 flow) an income occurrence posts the income transaction with the ledger amount; the user may edit the actual amount (the bank amount rules).
- Edge cases covered: no log yet, log changed after posting (offers "adjust transaction" but does not auto-edit), lag 0 and lag ≥ 2, `active_to` ends generation.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F3.4 from docs/specs/M3-recurring-income-and-taxes.md: IncomeSource, IncomeSourceTaxRule, WorkLog models + migration,
income occurrence generation (extend the F3.1 generator) with pay lag and expected-units modes, recalculation of pending occurrences on work-log change,
generation/maintenance of child tax-payment occurrences using calculate_taxes() from F3.3, CRUD UI with live preview, work-log UI (per work month).
Use the golden example in the spec as a test. Pure computation goes into domain/, orchestration into services/.
Do not touch posted occurrences. Tests for lag 0/1/2, estimated vs confirmed, skipped parent removes children.
```

---

## F3.5 Month page integration

**After F2.7 (owner, 2026-10-25):** the month page no longer lists transactions. The pending
"expected" rows with their Confirm button go into the tables of the **Transactions** page
(`/transactions/YYYY-MM`); the Expected and Forecast columns and the forecast KPIs stay on the
**Month** summary (and the shared flow bar, so Home shows them too); the missing-work-log banner
shows on both the Month and the Transactions page.

**As built (F3.4 PR #149 and the F3.5 follow-up):**
- Expected rows show for every pending occurrence due in the month (recurring items, income and
  tax payments), in the table of their direction; Confirm opens the F3.2 confirm form (date and
  amount) and returns to the same Transactions page. Viewers see the rows without Confirm.
- The toggle ("Show forecast", a pressed toggle) hides **both** extra columns (Expected and
  Forecast) and the forecast KPI line on the Month summary; Home always shows its forecast line.
  The choice is kept per browser and survives the summary's htmx swaps.
- The banner reads "Enter days worked for <work month> for <source>." and links to the income
  source. It is shown for the viewed month only: the relevant work month is the viewed month
  minus the source's pay lag.
- The overdue badge counts pending occurrences due before today (workspace time zone) and sits
  on the Upcoming link of the sidebar.

**Acceptance**
- Month page lists **pending occurrences of that month** in each table (visually distinct "expected" rows with a Confirm button); confirming moves them into real rows.
- Summary gets extra columns: **Expected (pending)** and **Forecast = actual + pending expected**; KPIs show "forecast spent / forecast earned" next to actuals. A toggle hides forecast columns.
- Banner: "Enter days worked for <month>" for variable income sources whose work log for the relevant work month is missing and whose pay date is within 10 days or past.
- Overdue count badge in the nav links to the Upcoming page.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F3.5 from docs/specs/M3-recurring-income-and-taxes.md: show pending occurrences inside the month page tables, add Expected and Forecast
columns to summarize_month (extend the domain function and its tests, keep existing behaviour for actuals unchanged), the missing-work-log banner,
and the overdue badge. Update the golden tests from F2.4 so they still pass.
```

---

## F3.6 Notifications and reminders

**Story:** I get a push/email before things are due, overdue reminders, and a prompt to log worked days.

**Acceptance**
- Notification channels per user via **Apprise URLs** (ntfy, Telegram, email, Gotify, …). URLs are encrypted at rest (Fernet key derived from `SECRET_KEY`; justify the `cryptography` dependency) and never shown in full after saving. "Send test" button.
- Daily job at the channel's `digest_time` (user timezone): (a) occurrences due within `lead_days` (item-specific, else channel default) not yet reminded; (b) overdue occurrences (max once per day, max 7 times); (c) "enter days worked for <month>" on the first day of the month (and again 3 days before an estimated income occurrence).
- Digest mode (one message per day) or per-item mode.
- Deduplication via `NotificationLog` unique key; failures logged with error, retried next run, never crash the job.
- Tests use a fake notifier; no network.

**As built (F3.6, [ADR-0021](../adr/0021-notifications.md); owner decisions of 2026-10-25):**
- Channels are per member and workspace; every member, viewers included, manages only their
  own; removing a member deletes their channels. Up to 10 per member and workspace.
- "User timezone" is the **workspace** time zone (users have none). A channel runs once per
  local day at or after `digest_time` (`last_run_on`), so a restart or downtime catches up.
- The log's unique key is `(channel_id, kind, subject_key, sent_on)`: `subject_key` is the
  occurrence id or `worklog:<source>:<YYYY-MM>` (a nullable `occurrence_id` would not dedupe
  work-log reminders). `sent_on` is the slot: the due date for due-soon (one reminder per due
  date; a rescheduled occurrence gets one more), the day for overdue, the trigger day for work
  logs. Rows are claimed as `sending` before the send; failed slots are retried by the next
  daily run; the overdue cap counts sent reminders only.
- Item-specific lead days: `scheduled_items.reminder_lead_days` (0–60, optional); income and
  tax occurrences use the channel default. All occurrence kinds are reminded, with per-kind
  toggles on the channel.
- Work-log reminders: on the 1st for the month just ended, and from three days before an
  estimated income occurrence until its due date; no other catch-up.
- Per-item mode sends at most 10 messages per run; the rest go in one more message.
- Amounts only with the channel's **Include amounts** (default off).
- URLs: one Apprise URL whose scheme is on the operator's allowlist
  (`ASSETBOARD_NOTIFICATION_SCHEMES`), Fernet-encrypted bound to the channel, masked as
  `scheme://••••••`. "Send test" is limited to 5 per 10 minutes per member. Sends time out
  after `ASSETBOARD_NOTIFICATION_TIMEOUT_SECONDS`; a tick stops starting channels after
  `ASSETBOARD_NOTIFICATION_TICK_SECONDS`. `ASSETBOARD_NOTIFICATIONS_ENABLED=false` turns
  delivery off.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F3.6 from docs/specs/M3-recurring-income-and-taxes.md: NotificationChannel/NotificationLog models + migration,
Apprise integration behind a Notifier interface (fake in tests), encrypted URL storage, settings UI with test button,
daily job producing due-soon/overdue/enter-work-units messages with deduplication, digest vs per-item mode.
Translatable message templates. Tests with the fixed Clock covering dedupe, overdue cap, timezone handling.
```

---

## F3.7 (stretch) Work calendar

**Acceptance:** optional day-level logging for a variable income source: a month calendar where you click days to mark worked (value 1 by default; half days/hours editable). If any day entries exist for a work month, `units = sum(days)` and the month-level `WorkLog` is derived and read-only. Holidays and weekends are shown; "fill working days" shortcut; bulk-clear month.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F3.7 (stretch) from docs/specs/M3-recurring-income-and-taxes.md: WorkDay model + migration, calendar UI (HTMX),
derivation of the month WorkLog from day entries, fill-working-days shortcut. Make sure F3.4 recalculation is triggered on every change.
```

**As built (F3.7; owner decisions of 2026-10-25):**
- `WorkDay(id, workspace_id, income_source_id, work_date, units Decimal, timestamps)`, unique
  `(workspace_id, income_source_id, work_date)`, composite FK to the income source with
  `ON DELETE CASCADE` (migration `0022_f37`, which also adds `work_logs.derived_from_days`).
  `0 < units <= 24` is enforced in the service, not by a SQL check: a check on a `DecimalType`
  column compares text on SQLite.
- Variable income sources only; the calendar is optional. A work month with any day entries has a
  `WorkLog` with `units = sum(days)` and `derived_from_days = true`, created or updated by the
  service; `log_work` refuses it (`derived_work_log`). The first day entry overwrites a manual log
  of that month (the `WorkLog` model has no note column, so nothing else is kept); removing the
  last day or clearing the month deletes the derived log.
- Toggle (1 unit by default), set value (locale number parsing), fill working days (weekdays that
  are not workspace-country holidays and have no entry; existing values are never overwritten)
  and clear month. Each runs in one transaction with the same role checks (editor;
  `require_role_now` inside `run_immediate`), the same closed-month rule (refused while the
  pending occurrence's pay month is closed) and the same F3.4 recalculation (`_sync_work_month`)
  as a month-level work log save; a posted occurrence is left unchanged and the page says so.
- A workspace country without holiday data shows and fills weekends only (working-day estimates
  still refuse it, as in F3.1).
- UI: `/income/{id}/calendar?month=YYYY-MM` (HTMX fragment swaps, plain-form fallbacks with
  redirects), day buttons with `aria-pressed` and a full-date `aria-label`, CLDR weekday names,
  "Clear month" behind the shared confirmation dialog, linked from each variable income source.
- Workspace deletion covers `work_days` through the metadata-driven delete; archive files
  (`work_days.csv`) stay with F3.8.

---

## F3.8 Archive extension: recurring items, income sources, taxes and work logs

**Story:** My workspace archive (M4) also backs up and restores my recurring items, income
sources, tax rules and work logs.

**Acceptance**
- New archive files (a minor format version per ADR-0016), with lineage `id` columns and
  references by id plus name as in M4: `scheduled_items.csv`, `scheduled_occurrences.csv`,
  `income_sources.csv`, `tax_rules.csv`, `tax_rule_versions.csv`, `income_source_tax_rules.csv`,
  `work_logs.csv` (and `work_days.csv` if F3.7 exists). `transactions.csv` gains optional
  `source` and `scheduled_occurrence_id` columns so posted occurrences stay linked after a
  restore.
- Restore recreates them after categories and before transactions (occurrences after
  transactions when they reference them); merge mode imports `work_logs.csv` alone (by income
  source name, never overwriting an existing month: conflict, listed); the other files are
  restore only.
- Notification channels are **not** archived (their Apprise URLs are secrets; F9.3 decides
  about secrets); they are listed in `ARCHIVE_EXCLUDED_MODELS` with that reason, notification
  logs too.
- The new models join `EMPTINESS_CHECKS`; the M4 round-trip test covers them.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F3.8 from docs/specs/M3-recurring-income-and-taxes.md as an extension of the M4 archive
(docs/specs/M4-ledger-export-import.md, ADR-0016): the new files with lineage ids, export, restore order, standalone
work_logs.csv import in merge mode, exclusions with reasons, emptiness checks, and the extended round-trip test.
```
