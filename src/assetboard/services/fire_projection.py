"""FIRE scenario results (F8.3): save-free projection of a scenario's inputs.

The service is the only caller of :func:`assetboard.domain.fire.project` for a saved scenario. It
maps the stored rows to the pure projection input, turns a missing starting value or an invalid
saved plan into a blocking result the page can explain, and validates a posted form without
writing anything for the live preview. Archived scenarios stay projectable; only edits are
refused.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from assetboard.domain.fire import (
    ContributionOverride,
    EventKind,
    FireProjectionError,
    Projection,
    ProjectionEvent,
    ProjectionInputs,
    Rule,
    project,
)
from assetboard.domain.money import Money
from assetboard.services.context import Context, Role, require_role
from assetboard.services.fire_scenarios import (
    ContributionView,
    EventView,
    FireError,
    FireScenarioDetail,
    FireScenarioInput,
    FireScenarioView,
    ValidatedScenario,
    get_fire_scenario,
    validate_scenario_input,
)


@dataclass(frozen=True, slots=True)
class ResultsBlock:
    """Why a saved scenario has no projection, and which input the page should point at."""

    code: str
    field: str


@dataclass(frozen=True, slots=True)
class FireScenarioResults:
    detail: FireScenarioDetail
    projection: Projection | None
    blocked: ResultsBlock | None = None
    preview: bool = False
    needs_extreme_confirmation: bool = False

    @property
    def show_cash(self) -> bool:
        """The cash column appears only when the projection has a non-zero cash flow."""
        return self.projection is not None and any(
            row.cash.amount_minor for row in self.projection.rows
        )


class MissingStartingValue(Exception):
    """A linked input mode has no resolved value yet; never fall back to zero."""

    def __init__(self, field: str) -> None:
        super().__init__(field)
        self.field = field


def _starting_minor(field: str, mode: str, amount: Money | None) -> int:
    if mode != "manual" or amount is None:
        raise MissingStartingValue(field)
    return amount.amount_minor


def projection_inputs(
    scenario: FireScenarioView,
    contributions: tuple[ContributionView, ...],
    events: tuple[EventView, ...],
) -> ProjectionInputs:
    """Map a saved scenario and its child rows to the pure projection's input."""
    currency = scenario.currency
    return ProjectionInputs(
        currency=currency,
        birth_year=scenario.birth_year,
        start_year=scenario.start_year,
        horizon_age=scenario.horizon_age,
        stop_working_age=scenario.stop_working_age,
        inflation_bp=scenario.inflation_bp,
        real_return_bp=scenario.real_return_bp,
        income_growth_bp=scenario.income_growth_bp,
        rule=Rule(scenario.rule),
        swr_bp=scenario.swr_bp,
        start_income=Money(
            _starting_minor("income_manual", scenario.income_mode, scenario.income_manual),
            currency,
        ),
        start_expenses=Money(
            _starting_minor("expenses_manual", scenario.expenses_mode, scenario.expenses_manual),
            currency,
        ),
        start_portfolio=Money(
            _starting_minor("portfolio_manual", scenario.portfolio_mode, scenario.portfolio_manual),
            currency,
        ),
        contributions=tuple(
            ContributionOverride(year=row.year, amount=row.amount) for row in contributions
        ),
        events=tuple(
            ProjectionEvent(year=row.year, kind=EventKind(row.kind), amount=row.amount)
            for row in events
        ),
    )


def inputs_from_validated(
    scenario: FireScenarioView, validated: ValidatedScenario
) -> ProjectionInputs:
    """Map a validated unsaved form to the pure projection's input."""
    currency = scenario.currency
    return ProjectionInputs(
        currency=currency,
        birth_year=validated.birth_year,
        start_year=scenario.start_year,
        horizon_age=validated.horizon_age,
        stop_working_age=validated.stop_working_age,
        inflation_bp=validated.inflation_bp,
        real_return_bp=validated.real_return_bp,
        income_growth_bp=validated.income_growth_bp,
        rule=validated.rule,
        swr_bp=validated.swr_bp,
        start_income=Money(validated.income_manual_minor, currency),
        start_expenses=Money(validated.expenses_manual_minor, currency),
        start_portfolio=Money(validated.portfolio_manual_minor, currency),
        contributions=tuple(
            ContributionOverride(year=row.year, amount=Money(row.amount_minor, currency))
            for row in validated.contributions
        ),
        events=tuple(
            ProjectionEvent(
                year=row.year,
                kind=EventKind(row.kind),
                amount=Money(row.amount_minor, currency),
            )
            for row in validated.events
        ),
    )


def results_from_detail(
    detail: FireScenarioDetail, *, preview: bool = False
) -> FireScenarioResults:
    """Project a loaded scenario; a missing input or invalid saved plan becomes a block."""
    try:
        inputs = projection_inputs(detail.scenario, detail.contributions, detail.events)
    except MissingStartingValue as exc:
        return FireScenarioResults(
            detail=detail,
            projection=None,
            blocked=ResultsBlock(code="missing_starting_value", field=exc.field),
            preview=preview,
        )
    try:
        projection = project(inputs)
    except FireProjectionError as exc:
        return FireScenarioResults(
            detail=detail,
            projection=None,
            blocked=ResultsBlock(code="invalid_projection", field=exc.field),
            preview=preview,
        )
    return FireScenarioResults(detail=detail, projection=projection, preview=preview)


def scenario_results(
    factory: sessionmaker[Session], ctx: Context, scenario_id: UUID
) -> FireScenarioResults:
    """One scenario's projection for the results page and the CSV (three queries)."""
    detail = get_fire_scenario(factory, ctx, scenario_id)
    return results_from_detail(detail)


def preview_scenario_results(
    ctx: Context, detail: FireScenarioDetail, scenario: FireScenarioInput
) -> FireScenarioResults:
    """Validate and project a posted form without writing; editors only, never archived.

    Name uniqueness is not checked: another scenario's name must not block a preview. An unusual
    changed rate is allowed but reported through ``needs_extreme_confirmation`` so the fragment can
    warn that saving still needs the confirmation box.
    """
    require_role(ctx, Role.EDITOR)
    saved = detail.scenario
    if saved.archived:
        raise FireError("scenario", "archived")
    validated = validate_scenario_input(
        scenario,
        start_year=saved.start_year,
        currency=saved.currency,
        default_horizon=saved.horizon_age,
        previous_rates=(
            saved.inflation_bp,
            saved.real_return_bp,
            saved.income_growth_bp,
            saved.swr_bp,
        ),
        preview=True,
    )
    inputs = inputs_from_validated(saved, validated)
    try:
        projection = project(inputs)
    except FireProjectionError as exc:
        raise FireError(exc.field, exc.code) from None
    return FireScenarioResults(
        detail=detail,
        projection=projection,
        preview=True,
        # Once the box is ticked, saving is no longer blocked, so the preview should not keep
        # telling the user to tick it.
        needs_extreme_confirmation=(
            validated.needs_extreme_confirmation and not scenario.extreme_rates_confirmed
        ),
    )


__all__ = [
    "FireScenarioResults",
    "MissingStartingValue",
    "ResultsBlock",
    "inputs_from_validated",
    "preview_scenario_results",
    "projection_inputs",
    "results_from_detail",
    "scenario_results",
]
