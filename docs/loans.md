# Loans

Loans are optional. The app stores the terms you enter and keeps every amount in the workspace
base currency; it never converts a loan into another currency. Changing the base currency is
locked once a loan exists.

## Adding a loan

**Loans → Add loan** asks how much you know:

- **I know the original terms** — original principal, origination date, term in months, first
  payment date and payment day.
- **I only know my current balance** — current balance, balance date, remaining months, next
  payment date, payment day and optionally the original principal. The app creates one starting
  balance checkpoint.

The **payment day** is the nominal monthly day. The first/next payment date is the first actual
due date; it must fall on the payment day, or on the last day when the month is shorter. Later
months use the payment day (31 January → 28 February, then 31 March). In the add wizard, leaving
the payment day empty uses the first payment date's day; enter 31 explicitly for month-end
semantics.

The **loan type** is annuity (roughly equal installments) or equal principal (the principal part
is equal and the installment falls over time). You can change the type later. The **rate type**
is fixed at creation: to switch between fixed and variable, archive the loan and add a new one.

The last step shows a read-only **Review**: the name, entry path, original principal or current
balance, term, first payment date, rate configuration, regular overpayment, cost components and
the **calculated first installment**. A current-balance loan calculates from today's balance. An
installment override replaces the calculated amount and is marked **override**. If the amount
cannot be calculated, the wizard shows the same validation message as saving would.

**Save loan** stores the loan and opens its Settings page. **Save and add another** stores it
and returns to a fresh Add loan form with a confirmation, which is useful when entering several
loans. A validation error marks the matching input with its message; errors that do not belong
to one field stay in the alert above the form.

## Interest

- **Fixed**: enter the rate; add later changes in **Settings → Rates**. Each rate is effective from its
  date and the latest rate at or before a payment applies.
- **Variable**: choose a reference rate and a margin. Values are maintained on
  **Loans → Reference rates**; the latest value at or before a payment plus the margin applies.

A rate above **25.00%** needs an explicit confirmation. Rates cannot exceed **100%**, and a
margin cannot be negative.

**Interest calculation** is monthly or actual/365. Monthly uses one twelfth of the annual rate
in every period. Actual/365 uses the real number of calendar days between payment dates. The
choice is on **Edit terms**; existing loans start with monthly.

## How the schedule is calculated

The schedule is a monthly table from the loan's start (or from the latest balance checkpoint).
Each row has an opening balance, interest, principal, costs, overpayments and a closing balance.

- **Payment dates** use the first payment date, then the nominal payment day in each later month.
  A shorter month uses its last day, so 31 January is followed by 28 or 29 February and then
  31 March.
- **Interest** is the opening balance times the annual rate. The default monthly method divides
  by 12. The actual/365 method uses the real number of days since the previous row. Each
  component is rounded half up to the currency's minor unit.
- **Rate changes** apply from their effective date. An annuity payment is recalculated when the
  rate changes. Equal-principal loans keep the same regular principal each month.
- **Costs** are charged while their dates are active, including both the first and last date.
  Several active costs are added together. A percentage cost is a monthly percentage of the
  opening balance.
- **Regular overpayments** are none, a fixed extra each month, or a target total monthly outflow.
  With the default "shorten the term" strategy the regular payment stays the same and the loan
  ends sooner; "lower the installment" recalculates the payment after an overpayment. Equal
  principal always shortens the term. One-off overpayments are applied at the next payment date.
- **The final payment** clears the remaining balance exactly, including a small rounding
  residual. The schedule stops at zero and never shows a negative balance.

A balance checkpoint starts the schedule from the balance you owe on that date; earlier rows are
not invented. When a checkpoint is used, the remaining months must be entered as the loan's term
or the schedule will be too long.

## The loan list

**Loans** is a small portfolio view of the loans that are not archived.

- The portfolio panel above the table totals the remaining principal, the current monthly
  installments, the amount due in the next 30 days and the balance-weighted average current
  rate. "Due in the next 30 days" counts projected installments after today up to and including
  the same day 30 days later. Archived loans are not in the totals.
- A loan whose stored terms cannot produce a schedule (for example a missing rate) is left out
  of the totals instead of breaking the page. The panel says how many loans were skipped and the
  loan's figures show a dash.
- The table has one set of columns: **Name**, **Type**, **Remaining**, **Progress**, **Rate**,
  **Next payment** and **Status**. The name opens the loan. **Remaining** is the projected
  principal today, **Progress** is the percentage repaid with a small progress bar, **Rate** is
  the current annual rate, and **Next payment** shows the next due date and its projected
  installment. A paid-off loan has no next payment and a 0.00 monthly installment.
- **Status** is **Active**, **Paid off** (the projected remaining principal is zero) or
  **Archived**. Archived loans stay in the separate **Archived loans** section with the same
  columns and status badge.

## The loan page

A loan has its own section navigation: **Overview**, **Schedule**, **Payments**, **Scenarios**
and **Settings**. The global **Loans / Installments / Reference rates** tabs stay visible above
it. The overview is read-only insight; changing the terms, rate, costs, overpayments, links or
archive state happens in Settings. The schedule and charts live on their own Schedule page.

- **Overview**: one header panel with an **Active**, **Paid off** or **Archived** status badge,
  a large repaid-vs-remaining principal bar (paid amount, percentage, original principal and
  payoff date), the current rate (with the reference rate and margin for a variable loan), the
  next payment and its principal/interest/costs/overpayment breakdown, the percentage repaid,
  the payoff date, the payments left, and remaining/lifetime interest and costs. The bar is
  inline SVG; its text labels carry the same information so colour is not the only cue.
- **Plan vs actual**: the last recorded actual payment, the latest balance checkpoint and its
  variance, and a plain **Ahead**, **Behind** or **On track** line. Without actual payments or
  checkpoints the panel says that no actual payments are recorded yet.
- **Property and loan-to-value (LTV)**: a compact, read-only card with the linked property value, remaining
  principal, LTV and equity at the chosen date. **Manage in Settings** opens the property and
  liability controls; the real-estate-only explanation lives there.
- **Record next payment**: the overview's primary action opens the payment dialog on the
  Payments page with the next schedule row's due date and total filled in. The Payments page
  keeps its own **Record payment** button.
- **Schedule page**: 24 monthly rows per page, starting on the page with today's or the next due
  payment. Rows are grouped by calendar year: a year header row appears before the first row of
  each year, including when a page starts in the middle of a year. The **Actual** / **Planned**
  toggle changes the component columns: **Actual** shows a recorded payment's components on its
  due date (the default), while **Planned** shows the projected components for every row. The
  toggle is kept when you move between pages. The row for today is marked **Today**; when today
  is not a payment date, the next one is marked **Next**. Rows before today without a payment
  are marked **Projected**; a row with a recorded payment is marked **Actual** (or **Planned**
  in the planned view). On a phone each row is a card with the date, total, principal, interest,
  costs, overpayment and closing balance, with no sideways page scrolling. On a wider screen the
  table scrolls inside its own container and the row number and due date columns stay pinned
  while the other columns scroll.
- **CSV**: **Download CSV** exports every row as machine-readable data: ISO dates, plain decimal
  rates and amounts, comma separated, CRLF line endings, UTF-8 without a BOM. It is available to
  viewers too.
- **Charts**: **Balance over time** compares your configured plan with the same loan without
  overpayments, and **Monthly payments** stacks principal, interest, costs and overpayments.
  Dashed vertical lines mark rate changes, including the new rate in the tooltip. Each chart has
  a **Plotted data** table for keyboard and screen-reader users; without JavaScript the table is
  shown instead of the chart.

**Settings** is one card per concern, in this order: **Terms**, **Property & net worth**,
**Rates**, **Costs**, **Overpayments**, **Upcoming & auto-record** and **Archive**. The Terms
card summarises the current key terms; its **Edit terms** button opens the separate edit page,
which returns to Settings when saved or cancelled. Viewers see every card read-only; only
editors and owners see the edit, create, delete and archive controls. Payment and checkpoint
dialogs stay on the Payments page. An invalid rate, cost, overpayment or property choice marks
the matching input and shows its message there; general errors stay in the alert above the
cards. The wide rate, cost and overpayment lists become card rows on phones and do not force
sideways page scrolling.

If the stored terms cannot produce a schedule (for example a missing rate), the overview explains
the problem and hides the schedule, charts and CSV until the terms are corrected.

## Installment plans

**Loans → Installments** tracks simple fixed installments such as a 0% purchase, with no
interest calculation. Create a plan with a name, monthly installment, remaining count, payment
day and expense category. The plan computes the next due date on or after today (clamping the
payment day to the end of a shorter month) and the end date after the remaining payments.

- The list shows active plans with monthly installment, remaining count, progress, total
  remaining (installment × remaining count), next due, end date and category. The plan name is
  the link to its detail; an **Open**/**Closed** badge names the state. Closed plans stay in a
  separate section with the same columns and progress.
- **Progress** is the posted installments out of the original count. The original count is the
  remaining count plus the plan occurrences already posted, so it stays correct without a
  separate stored total. The list uses a compact bar; the detail shows paid and remaining
  installments with the percentage and an inline bar. The text labels repeat the values, so
  colour is not the only cue.
- The plan detail shows the same totals plus its net-worth liability and generated Upcoming
  occurrences. **Create net-worth item** links one liability item in the **Installments**
  category; it resolves to installment × remaining count.
- **Generate occurrences** creates or refreshes one monthly expense scheduled item linked to
  the plan. The expected amount is the fixed installment. Posting an occurrence from Upcoming
  reduces the remaining count by one; posted occurrences are never counted twice. At zero the
  plan closes, its loan is archived and pending future occurrences are removed, while posted and
  past rows remain.
- **Post next installment** is available on the plan detail when the earliest pending occurrence
  is due today or earlier. It posts that occurrence through the normal Upcoming posting service,
  which records the expense transaction, reduces the count and refreshes the schedule. The
  button is hidden when nothing is due. A posted occurrence is not decremented again, and the
  next click posts the next due occurrence (or says that nothing is due).
- **Archive plan** closes a plan manually and removes pending future occurrences; **Restore
  plan** reopens it (unless the remaining count is zero). Editing the installment, remaining
  count, payment day or category updates the plan, its end date and generated occurrences.
- Installment plans appear in the Loans installments tab, not in the ordinary Loans list. They
  do not create `LoanPayment` rows, because this is a liability countdown rather than an
  interest-bearing loan payment.

## Actual payments and balance checkpoints

**Payments & balance** keeps the projected schedule and records what actually happened. It has
two sections: **Actual payments** (what you paid) and **Balance checkpoints** (what the bank
reported). An actual payment is split against the projected row; a checkpoint re-anchors the
schedule from the real balance, so together they show whether the loan is ahead of or behind the
plan. On a phone each row is a card with every amount and action; on a wider screen both lists
are tables that scroll inside their own container if needed.

- **Recording a payment**: choose installment or overpayment and enter the date and total. An
  installment total is split against the projected row on that date (or the first row after it):
  an equal total keeps the row's interest/costs/principal/overpayment, a larger total puts the
  excess into overpayment, and a smaller total fills interest, costs, principal and overpayment
  in that order. An overpayment payment goes entirely to the overpayment component. Open **Enter
  an explicit breakdown instead** to enter principal, interest, costs and overpayment yourself;
  they must add up to the total.
- **Ledger link**: optionally link a recent expense in the loan currency. It must not be deleted,
  must be an expense, and its amount must equal the payment total.
- **Actual rows**: on the loan schedule, a date with a recorded installment shows the actual
  components and an **Actual** badge. Dates before today without a payment stay **Projected**;
  future rows are unmarked. The closing column remains the projection from the terms.
- **Payment variance**: every actual installment shows its total and principal variance against
  the projected row on the same date, or the first later row (the same rule as the split). The
  variance is actual minus projected, so a positive value means you paid more than projected; a
  short label adds **More than projected**, **Less than projected** or **Matches projection**.
  A payment with no matching schedule row (for example an overpayment-only payment) says so, and
  a schedule that cannot be built shows **Variance unavailable** with the reason for screen
  readers.
- **Editing and deleting**: past payments can be edited or deleted. Changing the total (or the
  kind) without an explicit breakdown re-splits the payment against the projected row. An
  overpayment-only payment appears in the payment list, not as an invented monthly row.

**Balance checkpoints** are bank-statement balances. Add a date (not in the future, and not
before the loan starts) and the remaining principal. A checkpoint re-anchors the schedule from
that date. The list shows the projected balance from the previous checkpoint (or the loan's
origination) and the signed variance: a positive variance means the real balance is higher than
projected, a negative one means it is lower. The starting checkpoint of a loan entered from its
current balance has no earlier anchor, so it shows no variance and cannot be deleted. A
checkpoint that cannot be projected shows the reason instead of a value.

## Net worth, property and Upcoming

**Property & net worth** in Settings connects the loan to the rest of the app (the overview
shows the compact read-only property card):

- **Net-worth liability**: **Create net-worth item** adds one linked liability item in the
  **Mortgage** category. Every net-worth date resolves the remaining principal from the loan
  terms, checkpoints and recorded actual installment payments. Creating it again does not
  duplicate it.
- **Property and LTV**: choose an active real-estate asset item in the same currency. The page
  shows the property value on the chosen date, the remaining principal, **LTV** (remaining
  principal divided by the value) and equity (value minus remaining principal). Change the date
  and press **Show** to recalculate. If no property is linked, the value is missing, the item is
  archived, the currencies differ, or the balance is unavailable, the page says why instead of
  showing a misleading number. A zero property value leaves LTV unavailable but still shows
  equity against the remaining principal.
- **Recurring installments**: choose an expense category and **Generate occurrences** to create
  one monthly scheduled item linked to the loan. The expected amount of each pending occurrence
  comes from the projected installment for its due date, so a rate, cost, term, overpayment or
  checkpoint change refreshes pending future amounts. Posted, skipped and manually overridden
  occurrences are left alone. **Refresh occurrences** updates the same item after changing its
  category; **Disable generation** archives it and removes only pending future occurrences. A
  link opens Upcoming.
- **Automatic payment recording**: when this setting is on and a linked Upcoming occurrence is
  posted, the app also records an actual loan payment with source **upcoming**, its transaction
  link and the paid amount split against the projected row. The setting is off by default.

## What-if scenarios

**Scenarios** in the loan navigation compares the same loan under several plans without changing
the stored terms:

- **Baseline** is the loan without any overpayments.
- **Current plan** is the configured regular overpayment and one-off overpayments.
- **Saved scenarios** are named variations that are always included.
- **What-if cards** are up to three temporary variations for one comparison. Each card holds the
  name, overpayment fields, rate-shift fields and one-off fields, and has its own **Clear card**
  action. A **Clear rows** link resets the whole form.

Each card has three **presets** that fill its fields without submitting anything:

- **Add 10% of the installment** puts a fixed extra equal to 10% of the loan's first installment
  into the overpayment fields.
- **Raise the rate by 1 pp** fills a +1 percentage-point shift from today.
- **One-off 1,000** fills a one-off overpayment of 1,000 in the loan currency, dated today.

The presets only write form values; adjust them and press **Compare** as usual. They are
available to viewers too, who can compare temporary variations but cannot save or delete
scenarios.

Each variation can change the regular overpayment (same as the loan, none, a fixed extra or a
target total), the strategy (shorten the term or lower the installment), shift the annual rate by
a signed number of percentage points from a date, and add one one-off lump sum on a date. A rate
shift changes every rate from that date and inserts a rate-change row when the date falls between
two existing changes. A lump sum is added to the loan's existing one-off overpayments. Empty
cards are ignored; the rate-shift date and the lump-sum date must be entered together with their
values.

Each **saved scenario** card shows its name and parameters plus the change against the **current
plan**: the interest saved (or added) and how many months sooner (or later) the loan ends. A
favourable change is green, an unfavourable one red, and the text says which it is, so colour is
not the only cue. The full comparison table and the chart below still compare every row with the
baseline.

The comparison table shows the payoff date, months saved against the baseline, total interest,
interest saved against the baseline and total paid. Positive savings are shown in green and
negative ones in red; they are not investment advice. A variation that cannot be calculated
(for example a shift below 0%) shows an error in its row instead of breaking the page. If the
loan's own terms cannot produce a schedule, the page explains why and hides the comparison and
chart.

The chart overlays the closing balance of the baseline, the current plan and every saved or
temporary variation. Dashed vertical lines mark rate changes, and the **Plotted data** table is
available without JavaScript.

Give a what-if a name to **save** it; saving the same name again updates that scenario. Saved
scenarios can be deleted from the page. Viewers can compare scenarios and see saved ones, but
only editors and owners can save or delete them.

## Costs and overpayments

**Cost components** can be added in the last step of the loan wizard (up to three) or later on
Settings → Costs. They are charged every month while active: a percentage of the opening balance or
a fixed amount. Their dates are inclusive on both ends, and several active components may
overlap; all of them are added to the installment, and the Upcoming amounts derived from the
schedule follow the change. A wizard row with an empty **Applies from** starts with the loan.

Costs are configuration, not versioned history: editing a component changes every projected row
in its date range. To keep an old premium visible and change it from a date, set the old
component's **Applies until** to the day before the change and add a new component from the
change date. To stop a cost, set that end date (or delete the component if no projection needs
to keep it).

**Regular overpayment** is none, a fixed extra each month, or a target total monthly outflow.
The target must not be below the installment and may shorten the term (the default) or lower the
installment. **One-off overpayments** are planned rows; actual payments are recorded separately.

An **installment override** is for annuity loans where the bank's amount differs from the
calculated installment.

## Reference rates

Reference rates are workspace-scoped series such as WIBOR or EURIBOR. The page is a compact
series table: each row shows the rate name and notes, its current value, a small value-history
sparkline, and how many loans use it. Open a row to see the individual values with their dates,
the **Used by** loan links, and the rate and value edit/delete controls. Values that start in the
future can be corrected or deleted; past values are historical. A reference rate used by a loan
cannot be deleted.

Editors and owners create a series and add a value with its effective date; viewers can read
them but see no edit controls. The **New reference rate** action stays the page's primary
action, and the add/edit dialogs are unchanged.

## Archiving

**Settings → Archive** hides a loan from the active list and makes it read-only. **Restore loan**
brings it back. Loans are never hard-deleted.

## Backup and restore

Loans are part of the workspace archive (format 1.5): reference rates and values, terms, rate
changes, cost components, planned overpayments, actual payments, balance checkpoints, scenarios
and installment plans. Generated schedules are not stored in the archive; a restore rebuilds
them from the restored terms, so the schedule is identical. Property and ledger-payment links
are mapped to the restored net-worth items and transactions. Loan and reference-rate ids are
preserved when the destination does not already use them, which keeps linked net-worth items
and installment plans pointing at the same loans.

A merge of a full archive does not apply loan files; only a restore does. A lone
`reference_rate_values.csv` can be merged, matching the series by case-folded name and treating
an existing value for the same date as a duplicate or a conflict.

The app only stores and displays terms; it does not give financial advice. The installment
figures are estimates until the bank's actual payments are recorded.
