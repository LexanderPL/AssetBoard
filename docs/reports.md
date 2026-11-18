# Reports

Reports (`/reports`) show how much came in and went out over a range of years, month by month or
year by year. Every member of a workspace, viewers included, can open the page: it only reads
what they can already see in the ledger.

## Filters

The filters are a plain GET form, so every view is a bookmarkable address and works without
JavaScript. Unknown query parameters are ignored.

| Filter | Values | Default |
| --- | --- | --- |
| From year | `from=YYYY`, 1900–2199 | the current year and the two calendar years before it (3 years) |
| To year | `to=YYYY`, inclusive | the current year |
| Granularity | `step=month` or `step=year` | monthly up to a 3-year range, yearly beyond |
| Currency | `currency=XXX` | the workspace's base currency |

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

The savings-rate chart shows the share of earnings that regular spending did not use up:

`rate = (earned − spent) / earned`

A yearly rate is computed from the year's summed amounts, never as an average of monthly rates.
Each rate is rounded once, half away from zero: the page keeps it as integer basis points and as
tenths of a percent for display. The total row uses the exact sums for the whole range.

A period with no earnings has no rate: the line has a gap, the table shows “—” and screen readers
hear “Not available: nothing earned”. Rates below zero are drawn below the zero line.

With monthly granularity and at least 12 months in the range, a second line shows the **12-month
rolling rate**. It is computed from the same integers over the last twelve months, not by
averaging monthly rates, and starts at the 12th month of the range.

The rate counts money moved to savings and money merely left over alike, because both are part
of what regular spending did not use up. The Month page's “savings rate” KPI is narrower: it is
`saved / earned`, the share that went to savings and investments. See [Months](months.md).

## Limits

Phase A reports read only the ledger: net worth and portfolio history come in later phases. The
page does not offer a CSV download in this version; CSV export of ledger entries lives on the
[History](transactions.md) page and the report CSV export is part of a later report feature.
