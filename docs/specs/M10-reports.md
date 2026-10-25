# M10 — Reports

**Goal:** one Reports page with charts over the years: how much came in and went out, how much
was saved, where the money went, and later how net worth and the portfolio grew.

**Exit criteria (Phase A):** for any range of years a member sees income vs expenses and the
savings rate per month and per year, and expenses by category over time, with figures that
match the month summaries of F2.4 exactly. **Phase B** adds net worth (after M5) and portfolio
value (after M6) to the same page.

Owner decision, 2026-10-25. The milestone number only follows M9; it is not scheduled after it.
**Phase A depends on M2 only** (and should follow F2.7, whose chart conventions it reuses) and
can ship before M5/M6 in any minor release. Phase B features depend on the milestones named in
each feature.

Every agent prompt starts with: *"Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go)."*

**Data model:** none. Reports read existing rows (transactions, categories, and in Phase B
net-worth snapshots and portfolio data) and store nothing; there is nothing to add to the M4
archive.

**Shared rules for every report**
- **Page:** `/reports` ("Reports", pl "Raporty") in the sidebar after History. Every member,
  viewers included, can open it (it shows what they can already read); the services check the
  role like every other read.
- **Filters** (a plain GET form, so a view is a bookmarkable URL; works without JavaScript):
  - **Year range** `from=YYYY&to=YYYY`, inclusive; default: the current year and the two before
    it; at most 30 years; `from > to`, malformed or out-of-range values are marked on the form
    and show no charts.
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
  zone); the current month and year are marked "so far". Future-dated entries are not charted;
  a note says how many there are.
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
  transactions per request, not one per month.

**Phases**

| Phase | Features | Depends on |
|---|---|---|
| A (ledger only) | F10.1, F10.2, F10.3 | M2 (F2.4; F2.7 for the chart conventions) |
| B | F10.4 | M5 (F5.3 snapshots) |
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

---

## F10.2 Savings rate per month and per year

**Story:** I see whether I keep a larger share of what I earn than I used to.

**Acceptance**
- Uses F10.1's series. Savings rate per period `= (earned - spent) / earned` (00-overview
  §3.3), from the period's summed integers; a **yearly rate is computed from the year's sums,
  never as an average of monthly rates**.
- Kept as basis points and tenths of a percent, each rounded once from the exact ratio (F2.4's
  rule); a period with `earned = 0` has no rate (a gap in the line, "—" in the table, "Not
  available: nothing earned" for screen readers).
- Chart: line of the rate per period; with `step=month` also a 12-month rolling rate (from the
  rolling 12-month sums, not averaged rates), shown from the 12th month of the range.
- Negative rates are shown as such (the axis goes below zero).
- Golden tests: the F2.4 example month gives −43.3%; a year whose months are 50% and 0% with
  earnings 4 000.00 and 1 000.00 gives 40.0% (not 25.0%).

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F10.2 from docs/specs/M10-reports.md: savings rate per period and the 12-month rolling rate from summed integers,
rounded once as in F2.4, with gaps for zero earnings; chart and table on /reports. Golden tests from the spec.
```

---

## F10.3 Expenses by category over time

**Story:** I see which categories grow and which shrink over the years.

**Acceptance**
- Pure domain function, for example `domain.reports.category_series(entries, categories, *,
  start, end, step, currency) -> CategorySeries`: actual spending per regular expense category
  per period (`kind=expense, treatment=regular`), in minor units.
- Chart: stacked bars per period, one series per category in its colour. By default the 8
  categories with the largest total in the range are shown and the rest are combined as
  "Other"; a category multi-select (GET parameter `category`, repeatable) shows exactly the
  chosen ones (no "Other"). Archived categories appear under their name when they have
  spending in the range.
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

---

## F10.4 Net worth over time (Phase B, depends on M5)

**Acceptance**
- Shown only when the workspace has net-worth snapshots (F5.3); before M5 exists the section is
  absent, not disabled.
- Line chart per period: assets, liabilities and net worth, from `NetWorthSnapshot` (the last
  snapshot in each month or year; `backfill` snapshots are marked in the table, and a real
  snapshot wins over a backfill of the same date, as F5.3 says).
- Snapshots in another currency than the filter are left out and counted in a note.
- Periods without a snapshot are gaps, never interpolated.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F10.4 from docs/specs/M10-reports.md: net worth over time on /reports from F5.3's snapshots (last per period,
backfill marked, gaps not interpolated), hidden when there are no snapshots.
```

---

## F10.5 Portfolio value over time (Phase B, depends on M6)

**Acceptance**
- Shown only when the workspace has investment transactions (M6).
- Line chart of portfolio value and net invested capital per period, from F6.7's
  `portfolio_series(..., step=month)` (year step: the last month of each year). No new
  portfolio math; quantities and prices stay `Decimal` and are aggregated in Python.
- Values in the filter currency (the base currency; F6.3 FX rules apply).

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F10.5 from docs/specs/M10-reports.md: portfolio value and invested capital over time on /reports from F6.7's
portfolio_series, hidden without investment data. No new portfolio math.
```

---

## Relation to M9

F9.2 (M9) originally listed "income vs expenses vs saved per month and per year; savings-rate
trend". Those move here (F10.1, F10.2). F9.2's remaining reports (year overview grid, rolling
category averages, year-over-year, planned-vs-actual accuracy) are added as further sections of
this `/reports` page when M9 is built, using the same filters and aggregation rules.

## Open questions (for the owner)

1. **Default range and step.** Proposed: the current year and the two before, monthly up to
   3 years and yearly beyond. Prefer "all years with data"?
2. **Currency filter.** In v1 it can only show the base currency. Keep the selector visible (one
   value) for later multi-currency, or hide it until a second currency exists? Proposed: hide
   it when only one currency occurs.
3. **CSV of the report tables.** Not in Phase A (F2.6 and F9.2 cover CSV). Add a "Download CSV"
   per table?
4. **Withdrawals from savings.** Proposed: not shown in Phase A (they are not earnings). Should
   F10.1 show them as their own series?
5. **Sidebar placement.** Proposed: after History. Or among the later modules (Net worth,
   Investments)?
