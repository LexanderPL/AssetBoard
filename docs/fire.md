# FIRE plan

The FIRE planner keeps **one plan per workspace** and shows a year-by-year projection of when
the plan reaches financial independence. A plan is a current-year view: it starts in the
calendar year you open it, not in a saved start year. Every amount is nominal money in the plan
currency.

Projections are illustrative and not financial advice. The starting rates are examples, not
recommendations, and the projection assumes constant rates and no separate tax model.

## One plan

**FIRE projection** opens the plan. If the workspace has no plan, the page offers **Create your
FIRE plan**. A workspace has at most one plan; creating another is refused.

The plan keeps a **currency snapshot** from the workspace base currency when it is created or
migrated. If the workspace base currency changes later, the page shows a blocking notice and
does not reinterpret the saved amounts. Reset the plan and create a new one in the new currency
when that option is available.

## Old scenarios

Version 0.7 allowed several named scenarios. On the first use after the upgrade:

- one old scenario is converted automatically when an editor opens the planner;
- several old scenarios show a one-time chooser. Choose the scenario to keep and confirm that
  the others will be deleted. Download a backup from the workspace data page first if you want
  a copy of the old scenarios;
- viewers see the old scenarios but cannot convert them; an editor or owner must do it.

The converted plan is marked for review. Its linked values were resolved once during the
conversion and stored as manual amounts. A linked value that could not be resolved is stored as
zero and the migration reports which inputs were affected. Open the plan inputs, check the
values and save to clear the review banner.

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
They can be removed from the plan.

## Results

The results page shows:

- **FIRE age** and year, **years to FIRE**, the **FIRE number** and **progress** definitions;
- the sustainability check: the depletion age and the lowest net worth after income stops;
- a year-by-year schedule with income, expenses, net saving, investment gain, cash and net worth;
- a net-worth chart and an expenses-versus-gain chart;
- a collapsible **Plan inputs** panel and a **Download CSV** export.

Changing an input previews the new results without saving. Press **Save changes** to persist and
clear the review banner, or **Discard changes** to return to the saved values. The
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

Every member can read the plan and its results. Editors and owners can create, edit, convert old
scenarios and delete the plan. All write pages are CSRF-protected, and a cross-workspace plan is
reported as not found.
