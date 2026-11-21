# M10 — Reports

**Goal:** one Reports page with charts over the years: how much came in and went out, how much
was saved, where the money went, and later how net worth and the portfolio grew.

**Exit criteria (Phase A and B):** for any range of years a member sees income vs expenses and
the savings rate per month and per year, expenses by category over time, and net worth and
portfolio value over the same range, with figures that match the month summaries of F2.4 and the
existing module series exactly. Phase A shipped in v1.0; Phase B shipped after 1.0 on
2026-11-19, reusing the Net worth Summary and Investments Performance services.

Owner decision, 2026-10-25. The milestone number only follows M9; it is not scheduled after it.
**Phase A depends on M2 only** (and should follow F2.7, whose chart conventions it reuses) and
can ship before M5/M6 in any minor release. Phase B features depend on the milestones named in
each feature and shipped after 1.0 on 2026-11-19.

Every agent prompt starts with: *"Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go)."*

**Data model:** none. Reports read existing rows (transactions, categories, and in Phase B
the derived net-worth history and portfolio data) and store nothing; there is nothing to add to
the M4 archive.

**Shared rules for every report**
- **Page:** `/reports` ("Reports", pl "Raporty") last in the default sidebar order. Every member,
  viewers included, can open it (it shows what they can already read); the services check the
  role like every other read.
- **Filters** (a plain GET form, so a view is a bookmarkable URL; works without JavaScript):
  - **Remembered settings.** The last resolved filters (years, granularity, currency, categories,
    savings, report year and trend window) are kept in a one-year browser cookie (`HttpOnly`,
    `SameSite=Lax`; no amounts). A valid explicit report query parameter wins and refreshes it;
    an invalid explicit request shows its errors and keeps the previous settings. A bare
    `/reports` reads it; a cookie whose values no longer validate falls back to the current
    defaults and is cleared. `clear=1` deletes it and redirects to the clean page. The filter
    panel is closed by default and opens automatically when a field has a validation error.
  - **Year range** `from=YYYY&to=YYYY`, inclusive; default: the last three years that have
    transactions (the current year when there are none); at most 30 years; `from > to`,
    malformed or out-of-range values are marked on the form and show no charts. The dropdowns
    offer only years with transactions, plus an explicitly requested endpoint.
  - **Granularity** `step=month|year` (default `month` for a range up to 3 years, `year`
    beyond).
  - **Currency** `currency=XXX`: defaults to the workspace base currency; the selector lists
    only currencies that occur in the range. In v1 every ledger amount is in the base currency
    (it is locked once data exists, F2.2), so the selector shows one value; amounts of
    different currencies are never added together (an amount in another currency than the
    filter is left out and counted in a note, never converted in Phase A).
  - Unknown parameters are ignored.
- **Months and dates:** a transaction belongs to the calendar month of its `date` (already a
  workspace-calendar date). The range ends at the current month (from the Clock, workspace time
  zone); the current period in the period tables and the current month in the year overview are
  marked "so far". The year-over-year table has no badge; its current-year row is noted as still
  in progress. Future-dated entries are not charted; a note says how many there are.
- **Treatment rules** are those of 00-overview §3.3 and F2.4, reused, not re-implemented:
  `earned` = income/regular, `spent` = expense/regular, `saved` = expense/savings; transfers are
  in nothing; withdrawals from savings (income/savings) are not earnings. Soft-deleted rows never
  count; closed and open months count alike.
- **Aggregation:** amounts are summed as integer minor units **in Python** (domain layer), not
  with SQL aggregates, so the month figures are the same code path as F2.4. The service reads
  only the needed columns (`date`, `direction`, `amount_minor`, `currency`, `category_id`)
  through the workspace-scoped `TransactionRepository`, streamed in batches, ordered by date.
  No report touches a `DecimalType` column in SQL (Phase B quantities and prices are aggregated
  in Python as M6 requires). Year totals are sums of month totals (integers, exact); ratios are
  computed from the summed integers and rounded **once** (savings rate: basis points and tenths
  of a percent, half away from zero, as F2.4).
- **Charts** follow F2.7: vendored ECharts, data in a `<script type="application/json">` block
  read by a static module, no inline scripts or styles, colours from the CSS tokens in both
  themes (category series in the category colour), canvas `aria-hidden` with an sr-only table
  of the same numbers next to each chart (shown visibly without JavaScript), labels formatted by
  the server's locale helpers. Each chart has an empty state when the range has no data.
- **Performance:** 10 years × 5 000 transactions a year renders the Phase A page in < 500 ms
  on the CI runner (benchmark test with a generous margin), with one streamed read of the
  transactions per request, not one per month. Phase B passes the report's exact period-end
  dates to the net-worth and portfolio services; a short range reads from its start through
  today plus the carry-forward (and estimate/backfill) anchors, not every historical valuation
  and price.

**Phases**

| Phase | Features | Depends on |
|---|---|---|
| A (ledger only) | F10.1, F10.2, F10.3 | M2 (F2.4; F2.7 for the chart conventions) |
| B | F10.4 | M5 (F5.9 derived history) |
| B | F10.5 | M6 (F6.7 `portfolio_series`) |

---

## F10.1 Reports page and income vs expenses

**Story:** I see, month by month and year by year, how much I earned against how much I spent.

**Acceptance**
- The `/reports` page with the shared filters and the sidebar entry.
- Pure domain function, for example `domain.reports.ledger_series(entries, categories, *,
  start, end, step, currency) -> list[PeriodTotals]` with `PeriodTotals(period, earned, spent,
  saved, net)` as `Money`; periods with no entries are present with zeros (the series has no
  gaps).
- Chart: grouped bars per period, **earned** vs **spent**, with **saved** as a third series
  that the legend can hide; `net = earned - spent - saved` in the table (as F2.4).
- Table (sr-only next to the chart; also linked "Show as table"): period, earned, spent, saved,
  net; a total row for the range.
- Each month period in the table links to its Month page (`/m/YYYY-MM`).
- **Golden test:** for a synthetic workspace with 14 months of entries (including transfers,
  savings, withdrawals, a soft-deleted row and a closed month), every month's earned / spent /
  saved equals `services.summary.month_summary` for that month (F2.4 is the oracle), and each
  year equals the sum of its months.
- Property tests: series totals equal the sum of the included entries per treatment; transfers
  and soft-deleted rows never change any figure; every period in `[start, end]` appears once.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F10.1 from docs/specs/M10-reports.md: the /reports page with the shared filters (year range, step, currency),
a pure domain.reports.ledger_series in integer minor units reusing the F2.4 treatment rules, a streamed column-only read
through the scoped TransactionRepository (no SQL aggregates), the income vs expenses chart with its table (F2.7 chart
conventions). Golden test against services.summary.month_summary; property tests; the performance benchmark.
```

**As built (F10.1):**
- `/reports` is last in the default sidebar order; every member, viewers included, may open it. The
  filters are one plain GET form; unknown parameters are ignored.
- Defaults follow the data: the last three years that have transactions (the current year when
  there are none), monthly for a range up to 3 years and yearly beyond. The year selectors offer
  only years with transactions. The currency selector is hidden when only one currency occurs
  (always the base in v1). Withdrawals are not a series. There is no per-table CSV in this
  feature.
- The range ends with the current month in the workspace's time zone; a `to` year later than the
  current one is kept in the form and the chart stops at the current month with a note. Entries
  dated after today are excluded and counted in a note. Amounts in another currency than the
  selected one are excluded and counted, never converted.
- The service reads the categories, one distinct currency lookup and one column-only stream of
  transactions (`TransactionRepository.iter_ledger_columns`) per request; the table's total row is
  the exact integer sum of the periods. The performance test holds 10 years × 5 000 rows to
  2.5 s (a 5× margin over the 500 ms target on CI; F10.3 shares the same read and one pure pass
  now builds both report series, so the budget was raised from F10.1's original 2 s).
- The chart is a new `report-bars` kind of the F2.7 chart module: grouped bars, ECharts' legend
  hides a series, server-formatted labels, token colours, and an sr-only table with a
  “Show as table” control. User guide: [Reports](../reports.md).
- **As built (F11.8, 2026-11-20):** a range with no ledger data shows one actionable empty state
  instead of zero/dash tables; the filters use the shared disclosure with an active-filter
  count; a compact section nav links every visible section; portfolio gains use the
  success/danger colours; and an undated-only portfolio series keeps its section with the
  explanatory note instead of disappearing. (Reports polish 2 removed the net-worth
  section-level notes and closed the filter panel by default; a chartless net-worth series now has
  no section.)

---

## F10.2 Savings rate per month and per year

**Story:** I see whether I keep a larger share of what I earn than I used to.

**Acceptance**
- Uses F10.1's series. Savings rate per period `= saved / earned` (00-overview §3.3, owner
  decision 2026-10-24), from the period's summed integers; a **yearly rate is computed from the
  year's sums, never as an average of monthly rates**. This is the same measure as F2.4's KPI.
- Kept as basis points and tenths of a percent, each rounded once from the exact ratio (F2.4's
  rule); a period with `earned = 0` has no rate (a gap in the line, "—" in the table, "Not
  available: nothing earned" for screen readers).
- Chart: line of the rate per period; with `step=month` also a 12-month rolling rate (from the
  rolling 12-month sums, not averaged rates), shown from the 12th month of the range.
- A negative rate, if present in the data, is shown below zero.
- Golden tests: the F2.4 example month (saved 0 of 3 400.00 earned) gives 0.0%, not the old
  −43.3% left-over measure; a month with 800.00 saved of 3 000.00 earned gives 26.7%; a year
  whose months save 2 000.00 of 4 000.00 (50%) and 0 of 1 000.00 (0%) gives 40.0%, not 25.0%.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F10.2 from docs/specs/M10-reports.md: savings rate per period and the 12-month rolling rate from summed integers,
rounded once as in F2.4, with gaps for zero earnings; chart and table on /reports. Golden tests from the spec.
```

**As built (F10.2):**
- `domain.reports.savings_rate(earned, saved)` returns a `SavingsRate(bp, tenths)`; both values
  are rounded once by F2.4's `savings_rate_bp` / `savings_rate_tenths` (shared, not copied).
  `PeriodTotals.savings_rate` applies it to a period's exact sums;
  `rolling_savings_rates(series, months=12)` computes each window's saved/earned rate from its
  summed integers in one pass and returns an unavailable rate before the 12th month.
- The page adds a second section with a line chart (ECharts `line` options through the report
  chart module) and its own table: period, rate, and — monthly with at least 12 months — the
  rolling rate, plus a total row from the exact range sums. A missing point is `null` in the
  chart JSON (a gap, never zero); the table shows “—” with an sr-only reason. The rate axis
  includes zero and can extend below it for a negative rate. The monthly table rows keep the
  same month links as F10.1; the shared filters, currency selector and notes are unchanged.
- The earlier F10.2 draft used the pre-2026-10-24 `(earned - spent) / earned` formula. The owner
  changed the KPI to `saved / earned` in the overview and M2; this feature follows that decision
  so the report matches F2.4, and the stale acceptance/goldens were corrected in this PR.
- No migration and no new dependency: the feature reads F10.1's `ledger_series` only.

---

## F10.3 Expenses by category over time

**Story:** I see which categories grow and which shrink over the years.

**Acceptance**
- Pure domain function, for example `domain.reports.category_series(entries, categories, *,
  start, end, step, currency) -> CategorySeries`: actual spending per regular expense category
  per period (`kind=expense, treatment=regular`), in minor units.
- Chart: stacked bars per period, one series per category in its colour. By default the 8
  categories with the largest total in the range are shown and the rest are combined as
  "Other (combined)"; a category multi-select (GET parameter `category`, repeatable) shows
  exactly the chosen ones (no combined row). Archived categories appear under their name
  (marked "archived" in the chart/table/filter) when they have spending in the range.
- Option "Include saved / invested" (`savings=1`) adds the expense/savings categories as their
  own series.
- Table: category × period with row and column totals; the column totals equal F10.1's `spent`
  (plus `saved` when the option is on) for every period (property test).
- An unknown or other workspace's category id in the filter is ignored.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F10.3 from docs/specs/M10-reports.md: pure category_series, the stacked chart with top-8 + Other, the category
multi-select and the savings option, and the category x period table. Property test: column totals equal F10.1's spent
(and saved with the option).
```

**As built (F10.3):**
- `domain.reports.category_series` returns a `CategorySeries` of named/coloured `CategorySpend`
  rows (one per regular expense category, or per expense/savings category with the option) in
  integer minor units, ordered by range total. `top_categories` combines everything after the
  eight largest into one unnamed row that the view labels "Other (combined)"; it keeps every
  period total. A real category named "Other" and an archived duplicate are disambiguated by the
  label/archived marker, so ECharts legend names remain unique.
  A non-empty `selected` shows exactly those categories (zero rows included) and never combines.
- The service keeps F10.1's single scoped column read and runs one pure pass
  (`ledger_and_category_series`) that accumulates the ledger totals and the per-category sums
  together; a category filter narrows the computed series without a second pass. The repeatable
  `category` parameter is resolved against the workspace's eligible categories, so unknown and
  other-workspace ids are ignored and fall back to the default when none survive. Archived
  categories are offered only when they have spending in the range; they keep their current name
  and colour in the chart and table.
- The page adds a third section with a stacked-bar chart (new `report-category-bars` kind of the
  report chart module; the same `report-bars` option builder and a `stack` name), the category
  multi-select and "Include saved / invested" checkbox in the shared filter form, and the
  category × period table with row, column and grand totals. Category series use the category's
  `#rrggbb` colour or a theme token for Other; labels and amounts are server-formatted. A filter
  with no spending still shows its zero table row and a filter-specific empty state.
- Column totals equal F10.1's `spent` (and `saved` with the option) for every period without a
  category filter; the property test proves it against `ledger_series`. No migration and no new
  dependency. User guide: [Reports](../reports.md).

---

## F10.4 Net worth over time (Phase B, depends on M5)

**Acceptance**
- Shown only when the workspace has net-worth values (F5.9); before M5 exists the section is
  absent, not disabled.
- Line chart per period: assets, liabilities and net worth, from F5.9's derived history service.
  The report reuses that service rather than reading `NetWorthSnapshot`; stored snapshots are
  legacy archive data.
- Base currency only, as the derived service already is. Non-base, valueless and unavailable
  linked items stay excluded; a series with nothing to chart is absent (including one whose only
  items are in another currency). The Reports section shows no not-counted note: chart tooltips
  keep the per-point detail and estimated-value notes, and the Net worth Summary page keeps its
  warning. (Owner decision, 2026-11-21: the section-level notes were removed.)
- Periods without a derived point are gaps, never interpolated.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F10.4 from docs/specs/M10-reports.md: net worth over time on /reports from F5.9's derived history
service (last point per period, gaps not interpolated), hidden when there are no values. Do not read stored snapshots.
```

**As built (2026-11-19, bounded 2026-11-20; notes revised 2026-11-21).** `services.reports.net_worth_report` calls F5.9's
`networth_history(dates=...)` with the report period ends. The service reads only the report
window plus a carry-forward anchor, drops dates before the first recorded value, and keeps the
last point of each report month/year. The `/reports` section
reuses `web.networth_history.build_trend_view` for the chart, and one shared series macro renders
the chart and accessible table. The section shows no not-counted notes; the per-point detail stays
in the chart tooltips and on the Net worth Summary page. A series with no chart (including a
non-base-only one) is absent. Tests cover year/month selection, the base-currency rule and the
empty workspace.

---

## F10.5 Portfolio value over time (Phase B, depends on M6)

**Acceptance**
- Shown only when the workspace has investment transactions (M6).
- Line chart of portfolio value and net invested capital per period, from F6.7's
  `portfolio_series` with explicit report period-end dates (month-end or year-end, capped at
  today). No new portfolio math; quantities and prices stay `Decimal` and are aggregated in
  Python.
- Values in the filter currency (the base currency; F6.3 FX rules apply).

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F10.5 from docs/specs/M10-reports.md: portfolio value and invested capital over time on /reports from F6.7's
portfolio_series, hidden without investment data. No new portfolio math.
```

**As built (2026-11-19).** `services.reports.portfolio_report` calls F6.7's `performance_page`
with explicit report period-end dates (month/year end, capped at today); `web.charts.build_performance_chart` supplies the same chart JSON as the Performance page and Home widget.
The section is absent without investment transactions or outside the base currency. Tests cover
year/month selection, the base-currency rule and the empty workspace.

---

## Reports polish 2 (2026-11-21, owner request)

**As built (2026-11-21).** No migration and no new dependency.

- **Section navigation:** the links panel wraps with a flex row instead of scrolling sideways
  (`panel` kept, `overflow-x-auto` and `whitespace-nowrap` removed). It works at 390 px and
  1440 px; every visible section is reachable.
- **Filters:** the shared `filter_panel` macro gained `open=true`; Reports passes `false` unless
  the view has validation errors. The last resolved filters (from, to, step, currency,
  categories, savings, year, trend) are kept in a one-year `HttpOnly`, `SameSite=Lax` browser
  cookie. Any explicit report parameter wins and refreshes it; a bare `/reports` reads it; an
  invalid or oversized cookie falls back to the current defaults. `clear=1` deletes it and
  redirects to the clean URL. A **Clear filters** button submits that action inside the GET form.
  URLs stay bookmarkable and no-JS.
- **Year overview heat:** each category row now follows the Transactions T-1 gate. The comparison
  group is the category's non-zero months: at least three are needed, a month must be at least
  twice the row median, and the four steps then use the existing max-relative scale against the
  row's largest non-zero month. `domain/shading.py`'s `large_gates`, `maxima` and `shade` are
  reused; zero and low months stay neutral.
- **Net worth section:** the section-level “items had no value / another currency / unavailable”
  notes are gone. Chart tooltips keep the per-point detail and estimated-value notes, and the
  Net worth Summary page keeps its warning. A series with no chart has no section.

---

## Relation to M9

F9.2 (M9) originally listed "income vs expenses vs saved per month and per year; savings-rate
trend". Those move here (F10.1, F10.2). F9.2's remaining reports (year overview grid, rolling
category averages, year-over-year, planning accuracy) are added as further sections of
this `/reports` page when M9 is built, using the same filters and aggregation rules.

**As built (F9.2):** the page now also has the year overview (a category × month grid for a
selected `year`, with heat shading and month links), category trends (3- or 12-month rolling
averages over the displayed F10.3 series, `trend=3|12`), year-over-year (one row per year in the
range, income, expenses and savings, separate value and percent changes against the previous
year) and planning accuracy (an absolute-deviation score plus planned regular expense categories,
months planned/over/strictly greater, from the raw budget lines in one scoped read). The year
selectors offer only years with transactions; the report-table CSV was removed. Section details
and assumptions are in M9 F9.2; the user guide is [Reports](../reports.md).

**As built (2026-11-21, UI improvements R-4/R-5).**
- **Year-over-year:** full calendar years; the domain no longer clips the current year or the
  previous year to the current month. The service still reads the year before the range and the
  current year through today. `YearOverYearRow` carries `saved`, `saved_change`,
  `saved_change_tenths` and `has_previous`; changes and percentages are zero/`None` when the
  previous calendar year has no recorded income, expenses or savings. The table adds
  **Savings & investments** and a savings change group, includes savings in its footer, and
  always uses the whole ledger (the `savings=1` filter does not change it). Investment-module
  purchases are out of scope.
- **Planning accuracy:** `PlanAccuracy` carries the summed `planned`, summed absolute
  `deviation` and `score_tenths = max(0, 1000 − ratio_tenths(deviation, planned))`, rounded once
  and `None` when the planned total is zero. The section shows the score and an SVG progress bar
  above the unchanged months planned/over breakdown. Only planned regular expenses count; income
  and savings are excluded.

## Open questions (for the owner)

1. **Default range and step.** Resolved: the last three years with transactions, or the current
   year when there are none; monthly up to 3 years and yearly beyond.
2. **Currency filter.** In v1 it can only show the base currency. Keep the selector visible (one
   value) for later multi-currency, or hide it until a second currency exists? Proposed: hide
   it when only one currency occurs.
3. **CSV of the report tables.** Decided against: the year-overview CSV was removed and report
   tables have no CSV download. Ledger CSV stays on [History](../transactions.md).
4. **Withdrawals from savings.** Proposed: not shown in Phase A (they are not earnings). Should
   F10.1 show them as their own series?
5. **Sidebar placement.** Last in the default sidebar order. Or among the later modules (Net worth,
   Investments)?
