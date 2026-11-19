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
| From year | `from=YYYY`, 1900–2199 | the current year and the two calendar years before it (3 years) |
| To year | `to=YYYY`, inclusive | the current year |
| Granularity | `step=month` or `step=year` | monthly up to a 3-year range, yearly beyond |
| Currency | `currency=XXX` | the workspace's base currency |
| Categories | `category=<id>`, repeatable | empty: all categories in the year overview, the 8 largest (and Other) in the category charts |
| Include saved / invested | `savings=1` | off |
| Report year | `year=YYYY` | the report's end year, or the current year when the range ends later |
| Category trends window | `trend=3` or `trend=12` | 3 months |
| Year-over-year category | `yoy_category=<id>` | empty: all categories |

The **Categories** and **Include saved / invested** filters apply to the expenses-by-category
section, the year overview and category trends. **Year-over-year** has its own single category
selector; the other sections always use the whole ledger. An unknown or malformed `year` or
`yoy_category` is ignored and the default is used, so an old bookmark still opens.

The **Report year** selector chooses the year of the year-overview grid and the year-over-year
comparison. It does not change the range of the first three sections. The **Net worth over time**
and **Portfolio value over time** sections follow **From year**, **To year** and **Granularity**,
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
total row for the range. Each monthly row links to that month's page (`/m/YYYY-MM`). With
JavaScript the table is visually hidden beside the drawn chart; **Show as table** reveals it.
Without JavaScript the table stays visible in place of the chart. An empty range shows an empty
state and a table of zeros.

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

**Download CSV** exports exactly the grid's rows and totals for the same `year`, `category`,
`savings` and `currency` filters. The file is `reports-year-overview.csv`: UTF-8, comma separated,
RFC 4180 quoting, CRLF line ends, ISO month columns (`2026-01`), plain decimal amounts with the
currency's minor digits and no locale grouping. Headers and the total label are stable English
machine labels, not translated UI; category names are guarded against spreadsheet formula
execution. It is a normal authenticated GET, available to every member who can open the report.

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

The year-over-year table compares actual spending in the **Report year** with the year before.
Choose one category in **Year-over-year category**, or leave it empty to compare all categories
together. It shows the current amount, the previous amount, the absolute change and the percent
change. A positive change means more spending; it is shown in the danger colour, while a decrease
is shown as good.

The percent change is `(current - previous) / previous`, rounded once to one decimal place. When
the previous year spent nothing the percentage is undefined: the table shows “—” and screen
readers hear why. When the report year is the current year, both years are compared through the
current month ("so far") rather than a partial year against a full one. Like the year overview,
the comparison follows the category treatment rules and the **Include saved / invested** option;
income, withdrawals and transfers never count.

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
the table is the accessible version and works without JavaScript.

## Portfolio value over time

The portfolio section shows the portfolio value, invested capital and gain at the end of each
month or year in the report range. It uses the same valuation series as the Investments
Performance page and the Home portfolio widget; no portfolio math is repeated.

The section appears only when the workspace has investment transactions and the selected currency
is the workspace base currency. It is absent otherwise. The chart and table carry the same
figures; the table is the accessible version and works without JavaScript.

## Limits

Reports read the ledger, the derived net-worth history and the portfolio valuation series. The
year overview is the only table with a CSV download; CSV export of ledger entries lives on the
[History](transactions.md) page.
