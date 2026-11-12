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

## Interest

- **Fixed**: enter the rate; add later changes on the loan page. Each rate is effective from its
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

## The loan page

The loan page turns the terms into an overview and a monthly schedule:

- **Overview**: remaining principal today, the current rate (with the reference rate and margin
  for a variable loan), the next payment and its principal/interest/costs/overpayment breakdown,
  the percentage repaid, the payoff date, the payments left, and remaining/lifetime interest and
  costs. The percentage is a native progress bar with the same value in text.
- **Schedule**: 24 monthly rows per page, starting on the page with today's or the next due
  payment. The row for today is marked **Today**; when today is not a payment date, the next one
  is marked **Next**. Rows before today are marked **Projected** until actual payments are
  recorded. On a phone the table scrolls sideways inside its own container, with a visible hint;
  all columns stay available.
- **CSV**: **Download CSV** exports every row as machine-readable data: ISO dates, plain decimal
  rates and amounts, comma separated, CRLF line endings, UTF-8 without a BOM. It is available to
  viewers too.
- **Charts**: **Balance over time** compares your configured plan with the same loan without
  overpayments, and **Monthly payments** stacks principal, interest, costs and overpayments.
  Dashed vertical lines mark rate changes, including the new rate in the tooltip. Each chart has
  a **Plotted data** table for keyboard and screen-reader users; without JavaScript the table is
  shown instead of the chart.

If the stored terms cannot produce a schedule (for example a missing rate), the page explains
the problem and hides the schedule, charts and CSV until the terms are corrected.

## Installment plans

**Loans → Installments** tracks simple fixed installments such as a 0% purchase, with no
interest calculation. Create a plan with a name, monthly installment, remaining count, payment
day and expense category. The plan computes the next due date on or after today (clamping the
payment day to the end of a shorter month) and the end date after the remaining payments.

- The list shows active plans with monthly installment, remaining count, total remaining
  (installment × remaining count), next due, end date and category. Closed plans stay in a
  separate section.
- The plan detail shows the same totals plus its net-worth liability and generated Upcoming
  occurrences. **Create net-worth item** links one liability item in the **Installments**
  category; it resolves to installment × remaining count.
- **Generate occurrences** creates or refreshes one monthly expense scheduled item linked to
  the plan. The expected amount is the fixed installment. Posting an occurrence from Upcoming
  reduces the remaining count by one; posted occurrences are never counted twice. At zero the
  plan closes, its loan is archived and pending future occurrences are removed, while posted and
  past rows remain.
- **Archive plan** closes a plan manually and removes pending future occurrences; **Restore
  plan** reopens it (unless the remaining count is zero). Editing the installment, remaining
  count, payment day or category updates the plan, its end date and generated occurrences.
- Installment plans appear in the Loans installments tab, not in the ordinary Loans list. They
  do not create `LoanPayment` rows, because this is a liability countdown rather than an
  interest-bearing loan payment.

## Actual payments and balance checkpoints

The loan page keeps the terms and the projected schedule; **Payments** records what actually
happened.

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

**Net worth & property** on the loan page connects the loan to the rest of the app:

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

**Scenarios** on the loan page compares the same loan under several plans without changing the
stored terms:

- **Baseline** is the loan without any overpayments.
- **Current plan** is the configured regular overpayment and one-off overpayments.
- **Saved scenarios** are named variations that are always included.
- **What-if rows** are up to three temporary variations for one comparison.

Each variation can change the regular overpayment (same as the loan, none, a fixed extra or a
target total), the strategy (shorten the term or lower the installment), shift the annual rate by
a signed number of percentage points from a date, and add one one-off lump sum on a date. A rate
shift changes every rate from that date and inserts a rate-change row when the date falls between
two existing changes. A lump sum is added to the loan's existing one-off overpayments. Empty
what-if rows are ignored; the rate-shift date and the lump-sum date must be entered together with
their values.

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

**Cost components** are charged every month while active: a percentage of the opening balance
or a fixed amount. Their dates are inclusive on both ends, and several active components may
overlap; all of them are added to the installment, and the Upcoming amounts derived from the
schedule follow the change.

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

Reference rates are workspace-scoped series such as WIBOR or EURIBOR. Editors and owners create
a series and add a value with its effective date; viewers can read them. Values that start in the
future can be corrected or deleted; past values are historical. A reference rate used by a loan
cannot be deleted.

## Archiving

**Archive loan** hides a loan from the active list and makes it read-only. **Restore loan** brings
it back. Loans are never hard-deleted.

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
