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

## Costs and overpayments

**Cost components** are charged every month while active: a percentage of the opening balance
or a fixed amount. Their dates are inclusive on both ends, and several active components may
overlap; all of them are added. The schedule engine in a later release uses them.

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

The app only stores and displays terms; it does not give financial advice. The installment
figures are estimates until the bank's actual payments are recorded.
