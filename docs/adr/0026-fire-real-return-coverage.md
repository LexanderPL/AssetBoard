# ADR-0026: FIRE real-return coverage and exact nominal compounding

**Status:** Accepted
**Date:** 2026-11-16

## Context

ADR-0025 recorded the F8.2 parity model: the annual gain used the additive
`inflation + real return` rate and the returns-cover rule fired when `expenses_t < gain_t`.
That made the FIRE decision depend on the portfolio's **nominal** gain, so it could declare
independence while the portfolio's real capital was shrinking. The owner approved the F8.19
model correction: the rule must mean that the return above inflation covers the year's
expenses, and the nominal gain must compound the two rates instead of adding them. ADR-0025
explicitly asked for a spec change and a new ADR when either parity choice changed.

## Decision

- The nominal gain rate is the exact compounding of inflation and the return above inflation:
  `(1 + inflation) * (1 + real return) - 1`. The engine keeps integer basis points and rounds
  once per row with the ADR-0025 half-up rule:
  `gain = round_ratio(opening * (inflation_bp*10000 + real_return_bp*10000 +
  inflation_bp*real_return_bp), 100_000_000)`.
- `returns_cover_expenses` fires on the first row with a non-negative opening net worth where
  `opening * real_return_bp > expenses * 10000` (strict). It compares the return above
  inflation with nominal expenses, so FIRE preserves real capital. The withdrawal-rate rule is
  unchanged.
- The FIRE number under that rule is `expenses * 10000 / real_return_bp`, rounded once, and is
  undefined when FIRE is never reached or the real return is not positive.
- An effective expense line below zero (from an expense change or an included loan drop) is a
  projection error, not a FIRE row. The error carries the row's year. Preview and save surface
  it, and a sensitivity cell that would produce negative expenses fails the grid visibly.
- Sensitivity scales the whole expense line — starting expenses, every expense-change event and
  every loan payment — with one rounding per component. Income, cash and saving overrides keep
  their saved values.

## Consequences

- A higher nominal gain no longer makes an unconservative FIRE decision; the rule now answers
  the real-capital question the label promises.
- Existing projected values, golden data and the independent oracle change. The new expected
  values are derived from the exact formula, not from the implementation.
- A plan with a very large expense reduction or an included loan payment larger than the
  remaining expense line is refused with the affected year instead of displaying misleading
  results or a negative-expense FIRE row.
- The projection still uses no floating point, one rounding per computed line and the same
  recurrence; ADR-0025's storage, rounding and closed-form growth rules remain in force.
