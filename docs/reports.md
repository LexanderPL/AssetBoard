# Reports

Reports (`/reports`) show how much came in and went out over a range of years, month by month or
year by year, and where the spending went by category. Every member of a workspace, viewers
included, can open the page: it only reads what they can already see in the ledger.

## Filters

The filters are a plain GET form, so every view is a bookmarkable address and works without
JavaScript. Unknown query parameters are ignored.

| Filter | Values | Default |
| --- | --- | --- |
| From year | `from=YYYY`, 1900–2199 | the current year and the two calendar years before it (3 years) |
| To year | `to=YYYY`, inclusive | the current year |
| Granularity | `step=month` or `step=year` | monthly up to a 3-year range, yearly beyond |
| Currency | `currency=XXX` | the workspace's base currency |
| Categories | `category=<id>`, repeatable | empty: the 8 largest categories and Other |
| Include saved / invested | `savings=1` | off |

The **Categories** and **Include saved / invested** filters apply to the expenses-by-category
section; the other sections always use the whole ledger.

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

## Limits

Phase A reports read only the ledger: net worth and portfolio history come in later phases. The
page does not offer a CSV download in this version; CSV export of ledger entries lives on the
[History](transactions.md) page and the report CSV export is part of a later report feature.
