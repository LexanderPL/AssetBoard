"""FIRE scenario results (F8.3/F8.4): save-free projection of a scenario's inputs.

The service is the only caller of :func:`assetboard.domain.fire.project` for a saved scenario. It
maps the stored rows to the pure projection input, selects a manual or linked starting value,
turns missing data or an invalid saved plan into blocking results the page can explain, and
validates a posted form without writing anything for the live preview. Archived scenarios stay
projectable; only edits are refused.
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
from assetboard.services.fire_linked import (
    ExpenseExclusionError,
    LinkedInputs,
    LoanPayoffSuggestion,
    ResultsBlock,
    linked_inputs_in_session,
    loan_payoff_suggestions_in_session,
    validate_expense_exclusions_in_session,
)
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
class FireScenarioResults:
    detail: FireScenarioDetail
    projection: Projection | None
    linked: LinkedInputs | None = None
    blocks: tuple[ResultsBlock, ...] = ()
    loan_suggestions: tuple[LoanPayoffSuggestion, ...] = ()
    preview: bool = False
    needs_extreme_confirmation: bool = False

    @property
    def blocked(self) -> ResultsBlock | None:
        """The first block, kept for F8.3 callers that understood a single block."""
        return self.blocks[0] if self.blocks else None

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


def _linked_total(linked: LinkedInputs | None, direction: str) -> Money | None:
    if linked is None:
        return None
    if direction == "portfolio":
        return linked.portfolio.total if linked.portfolio is not None else None
    series = linked.income if direction == "income" else linked.expenses
    return series.annual_total if series is not None else None


def _starting_amount(
    field: str,
    mode: str,
    amount: Money | None,
    linked: LinkedInputs | None,
    direction: str,
) -> int:
    if mode == "manual":
        if amount is None:
            raise MissingStartingValue(field)
        return amount.amount_minor
    resolved = _linked_total(linked, direction)
    if resolved is None:
        raise MissingStartingValue(field)
    return resolved.amount_minor


def projection_inputs(
    scenario: FireScenarioView,
    contributions: tuple[ContributionView, ...],
    events: tuple[EventView, ...],
    *,
    linked: LinkedInputs | None = None,
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
            _starting_amount(
                "income_manual", scenario.income_mode, scenario.income_manual, linked, "income"
            ),
            currency,
        ),
        start_expenses=Money(
            _starting_amount(
                "expenses_manual",
                scenario.expenses_mode,
                scenario.expenses_manual,
                linked,
                "expenses",
            ),
            currency,
        ),
        start_portfolio=Money(
            _starting_amount(
                "portfolio_manual",
                scenario.portfolio_mode,
                scenario.portfolio_manual,
                linked,
                "portfolio",
            ),
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
    scenario: FireScenarioView,
    validated: ValidatedScenario,
    *,
    linked: LinkedInputs | None = None,
) -> ProjectionInputs:
    """Map a validated unsaved form to the pure projection's input."""
    currency = scenario.currency

    def value(mode: str, manual: int | None, direction: str, field: str) -> int:
        if mode == "manual":
            if manual is None:
                raise MissingStartingValue(field)
            return manual
        resolved = _linked_total(linked, direction)
        if resolved is None:
            raise MissingStartingValue(field)
        return resolved.amount_minor

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
        start_income=Money(
            value(
                validated.income_mode.value,
                validated.income_manual_minor,
                "income",
                "income_manual",
            ),
            currency,
        ),
        start_expenses=Money(
            value(
                validated.expenses_mode.value,
                validated.expenses_manual_minor,
                "expenses",
                "expenses_manual",
            ),
            currency,
        ),
        start_portfolio=Money(
            value(
                validated.portfolio_mode.value,
                validated.portfolio_manual_minor,
                "portfolio",
                "portfolio_manual",
            ),
            currency,
        ),
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
    detail: FireScenarioDetail,
    *,
    linked: LinkedInputs | None = None,
    loan_suggestions: tuple[LoanPayoffSuggestion, ...] = (),
    preview: bool = False,
) -> FireScenarioResults:
    """Project a loaded scenario; missing data or an invalid saved plan becomes a block."""
    if linked is not None and linked.blocks:
        return FireScenarioResults(
            detail=detail,
            projection=None,
            linked=linked,
            blocks=linked.blocks,
            loan_suggestions=loan_suggestions,
            preview=preview,
        )
    try:
        inputs = projection_inputs(
            detail.scenario, detail.contributions, detail.events, linked=linked
        )
    except MissingStartingValue as exc:
        return FireScenarioResults(
            detail=detail,
            projection=None,
            linked=linked,
            blocks=(ResultsBlock(code="missing_starting_value", field=exc.field),),
            loan_suggestions=loan_suggestions,
            preview=preview,
        )
    try:
        projection = project(inputs)
    except FireProjectionError as exc:
        return FireScenarioResults(
            detail=detail,
            projection=None,
            linked=linked,
            blocks=(ResultsBlock(code="invalid_projection", field=exc.field),),
            loan_suggestions=loan_suggestions,
            preview=preview,
        )
    return FireScenarioResults(
        detail=detail,
        projection=projection,
        linked=linked,
        loan_suggestions=loan_suggestions,
        preview=preview,
    )


def scenario_results(
    factory: sessionmaker[Session], ctx: Context, scenario_id: UUID
) -> FireScenarioResults:
    """One scenario's projection, linked inputs and loan suggestions for the results page."""
    detail = get_fire_scenario(factory, ctx, scenario_id)
    scenario = detail.scenario
    has_linked = (
        scenario.income_mode != "manual"
        or scenario.expenses_mode != "manual"
        or scenario.portfolio_mode != "manual"
    )
    with factory() as session:
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
            if has_linked
            else None
        )
        suggestions = loan_payoff_suggestions_in_session(
            session,
            ctx,
            currency=scenario.currency,
            start_year=scenario.start_year,
            last_year=scenario.birth_year + scenario.horizon_age,
            existing_events=tuple((event.year, event.kind, event.label) for event in detail.events),
        )
    return results_from_detail(detail, linked=linked, loan_suggestions=suggestions)


def _resolve_preview_linked(
    factory: sessionmaker[Session] | None,
    ctx: Context,
    saved: FireScenarioView,
    validated: ValidatedScenario,
) -> LinkedInputs | None:
    has_linked = (
        validated.income_mode.value != "manual"
        or validated.expenses_mode.value != "manual"
        or validated.portfolio_mode.value != "manual"
    )
    if not validated.expense_exclusion_ids and not has_linked:
        return None
    if factory is None:
        raise FireError("income_manual", "linked_mode_unavailable")
    with factory() as session:
        try:
            exclusion_ids = validate_expense_exclusions_in_session(
                session, ctx, validated.expense_exclusion_ids
            )
        except ExpenseExclusionError:
            raise FireError("expense_exclusions", "invalid_expense_exclusion") from None
        if not has_linked:
            return None
        return linked_inputs_in_session(
            session,
            ctx,
            currency=saved.currency,
            start_year=saved.start_year,
            income_mode=validated.income_mode.value,
            expenses_mode=validated.expenses_mode.value,
            portfolio_mode=validated.portfolio_mode.value,
            expense_exclusion_ids=exclusion_ids,
        )


def preview_scenario_results(
    ctx: Context,
    detail: FireScenarioDetail,
    scenario: FireScenarioInput,
    *,
    factory: sessionmaker[Session] | None = None,
) -> FireScenarioResults:
    """Validate and project a posted form without writing; editors only, never archived.

    Name uniqueness is not checked: another scenario's name must not block a preview. An unusual
    changed rate is allowed but reported through ``needs_extreme_confirmation`` so the fragment can
    warn that saving still needs the confirmation box. Linked modes and exclusions are resolved
    from the posted form without writing; ``factory`` is required for them.
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
    linked = _resolve_preview_linked(factory, ctx, saved, validated)
    needs_confirmation = (
        validated.needs_extreme_confirmation and not scenario.extreme_rates_confirmed
    )
    if linked is not None and linked.blocks:
        return FireScenarioResults(
            detail=detail,
            projection=None,
            linked=linked,
            blocks=linked.blocks,
            preview=True,
            needs_extreme_confirmation=needs_confirmation,
        )
    try:
        inputs = inputs_from_validated(saved, validated, linked=linked)
        projection = project(inputs)
    except MissingStartingValue as exc:
        return FireScenarioResults(
            detail=detail,
            projection=None,
            linked=linked,
            blocks=(ResultsBlock(code="missing_starting_value", field=exc.field),),
            preview=True,
            needs_extreme_confirmation=needs_confirmation,
        )
    except FireProjectionError as exc:
        raise FireError(exc.field, exc.code) from None
    return FireScenarioResults(
        detail=detail,
        projection=projection,
        linked=linked,
        preview=True,
        needs_extreme_confirmation=needs_confirmation,
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
