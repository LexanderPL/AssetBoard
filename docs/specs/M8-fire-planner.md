# M8 — FIRE planner

**Goal:** a year-by-year financial-independence projection that mirrors your spreadsheet's model, then improves it: named scenarios, inputs linked to real data, one-off events, sustainability check, comparison and sensitivity.

**Exit criteria:** the projection engine reproduces the spreadsheet's results for the same inputs (golden test); scenarios can pull starting portfolio, expenses and income from the app's own data; two or three scenarios can be compared; the dashboard (M9) can show FIRE progress.

Every agent prompt starts with: *"Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go)."*

**Model of the source spreadsheet (to be mirrored exactly in "parity" behaviour)**
Inputs: inflation `i`; annual investment return above inflation `r`; annual income growth `g`; "expected FIRE age" = the age at which **income stops**; birth year (embedded in formulas); starting income; starting expenses (today's money); starting portfolio (investable assets); explicit yearly net-savings values for the first years.

Rows per year `t` (`t = 0` is the current year):
```
age_t      = year_t - birth_year
income_t   = start_income                              (t = 0)
           = income_{t-1} * (1 + g)  if age_t < stop_age else 0   (t ≥ 1)
expenses_t = start_expenses                            (t = 0)
           = expenses_{t-1} * (1 + i)                  (t ≥ 1)
net_t      = start_portfolio                           (t = 0, column reused as the start value)
           = override_t if present else income_t - expenses_t     (t ≥ 1)
gain_t     = (t = 0: net_0 ; t ≥ 1: networth_{t-1}) * (i + r)
networth_t = net_0 + gain_0                            (t = 0)
           = networth_{t-1} + net_t + gain_t           (t ≥ 1)
```
Computed outputs: **FIRE age/year** = first row where `expenses_t < gain_t` (returns cover inflated expenses); **% achieved (sheet definition)** = `networth_0 / networth_at_FIRE_row`.

**Data model (sketch)**
- `FireScenario(id, workspace_id, name, birth_year, start_year, inflation_bp, real_return_bp, income_growth_bp, stop_working_age, horizon_age (default: the profile's life_expectancy, 90 if unset — owner decision 2026-10), rule[returns_cover_expenses|swr], swr_bp?, income_mode[manual|trailing_12m], income_manual?, expenses_mode[manual|trailing_12m], expenses_manual?, portfolio_mode[manual|linked_investable], portfolio_manual?, is_default, archived_at)`
- `FireContribution(scenario_id, year, net_amount Money)` — explicit yearly net-savings overrides.
- `FireEvent(scenario_id, year, kind[cash|expense_change|income_change], amount Money, label)` — `cash`: one-off added to net worth in that year; `expense_change` / `income_change`: annual change from that year onward, entered in today's money and inflated/grown like the base series.

`start_year` is fixed when the scenario is created (the workspace's current year) and is not
editable, so a saved plan stays reproducible; `age_at_start = start_year - birth_year` is the
"current age" used by validation. Contribution and event amounts are signed minor units (an
explicit exception to overview §3.2): positive saves/adds/increases, negative
dissaves/withdraws/reduces.

---

## F8.1 Scenario model and settings

**Acceptance**
- Scenario CRUD with all fields above; duplicate scenario; mark one as default.
- Rates are entered as percentages and stored as basis points. Birth year is prefilled from the user profile (optional profile field `birth_year`, added in F6.11); `life_expectancy` is also a profile field from F6.11 and defaults to 90.
- Contribution overrides editor (table: year → amount, add a row or paste a column) and events editor.
- Validation: horizon_age > current age; stop_working_age ≥ current age; rates within sane ranges (confirm outside).
- Plain-language tooltips explain each input; a notice states that projections are illustrative, not advice.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F8.1 from docs/specs/M8-fire-planner.md: FireScenario, FireContribution, FireEvent models + migration, optional profile birth_year,
CRUD/duplicate/default UI with contribution and event editors, validation and tooltips (translatable). No projection logic yet.
```

### As built (M8.F8.1)

- `0059_f81_fire_scenarios` creates `fire_scenarios`, `fire_contributions` and `fire_events`.
  The scenario carries a `currency` snapshotted from the workspace base currency and a
  `start_year` fixed from the injected `Clock` at creation. Active names are unique ignoring
  case through a partial unique index on `(workspace_id, name_key) WHERE archived_at IS NULL`;
  archived names may be reused. A second partial unique index keeps at most one active default.
  Checks cover the year range, `birth_year <= start_year`, the horizon/stop ages against
  `start_year - birth_year`, the rate hard bounds, the rule/SWR pairing, the three mode enums,
  manual amounts (0..MAX, linked amounts null) and "default not archived". Child rows use
  composite `(workspace_id, scenario_id)` foreign keys with `ON DELETE CASCADE`; contribution
  years are unique per scenario. Both child amount columns are signed minor units.
- `services.fire_scenarios` is the only writer: role checks in the service (plus
  `require_role_now` under the write lock), `CurrentWorkspaceRepository.lock(exclusive=True)`,
  audit events without amounts (`fire_scenario.created`, `.updated`, `.duplicated`,
  `.archived`, `.restored`, `.default_changed`) and the injected clock for `archived_at`.
  The first active scenario becomes the default; archiving the default promotes the next active
  scenario; restoring into an empty active list promotes the restored one. There is no hard
  delete. Archived scenarios are read-only; restoring a name that another active scenario now
  uses renames the restored row with the same `(copy)` / `(copy N)` suffix as duplication.
  Saving reconciles the editors instead of replacing every child row: contributions are matched
  by year and events by a hidden row id, so unchanged rows keep their ids and timestamps (F8.6
  archive lineage and audit rely on that). An event id that belongs to another scenario is
  refused with `invalid_event`, and the same id twice with `duplicate_event`.
- Validation returns `FireError(field, code)`: a required birth year (`birth_year_required`),
  `invalid_birth_year`, `horizon_not_after_age`, `stop_before_age`, `invalid_horizon`,
  `invalid_stop_age`, hard-bound rate errors, `extreme_rate_confirmation_required` for a
  **changed** rate outside the ordinary ranges, `duplicate_name`, `rows_outside_window`,
  `duplicate_contribution_year`, the 120/200 row caps, `invalid_event_kind`, `invalid_label`,
  `invalid_event`, `duplicate_event` and `archived`. A stop age after the horizon is allowed and
  shown as a warning. Contribution years are `start_year+1 … birth_year+horizon_age`; event
  years are `start_year … birth_year+horizon_age`. Rows are never deleted silently: an outside
  row blocks the save and stays on the form.
- F8.1 stores `income_mode`, `expenses_mode` and `portfolio_mode` but accepts only `manual`;
  linked values fail with `linked_mode_unavailable` until F8.4. The starting amounts are in
  today's money in the scenario currency. Illustrative prefill (2.5% inflation, 5% real return,
  3% income growth, 4% SWR) is editable and the page says it is not advice.
- The settings UI is `GET /fire` (`fire_scenarios`), `GET /fire/new` with `POST /fire`,
  `GET /fire/{id}`, `GET /fire/{id}/edit` with `POST /fire/{id}`, and POST actions for
  duplicate, default, archive and restore.
  Viewers read; editors and owners write. The form has plain-language tooltips, the
  contribution editor (add/remove rows and a paste column that accepts bare amounts with blank
  lines consuming a year, or `year, amount` / `year<TAB>amount` pairs), the events editor and
  the disclaimer. The sidebar's disabled FIRE item is now a link.
- The three models are in `EMPTINESS_CHECKS` but excluded from the archive format until F8.6
  (`FIRE scenarios require the F8.6 archive extension`), and excluded from the demo with
  reasoned entries. Audit events are shown in a new **FIRE planner** group. Polish translations
  are complete; the user guide is `docs/fire.md`.

---

## F8.2 Projection engine (pure domain)

**Acceptance**
- `project(inputs) -> Projection` in `domain/fire.py`; no I/O. Rows contain nominal and **real (today's money)** values for income, expenses, net, gain, net worth.
- **Parity mode** (default for the `returns_cover_expenses` rule) follows the formulas in this spec exactly, including the `t = 0` convention.
- Extensions on top, all optional and neutral when unused: contribution overrides; events (`cash`, `expense_change`, `income_change`); rule `swr` (FIRE when `networth_{t-1} × swr ≥ expenses_t`); continuation until `horizon_age`.
- Metrics: `fire_year`, `fire_age` (or `None`), `networth_at_fire` (nominal and real), `fire_number_today` (real: annual expenses ÷ `swr`, or ÷ `(i + r)`-equivalent for the sheet rule, documented), `progress_sheet`, `progress_today = start_portfolio / fire_number_today`, `years_to_fire`, `depletion_age` (first age with net worth < 0 after income stops, else `None`), `min_networth_after_stop`.
- **Golden test:** an independent naive loop in the test module, transcribed from the spreadsheet formulas above, matches the engine exactly (every row, FIRE age, % achieved) for at least three input sets, including one where FIRE is never reached and one with overrides for the first ten years.
- Property tests: with zero return and zero inflation, net worth equals start + cumulative net; with events absent, results equal parity mode; real values equal nominal ÷ (1+i)^t.
- Uses integer minor units internally; a single rounding step per row (documented), or `Decimal` with explicit quantization — state the choice in an ADR.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F8.2 from docs/specs/M8-fire-planner.md: pure project() with parity mode exactly as the formulas in the spec, plus overrides, events,
the swr rule, horizon continuation, and all listed metrics (nominal and real). Write a short ADR on numeric representation/rounding.
Golden test: independent naive loop in the tests; plus the property tests. No UI.
```

### As built (M8.F8.2)

- `domain.fire` exports `project(inputs: ProjectionInputs) -> Projection` plus
  `ContributionOverride`, `ProjectionEvent`, `ProjectionInputs`, `ProjectionRow`, `Projection`
  and `FireProjectionError(field, code)`. It is pure: no database, clock, environment or I/O.
  The F8.1 names and behaviour are unchanged.
- Money stays in integer minor units and rates in basis points; `net`, `net_worth`, cash and the
  result metrics use `Money`. Each computed line is rounded half away from zero once; real values
  are one deflation step from each nominal line. The returned `Projection.inputs` is the validated
  input with the currency canonicalized to uppercase, so it matches the rows. The recurrence, the
  additive `i + r`, the `t = 0` convention and the strict-`<` timing artefact are recorded in
  ADR-0025.
- Rows run from `start_year` through `birth_year + horizon_age` (`horizon_age - age_at_start + 1`
  rows). `t = 0` reuses the net column as `start_portfolio`, earns
  `round(start_portfolio * (i + r))` and adds start-year cash after that gain. Later rows grow
  base income with `g` while `age < stop_working_age` and base expenses with `i`, use
  `net = override` when present else `income - expenses`, and add cash after the gain. Base
  series and event adjustments stay separate: income changes inflate with `g` and expense
  changes with `i` from the start year, active from the event year; same-kind/same-year events
  sum before one rounding, while different event years round separately. Income changes continue
  after the salary stop. The start-year row always pays the base income, even when
  `stop_working_age` equals `age_at_start`; `income_stopped` is false there. Negative series and
  overrides are not clamped.
- FIRE is the first row with a non-negative opening net worth and either `expenses < gain`
  (`returns_cover_expenses`, strict) or `opening * swr_bp >= expenses * 10_000` (`swr`,
  inclusive); cash does not enter the predicate in its event year. Only the first crossing is
  marked; rows continue to the horizon and after income stops.
- Metrics: `fire_index/year/age`, `years_to_fire`, nominal and real `networth_at_fire`,
  `fire_number_today` (`expenses_0 / (i + r)` when that is positive, or `/ swr`; zero expenses
  give zero, otherwise an undefined number is `None`), `progress_sheet_bp`, `progress_today_bp`
  (computed even when FIRE is never reached), `depletion_age` and `min_networth_after_stop` over
  rows from `max(1, stop_age - age_at_start)` onward. Progress is unbounded basis points;
  undefined ratios are `None`. Never-FIRE plans leave the FIRE-dependent fields `None`.
- Input validation mirrors F8.1: supported currency and matching `Money` currencies, the year
  range and `birth_year <= start_year`, `MIN_HORIZON_AGE..MAX_HORIZON_AGE` with
  `horizon_age > age_at_start`, the last year within `MAX_YEAR`, stop-age bounds, hard rate
  bounds, the rule/SWR pairing, non-negative start amounts up to `MAX_AMOUNT_MINOR`, contribution
  years `start_year+1..last_year` with unique years, event years `start_year..last_year`, valid
  event kinds and signed amount limits. Errors are `FireProjectionError(field, code)`. The
  last-year check is a future guard: F8.1's event window has no `MAX_YEAR` cap, but a scenario
  created now cannot reach it with the 120-year horizon.
- Tests: `tests/unit/test_fire_projection.py` compares every row field and metric with an
  independent `Fraction`/half-up oracle for five golden sets (parity, never-FIRE + depletion,
  first-ten-years overrides, events, SWR), plus deterministic edge and validation cases;
  `tests/unit/test_fire_projection_properties.py` covers zero return + inflation, the no-event
  parity recurrence and real = nominal / (1 + i)^t. No service adapter, page, migration, JS,
  template or translatable string is added; F8.3 consumes `project()`.

---

## F8.3 Scenario page: table, chart, key metrics

**Acceptance**
- Key metrics header: FIRE age/year, years to FIRE, FIRE number (today's money), progress bar (sheet definition and today's-money definition shown together with a tooltip explaining the difference), depletion age warning if any.
- Year-by-year table (like the spreadsheet) with a nominal/real toggle; FIRE row highlighted; rows after income stops styled differently; CSV export.
- Chart: net worth over time with the FIRE point marked; second chart: expenses vs investment gain lines showing the crossing.
- Changing any input re-renders results instantly (HTMX; no page reload) with debounce.
- Empty/never-reached states are explained ("With these inputs returns never cover expenses before age 95").

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F8.3 from docs/specs/M8-fire-planner.md: scenario results page with key metrics, nominal/real table toggle, CSV export, two ECharts charts,
live recalculation via HTMX with debounce. The UI calls the service that calls project(); no math in templates or JS.
```

### As built (M8.F8.3)

- `GET /fire/{id}` is the scenario's combined page: results first, then a collapsible
  **Plan inputs** panel with the complete F8.1 form. `GET /fire/{id}/edit` is a 303 to
  `?edit=1#inputs` (archived: `?error=archived`). `POST /fire/{id}/preview` validates and
  projects the posted form without writing and returns only the results fragment; an invalid
  preview keeps the saved results and names the field. `GET /fire/{id}/results.csv` writes both
  bases with the fixed header, CRLF, plain dot decimals, no BOM and `1`/`0` flags. The existing
  create, duplicate, default, archive and restore actions are unchanged; saving preserves the
  nominal/real basis in the redirect.
- `services.fire_projection` maps a saved scenario and its child rows to `ProjectionInputs`,
  reports a missing linked starting value or an invalid saved plan as a `ResultsBlock` instead of
  zero or a server error, and offers `inputs_from_validated` for the save-free preview. A saved
  result costs three queries; the preview performs no queries and no writes. Archived scenarios
  still project but never preview; previews require an editor.
- `services.fire_scenarios.validate_scenario_input` is public and takes `previous_rates`; preview
  mode skips the unusual-rate save gate (the result reports `needs_extreme_confirmation` until the
  confirmation box is ticked) and name uniqueness stays a repository check, so another scenario's
  name cannot block a preview. All F8.1 create/update behavior is preserved.
- Metrics show FIRE age/year, years to FIRE, the FIRE number, depletion/minimum-after-stop, and
  both progress definitions (sheet and today's money) with exact percentages and bars capped
  visually at 100%. The nominal/real table marks FIRE, income-stopped and depleted rows; the cash
  column appears only when it has a value; the `t = 0` net column footnote is present. Never,
  depletion and blocked states are explained; undefined values are `—`, never 0.
- `web.fire_charts` prepares both ECharts series, localized axes/tooltips/series names, FIRE
  markLine/markPoint, income-stop/depletion markers, and the accessible plotted-data rows. The
  net-worth chart follows the basis; expenses-versus-gain stays nominal. Chart JSON travels in a
  `data-chart-json` attribute, so HTMX swaps cannot strip it; `charts.js` reads it (existing
  script sources still work), redraws after swaps and disposes detached instances. The shared
  nice-axis helper is `web.chart_axis`; loan charts use the same code unchanged. The results page
  loads the chart scripts whenever it can show charts or preview, so a blocked saved plan can
  draw the charts that a successful preview produces.
- The live preview is enabled only for editors on active scenarios with `hx-post`, `hx-include`,
  debounced `hx-trigger`, `hx-target`/`hx-swap` and `hx-sync`; `fire.js` dispatches
  `fire:changed` after row add/remove/paste and marks a preview-error field. The form wraps the
  results and the inputs, while only `#fire-results` is swapped: focus and unsaved values stay,
  and previews never save. The nominal/real switch is a radio group in that form for editors, so
  changing it previews in place instead of navigating; viewers and archived plans keep plain
  links.
- `plain_signed_amount` in `domain.ledger_csv` writes negative CSV amounts; the FIRE CSV uses it
  for every amount. No migration, model, dependency or ADR is added. Polish translations are
  complete with no fuzzy entries; the user guide is `docs/fire.md`.

---

## F8.4 Inputs linked to real data

**Acceptance**
- `portfolio_mode=linked_investable`: starting portfolio = value of investable portfolio classes (M6, look-through) + net-worth items flagged `investable` (e.g., cash savings), in base currency, as of today. Shows the breakdown and a link to each source.
- `expenses_mode=trailing_12m`: last 12 **full** months of expense transactions with `treatment=regular` (savings and transfers excluded), shown with per-category breakdown; one-off "unusual" transactions can be excluded by the user via a per-scenario exclusion list of categories.
- `income_mode=trailing_12m`: earned income over the same window (regular treatment). Fewer than 12 months of data → use available months scaled, with a visible warning.
- **Loan payoff suggestion:** for each loan (M7) show "Payoff in <year> frees <amount>/year (today's money)"; one click creates an `expense_change` event (negative expense change) for that year.
- Linked values display "as of" timestamps and never silently fall back to zero: missing data → blocking message in the scenario header.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F8.4 from docs/specs/M8-fire-planner.md: services that compute linked starting portfolio, trailing-12-month expenses/income (full months,
treatment=regular, category exclusions), and loan-payoff event suggestions; wire the *_mode fields in the scenario form and results.
Tests with the fixed Clock: window boundaries (full months only), savings/transfer exclusion, fewer-than-12-months handling, linked portfolio equals M6 valuation.
```

---

## F8.5 Scenario comparison and sensitivity

**Acceptance**
- Compare up to 3 scenarios: table (FIRE age/year, net worth at FIRE real, depletion age, years to FIRE) and overlay chart of net worth.
- **Sensitivity grid** for one scenario: pure `sensitivity(inputs, return_deltas, expense_factors) -> grid` of FIRE age; default axes: real return −2…+2 pp in 1 pp steps × expenses −20%…+20% in 10% steps; cells show age or "—" (never); heat-map styling.
- Tests: the base cell equals the scenario's FIRE age; monotonicity (higher return never delays FIRE; higher expenses never accelerate it) as property tests.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F8.5 from docs/specs/M8-fire-planner.md: multi-scenario comparison page and a pure sensitivity() function with the monotonicity property tests,
heat-map grid UI.
```

---

## F8.6 Archive extension: FIRE scenarios

Replaces the former workbook "importer extension" (M4 no longer has a spreadsheet importer; owner
decision, 2026-10-18). A scenario from a spreadsheet is entered with the F8.1 form.

**Acceptance**
- New archive files (a minor format version per ADR-0016) with lineage `id` columns as in M4:
  `fire_scenarios.csv`, `fire_contributions.csv`, `fire_events.csv`; links to other modules'
  data by lineage id.
- Restore only (no standalone import); a scenario whose linked data is missing is restored with
  manual values and listed.
- The new models join `EMPTINESS_CHECKS`; the M4 round-trip test covers them, and `project()`
  gives the same result for a restored scenario.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F8.6 from docs/specs/M8-fire-planner.md as an extension of the M4 archive (docs/specs/M4-ledger-export-import.md,
ADR-0016): the new files with lineage ids, export, restore, emptiness checks, extended round-trip test with identical
projections after restore.
```

---

## F8.7 (stretch) Monte Carlo and Coast FIRE

**Acceptance:** Monte Carlo with user-set mean/volatility of real returns (seeded RNG for reproducibility) showing the probability the portfolio survives to `horizon_age` and percentile bands (10/50/90) on the chart; Coast FIRE number = portfolio needed today so growth alone reaches the FIRE number by a target age; both clearly labelled as simplified models.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F8.7 (stretch) from docs/specs/M8-fire-planner.md: pure monte_carlo() with a seeded numpy-free RNG (or justify numpy), percentile bands,
survival probability, coast_fire_number(). Tests: determinism with the same seed; zero-volatility result equals the deterministic projection.
```
