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
- Automatic tax modelling (see §10; a follow-up decision).
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
- Deleting/resetting the plan hard-deletes the row and its events after typed confirmation;
  an audit event is recorded. A workspace with no plan opens the wizard.

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
gain       = opening net worth * (inflation + real return), rounded once
net worth  = opening + net + gain + cash
```

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
- **Years to FIRE:** `fire_age - age_at_start`; the current year is year 0.
- **FIRE number (at FIRE):** the nominal annual expenses at the FIRE row divided by the
  selected rule's rate (SWR, or the nominal gain rate for returns-cover-expenses). Shown with
  its year and a plain-language definition; `—` when FIRE is never reached or the rate is
  not positive. This replaces the v1 "today's money" FIRE number.
- **Plan progress:** starting portfolio divided by net worth at FIRE, shown as a percentage
  with the definition; `—` when FIRE is never reached. **Today's-money progress** is removed.
- **Sustainability:** depletion age and lowest net worth after income stops; nominal.
- **No real/today's-money fields or columns.** CSV, charts, sensitivity and comparison-free
  pages export/show nominal values only.

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

Open detail for implementation: the exact definition of "annual payment" when the next
12 months contain a one-off overpayment or the payoff falls inside the year. The spec's
intent is: use the recurring monthly outflow (including regular overpayment) × 12, not the
sum of the remaining rows and never a saved snapshot.

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

## 9. Archive format (planned 2.0)

The archive extension is updated in the same feature that reshapes the model, so the archive
never describes a model that no longer exists.

- Format version **2.0** (a breaking layout change from 1.8; import support for ≤1.8 remains).
- Files: `fire_plan.csv` (one row, header-only when no plan) and `fire_events.csv`.
  No `fire_contributions.csv`, `fire_expense_exclusions.csv` or `fire_scenarios.csv`.
- The plan row contains birth year, stop/horizon ages, rates, rule/SWR, manual amounts,
  currency and `needs_review`. Suggested values, resolved loan data and projection results
  are derived and not archived.
- Events export kind, year, amount and label. Past events are included as archived history;
  they are not applied on restore unless still in the future.
- 1.8 archives import through the migration mapping in §8.2; multiple FIRE scenarios follow
  the default/first rule and produce warnings.
- `docs/archive-format.md` and its golden fixtures are updated in the same PR; old fixtures
  remain readable.

---

## 10. Taxes (open decision)

The planner currently uses cash-flow amounts and does not model taxes separately. The owner
has raised two different effects:

1. income taxes disappear when income stops;
2. investment-gain taxes appear when assets are sold.

**Recommended v2 default:** treat the plan as **after-tax cash flows**. The manual
income is after tax; expenses include taxes actually paid; suggestions come from the ledger,
which is already after tax. The help text states this. Income-tax disappearance and
capital-gains tax are not modelled automatically in v2.

**Follow-up options (not in this rework unless the owner chooses one):**

- an optional annual income-tax amount that stops with income;
- an optional effective income-tax rate applied to gross income;
- a capital-gains tax rate applied to withdrawals or gains, which needs a withdrawal model;
- reusing the M3 tax rules to derive an effective rate.

This is the one product decision still open. It does not block the model/UI features, but it
must be settled before the results/copy feature claims to be tax-aware.

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
  `fire_legacy_scenarios_skipped`. The reader upgrades 1.8 archives in memory (default/first,
  fallbacks, contribution conversion, loan-payoff removal, skipped-scenario warnings). A 2.0
  restore preserves `needs_review`.
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
- **Archive:** format 2.0 as in §9, with 1.8 compatibility and golden fixtures.
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

No feature starts until the owner approves this spec and the spec PR is merged. After that,
each feature is implemented on its own branch and merged only after its own green gate and
review.
