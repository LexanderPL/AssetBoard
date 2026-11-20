# Reports

Reports (`/reports`) show how much came in and went out over a range of years, month by month or
year by year, where the spending went by category, how categories trend, how a year compares
with the one before, how often actual spending beat its plan, and how net worth and the portfolio
moved over the same range. Every member of a workspace, viewers included, can open the page: it
only reads what they can already see in the ledger.

Screenshots: [desktop light](assets/screenshots/desktop/light/reports.jpg) ·
[desktop dark](assets/screenshots/desktop/dark/reports.jpg) ·
[phone light](assets/screenshots/phone/light/reports.jpg) ·
[phone dark](assets/screenshots/phone/dark/reports.jpg). See the [gallery](screenshots.md#reports).

## Filters

The filters are a plain GET form, so every view is a bookmarkable address and works without
JavaScript. Unknown query parameters are ignored.

| Filter | Values | Default |
| --- | --- | --- |
| From year | `from=YYYY`, 1900–2199 | the earliest of the last three years that have transactions |
| To year | `to=YYYY`, inclusive | the latest of the last three years that have transactions |
| Granularity | `step=month` or `step=year` | monthly up to a 3-year range, yearly beyond |
| Currency | `currency=XXX` | the workspace's base currency |
| Categories | `category=<id>`, repeatable | empty: all categories in the year overview, the 8 largest (and Other) in the category charts |
| Include saved / invested | `savings=1` | off |
| Report year | `year=YYYY` | the latest data year inside the range, or the range end, capped at the current year |
| Category trends window | `trend=3` or `trend=12` | 3 months |

The **From year** and **To year** dropdowns offer only years that have at least one non-deleted
transaction on or before today. The default range uses the last three such years, or fewer when
they would span more than 30 years (and the current year when the workspace has no transactions).
A range can still span empty years between two selected years; those years show zeros. A URL may
name any year from 1900 to 2199, so an old bookmark still opens even when that year is not in the
dropdown.

The **Categories** and **Include saved / invested** filters apply to the expenses-by-category
section, the year overview and category trends. The year-over-year table always uses the whole
ledger. An unknown or malformed `year` is ignored and the default is used.

The **Report year** selector sits inside the **Year overview** panel and chooses the grid's year.
It does not change the range of the other sections, and its options are the data years inside the
selected range (plus an explicitly requested year). The **Net worth over time** and
**Portfolio value over time** sections follow **From year**, **To year** and **Granularity**,
ignore the category/savings filters, and are shown only in the workspace base currency.

A range is at most 30 years. A malformed year, `from` after `to`, or a longer range is marked on
the field and no chart is drawn. The range always ends with the current month in the workspace's
time zone, even when `to` names a later year; the page then says so.

The currency selector appears only when more than one currency occurs in the charted range; the
base currency is always listed as the fallback. In v1 ledger entries are in the workspace's base
currency, so it usually stays hidden. Amounts in another currency than the selected one are never
converted or added: they are left out and counted in a note.

## What counts

The report uses the same treatment rules as the month summary:

- **Earned** — income categories with the `regular` treatment.
- **Spent** — expense categories with the `regular` treatment.
- **Saved** — expense categories with the `savings` treatment (money moved to savings or
  investments).
- **Transfers** between your own accounts are in no figure.
- **Withdrawals from savings** (income with the `savings` treatment) are not earnings and are not
  a separate series in this first version.
- `net = earned - spent - saved`, exactly as on the Month page.

Soft-deleted entries never count. Closed and open months count alike. Entries dated after today
are not charted; the page notes how many there are. The current month and year are marked
“so far”.

## Chart and table

The chart draws grouped bars per period: **earned** against **spent**, with **saved** as a third
series. Use the legend to hide or show a series. Amounts and labels are formatted on the server
with your locale and the currency's native style.

The table below the chart has the same figures: period, earned, spent, saved and net, plus a
total row for the range. Each monthly row links to that month's page (`/m/YYYY-MM`); each yearly
row links to the Year overview focused on that year. With JavaScript the table is visually hidden
beside the drawn chart; **Show as table** reveals it. Without JavaScript the table stays visible
in place of the chart. An empty range shows an empty state and a table of zeros.

The same date links are used throughout the page: month labels open the month page, and year
labels (including year-overview column headings and year-over-year rows) open the Year overview
for that year with the current filters preserved. Chart data points and x-axis labels are
clickable too; the tables are the keyboard-accessible equivalent.

## Savings rate

The savings-rate chart shows the share of earnings moved to savings and investments:

`rate = saved / earned`

This is the same measure as the Month page's savings-rate KPI.

A yearly rate is computed from the year's summed amounts, never as an average of monthly rates.
Each rate is rounded once, half away from zero: the page keeps it as integer basis points and as
tenths of a percent for display. The total row uses the exact sums for the whole range.

A period with no earnings has no rate: the line has a gap, the table shows “—” and screen readers
hear “Not available: nothing earned”. A negative rate, if the data contains one, is drawn below
the zero line.

With monthly granularity and at least 12 months in the range, a second line shows the **12-month
rolling rate**. It is computed from the same integers over the last twelve months, not by
averaging monthly rates, and starts at the 12th month of the range.

Money merely left over is not savings: only expense categories with the `savings` treatment
count. See [Months](months.md) for the same KPI in the monthly summary.

## Expenses by category over time

The category chart stacks one bar segment per category for every period. It uses the same
treatment rules as the other reports: only expense categories with the `regular` treatment count;
income, transfers and withdrawals from savings never appear. A category's segment uses the
category's own colour, and archived categories keep their name and history when they had spending
in the range.

By default the eight categories with the largest total in the range are shown separately and all
others are combined into **Other (combined)**. A real category named “Other” stays a separate
series, so the two never collide in the legend or table. Choose one or more categories in the
**Categories** filter (the URL carries one `category=<id>` parameter per choice) to show exactly
those categories and no combined row. An unknown category id, a category of another workspace, or one that does not fit the
current option is ignored, so an old bookmark still opens.

**Include saved / invested** (`savings=1`) adds expense categories with the `savings` treatment
as their own series, so the chart covers everything the ledger counted as saved as well as spent.

The table has a row per category and a column per period, with row and column totals. Without a
category filter the column totals equal the income-vs-expenses report's **spent** for the same
period, plus **saved** when the option is on; **Other (combined)** takes the remainder. With a category
filter the column totals are the sum of the chosen categories.

## Year overview

The year overview is a category × month grid of actual spending for the **Report year**. It has
one row per category with spending in that year, one column for each month from January to
December, a row total, a column total and a grand total. Months without entries are zero. Closed
and open months count alike; entries dated after today are not charted.

The grid follows the category rules above: regular expenses only, plus expense categories with
the `savings` treatment when **Include saved / invested** is on. Income, withdrawals and transfers
never appear. Without a category filter every such category is shown; with a filter exactly the
chosen categories appear (including a zero row). A category's swatch uses its colour. Darker
cells mean more spending in that month; the shading uses the theme's colour tokens, so it follows
the light and dark themes.

The month column headings link to that month's page. The **Report year** selector in this panel
changes only the grid; changing it reloads the page with the other filters preserved.

## Category trends

The category-trends table shows a rolling average for each category shown in the category chart:
the **3-month** or **12-month** average chosen in **Category trends window**. A point is the exact
sum of the category's spending over that window divided by the window length, rounded once to
the currency's minor unit. The average is only shown when the window is full; earlier periods
show “—” (and a screen-reader reason) and are gaps in the chart, never zeros.

The section uses the same categories as the expenses-by-category chart: the selected categories,
or the eight largest plus **Other (combined)** when none are selected. It is monthly only; with
yearly granularity the section asks you to choose **Monthly**. The chart and table carry the same
figures; the table is the accessible version and works without JavaScript.

## Year-over-year

The year-over-year table has one row per year in the report range, up to the current year.
Each row shows the year's
**income** and **expenses**, then the change against the previous year for both: the absolute
value and the percentage. Income growth is shown as good and an income drop as bad; an expense
increase is shown as bad and a decrease as good. The signs and percentages carry the meaning, so
colour is not the only cue.

The percent change is `(current - previous) / previous`, rounded once to one decimal place. When
the previous year had no income or no expenses, that percentage is undefined: the table shows “—”
and screen readers hear why. The first year in the range is compared with the year before it even
when that year is outside the selected range. When the current year is included, both years are
compared through the current month ("so far") rather than a partial year against a full one. The
table uses the whole ledger: income, expenses and transfers follow the same treatment rules as
the rest of the page; the category filter does not narrow it.

## Planned vs actual accuracy

The plan-accuracy table answers how often each expense category's actual spending exceeded its
plan over the report range. For every **planned regular expense category** it shows:

- **Months planned** — calendar months in the range with a budget line for that category.
- **Months over** — planned months whose actual spending was **greater** than the plan. Equal
  amounts are not over.
- **Over %** — months over divided by months planned, rounded once to one decimal place.

Only regular expense categories with at least one planned month appear; transfers, income,
withdrawals, savings and categories without plans are left out. The table has a footer with the
same counts across all category-months. Open and closed months count alike, and plans for future
months are outside the report range. Amounts are compared in the selected currency; plans in
another currency are left out, never converted.

## Net worth over time

The net-worth section shows assets, liabilities and net worth at the end of each month or year
in the report range. It reuses the same derived history as the Net worth Summary page: saved
values, linked items and prices are calculated on read, and the last point of each period is
shown. There is no interpolation between periods.

The section appears only when the workspace has net-worth values and the selected currency is
the workspace base currency. It is absent otherwise. The chart and table carry the same figures;
the data table is collapsed behind **Show as table** with JavaScript and stays visible without
JavaScript. Period rows link to the month page or the focused Year overview.

## Portfolio value over time

The portfolio section shows the portfolio value, invested capital and gain at the end of each
month or year in the report range. It uses the same valuation series as the Investments
Performance page and the Home portfolio widget; no portfolio math is repeated.

The section appears only when the workspace has investment transactions and the selected currency
is the workspace base currency. It is absent otherwise. The chart and table carry the same
figures; the data table is collapsed behind **Show as table** with JavaScript and stays visible
without JavaScript. Period rows link to the month page or the focused Year overview.

## Limits

Reports read the ledger, the derived net-worth history and the portfolio valuation series. There
is no report-table CSV export; CSV export of ledger entries lives on the
[History](transactions.md) page.
