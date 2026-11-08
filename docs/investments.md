# Investments

The **Investments** section records what you hold outside the ledger and how its value is split
across asset classes. It is the foundation for the portfolio, price and performance views that
arrive in later updates; this page currently manages the structure.

> **Backups:** investments are not part of the workspace archive yet. An export taken before the
> investments archive extension does not contain asset classes, instruments, allocations,
> prices or FX rates.

## Asset classes

Asset classes group instruments for the portfolio and allocation views. A new workspace starts
with the suggested set:

| Class | Investable |
|---|---|
| Stocks | yes |
| Bonds | yes |
| Metals | yes |
| Crypto | yes |
| Currencies | yes |
| Real estate | no |
| Primary residence | no |
| Other | no |

**Investable** marks the classes that count as invested capital for the FIRE planner and the
net-worth helper. You can rename a class, change its colour or investable flag, and move it up
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
| Real estate | property | manual price, quantity 1 |
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
  hidden and stored as 1 for a base-currency instrument. A later update will offer the stored
  rate as the default.
- **Note** — optional, up to 200 characters.

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
lowest-priority FX source until provider rates arrive. FX rates older than the staleness
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
EODHD history uses adjusted closes, so splits and distributions do not create false price jumps.

## Allocation

Every instrument's value is split across one or more asset classes with weights in percent. The
weights must total **exactly 100%**; an instrument with no allocation cannot be saved. The page
shows the running total and blocks the save button while it is not 100%, and the server checks
the same rule. A single instrument may be split, for example 80% Stocks and 20% Bonds for a
mixed fund.

An archived class that an instrument already uses stays visible in its allocation editor; you
can keep the weight or remove it, but you cannot add a new allocation to an archived class.
"Other" is the catch-all class.

## Roles

Everyone in the workspace can read the Investments pages. Adding, editing, reordering,
archiving and restoring classes and instruments, and changing allocations, needs **editor** or
**owner**. Viewers see the lists without the forms.
