# M8 v2 — FIRE planner rework

**Status:** design only. This document replaces the user-facing behaviour of the original
M8 FIRE planner ([M8-fire-planner.md](M8-fire-planner.md)); F8.1–F8.6 remain the as-built
history of the 0.7.x implementation. No feature in this document is implemented until the
owner approves this spec and each feature PR is separately approved.

**Goal:** a single, current-year FIRE plan that is transparent, quick to set up and easy to
keep current. The plan is a live view of the workspace's real data where possible, and a
small set of manual assumptions where the user must decide the future.

Every agent prompt starts with: *"Follow the standard workflow in
docs/specs/00-overview.md §8 (plan first, wait for my go)."*

---

## 1. Owner decisions (2026-11-15)

1. **One plan total.** A workspace has at most one FIRE plan. If a workspace has several
   old scenarios, the user chooses one and the others are deleted. The chosen plan may need
   manual edits to fit the new format.
2. **No start year.** The first projection year is always the current calendar year. Events
   are stored and shown as calendar years, not ages or offsets. If an event's year is now in
   the past, it is **archived**: kept for history, not applied to the projection.
3. **Manual projections with suggestions.** The user enters annual income, expenses and
   portfolio manually. The app calculates suggested values from the last 12 full months
   (and current investable assets) and offers an explicit **Use suggested value** button.
   Suggestions never prefill a field automatically. The expenses suggestion shows the
   category breakdown.
4. **View mode and edit mode.** The plan page is read-only in view mode. Edit mode makes the
   fields editable, recalculates the results, charts and year-by-year schedule after every
   change, and offers **Save changes** / **Discard changes**.
5. **Server-side calculation.** The projection stays in the Python domain layer and is
   recalculated through the existing server-rendered/HTMX flow. No JavaScript projection
   engine is added.
6. **Unified adjustments.** Contribution overrides become one more event kind
   (`saving_override`) in a single adjustments list. The separate contribution table,
   editor and bulk-paste box are removed.
7. **Live loans.** The projection reads active loan schedules directly. It shows the loan
   payment, payoff year and the effect of the payoff in the plan, and recalculates when the
   loan changes. A loan can be ignored, which keeps its payment in the projection and
   produces no expense drop. The old one-click loan-payoff event is removed.
8. **Nominal money only.** The planner shows each year's actual money (nominal). The
   "today's money"/real view and the basis toggle are removed.
9. **No spreadsheet wording** in the UI or user documentation.
10. **Taxes are not modelled.** The planner uses the amounts recorded in the ledger as they are.
    Income may be gross; expenses should include the taxes actually paid. No tax split, tax
    field or tax calculation is added.

---

## 2. Principles

- **Transparency over cleverness.** Every derived number the projection uses (suggested
  income/expenses, loan payments, event effects) must be visible or one click away, with its
  source and as-of date.
- **One source of truth.** The plan stores the user's assumptions; live data (suggestions,
  loans, current asset values) is read from the app at calculation time.
- **No silent zero.** A missing price, FX rate, value or loan schedule is explained; it never
  becomes zero without telling the user.
- **No hidden writes.** Preview/edit calculations never write. Only **Save changes**,
  **Create plan**, **Reset plan** and explicit adjustment actions write.
- **Real data first.** The starting point is today: current year, current portfolio value,
  last 12 full months of actual income and expenses. The future is the user's assumptions.

---

## 3. Non-goals for this rework

- Multiple FIRE plans, comparison, duplicate, default/archive lifecycle, or scenario names.
- Client-side projection or a JavaScript duplicate of the finance math.
- Automatic tax modelling (settled: not modelled in v2; see §10).
- Bank sync, Monte Carlo, Coast FIRE, or tax filing.
- Rewriting the M7 loan module. The FIRE planner reads its schedules; it does not change
  loan terms.

---

## 4. Data model

### 4.1 One plan

The existing `fire_scenarios` table is reshaped in place and keeps its name for migration
continuity, but a workspace has **one row**:

`FireScenario(id, workspace_id, created_at, updated_at, birth_year, stop_working_age,
horizon_age, inflation_bp, real_return_bp, income_growth_bp, rule, swr_bp, income_minor,
expenses_minor, portfolio_minor, currency, needs_review)`

- `workspace_id` is unique. There is no `name`, `start_year`, `is_default`, `archived_at`,
  `income_mode`, `expenses_mode`, `portfolio_mode`, or manual/linked pair.
- `currency` is a snapshot of the workspace base currency when the plan is created or
  migrated. If the workspace base currency changes, the plan shows a blocking review notice
  instead of silently reinterpreting amounts.
- `needs_review` is set on migrated plans. The view page shows a banner until the user opens
  edit mode and saves.
- `income_minor`, `expenses_minor` and `portfolio_minor` are non-negative integer minor
  units in `currency`; zero is allowed but shown as zero, not as missing.
- There is no user-facing plan name. The page is titled **FIRE plan**.
- `FireLoanSetting(workspace_id, scenario_id, loan_id, included)` stores one row only for an
  ignored loan; absence means included. Composite workspace foreign keys cascade with the plan
  and restrict loan deletion. Existing plans start with every loan included.
- Deleting/resetting the plan hard-deletes the row, its events and its loan settings after typed
  confirmation; an audit event is recorded. A workspace with no plan opens the wizard.

### 4.2 Events (adjustments)

`FireEvent(id, workspace_id, scenario_id, year, kind, amount_minor, label)` with kinds:

| Kind | Meaning |
|---|---|
| `cash` | One-off cash flow added to net worth in that year |
| `income_change` | Permanent annual income change from that year onward |
| `expense_change` | Permanent annual expense change from that year onward |
| `saving_override` | Replaces that year's net saving; one per year |

- Events are stored and displayed as calendar years. The UI may offer an inline age→year
  helper, but no age is stored.
- An event whose year is before the current year is **archived by calculation**, not by a
  stored flag: it is shown in a collapsed **Archived adjustments** section, is read-only for
  the projection, and can be deleted.
- A `saving_override` must be unique per plan and year. Other kinds may repeat in a year and
  are summed by kind/year, with one rounding.
- `source_loan_id` and one-click loan-payoff events are removed. Loans are live (§7).
- `FireContribution` is removed; existing rows become `saving_override` events during
  migration.
- Cash, income-change and expense-change events may start in the current year and run through
  the horizon year. A saving override starts in `current_year + 1` because the current row
  uses the starting portfolio as its net saving. Past events are archived, not validation
  errors.

### 4.3 Suggested values

Suggestions are **not stored**:

- **Income:** annualized regular income from the last 12 full calendar months, using the
  distinct months with data (gaps are not zero). The current partial month is excluded.
- **Expenses:** annualized regular expenses over the same window, with a category
  breakdown. The amount includes loan payments (see §7), and the UI shows the loan portion.
- **Portfolio:** current investable assets: the M6 investable look-through, the unallocated
  bucket and active manual M5 items that are investable and included in net worth, converted
  to the plan currency with the latest FX. Missing prices/values/FX block the suggestion;
  the user can still type a value.

A suggestion is used only when the user clicks **Use suggested value** (or **Use all
suggestions**). The copied number becomes the saved manual amount; the suggestion itself
keeps updating from real data and shows when it differs from the saved value.

---

## 5. Projection

### 5.1 Inputs and horizon

- `current_year` = the year of `ctx.clock.today(workspace.timezone)`.
- The first row is `current_year`. The last row is `birth_year + horizon_age`.
- `age_at_start = current_year - birth_year`; it is recalculated whenever the plan is
  viewed. `horizon_age` remains the age at the last projected year.
- `stop_working_age` remains the age at which base income stops. Base income is paid while
  `age < stop_working_age`; the age-equal year has no base income.
- If the current age is at or beyond the horizon, the plan is invalid and blocks with a
  clear message. If the user is already past the stop age, income is zero and a warning
  explains it.

### 5.2 Yearly model (nominal only)

For each year from `current_year` to the last year:

```
age        = year - birth_year
income     = base income grown while age < stop_working_age, plus active income_change events
expenses   = base expenses inflated, plus active expense_change events
loan_drop  = sum of included loans whose projected payoff year <= year
             (their recurring annual payment; §7)
net        = saving_override if present else income - (expenses - loan_drop)
gain       = opening net worth * ((1 + inflation) * (1 + real return) - 1), rounded once
net worth  = opening + net + gain + cash
```

The nominal gain compounds inflation and the return above inflation exactly; it is not the
additive `inflation + return above inflation`. The effective expense line after `loan_drop`
must be non-negative: a negative line is a projection error naming its year, not a FIRE row.

The base expenses already include the loan payments because the suggested expenses are the
actual ledger expenses. `loan_drop` removes the included loan's recurring annual payment from
the payoff year onward; it is not added on top. An ignored loan has no drop.

- `t = 0` (the current year) uses the starting portfolio as its net saving, earns that
  year's gain, then adds a current-year cash event. This convention is retained from the
  existing projection but is described only in plain language in the UI.
- Income changes inflate with income growth from the current year and stay active from their
  event year onward. Expense changes inflate with inflation from the current year and stay
  active from their event year onward.
- A saving override replaces the net line for its year only; it does not alter the displayed
  income, expenses, loan payments or cash.
- All amounts are integer minor units; rates are basis points; one half-up rounding per
  computed line. No floats.

### 5.3 Metrics

- **FIRE age/year:** the first row that satisfies the selected rule, using nominal values.
  Under **Returns cover expenses** the row's opening net worth times the **return above
  inflation** must strictly exceed that year's nominal expenses (`opening * real_return_bp >
  expenses * 10000`), so the portfolio's real capital is preserved; the nominal gain does not
  enter this test. The withdrawal-rate rule is unchanged.
- **Years to FIRE:** `fire_age - age_at_start`; the current year is year 0.
- **FIRE number (at FIRE):** the nominal annual expenses at the FIRE row divided by the
  selected rule's rate (the SWR, or the return above inflation for
  returns-cover-expenses), rounded once. Shown with its year and a plain-language definition;
  `—` when FIRE is never reached or the rate is not positive. This replaces the v1 "today's
  money" FIRE number.
- **Plan progress:** starting portfolio divided by net worth at FIRE, shown as a percentage
  with the definition; `—` when FIRE is never reached. **Today's-money progress** is removed.
- **Sustainability:** depletion age and lowest net worth after income stops; nominal.
- **No real/today's-money fields or columns.** Charts, sensitivity and the result pages show
  nominal values only.

---

## 6. Screens and flows

### 6.1 `/fire` (view or wizard)

- **No plan:** the setup wizard (§6.2).
- **One plan:** view mode. Read-only summary, charts, year-by-year schedule, adjustments,
  live loans and the sensitivity panel. Editors see **Edit plan**; viewers see no edit
  control. A **Reset plan** action is available to editors with typed confirmation and an
  optional archive download offer.
- **Multiple old scenarios:** a one-time chooser appears before anything else (§8).

### 6.2 Setup wizard

The wizard is the only creation path and is available only when no plan exists.

1. **About you:** birth year (profile prefill allowed), stop-working age, horizon age. Shows
   the current age and the first/last projected year.
2. **Income and expenses:** manual fields, empty by default. Next to each field, a suggestion
   card shows the amount, window, months used and warnings; **Use suggested value** copies
   it. Expenses also show the category breakdown and the loan portion.
3. **Portfolio:** manual field, empty by default, with the current investable-assets
   suggestion and **Use suggested value**.
4. **Assumptions and review:** inflation, real return, income growth, FIRE rule/SWR; a
   final summary and **Create plan**. Once F8.12 exists, this step also shows a read-only
   list of active loans (name, projected payoff year, annual payment, included/ignored).

- **Next/Back** moves between steps; each step validates before continuing.
- **Cancel** leaves no plan.
- No events or loan edits in the wizard; they are added after creation in edit mode. Until
  F8.12 lands, the review step notes that active loans will be included after creation
  rather than showing a loan list.
- A plan created by the wizard has `needs_review = false`.
- The wizard never prefills a suggestion; only an explicit button copies a value.

### 6.3 View mode

Read-only and viewer-accessible:

- key metrics and the plan-progress definitions;
- nominal net-worth/income/expense charts;
- the year-by-year schedule with income, expenses, loan payments, net saving, gain, cash and
  net worth;
- **Adjustments** (active and archived), with years, kinds, amounts and labels;
- **Loans:** each active loan's name, current annual payment, projected payoff year,
  include/ignore state, and the effect on the plan;
- the sensitivity panel (nominal FIRE age only);
- a **needs review** banner for migrated plans.

### 6.4 Edit mode

- Separate route, e.g. `GET /fire/edit`, editor-only; viewers are refused.
- All plan fields and the adjustments editor are editable.
- Results, charts and the schedule recalculate **server-side after every change** (existing
  HTMX preview pattern). No client-side projection.
- **Save changes** validates, writes and returns to view mode with a success notice.
  Invalid input shows inline errors and does not overwrite the saved plan.
- **Discard changes** restores the last saved values; leaving with unsaved changes asks for
  confirmation.
- Loan data is read live; editing a loan in another tab is reflected on the next
  calculation. The loan section is not a snapshot.
- Saving clears `needs_review`.

---

## 7. Live loans

The planner reads the M7 loan schedules at calculation time, never from saved FIRE events.

For each active loan in the plan currency:

- Show the name, **annual payment**, **projected payoff year**, and the current balance if
  useful. The annual payment is the next scheduled monthly payment × 12, using its recurring
  components (interest + principal + costs + regular overpayment) and excluding one-off
  overpayments, which affect only the payoff date.
- **Include in plan** (default on): the loan payment is part of the expenses the plan
  already contains; from the projected payoff year onward the projection removes that
  annual payment. This is the expense drop.
- **Ignore in plan:** the loan is shown but produces no expense drop; its payment stays in
  the plan through the horizon. This is the explicit way to say "I do not want this loan to
  change the projection."
- Changing the loan's overpayment plan, rate, term or payoff date changes the FIRE plan on
  the next view/edit calculation. No saved event is created and no button is needed.
- **Regular overpayments are included** in the annual payment. One-off overpayments affect
  the projected payoff date but are not part of the recurring annual payment. This fixes
  the v1 bug where the suggested drop excluded regular overpayments while the expense
  baseline included them.
- Multiple loans are summed; each has its own payoff year.
- Loans in another currency, archived/paid-off loans, and loans with invalid schedules are
  shown with a reason and excluded from the adjustment. They never silently become zero.
- The loan payment is part of the expenses baseline. The UI must state this and show the
  loan portion so a user who enters expenses without loan payments can correct it. The
  suggested expenses include the loan payments found in the ledger.

Resolved in F8.12: the annual payment is the next scheduled monthly payment in a schedule
that keeps past/current one-off overpayments but excludes future ones, multiplied by twelve.
It includes interest, principal, costs and the regular overpayment. A future one-off changes
only the projected payoff year. The drop starts in the payoff year, is not prorated and is not
inflated. The ignored-loan choice is persisted per plan and archived in format 2.1.

---

## 8. Migration from v1

### 8.1 Choosing the plan

- Zero scenarios: no migration; `/fire` starts the wizard.
- One scenario: migrate it automatically and set `needs_review`.
- More than one: `/fire` shows a one-time chooser listing active and archived scenarios
  with name, start year/age, default/archived badges and counts. The user selects one and
  confirms that the others will be **deleted**. An archive download can be offered before
  deletion. The chosen plan is migrated; all other FIRE scenario rows and their children are
  hard-deleted with an audit event.
- Because the chooser runs in the app, the schema migration must not require a single row
  before the user has chosen. Use a nullable legacy/current marker and a **partial unique
  index on the live plan**, or an equivalent two-step migration. Existing scenario rows stay
  readable until the chooser deletes them; no data is lost before the choice. The partial
  index and the legacy cleanup are removed once the migration window is over.

### 8.2 Field mapping

| v1 | v2 |
|---|---|
| `birth_year`, rates, stop/horizon, rule, SWR | preserved |
| manual income/expenses/portfolio | preserved as the new manual amounts |
| linked income/expenses/portfolio | resolved once at migration time and stored as the manual amount; if resolution blocks, store 0 and add a warning/needs review |
| `FireContribution` rows | `FireEvent` kind `saving_override`, same year/amount/label |
| existing events | preserved by kind; years kept |
| one-click loan payoff events (`source_loan_id`) | removed; live loans replace them |
| events/overrides with year < current year | kept as archived events, not applied |
| `name`, `start_year`, `is_default`, `archived_at`, modes | removed |
| scenario currency | kept as the plan `currency` snapshot; if it differs from the current workspace base, block with a review notice |

- The plan is created with `needs_review = true` even when most fields map cleanly, because
  the new current-year anchor and manual-only model change the results. The view page says
  what changed and links to edit mode.
- Migration is a one-way data migration; the old scenario table is reshaped, not kept as a
  parallel model.
- Archive restore of a 1.8 archive with multiple FIRE scenarios imports the default active
  scenario (or the first active, or the first archived) and lists the skipped scenarios as
  warnings; the user can edit the imported plan afterwards.

---

## 9. Archive format (planned 2.2)

The archive extension is updated in the same feature that reshapes the model, so the archive
never describes a model that no longer exists.

- Format version **2.0** (a breaking layout change from 1.8; import support for ≤1.8 remains).
- Format **2.1** adds `fire_loan_settings.csv` to the 2.0 files `fire_plan.csv` (one row,
  header-only when no plan) and `fire_events.csv`. No `fire_contributions.csv`,
  `fire_expense_exclusions.csv` or `fire_scenarios.csv`. A 2.0 archive still reads with every
  loan included.
- Format **2.2** adds `lasts_to_horizon` to the `fire_plan.csv` rule values. A 2.1 archive still
  reads with the two older rules; the new rule stores a blank `swr_bp`.
- The plan row contains birth year, stop/horizon ages, rates, rule/SWR, manual amounts,
  currency and `needs_review`. Suggested values, loan schedules and projection results are
  derived and not archived; the plan's ignored-loan choices are user intent and are stored in
  `fire_loan_settings.csv`.
- Events export kind, year, amount and label. Past events are included as archived history;
  they are not applied on restore unless still in the future.
- 1.8 archives import through the migration mapping in §8.2; multiple FIRE scenarios follow
  the default/first rule and produce warnings.
- `docs/archive-format.md` and its golden fixtures are updated in the same PR; old fixtures
  remain readable.

---

## 10. Taxes (settled 2026-11-15: option B)

The planner does **not** split income into gross and net, and it does **not** calculate or model
taxes. It uses the amounts the user enters and reads from the ledger as they are:

- income may be gross or net, depending on what the ledger records;
- expenses should include the taxes actually paid;
- suggestions use the ledger's recorded cash flows and carry the same treatment;
- no result, field or help text promises an after-tax amount.

Not modelled in v2: income-tax disappearance when income stops, an effective income-tax rate,
capital-gains tax on withdrawals or gains, and derived rates from the M3 tax rules. These are
follow-ups; the owner chose option B, so F8.14 adds no tax field and no tax calculation.

---

## 11. Features

Each feature is a separate PR. Every PR must leave `make check` green, keep existing data
readable, and update its own tests, i18n and user docs. A feature may be split into stacked
PRs if that keeps review manageable; the acceptance criteria remain the unit of done.

### F8.8 — One-plan model, migration and archive 2.0

**Acceptance**

- The schema has one live FIRE plan per workspace (a partial unique index on the live plan
  while legacy rows await the chooser; a plain unique `workspace_id` after cleanup), no name/
  start year/default/archive/modes; manual amounts, currency snapshot and `needs_review`.
- `FireEvent` supports `cash`, `income_change`, `expense_change`, `saving_override`; the
  contribution table is migrated and removed.
- Old scenarios are chosen/deleted as in §8; one scenario auto-migrates; linked values are
  resolved or zeroed with a warning; one-click loan payoff events are removed.
- The projection starts in the current year from the injected Clock; past events are
  archived and ignored.
- Archive export/import is format 2.0 with `fire_plan.csv` + `fire_events.csv`; 1.8 archives
  still import with warnings.
- The current FIRE page is adapted to the single plan and current-year model; the list,
  compare, duplicate, default and archive/restore routes are removed or redirect.
- Tests: migration chooser, tenancy, unique workspace, all mapping paths, current-year
  rollover, archived events, archive round-trip and 1.8 compatibility.

**Agent prompt**

```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F8.8 from docs/specs/M8-fire-planner-v2.md: one-plan schema/migration, chosen-plan
deletion, current-year projection anchor, saving_override events, removal of FireContribution
and loan-payoff events, and archive format 2.0. Keep the app green and the archive readable.
No wizard, view/edit split or live-loan UI yet.
```

### As built (M8.F8.8)

- The `fire_scenarios` table is rebuilt in place: one live plan per workspace, `needs_review`,
  a currency snapshot and the three manual amounts. The temporary `is_live` and `legacy` columns
  keep old scenarios readable until the chooser runs; `uq_fire_scenarios_live_workspace` is a
  partial unique index while the migration window is open. A later cleanup revision will drop
  the temporary columns and replace it with a plain unique `workspace_id`.
- `fire_contributions` and `fire_expense_exclusions` are removed. Contribution rows become
  `saving_override` events labelled **Saving override**; one-click loan-payoff events are
  deleted and `source_loan_id` is gone. Events keep calendar years and the four kinds.
- `0063_f88_one_fire_plan` copies the data in Python and rebuilds the FIRE tables, which keeps
  the change portable to SQLite and PostgreSQL. The downgrade is structural and lossy for
  scenarios deleted by the chooser.
- A single legacy scenario auto-migrates on the first editor visit to `/fire`; several rows show
  a one-time chooser. The chosen row is migrated, the others are hard-deleted and the action is
  audited without amounts. Viewers see the chooser but cannot convert it.
- The projection anchor is the injected clock's current year. Past adjustments are stored as
  archived history and are not applied; a saving override that becomes the current year is
  archived and the rest of the plan can still be saved.
- The current results/inputs page is adapted to one plan. Compare, the scenario list, duplicate,
  default, archive/restore, linked-mode selection, exclusions and loan suggestions are removed;
  sensitivity, CSV, charts, preview and the nominal/real view remain until F8.13.
- Archive format 2.0 adds `fire_plan.csv` and the new `fire_events.csv`, and removes the four
  1.8 FIRE files. Export while several legacy rows await the choice writes the default/first
  scenario, resolves linked inputs at export time, sets `needs_review=true` and records
  `fire_legacy_scenarios_skipped`; a linked input that cannot be resolved is written as zero and
  its field is named in `fire_plan_zeroed_fields`, never silently. The reader upgrades 1.8
  archives in memory (default/first, fallbacks, contribution conversion, loan-payoff removal,
  skipped-scenario warnings). A 2.0 restore preserves `needs_review`.
- `fire_linked` resolution internals remain for the migration/export and F8.9 suggestions;
  expense-exclusion validation and loan-payoff code are gone.
- A currency mismatch blocks projection and saving with a notice; reset/recreate is left to
  F8.9. The reload/restore UI is also F8.9; F8.8 provides the service and audit.
- Tests cover the migration chooser, partial unique index, manual/linked/blocked mappings,
  contribution conversion, archived events, year rollover, deletion audit, archive round trip,
  1.8 imports, tenancy, roles and query budgets.

### F8.9 — One-plan UX and setup wizard

**Acceptance**

- `/fire` shows the view page when a plan exists and the wizard when it does not.
- Wizard steps: about you, income/expenses, portfolio, assumptions/review; Next/Back,
  validation, Cancel leaves no plan, Create plan writes once.
- Suggestions are shown but never prefill; **Use suggested value** copies income, expenses
  and portfolio; expenses show the category breakdown and loan portion; the current assets
  suggestion is available.
- The wizard notes that active loans will be included after creation; the read-only loan
  review list is added in F8.12. No loan edits.
- List/compare/duplicate/default/archive controls are gone; **Reset plan** exists with typed
  confirmation, optional archive download and audit.
- Tests: wizard guards, one-plan rule, no prefill, suggestion buttons, validation, cancel,
  reset, role checks, i18n.

**Agent prompt**

```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F8.9 from docs/specs/M8-fire-planner-v2.md: the one-plan view shell, setup wizard,
suggestion cards with explicit Use buttons, category breakdown and Reset plan. No view/edit
editing form beyond the wizard and no live-loan calculation changes.
```

### As built (M8.F8.9)

- **Entry.** `GET /fire` renders the view directly when a live plan exists (no id in the URL),
  the chooser while legacy rows await the choice, the wizard step 1 when the workspace has no
  plan (editors/owners), and a read-only empty state for viewers. `GET /fire/{scenario_id}` keeps
  old links working: it verifies the live plan id (foreign/unknown ids are 404) and redirects to
  `/fire`, preserving `basis`, `sensitivity`, `edit`, `notice` and `error`. `/fire/new` and
  `/fire/compare` remain 303 redirects. The old `POST /fire` create route and `fire/form.html`
  are gone.
- **Wizard.** `GET /fire/setup` and `POST /fire/setup`; one POST per step, no server draft and no
  write before Create. Hidden fields carry the other steps, `step` is 1–4, `go=next|back|review|create`,
  and `use=<field>` is the server-side **Use suggested value** action (no JavaScript). Every POST
  re-validates steps 1..current, and a hidden-field error returns to the owning step. Cancel is a
  plain GET to `/fire?cancelled=1`; Cancel/Back/Next/Review/Use never write. The review renders a
  hidden digest of every value it summarises; Create re-validates and refreshes the review instead
  of writing when a step-4 field, the workspace currency or the current year changed since that
  render, so the summary the user confirms is always the plan that is saved. Create calls
  `create_fire_plan` once under the workspace lock; it re-validates, refuses an existing plan or
  legacy rows, snapshots the currency, sets `needs_review=false` and audits
  `fire_plan.created`. A double submit redirects with `plan_exists`. The wizard never migrates a
  legacy row (`plan_entry(auto_migrate=False)`) and never reads event or loan fields; forged
  fields are ignored. A base-currency change between steps is not converted; the refreshed review
  makes the user confirm the new currency.
- **Steps.** About you (birth year from the profile, horizon from the profile's life expectancy,
  stop-working age empty/required; current age and first/last year shown); income and expenses
  (empty by default; empty is `amount_required`, explicit 0 valid); portfolio (empty by default);
  assumptions/review (illustrative rates, rule/SWR, unusual-rate confirmation only when needed,
  a text summary, the unconditional F8.12 loan-note placeholder and the disclaimer). No
  projection metrics or charts are shown in the wizard.
- **Suggestions.** `services.fire_linked.setup_suggestions` reuses the F8.4 trailing-12-full-month
  income/expense resolution, its annualization, warnings and as-of date, and the M6/M5 investable
  portfolio look-through. The expenses card shows the category breakdown as **raw window totals**
  plus the window total and the annualized headline. The **loan portion** is the subset of
  regular expense rows in the window linked from `loan_payments.transaction_id` (one grouped
  query, `TransactionRepository.regular_window_loan_totals`); it is a window total, and when no
  rows are linked the card explains that instead of showing a zero. F8.12 adds the live scheduled
  annual payment separately. Suggestion cards never prefill; Use copies the locale-formatted
  amount into the field. A blocked card drops its series/portfolio value, shows the translated
  reason, hides Use and keeps the manual field, so it never displays a placeholder zero. The
  portfolio card's source rows link to the asset class (`/investments/classes#asset-class-<id>`)
  or the net-worth item (`/net-worth?side=asset#item-<id>`); the unallocated bucket has no source
  page. No new dependency, model or migration.
- **Reset.** `GET/POST /fire/reset`, editor-only and CSRF-protected. The page shows the adjustment
  counts, a currency-mismatch warning, a link to the workspace archive (`/workspace/data`) and a
  typed confirmation of the fixed ASCII word `reset` (trimmed, case-insensitive). A wrong/empty
  confirmation returns 400 and deletes nothing; the existing `delete_fire_plan` hard-deletes the
  plan and events and audits `fire_plan.deleted` with `events_deleted` and no amounts. Success
  redirects to `/fire?notice=reset`, which starts the wizard. A currency-mismatched plan can be
  reset (the escape hatch F8.8 left for this feature). Viewers get 403 and see no reset link.
- **View shell.** The editor keeps the existing inline Plan inputs/preview panel until F8.10. The
  view header adds **Reset plan** (editors only) and moves Edit, the needs-review link and the
  sensitivity page/inputs links to the id-free `/fire` URL. The currency-mismatch panel links to
  Reset. `PlanEntry` now returns the already-loaded detail so the view page does not load the
  plan and events twice. The needs-review banner links editors to `/fire/edit#amounts`.
- **Tests.** Wizard entry/steps/back/cancel/validation/no-write/create-once/double-submit,
  hidden-field carry, suggestions and no prefill, Use copying, category breakdown, loan portion,
  blocked suggestions without a zero, portfolio source links (asset class and net-worth item), no
  events/loans (including malformed forged fields), review, the stale-review refresh (field and
  currency), unusual rates, profile prefill, viewer/legacy guards, CSRF, Polish rendering, reset
  confirmation/audit/roles/CSRF/currency mismatch, the no-plan 404 and the id redirect; existing
  F8.8 results, preview, CSV, sensitivity and chooser tests were updated to the id-free view URL
  while the preview/save/CSV routes stay id-based for F8.10. Chrome covers the full wizard, Use
  buttons, reset, 390/1440 widths and CSP. The new transaction repository method is in the tenant
  coverage/test. All tests ran single-core.


### F8.10 — View/edit modes and suggested inputs

**Acceptance**

- View mode is read-only for editors and viewers; edit mode is editor-only and separate.
- Edit mode recalculates summary, charts and the year-by-year schedule after every change
  through the server-rendered/HTMX preview; no client-side projection.
- Save validates and persists, returns to view with a notice, and clears `needs_review`.
  Discard restores the last saved values; leaving with unsaved changes confirms.
- Suggestion cards show the window, months used, as-of date, warnings and category breakdown;
  **Use suggested value** copies into the field; the saved value is never overwritten
  automatically.
- Tests: mode permissions, save/discard, dirty guard, live recalculation, suggestion copy,
  blocked suggestions, no writes on preview, browser coverage.

**Agent prompt**

```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F8.10 from docs/specs/M8-fire-planner-v2.md: explicit view/edit modes, save/discard,
server-side live recalculation and manual inputs with explicit suggestion buttons and expense
breakdown. No client-side projection.
```

### As built (M8.F8.10)

- **Modes.** `GET /fire` is read-only for every role: results, charts, the year-by-year table, a
  read-only **Plan values** summary, active and archived adjustments, the loans placeholder and
  the saved-plan sensitivity panel. It has no form, no inputs and no preview hooks, and does not
  load `fire.js`. Editors see **Edit plan** (not on a currency mismatch); viewers see no edit or
  reset control. CSV and sensitivity stay viewer-accessible; CSV is hidden when the projection is
  blocked. F8.15 later removed the CSV action and its routes.
- **Editor.** `GET /fire/edit` is editor-only and always separate. It shows the overview/results
  first, then one form with every plan field, the three amount suggestion cards and the existing
  adjustments table. `POST /fire/edit/preview` returns the results fragment for the HTMX live
  recalculation; it loads no suggestions and never writes. `POST /fire/edit` re-validates under the
  workspace lock, persists, audits `fire_plan.updated`, clears `needs_review` and redirects to
  `/fire?notice=saved` (preserving `basis`). Errors render the full edit page with posted values,
  inline messages and `data-fire-dirty="1"`; a truncated save without the adjustment-section
  marker is rejected before the service so it cannot delete all events, while an intentional empty
  adjustment list (marker present, every row removed) still saves. The amount copy stated the
  after-tax default that §10 then recommended; F8.14 replaced it with the ledger-based tax copy.
- **Discard and dirty guard.** **Discard changes** is `GET /fire/edit?discarded=1`, stays in edit
  mode, reloads the saved values and shows a notice. **View plan** leaves; while the form is dirty,
  `fire.js` sets `data-fire-dirty=1`, shows a translated **Unsaved changes** status and uses the
  native `beforeunload` prompt. Basis changes are not dirty. The server-rendered error page starts
  dirty.
- **Suggestions.** The edit page calls `services.fire_linked.setup_suggestions` once for income,
  expenses and portfolio; the F8.9 cards gained the data as-of line and an optional saved-value
  note and copy button. **Use suggested value** copies a server-formatted `data-suggested-input`
  string into the field; no server route, no JS number parsing, no automatic overwrite. Blocked
  cards keep their reason, hide Use and never show a zero. Suggestions resolve on page load and
  error renders, not on every preview, and never write.
- **URLs.** Canonical URLs are id-free: `/fire/edit`, `/fire/edit/preview`, `/fire/sensitivity`,
  `/fire/results.csv`. Old GET id URLs (`/fire/{id}`, `/fire/{id}/edit`, `/fire/{id}/sensitivity`,
  `/fire/{id}/results.csv`) verify the live plan and redirect; the old POST preview/save id routes
  are gone. `/fire?edit=1` redirects to the editor for a live plan and is ignored otherwise.
  `/fire/edit` never migrates legacy rows; it redirects to `/fire` for no plan, legacy rows or a
  currency mismatch. The CSV filename is `fire-plan.csv`.
- **Scope.** No schema, migration, projection-math or dependency changes. The F8.8 adjustments
  table stays editable-with-badge until F8.11; the loans section is a placeholder until F8.12; the
  nominal/real basis remains until F8.13. Cross-tab last-write-wins is accepted for this feature.
- **Tests.** View/edit permissions, no-form view, save/discard/needs-review, error preservation,
  dirty marker, preview/no-writes, malformed partial save, suggestion cards/as-of/differs/copy and
  blocked cards, id-free/legacy URLs, currency mismatch, legacy redirect, CSV/sensitivity,
  query-budget constants, CSP, Polish rendering and Chrome coverage (live preview, copy, dirty
  guard, discard, save, blocked preview, 390/1440 widths). All tests ran single-core.

### F8.11 — Unified adjustments

**Acceptance**

- One adjustments list supports cash, income change, expense change and saving override.
- No bulk-paste box; rows are added individually and edited in place.
- A saving override replaces that year's net saving and is unique per year; other kinds may
  repeat and sum.
- Past events appear in a collapsed read-only **Archived adjustments** section and are not
  applied; they can be deleted.
- The contribution table/editor and their i18n strings are gone.
- Tests: kinds, uniqueness, summing, archived events, age→year helper if added, migration
  compatibility, no bulk input.

**Agent prompt**

```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F8.11 from docs/specs/M8-fire-planner-v2.md: the unified adjustments editor and
archived-event behaviour, including saving_override. Remove the remaining contribution UI
and bulk-paste code.
```

### As built (M8.F8.11)

- **One editor.** The edit page has a single **Adjustments** section with all four kinds. Rows are
  added one at a time, edited in place and removed with a checked **Remove** box; there is no
  bulk-paste, contribution or DOM-deletion path. The trailing blank row is the no-JavaScript
  starting point; the add button clones it before that row and focuses the new year field.
- **Save-all.** Save changes writes the plan fields and every adjustment change in one
  transaction. A checked Remove box is posted with the saved UUID; archived rows use a checkbox
  whose value is the UUID. The service deletes removed ids first, then creates/updates, and leaves
  every other saved row untouched. A missing saved active row is an error, never a deletion; the
  form's `adjustments_present` marker rejects a truncated request before the service.
- **Archived history.** Archived rows are read-only and collapsed with a reason tag: before the
  current year, after the plan's last year, or a saving override in the current year. They are
  not editable or movable; editors and owners can delete one only by ticking Remove and saving.
  Unchanged events may keep a year outside a changed horizon: shrinking archives them, expanding
  restores them, and the preview includes newly active preserved events unchanged. The editor says
  so in the archived section instead of claiming they can never apply, and the empty hint changes
  to **No current adjustments** when only history remains and disappears once a row is added.
- **Validation.** The service owns the four kinds, year windows, one saving override per plan/year,
  the current-year start rule and the F8.8 rollover exception, labels, signed amounts, zero and
  the 200-event limit. `FireError.row`/`column` drive inline row errors; the preview names the
  exact input so `fire.js` can mark it without moving focus. The parser lives in
  `web/fire_adjustments.py`; the route only parses/wires and the projection never writes on
  preview.
- **View mode.** The view page shows the active adjustments as a wrapping read-only list and the
  same archived section with no controls; no form or editor JavaScript is loaded.
- **Tests and docs.** Unit tests cover the parser, all four kinds, explicit removal, archived
  preservation/deletion, strict partial posts, rollover, horizon shrink/expand, uniqueness,
  row errors, limits, audit and query budgets. Web tests cover the editor, preview, save-all,
  archived deletion, read-only view, viewer/CSRF, i18n and CSP; Chromium covers add/edit/remove,
  archive deletion, focus/announcement, dirty state and 390/1440 widths. `docs/fire.md` is
  updated. No schema, migration, projection-math or dependency changes; no age→year helper was
  added.

### F8.12 — Live loans

**Acceptance**

- The plan reads active loan schedules live; no saved loan-payoff events.
- Each loan shows name, annual payment including regular overpayments, projected payoff
  year, and include/ignore state; changing the loan changes the FIRE view on the next
  calculation.
- Included loans remove their annual payment from expenses at the payoff year; ignored
  loans produce no drop and remain in the baseline.
- Multiple loans, currency mismatch, paid-off/archived loans, broken schedules and one-off
  overpayments are handled with visible reasons; no silent zero.
- The UI shows the loan component of expenses and explains that the entered expenses are
  expected to include loan payments. The read-only loan review is added to the wizard's final
  step.
- Tests: the v1 overpayment bug, payoff-year changes, ignore toggle, multiple loans,
  currencies, broken schedules, no writes, transparency copy.

**Agent prompt**

```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F8.12 from docs/specs/M8-fire-planner-v2.md: live loan schedules in the FIRE
projection, include/ignore, regular-overpayment-aware annual payments and transparent loan
UI. Remove the saved loan-payoff event action.
```

### As built (M8.F8.12)

- **Persistence.** `fire_loan_settings` stores one row only for an ignored loan; absence means
  included. Composite workspace FKs cascade with the plan and restrict loan deletion. Migration
  `0064_f812_fire_loan_settings` creates the table and existing plans default to every loan
  included, with no `needs_review` change.
- **Resolution.** `services.fire_loans` bulk-loads every schedule loan and its children once
  (no per-loan SQL), builds the actual schedule and a recurring schedule that keeps one-off
  overpayments dated on or before today but excludes future ones. The next recurring row's
  interest + principal + costs + regular overpayment ×12 is the annual payment. The actual
  schedule's summary gives the payoff date. The resolver returns included, ignored, archived,
  paid-off, not-drawn, currency-mismatch, schedule-error, no-payment, amount-invalid and
  installment-plan rows with totals and payoff years; it never writes. A tranche loan with no
  drawn balance and undrawn principal is **not drawn**, even when it has a future transfer. The
  first-payoff line and payoff badges use included loans only.
- **Projection.** `ProjectionInputs.loans` and `ProjectionRow.loan_payment` /
  `real_loan_payment` were added. `ProjectionRow.expenses` is the net line after included
  loans stop being paid; `loan_payment` is the part still attributable to included loans before
  payoff and ignored loans throughout. `net = income - expenses`. No-loan behaviour and the
  independent oracle are unchanged; property tests cover the loan drop.
- **UI.** The view has a read-only Loans panel with name, annual/monthly payment, payoff,
  included/ignored badge, effect, totals, payoff badges and excluded reasons. The editor has
  native Included/Ignored radio cards inside the plan form; **Save changes** writes them with
  the plan, **Discard changes** restores them, and preview uses posted toggles without writing.
  The wizard review lists active loans read-only. The table has a **Loan payments** column and
  a payoff badge; charts mark included payoffs. The expense suggestion card adds the live
  scheduled annual payment separately from the ledger-linked window portion. In edit mode the
  results summary is labelled as the live preview of the posted choices while the detailed
  cards below keep showing the saved choice until save; the understated-expenses warning lives
  in the results summary and uses the validated posted expenses during a preview.
- **Forged toggles.** Only a loan the live resolver currently marks included or ignored can be
  posted. A request naming an archived, paid-off, not-drawn, broken, currency-mismatched,
  amount-invalid or installment-plan loan is rejected without touching its saved setting, so an
  ignored loan keeps that choice through archiving and restore; a save whose form has no loan
  section changes no setting.
- **Warnings and approximations.** An included payment greater than the entered expenses shows
  a warning, but the drop is not capped and the plan is not blocked. The drop is fixed nominal
  and is not inflated; sensitivity scales the whole expense line including each loan's scaled
  annual payment, with payoff years fixed; a payoff inside a year removes the full annual
  payment from that year; a final balloon row is annualised as specified. These are documented
  in `docs/fire.md`. The shared loan view tolerates a missing loan start only for the FIRE
  resolver; the loan pages keep their existing strict error.
- **Archive 2.1.** `FORMAT_VERSION` is 2.1. `required_files`/`required_columns` keep 2.0 archives
  readable and require the new file only for 2.1. Export writes ignored rows; restore maps loan
  lineage ids, skips a missing loan with a warning, accepts `included=true` as a no-op and
  counts/audits only created settings. `EMPTINESS_CHECKS`, demo exclusions, tenant
  factories/coverage/isolation, the migration table set, model exports and the golden 2.1
  fixture cover the new table. Merge still reports `fire_restore_only`.
- **Tests.** Annual-payment/regular-overpayment, target-total, interest-only and future-one-off
  tests, payoff changes, ignore toggles, first-included-payoff, multiple loans,
  currency/paid-off/archived/not-drawn/broken/installment states, projection integration,
  sensitivity loan scaling, preview/save/no-write, forged ineligible toggles with a preserved
  saved ignore, query budgets, tenant/roles/CSRF, archive round-trip, 2.0 compatibility,
  duplicate/invalid/no-op settings, Polish i18n and Chromium coverage. All tests were run
  single-core.

### F8.13 — Nominal-only results and wording

**Acceptance**

- The projection, summary, charts, table, CSV and sensitivity expose nominal values only;
  the real/today's-money basis, toggle, fields and CSV columns are removed.
- FIRE number, progress and sustainability are defined in nominal terms and explained in
  plain language.
- "Sheet progress", "spreadsheet" and other internal-model wording are gone from the UI,
  help text and user documentation; **Plan progress** replaces them.
- Sensitivity remains available, shows FIRE age only, and uses nominal inputs.
- Tests: no real fields/strings, nominal CSV, metrics, chart titles, i18n, docs wording,
  browser coverage.

**Agent prompt**

```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F8.13 from docs/specs/M8-fire-planner-v2.md: remove the real/today's-money view,
make every result nominal, rename the progress metric and remove spreadsheet wording from
the UI and user docs.
```

### As built (M8.F8.13)

- **Domain.** `ProjectionRow` lost `real_income`, `real_expenses`, `real_loan_payment`,
  `real_net`, `real_gain`, `real_cash` and `real_net_worth`; `_deflate` was deleted.
  `Projection` lost `real_networth_at_fire`, `fire_number_today`, `progress_sheet_bp` and
  `progress_today_bp`, and gained `fire_number` and `plan_progress_bp`. `ProjectionInputs`
  keeps `real_return_bp` as the stored return-above-inflation assumption.
- **Metrics.** FIRE number is the FIRE row's nominal expenses divided by the rule's rate
  (the withdrawal rate, or `inflation + real return` under returns-cover-expenses), rounded
  once. It is `None` when FIRE is never reached or the rate is not positive; zero expenses
  with a positive rate give zero. Plan progress is the starting portfolio divided by the
  nominal net worth at FIRE, rounded once; it is `None` when FIRE is never reached or that
  net worth is not positive. Both use nominal values only. Sustainability still reports the
  nominal depletion age and lowest net worth after income stops. **Superseded by F8.19:** the
  returns-cover predicate and FIRE number now use the return above inflation, and the gain
  compounds exactly instead of adding the two rates.
- **Results.** The basis toggle, `basis` query parameter and all basis links/radios/hidden
  fields and redirect plumbing were removed; an old `?basis=real` request renders the nominal
  page, and legacy id redirects drop the parameter. Charts always use `net_worth` and the
  table always shows nominal columns. CSV format is `year, age, income, expenses,
  loan_payment, net, gain, cash, net_worth, is_fire, income_stopped, currency`. F8.12's
  `loan_payment` remains; `real_loan_payment` is gone. `Plan progress` replaces the two old
  progress cards.
- **Copy and sensitivity.** Visible labels say **Return above inflation** while the internal
  `real_return_bp` field and form name stay unchanged. **FIRE number** is shown with the FIRE
  year and a plain-language definition. The sensitivity grid still shows FIRE age only,
  uses the saved plan, and has no amounts; its visible “real return” wording was replaced.
  FIRE UI and `docs/fire.md` no longer use spreadsheet, sheet-progress or today's-money
  wording. The internal `sheet` CSS class was kept.
- **Compatibility.** No model, migration, archive-format, dependency or loan-math change.
  F8.10 view/edit, F8.11 adjustments, F8.12 live loans, roles, CSRF, query budgets and tenant
  isolation are unchanged. Existing archives remain 2.1-compatible.
- **Superseded by F8.15.** The results CSV delivered by F8.3 and made nominal here was removed in
  F8.15; the projection, results and archive did not change.
- **Tests and docs.** Unit tests cover the new row/metric shape, golden FIRE number/progress,
  undefined states, zero expenses, loan-driven metric changes, nominal charts and CSV, and
  property invariants. Web tests cover the removed controls/query/links, metric copy, Polish
  catalogs, sensitivity wording and the static template scan. Chromium covers view metrics,
  the editor and the wizard copy. `docs/fire.md`, this as-built section and ADR-0025's
  superseded note were updated. All tests ran single-core.

### F8.14 — FIRE setup and input polish

**Acceptance**

- Birth year is prefilled from the profile for new plans; the wizard shows a conditional
  profile-source state and an existing plan keeps its saved birth year.
- FIRE form help is limited to non-obvious assumptions, sits next to the label, and the
  “Required. Enter 0…” boilerplate is gone.
- The income suggestion shows the trailing-window category breakdown with an income caption;
  the expense caption stays correct.
- The suggestion boilerplate paragraph is removed from the wizard and edit page; amount,
  window, as-of, warnings and saved-versus-suggested remain.
- Tax copy says income and expenses are ledger amounts, income may be gross, expenses should
  include taxes actually paid, and taxes are not modelled separately. No tax field or
  calculation is added.
- The portfolio suggestion lists skipped non-investable classes and double-counted linked
  net-worth items by name, link and reason; no skipped amount is counted.
- The withdrawal-rate field is enabled only for the withdrawal-rate rule, hidden/disabled
  otherwise by CSP-safe JavaScript, with a working no-JavaScript fallback; the server ignores
  the rate for other rules.
- No model, migration, archive, loan-math or dependency change.
- Tests cover copy and absence, hint placement/accessibility, profile states, income
  categories, skipped sources, SWR toggle/no-JS, i18n, CSP, browser widths and query budgets.

**Agent prompt**

```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F8.14 from docs/specs/M8-fire-planner-v2.md: FIRE setup and input polish for birth-year
source copy, label-adjacent hints, income category suggestions, ledger-based tax copy and the
withdrawal-rate field toggle. Keep the models, migrations, archive, loan math and projection math
unchanged.
```

### As built (M8.F8.14)

- **Birth year.** New plans prefill from the profile. The wizard shows one supporting line with
  `data-fire-birth-year-source` (`profile`, `empty`, `changed`, `entered`, `missing`): the
  profile link appears when the field is empty, missing, or entered without a profile; an
  edited wizard year says the profile still has the saved value, and a year entered without a
  profile says the plan keeps its own value. The edit form explains that the plan keeps its saved birth year; changing the
  profile never rewrites an existing plan. The field is required with explicit `id`/`for` and
  ARIA wiring. No profile model, service or M6 risk change.
- **Hints.** The four non-obvious assumptions keep a focusable question-mark note beside the
  label — return above inflation, independence rule, withdrawal rate and annual expenses
  (taxes/loan treatment). Every other FIRE field hint and every “Required. Enter 0…” /
  “Required. Add it to your profile…” span is gone. Hinted inputs use
  `aria-describedby="<name>-hint"` (plus the error id on failure). The hint macro gained an
  optional id, `data-fire-hint` and a visible focus ring.
- **Suggestions.** The income card shows the trailing-window category breakdown with the
  **Income by category in the window** caption; expenses keep the explicit expense caption. Both
  tables carry `data-fire-suggestion-categories="income|expenses"`. The boilerplate “A suggestion
  never fills a field by itself…” paragraph is gone from the wizard and editor; amount, window,
  months with data, as-of date, warnings, blocked reason, Use button and saved-versus-suggested
  note remain.
- **Skipped sources.** `LinkedPortfolio` carries a `skipped` tuple of
  `SkippedPortfolioSourceRow` rows (name, source kind/id, reason, optional colour, no amount);
  the count properties derive from it. The portfolio card lists a collapsed **Skipped**
  subsection with the reason and a link to the asset class or net-worth item. The total and the
  warnings still count only the included sources; the rows are built from data already loaded,
  with no per-row queries.
- **Tax copy.** FIRE templates and `docs/fire.md` say income and expenses are ledger amounts,
  income may be gross, expenses should include the taxes actually paid, and the planner does
  not model taxes separately. No tax field, rate, calculation, model or migration was added.
- **Withdrawal rate.** Both forms render the field visible and enabled for the no-JavaScript
  fallback; `fire.js` hides and disables it unless the rule is **Withdrawal rate**, and the
  wizard now loads `fire.js`. The route only parses a posted `swr` when the rule is
  `withdrawal rate`, so a malformed value cannot fail a non-SWR save.
- **Tests and docs.** Unit tests cover skipped rows, the query budget and income categories;
  web tests cover profile states, hint placement/absence, both category tables, skipped sources,
  tax copy, no boilerplate, SWR markup/no-JS and the malformed-SWR regression; Chromium covers
  the wizard and editor toggles, scoped category locators, a JavaScript-disabled edit context,
  390/1440 widths and CSP. `docs/fire.md`, §1, §3 and §10 were updated. No model, migration,
  archive, dependency, loan-math or projection-math change. All tests ran single-core.

### F8.15 — FIRE results and editor cleanup

**Acceptance**

- The obsolete **FIRE plan** overview panel is gone from the view and edit pages. Its horizon,
  income-stopped and stop-after-horizon warnings move into a compact warning stack above the
  results or the form, and appear only when the condition is present.
- The year-by-year table has no **Loan payments** column and no column hint. The loan payoff
  badge, the **Loans** panel and the projection's loan-payment data stay.
- The **Download CSV** action is removed. `/fire/results.csv`, the legacy
  `/fire/{scenario_id}/results.csv` redirect, `web/fire_csv.py` and their tests are deleted. The
  archive CSV files are unaffected.
- The adjustments editor is a compact table with Year, Type, Description, Amount and an action
  column inside a horizontal scroll container, so a 390px page scrolls the table inside its panel.
  A visible **Remove** control wraps a visually hidden checkbox with the existing
  `event_remove_<index>` name: removal works without JavaScript, while JavaScript dims a saved
  row, changes the label to **Undo** and removes an unsaved new row from the DOM. **Save changes**
  applies saved-row removals; the server parser and `adjustments_present` marker are unchanged.
- **Plan progress** is a full-width panel directly below the four key metric cards, styled like the
  loan repayment bar: a large percentage, a **Starting portfolio** / **Net worth at FIRE** legend
  with formatted amounts, a 32px bar with a success fill and subtle track, a visual cap at 100%
  and the exact percentage in accessible text. A no-FIRE or non-positive-target state shows a
  muted bar and an explanation. `role="progressbar"` and the ARIA value attributes stay.
- The **Loan choices** included/ignored totals line above the results is gone. The
  understated-expenses warning and the full **Loans** section stay.
- Unusual rates are confirmed when the plan is saved or created, not with a checkbox. Normal forms
  contain no confirmation checkbox. Preview shows the unusual-rate warning only when
  `needs_extreme_confirmation` is true. **Save changes** with a changed unusual rate re-renders the
  editor with a **Confirm and save** panel and writes nothing before that click. **Create plan**
  with unusual rates re-renders step 4 with a **Confirm and create plan** panel. The confirmation
  is tied to the rates shown; changing them asks again, and the wizard review signature still
  refuses changed values.
- The withdrawal-rate field stays in both forms. Server markup is visible and enabled for the
  no-JavaScript fallback; JavaScript disables and dims it unless the rule uses it, without
  `hidden` or the `hidden` class. A visible note says it is used only with the withdrawal-rate
  rule. The server hardening from F8.14 is unchanged.
- The read-only **First projected year** field is removed from Plan inputs; the Currency field
  stays. The projection still starts in the current year, and the wizard review and table keep
  their first-year information.
- No model, migration, archive, dependency, projection-math or loan-math change.
- Tests cover the removed panels/column/action/field, CSV-route removal, the table editor and
  removal flow, the progress bar states, loan totals removal, save-time confirmation (including a
  changed rate after the panel), always-visible withdrawal-rate states, no-JavaScript fallback,
  390/1440 widths, keyboard/ARIA access, i18n and CSP.

**Agent prompt**

```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F8.15 from docs/specs/M8-fire-planner-v2.md: remove the obsolete FIRE overview, CSV
export, loan-payments table column and first-year field; rebuild adjustments as a slim table
with visible Remove controls; make Plan progress a large loan-style bar; move unusual-rate
confirmation to save/create time; and keep the withdrawal-rate field visible but inactive.
Keep models, migrations, archive, dependencies and projection/loan math unchanged.
```

### As built (M8.F8.15)

- **Overview and warnings.** `view.html` and `edit.html` no longer render `data-fire-overview`,
  the heading, the first-year/age/currency line or the adjustment counts. The horizon,
  income-stopped and stop-after-horizon messages render in `data-fire-warnings` above the results
  (view) or form (edit), only when present; the `needs_review` banner is unchanged.
- **Table and loans.** The year-by-year table dropped the **Loan payments** header, cell and
  footnote. `ProjectionRow.loan_payment`, the payoff badge, the **Loans** panel and chart markers
  are unchanged. The **Loan choices** totals line (`data-fire-loans-preview`) was removed; the
  understated-expenses warning still depends on the live loan section.
- **CSV removed.** The header action, `fire_results_csv`, the legacy `fire_scenario_csv` redirect,
  `src/assetboard/web/fire_csv.py` and `tests/unit/test_fire_csv.py` were deleted, along with the
  route tests and documentation links. `/fire/results.csv` and `/fire/{id}/results.csv` now return
  404. No module imports `fire_csv`. F8.15 supersedes the CSV export delivered by F8.3 and made
  nominal by F8.13; the workspace archive's `fire_plan.csv`/`fire_events.csv` are unrelated and
  unchanged.
- **Adjustments.** `_adjustments_edit.html` is a `sheet` table in an `overflow-x-auto` region with
  Year, Type, Description, Amount and Action columns. The trailing blank `new` row stays last;
  **Add an adjustment** clones the `<tr>` template through a `tbody` holder before it. Saved rows
  and JavaScript-added rows use a visible **Remove** label around a `peer sr-only` checkbox with
  the existing `event_remove_<index>` name. `fire.js` dims a saved row, changes the label and ARIA
  name to **Undo**, and removes an unsaved row (and its inline error row) from the DOM. Archived
  adjustments use the same visible control. The server parser, `adjustments_present`,
  apply-on-save semantics, inline row errors and archived state are unchanged. The empty-state
  hint tracks JavaScript-added and removed rows, removing an unsaved row announces that it was
  removed, and focus still moves to the new row's year field.
- **Plan progress.** `_metrics.html` renders `data-fire-progress="plan"` directly below the four
  metric cards. It shows the large percentage, `Starting portfolio` and `Net worth at FIRE`
  amounts from `projection.inputs.start_portfolio` and `projection.networth_at_fire`, and a
  32px `viewBox="0 0 100 32"` SVG. The fill is capped at 100% in its width while
  `aria-valuetext` keeps the exact percentage; `aria-valuenow` is the capped integer. No-FIRE and
  non-positive-target states render a muted track, `aria-valuetext="Not defined"`, a zero value
  and an explanation. The duplicate never-FIRE banner and the old “Bar capped at 100%, exact
  value …” sentence are gone; the undefined note carries the reason and the accessible value
  keeps the exact percentage for defined values. No arithmetic moved into JavaScript.
- **Unusual rates.** The checkbox and its error field are gone from `_form_body.html` and the
  wizard. Preview keeps `data-fire-extreme-warning` only when `needs_extreme_confirmation` is
  true. The edit save route strips a confirmation unless the posted rate digest matches the one
  the panel rendered, then re-renders `data-fire-extreme-confirm` with **Confirm and save** and
  writes only after that button posts the digest plus `extreme_rates_confirmed=1`. The wizard
  renders the same panel on step 4 with **Confirm and create plan**; the panel posts
  `extreme_rates_confirmed=1` with `go=create`, while the review signature still forces a fresh
  review (and a fresh confirmation) when values changed. The review's own unusual-rate warning
  is not repeated beside the confirmation panel. Copy now says “Confirm it to save the plan” /
  “Confirm it to create the plan” and keeps the unusual-range explanation.
- **Withdrawal rate.** Both forms keep the field with its `data-fire-swr-field` wrapper and add
  `data-fire-swr-note`. Server markup is visible and enabled. `fire.js` sets `input.disabled`,
  toggles `opacity-60` and `data-fire-swr-inactive`, and never sets `hidden` or the `hidden`
  class. The server still reads `swr` only for the withdrawal-rate rule.
- **First year.** The read-only **First projected year** input was removed from `_form_body.html`;
  **Currency** remains. The wizard review, the table and the projection's current-year start are
  unchanged.
- **Tests and docs.** Web tests cover the removed overview/CSV/loan preview/column/first-year
  field, all three compact warnings and their absence, the slim table and hidden checkbox, the
  save-time confirmation (including a changed rate after the panel and a forged or missing
  digest), preview warning scope, the defined/capped/undefined progress states with both legend
  amounts, the no-nag case for an unchanged unusual saved rate, SWR notes and Polish copy.
  Chromium covers add/remove/undo/remove-unsaved and the trailing blank row, keyboard Space on
  the hidden checkbox, saved-row apply-on-save, no-JavaScript removal, the 390px in-panel table
  scroll, the always-visible disabled/enabled SWR field, the no-JavaScript edit fallback, CSP and
  1440 widths. `docs/fire.md`, this section, the overview range and the F8.13 superseded note
  were updated. No model, migration, archive, dependency, projection-math or loan-math change.
  All tests ran single-core.

### F8.16 — Living-plan year review

**Acceptance**

- The plan keeps the current-year projection anchor; it does not store a start year.
- The plan's last-saved year (`updated_at` in the workspace timezone) is shown as the year the
  starting values were last reviewed.
- When that year is before the current calendar year, the view and edit pages show a review
  banner naming both years. A viewer sees the banner without an edit link; an editor/owner sees
  a link to the plan inputs.
- Saving the plan in the new year clears the banner. Amounts are not updated automatically.
- No schema, migration, archive, dependency or projection-math change.

**Agent prompt**

```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F8.16 from docs/specs/M8-fire-planner-v2.md: use the plan's last-saved workspace-local
year for a living-plan review banner; keep the current-year anchor; do not auto-change values.
```

### As built (M8.F8.16)

- `FirePlanView.values_year` is computed from `updated_at` in the workspace timezone; no column
  or migration was added.
- `view.html` and `edit.html` render `data-fire-year-review` only when `values_year` is before
  `current_year`. The copy names both years; the view links editors/owners to the plan inputs,
  while viewers see the explanation without the link. The edit banner marks
  `data-fire-review-section="amounts"` so the F8.17 accordion can open the Starting amounts
  section.
- Saving the plan updates `updated_at` explicitly, so even an unchanged form counts as a save and
  clears the banner in the new year. The stored amounts are still reused until the user changes
  them.
- Tests freeze the clock in 2026, create a plan, advance it to 2027, assert the banner on view
  and edit, save the unchanged values and assert it is gone; a current-year plan has no banner.
  Workspace-timezone year conversion (the same instant is 2026 in UTC and 2027 at UTC+14) and
  the viewer/editor links are covered. No schema, migration, archive or dependency change.

### F8.17 — FIRE projection and editor layout

**Acceptance**

- The view puts **Plan values**, **Adjustments** and **Loans** inside one collapsed
  `<details id="plan-inputs">` between the metrics/progress block and the year-by-year table.
  The label is **Plan values, adjustments and loans**. The details is native (click, Enter or
  Space), needs no JavaScript, and the view still loads no `fire.js`.
- The editor order is: metrics/progress, the five-section exclusive accordion (**Plan**,
  **Assumptions**, **Starting amounts**, **Adjustments**, **Loans**), then the table/charts,
  disclaimer and sensitivity. All sections are collapsed by default and only one is open.
  Native `<details name="fire-inputs">` is used with a JavaScript fallback for older browsers.
- A save error opens the section that owns the field; an adjustment-row error opens
  **Adjustments**, unusual-rate confirmation opens **Assumptions**, and the F8.16 year review
  opens **Starting amounts**. An error section wins over the year review. Preview errors open
  the section with the invalid control without moving focus. `/fire/edit#amounts` opens that
  section.
- Live preview updates the metrics/progress and the table/charts in one request through two
  fragments. The form is never inside a swapped fragment. The preview response always emits an
  OOB `#fire-results-detail` wrapper so a blocked or no-projection preview clears a stale table.
  Preview errors, warnings, blocked, loan-understated and no-projection states stay visible.
- The sensitivity grid is always rendered on the view and edit pages from the **saved** plan.
  There is no **Show sensitivity grid** button and no `?sensitivity=1` behaviour. The editor has
  a **Refresh grid** control, hidden without JavaScript, which still fetches the fragment route
  and never uses unsaved values or posted loan choices.
- The year-by-year section has no outer panel/padding; the table's own `.sheet` border remains.
  Adjustment Year and Amount inputs fill their cells (`w-full`), with sensible header widths.
- No schema, migration, archive, dependency, projection-math or loan-math change. The `pl`
  catalog is complete with no fuzzy entries.
- Tests cover the view structure and order, the collapsed details, the five-section accordion and
  its open rules, split preview/OOB behaviour (including clearing a blocked detail), the
  always-on sensitivity grid and saved-plan semantics, the removed load/query path, the table
  frame and input widths, CSP, no-JavaScript submission, keyboard disclosure, 390/1440 widths and
  Polish copy.

**Agent prompt**

```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F8.17 from docs/specs/M8-fire-planner-v2.md: collapse the view's plan values,
adjustments and loans into one details; rebuild the editor as a five-section exclusive accordion
with server-side open rules; split the live preview into metrics and table/charts fragments with
an always-emitted OOB wrapper; make the sensitivity grid always render from the saved plan with
an editor-only Refresh grid; and remove the year-by-year panel frame. Keep models, migrations,
archive, dependencies, projection math and loan math unchanged.
```

### As built (M8.F8.17)

- **View.** `view.html` composes notices, `#fire-metrics`, one collapsed
  `_plan_inputs_view.html` (`details#plan-inputs`, `data-fire-plan-inputs`, label “Plan values,
  adjustments and loans”), `#fire-results-detail`, the disclaimer and the sensitivity panel.
  `_plan_values.html`, `_adjustments_view.html` and the embedded `_loans.html` render without
  nested panels; the loans heading stays in the view and is hidden in the editor. The summary
  wraps at 390px and the disclosure is native. `data-fire-results` stays on the view root.
- **Editor.** `edit.html` puts `#fire-metrics` inside the preview wrapper, then the form with the
  heading, `_form_body.html` (five `<details name="fire-inputs">` sections, `space-y-3`,
  `panel group`, `group-open` marker) and the save/discard actions, then
  `#fire-results-detail`. All sections are collapsed by default; `open_section` is computed in
  `fire_routes._editor_section` with precedence error field/row → unusual-rate confirmation →
  F8.16 year review. `fire.js` (`setupExclusiveAccordion`, `openSectionFor`,
  `openSectionForHash`, `syncPreviewError`) supplies the exclusivity fallback, hash opening and
  preview-error opening. Closed controls stay in the DOM and submit normally.
- **Preview fragments.** `_results_metrics.html` holds the preview error, preview note, extreme
  warning, block panel, loan-expense warning and `_metrics.html`; `_results_detail.html` holds
  the table and charts under `{% if projection %}`; `_preview.html` emits the OOB
  `#fire-results-detail` wrapper first and the metrics content second. `fire_edit_preview`
  renders `_preview.html` for success and `FireError`. The preview request is unchanged
  (`hx-include="#fire-form"`, `hx-target="#fire-metrics"`, `hx-swap="innerHTML"`,
  `hx-sync="this:replace"`), so the form is never swapped.
- **Sensitivity.** `_view_context` and `_edit_context` always call `_sensitivity_context`.
  `_render_edit` keeps `saved_results` for the grid while a posted loan toggle may recompute
  `results` for the editor's metrics and table. `sensitivity_page_url` and the load branch are gone;
  `sensitivity_refresh` is true only in the editor. `fire.js` reveals the hidden **Refresh grid**
  button. The fragment route renders `_sensitivity.html` only. The refresh button requests
  `?in_editor=1`, so a swapped blocked-state link stays at `#amounts` instead of reloading the
  editor; a plain fragment request still links to `/fire/edit#amounts`. `fire_scenario_sensitivity`
  redirects to `/fire#sensitivity`; `fire_scenario_page` forwards only `notice`/`error`; the
  `sensitivity` query parameter is ignored.
- **Styling.** `_table.html` dropped `panel` and its padding; the table keeps `sheet`,
  `overflow-x-auto`, heading and row styling. `_adjustments_edit.html` uses `w-full` Year/Amount
  inputs and `w-24`/`w-36` header cells.
- **Tests and docs.** Web tests cover the view order and collapsed details, the editor accordion
  and all open rules (including the year review and error precedence), the split preview/OOB
  clearing, the absent sensitivity load path, once-per-page grid computation and saved-plan
  semantics. Browser tests cover keyboard disclosure, exclusivity, preview error reopening, hash
  opening, year-review opening, edit/view sensitivity refresh visibility, no-JavaScript save of
  closed sections, 390/1440 widths and CSP. `docs/fire.md`, this section and the overview range
  were updated. No model, migration, archive, dependency, projection-math or loan-math change.

### F8.18 — FIRE display and input polish

**Acceptance**

- The year-by-year table has no explanatory paragraphs below it. It uses the same `bg-surface`
  sheet treatment as the Transactions tables and keeps the FIRE, depleted and income-stopped row
  colours.
- The view gives the year-by-year results the same spacing below the **Plan values, adjustments
  and loans** details as the editor.
- The editor's **Plan** section has no visible currency field and no visible birth-year field or
  source copy. A hidden `birth_year` posts the saved value, so saving keeps working. The setup
  wizard keeps its visible birth-year step.
- The editor and the wizard put **Independence rule** and **Withdrawal rate** side by side in
  their own two-column row, below the inflation/return/income-growth row. The “used only when the
  independence rule is Withdrawal rate” note is gone. The withdrawal-rate field stays visible and
  is JavaScript-disabled for the other rule.
- The disclaimer is the last block on both the view and edit pages, after the sensitivity panel.
- In the income and expense suggestion cards, the category table is behind a **Where this value
  comes from** disclosure, matching the portfolio suggestion. The loan-portion and scheduled-loan
  lines stay visible outside the disclosure; the category table and its row hooks stay intact.
- The starting-portfolio input fills its full-width column and matches the width of the
  full-width **Suggested starting portfolio** card.
- No schema, migration, archive, dependency, projection-math or loan-math change. The `pl`
  catalog is complete with no fuzzy entries.
- Tests cover the removed copy, the hidden birth year and a successful save, the absent visible
  currency/birth-year fields, the rule/SWR row and absent note, the collapsed category details
  opening in a browser, the disclaimer order, the table background and row tints, the view
  spacing and the portfolio input width.

**Agent prompt**

```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F8.18 from docs/specs/M8-fire-planner-v2.md: remove the two year-by-year notes, give
the table the Transactions surface treatment and the view the editor spacing, hide the editor's
currency and birth-year fields while keeping the hidden birth-year value, move rule/SWR into
their own row without the note, move the disclaimer below sensitivity, put suggestion categories
behind a details, and uncap the starting-portfolio input. Keep models, migrations, archive,
dependencies, projection math and loan math unchanged.
```

### As built (M8.F8.18)

- `_table.html` renders the heading, the scroll region and the table with no explanatory
  paragraphs below it; the first-year and row highlight paragraphs are gone. (F8.19 later adds a
  short first-year hint next to the heading.) The table is `sheet bg-surface`, and the
  `bg-success-soft`, `bg-danger-soft` and `text-muted` row classes and their data hooks are
  unchanged.
- `view.html` puts `mt-8` on `#fire-results-detail`, matching the editor, and includes the
  disclaimer after `_sensitivity_panel.html`; `edit.html` does the same. The disclaimer is the
  last child of the view/editor page wrapper.
- The editor's Plan section posts `<input type="hidden" name="birth_year">` with the saved value
  and no longer renders the readonly currency display, a birth-year input or its source text. The
  wizard step 1 is unchanged.
- Both `_form_body.html` and `wizard.html` split their assumption fields into a three-column
  rates row and a two-column rule/SWR row (`data-fire-swr-row`). `data-fire-swr-note` and its
  copy are removed; the existing `fire.js` visibility/disable behaviour is unchanged.
- `_suggestion.html` renders the category table inside a native `<details>` whose summary reuses
  the existing **Where this value comes from** msgid. `data-fire-suggestion-categories`,
  `data-fire-suggestion-category` and the caption stay inside; the loan-portion and
  scheduled-loan paragraphs stay outside and visible.
- The starting-portfolio input no longer carries the `lg:max-w-[calc(50%-0.625rem)]` cap, so it
  fills its `lg:col-span-2` column like the portfolio suggestion card.
- Web tests cover the absent copy, hidden birth year and save, hidden currency/birth year, the
  rule/SWR row, the category disclosure and loan lines, the table background and the FIRE,
  depleted and income-stopped row tints, view spacing and disclaimer order, and the uncapped
  portfolio field. Browser tests cover the disclaimer as the last block, `mt-8` spacing, the
  table class, hidden birth year, category collapse/open in the editor and wizard, the
  side-by-side rule/SWR layout, absent note, the portfolio input/card width match, CSP and
  390/1440 widths. `docs/fire.md` and the overview range were updated. No model, migration,
  archive, dependency, projection-math or loan-math change.

### F8.19 — FIRE real-return coverage and model corrections

**Acceptance**

- The nominal gain compounds inflation and the return above inflation exactly:
  `(1 + inflation) * (1 + real return) - 1`, with integer basis-point arithmetic and one
  half-up rounding per row. `ProjectionRow.gain` remains the nominal investment gain.
- **Returns cover expenses** compares the opening net worth's return above inflation with the
  year's nominal expenses (`opening * real_return_bp > expenses * 10000`, strict). The FIRE
  number under that rule is `fire_row.expenses * 10000 / real_return_bp`, rounded once, and is
  undefined when FIRE is never reached or the real return is not positive. The withdrawal-rate
  rule is unchanged. The visible rule label stays **Returns cover expenses**; its hint and the
  FIRE-number hint explain the real-return meaning.
- A negative effective expense line (after included-loan drops and expense-change events)
  raises `FireProjectionError` with code `expenses_negative` and the row's year. The results
  block, preview and save show the year and a clear message. A plan with zero portfolio and
  negative expenses never declares FIRE. Both a negative `expense_change` and an included loan
  drop larger than expenses are covered.
- Sensitivity scales the whole expense line: starting expenses, every `expense_change` event
  and every loan's recurring payment, with one rounding per component. Income, cash and saving
  overrides stay unchanged. The factor-10,000 base cell equals the direct projection. A scaled
  cell whose expenses would be negative fails the grid with `SensitivityError` and a visible
  year-aware message, never a false FIRE cell or a server error.
- The current-year first row is unchanged and keeps a short hint next to the **Year by year**
  heading: the first row is the starting point, today's portfolio earns one year's gain, and
  the current-year income/expenses are shown while the row's net saving is the starting
  portfolio. `docs/fire.md` and this as-built section carry the same explanation.
- Every new string is translatable; the Polish catalog is complete with no fuzzy entries. No
  schema, migration, archive, dependency, loan-math or UI-layout change. Tests cover the exact
  compounding, real-return predicate and FIRE number, negative-expense errors in the domain,
  preview, save, results block and sensitivity grid, whole-expense-line sensitivity scaling,
  the first-year hint, i18n and the existing browser suites.

**Agent prompt**

```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F8.19 from docs/specs/M8-fire-planner-v2.md: exact nominal compounding, returns-cover
using the real return, negative-expense projection errors with their year, whole-expense-line
sensitivity scaling, and the current-year explanation. Keep models, migrations, archive,
dependencies, loan math and the UI layout unchanged.
```

### As built (M8.F8.19)

- **Compounding.** `project` computes the nominal gain numerator
  `inflation_bp*10_000 + real_return_bp*10_000 + inflation_bp*real_return_bp` and applies
  `round_ratio(opening * numerator, 100_000_000)` once per row. The additive gain rate is gone
  from the engine, docstrings, test oracle, property tests and golden values.
- **Returns cover.** The predicate is `opening * real_return_bp > expenses * 10_000` (the
  non-negative opening guard remains). The FIRE number uses `real_return_bp` under this rule
  and is `None` when the real return is not positive. The SWR predicate and FIRE number are
  unchanged. Visible copy says **Returns cover expenses**, explains that the return above
  inflation must cover the year's expenses and preserve real capital, and the FIRE-number hint
  and undefined note use **return above inflation** instead of the nominal gain rate.
- **Negative expenses.** `FireProjectionError` and `SensitivityError` gained an optional
  `year`; `ResultsBlock` carries it too. A negative effective line raises `expenses_negative`
  with field `events` when the gross line is negative and `loans` when the included-loan drop
  causes it. The results and sensitivity panels show the translated message and `(Year YYYY)`;
  preview and save show the same translated error, and save runs the preview projection before
  `update_fire_plan` so an unprojectable plan is not written. A scaled sensitivity cell with
  negative expenses raises `SensitivityError("expense_factors_bp", "expenses_negative",
  year=...)` and renders the visible grid error.
- **Sensitivity.** The expense factor now scales the starting expenses, every
  `expense_change` event and every loan payment, each rounded once; cash, income changes and
  saving overrides stay saved. The factor-10,000 cell reuses the direct projection, so it is
  identical by construction. Sensitivity tests cover the scaling, the base-cell equality and
  the negative-cell error.
- **Current year.** `_table.html` keeps the first-row model and adds a short
  `data-fire-first-year-hint` next to **Year by year**: the first row is the starting point,
  today's portfolio earns one year's gain, the current year's income and expenses are shown, and
  that row's net saving is the starting portfolio. The old trailing footnotes stay removed
  (F8.18). `docs/fire.md` carries the same explanation.
- **Tests and docs.** Unit tests cover the exact compounding, the real-return predicate and
  FIRE number, strict boundaries, negative-expense errors from both causes, the zero-portfolio
  case, exact sensitivity scaling of all expense components, the base cell and the negative
  scaled cell. Web tests cover the translated block/preview/save/sensitivity errors with the
  year, both negative-expense causes, the first-year hint, the updated golden metrics and
  Polish rendering. Browser coverage uses the existing FIRE wizard, edit, adjustments and loan
  modules. `docs/fire.md`, this section, §5.2/§5.3, the overview range and ADR-0025's amended
  note were updated. No model, migration, archive, dependency, loan-math or UI-layout change.

### F8.20 — FIRE wizard and risk table polish

**Acceptance**

- Wizard step 1 puts **Birth year** on its own full-width row. **Horizon age** and **Income
  stops at age** share the next row and their inputs align.
- Edit mode shows the illustrative 4% in the inactive **Withdrawal rate** field when the plan
  stores no rate (for example under **Returns cover expenses**). The field stays visible and is
  JavaScript-disabled for the other rule. Saving still stores no rate, and the server still
  ignores a posted rate for that rule.
- The Portfolio **Holdings**, **By asset class** and **Risk balance** tables use
  `sheet bg-surface`, matching the Transactions history table.
- No schema, migration, archive, dependency or projection/loan-math change. The `pl` catalog is
  complete with no fuzzy entries.
- Tests cover the wizard row structure, the edit-mode 4% display and no-store save, the three
  portfolio table classes, and the aligned wizard layout in a browser.

**Agent prompt**

```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F8.20 from docs/specs/M8-fire-planner-v2.md: put Birth year on its own full-width
wizard row with the two ages paired below, show the illustrative 4% in the inactive edit-mode
withdrawal-rate field when no rate is stored, and give the Portfolio Holdings, By asset class and
Risk balance tables the Transactions surface background. Keep models, migrations, archive,
dependencies, projection math and loan math unchanged.
```

### As built (M8.F8.20)

- `wizard.html` step 1 uses a single-column outer grid for **Birth year** and a nested
  two-column `sm:grid-cols-2` row for **Horizon age** and **Income stops at age**, so the
  profile-source copy under birth year no longer offsets the age fields.
- `_detail_values` falls back to `ILLUSTRATIVE_SWR_BP` (400 bp) when the saved plan has no
  withdrawal rate, so edit mode shows `4` in the inactive field; saving under **Returns cover
  expenses** still leaves `swr_bp` unset and the posted rate ignored.
- `_portfolio_body.html` adds `bg-surface` to the Holdings, By asset class and Risk balance
  tables; no markup, hooks or row styling changed otherwise.
- Web tests cover the wizard row structure, the 4% edit display and the no-store save, and the
  three table classes. The browser wizard module checks the birth-year row is full width and the
  two ages share a row; the browser edit module checks the displayed 4%. `docs/fire.md`,
  `docs/investments.md` and the overview range were updated. No model, migration, archive,
  dependency or projection/loan-math change.

### F8.21 — Optional horizon-based independence rule

**Acceptance**

- Add `lasts_to_horizon` as a third `Rule`. It is stored in the existing `fire_scenarios.rule`
  column with `swr_bp = NULL`; no new column or table. The database `rule_valid`/`swr_valid`
  check constraints and a reversible migration accept it.
- The rule finds the earliest candidate age from the current age through the earlier of
  **Income stops at age** and **horizon age − 1** where retiring survives. The candidate
  simulation stops **base income at that candidate age**, not at the configured income-stop
  age, and keeps every other series and event as entered: income/expense changes, saving
  overrides, cash events, inflation/return/income growth and included/ignored loans with their
  payoff drops. The candidate's opening balance and every later net worth through the horizon
  must be non-negative; zero at the horizon counts as lasting.
- The FIRE row is the candidate row in the normal projection. The displayed rows still use the
  configured income-stop age. The rule cannot choose an age later than the configured
  income-stop age or the horizon. If base income has already stopped, today is the only
  candidate.
- The rule has no rate-based FIRE number and no net-worth-target progress. Both metrics show
  as not defined with rule-specific explanations; net worth at FIRE remains visible.
- **Returns cover expenses** remains the default for new plans. Existing plans keep their saved
  rule unless the user selects another; selecting the horizon rule and saving stores
  `swr_bp = NULL` and the server ignores a posted withdrawal rate for it.
- Archive format **2.2** accepts the new rule value and keeps 2.0/2.1 readable. The `pl`
  catalog is complete with no fuzzy entries.
- No change to loan math, money/rounding rules, the other independence rules or dependencies.
- Tests cover the earliest viable age, zero at the horizon, the stop-age cap, the candidate
  income-stop assumption (not the full plan path), never-fire, undefined FIRE
  number/progress, validation, the golden/oracle path, sensitivity, save/persistence, archive
  round-trip, migration constraints and a browser selection with CSP and 390/1440 widths.

**Agent prompt**

```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F8.21 from docs/specs/M8-fire-planner-v2.md: add the optional Portfolio lasts to the
horizon independence rule, simulate stopping base income at each candidate FIRE age up to the
configured income-stop age, keep the rule off by default, explain why the FIRE number and plan
progress are not defined for it, and update the DB constraints, archive 2.2, docs, i18n and
tests. Keep loan math, money/rounding rules, the other rules and dependencies unchanged.
```

### As built (M8.F8.21)

- `Rule.LASTS_TO_HORIZON` is `lasts_to_horizon`; `project` builds the normal rows once and then
  runs a candidate simulation for the rule. `_horizon_fire_index` caps candidates at
  `max(current age, min(stop_working_age, horizon_age - 1))`, rebuilds the row series with the
  candidate stop age, requires a non-negative opening balance at the candidate and non-negative
  net worth through the horizon, and returns the first surviving candidate. The chosen row is
  marked `is_fire`; when base income already stopped, the current age is the candidate.
- The candidate simulation keeps events, loans, rates and the configured amounts. The displayed
  table, depletion window and sustainability check still use the configured income-stop age.
- `fire_number` and `plan_progress_bp` are `None` for the rule; `networth_at_fire` stays. The
  metrics cards use rule-specific help and not-defined notes in `_metrics.html`.
- The wizard and editor list the rule after the two existing choices; **Returns cover expenses**
  stays first and selected by default. The existing JavaScript disables the withdrawal-rate field
  for any non-SWR rule, and the route parses no SWR for it.
- Migration `0065_f821_horizon_rule` drops and recreates `ck_fire_scenarios_rule_valid` and
  `ck_fire_scenarios_swr_valid`; downgrade converts horizon rows to `returns_cover_expenses`
  before restoring the old checks. `FORMAT_VERSION` is `2.2`; the archive reader accepts the new
  value and still reads 2.0/2.1 archives.
- Unit tests cover both golden/oracle paths, the exact candidate boundary, zero at the horizon,
  the stop-age cap, the counterfactual path and validation; the property suite samples the new
  rule; sensitivity reuses the base projection. Web tests cover the selector default, saving
  without an SWR and the metrics notes; the archive round-trip is covered in the FIRE archive
  tests. The browser wizard checks the selection and the inactive rate field. `docs/fire.md`,
  `docs/archive-format.md`, the overview range and ADR-0027 were updated.

---

## 12. Cross-cutting requirements

- **AGENTS.md applies:** no float for money, rates in basis points, Clock injection, scoped
  repositories, service-layer roles, CSRF on writes, escaped output, CSP-safe assets, all
  strings translatable, Polish catalog complete.
- **No new runtime dependency** and no ADR for a stack change; the rework deliberately keeps
  the Python projection and server-side rendering.
- **Query budgets:** one plan loads its events and live loan inputs in bounded queries; no
  N+1 per loan or per event year.
- **Tenant isolation:** the one-plan table and events remain workspace-scoped and covered by
  the tenant suite; cross-workspace IDs are 404, not empty.
- **Empty states:** no plan, no events, no loans, no suggestions and blocked suggestions all
  have explicit copy and never show a fake zero.
- **Archive:** format 2.2 as in §9, with 2.0/2.1 and 1.8 compatibility and golden fixtures.
- **Docs:** every feature updates `docs/fire.md`, the spec's as-built section, and the
  archive docs where relevant. User docs never mention the spreadsheet.
- **Accessibility:** wizard steps, view/edit controls, archived-event sections, loan
  include/ignore toggles and charts are keyboard usable and screen-reader labelled.
- **Mobile:** the wizard, schedule, adjustment rows and loan cards remain usable without
  horizontal overflow.

---

## 13. Delivery order

1. F8.8 — one-plan model, migration, current-year anchor, archive 2.0.
2. F8.9 — one-plan UX and setup wizard.
3. F8.10 — view/edit modes and suggested inputs.
4. F8.11 — unified adjustments.
5. F8.12 — live loans.
6. F8.13 — nominal-only results and wording.
7. F8.14 — FIRE setup and input polish.
8. F8.15 — FIRE results and editor cleanup.
9. F8.16 — living-plan year review.
10. F8.17 — FIRE projection and editor layout.
11. F8.18 — FIRE display and input polish.
12. F8.19 — FIRE real-return coverage and model corrections.
13. F8.20 — FIRE wizard and risk table polish.
14. F8.21 — optional horizon-based independence rule.

No feature starts until the owner approves this spec and the spec PR is merged. After that,
each feature is implemented on its own branch and merged only after its own green gate and
review.
