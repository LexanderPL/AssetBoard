"""F8.5 scenario comparison: up to three saved plans projected side by side.

The comparison is read-only and viewer-accessible. One session loads every scenario detail and
all child rows in four queries, then resolves each scenario's F8.4 linked inputs through the
same session. Loan suggestions are deliberately not loaded: the comparison has no one-click
actions. Mixed currencies are a page error (no FX conversion); mixed start years are allowed and
reported so the page can explain the real-value caveat. Nothing is written or audited.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from assetboard.domain.fire import Projection
from assetboard.domain.money import Money
from assetboard.services.context import Context, Role, require_role
from assetboard.services.fire_linked import ResultsBlock, linked_inputs_in_session
from assetboard.services.fire_projection import results_from_detail
from assetboard.services.fire_scenarios import FireScenarioView, details_in_session

MAX_COMPARISON_SCENARIOS = 3


@dataclass(frozen=True, slots=True)
class FireComparisonRow:
    """One selected scenario; a blocked plan keeps its messages instead of a projection."""

    scenario: FireScenarioView
    projection: Projection | None
    blocks: tuple[ResultsBlock, ...] = ()

    @property
    def blocked(self) -> bool:
        return bool(self.blocks)

    @property
    def never(self) -> bool:
        """The plan projects but never reaches FIRE before the horizon."""
        return self.projection is not None and self.projection.fire_age is None

    @property
    def fire_age(self) -> int | None:
        return self.projection.fire_age if self.projection is not None else None

    @property
    def fire_year(self) -> int | None:
        return self.projection.fire_year if self.projection is not None else None

    @property
    def years_to_fire(self) -> int | None:
        return self.projection.years_to_fire if self.projection is not None else None

    @property
    def real_networth_at_fire(self) -> Money | None:
        if self.projection is None:
            return None
        return self.projection.real_networth_at_fire

    @property
    def depletion_age(self) -> int | None:
        return self.projection.depletion_age if self.projection is not None else None

    @property
    def depletion_checked(self) -> bool:
        """Whether any year after income stops falls inside the projection's horizon."""
        return self.projection is not None and self.projection.min_networth_after_stop is not None


@dataclass(frozen=True, slots=True)
class FireComparison:
    rows: tuple[FireComparisonRow, ...]
    selected_ids: tuple[UUID, ...]
    error_code: str | None = None
    mixed_start_years: bool = False

    @property
    def has_projection(self) -> bool:
        return any(row.projection is not None for row in self.rows)


def compare_fire_scenarios(
    factory: sessionmaker[Session], ctx: Context, scenario_ids: tuple[UUID, ...]
) -> FireComparison:
    """Project up to three saved scenarios in the given order; viewers may read.

    Duplicate ids are removed while the first occurrence keeps its position. More than
    ``MAX_COMPARISON_SCENARIOS`` is reported as ``too_many_scenarios`` without loading any row.
    A missing or cross-workspace scenario raises ``NotFound``. A mixed-currency selection is
    reported as ``currency_mismatch`` with no rows: there is no FX conversion in F8.5.
    """
    require_role(ctx, Role.VIEWER)
    ordered = tuple(dict.fromkeys(scenario_ids))
    if len(ordered) > MAX_COMPARISON_SCENARIOS:
        return FireComparison(
            rows=(),
            selected_ids=ordered,
            error_code="too_many_scenarios",
        )
    if not ordered:
        return FireComparison(rows=(), selected_ids=())
    with factory() as session:
        details = details_in_session(session, ctx, ordered)
        if len({detail.scenario.currency for detail in details}) > 1:
            return FireComparison(
                rows=(),
                selected_ids=ordered,
                error_code="currency_mismatch",
            )
        rows: list[FireComparisonRow] = []
        for detail in details:
            scenario = detail.scenario
            linked = (
                linked_inputs_in_session(
                    session,
                    ctx,
                    currency=scenario.currency,
                    start_year=scenario.start_year,
                    income_mode=scenario.income_mode,
                    expenses_mode=scenario.expenses_mode,
                    portfolio_mode=scenario.portfolio_mode,
                    expense_exclusion_ids=detail.expense_exclusion_ids,
                )
                if scenario.linked
                else None
            )
            results = results_from_detail(detail, linked=linked)
            rows.append(
                FireComparisonRow(
                    scenario=scenario,
                    projection=results.projection,
                    blocks=results.blocks,
                )
            )
    return FireComparison(
        rows=tuple(rows),
        selected_ids=ordered,
        mixed_start_years=len({detail.scenario.start_year for detail in details}) > 1,
    )


__all__ = [
    "FireComparison",
    "FireComparisonRow",
    "MAX_COMPARISON_SCENARIOS",
    "compare_fire_scenarios",
]
