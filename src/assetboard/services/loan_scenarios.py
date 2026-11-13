"""Loan what-if scenarios (F7.4).

A scenario changes only the overpayment settings, the strategy, the rate timeline and/or one
extra lump sum. The comparison always starts from the same loaded loan inputs, so the baseline,
the current plan and every saved or ephemeral variation use the same engine, anchor and costs.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from assetboard.db.engine import run_immediate
from assetboard.db.models.loans import LoanScenario
from assetboard.db.repositories import (
    AuditEventRepository,
    CurrentWorkspaceRepository,
    LoanRepository,
    LoanScenarioRepository,
    NotFound,
)
from assetboard.domain.loans import (
    MAX_LOAN_RATE_BP,
    LoanTerms,
    LoanValidationError,
    OneOffOverpayment,
    OverpaymentMode,
    OverpaymentStrategy,
    Schedule,
    ScheduleError,
    clean_loan_name,
    shift_rate_timeline,
    summarize_schedule,
    valid_date,
)
from assetboard.domain.money import Money
from assetboard.domain.text import text_key
from assetboard.i18n import gettext
from assetboard.services import loan_schedule, loans
from assetboard.services.context import Context, Role, require_role, require_role_now

MAX_EPHEMERAL_SCENARIOS = 3


@dataclass(frozen=True, slots=True)
class ScenarioParameters:
    """One what-if; ``None`` means "same as the loan" for each field."""

    name: str | None
    overpayment_mode: OverpaymentMode | None
    overpayment_amount: Money | None
    strategy: OverpaymentStrategy | None
    rate_shift_bp: int | None
    rate_shift_from: date | None
    one_off_date: date | None
    one_off_amount: Money | None


@dataclass(frozen=True, slots=True)
class LoanScenarioView:
    id: UUID
    name: str
    parameters: ScenarioParameters


@dataclass(frozen=True, slots=True)
class ScenarioComparisonRow:
    label: str
    name: str | None
    is_baseline: bool
    is_current: bool
    payoff_date: date | None
    months_saved: int | None
    total_interest: Money | None
    interest_saved: Money | None
    total_paid: Money | None
    schedule: Schedule | None
    parameters: ScenarioParameters | None
    scenario_id: UUID | None
    error_code: str | None
    interest_saved_vs_current: Money | None = None
    months_saved_vs_current: int | None = None

    @property
    def interest_saved_vs_current_abs(self) -> Money | None:
        if self.interest_saved_vs_current is None:
            return None
        return Money(
            abs(self.interest_saved_vs_current.amount_minor),
            self.interest_saved_vs_current.currency,
        )

    @property
    def months_saved_vs_current_abs(self) -> int | None:
        if self.months_saved_vs_current is None:
            return None
        return abs(self.months_saved_vs_current)


@dataclass(frozen=True, slots=True)
class LoanScenarioComparison:
    view: loans.LoanView
    baseline: ScenarioComparisonRow | None
    current: ScenarioComparisonRow | None
    saved: tuple[ScenarioComparisonRow, ...]
    ephemeral: tuple[ScenarioComparisonRow, ...]
    error_code: str | None
    as_of: date

    @property
    def rows(self) -> tuple[ScenarioComparisonRow, ...]:
        rows: list[ScenarioComparisonRow] = []
        if self.baseline is not None:
            rows.append(self.baseline)
        if self.current is not None:
            rows.append(self.current)
        rows.extend(self.saved)
        rows.extend(self.ephemeral)
        return tuple(rows)


def _require_viewer(ctx: Context) -> None:
    require_role(ctx, Role.VIEWER)


def _mode(value: OverpaymentMode | str | None) -> OverpaymentMode | None:
    if value is None:
        return None
    try:
        return OverpaymentMode(value)
    except ValueError:
        raise loans.LoanError("overpayment_mode", "invalid_overpayment_mode") from None


def _strategy(value: OverpaymentStrategy | str | None) -> OverpaymentStrategy | None:
    if value is None:
        return None
    try:
        return OverpaymentStrategy(value)
    except ValueError:
        raise loans.LoanError("overpayment_strategy", "invalid_strategy") from None


def validate_scenario_parameters(
    parameters: ScenarioParameters, currency: str
) -> ScenarioParameters:
    """Normalise a scenario and refuse an impossible parameter combination."""
    name = clean_loan_name(parameters.name) if parameters.name is not None else None
    mode = _mode(parameters.overpayment_mode)
    strategy = _strategy(parameters.strategy)
    amount = parameters.overpayment_amount
    if amount is not None and amount.currency != currency:
        raise loans.LoanError("overpayment_amount", "invalid_currency")
    if mode is None or mode is OverpaymentMode.NONE:
        if amount is not None:
            raise loans.LoanError("overpayment_amount", "invalid_overpayment")
    elif amount is None or amount.amount_minor <= 0:
        raise loans.LoanError("overpayment_amount", "invalid_overpayment")

    shift = parameters.rate_shift_bp
    shift_from = parameters.rate_shift_from
    if (shift is None) != (shift_from is None):
        raise loans.LoanError("rate_shift_bp", "rate_shift_pair_required")
    if shift is not None:
        if shift == 0:
            shift = None
        elif not -MAX_LOAN_RATE_BP <= shift <= MAX_LOAN_RATE_BP:
            raise loans.LoanError("rate_shift_bp", "invalid_rate_shift")
        else:
            valid_date(shift_from, "rate_shift_from")  # type: ignore[arg-type]
    if shift is None:
        shift_from = None

    one_off_date = parameters.one_off_date
    one_off_amount = parameters.one_off_amount
    if (one_off_date is None) != (one_off_amount is None):
        raise loans.LoanError("one_off_amount", "one_off_pair_required")
    if one_off_amount is not None:
        valid_date(one_off_date, "one_off_date")  # type: ignore[arg-type]
        if one_off_amount.currency != currency or one_off_amount.amount_minor <= 0:
            raise loans.LoanError("one_off_amount", "invalid_overpayment")
    return ScenarioParameters(
        name=name,
        overpayment_mode=mode,
        overpayment_amount=amount,
        strategy=strategy,
        rate_shift_bp=shift,
        rate_shift_from=shift_from,
        one_off_date=one_off_date,
        one_off_amount=one_off_amount,
    )


def scenario_parameters_from_model(row: LoanScenario, currency: str) -> ScenarioParameters:
    """The saved parameters with the loan currency supplied by the caller."""

    def money(minor: int | None) -> Money | None:
        return None if minor is None else Money(minor, currency)

    return ScenarioParameters(
        name=row.name,
        overpayment_mode=OverpaymentMode(row.overpayment_mode)
        if row.overpayment_mode is not None
        else None,
        overpayment_amount=money(row.overpayment_amount_minor),
        strategy=OverpaymentStrategy(row.overpayment_strategy)
        if row.overpayment_strategy is not None
        else None,
        rate_shift_bp=row.rate_shift_bp,
        rate_shift_from=row.rate_shift_from,
        one_off_date=row.one_off_date,
        one_off_amount=money(row.one_off_amount_minor),
    )


def _parameters_from_model(row: LoanScenario, currency: str) -> ScenarioParameters:
    return scenario_parameters_from_model(row, currency)


def _row(
    inputs: loan_schedule.LoanScheduleInputs,
    parameters: ScenarioParameters | None,
    label: str,
    *,
    is_baseline: bool = False,
    is_current: bool = False,
    scenario_id: UUID | None = None,
    baseline: Schedule | None = None,
    current: Schedule | None = None,
    schedule_override: Schedule | None = None,
) -> ScenarioComparisonRow:
    name = parameters.name if parameters is not None else None
    terms: LoanTerms = inputs.terms
    rate_timeline = inputs.rate_timeline
    overpayments = inputs.overpayments
    strategy: OverpaymentStrategy = inputs.strategy
    try:
        if schedule_override is not None:
            schedule = schedule_override
        else:
            if parameters is not None:
                if parameters.overpayment_mode is not None:
                    terms = replace(
                        terms,
                        overpayment_mode=parameters.overpayment_mode,
                        overpayment_amount=(
                            parameters.overpayment_amount
                            if parameters.overpayment_mode is not OverpaymentMode.NONE
                            else None
                        ),
                    )
                if parameters.strategy is not None:
                    strategy = parameters.strategy
                if parameters.rate_shift_bp is not None and parameters.rate_shift_from is not None:
                    rate_timeline = shift_rate_timeline(
                        inputs.rate_timeline,
                        parameters.rate_shift_bp,
                        parameters.rate_shift_from,
                    )
                if parameters.one_off_date is not None and parameters.one_off_amount is not None:
                    overpayments = [
                        *inputs.overpayments,
                        OneOffOverpayment(
                            date=parameters.one_off_date, amount=parameters.one_off_amount
                        ),
                    ]
            schedule = loan_schedule.build_inputs_schedule(
                inputs,
                terms=terms,
                rates=rate_timeline,
                overpayments=overpayments,
                strategy=strategy,
            )
        summary = summarize_schedule(schedule, inputs.as_of)
    except (ScheduleError, LoanValidationError) as exc:
        return ScenarioComparisonRow(
            label=label,
            name=name,
            is_baseline=is_baseline,
            is_current=is_current,
            payoff_date=None,
            months_saved=None,
            total_interest=None,
            interest_saved=None,
            total_paid=None,
            schedule=None,
            parameters=parameters,
            scenario_id=scenario_id,
            error_code=exc.code,
        )
    months_saved = None
    interest_saved = None
    if baseline is not None:
        months_saved = len(baseline.rows) - len(schedule.rows)
        interest_saved = Money(
            sum(row.interest.amount_minor for row in baseline.rows)
            - summary.total_interest.amount_minor,
            schedule.currency,
        )
    interest_saved_vs_current = None
    months_saved_vs_current = None
    if current is not None:
        months_saved_vs_current = len(current.rows) - len(schedule.rows)
        interest_saved_vs_current = Money(
            sum(row.interest.amount_minor for row in current.rows)
            - summary.total_interest.amount_minor,
            schedule.currency,
        )
    return ScenarioComparisonRow(
        label=label,
        name=name,
        is_baseline=is_baseline,
        is_current=is_current,
        payoff_date=summary.payoff_date,
        months_saved=months_saved,
        total_interest=summary.total_interest,
        interest_saved=interest_saved,
        total_paid=summary.total_paid,
        schedule=schedule,
        parameters=parameters,
        scenario_id=scenario_id,
        error_code=None,
        interest_saved_vs_current=interest_saved_vs_current,
        months_saved_vs_current=months_saved_vs_current,
    )


def _saved_views(
    session: Session, ctx: Context, loan_id: UUID, currency: str
) -> tuple[LoanScenarioView, ...]:
    return tuple(
        LoanScenarioView(
            id=row.id,
            name=row.name,
            parameters=_parameters_from_model(row, currency),
        )
        for row in LoanScenarioRepository(session, ctx).for_loan(loan_id)
    )


def list_loan_scenarios(
    factory: sessionmaker[Session], ctx: Context, loan_id: UUID
) -> tuple[LoanScenarioView, ...]:
    """Saved scenarios of one loan; viewers may read them."""
    _require_viewer(ctx)
    with factory() as session:
        loan = LoanRepository(session, ctx).get_or_raise(loan_id)
        return _saved_views(session, ctx, loan.id, loan.currency)


def _validate_count(ephemeral: list[ScenarioParameters]) -> None:
    if len(ephemeral) > MAX_EPHEMERAL_SCENARIOS:
        raise loans.LoanError("scenario", "too_many_scenarios")


def compare_loan_scenarios(
    factory: sessionmaker[Session],
    ctx: Context,
    loan_id: UUID,
    ephemeral: list[ScenarioParameters],
    *,
    locale: str | None = None,
) -> LoanScenarioComparison:
    """Build the baseline, current plan, saved scenarios and ephemeral variations."""
    _require_viewer(ctx)
    _validate_count(ephemeral)
    label_locale = locale or ctx.user.locale or ctx.workspace.locale
    try:
        inputs = loan_schedule.load_loan_schedule_inputs(factory, ctx, loan_id)
    except (ScheduleError, LoanValidationError) as exc:
        view = loans.get_loan(factory, ctx, loan_id)
        return LoanScenarioComparison(
            view=view,
            baseline=None,
            current=None,
            saved=(),
            ephemeral=(),
            error_code=exc.code,
            as_of=ctx.clock.today(ctx.workspace.timezone),
        )
    currency = inputs.view.currency
    try:
        baseline_terms = replace(
            inputs.terms,
            overpayment_mode=OverpaymentMode.NONE,
            overpayment_amount=None,
        )
        baseline_schedule = loan_schedule.build_inputs_schedule(
            inputs, terms=baseline_terms, overpayments=[]
        )
        baseline = _row(
            inputs,
            None,
            gettext("Baseline", label_locale),
            is_baseline=True,
            baseline=baseline_schedule,
            schedule_override=baseline_schedule,
        )
        current = _row(
            inputs,
            None,
            gettext("Current plan", label_locale),
            is_current=True,
            baseline=baseline_schedule,
        )
    except (ScheduleError, LoanValidationError) as exc:
        return LoanScenarioComparison(
            view=inputs.view,
            baseline=None,
            current=None,
            saved=(),
            ephemeral=(),
            error_code=exc.code,
            as_of=inputs.as_of,
        )
    with factory() as session:
        saved_views = _saved_views(session, ctx, inputs.view.id, currency)
    saved_rows: list[ScenarioComparisonRow] = []
    for view in saved_views:
        saved_rows.append(
            _row(
                inputs,
                view.parameters,
                view.name,
                scenario_id=view.id,
                baseline=baseline_schedule,
                current=current.schedule,
            )
        )
    ephemeral_rows = [
        _row(
            inputs,
            parameters,
            gettext("What-if %(number)d", label_locale) % {"number": index},
            baseline=baseline_schedule,
        )
        for index, parameters in enumerate(ephemeral, start=1)
    ]
    return LoanScenarioComparison(
        view=inputs.view,
        baseline=baseline,
        current=current,
        saved=tuple(saved_rows),
        ephemeral=tuple(ephemeral_rows),
        error_code=None,
        as_of=inputs.as_of,
    )


def _scenario_values(parameters: ScenarioParameters) -> dict[str, object]:
    return {
        "name": parameters.name or "",
        "name_key": _name_key(parameters.name or ""),
        "overpayment_mode": (
            parameters.overpayment_mode.value if parameters.overpayment_mode is not None else None
        ),
        "overpayment_amount_minor": (
            parameters.overpayment_amount.amount_minor
            if parameters.overpayment_amount is not None
            else None
        ),
        "overpayment_strategy": (
            parameters.strategy.value if parameters.strategy is not None else None
        ),
        "rate_shift_bp": parameters.rate_shift_bp,
        "rate_shift_from": parameters.rate_shift_from,
        "one_off_date": parameters.one_off_date,
        "one_off_amount_minor": (
            parameters.one_off_amount.amount_minor
            if parameters.one_off_amount is not None
            else None
        ),
    }


def _name_key(name: str) -> str:
    key = text_key(name)
    if len(key) > 255:
        raise loans.LoanError("name", "invalid_name")
    return key


def create_or_update_loan_scenario(
    factory: sessionmaker[Session],
    ctx: Context,
    loan_id: UUID,
    parameters: ScenarioParameters,
) -> UUID:
    """Save a named scenario; saving the same name again updates that scenario."""
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> UUID:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        loan = LoanRepository(session, ctx).get_or_raise(loan_id)
        if loan.archived_at is not None:
            raise loans.LoanError("loan", "archived")
        if not parameters.name:
            raise loans.LoanError("name", "invalid_name")
        validated = validate_scenario_parameters(parameters, loan.currency)
        values = _scenario_values(validated)
        repository = LoanScenarioRepository(session, ctx)
        existing = repository.by_name_key(loan.id, values["name_key"])  # type: ignore[arg-type]
        if existing is not None:
            repository.update(existing.id, **values)
            scenario_id = existing.id
            action = "loan_scenario.updated"
        else:
            scenario = repository.create(loan_id=loan.id, **values)
            scenario_id = scenario.id
            action = "loan_scenario.created"
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action=action,
            entity_type="loan",
            entity_id=str(loan.id),
            ts=ctx.clock.now(),
            data={"scenario_id": str(scenario_id), "name": validated.name or ""},
        )
        return scenario_id

    return run_immediate(factory, write)


def delete_loan_scenario(
    factory: sessionmaker[Session], ctx: Context, loan_id: UUID, scenario_id: UUID
) -> None:
    """Delete one of this loan's scenarios; a row of another loan is not found."""
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        loan = LoanRepository(session, ctx).get_or_raise(loan_id)
        if loan.archived_at is not None:
            raise loans.LoanError("loan", "archived")
        repository = LoanScenarioRepository(session, ctx)
        scenario = repository.get_for_loan(loan.id, scenario_id)
        if scenario is None:
            raise NotFound
        repository.delete(scenario)
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action="loan_scenario.deleted",
            entity_type="loan",
            entity_id=str(loan.id),
            ts=ctx.clock.now(),
            data={"scenario_id": str(scenario.id), "name": scenario.name},
        )

    run_immediate(factory, write)


__all__ = [
    "LoanScenarioComparison",
    "LoanScenarioView",
    "MAX_EPHEMERAL_SCENARIOS",
    "ScenarioComparisonRow",
    "ScenarioParameters",
    "compare_loan_scenarios",
    "create_or_update_loan_scenario",
    "delete_loan_scenario",
    "list_loan_scenarios",
    "scenario_parameters_from_model",
    "validate_scenario_parameters",
]
