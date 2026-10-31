# ADR-0022: Tax kinds and payment schedule

**Status:** Proposed
**Date:** 2026-10-31

## Context

F3.3/F3.4 modelled a tax with an `effect` (`added` or `withheld`) and let every income source
choose a `record_as` mode (`cash_received`, `net`, `take_home`) that decided both the ledger
amount and which taxes got a planned payment. Two settings on two pages had to agree before one
tax behaved as intended, the words were accountancy terms, and the payment day, month offset
and category had to be repeated on every attachment. The owner approved a redesign (clickable
mockup, 2026-10-31) built around one question per tax: *who pays it to the tax office?* Taxes
also had to be optional in the menu, and taxes that do not depend on income (real estate,
vehicle) had no home.

## Decision

- **One `kind` per tax rule** replaces `effect` + `record_as`:
  - `added`: charged on top of what the user invoices (VAT). It increases what the user
    receives and a payment is planned.
  - `self_paid`: paid by the user from the income. What the user receives is unchanged and a
    payment is planned.
  - `payer_withheld`: taken off by the payer. It reduces what the user receives and nothing is
    planned.

  The income occurrence's expected amount is `net + added − payer_withheld`; planned payments are
  `added + self_paid` for attachments whose `generate_payment` is on. `base` (`net`, or `gross =
  net + added`) stays; `added` taxes are always on net. `income_sources.record_as` is dropped:
  the behaviour now belongs to each tax, so one source can mix a VAT, a self-paid contribution and
  a withheld tax, which no single `record_as` value could express.
- **The payment schedule lives on the rule**: `payment_day` (1–31), `payment_timing`
  (`next_month`, `same_month`, `after_quarter`: the month after the end of the pay date's
  calendar quarter) and `payment_category_id` (required for `added` and `self_paid`). An
  attachment's `due_day`, `due_month_offset` and `payment_category_id` become nullable overrides;
  NULL means the rule's default. The income page only offers the payment-day override.
- **Kind and base are editable** until posted income was calculated with the tax; then they are
  locked and the user is told to create a new tax. Rates stay append-only versions; the first
  version is valid from 1900-01-01 ("from the start").
- **Migration `0027_taxes_redesign`** maps `added` → `added`, and `withheld` → `payer_withheld`
  when any active source it is attached to recorded take-home income, otherwise `self_paid`.
  Rule defaults come from the first attachment (by creation time; offset 0 → `same_month`, any
  other → `next_month`), or day 10, next month and no category without attachments. Attachment
  values equal to the defaults become NULL. Downgrade is lossy: `record_as` returns as
  `take_home` for sources with a payer-withheld tax and `cash_received` otherwise (never `net`),
  `after_quarter` becomes a one-month offset, and an attachment that has no category anywhere
  gets the workspace's first non-transfer expense category (or is deleted without one).
- Mapping `withheld` + `cash_received` to `self_paid` keeps the old amounts exactly (received =
  gross, payments for every tax). `take_home` sources keep their received amount except that an
  added tax now increases it. `net` sources change: added taxes are now received and planned.
  The startup catch-up therefore recalculates every pending income occurrence and its tax
  payments (`resync_pending_income`) before generating; posted rows never change.
- **A workspace switch `taxes_enabled`** (owners, Workspace settings → Menu) hides the Taxes menu
  item, the tax pages (404) and the tax parts of income. It is on after the upgrade for
  workspaces that have a tax rule and off for new workspaces, and cannot be turned off while a
  non-archived income source has a tax attached.
- **Other taxes** are recurring items with `scheduled_items.is_tax`, created from the Taxes page
  (fixed-amount expense; yearly, quarterly = monthly every 3 months, monthly or once) and edited
  on the recurring item page.
- The separate calculator page is replaced by the example panel of the tax form (10 000 net,
  this tax only), rendered on the server and refreshed with htmx.

## Consequences

- One plain-language choice per tax; the income page only ticks taxes.
- The F3.3 golden example now has three variants by kind instead of three `record_as` modes; the
  invariant "received − planned payments = take-home" still holds for every input.
- Every pending income occurrence is recalculated at each start. This is idempotent and keeps
  pending rows in step with rate changes too.
- A downgrade cannot restore `net` recording or quarterly timing.
