# FIRE plan

The FIRE planner keeps **one plan per workspace** and shows a year-by-year projection of when
the plan reaches financial independence. A plan is a current-year view: it starts in the
calendar year you open it, not in a saved start year. Every amount is nominal money in the plan
currency.

Projections are illustrative and not financial advice. The starting rates are examples, not
recommendations. The projection assumes constant rates. Income and expenses are the amounts
recorded in your ledger: income may be gross, and expenses should include the taxes you actually
pay and your loan payments. The planner does not model taxes separately.

## One plan

**FIRE projection** opens the plan. If the workspace has no plan, the page starts the four-step
**setup wizard**. A workspace has at most one plan; creating another is refused.

The plan page is **read-only**. Editors and owners open a separate **Edit plan** page, change the
values and adjustments there, and return to the read-only view after **Save changes**. Viewers can
read the plan, its results and its adjustments but never see an edit control.

The plan keeps a **currency snapshot** from the workspace base currency when it is created or
migrated. If the workspace base currency changes later, the page shows a blocking notice and
does not reinterpret the saved amounts. **Reset plan** deletes the plan and lets you create a new
one in the current currency.

A viewer in a workspace with no plan sees an empty state and is told that an editor or owner can
set it up.

## Setting up the plan

The wizard writes nothing until the last step. **Next** validates the current step and its
earlier steps; **Back** returns without saving; **Cancel** leaves the wizard and no plan exists.
The step values travel with the form, so leaving the wizard discards them. If the workspace base
currency changes while you are in the wizard, the amounts are not converted; the refreshed
review shows the new currency and you confirm it before **Create plan**.

The review on the last step summarises the values that will be saved. If you change an
assumption on that step, **Review changes** (or pressing **Create plan**) refreshes the summary
first; **Create plan** never saves values that differ from the review you last saw.

1. **About you:** birth year (prefilled from your profile for a new plan, editable for this
   plan), horizon age (prefilled from your profile's life expectancy) and the age at which
   income stops. Birth year has its own full-width row; horizon age and the income-stop age share
   the row below it. The page shows your current age and the first and last projected years. A
   new plan stores its own birth year; changing your profile later does not change an existing
   plan.
2. **Income and expenses:** empty by default. Each field has a suggestion card: **Use suggested
   value** copies the suggestion into the field. The income and expense suggestions use the
   last 12 full calendar months, with the window, months with data and any warnings shown. Both
   cards put their category breakdown behind a **Where this value comes from** disclosure; the
   expense card also shows the loan payments that are linked to ledger expenses, outside that
   disclosure and always visible.
3. **Portfolio:** the starting portfolio, with the current investable-assets suggestion, its
   as-of date and a list of the sources (portfolio classes and manual net-worth items) that link
   to the page where each source lives. A **Skipped** subsection lists the sources that are not
   counted: non-investable asset classes and linked net-worth items whose value is already
   counted through their source. Skipped sources show their name, a link and the reason, never
   an amount.
4. **Assumptions and review:** inflation, return above inflation, income growth and the
   independence rule. **Independence rule** and **Withdrawal rate** sit side by side in their
   own row below the three rates. **Returns cover expenses** is the default: it checks whether
   the portfolio's return above inflation covers the year's expenses, which preserves the
   portfolio's real capital. **Withdrawal rate** is used only when the independence rule is
   **Withdrawal rate**. **Portfolio lasts to the horizon** is an optional rule described below.
   With any rule other than **Withdrawal rate** the rate field stays visible but inactive and
   disabled; without JavaScript it stays enabled, and the server ignores the rate for those
   rules. The review shows the entered values, the projected window, the
   warnings and a read-only list of active loans with their annual payment, payoff year and
   included state. A changed rate outside the usual range shows a warning. **Create plan** then
   opens a confirmation panel; nothing is saved until **Confirm and create plan**. There is no
   confirmation checkbox. Adjustments and loan include/ignore choices are changed after creation
   in edit mode.

A blocked suggestion explains why it is unavailable (no data, another ledger currency, a missing
price/FX rate or value) and leaves the field editable; the app never shows a fake zero. The
starting rates are illustrative examples, not recommendations.

## Viewing the plan

The plan page shows the saved plan without any inputs or preview controls:

- the key metrics, the plan-progress bar and their definitions;
- one collapsed **Plan values, adjustments and loans** section between the progress bar and the
  year-by-year results. It holds the saved **Plan values** (rates, ages and the three manual
  amounts), the **Adjustments** applied to the plan (with before-the-current-year adjustments in
  a collapsed **Archived adjustments** section) and the **Loans** panel with each active loan's
  annual payment, payoff year, included/ignored state and effect on the plan. Open it with a
  click, Enter or Space; no JavaScript is needed;
- the year-by-year schedule and the net-worth and expenses-versus-gain charts;
- the **Sensitivity** grid, which always uses the saved plan and never unsaved changes;
- the disclaimer, which is the last block on the page.

A converted plan shows a review banner until an editor or owner saves it in edit mode. A plan
whose values were last reviewed in an earlier year shows the living-plan review banner; its
editors' link opens the edit page with **Starting amounts** open. The page never writes. All
amounts are nominal: each year's actual money, not adjusted for inflation.

## Editing the plan

Editors and owners open **Edit plan**. The edit page is a separate page; it is not available to
viewers. It contains every plan field and the adjustments editor.

The page order is: key metrics and plan progress, the plan-inputs accordion, then the
year-by-year table and charts, the sensitivity grid and the disclaimer. The accordion has five
sections: **Plan**, **Assumptions**, **Starting amounts**, **Adjustments** and **Loans**. All are
collapsed by default and only one can be open at a time. The server opens the section that owns a
save error, **Adjustments** for an adjustment-row error, **Assumptions** when unusual rates need
confirmation, and **Starting amounts** when the living-plan year review is due; an error section
wins over the year review. `/fire/edit#amounts` opens that section directly.

Results, charts and the year-by-year schedule recalculate on the server after every change; the
page previews the unsaved plan and never saves a preview. The preview updates the metrics and the
table/charts independently and never replaces the form, so focus and entered values stay in
place. A preview error shows the message at the top of the results and opens the section that
owns the invalid control without moving focus. **Save changes** validates the whole plan, writes
it, clears the review banner and returns to the read-only view with a notice. An invalid value
shows an inline error and leaves the saved plan untouched. A plan whose expense line would be
negative in a year is also refused and the page names that year. **Discard changes** reloads the last
saved values and keeps you in edit mode. Leaving with unsaved changes asks for confirmation, and
an **Unsaved changes** indicator shows the state.

Each amount has a suggestion card:

- **Income** and **expenses** use the last 12 full calendar months, with the window, the months
  with data, the data date, any warnings and the annualized amount. Both cards put the
  per-category totals behind a **Where this value comes from** disclosure; the expenses card
  also shows the loan payments linked to ledger expenses in the window, outside that disclosure
  and always visible.
- **Portfolio** shows the current investable assets, the as-of date and the sources that make up
  the value. Its **Skipped** subsection lists the sources that are not counted: non-investable
  asset classes and linked net-worth items whose value is already counted through their source.
  Skipped sources show their name, a link and the reason, never an amount.

**Use suggested value** copies the suggestion into the field. Nothing is copied until you press
it, and the saved amount changes only when you press **Save changes**. The suggestions come from
recorded ledger cash flows and carry the same treatment as the ledger; the planner does not
adjust them for tax. The card shows the saved amount and says whether it matches or differs from
the current suggestion. A blocked suggestion explains why and leaves the field for manual
entry. Suggestions are read-only and never write.

The adjustments row editor is available in edit mode; a saved adjustment before the current year
stays archived history and is not applied. The loan section reads the current loan schedules
live; its include/ignore choices are saved with **Save changes**. If you edit the same plan in
two tabs, the last save wins; reload the other tab before saving.

## Live loans

The plan reads the active loan schedules from **Loans** every time it is viewed or previewed. It
does not save a loan-payoff event and there is no button to create one: changing a loan's rate,
term or overpayment plan changes the next FIRE calculation.

- **Annual payment** is the next scheduled monthly payment times twelve. It includes interest,
  principal, costs and the loan's regular overpayment. A future one-off overpayment changes the
  projected payoff date only; it is not part of the recurring annual payment. The planner does
  not sum the remaining twelve months and does not prorate a payoff inside a year.
- **Included** loans are part of the expenses the plan already contains. From the projected
  payoff year onward the planner removes their annual payment, so the expense line and the FIRE
  metrics reflect the lower spending. A payoff in the current year removes the full annual
  payment from that year.
- **Ignored** loans are shown in the plan but produce no expense drop; their payment stays in the
  baseline through the horizon. Ignore a loan only when its payment is not part of the expenses
  you entered, or when you deliberately do not want its payoff to affect the forecast.
- The loan choice is saved per plan with **Save changes**; **Discard changes** restores the saved
  choice and a preview never writes. Existing plans treat every loan as included until you
  change it. In edit mode the results summary at the top of the page is the live preview of the
  posted choices, including the expense warning; the detailed loan cards below keep showing the
  saved choice and its effect until you save.
- Multiple loans are summed, each with its own payoff year. A badge on the payoff year marks the
  year a loan ends; the **Loans** panel shows the annual payment and its effect. The year-by-year
  table no longer repeats a separate loan-payments column. The charts mark included loan payoffs.
- Loans in another currency, archived loans, paid-off and not-drawn loans, broken schedules and
  installment plans are listed with a reason and are not included. They are never silently
  treated as zero. A saved ignore survives archiving the loan, so restoring it restores the
  choice; a request that tries to change an excluded loan's choice is rejected rather than
  silently rewriting that saved choice.
- If the scheduled loan payments are larger than the entered annual expenses, the page warns
  that the expenses may not include them. The planner does not cap the drop. If removing an
  included loan payment (or an annual expense change) would make the expense line negative in
  a year, the projection is blocked and names that year; the plan cannot declare FIRE from a
  negative expense line.
- Loan payments are fixed nominal amounts; the rest of the expense line inflates with the plan's
  inflation rate. This is an approximation, especially for a loan whose rate or payment changes
  in the future. Sensitivity scaling applies to the whole expense line, including the loan
  component; payoff years stay as projected.
- A final contractual row that contains a balloon payment is annualised as the next scheduled
  payment times twelve, as specified; the page can therefore overstate the recurring payment in
  that final period.

## Reset plan

Editors and owners can **Reset plan** from the plan page. The reset page shows how many
adjustments will be deleted, offers a link to the workspace archive for a copy, and asks you to
type **reset** to confirm. A wrong or empty confirmation deletes nothing. A successful reset is
recorded in the audit log and returns to the setup wizard.

## Old scenarios

Version 0.7 allowed several named scenarios. On the first use after the upgrade:

- one old scenario is converted automatically when an editor opens the planner;
- several old scenarios show a one-time chooser. Choose the scenario to keep and confirm that
  the others will be deleted. Download a backup from the workspace data page first if you want
  a copy of the old scenarios;
- viewers see the old scenarios but cannot convert them; an editor or owner must do it.

The converted plan is marked for review. Its linked values were resolved once during the
conversion and stored as manual amounts. A linked value that could not be resolved is stored as
zero and the migration records which inputs were affected in the audit log. Open edit mode,
check the values and save to clear the review banner.

## Plan fields

- The projection always starts in the current calendar year. There is no first-year input field.
- The plan currency is the snapshot taken when the plan was created. It is not an input: edit
  mode shows no currency field, and a workspace base-currency change blocks the plan with a
  review notice instead of converting the saved amounts.
- The plan is **living**: in a later year it starts in that year, but the page shows a
  **review** banner until you save it again. The banner names the year the values were last
  reviewed and the current first year. Review income, expenses and portfolio, then press
  **Save changes**; the next year starts with the same saved amounts until you change them.
  The projection does not silently update the amounts. Editors and owners get a link to the plan
  inputs from the banner; viewers see the same notice without the link.
- **Birth year** turns plan years into ages. The setup wizard prefills it from your profile; the
  plan keeps the value saved with it, so later profile changes do not alter an existing plan.
  Edit mode keeps that saved value without showing a birth-year field.
- **Horizon age** is the last age in the projection. It defaults from your profile's life
  expectancy (90 when the profile has none).
- **Income stops at age** ends the base income. Income is paid while the age is below this
  value; the equal age has no base income. If the age is already in the past, base income is
  zero and the page explains it. A stop age after the horizon is allowed and marked with a
  warning.
- **Annual income**, **annual expenses** and **starting portfolio** are manual amounts in the
  plan currency and at the current year's money. They cannot be negative; zero is allowed and
  shown as zero. Income and expenses are the amounts recorded in your ledger. Income may be
  gross; expenses should include the taxes you actually pay. The projection does not model taxes
  separately, including income-tax disappearance when income stops or capital-gains tax.
- Expenses are expected to include your loan payments. The planner removes an included loan's
  recurring annual payment from its projected payoff year onward; an ignored loan stays in the
  forecast through the horizon.

## Rates

Rates are entered as percentages and stored as basis points. They may be negative where that
makes sense. The hard bounds are −20% to 50% for inflation, return above inflation and income
growth, and 0.5% to 20% for the withdrawal rate.

The ordinary ranges are 0–10% inflation, −2–8% return above inflation, 0–10% income growth and
2–6% withdrawal rate. A rate outside its ordinary range asks for confirmation when you save or
create the plan. Edit mode shows a confirmation panel with **Confirm and save**; the wizard shows
a confirmation panel with **Confirm and create plan**. Nothing is written until you confirm. The
confirmation applies to the rates shown: if you change one of them first, the page asks again.
Only a rate you actually changed asks for confirmation again, so editing another field of a plan
with an unusual rate does not nag on every save. There is no confirmation checkbox.

The projection compounds inflation and the return above inflation exactly: the nominal gain is
`(1 + inflation) × (1 + return above inflation) − 1`. The two rates are not added together.

The starting values (2.5% inflation, 5% return above inflation, 3% income growth and 4%
withdrawal rate) are illustrative examples, not recommendations.

**Withdrawal rate** is used only when the independence rule is **Withdrawal rate**. With
**Returns cover expenses** or **Portfolio lasts to the horizon** the field stays in the layout
but is inactive and disabled; without JavaScript it stays enabled, and the server ignores the
rate for those rules. When the saved plan stores no withdrawal rate, edit mode shows the
illustrative 4% in the inactive field so it matches the help text; the rate is still not stored
for those rules. The two fields share a row below inflation, return above inflation and income
growth; there is no separate note beside the withdrawal rate.

## Independence rules

The plan reaches FIRE at the first age that satisfies the selected rule. The rule is only a
definition of independence; the year-by-year table always shows the plan you entered, including
the configured **Income stops at age**.

- **Returns cover expenses** (the default): the portfolio's return above inflation covers that
  year's expenses. This preserves the portfolio's real capital.
- **Withdrawal rate**: the portfolio multiplied by the withdrawal rate covers the year's
  expenses. The rate is stored with the plan and is used only by this rule.
- **Portfolio lasts to the horizon**: the earliest age at which stopping base income *at that
  age* leaves the portfolio non-negative in every year through the horizon. Zero at the horizon
  counts as lasting. The rule considers retiring at any age from today up to the earlier of
  **Income stops at age** and the year before the horizon; it cannot declare FIRE after base
  income has already stopped. Income and expense changes, saving overrides, loans and all other
  assumptions stay as entered. This rule deliberately allows spending principal and is not a
  perpetual-withdrawal or longevity guarantee.

The FIRE number is defined for **Returns cover expenses** and **Withdrawal rate**. **Portfolio
lasts to the horizon** has no rate-based FIRE number and no net-worth target, so the FIRE-number
and plan-progress cards explain that they are not defined for that rule; **Net worth at FIRE**
still shows the projected amount.

## Adjustments

The plan's **Adjustments** list holds four kinds of event, stored as calendar years:

- **One-off cash flow** adds to net worth in that year; a negative amount is a withdrawal.
- **Annual income change** changes the income series from that year onward and grows with
  income growth.
- **Annual expense change** changes the expense series from that year onward and grows with
  inflation.
- **Saving override** replaces that year's net saving. A year can have only one override, and a
  new override starts in the next year because the current row uses the starting portfolio.

In edit mode, adjustments are a compact table and are added one row at a time and edited in
place. There is no bulk-paste box. Rows of the same kind and year are added together. A zero
amount is allowed; negative amounts reduce, positive amounts increase. All adjustment changes,
together with the plan fields, are saved by **Save changes** in one transaction; **Discard
changes** leaves the saved adjustments untouched.

To remove an adjustment, choose **Remove** at the end of its row and save. A saved row is marked,
the control changes to **Undo**, and nothing is deleted until **Save changes**; choosing
**Undo** keeps the row. A row you added but have not saved is removed immediately and has no
effect on the saved plan. The checkbox behind the control is visually hidden, so removal works
without JavaScript too. A saved row is never deleted just because it is missing from a request;
the app reloads the rows and asks you to try again.

Adjustments from before the current year, after the plan's last year, and a saving override that
has become the current year after a year rollover are kept as **Archived adjustments**. They stay
visible in a collapsed section with the reason, are read-only, and are not applied to the
projection. Editors and owners can delete an archived adjustment by choosing **Remove** and
saving; viewers cannot. Archived history is never edited in place or moved to another year. An
adjustment archived only because it is after the plan's last year applies again when you raise the
horizon; the preview includes it before you save.

## Results

The plan page shows nominal values only:

- **FIRE age** and year, **years to FIRE** and the **FIRE number**. The FIRE number is the
  nominal expenses in the FIRE year divided by the rate the independence rule uses: the
  withdrawal rate, or the return above inflation when returns must cover expenses. It is shown
  with the FIRE year, and is a dash when FIRE is never reached or the rate is not positive.
  **Portfolio lasts to the horizon** has no rate-based FIRE number; the card says so explicitly;
- **Plan progress** is a full-width bar directly below the four key metric cards. The percentage
  is shown large, and the legend shows the **Starting portfolio** and **Net worth at FIRE** with
  their formatted amounts. The bar fills in success colour and is capped visually at 100%; the
  accessible value keeps the exact percentage. When FIRE is not reached or net worth at FIRE is
  not positive, the bar is muted and the page explains why. It is the starting portfolio divided
  by the projected nominal net worth at FIRE, so 100% means the plan has reached its FIRE
  net-worth target. **Portfolio lasts to the horizon** has no net-worth target, so its progress
  is not defined and the card says so;
- the sustainability check: the nominal depletion age and the lowest nominal net worth after
  income stops;
- a clear block instead of results when the annual expense line would be negative in any year.
  The message names the year, and the plan never shows FIRE from negative expenses. The same
  applies to a sensitivity expense level that would make the line negative;
- a year-by-year schedule with income, expenses, net saving, investment gain, cash and net worth.
  Every amount is the actual money of that year, not adjusted for inflation. It sits directly in
  the page column and uses the same surface background as the Transactions tables, with the
  table's own border and the FIRE, depleted and income-stopped row tints. The first row is the
  starting point: today's portfolio earns one year's gain, and the current year's income and
  expenses are shown, but that row's net saving is the starting portfolio;
- a nominal net-worth chart and a nominal expenses-versus-gain chart, each with accessible
  plotted data;
- for editors and owners, **Edit plan** and **Reset plan**. The plan page has no download action.

The **Plan values**, **Adjustments** and **Loans** sections live together in one collapsed
disclosure between the progress bar and the year-by-year schedule. The plan page is read-only.
Editors and owners change the values on the separate edit page.

The **Sensitivity** panel projects the saved plan across a range of returns above inflation and
expense levels. It is always rendered on the view and edit pages; there is no load button. It
shows only the resulting FIRE age, never amounts. It always uses the saved plan, never unsaved
preview values or posted loan choices. A blocked plan shows the reason instead of a grid, and
never a fake zero. When an expense level would make the expense line negative, the grid shows
the reason and the year instead of a misleading FIRE cell. The editor additionally shows
**Refresh grid**, which re-fetches the grid from the saved plan; it is hidden without JavaScript
because the grid is already rendered.

Each expense level scales the whole expense line: the starting expenses, every annual expense
change and every loan's recurring payment, with one rounding for each component. Income, cash
flows and saving overrides keep their saved values. The saved plan's own expense level therefore
matches the direct projection exactly.

## Archive and restore

The workspace archive uses format **2.1**. It has one `fire_plan.csv` row (header-only when
there is no plan), `fire_events.csv` and `fire_loan_settings.csv`; there are no scenario,
contribution or exclusion files. A 2.0 archive still restores with every loan included.
Archive export includes the plan currency, manual amounts, `needs_review`, every adjustment
(including archived history) and the plan's ignored-loan choices. Suggested values, live loan
schedules and projection results are derived and are not archived.

A 1.8 archive still imports. It keeps the default active scenario, else the first active, else
the first archived one; linked values use the exported fallbacks; contribution overrides become
saving overrides; one-click loan-payoff events are removed; and the other scenarios are listed
as skipped. A merge ignores FIRE files and reports them as restore-only. There is no standalone
FIRE CSV import.

## Roles

Every member can read the plan, its results, its adjustments and the sensitivity grid. Editors
and owners can set up the plan, open the separate edit page, save or discard changes, convert old
scenarios and reset the plan. Viewers never see edit controls. All write pages are CSRF-protected,
and a cross-workspace plan is reported as not found.
