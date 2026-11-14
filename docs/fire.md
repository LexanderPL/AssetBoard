# FIRE planner

The FIRE planner stores plans for financial independence and early retirement and shows their
year-by-year results. A plan's page puts the results first and keeps the complete input form in
a collapsible **Plan inputs** panel. The projection engine and the results page are documented
below.

## Scenarios

**FIRE projection → New scenario** creates a plan. Every field can be changed later except the
start year and currency, which are fixed when the scenario is created. **Edit scenario** opens
the plan's inputs on its results page. **Save changes** in the inputs panel persists the edits;
changing an input before saving only previews the new results. **Duplicate** copies the
assumptions, contribution overrides and events under a free `(copy)` name. There is no hard
delete: **Archive** makes a scenario read-only and keeps its inputs; **Restore** brings it back.

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
- **Income stops at age** ends the yearly income series; from then on the plan spends from the
  portfolio. It cannot be before your age at the start. A stop age after the horizon is allowed
  and marked with a warning.

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

Annual income, annual expenses and the starting portfolio can be entered manually in the plan
currency and in today's money, or read from the app's own data. The currency is snapshotted
from the workspace base currency when the scenario is created. Manual amounts cannot be
negative; a linked mode stores no manual amount. Annual income is all **regular** income:
earnings, dividends and interest, not only salary.

- **Income → Last 12 full months** sums every income transaction with the **regular** treatment
  in the last twelve complete calendar months. Savings withdrawals and transfers are excluded.
  It includes investment dividends and interest because the ledger has no earned/passive flag.
- **Expenses → Last 12 full months** sums every expense transaction with the regular treatment
  in the same window. Savings and transfers are excluded.
- **Starting portfolio → Investable assets** adds the current M6 portfolio look-through for
  every asset class marked **investable** (archived classes included), the portfolio's
  unallocated bucket, and manual net-worth items with **investable** and **include in net
  worth** enabled. Linked net-worth mirrors are skipped to avoid double counting.

The current partial month is never used. The window is always the twelve full months before the
current month; only months with included rows count. If fewer than twelve months have data, the
window total is scaled to a year from the available months, and the linked panel says how many
months were used and shows the window. A gap month is not treated as zero. If a plan starts in
an earlier year, the panel warns that today's data is used; later months cannot be included
until a new scenario is created. Amounts in another currency than the plan block the linked
mode instead of being silently dropped.

### Expense exclusions

The **Expense exclusions** checkbox list leaves regular expense categories out of the linked
trailing expenses, for example for unusual one-off spending. Exclusions are saved per scenario
and have no effect on income. An excluded category's window total is shown separately for
transparency. Income categories, savings and transfer categories cannot be excluded. A category
used by an exclusion cannot be deleted (it is archived instead) or changed to a non-regular or
income category while the scenario references it; archived categories remain valid and are
shown with an **Archived** badge. Excluding every category with data blocks the linked expenses.

## Linked portfolio breakdown

When the starting portfolio is linked, the results page lists every source: its name, amount,
valuation date and a link to the portfolio or net-worth item. The portfolio's as-of date is
today. The panel notes non-investable classes that were left out and the linked net-worth items
that were skipped. A missing price, FX rate, item value or any other missing source is a blocking
message, never a zero. A portfolio whose sources sum to zero is allowed but marked with a
warning. The portfolio currency must still equal the workspace base currency; otherwise the
linked modes are blocked while manual values keep working.

## Loan payoff suggestions

For every active schedule loan in the plan currency, the results page suggests adding its payoff
as an **annual expense change**: “Payoff in <year> frees <amount>/year (today's money).” The
amount is the sum of the next up to twelve contractual payments (interest, principal and costs,
without overpayments), not the next instalment multiplied by twelve. **Add payoff event** saves
the event immediately, after a confirmation that unsaved changes in **Plan inputs** will be
lost. The button needs an editor and an active scenario, is CSRF-protected, and is idempotent:
the event remembers which loan it came from, so adding the same loan payoff again leaves one
event in every interface language. The stored label is written in your interface language.
Loans already paid off, in another currency, whose schedule cannot be calculated or whose payoff
falls outside the plan window are shown with a short explanation instead of a button; archived
loans are not suggested. Suggestions are read from the saved plan and loan schedules when the
page loads; unsaved Plan-input changes do not update them.

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

## Projection

The planner projects a plan year by year from the start year through the horizon age. Each year
shows annual income, expenses, net saving, investment gain and net worth, both in that year's
money (nominal) and in today's money (real). The first year uses the starting portfolio as its
net saving; its gain is earned on that portfolio. Later gains are earned on the previous year's
net worth. The nominal gain rate is inflation **plus** real return, as in the original
spreadsheet: 2.5% inflation and 5% real return mean a 7.5% nominal gain, not 7.625%.

Base income grows with income growth while your age is below the stop age, then stops. Base
expenses grow with inflation. Contribution overrides replace the net saving of their year; a
one-off cash flow is added after that year's gain, so it earns from the following year. Annual
income and expense changes are separate from the base series and stay active from their event
year onward, including after income stops. The projection continues to the horizon after FIRE
and after income stops.

Two FIRE rules are available:

- **Returns cover expenses** — FIRE in the first year whose investment gain is strictly greater
  than that year's expenses. Because the gain is earned on the previous year's balance, the row
  is marked one year after the theoretical moment the return first covers the year's expenses.
- **Withdrawal rate** — FIRE in the first year whose opening portfolio times the withdrawal rate
  is at least that year's expenses.

The results include the FIRE year and age, years to FIRE, net worth at FIRE (nominal and today's
money), the FIRE number in today's money, two progress figures, the depletion age and the
minimum net worth after income stops. The **sheet progress** compares the first year's net worth
with the net worth at FIRE. The **today's progress** compares the starting portfolio with the
FIRE number and is shown even when FIRE is never reached. Depletion and the minimum look at
nominal net worth from the stop age onward, never at the start row. Undefined metrics are
reported as not reached, not as zero, and a plan whose opening net worth is negative does not
switch to FIRE.

Money is rounded once per computed line, half away from zero; real values are deflated
independently from each nominal line. The numeric and rounding rules are recorded in
[ADR-0025](adr/0025-fire-projection-rounding.md). Projections are illustrative, not advice.

## Results

The results page opens with the key metrics:

- **FIRE age** and its year, or **—** when the plan never reaches FIRE;
- **Years to FIRE**, where the start year counts as year 0;
- the **FIRE number** in today's money, or **—** when the rates leave it undefined;
- a **sustainability** card with the depletion age and the lowest net worth after income stops,
  or a note that the portfolio lasts to the horizon or was not checked;
- **Sheet progress** (start net worth ÷ net worth at FIRE) and **Today's-money progress** (start
  portfolio ÷ FIRE number), always shown together with their definitions. A bar stops visually
  at 100%, while the exact percentage can be higher. An undefined progress is **—**, never 0%.

The year-by-year table follows the spreadsheet: year, age, income, expenses, net saving, gain,
cash and net worth. A cash column appears only when the plan has a non-zero cash flow. In the
start year, net saving shows the starting portfolio and earns that year's gain. The **FIRE** row
is highlighted, rows after income stops are muted and marked **No income**, and a depleted row
is marked in red. **Each year's money** shows nominal values; **Today's money** deflates every
amount by the plan's inflation. This is a view preference and is not saved with the scenario.
Editors switch it with a radio control that previews in place, so unsaved input changes are not
lost; viewers and archived plans use plain links.

The net-worth chart follows the same nominal/real choice. It marks the FIRE row with a line and
a pinned point, the income stop and any depletion. The expenses-versus-gain chart is always
nominal because the returns-cover-expenses rule compares nominal values. Both charts have a
**Plotted data** table for keyboard and screen-reader access.

**Download CSV** writes one row per plan year with both bases, independent of the table's
toggle. The file is machine-readable: UTF-8 without a byte-order mark, comma-separated, CRLF
line endings, plain dot decimals with the currency's usual digits and `1`/`0` flags. It contains
no scenario name or metrics.

Changing any input previews the result without saving it; the form keeps the focus and the
results update in place. The preview never writes to the database. A preview that cannot be
projected keeps the last saved results and explains which input needs fixing. A changed
unusual rate is allowed in the preview but warns that saving still needs the confirmation box.
Viewers and archived scenarios are read-only and do not preview.

A plan whose inputs cannot be projected shows every reason in a blocking panel instead of
metrics or charts, and its CSV download returns a conflict rather than an empty file. A linked
mode can be blocked by missing ledger data, a currency mismatch, missing prices, FX rates or
net-worth values, an all-excluded expense list, or no investable portfolio sources. The panel
says which input is affected and suggests switching it to **Manual** or fixing the linked data.
The **Linked inputs** panel still shows the resolved breakdown, window, months with data, as-of
dates, warnings and source links so the problem is visible. A preview resolves the posted linked
modes and exclusions without writing anything.

## Comparing scenarios

**Compare scenarios** on the FIRE planner page opens a read-only comparison. Tick up to three
saved scenarios and submit; with nothing ticked, the default active scenario is shown. The
selection is part of the URL (`?scenario=…&scenario=…`), so a comparison can be bookmarked.
Archived scenarios can be selected and are labelled **Archived**. The scenario order in the
table and the chart is the order in the URL.

The table shows each scenario's start age and year, status, FIRE age and year, years to FIRE,
net worth at FIRE in today's money, and depletion age. A plan that never reaches FIRE before its
horizon says so. A blocked plan keeps its row and lists every reason with a link back to its
**Plan inputs**; it is left out of the chart. When no year after income stops falls inside the
horizon, depletion is shown as **—** with an explanation.

The overlay chart shows net worth over the union of the scenarios' calendar years, so a series
has a gap outside its own start year and horizon. It opens in each year's money and has a
**Today's money** toggle. The table's net-worth-at-FIRE column is always in today's money.
Scenarios can start in different years; a note explains that today's-money values are anchored
to each scenario's own start year. Currencies are never converted: selecting scenarios with
different currencies shows an error and no table or chart. Comparisons write nothing and are
available to every role.

## Sensitivity

A scenario's results page has a **Sensitivity** panel below the plan inputs. **Show sensitivity
grid** loads it on demand; without JavaScript the same link reloads the page with the grid
shown. **Refresh grid** reloads it. The grid always uses the saved plan, including linked values
and category exclusions; unsaved preview changes do not affect it.

The default grid varies the real return by −2, −1, 0, +1 and +2 percentage points and the
starting expenses by 80%, 90%, 100%, 110% and 120% of the saved plan; each expense row shows the
level with its −20%, −10%, 0%, +10% or +20% difference. Only the starting expenses
are scaled: contribution overrides, cash flows, income and expense-change events keep their saved
values. Each cell shows the FIRE age, or **—** when FIRE is never reached before the horizon. The
cell colours compare the age with the saved plan: earlier is better, the same age is neutral,
later is worse, and the saved plan itself is the **Base** cell with a ring and badge. A legend
explains the colours, and every cell has a title and a screen-reader sentence. The expense column
stays visible while the table scrolls sideways on a narrow screen.

The grid cannot be calculated when the selected real-return range would leave the supported
hard bounds (for example a saved return near +50%) or when the scaled starting expenses exceed
the supported amount; the panel explains the range instead of showing a partial grid. A blocked
plan shows its blocking messages instead of a grid. Archived scenarios and viewers can use the
panel.

## Validation

The form refuses and explains:

- a missing or impossible birth year;
- a horizon that is not older than your age at the start;
- an income stop age before your age at the start;
- rates outside the hard bounds, or outside the ordinary ranges without the confirmation;
- contribution years before the year after the start or after the horizon;
- event years before the start or after the horizon;
- duplicate contribution years, a blank event description, or too many rows.

Nothing is saved when validation fails; the rows you typed stay on the form. The projection also
refuses a plan whose last year is past the supported year range; with the start year fixed at
creation, this is a guard for far-future plans.

## Privacy and scope

The FIRE planner is part of the workspace, so every member can read its scenarios and editors
and owners can change them. Projections are illustrative and not financial advice.
