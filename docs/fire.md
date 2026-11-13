# FIRE planner

The FIRE planner stores plans for financial independence and early retirement. F8.1 covers the
scenarios and their settings; the year-by-year projection, charts and linked inputs arrive in
later updates. Until then the page stores and validates each plan.

## Scenarios

**FIRE projection → New scenario** creates a plan. Every field can be changed later except the
start year and currency, which are fixed when the scenario is created. **Edit scenario** saves
the changes. **Duplicate** copies the assumptions, contribution overrides and events under a
free `(copy)` name. There is no hard delete: **Archive** makes a scenario read-only and keeps
its inputs; **Restore** brings it back.

Active scenario names are unique ignoring case. Archiving a scenario lets its name be reused.
Exactly one active scenario is the default while an active scenario exists: the first one
created becomes the default, archiving the default promotes the next active scenario, and
restoring a scenario into an empty list makes it the default. **Make default** moves the mark.
Archived scenarios stay visible under **Archived scenarios**.

## Plan fields

- **Start year** is fixed when the scenario is created (the workspace's current year) and shown
  read-only. It keeps a saved plan reproducible and turns plan years into ages.
- **Birth year** is required. It is prefilled from your profile, but you can change it for one
  scenario without changing the profile. If it is missing, the form links to your profile.
- **Horizon age** is the last age in the projection. It defaults from your profile's life
  expectancy (90 when the profile has none).
- **Income stops at age** ends earned income; from then on the plan spends from the portfolio.
  It cannot be before your age at the start. A stop age after the horizon is allowed and marked
  with a warning.

## Rates

Rates are entered as percentages and stored as basis points. They may be negative where that
makes sense (a negative real return). The hard bounds are −20% to 50% for inflation, real return
and income growth, and 0.5% to 20% for the withdrawal rate.

The ordinary ranges are 0–10% inflation, −2–8% real return, 0–10% income growth and 2–6%
withdrawal rate. A rate outside its ordinary range needs the **unusual rates** confirmation.
When editing, only a rate you actually changed asks for it again.

The starting values (2.5% inflation, 5% real return, 3% income growth and 4% withdrawal rate)
are illustrative examples, not recommendations.

## Starting amounts

Annual income, annual expenses and the starting portfolio are entered manually in the plan
currency and in today's money. The currency is snapshotted from the workspace base currency
when the scenario is created. Amounts cannot be negative.

The input modes for linking these values to the app's own data (trailing 12 months of
transactions, the investable portfolio) are stored with the scenario but are not editable yet:
F8.1 accepts manual values only.

## Contribution overrides

A contribution override is the **net saving** of one year: a positive amount is money added to
the portfolio, a negative amount is money taken out. A year without a row uses income minus
expenses. Overrides apply from the year after the start through the horizon year.

Add rows one by one with **Add a contribution row**, or paste a column and press **Fill rows
from the pasted column**. Pasting supports:

- **bare amounts**, one per line: the first amount fills the year after the start, the next the
  following year, and so on; a blank line skips a year; and
- **year and amount pairs**, such as `2029, 15000` or a year and an amount separated by a tab,
  in any order.

The browser only fills the rows; the server parses every amount in your interface language.
Pasting replaces the rows currently shown. The form refuses years outside the plan window
instead of silently dropping them. A scenario can hold at most 120 contribution rows, and each
year can appear once.

## Events

An event is a one-off change in a plan year:

- **One-off cash flow** — added to net worth in that year (positive inflow, negative outflow).
- **Annual expense change** — changes the yearly expense series from that year onward
  (negative reduces expenses, for example when a loan is repaid).
- **Annual income change** — changes the yearly income series from that year onward (positive
  increases income, negative removes it).

Event amounts are signed in the same way as contribution overrides. Events may start in the
start year and run through the horizon year. A scenario can hold at most 200 events.

## Validation

The form refuses and explains:

- a missing or impossible birth year;
- a horizon that is not older than your age at the start;
- an income stop age before your age at the start;
- rates outside the hard bounds, or outside the ordinary ranges without the confirmation;
- contribution years before the year after the start or after the horizon;
- event years before the start or after the horizon;
- duplicate contribution years, a blank event description, or too many rows.

Nothing is saved when validation fails; the rows you typed stay on the form.

## Privacy and scope

The FIRE planner is part of the workspace, so every member can read its scenarios and editors
and owners can change them. Projections are illustrative and not financial advice.
