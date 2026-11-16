"""The service between a saved FIRE plan and the pure projection (F8.8).

The plan is a current-year projection: the service passes the injected clock's year as the
projection start year and only active events. Past events stay stored and are shown as archived
history, never applied. A currency mismatch or an invalid saved plan becomes a blocking result the
page can explain instead of a zero or a server error.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from assetboard.domain.fire import (
    EventKind,
    FireProjectionError,
    Projection,
    ProjectionEvent,
    ProjectionInputs,
    Rule,
    project,
)
from assetboard.domain.money import Money
from assetboard.services import fire_loans
from assetboard.services.context import Context, Role, require_role
from assetboard.services.fire_loans import FireLoanData
from assetboard.services.fire_scenarios import (
    EventView,
    FireError,
    FirePlanDetail,
    FirePlanInput,
    FirePlanView,
    SavedEvent,
    get_fire_plan,
    validate_plan_input,
)


@dataclass(frozen=True, slots=True)
class ResultsBlock:
    code: str
    field: str | None = None
    detail: str | None = None
    year: int | None = None


@dataclass(frozen=True, slots=True)
class FirePlanResults:
    detail: FirePlanDetail
    projection: Projection | None
    loans: FireLoanData | None = None
    blocks: tuple[ResultsBlock, ...] = ()
    preview: bool = False
    needs_extreme_confirmation: bool = False

    @property
    def blocked(self) -> ResultsBlock | None:
        """The first block, kept for callers that understood a single block."""
        return self.blocks[0] if self.blocks else None

    @property
    def show_cash(self) -> bool:
        return self.projection is not None and any(
            row.cash.amount_minor for row in self.projection.rows
        )


# Old name kept for F8.1-F8.6 callers and tests during the one-plan transition.
FireScenarioResults = FirePlanResults


def projection_inputs(
    plan: FirePlanView,
    events: tuple[EventView, ...],
    *,
    current_year: int,
    loans: FireLoanData | None = None,
) -> ProjectionInputs:
    """Map the live plan, its active events and live loans to the pure projection's input."""
    currency = plan.currency
    return ProjectionInputs(
        currency=currency,
        birth_year=plan.birth_year,
        start_year=current_year,
        horizon_age=plan.horizon_age,
        stop_working_age=plan.stop_working_age,
        inflation_bp=plan.inflation_bp,
        real_return_bp=plan.real_return_bp,
        income_growth_bp=plan.income_growth_bp,
        rule=Rule(plan.rule),
        swr_bp=plan.swr_bp,
        start_income=Money(plan.income.amount_minor, currency),
        start_expenses=Money(plan.expenses.amount_minor, currency),
        start_portfolio=Money(plan.portfolio.amount_minor, currency),
        events=tuple(
            ProjectionEvent(year=row.year, kind=EventKind(row.kind), amount=row.amount)
            for row in events
        ),
        loans=loans.projection_loans() if loans is not None else (),
    )


def inputs_from_validated(
    plan: FirePlanView,
    validated: object,
    *,
    current_year: int,
    loans: FireLoanData | None = None,
) -> ProjectionInputs:
    """Map a validated unsaved form to the pure projection's input."""
    # Imported here to avoid a circular import in annotations.
    from assetboard.services.fire_scenarios import ValidatedPlan, event_is_active

    assert isinstance(validated, ValidatedPlan)
    currency = plan.currency
    last_year = validated.birth_year + validated.horizon_age
    return ProjectionInputs(
        currency=currency,
        birth_year=validated.birth_year,
        start_year=current_year,
        horizon_age=validated.horizon_age,
        stop_working_age=validated.stop_working_age,
        inflation_bp=validated.inflation_bp,
        real_return_bp=validated.real_return_bp,
        income_growth_bp=validated.income_growth_bp,
        rule=validated.rule,
        swr_bp=validated.swr_bp,
        start_income=Money(validated.income_minor, currency),
        start_expenses=Money(validated.expenses_minor, currency),
        start_portfolio=Money(validated.portfolio_minor, currency),
        events=tuple(
            ProjectionEvent(
                year=row.year,
                kind=EventKind(row.kind),
                amount=Money(row.amount_minor, currency),
            )
            for row in validated.events
            if event_is_active(row.year, row.kind, current_year, last_year)
        ),
        loans=loans.projection_loans() if loans is not None else (),
    )


def results_from_detail(
    detail: FirePlanDetail, *, loans: FireLoanData | None = None
) -> FirePlanResults:
    """Project a loaded plan; a currency mismatch or invalid saved plan becomes a block."""
    plan = detail.plan
    if plan.currency_mismatch:
        return FirePlanResults(
            detail=detail,
            projection=None,
            loans=None,
            blocks=(ResultsBlock(code="plan_currency_mismatch", field="currency"),),
        )
    if plan.horizon_reached:
        return FirePlanResults(
            detail=detail,
            projection=None,
            loans=loans,
            blocks=(ResultsBlock(code="horizon_not_after_age", field="horizon_age"),),
        )
    try:
        projection = project(
            projection_inputs(
                plan,
                detail.active_events,
                current_year=plan.current_year,
                loans=loans,
            )
        )
    except FireProjectionError as exc:
        return FirePlanResults(
            detail=detail,
            projection=None,
            loans=loans,
            blocks=(ResultsBlock(code=exc.code, field=exc.field, year=exc.year),),
        )
    return FirePlanResults(detail=detail, projection=projection, loans=loans)


def results_for_detail(
    factory: sessionmaker[Session], ctx: Context, detail: FirePlanDetail
) -> FirePlanResults:
    """Load the plan's live loans once, then project the already-loaded plan detail."""
    plan = detail.plan
    loans = None
    if not plan.currency_mismatch:
        loans = fire_loans.load_fire_loans(
            factory,
            ctx,
            currency=plan.currency,
            ignored_loan_ids=detail.ignored_loan_ids,
        )
    return results_from_detail(detail, loans=loans)


def scenario_results(
    factory: sessionmaker[Session], ctx: Context, scenario_id: UUID | None = None
) -> FirePlanResults:
    """The live plan's projection for the results page."""
    detail = get_fire_plan(factory, ctx)
    if scenario_id is not None and detail.plan.id != scenario_id:
        from assetboard.db.repositories import NotFound

        raise NotFound
    return results_for_detail(factory, ctx, detail)


def preview_plan_results(
    ctx: Context,
    detail: FirePlanDetail,
    plan_input: FirePlanInput,
    *,
    factory: sessionmaker[Session] | None = None,
) -> FirePlanResults:
    """Validate and project a posted form without writing; editors only."""
    require_role(ctx, Role.EDITOR)
    saved = detail.plan
    if saved.currency_mismatch:
        raise FireError("currency", "plan_currency_mismatch")
    saved_events = {
        event.id: SavedEvent(
            year=event.year,
            kind=event.kind,
            amount_minor=event.amount.amount_minor,
            label=event.label,
            archived=event.archived,
        )
        for event in detail.events
    }
    validated = validate_plan_input(
        plan_input,
        current_year=saved.current_year,
        currency=saved.currency,
        default_horizon=saved.horizon_age,
        previous_rates=(
            saved.inflation_bp,
            saved.real_return_bp,
            saved.income_growth_bp,
            saved.swr_bp,
        ),
        saved_events=saved_events,
        preview=True,
    )
    needs_confirmation = (
        validated.needs_extreme_confirmation and not plan_input.extreme_rates_confirmed
    )
    loans = None
    if factory is not None:
        overrides = {
            setting.loan_id: setting.included for setting in (plan_input.loan_settings or ())
        }
        loans = fire_loans.load_fire_loans(
            factory,
            ctx,
            currency=saved.currency,
            ignored_loan_ids=detail.ignored_loan_ids,
            included_overrides=overrides,
        )
    try:
        projection = project(
            inputs_from_validated(saved, validated, current_year=saved.current_year, loans=loans)
        )
    except FireProjectionError as exc:
        raise FireError(exc.field, exc.code, year=exc.year) from None
    return FirePlanResults(
        detail=detail,
        projection=projection,
        loans=loans,
        preview=True,
        needs_extreme_confirmation=needs_confirmation,
    )


# Old name kept for routes and tests during the one-plan transition.
preview_scenario_results = preview_plan_results


__all__ = [
    "FirePlanResults",
    "FireScenarioResults",
    "ResultsBlock",
    "inputs_from_validated",
    "preview_plan_results",
    "preview_scenario_results",
    "projection_inputs",
    "results_from_detail",
    "scenario_results",
]
