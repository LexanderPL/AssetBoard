# ADR-0027: Optional horizon-based FIRE independence rule

**Status:** Accepted
**Date:** 2026-11-17

## Context

The two existing independence rules preserve capital in different ways: **Returns cover expenses**
requires the return above inflation to cover the year's expenses, and **Withdrawal rate** applies a
chosen withdrawal percentage. Neither answers the narrower question “can I stop working now and
still pay my expenses until the plan horizon, even if the principal is spent?”.

The owner asked for that question as an optional third rule, but explicitly did **not** want it to
become the default. The rule must not reuse the plan's configured income-stop age when testing an
earlier retirement age; otherwise future salary would make an early retirement look viable.

## Decision

- Add `lasts_to_horizon` as a third `Rule`. It stores no withdrawal rate.
- For each candidate age from the current age through the earlier of the configured
  **Income stops at age** and **horizon age − 1**, rebuild the projection with base income
  stopping at that candidate age. Keep every other input and event as entered.
- The candidate is viable when its opening balance and every net worth from that row through the
  horizon are non-negative. Zero at the horizon counts as lasting. The first viable candidate is
  the FIRE row.
- The cap is deliberate: the rule cannot declare FIRE after base income has already stopped, and
  a plan that only survives by working past its configured income-stop age is not independent
  under the plan as entered.
- The displayed projection still follows the configured income-stop age. The rule's counterfactual
  is visible only through the FIRE marker, FIRE age/year and years-to-FIRE metrics.
- The rule has no rate-based FIRE number and no net-worth-target plan progress. Both are `None`
  with a rule-specific explanation; net worth at FIRE remains available.
- The database check constraints accept the value and require a null `swr_bp`. The downgrade
  converts horizon-rule plans to `returns_cover_expenses` because the old schema cannot represent
  the choice.
- Archive format 2.2 adds the rule value; 2.0 and 2.1 archives remain readable.

## Consequences

- The rule can declare FIRE earlier than the capital-preserving rules because spending principal
  is allowed until the horizon. The projection stays deterministic: it uses the entered constant
  rates, not a set of market paths, so it is one illustrative path. It is not a safe-withdrawal,
  longevity or real-capital guarantee.
- A candidate simulation costs one projection per candidate age. The horizon is capped at age 120
  and all arithmetic stays integer; the normal projection and the other two rules are unchanged.
- Sensitivity, preview and save use the same rule automatically. The FIRE number and plan-progress
  cards explain that they do not apply instead of showing a misleading zero.
- **Returns cover expenses** remains the default for new plans, and saved plans keep their rule
  until the user selects another.
