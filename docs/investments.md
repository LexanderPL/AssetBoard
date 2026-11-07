# Investments

The **Investments** section records what you hold outside the ledger and how its value is split
across asset classes. It is the foundation for the portfolio, price and performance views that
arrive in later updates; this page currently manages the structure.

> **Backups:** investments are not part of the workspace archive yet. An export taken before the
> investments archive extension does not contain asset classes, instruments or allocations.

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

A **Bond (model value)** instrument can be created before its terms exist. It shows a "Terms
needed" badge and cannot be valued until the bond-terms form arrives in a later step.

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
