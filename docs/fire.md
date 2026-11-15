# FIRE plan

The FIRE planner keeps **one plan per workspace** and shows a year-by-year projection of when
the plan reaches financial independence. A plan is a current-year view: it starts in the
calendar year you open it, not in a saved start year. Every amount is nominal money in the plan
currency.

Projections are illustrative and not financial advice. The starting rates are examples, not
recommendations, and the projection assumes constant rates and no separate tax model.

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

1. **About you:** birth year (prefilled from your profile, editable for this plan), horizon age
   (prefilled from your profile's life expectancy) and the age at which income stops. The page
   shows your current age and the first and last projected years.
2. **Income and expenses:** empty by default. Each field has a suggestion card: **Use suggested
   value** copies the suggestion into the field, but a suggestion never fills a field by itself.
   The income and expense suggestions use the last 12 full calendar months, with the window,
   months with data and any warnings shown. The expense card also shows the category breakdown
   (window totals) and the loan payments that are linked to ledger expenses.
3. **Portfolio:** the starting portfolio, with the current investable-assets suggestion, its
   as-of date and a list of the sources (portfolio classes and manual net-worth items) that link
   to the page where each source lives.
4. **Assumptions and review:** inflation, real return, income growth and the independence rule.
   The review shows the entered values, the projected window and the warnings; **Create plan**
   writes the plan once. Adjustments are added after creation, and active loans are listed in the
   plan after a later update.

A blocked suggestion explains why it is unavailable (no data, another ledger currency, a missing
price/FX rate or value) and leaves the field editable; the app never shows a fake zero. The
starting rates are illustrative examples, not recommendations.

## Viewing the plan

The plan page shows the saved plan without any inputs or preview controls:

- the key metrics and their definitions;
- the net-worth and expenses-versus-gain charts;
- the year-by-year schedule;
- the saved **Plan values** (rates, ages and the three manual amounts);
- the **Adjustments** applied to the plan, with before-the-current-year adjustments in a collapsed
  **Archived adjustments** section;
- the **Sensitivity** grid, which always uses the saved plan and never unsaved changes;
- the **Download CSV** action when the projection is available.

A converted plan shows a review banner until an editor or owner saves it in edit mode. The page
never writes. The nominal/today's-money switch is a view preference in the link, not a saved
plan value.

## Editing the plan

Editors and owners open **Edit plan**. The edit page is a separate page; it is not available to
viewers. It contains every plan field and the adjustments editor.

Results, charts and the year-by-year schedule recalculate on the server after every change; the
page previews the unsaved plan and never saves a preview. **Save changes** validates the whole
plan, writes it, clears the review banner and returns to the read-only view with a notice. An
invalid value shows an inline error and leaves the saved plan untouched. **Discard changes**
reloads the last saved values and keeps you in edit mode. Leaving with unsaved changes asks for
confirmation, and an **Unsaved changes** indicator shows the state.

Each amount has a suggestion card:

- **Income** and **expenses** use the last 12 full calendar months, with the window, the months
  with data, the data date, any warnings and the annualized amount. The expenses card also shows
  the category breakdown and the loan payments linked to ledger expenses in the window.
- **Portfolio** shows the current investable assets, the as-of date and the sources that make up
  the value.

**Use suggested value** copies the suggestion into the field. A suggestion never fills a field by
itself, and the saved amount changes only when you press **Save changes**. The card shows the
saved amount and says whether it matches or differs from the current suggestion. A blocked
suggestion explains why and leaves the field for manual entry. Suggestions are read-only and
never write.

The adjustments row editor is available in edit mode; a saved adjustment before the current year
stays archived history and is not applied. A later update adds the live loan schedules; until
then, enter expenses that include your loan payments. If you edit the same plan in two tabs, the
last save wins; reload the other tab before saving.

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

- **First projected year** is always the current year and is shown read-only.
- **Birth year** turns plan years into ages. It is prefilled from your profile, but you can
  change it for this plan without changing the profile.
- **Horizon age** is the last age in the projection. It defaults from your profile's life
  expectancy (90 when the profile has none).
- **Income stops at age** ends the base income. Income is paid while the age is below this
  value; the equal age has no base income. If the age is already in the past, base income is
  zero and the page explains it. A stop age after the horizon is allowed and marked with a
  warning.
- **Annual income**, **annual expenses** and **starting portfolio** are manual amounts in the
  plan currency and at the current year's money. They cannot be negative; zero is allowed and
  shown as zero.
- Expenses are expected to include your loan payments. The planner removes an included loan's
  payment after its projected payoff when live loans are shown in a later update.

## Rates

Rates are entered as percentages and stored as basis points. They may be negative where that
makes sense. The hard bounds are −20% to 50% for inflation, real return and income growth, and
0.5% to 20% for the withdrawal rate.

The ordinary ranges are 0–10% inflation, −2–8% real return, 0–10% income growth and 2–6%
withdrawal rate. A rate outside its ordinary range needs the **unusual rates** confirmation.
When editing, only a rate you actually changed asks for it again.

The starting values (2.5% inflation, 5% real return, 3% income growth and 4% withdrawal rate)
are illustrative examples, not recommendations.

## Adjustments

The plan's **Adjustments** table holds four kinds of event, stored as calendar years:

- **One-off cash flow** adds to net worth in that year.
- **Annual income change** changes the income series from that year onward.
- **Annual expense change** changes the expense series from that year onward.
- **Saving override** replaces that year's net saving. A year can have only one override, and a
  new override starts in the next year because the current row uses the starting portfolio.

Negative amounts reduce, positive amounts increase. Cash, income-change and expense-change
events may repeat in a year and are summed by kind. Adjustments from before the current year,
and a saving override that has become the current year after a year rollover, are kept as
**Archived adjustments**: they stay visible as history but are not applied to the projection.
Adjustments are edited on the separate edit page.

## Results

The plan page shows:

- **FIRE age** and year, **years to FIRE**, the **FIRE number** and **progress** definitions;
- the sustainability check: the depletion age and the lowest net worth after income stops;
- a year-by-year schedule with income, expenses, net saving, investment gain, cash and net worth;
- a net-worth chart and an expenses-versus-gain chart;
- **Download CSV** and, for editors and owners, **Edit plan** and **Reset plan**.

The plan page is read-only. Editors and owners change the values on the separate edit page; the
nominal/today's-money basis switch is a view preference and is not saved.

The **Sensitivity** panel projects the saved plan across a range of real returns and expense
levels. It always uses the saved plan, never unsaved preview values. A blocked plan shows the
reason instead of a grid, and never a fake zero.

## Archive and restore

The workspace archive uses format **2.0**. It has one `fire_plan.csv` row (header-only when
there is no plan) and `fire_events.csv`; there are no scenario, contribution or exclusion files.
Archive export includes the plan currency, manual amounts, `needs_review` and every adjustment,
including archived history. Suggested values, live loan data and projection results are derived
and are not archived.

A 1.8 archive still imports. It keeps the default active scenario, else the first active, else
the first archived one; linked values use the exported fallbacks; contribution overrides become
saving overrides; one-click loan-payoff events are removed; and the other scenarios are listed
as skipped. A merge ignores FIRE files and reports them as restore-only. There is no standalone
FIRE CSV import.

## Roles

Every member can read the plan, its results, its adjustments, the CSV and the sensitivity grid.
Editors and owners can set up the plan, open the separate edit page, save or discard changes,
convert old scenarios and reset the plan. Viewers never see edit controls. All write pages are
CSRF-protected, and a cross-workspace plan is reported as not found.
