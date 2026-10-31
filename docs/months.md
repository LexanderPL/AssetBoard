# Months

Your money is tracked month by month. The main navigation starts with four pages:

- **Home** (`/`): how the current month is going. The flow bar described below, a donut of
  the month's expenses by category next to a donut of its income by category, and up to ten
  upcoming recurring items (overdue ones, highlighted, and those due within the Upcoming
  window, 30 days unless the owner changes it in Workspace settings; see
  [Recurring items](recurring.md)). Editors and owners can mark an item paid or received, or
  skip it, right there; **All upcoming items** opens the Upcoming payments page.
- **Month** (`/m`, `/m/2026-10`): the summary of one month, with its planned values and the
  expense donut (this page).
- **Transactions** (`/transactions`, `/transactions/2026-10`): where you enter, edit and
  delete a month's expenses and income (see [Transactions](transactions.md)).
- **History** (`/history`): every entry of every month, with search, filters, CSV export and
  changing many categories at once (see [Transactions](transactions.md#history)).

Open "Month" in the main navigation: it shows the current month's summary. "Transactions" in
the main navigation opens the same month's Transactions page. Every month has its own address, such as `/m/2026-10` for October 2026, so you can bookmark it. The address names
only the month, not the workspace: whoever opens the link sees that month in the workspace they
currently have selected. A member who belongs only to your workspace sees your month; a member
of several workspaces may need to switch first.

"The current month" is the month of today's date in the workspace's time zone (Workspace
settings → Time zone). Just after midnight on the 1st, the new month begins at that time in that
zone, not at midnight UTC.

## Moving between months

- **‹ Previous** and **Next ›** go one month back or forward, across years as well.
- **Today** goes back to the current month.
- The **month and year** button opens a picker: use **‹** and **›** to change the year, then
  choose one of the twelve months. It needs no JavaScript, and the month opens as soon as you
  choose it.

Every month from January 1900 to December 2199 can be opened, including months with no data yet
and future months, which you can use for planning. The Transactions page has the same
navigation.

## The month summary

The **Summary** block at the top of the month page shows how the month is going:

- A **flow bar** of what you **earned**, split into what you **spent** (red), **saved /
  invested** (blue) and what is **left** (green; earned minus spent minus saved). When spending
  and saving are more than you earned, the bar grows to their total, the part beyond your
  earnings is hatched red, a line marks what you earned and "Left" turns red. Below it, "Saved /
  invested 26.7% of income" is the savings rate: what you saved or invested, as a share of what
  you earned (money merely left over is "Left", not savings); it is not shown when nothing was
  earned. Money moved between your own accounts (transfer categories)
  counts nowhere.
- Two bars compare what you spent and earned with what you planned; the dark line marks the
  plan.
- A category row turns red when you spent more than planned on it, and green when an income
  category brought more than planned.
- A table with every category in groups (Expenses, Saved / invested, Income, Withdrawn from
  savings): **Plan**, **Actual** and **Difference**, and a total per group. A positive
  difference is always good news: spent less, earned more, saved more or withdrew less than
  planned. A category with entries but no plan shows a plan of 0.00.
- **Pending** amounts come from your [recurring items and income sources](recurring.md): what is
  still due this month and not yet confirmed. They are part of Actual, shown as a lighter
  second line under the confirmed amount, for example "+ €1,200.00 pending" (only when something
  is pending). Hover it to see the month "If everything pending arrives" (screen readers read
  the same). Confirming an item on the Transactions page moves its amount from pending into the
  confirmed amount. The difference and the red or green row colour count confirmed amounts
  only; when the pending amount would take a category over its plan, a softer line under it
  says "Over plan if everything pending arrives".
- In the bars, the solid part is confirmed and the **striped** part after it is pending; the
  text above each bar adds "+ … pending", and "(over plan if everything pending arrives)" when
  only the pending part goes past the plan.
- The flow bar is titled **Spent vs earned**. With something pending it draws each part
  confirmed (solid) and then pending (striped); the legend gives each figure with "+ …
  pending", and Left as it would be if everything pending arrives. Nothing is confirmed in a
  future month, so there the bar shows what you expect to spend and earn, and its title reads
  **Expected spent vs expected earned**. (Home still shows the older "Forecast spent … ·
  forecast earned …" line and second bar.)
- **Show pending** hides or shows every pending part: the "+ … pending" lines, the striped
  parts of the bars and the pending flow bar (the flow bar then shows confirmed amounts only).
  This browser remembers the choice.
- **Largest first** sorts each group by the actual amount; **Your order** uses the order of
  your categories. This browser remembers the choice.

The summary updates as soon as you change a planned value, and shows every entry made on the
Transactions page the next time you open it.

Below the summary, **Expenses by category** is a donut of what you spent per category (regular
expense categories only: saving and investing are in the flow bar, transfers count nowhere).
Each slice has the category's colour; categories with nothing spent have no slice, and beyond
eight categories the smallest are grouped as "Other". A table with the same amounts and shares
sits beside it for screen readers, and is shown in place of the chart when JavaScript is off.
The chart follows the light or dark theme. Home shows the same donut and one for income.

Click a category's name in the summary to open the Transactions page with only that category's
entries (see [Transactions](transactions.md#showing-one-category)).

## Planning a month

Type the amount you plan for a category in its **Planned** field in the summary:

- **Enter** saves and moves to the next category; leaving the field (Tab or a click elsewhere)
  saves too. Amounts are typed as for entries (`550`, `1 234,56`, `12,5`).
- **0** or an empty field removes the planned value. **Esc** puts back the saved value.
- Transfers are not part of the plan. A planned value of a category that was later archived or
  turned into a transfer stays visible (marked, or under "Transfers (not counted)") so you can
  change or remove it, but such categories take no new planned values.

When a month has no planned values yet and the previous month has some, the block offers **Copy
plan from <previous month>**. One click copies them all; it never overwrites a planned value
that already exists, and it leaves out archived and transfer categories (and says how many).
To have this happen by itself, an owner can turn on "Copy the previous month's plan when a month
is opened for the first time" in [Workspace settings](workspace-settings.md): then the current
month or a later one is filled the first time an editor or owner opens it, once. A month you
emptied on purpose stays empty, and past months always use the button.

Viewers see the planned values but cannot change them. Changes are recorded in the workspace
audit log under "Planned values".

## Closing a month

When a month is done, close it so nothing in it changes by accident:

- **Close month** is offered to editors and owners, for the current month and past months.
  Future months stay open for planning and cannot be closed. You are asked to confirm first.
- A closed month's entries and planned values are **read-only for everyone, owners included**.
  The page shows a "Closed" badge and a banner that says when the month was closed and by whom.
  Every member sees it, viewers included.
- **Reopen month** is offered to owners only. After reopening, the month's entries and planned
  values can change again.
- If someone else closed or reopened the month in the meantime (another tab, a double click),
  the page says the month was already closed, or is already open, and nothing changes.

Closing and reopening are recorded in the workspace audit log under "Closing months".
