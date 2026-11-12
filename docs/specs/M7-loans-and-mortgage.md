# M7 — Loans and mortgage

**Goal:** model a mortgage (and other debts) realistically: annuity or equal-principal installments, fixed or variable rate (reference rate + margin), insurance/fee components, monthly and one-off overpayments, payoff date, interest saved, what-if scenarios — and connect it to net worth and recurring payments.

**Exit criteria:** a loan can be defined from either its original terms or a current balance; the schedule engine reproduces your spreadsheet's behaviour (including "top up to a total monthly amount" overpayments); the loan page shows remaining principal today, payoff date, % repaid, interest saved, charts and scenarios; the loan feeds net worth and appears in Upcoming as a scheduled payment.

Every agent prompt starts with: *"Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go)."*

**Observed behaviour of the source spreadsheet (to be matched)**
- Installment from original terms: `PMT(rate/12, term, -principal)`, rate = reference rate + margin.
- Monthly interest = `balance × annual_rate / 12`.
- Extra cost components each month: one **percentage of the remaining balance** (a very small percent) and one **fixed amount**.
- Overpayment is defined as a **target total monthly outflow**: `overpayment = target − principal − interest − costs` while the balance exceeds the target; when the balance is smaller, the remainder is paid off.
- Outputs: total interest, % repaid, remaining principal today (lookup by today's date), approximate payoff date.
- The schedule starts from a *current balance* rather than from origination.

**Data model (sketch)**
- `Loan(id, workspace_id, name, kind[annuity|equal_principal|installment_plan], principal_original Money?, origination_date?, first_payment_date, term_months, payment_day, rate_mode[fixed|variable], margin_bp?, reference_rate_id?, day_count[monthly|actual_365], installment_override Money?, overpayment_mode[none|fixed_extra|target_total], overpayment_amount Money?, overpayment_strategy[reduce_term|reduce_installment], property_instrument_id?, scheduled_item_id?, archived_at)`
- `LoanRateChange(loan_id, effective_from, rate_bp)` — for `rate_mode=fixed` (rate changes over time).
- `ReferenceRate(id, workspace_id, name, notes)`; `ReferenceRateValue(reference_rate_id, effective_from, value_bp)` — user-defined series (WIBOR 1M, WIRON, EURIBOR… the app hardcodes none).
- `LoanCostComponent(loan_id, name, mode[percent|fixed], rate_bp?/fixed Money?, basis[opening_balance], effective_from, effective_to?)` — uses `PercentOrFixed`; percent is applied monthly to the opening balance.
- `LoanOverpayment(loan_id, date, amount Money, note)` — one-off planned/actual overpayments.
- `LoanPayment(loan_id, date, kind[installment|overpayment], total, principal, interest, costs, note)` — actual payments (optional).
- `LoanBalanceCheckpoint(loan_id, as_of, remaining_principal Money, note)` — bank-confirmed balances.
- `InstallmentPlan(loan_id, installment Money, remaining_count, end_date)` — only for `kind=installment_plan`.

---

## F7.1 Loans: terms and configuration

**Story:** I define my mortgage once, however much I know about it.

**Acceptance**
- Loan wizard with two entry paths: **"I know the original terms"** (principal, origination date, term, first payment date) and **"I only know my current balance"** (balance, date, remaining months → creates a checkpoint; `principal_original` optional but needed for % repaid).
- Rate configuration: fixed with an editable list of rate changes, or variable = reference rate + margin; reference rates managed on their own page (add a value with an effective date).
- Cost components (percent of opening balance per month and/or fixed amount per month) with effective dates.
- Overpayment settings: none / fixed extra amount per month / **target total monthly amount**; strategy `reduce_term` (default) or `reduce_installment`; one-off overpayments list.
- Optional `installment_override` for when the bank's actual installment differs from the computed one.
- Validation: term > 0, margin/rates in a sane range with a confirmation for rates above 25%, overpayment target not below installment.
- Editors/owners only.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F7.1 from docs/specs/M7-loans-and-mortgage.md: Loan, LoanRateChange, ReferenceRate(+Value), LoanCostComponent, LoanOverpayment,
LoanBalanceCheckpoint models + migration (reuse the PercentOrFixed value object from M3), services, and the two-path wizard + edit screens.
No schedule computation yet. Tests for validation rules and workspace scoping.
```

### As built (M7.F7.1)

The eight product-owner decisions implemented in F7.1:

1. `first_payment_date` is the first actual due date and `payment_day` the nominal day for later
   months. Both are required on the original path; leaving the wizard's payment-day field empty
   uses the first payment date's day. The first date must equal
   `min(payment_day, last_day_of_month(first_payment_date))`.
2. The current-balance path collects the balance, balance date, remaining months, next payment
   date, payment day and optional original principal. It creates one checkpoint. A fixed initial
   rate is effective from the balance date; `origination_date` stays empty.
3. `kind` is editable after creation. `rate_mode` is fixed at creation: switching fixed↔variable
   requires archiving the loan and creating a new one.
4. A cost component's `effective_to` is inclusive. Multiple active components may overlap and are
   all summed.
5. Rates above 25.00% require explicit confirmation (25.00% itself does not). The absolute
   maximum is 100%; a margin must be non-negative.
6. The wizard collects the core terms, the initial/current rate and regular overpayment settings.
   Rate changes, cost components and one-off overpayments are added on the loan detail page.
7. All loan amounts use the workspace base currency and a loan locks that currency. One-off
   overpayments are planned rows. Reference rates are workspace-scoped: viewers read, editors and
   owners write. Loans archive/restore and are never hard-deleted. F7.7 owns installment plans.
   Target-total validation uses the pure `first_installment` helper, which F7.2 reuses.
8. No schedule computation beyond that validation helper.

Implementation notes: the new loan tables also join the M4 archive emptiness checks so a restore
cannot target a workspace that already has loans; F7.8 adds their archive files. The demo builder
creates one fixed mortgage entered from original terms and one variable car loan entered from its
current balance. `day_count`, the property link and the scheduled-item link remain out of F7.1;
F7.6 uses a net-worth property item rather than the earlier `property_instrument_id` sketch.

---

## F7.2 Schedule engine (pure domain)

**Acceptance**
- `build_schedule(terms, rate_timeline, costs, overpayments, anchor, as_of, strategy, day_count) -> Schedule` in `domain/loans.py`; no I/O, no clock.
- Rows: `n, date, annual_rate_bp, opening, interest, principal, overpayment, costs, installment_total, closing`.
- Start state = latest checkpoint ≤ `as_of`, otherwise origination.
- Annuity installment = `PMT(rate/12, remaining_months, opening)` unless `installment_override`; recomputed when the rate changes (and after overpayments with `reduce_installment`). Equal-principal: `principal = original / term`.
- `monthly` day count: `interest = opening × rate / 12`; `actual_365`: `opening × rate × days/365`. Rounding half-up per component per row.
- Overpayment modes: `fixed_extra` = extra on top of installment; `target_total` = `max(0, target − principal − interest − costs)` while balance exceeds the target; the last payment clears the balance exactly (no negative or tiny residual balances).
- Summary function: remaining principal at `as_of`, next installment total, total remaining interest and costs, payoff date, months remaining, % repaid.
- **Golden tests**
  1. No overpayments: schedule equals `numpy_financial.pmt/ipmt/ppmt` row by row (dev-only oracle) for a sample loan.
  2. With overpayments: an independent naive loop in the test module, transcribed from the spreadsheet behaviour listed above, matches the engine exactly (rows and payoff date).
  3. Property tests: closing balance never negative; sum of principal + overpayments = starting balance; total paid = principal + interest + costs; more overpayment never increases total interest.
- Performance: a 360-month schedule builds in < 10 ms.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F7.2 from docs/specs/M7-loans-and-mortgage.md: pure build_schedule() and summary functions in domain/loans.py with annuity and equal-principal,
rate changes, cost components, both overpayment modes, both strategies, both day-count conventions, exact final payment.
Money is integer minor units, half-up rounding per component. Golden tests: numpy_financial oracle (dev dependency only) and an independent naive loop in the tests;
plus the property tests listed. No UI in this feature.
```

### As built (M7.F7.2)

The pure schedule engine lives in `src/assetboard/domain/loans.py`:

- `select_anchor(terms, checkpoints, as_of) -> ScheduleAnchor` takes the latest checkpoint at
  or before `as_of`; otherwise it uses the origination date and original principal. A loan with
  neither raises `ScheduleError("no_anchor")`.
- `build_schedule(terms, rate_timeline, costs, overpayments, anchor, as_of, strategy,
  day_count) -> Schedule` builds monthly rows. `terms.term_months` is the number of payments from
  the chosen anchor: F7.3/F7.5 must pass the remaining months when re-anchoring at a checkpoint.
- Rows start at `first_payment_date` when it is after the anchor; after a mid-term checkpoint the
  first row is the next nominal payment day (clamped to month end). Later rows continue from that
  first date and the nominal payment day. The first ACTUAL_365 interest period runs from the
  anchor date to the first row; later periods run from the previous row date.
- Rate periods are sorted; duplicate effective dates raise `ScheduleError("duplicate_rate")` and
  a row with no effective rate raises `ScheduleError("missing_rate")`. The latest rate at or
  before the row date applies.
- The annuity principal is the rounded difference between the unrounded payment computed for
  the current opening balance and remaining contractual months, and the unrounded interest. This
  is the same component `numpy_financial.ppmt` reports. The payment is recomputed at a rate
  change and, for `reduce_installment`, after an overpayment. With `reduce_term` the payment is
  held, so the term shortens. `installment_override` replaces the calculated payment.
- Equal principal uses `half_up(anchor.balance / term_months)` as the regular principal; the
  final contractual row clears the residual. `reduce_installment` has no effect; overpayments
  shorten the term.
- Costs are inclusive on both dates; every active component is summed. A percent cost is a
  monthly percent of the opening balance, rounded half up per component; fixed costs add as-is.
- `fixed_extra` adds the configured amount. `target_total` uses
  `max(0, target - principal - interest - costs)` and is capped so the closing balance cannot go
  negative. One-off overpayments apply at the first due date on or after their date, only when
  their date is strictly after the anchor, and are ignored after payoff.
- The final contractual row clears any remaining balance; rows stop as soon as the closing
  balance is zero. `installment_total = interest + principal + overpayment + costs`.
- `summarize_schedule(schedule, as_of)`: a payment on `as_of` is treated as made. Remaining
  principal is the last closing at or before `as_of` (or the anchor balance); next payment is
  the first row after `as_of`; remaining interest/costs are the sums of future rows; payoff is
  the last row date; months remaining is the number of future rows; percent repaid is
  `(original - remaining) / original` in basis points clamped to 0..10000, using the anchor
  balance when the original principal is absent. Total fields are lifetime totals and include
  overpayments.
- A zero anchor balance returns an empty schedule and a zero summary. Invalid input uses
  `LoanValidationError`; schedule-only problems use `ScheduleError` with a `code`.
- `numpy-financial` is a test-only dependency (BSD-3-Clause) used as the independent annuity
  oracle.

---

## F7.3 Loan page: overview, schedule and charts

**Acceptance**
- Loan overview: remaining principal today, next payment (date + amount breakdown), current rate (with reference + margin), % repaid with progress bar, payoff date, total interest remaining/lifetime, total costs, months remaining.
- Schedule table (paged, highlights "today", shows past rows as projected-or-actual when payments exist), CSV export.
- Charts: balance over time (baseline vs with overpayments), stacked principal/interest/costs per month.
- Rate-change markers on the charts.
- All numbers come from `build_schedule()`; the UI contains no finance math.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F7.3 from docs/specs/M7-loans-and-mortgage.md: loan overview page, schedule table with CSV export, ECharts balance and stacked payment charts.
Service layer calls build_schedule(); templates only render. Tests: service returns the same summary as the domain function for a fixture loan.
```

### As built (M7.F7.3)

- `loans.day_count` was added by `0052_f73_loan_day_count` (String(16), not null, server
  default `monthly`, check `monthly|actual_365`). Create keeps the monthly default; the edit
  terms page exposes **Monthly (12 equal periods)** and **Actual/365**. `update_loan_terms`
  accepts `day_count=None` to keep the stored value.
- `LoanView` now carries `reference_rate_values` and `day_count`; the schedule service maps the
  view to `LoanTerms`, fixed or variable `RatePeriod`s, `CostPeriod`s, one-off overpayments and
  checkpoints. It builds the configured schedule and a no-overpayment baseline, then calls
  `summarize_schedule`. No finance math lives in the service or UI.
- The loan detail page shows the overview, a 24-row schedule page, a CSV download and two
  ECharts charts. The default schedule page contains today's row or the next future row. The
  today/next row is highlighted and past rows are marked projected. The table scrolls inside its
  own container on phones; no critical column is hidden.
- CSV is viewer-readable, all rows, UTF-8 without BOM, CRLF, header
  `n,date,rate,opening,interest,principal,overpayment,costs,total,closing,currency`, plain
  decimals via `plain_amount`, `Content-Disposition: attachment`, `Cache-Control: no-store`.
  A schedule error returns 409.
- Charts use server-formatted labels, amounts and tooltips; the browser receives minor units and
  no money arithmetic. Rate changes are vertical dashed mark lines (and appear in the axis
  tooltip). The balance chart compares the configured plan with the no-overpayment baseline and
  ends a shorter plan series with nulls. The payments chart stacks principal, interest, costs and
  overpayment when present. Both have **Plotted data** tables; canvases stay hidden without JS.
- Build errors are mapped to short translated explanations; the schedule, charts and CSV are
  omitted until the terms can produce a schedule.

---

## F7.4 Overpayment scenarios (what-if)

**Story:** I can see what happens if I overpay more, less, or if the rate changes.

**Acceptance**
- Baseline = same loan with no overpayments; "current plan" = configured overpayments; **what-if** = up to 3 user-defined variations (different overpayment mode/amount, strategy, rate shift ±X percentage points from a chosen date, one-off lump sum).
- Comparison table: payoff date, months saved vs baseline, total interest, **interest saved vs baseline**, total paid; chart overlaying balances.
- What-ifs are ephemeral by default; "save scenario" persists name + parameters (`LoanScenario` model).
- Results are deterministic and use the same engine.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F7.4 from docs/specs/M7-loans-and-mortgage.md: scenario parameter object, comparison service calling build_schedule() for baseline/plan/what-ifs,
comparison table + overlay chart, optional LoanScenario persistence (model + migration). Tests: interest saved equals baseline interest minus scenario interest;
identical parameters give identical results.
```

### As built (M7.F7.4)

- `LoanScenario` was added by `0053_f74_loan_scenarios`: workspace-scoped with a composite
  `(workspace_id, loan_id)` foreign key. `name`/`name_key` identify a scenario within the loan;
  a null overpayment mode, strategy, rate shift or one-off pair means "same as the loan". The
  rate shift is signed basis points (`0` is stored as null), and the one-off amount is positive.
  Checks cover each parameter combination and the date range. Unique
  `(workspace_id, loan_id, name_key)`; index `(workspace_id, loan_id, name)`.
- `domain.loans.shift_rate_timeline()` shifts the rate in force from a date and all later
  periods, inserts an effective period when the date falls between two changes, appends one
  after the last and shifts everything when the date precedes the first. Duplicate dates,
  missing rates and a result outside 0..10000 bp raise `duplicate_rate`, `missing_rate` or
  `invalid_rate_shift`.
- `services.loan_schedule` exposes `LoanScheduleInputs`,
  `load_loan_schedule_inputs()` and `build_inputs_schedule()`; the F7.3 `loan_schedule()`
  behaviour is unchanged. `services.loan_scenarios` compares the baseline, current plan, saved
  scenarios and up to three ephemeral rows with the same engine, anchor, costs and day count.
  A scenario row replaces the overpayment mode/amount, strategy, rate timeline or one-off list
  only when set. A calculation error marks that row; a broken loan is a page-level error.
- `months_saved` is the baseline row count minus the scenario row count (positive = sooner).
  `interest_saved` is the baseline's lifetime interest minus the scenario's lifetime interest.
  Identical parameters produce identical schedules and totals.
- The page is `GET/POST /loans/{loan_id}/scenarios` (`loan_scenarios`). It shows the comparison
  table, the overlay balance chart with a plotted-data table, the saved list with delete
  confirmation and three what-if forms. A non-empty name saves/upserts by
  `(loan_id, name_key)`; an empty name is ephemeral. Named rows are validated before any save.
- Viewers may load the page, compare ephemeral rows and see saved scenarios; editors/owners
  save and delete. Audit events record `loan_scenario.created`, `.updated` and `.deleted`
  without amounts. A scenario of another workspace or another loan is `NotFound`.
- `LoanScenario` is excluded from the archive until F7.8 but included in the workspace
  emptiness check. The demo excludes it with the reason "scenarios are user what-ifs, not
  sample data".

---

## F7.5 Actual payments and checkpoints

**Acceptance**
- Record actual payments (installment/overpayment) with date and amounts, or accept a single total and let the app split it per the schedule; link optionally to the ledger `Transaction`.
- Add **balance checkpoints** from bank statements; the schedule re-anchors on the latest checkpoint and shows the **variance** between the projected balance and the checkpoint at that date.
- Posting an installment occurrence from Upcoming (F7.6) can create a `LoanPayment` automatically (setting).
- Past rows with recorded payments display actual values.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F7.5 from docs/specs/M7-loans-and-mortgage.md: LoanPayment model + migration, payment recording UI/service, checkpoint re-anchoring and variance display,
optional ledger transaction link. Tests: re-anchoring changes the future schedule only; variance computation; actuals override projections for past rows.
```

### As built (M7.F7.5)

- `LoanPayment` was added by `0054_f75_loan_payments`: workspace-scoped with composite
  `(workspace_id, loan_id)` and optional `(workspace_id, transaction_id)` foreign keys. `kind`
  is `installment|overpayment`, `source` is `manual|upcoming`, the total and every component are
  non-negative minor units, and a check enforces `total = principal + interest + costs +
  overpayment`. The same migration adds the unique `(workspace_id, loan_id, as_of)` constraint
  on balance checkpoints; F7.1 created at most the starting checkpoint.
- Recording a payment accepts one total or an explicit breakdown. The pure
  `domain.loans.split_loan_payment()` finds the projected installment row (exact date or the
  first later row) and splits: equal keeps the row components, greater adds the excess to
  overpayment, smaller fills interest, costs, principal and overpayment in that order.
  `kind=overpayment` assigns the whole total to overpayment. An explicit breakdown must be
  non-negative, in the loan currency and sum to the total. Edits re-split when the total or kind
  changes and no breakdown is supplied; otherwise the stored components remain.
- A payment may link one ledger transaction. The service checks that it exists, is not
  soft-deleted, is an expense, uses the loan currency and has exactly the payment total.
  `TransactionRepository.recent_expenses()` supplies the form select.
- `services.loan_payments` owns payment and checkpoint CRUD, locks the workspace and re-checks
  the editor role inside writes, and audits ids/dates/kinds without amounts. Payments are
  viewer-readable; writes need editor or owner.
- Checkpoints are dated today or earlier and not before origination. A duplicate date is
  refused. The first checkpoint of a current-balance loan is the starting anchor: it has no
  projection or variance and cannot be deleted. Every other checkpoint projects from the latest
  earlier checkpoint, otherwise origination, with the F7.2 engine; the projected balance is the
  last closing at/before the date (or the anchor balance). Variance is checkpoint balance minus
  projection. A projection error marks that row instead of failing the list.
- `services.loan_schedule` now also loads actual payments. Installment payments are aggregated
  by date and replace the components/total in the schedule table with an **Actual** badge;
  overpayment-only payments stay in the payment list. The closing column remains projected.
- The detail page links to **Payments**; the page has responsive payment/checkpoint tables,
  dialog forms, edit/delete actions, CSRF, gettext and no inline scripts or styles.
- The archive excludes `LoanPayment` until F7.8 but includes it in the emptiness check. The demo
  excludes actual payments as user records.

---

## F7.6 Integrations: net worth, property LTV, scheduled payments

**Acceptance**
- Register `loan_balance` resolver (M5 framework) returning remaining principal at any `as_of` (uses recorded checkpoints/payments where available, projection otherwise); helper creates a linked liability item in the Mortgage category.
- Link a loan to a property **net-worth asset item** (F5.7, Real estate category; owner decision 2026-11-09, replacing the M6 instrument link) → show **LTV** = remaining principal / the item's effective value, and equity = value − remaining principal, both in the base currency and at any `as_of` (the item's valuation history, F5.6 estimates included). An item in another currency, or without a value at that date, shows LTV as unavailable with the reason.
- **Scheduled payment generation:** one click creates/maintains a `ScheduledItem`-backed series of installment occurrences (category selectable), `amount_mode=from_loan`: expected amount for each due date comes from the engine (so a rate change updates future pending occurrences automatically).
- Disabling generation removes pending future occurrences only.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F7.6 from docs/specs/M7-loans-and-mortgage.md: loan_balance ValuationResolver, linked liability helper, property LTV/equity display,
and generation of installment occurrences with amount_mode=from_loan (extend the M3 generator; recalculation of pending future occurrences on loan changes).
Tests: resolver values for past/future dates, occurrence amounts follow rate changes, posted occurrences untouched.
```

---

## F7.7 Installment plans (simple fixed installments)

**Story:** For 0% installment purchases and similar, I just track installment amount, remaining count and end date.

**Acceptance**
- `kind=installment_plan`: name, installment amount, remaining count (or end date), payment day, category; remaining liability = installment × remaining count (no interest modelling).
- Decrement remaining count when an installment occurrence is posted; plan closes at 0.
- Appears in net worth (linked liability, "Installments" category) and in Upcoming.
- Overview table of all installment plans: monthly total, total remaining, last end date.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F7.7 from docs/specs/M7-loans-and-mortgage.md: InstallmentPlan model + migration, UI, generation of occurrences via the M3 generator,
decrement-on-post behaviour, linked liability and overview table. Tests for countdown, closing at zero, and idempotent generation.
```

---

## F7.8 Archive extension: loans

Replaces the former workbook "importer extension" (M4 no longer has a spreadsheet importer; owner
decision, 2026-10-18). Loans are entered with the F7.1 wizard; the archive backs them up.

**Acceptance**
- New archive files (a minor format version per ADR-0016) with lineage `id` columns and
  references by id plus name as in M4: `loans.csv`, `loan_rate_changes.csv`,
  `reference_rates.csv`, `reference_rate_values.csv`, `loan_cost_components.csv`,
  `loan_overpayments.csv`, `loan_payments.csv`, `loan_balance_checkpoints.csv`,
  `loan_scenarios.csv`, `installment_plans.csv`. Schedules are not archived (recomputed by the
  engine).
- Restore recreates them in dependency order; standalone import in merge mode only for
  `reference_rate_values.csv` (by reference rate name; same date = duplicate or conflict). Other
  files are restore only.
- The new models join `EMPTINESS_CHECKS`; the M4 round-trip test covers them, and a restored
  loan produces the same `build_schedule()` result as the original.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F7.8 from docs/specs/M7-loans-and-mortgage.md as an extension of the M4 archive (docs/specs/M4-ledger-export-import.md,
ADR-0016): the new files with lineage ids, export, restore order, standalone reference_rate_values.csv import,
emptiness checks, extended round-trip test incl. identical schedules after restore.
```
