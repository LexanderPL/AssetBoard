# Taxes

Taxes are optional. Turn them on in **Workspace settings → Menu → Taxes** (owners); the switch
cannot be turned off while an active income source has a tax ticked, and the settings page lists
those sources. When it is off, the Taxes menu item, the tax pages and the tax parts of the income
page are hidden. AssetBoard does not provide tax rates or tax advice: you enter the rates.

## Taxes on income

**New tax** asks one question: *who pays it to the tax office?*

- **I do. It is added on top of what I charge** (like VAT): what you receive grows by the tax,
  and a payment is planned.
- **I do, from my income** (like income tax or contributions you pay yourself): what you receive
  is unchanged, and a payment is planned.
- **The payer takes it off before paying me** (like tax on a salary): what you receive shrinks by
  the tax, and nothing is planned.

The amount is a percentage (your locale's number format, at most two decimals, e.g. `23` or
`12,75`) or a fixed amount per payment in the workspace currency. For taxes you pay, choose the
payment day, the month (the month after you are paid, the month you are paid, or the month after
each quarter: April, July, October, January) and the expense category for the payment. Taxes
from your income and withheld taxes can be calculated on the net amount or, under the collapsed
advanced option, on net plus taxes added on top. The **Example** panel shows what the tax means
for 10 000 of net income; without JavaScript it shows the saved values.

Each percentage is rounded half up to the currency's minor unit, once per tax. The first rate is
valid from the start; **Rate changes** on the Taxes page add a later rate, and past months keep
the old one. Who pays the tax and its base can change until income calculated with the tax has
been posted; after that create a new tax. Archiving (on the edit page) stops using a tax in new
calculations and keeps its history.

## Other taxes

Taxes that do not depend on income, like real estate or vehicle tax, are added with **Add
another tax**: name, amount, how often (every year, quarter or month, or once), next due date and
expense category. They are recurring items marked as taxes: they show on the Recurring page too,
and **Edit** opens the recurring item.

The former calculator page now redirects to Taxes; its function is the example panel.
