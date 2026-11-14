# ADR-0025: FIRE projection numeric representation and rounding

**Status:** Accepted
**Date:** 2026-11-13

## Context

M8 F8.2 projects a FIRE plan from integer basis points and integer minor units, and must reproduce
the source spreadsheet's numbers exactly. Money and rates cannot use `float`, and the projection
mixes compounding (income growth, inflation), a recursive portfolio balance, and signed user
overrides and events. Every ambiguous rounding point changes a later year's balance, so the rule
has to be fixed, documented and testable against an independent oracle.

## Decision

- All money is `Money(amount_minor: int, currency)`; every rate is integer basis points (1 bp =
  0.01%); ages and years are integers. The projection engine uses no floating point.
- There is **one half-up rounding per computed line**: base income and expense growth, each event
  component, the annual gain, each real (deflated) field, the FIRE number and the progress ratios.
  `net`, `net_worth`, cash sums and other totals are exact additions of already-rounded integers.
- `round_ratio(n, d)` rounds half **away from zero**: `(2n + d) // (2d)` when `n >= 0`, and
  `-((-2n + d) // (2d))` when `n < 0`. The denominator must be positive. Negative values are
  never clamped to zero.
- Growth and deflation use the closed form once per line:
  `inflate(v, bp, t) = round_ratio(v * (10000 + bp)^t, 10000^t)` and
  `deflate(v, i, t) = round_ratio(v * 10000^t, (10000 + i)^t)`. Growing a balance iteratively and
  rounding every year would drift; the spreadsheet keeps full precision in its cells, so its
  iterative product equals this closed form and only the displayed line is rounded.
- The nominal gain rate is the **additive** `inflation + real return`, not the compounded
  `(1 + inflation)(1 + real return) - 1`, because that is what the source spreadsheet's formulas
  say. The gain is `round_ratio(opening * (i + r), 10000)`, where the opening is the previous
  row's net worth (or the starting portfolio at `t = 0`).
- The `t = 0` convention is the spreadsheet's: the net column holds the starting portfolio,
  `gain_0` is computed on it, and `net_worth_0 = net_0 + gain_0 + cash_0`. From `t >= 1`,
  `net_worth_t = net_worth_{t-1} + net_t + gain_t + cash_t`. Cash in a year is added **after**
  that year's gain and therefore earns only from the following year.
- Events are separate components of the annual series. `income_change` amounts are inflated with
  income growth, `expense_change` amounts with inflation, both from the scenario's start year
  (today's money), and are active from their event year onward. Events of the same kind and year
  are summed before the single inflation. Cash events are nominal in their event year.
- Real values are one independent deflation step per nominal line and year:
  `real = deflate(nominal, i, t)`. They are display values and are not expected to satisfy the
  nominal recurrence exactly after rounding.
- Undefined metrics are `None` rather than zero. A negative opening net worth blocks the FIRE
  predicate. The `returns_cover_expenses` rule uses the strict `expenses_t < gain_t`; because the
  gain is earned on the previous year's balance, the FIRE row is marked one year after the
  theoretical moment returns first cover that year's expenses. This timing artifact is the
  spreadsheet's definition and is locked by the golden test. The `swr` rule uses the inclusive
  `opening_t * swr_bp >= expenses_t * 10000`.
- Inputs are validated to `MAX_AMOUNT_MINOR`, the hard rate bounds, the supported year range and
  the plan windows, and the currency is canonicalized to uppercase in the returned
  `Projection.inputs`. Invalid inputs raise `FireProjectionError(field, code)`; the engine is pure
  and never reads a clock, database or environment.

## Consequences

- Every platform and database reproduces the same integers; Decimal context and float drift
  cannot change a plan.
- Rounding once per line can differ by one minor unit from an unrounded mathematical model. The
  golden test against a `Fraction` oracle accepts exactly the rule above.
- The additive gain and the strict-`<` FIRE row are parity choices. Changing either is a product
  decision and needs a spec change, a new ADR and new golden data.
- Real values are display-only; F8.3 and F8.5 must label them as today's money rather than use
  them to rebuild the nominal recurrence.
- Closed-form powers create large intermediate integers, but a 120-year horizon keeps the work
  negligible for a household plan.
