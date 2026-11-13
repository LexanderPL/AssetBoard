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
