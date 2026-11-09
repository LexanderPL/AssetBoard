# Investments

The **Investments** section records what you hold outside the ledger and how its value is split
across asset classes. The **Portfolio** tab values what you hold today; the other tabs manage
instruments, asset classes, transactions and price providers. Performance over time arrives in
a later update.

> **Backups:** investments are not part of the workspace archive yet. An export taken before the
> investments archive extension does not contain asset classes, instruments, allocations,
> prices or FX rates.

## Asset classes

Asset classes group instruments for the portfolio and allocation views. A new workspace starts
with the suggested set:

| Class | Investable | Risk tier |
|---|---|---|
| Stocks | yes | Growth |
| Bonds | yes | Safe |
| Metals | yes | Safe |
| Crypto | yes | Speculative |
| Currencies | yes | Growth |
| Other | yes | Unassigned |

Property is not an investment here: record your home and other real estate in **Net worth**, in
the **Real estate** category. Workspaces created before this change also have the **Real
estate** and **Primary residence** classes (not investable); they stay as you left them, and you
can archive them once nothing uses them.

**Investable** marks the classes that count as invested capital for the FIRE planner and the
net-worth helper. It never hides anything in Investments: the portfolio counts every class and
marks a non-investable one *not counted in FIRE* (for example an emergency fund or money set
aside for a purchase). You can rename a class, change its colour or investable flag, and move it up
or down; the order is used wherever classes are listed. There is no "add suggested classes"
action while an active class exists. A workspace created before this feature can add the
suggested set once when it has no active class; nothing is added automatically.

If every class is archived, the seed action can add a fresh suggested set. The archived originals
remain; restoring one then fails while the new class with the same name is active. Rename or
archive the new class first if you want the original back.

Archiving a class keeps it on instruments that already use it and keeps its place in history.
It cannot be chosen for a new allocation. Restoring it puts it at the end of the list; restoring
is refused if another active class has the same name. Names are unique among active classes,
ignoring case.

## Instruments

An instrument is one thing you hold: a stock, ETF, bond, metal, currency, property or anything
else. Its fields are:

- **Name** — unique among active instruments, ignoring case.
- **Symbol** — optional, for example `VWCE.DE` or `BTC`.
- **Type** — the defaults below.
- **Quote currency** — the currency of the unit price and fees.
- **Unit label** — what one unit is called. A currency instrument always uses its currency code
  (for example `USD`), whatever label is typed.

| Type | Unit default | Valuation |
|---|---|---|
| Stock | share | manual price |
| ETF | share | manual price |
| Bond (market price) | bond | manual price |
| Bond (model value) | bond | model value from terms |
| Metal | oz | manual price |
| Crypto | coin | manual price |
| Currency | the currency code | unit price 1 in its own currency; value converted from FX |
| Real estate | property | manual price, quantity 1 (prefer a net-worth item, see above) |
| Other | unit | manual price |

A **currency** instrument's `quote_currency` is the held currency. Its unit price is always **1**
in that currency, so there is no market price to enter or fetch; only the FX rate to the base
currency converts it. Cash in the base currency is therefore price 1 at rate 1.

### Bond model valuation

A **Bond (model value)** instrument is valued from its terms instead of a market price. Open the
instrument to enter the series code, face value per unit, maturity, interest mode, rate schedule,
tax rate and an optional early-redemption fee. The instrument can be created and purchases
recorded before the terms exist; until then it shows a **Terms needed** badge and uses the
transaction prices as a fallback.

The rate schedule starts on the first date and has one entry per annual anniversary. A 29
February start uses 28 February in common years. Maturity may fall between anniversaries, so the
last period can be partial. Leave future rates out: they default to the last rate entered.

The displayed **model value (net of tax)** is a redeem-now estimate:

`net = face + interest − fee − tax`, where `tax = max(0, interest − fee) × tax rate` and the
early-redemption fee is capped at the accrued interest, so the value never falls below face.
Interest accrues actual/actual from each purchase date (undated legacy lots start at the first
schedule date; a partial final period is prorated over a full year). `capitalized_annually` compounds at period ends; `paid_out_annually` keeps only
the current period's accrued interest and resets when a coupon is paid; `at_maturity_simple`
adds simple interest. At or after maturity the value freezes and equals the projected net at
maturity.

**Face value vs. unit price.** The face value (in the terms) is the nominal the interest accrues
on. The **unit price** of a transaction is what you actually paid per bond, your cost basis: for
example 99.90 paid for a 100 bond on a rollover. Once terms exist, the transaction form starts
with the face value as the unit price; change it when you paid something else.

Sells consume the oldest purchase lots first for interest entitlement, regardless of the
workspace cost-basis setting. The projected value at maturity excludes the early-redemption fee.
Coupons paid out annually are not part of the bond value; record them as income in the ledger.

Once terms exist, the model value is the only price source: manual prices are refused and the
provider fields are not available for this instrument type. Each displayed line is rounded
independently to the currency's minor unit, so gross accrued minus fee and tax can differ from
net by one minor unit. These are estimates from the terms you enter, not investment advice.

Instruments are archived, not deleted. Archiving keeps their allocation and history and hides
them from the active list; restoring is refused if another active instrument has the same name.

## Transactions and holdings

Open an instrument by clicking its name, or use the **Transactions** tab, to record a **buy** or
**sell**:

- **Date** — a trade date, not a plan. A date after today is refused.
- **Quantity** — greater than zero, with up to 18 decimal places.
- **Unit price** — in the instrument's quote currency; zero is allowed. A currency instrument is
  always priced at 1, whatever is typed.
- **Fee** — in the same quote currency, with no more decimal places than the currency has. A
  sell's fee may be larger than its gross amount; the difference is a realized loss.
- **FX rate to the base currency** — required when the instrument is quoted in another currency;
  hidden and stored as 1 for a base-currency instrument. A new transaction starts with the
  stored rate for its date (the latest manual or provider rate on or before that date, marked
  "Stored rate for …") when one exists; you can change it. Changing the date re-fills a rate you
  have not touched with the stored rate for the new date, or clears it when none is stored, so
  a back-dated trade never keeps today's rate by accident; a rate you typed stays. Leaving it
  empty shows an error on the field and keeps everything you entered.
- **Note** — optional, up to 200 characters.

### Amounts in the base currency

Some brokers show only base-currency amounts (for example PLN) for an ETF quoted in USD or EUR.
For an instrument quoted in another currency, the form offers **Amounts in: USD | PLN** (the
instrument's quote currency and your base currency). Choose the base currency and enter:

- the quantity,
- the **unit price in the base currency** and the **fee in the base currency**, exactly as the
  broker shows them,
- the FX rate (pre-filled from stored FX as above).

The app keeps the base amounts you entered and also stores the quote unit price
(`base price ÷ rate`, 18 decimal places) and the quote fee (`base fee ÷ rate`, rounded to the
currency's minor unit). The cost of a buy is then exactly `quantity × base price + base fee`
(proceeds of a sell: `quantity × base price − base fee`), rounded once, so the broker's FX
spread is part of the cost. The derived quote price keeps the instrument's transaction price and
transaction FX rate correct (for example "≈ 398.77 USD" at the real rate, not "1 600 USD" at
rate 1).

In the history such a row shows the base amounts first, with the derived quote price (rounded to
the quote currency's minor unit) and the rate beside them:
`1600 PLN · ≈ 398.77 USD @ 4.0123`. Editing opens the row in the mode it was saved in. Switching
an existing row to the base currency pre-fills `quote price × rate` and `quote fee × rate`;
switching back keeps the derived quote values and drops the base amounts.

### Dividends, interest and splits

Choose **Dividend / interest** to record investment income. Enter the **gross amount** in the
instrument's quote currency and the optional **withholding** (tax withheld). The net amount is
gross − withholding. Tick **Record as income** to add the net to the ledger as income and choose
an income category: the form preselects the most recent dividend's category, otherwise the
suggested **Interest** category when it is active. The ledger row is managed through the
dividend — edit or delete it from the Investments transaction, not from the month page — and it
shows a **from investment income** badge there. Recording income for a dividend in a closed
month is refused; an unlinked dividend is not affected.

The **Income** tab shows gross, withholding, net and the number of payments per calendar year,
newest first. Linked ledger rows are not counted twice. Undated legacy dividends are excluded
from the years and noted.

Choose **Split** for a split or reverse split and enter the **new units** and **old units**: a
2-for-1 split is 2 and 1; a 1-for-10 reverse split is 1 and 10. The holding quantity scales and
the cost basis stays the same. Splits are not available for currency or bond-model instruments.
A price observed before a split is multiplied by old/new when it is carried forward, so the
value does not jump on the split date. Deleting or editing a split can leave a later sell
overselling; that is refused, like any other oversell.

### Bulk add

The **Bulk add** panel on the Transactions tab (editors and owners) takes many past trades at
once. Paste one transaction per line, fields separated by `;`:

```
date; instrument; type; quantity; unit price; fee; fx; amounts currency
```

- Spaces around fields are ignored and empty fields at the end may be left out. Blank lines and
  lines starting with `#` are skipped. At most 500 lines and 64 KiB.
- **date** — `YYYY-MM-DD` or your short date with a four-digit year (`1/15/2026` in English,
  `15.01.2026` in Polish); today or earlier. Bulk add cannot create undated lots.
- **instrument** — an active instrument's name or symbol, in any letter case. A name that
  matches more than one instrument, or none, is an error; bulk add never creates instruments.
- **type** — `buy` or `sell`; Polish `kupno`/`zakup` and `sprzedaż`/`sprzedaz` also work.
- **quantity**, **unit price**, **fee** — in the interface number format (`1 234,56` in
  Polish). The fee defaults to 0; a currency instrument ignores the price (always 1).
- **amounts currency** — blank or the instrument's quote currency for quote amounts; your base
  currency code for [amounts in the base currency](#amounts-in-the-base-currency). Any other code
  is an error.
- **fx** — needed for an instrument quoted in another currency. Leave it blank to use the stored
  rate for the date (the preview marks it "Stored rate for …"); without one the line says
  *FX rate needed*. For a base-currency instrument it must be blank or 1.

Example (base currency PLN):

```
# date; instrument; type; quantity; unit price; fee; fx; amounts currency
2026-01-15; VWCE; buy; 4; 1600,00; 5; 4,0123; PLN
2026-02-03; World ETF; buy; 2; 101,5
2026-03-01; vwce; sprzedaż; 1; 1700; 5; ; PLN
```

**Preview** lists every line with its values, the FX rate used and the total cost or proceeds in
the base currency, or its error. Nothing is created while any line has an error. **Create**
sends the same text again; the server checks it all again (including the instruments and FX
as they are now), then creates every row or none. A sell that would exceed the quantity held is
checked with the pasted rows merged into the existing history by date, so an oversell caused
only by the combination of pasted rows is caught too. The rows of one bulk add share a batch id
and one audit entry records how many were added. There is no batch undo; each row can be
deleted (and undone) as usual.

The instrument page shows the current quantity, average cost (per unit, in the base currency),
cost basis and realized P&L. Holdings are recomputed on read with the workspace's **Average cost**
or **FIFO** setting (Workspace settings → Investment cost basis); switching recalculates the
whole history and stores nothing derived.

A sell that would exceed the quantity held at that point is refused, and the error names the
failing sale's date. This is checked over the whole history on every change, including editing
an earlier buy, back-dating a sell, deleting a buy and undoing a deleted sell.

Deleting a transaction is a soft delete with the usual 10-second **Undo**; holdings ignore
deleted rows, but deleted rows still lock the base currency and the instrument's currency/type.
An archived instrument's history remains visible, but adding, editing, deleting or undoing its
transactions is refused until it is restored.

Undated legacy lots (created only by an import in a later update) count in current holdings but
are excluded from dated history until they get a date.

## Prices and FX

Each instrument page has a **Prices** panel: the latest price, a small line chart, a table of
points and — for editors — a form to add, change or delete **manual prices**. Manual prices are
the main source for real estate and other illiquid assets. A manual price cannot be dated after
today, and the form and its actions disappear while the instrument is archived. Saving a price
on a date that already has a manual point changes that point instead of adding a second one.

Prices are in the instrument's quote currency. A **currency** instrument is always priced at 1
in its own currency, so it has no manual price form; its base-currency value comes entirely from
FX. Transaction unit prices also appear as prices, labelled **Transaction**. They are derived
when the page is read, not stored, so editing or deleting a transaction immediately changes
them.

The effective price on a date is the latest observation on or before that date (carry forward).
On the same date the order is **manual > provider > transaction**; a newer date always wins.
A price older than the constant `DEFAULT_STALE_AFTER_DAYS` (5 days) is marked **Stale**. The
threshold is measured against the date being valued, not against today, so historical views do
not drift.

### FX convention

FX rates are stored as **base-currency units per one unit of the quote currency**. The workspace
base currency itself is always 1. To convert between two non-base currencies, divide through the
base currency:

```text
rate(from, to) = rate(base, from) / rate(base, to)
```

For example, with EUR as the base currency, a stored EUR/USD rate of 0.92 means one US dollar is
worth 0.92 euro. A transaction's `fx_to_base` follows the same convention and acts as the
lowest-priority FX source until provider rates arrive (it is never used to pre-fill a form). FX
rates older than the staleness
threshold are flagged the same way as prices. There is no FX entry form yet.

## Price providers

Provider access is **opt-in per workspace** and only an owner can change providers or keys.
Editors can assign an enabled provider to an instrument and run **Test lookup**.

- **NBP** publishes PLN-based FX tables and the official gold price in PLN per gram. Gold is
  converted to troy ounces for the default metal unit; an instrument using NBP gold must be
  quoted in PLN and uses the symbol `XAU`.
- **ECB** publishes EUR-based reference rates. ECB quotes "currency units per EUR", so the app
  inverts them to the project convention.
- **CoinGecko** quotes crypto directly in USD, EUR or PLN; the provider symbol is the CoinGecko
  coin id, for example `bitcoin`. A demo API key is optional. The required
  "Powered by CoinGecko" badge appears while the provider is enabled.
- **EODHD** is the optional keyed equities/ETFs path; the provider symbol is the ticker with its
  exchange suffix, for example `VWCE.DE`; the instrument's quote currency must match the
  exchange's currency. Its free tier has a **daily call budget** (20 by default, counted per
  UTC day per workspace). The app refreshes latest prices first and spends
  remaining calls on history; when the budget is used up it skips the rest and keeps the last
  known prices. Coverage of GPW, XETRA and LSE symbols is unverified until tested with a real
  key.
- **GoldAPI.io** is the optional keyed precious-metals path: `XAU`, `XAG`, `XPT` and `XPD` in
  the provider's documented currencies, including PLN, EUR, USD, GBP and CHF. Set the provider
  symbol to the metal code and the instrument currency to the currency you want quotes in.
  Prices are per troy ounce and are converted to the instrument's `oz`, `g` or `kg` unit. The
  API key is required; it is stored encrypted and never shown again. Its default budget is
  **4 calls/day** (UTC) to stay under the free plan's roughly 150 monthly requests; the owner can
  raise it on the provider card, which also shows the calls used today. Latest prices are
  fetched before history, and a test lookup uses one call. History is limited to the latest 90
  days: the first backfill fetches that window once, and the instrument shows the “history
  limited” note when the first transaction is older (enter older prices manually if you need
  them). No attribution badge is required; displaying prices inside the app is allowed, but
  reselling the data or building a competing API is not. The scheduled refresh runs at 22:50
  UTC.

Both providers are converted into the workspace base currency before they are stored whenever
the provider publishes that base. For example, NBP in a EUR workspace stores
`PLN per X ÷ PLN per EUR`; ECB in a PLN workspace stores `EUR per X ÷ EUR per PLN`. If the
provider does not publish the workspace base at all, FX from that provider is skipped and the
provider page shows a warning. The arithmetic uses an 80-digit decimal context; no value passes
through `float`.

The **Providers** tab (owners only) enables a provider and shows its last success, last error and
the number of instruments using it. Enabling a provider queues a one-off history backfill from
each instrument's first transaction date; the daily job fills anything still missing. "Refresh
now" fetches only the latest prices and FX rates and never waits behind a long rate limit: if the
provider cannot answer within a few seconds, the page says it is rate limited and when to try
again. Scheduled jobs may wait and are polite to the providers: NBP at most once per second,
ECB at most once per hour (its response cache covers the daily run), and both daily after market
close.

A failed refresh keeps the last known price. The failure is recorded in `provider_errors`
(pruned after 90 days) and the instrument shows a provider-error badge with the age of its last
known price. Manual prices still win; transactions continue to fill gaps.

History is backfilled once from each instrument's first transaction date. CoinGecko's
public/demo API and Basic plan limit how far back history goes (the current Basic plan is
restricted to the past two years per its documentation); the app requests `days=max`, stops at
whatever the plan returns, and records a **history limited** note instead of retrying forever.
EODHD history stores the raw `close`, falling back to `adjusted_close` only when `close` is
missing. The app adjusts raw prices across splits itself, so pre-F6.10 history that was
backfilled with adjusted closes may still show a step until it is backfilled again.

## Allocation

Every instrument's value is split across one or more asset classes with weights in percent. The
weights must total **exactly 100%**; an instrument with no allocation cannot be saved. The page
shows the running total and blocks the save button while it is not 100%, and the server checks
the same rule. A single instrument may be split, for example 80% Stocks and 20% Bonds for a
mixed fund.

An archived class that an instrument already uses stays visible in its allocation editor; you
can keep the weight or remove it, but you cannot add a new allocation to an archived class.
"Other" is the catch-all class.

## Portfolio

**Investments → Portfolio** values everything you hold today (quantity above zero), in the
workspace base currency. It is read-only.

- **Holdings**: instrument, quantity, average cost (cost basis ÷ quantity), the price used with
  its date, value, cost basis, unrealized gain (amount and %) and share of the portfolio.
  Archived instruments you still hold are listed with an *Archived* badge; instruments you have
  sold completely are not listed, but their realized P&L still counts.
- **Value** is quantity × price × FX rate, rounded once to the base currency's minor unit. Prices
  follow the usual priority (manual, provider, transaction; the newest date wins). Bond-model
  instruments use the model value; without bond terms they fall back to the last transaction
  price and show *Bond terms needed*. A currency instrument is priced at 1.
- **Stale data**: a price or FX rate older than five days is still used, with a *Stale price* or
  *Stale FX rate* badge.
- **Missing data**: an instrument with no price, or whose currency has no FX rate, stays in the
  table with a warning, but its value **and its cost basis** are left out of every total, the
  asset-class table and the chart, so the totals and gain % stay comparable. A note above the
  table says how many holdings are left out. Nothing is ever valued at zero by accident.
- **Totals**: value, cost basis, unrealized gain, and realized P&L for the current year (sells
  dated from 1 January in the workspace's time zone through today) and for all time, using the
  workspace's cost basis method. A sell of an undated imported lot counts in both when the sell
  itself is dated; an undated sell counts in the all-time figure only.
- **By asset class** (look-through): each instrument's value and cost are split by its
  allocation weights. An 80/20 Stocks/Bonds fund worth 1 000.00 adds exactly 800.00 to Stocks
  and 200.00 to Bonds. Rounding never loses or invents a cent: the class with the largest weight
  takes the remainder (on a tie, the class listed first on the Asset classes tab). Gain % is
  shown per class, or *—* when the class has no cost. A class that is not investable is marked
  *not counted in FIRE*; it still counts in every total and in the chart.
- **Class donut**: the totals box also shows a compact donut of the look-through value by asset
  class, in the classes' colours, including archived classes and an *Unallocated* slice for
  instruments without an allocation. Its total is the portfolio value.
- **Chart**: a donut of instrument values, titled *Holdings by instrument*. Choose its scope above
  it: one asset class (each instrument contributes only its part in that class) or the whole
  portfolio. It opens on the class created as *Stocks* (*Akcje*) when you hold something there,
  otherwise on the whole portfolio. Slices under 2% are grouped into *Other*. Slices use the
  chart palette; the class colours appear in the asset-class table. Without JavaScript the
  chart's table is shown.

Shares and percentages are rounded once for display, so they may not add up to exactly 100%.

## Risk balance

**Risk balance** on the Portfolio tab groups the included portfolio value into three tiers:
**Safe** (bonds, metals, deposits), **Growth** (broad funds, currencies) and **Speculative**
(play money: crypto and single-company shares). Each asset class has an optional tier; an
instrument can override it for its whole value. An instrument split 80/20 between a Growth and a
Safe class contributes 80% to Growth and 20% to Safe unless it has an override.

A class with no tier is **Unassigned**. Its value is shown separately and left out of the tier
percentages, so the three tiers always add up to 100% of the assigned value. An instrument
excluded from the portfolio because it has no price or FX rate is also excluded here. The
balance is recomputed on every page load; there is no daily cache. When anything is unassigned,
a warning under the table shows the unassigned amount and share, explains that the classes need
a risk tier and links to the Asset classes tab. If any of them match a suggested class name, the
warning also offers **Apply suggested risk tiers**, which fills only empty tiers in one click.

The workspace has a target policy, editable by editors and owners under **Edit target
allocation**:

- **Fixed targets** (the default): Safe 50%, Growth 40%, Speculative 10%. The three must total
  exactly 100%.
- **Age-scaled**: speculative is a fixed share (default 10%); Safe is
  `(100% − speculative) × age ÷ life expectancy`; Growth is the rest. Safe is capped at
  `100% − speculative` once age reaches life expectancy.
- **Age in bonds**: Safe is your age as a percentage, speculative is a fixed share (default 0%),
  and Growth is the rest; Safe is capped the same way.

Age and life expectancy come from **your Profile**, not from another member's profile: the
policy belongs to the workspace, but the age used for the targets belongs to whoever is
viewing it. Set **Birth year** and **Life expectancy** in Profile; age-based modes are disabled
until a birth year exists. The fixed targets stay stored when you switch modes, so switching
back does not lose them.

Per tier the table shows the current value, current share, target share and the difference in
percentage points and money. A tier is marked **Outside tolerance** when the absolute difference
exceeds the tolerance (default 5 percentage points). When a gap is outside tolerance, the section
suggests moving the smaller of the largest overweight and largest underweight amounts, for example
“Move about €300.00 from Growth to Safe”. The section is illustrative only and is not investment
advice.

Existing workspaces whose classes predate tiers can use **Apply suggested risk tiers** on the
Asset classes tab. It fills an empty tier when the class name matches a suggested class name in
English or Polish (including the older suggested set with Real estate and Primary residence).
It never overwrites a tier you set, and running it again changes nothing.

## Performance

**Investments → Performance** shows how the portfolio's value and invested capital changed over
time, and the money-weighted return for standard periods. It is read-only.

- **Series**: value and cumulative net invested capital, in the base currency. Quantity comes
  from dated buy/sell transactions and splits; undated legacy lots are excluded and a note links
  to the transaction history. Prices and FX rates carry forward from the latest observation on or
  before each point, with a price observed before a split multiplied by old/new. When a holding
  has no observation on or before a point, the chart backfills it from the first known later
  observation, so a price history that starts after the purchase does not draw a zero dip. A
  transaction-derived price and FX rate cover every instrument from its first buy.
- **Dividends and splits**: a dividend's net amount in the base currency is a positive XIRR cash
  flow and reduces net invested capital; splits are not cash flows. Neither is used as a
  transaction-price observation.
- **Ranges**: the chart opens on **1 year**. YTD, 3 years, 5 years and all time are also
  available. Ranges up to 90 days use daily points; longer ranges use weekly points, and an
  all-time range over five years uses monthly points. The end date is always included. Points
  before the first dated transaction are dropped, so the chart does not start with a flat zero
  prefix.
- **Class filter**: the default is the whole portfolio. Choosing an asset class applies the
  same look-through weights as the Portfolio tab to both value and invested capital, so an
  80/20 fund contributes 80% to Stocks. Class series add up exactly to the whole-portfolio
  series.
- **Bond models**: each point uses the F6.5 model value for the lots held at that date, not a
  static price series. Coupon resets and capitalisation therefore appear at the right dates.
- **Settlement rows**: amounts entered in the base currency use their exact stored base total for
  net invested and XIRR; the broker's FX spread is part of the flow.
- **Missing data**: if a held instrument has no price or FX rate at a point even after leading
  backfill, both its value and its net invested contribution are left out there and the point is
  marked incomplete. The chart draws a gap there (it does not connect across it) and the
  plotted-data table shows “—”, so missing data never appears as a fake zero. A period whose
  start or end is incomplete shows its value change with a warning and its XIRR is undefined.
- **Periods**: 1 month, 3 months, year to date, 1 year and all time. The table shows start and
  end value, absolute and percentage value change, net flows, **Δ value − net flows** (the gain
  not explained by deposits or withdrawals), and the return.
- **XIRR**: start value is a negative cashflow at the end of the start date (after that day's
  trades); buys after the start are negative and sells positive; the end value is positive.
  All-time starts at zero on the day before the first dated transaction, so every flow is
  included. XIRR uses actual/365. Periods shorter than 365 days show the period money-weighted
  return, `(1 + annual rate)^(days/365) − 1`, labelled *Period return*; periods of 365 days or
  more (1 year, or an all-time history at least that long) show the annualised XIRR.
- **Chart and table**: the value line and invested-capital line enclose the gain band. Without
  JavaScript the period table and the plotted-data table remain available.

## Net worth

Asset classes and individual holdings can feed **Net Worth → Assets** as linked items. New
workspaces keep one linked asset item per active class up to date automatically (the
**Keep investment asset classes linked in net worth automatically** setting under Workspace
settings). Existing workspaces start with it off and can turn it on. When it is off, an editor
or owner sees **Create net-worth items from asset classes** on the Assets page while at least
one eligible class has no link; it creates one linked asset item per active class with a value,
in an **Investments** category created on demand (pl **Inwestycje**). Empty classes, classes whose
value is unavailable because of a missing price or FX rate, and classes already linked are
skipped. Archiving a linked item is respected: neither automatic nor manual linking recreates
it. The **Link to…** picker also offers active instruments you currently hold. Sold-out and
archived targets are not offered for new links, but existing links keep resolving.

The linked value is read-only. For today it matches the **Portfolio** tab exactly, including
undated imported lots and the latest prices and FX rates. For a past snapshot or a backfill it
uses the **Performance** history: dated transactions only, carried-forward observations and the
per-date bond value. Undated lots are therefore absent from historical snapshots, so the first
month-end snapshot can be lower than today's value when such lots exist. A linked class is
**Value unavailable** when any held instrument in it lacks a price or FX rate; it never shows a
partial class total.

The Assets page warns about possible double counting when a class and one of its holdings are
both linked, or when a legacy real-estate instrument and a **Real estate** item both have a
positive value. Warnings never change a total. See `docs/net-worth.md` for the linked-item
details.

## Roles

Everyone in the workspace can read the Investments pages. Adding, editing, reordering,
archiving and restoring classes and instruments, and changing allocations, needs **editor** or
**owner**. Viewers see the lists without the forms.
