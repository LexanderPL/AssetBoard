# Home dashboard

Home (`/`) is the page you land on after signing in. It is built from **widgets**: small cards
that each show one part of the workspace and link to the module that owns it. You choose which
widgets appear and in what order; the choice is saved separately for each workspace and account.

## The widget catalogue

| Widget | What it shows |
|---|---|
| This month: in vs out | The current month's earnings, spending, saved/invested and what is left |
| This month: expenses by category | The month's spending by category |
| This month: income by category | The month's income by category |
| This month: planned vs actual | Spending and income against the plan, the savings rate and the forecast |
| Upcoming payments | Overdue items and payments due in the Upcoming window; editors and owners can confirm or skip them |
| Net worth | The current net worth and its 12-month trend |
| Portfolio | The portfolio value, gain, allocation by asset class and a value-over-time chart |
| Portfolio changes | The change since yesterday and since the start of the month, plus the largest movers |
| Loans | The remaining principal, payoff date and repaid share of up to three active loans |
| FIRE | Plan progress and the FIRE age from the workspace's FIRE plan |
| Missing data | Stale prices, missing work logs and imports that are planned but not applied |

A module that has no data yet shows a short empty state with a link to set it up, for example
“Set up investments”. A widget for a module that has not shipped is not in the catalogue.

## Defaults

An account that has never changed Home sees every widget above, in catalogue order. When a later
release adds a widget, accounts that never customised Home see it automatically. Accounts that
have saved their own layout do not: the new widget appears in the picker but stays off until it
is chosen.

If you remove every widget, Home shows an empty state with a link back to the picker.

## Customising Home

Choose **Customise** at the bottom of Home, below the widgets:

- tick the widgets you want;
- use the **↑** and **↓** buttons to set their order (no drag and drop needed; it works with a
  keyboard and on a phone);
- choose how many upcoming payments the Upcoming widget lists (between 1 and 50; the default is
  10; the rest stay on the Upcoming page);
- choose the Portfolio chart range: 1 month, 3 months, 6 months, 1 year (the default) or all time;
- **Save** applies the layout, or **Reset to default** returns to the full catalogue and default
  settings.

With JavaScript, moving a widget only changes the draft on the screen until you save. The page is
a plain form, so without JavaScript an arrow rerenders the draft on the server and **Save** still
applies it.

The Portfolio value-over-time chart shows the current value and the invested capital. When there
is no dated investment history it says so and links to the portfolio page instead of drawing an
empty chart.

## Loading and accessibility

Each widget loads independently after the page shell appears, so a slow module does not hold up
the rest of Home. On a phone the widgets stack in one column in your chosen order. Charts follow
the same rules as the rest of the app: the drawing is hidden from assistive technology and a
table with the same figures sits next to it, visible without JavaScript. If JavaScript is off,
each placeholder links directly to its module page.

## Missing data

The **Missing data** widget is a checklist, not a warning that blocks the app:

- **Stale or missing prices** are holdings whose latest price is older than the usual
  threshold or has no price at all; open the portfolio to add a price or refresh a provider.
- **Missing work logs** are variable income sources whose payment is due soon and whose month has
  no work log; open the income calendar to fill it.
- **Imports waiting to be applied** are planned imports whose file has not been applied yet. This
  section is only shown to editors and owners, like the import page itself.

When nothing needs attention the widget says so.
