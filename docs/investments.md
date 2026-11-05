# Investments

The **Investments** section records what you hold outside the ledger and how its value is split
across asset classes. It is the foundation for the portfolio, price and performance views that
arrive in later updates; this page currently manages the structure.

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
| Currency | the currency code | FX rate to the base currency |
| Real estate | property | manual price, quantity 1 |
| Other | unit | manual price |

A **Bond (model value)** instrument can be created before its terms exist. It shows a "Terms
needed" badge and cannot be valued until the bond-terms form arrives in a later step.

Instruments are archived, not deleted. Archiving keeps their allocation and history and hides
them from the active list; restoring is refused if another active instrument has the same name.

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
