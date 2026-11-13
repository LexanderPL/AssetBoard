"""FIRE scenario settings (F8.1).

A scenario is created with the workspace base currency and the current plan year snapshotted;
later changes keep both, so a saved plan stays reproducible. Validation lives here, not in the
route: roles, the required birth year, rate bounds and the confirmation for unusual rates, the
age horizon and the contribution/event year windows. Archived scenarios are read-only; there is
no hard delete. Exactly one active scenario is the default whenever an active one exists.

F8.1 stores the linked input modes but accepts only ``manual``; a linked value is refused with
``linked_mode_unavailable`` until F8.4 resolves it from the app's own data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from assetboard.db.engine import run_immediate
from assetboard.db.models.fire import FireScenario
from assetboard.db.models.identity import User
from assetboard.db.repositories import (
    AuditEventRepository,
    CurrentWorkspaceRepository,
    FireContributionRepository,
    FireEventRepository,
    FireScenarioRepository,
)
from assetboard.domain.fire import (
    DEFAULT_LIFE_EXPECTANCY,
    ILLUSTRATIVE_INCOME_GROWTH_BP,
    ILLUSTRATIVE_INFLATION_BP,
    ILLUSTRATIVE_REAL_RETURN_BP,
    ILLUSTRATIVE_SWR_BP,
    MAX_CONTRIBUTION_ROWS,
    MAX_EVENT_ROWS,
    MAX_HORIZON_AGE,
    MAX_NAME_LENGTH,
    MAX_STOP_WORKING_AGE,
    MAX_YEAR,
    MIN_HORIZON_AGE,
    MIN_YEAR,
    RATE_HARD_MAX_BP,
    RATE_HARD_MIN_BP,
    SWR_HARD_MAX_BP,
    SWR_HARD_MIN_BP,
    EventKind,
    ExpensesMode,
    IncomeMode,
    PortfolioMode,
    Rule,
    age_at_start,
    clean_event_label,
    clean_scenario_name,
    contribution_years,
    default_horizon_age,
    event_years,
    extreme_rate_change,
    year_in_window,
)
from assetboard.domain.money import Money
from assetboard.domain.text import text_key
from assetboard.domain.transactions import MAX_AMOUNT_MINOR
from assetboard.services.context import Context, Role, require_role, require_role_now


class FireError(Exception):
    """Invalid scenario input; ``field`` names the form control and ``code`` the message."""

    def __init__(self, field: str, code: str) -> None:
        super().__init__(code)
        self.field = field
        self.code = code


@dataclass(frozen=True, slots=True)
class ContributionInput:
    year: int
    amount_minor: int


@dataclass(frozen=True, slots=True)
class EventInput:
    year: int
    kind: str
    amount_minor: int
    label: str
    # The saved row this editor row came from; ``None`` creates a new event. A saving keeps the
    # identity (and timestamps) of unchanged events, which F8.6 archive lineage relies on.
    id: UUID | None = None


@dataclass(frozen=True, slots=True)
class FireScenarioInput:
    name: str = ""
    birth_year: int | None = None
    inflation_bp: int = ILLUSTRATIVE_INFLATION_BP
    real_return_bp: int = ILLUSTRATIVE_REAL_RETURN_BP
    income_growth_bp: int = ILLUSTRATIVE_INCOME_GROWTH_BP
    stop_working_age: int = 0
    # None takes the profile's life expectancy on create (90 without one), or the saved horizon
    # on update; the web form always posts an explicit value.
    horizon_age: int | None = None
    rule: str = Rule.RETURNS_COVER_EXPENSES.value
    swr_bp: int | None = None
    income_mode: str = IncomeMode.MANUAL.value
    income_manual: Money | None = None
    expenses_mode: str = ExpensesMode.MANUAL.value
    expenses_manual: Money | None = None
    portfolio_mode: str = PortfolioMode.MANUAL.value
    portfolio_manual: Money | None = None
    extreme_rates_confirmed: bool = False
    contributions: tuple[ContributionInput, ...] = ()
    events: tuple[EventInput, ...] = ()


@dataclass(frozen=True, slots=True)
class FireScenarioDefaults:
    start_year: int
    currency: str
    birth_year: int | None
    life_expectancy: int
    inflation_bp: int = ILLUSTRATIVE_INFLATION_BP
    real_return_bp: int = ILLUSTRATIVE_REAL_RETURN_BP
    income_growth_bp: int = ILLUSTRATIVE_INCOME_GROWTH_BP
    swr_bp: int = ILLUSTRATIVE_SWR_BP


@dataclass(frozen=True, slots=True)
class ContributionView:
    id: UUID
    year: int
    amount: Money


@dataclass(frozen=True, slots=True)
class EventView:
    id: UUID
    year: int
    kind: str
    amount: Money
    label: str


@dataclass(frozen=True, slots=True)
class FireScenarioView:
    id: UUID
    name: str
    currency: str
    birth_year: int
    start_year: int
    age_at_start: int
    inflation_bp: int
    real_return_bp: int
    income_growth_bp: int
    stop_working_age: int
    horizon_age: int
    rule: str
    swr_bp: int | None
    income_mode: str
    income_manual: Money | None
    expenses_mode: str
    expenses_manual: Money | None
    portfolio_mode: str
    portfolio_manual: Money | None
    is_default: bool
    archived_at: datetime | None
    contribution_count: int
    event_count: int

    @property
    def archived(self) -> bool:
        return self.archived_at is not None

    @property
    def stop_after_horizon(self) -> bool:
        """Work continues past the horizon: allowed, shown as a non-blocking warning."""
        return self.stop_working_age > self.horizon_age


@dataclass(frozen=True, slots=True)
class FireScenarioDetail:
    scenario: FireScenarioView
    contributions: tuple[ContributionView, ...]
    events: tuple[EventView, ...]


@dataclass(frozen=True, slots=True)
class FireScenarioList:
    active: tuple[FireScenarioView, ...]
    archived: tuple[FireScenarioView, ...]


@dataclass(frozen=True, slots=True)
class _ValidatedScenario:
    name: str
    name_key: str
    birth_year: int
    horizon_age: int
    stop_working_age: int
    inflation_bp: int
    real_return_bp: int
    income_growth_bp: int
    rule: Rule
    swr_bp: int | None
    income_mode: IncomeMode
    income_manual_minor: int
    expenses_mode: ExpensesMode
    expenses_manual_minor: int
    portfolio_mode: PortfolioMode
    portfolio_manual_minor: int
    contributions: tuple[ContributionInput, ...]
    events: tuple[EventInput, ...]


def _require_viewer(ctx: Context) -> None:
    require_role(ctx, Role.VIEWER)


def _mode[T: (IncomeMode, ExpensesMode, PortfolioMode)](
    enum: type[T], value: str, field: str, code: str
) -> T:
    try:
        return enum(value)
    except ValueError:
        raise FireError(field, code) from None


def _integer(value: object, field: str, code: str) -> int:
    if type(value) is not int:
        raise FireError(field, code)
    return value


def _rate(value: object, field: str, code: str) -> int:
    rate = _integer(value, field, code)
    if not RATE_HARD_MIN_BP <= rate <= RATE_HARD_MAX_BP:
        raise FireError(field, code)
    return rate


def _manual_minor(
    field: str,
    mode: IncomeMode | ExpensesMode | PortfolioMode,
    amount: Money | None,
    currency: str,
) -> int:
    # F8.1 accepts manual inputs only; F8.4 removes this branch and resolves linked values.
    if mode.value != "manual":
        raise FireError(field, "linked_mode_unavailable")
    if amount is None or amount.currency != currency:
        raise FireError(field, "invalid_amount")
    if not 0 <= amount.amount_minor <= MAX_AMOUNT_MINOR:
        raise FireError(field, "invalid_amount")
    return amount.amount_minor


def _validate(
    scenario: FireScenarioInput,
    *,
    start_year: int,
    currency: str,
    default_horizon: int,
    previous: FireScenario | None,
) -> _ValidatedScenario:
    try:
        name = clean_scenario_name(scenario.name)
    except ValueError:
        raise FireError("name", "invalid_name") from None
    if not name or len(name) > MAX_NAME_LENGTH:
        raise FireError("name", "invalid_name")

    if scenario.birth_year is None:
        raise FireError("birth_year", "birth_year_required")
    birth_year = _integer(scenario.birth_year, "birth_year", "invalid_birth_year")
    if not MIN_YEAR <= birth_year <= MAX_YEAR or birth_year > start_year:
        raise FireError("birth_year", "invalid_birth_year")
    age = age_at_start(start_year, birth_year)

    horizon_age = _integer(
        scenario.horizon_age if scenario.horizon_age is not None else default_horizon,
        "horizon_age",
        "invalid_horizon",
    )
    if not MIN_HORIZON_AGE <= horizon_age <= MAX_HORIZON_AGE:
        raise FireError("horizon_age", "invalid_horizon")
    if horizon_age <= age:
        raise FireError("horizon_age", "horizon_not_after_age")

    stop_working_age = _integer(scenario.stop_working_age, "stop_working_age", "invalid_stop_age")
    if not 0 <= stop_working_age <= MAX_STOP_WORKING_AGE:
        raise FireError("stop_working_age", "invalid_stop_age")
    if stop_working_age < age:
        raise FireError("stop_working_age", "stop_before_age")

    inflation_bp = _rate(scenario.inflation_bp, "inflation", "invalid_rate")
    real_return_bp = _rate(scenario.real_return_bp, "real_return", "invalid_rate")
    income_growth_bp = _rate(scenario.income_growth_bp, "income_growth", "invalid_rate")

    try:
        rule = Rule(scenario.rule)
    except ValueError:
        raise FireError("rule", "invalid_rule") from None
    swr_bp: int | None = None
    if rule is Rule.SWR:
        swr_bp = _integer(scenario.swr_bp, "swr", "invalid_swr")
        if not SWR_HARD_MIN_BP <= swr_bp <= SWR_HARD_MAX_BP:
            raise FireError("swr", "invalid_swr")
    else:
        swr_bp = None

    income_mode = _mode(IncomeMode, scenario.income_mode, "income_mode", "invalid_income_mode")
    expenses_mode = _mode(
        ExpensesMode, scenario.expenses_mode, "expenses_mode", "invalid_expenses_mode"
    )
    portfolio_mode = _mode(
        PortfolioMode, scenario.portfolio_mode, "portfolio_mode", "invalid_portfolio_mode"
    )
    income_manual = _manual_minor("income_manual", income_mode, scenario.income_manual, currency)
    expenses_manual = _manual_minor(
        "expenses_manual", expenses_mode, scenario.expenses_manual, currency
    )
    portfolio_manual = _manual_minor(
        "portfolio_manual", portfolio_mode, scenario.portfolio_manual, currency
    )

    needs_confirmation = extreme_rate_change(
        inflation_bp=inflation_bp,
        real_return_bp=real_return_bp,
        income_growth_bp=income_growth_bp,
        swr_bp=swr_bp,
        previous=None
        if previous is None
        else (
            previous.inflation_bp,
            previous.real_return_bp,
            previous.income_growth_bp,
            previous.swr_bp,
        ),
    )
    if needs_confirmation and not scenario.extreme_rates_confirmed:
        raise FireError("extreme_rates_confirmed", "extreme_rate_confirmation_required")

    if len(scenario.contributions) > MAX_CONTRIBUTION_ROWS:
        raise FireError("contributions", "too_many_contributions")
    years = contribution_years(start_year, birth_year, horizon_age)
    seen_contributions: set[int] = set()
    contributions: list[ContributionInput] = []
    for row in scenario.contributions:
        if not year_in_window(row.year, years):
            raise FireError("contributions", "rows_outside_window")
        if row.year in seen_contributions:
            raise FireError("contributions", "duplicate_contribution_year")
        seen_contributions.add(row.year)
        if type(row.amount_minor) is not int or abs(row.amount_minor) > MAX_AMOUNT_MINOR:
            raise FireError("contributions", "invalid_amount")
        contributions.append(row)

    if len(scenario.events) > MAX_EVENT_ROWS:
        raise FireError("events", "too_many_events")
    event_window = event_years(start_year, birth_year, horizon_age)
    events: list[EventInput] = []
    seen_event_ids: set[UUID] = set()
    for row in scenario.events:
        if not year_in_window(row.year, event_window):
            raise FireError("events", "rows_outside_window")
        if row.id is not None:
            if row.id in seen_event_ids:
                raise FireError("events", "duplicate_event")
            seen_event_ids.add(row.id)
        try:
            kind = EventKind(row.kind)
        except ValueError:
            raise FireError("events", "invalid_event_kind") from None
        if type(row.amount_minor) is not int or abs(row.amount_minor) > MAX_AMOUNT_MINOR:
            raise FireError("events", "invalid_amount")
        try:
            label = clean_event_label(row.label)
        except ValueError:
            raise FireError("events", "invalid_label") from None
        events.append(
            EventInput(
                year=row.year,
                kind=kind.value,
                amount_minor=row.amount_minor,
                label=label,
                id=row.id,
            )
        )

    return _ValidatedScenario(
        name=name,
        name_key=text_key(name),
        birth_year=birth_year,
        horizon_age=horizon_age,
        stop_working_age=stop_working_age,
        inflation_bp=inflation_bp,
        real_return_bp=real_return_bp,
        income_growth_bp=income_growth_bp,
        rule=rule,
        swr_bp=swr_bp,
        income_mode=income_mode,
        income_manual_minor=income_manual,
        expenses_mode=expenses_mode,
        expenses_manual_minor=expenses_manual,
        portfolio_mode=portfolio_mode,
        portfolio_manual_minor=portfolio_manual,
        contributions=tuple(contributions),
        events=tuple(events),
    )


def _view(row: FireScenario, *, contribution_count: int, event_count: int) -> FireScenarioView:
    currency = row.currency
    return FireScenarioView(
        id=row.id,
        name=row.name,
        currency=currency,
        birth_year=row.birth_year,
        start_year=row.start_year,
        age_at_start=age_at_start(row.start_year, row.birth_year),
        inflation_bp=row.inflation_bp,
        real_return_bp=row.real_return_bp,
        income_growth_bp=row.income_growth_bp,
        stop_working_age=row.stop_working_age,
        horizon_age=row.horizon_age,
        rule=row.rule,
        swr_bp=row.swr_bp,
        income_mode=row.income_mode,
        income_manual=Money(row.income_manual_minor, currency)
        if row.income_manual_minor is not None
        else None,
        expenses_mode=row.expenses_mode,
        expenses_manual=Money(row.expenses_manual_minor, currency)
        if row.expenses_manual_minor is not None
        else None,
        portfolio_mode=row.portfolio_mode,
        portfolio_manual=Money(row.portfolio_manual_minor, currency)
        if row.portfolio_manual_minor is not None
        else None,
        is_default=row.is_default,
        archived_at=row.archived_at,
        contribution_count=contribution_count,
        event_count=event_count,
    )


def _scenario_values(validated: _ValidatedScenario) -> dict[str, object]:
    return {
        "name": validated.name,
        "name_key": validated.name_key,
        "birth_year": validated.birth_year,
        "inflation_bp": validated.inflation_bp,
        "real_return_bp": validated.real_return_bp,
        "income_growth_bp": validated.income_growth_bp,
        "stop_working_age": validated.stop_working_age,
        "horizon_age": validated.horizon_age,
        "rule": validated.rule.value,
        "swr_bp": validated.swr_bp,
        "income_mode": validated.income_mode.value,
        "income_manual_minor": validated.income_manual_minor,
        "expenses_mode": validated.expenses_mode.value,
        "expenses_manual_minor": validated.expenses_manual_minor,
        "portfolio_mode": validated.portfolio_mode.value,
        "portfolio_manual_minor": validated.portfolio_manual_minor,
    }


def _free_copy_name(name: str, active_keys: set[str]) -> str:
    """A free active name: ``Name (copy)``, then ``Name (copy 2)``, ``Name (copy 3)``…"""
    for number in range(1, 10_000):
        suffix = " (copy)" if number == 1 else f" (copy {number})"
        base = name[: max(1, MAX_NAME_LENGTH - len(suffix))]
        candidate = f"{base}{suffix}"
        if text_key(candidate) not in active_keys:
            return candidate
    raise FireError("name", "duplicate_name")


def _audit(session: Session, ctx: Context, action: str, row: FireScenario) -> None:
    AuditEventRepository(session, ctx).record(
        actor_user_id=ctx.user.id,
        action=action,
        entity_type="fire_scenario",
        entity_id=str(row.id),
        ts=ctx.clock.now(),
        data={"name": row.name},
    )


def _reconcile_rows(
    session: Session,
    ctx: Context,
    scenario_id: UUID,
    validated: _ValidatedScenario,
) -> None:
    """Make the saved child rows match the editor, keeping the identity of unchanged rows.

    Contributions are matched by year (unique per scenario). Events carry a hidden row id, so
    two events in the same year can be edited independently. A row omitted from the editor is
    deleted, a new row is created and a changed row is updated; an event id that does not belong
    to this scenario is refused instead of silently creating a second row. Stable child ids and
    timestamps matter for the F8.6 archive lineage and for an honest audit trail.
    """
    contributions = FireContributionRepository(session, ctx)
    existing_contributions = {row.year: row for row in contributions.for_scenario(scenario_id)}
    wanted_years = {row.year for row in validated.contributions}
    for row in validated.contributions:
        current = existing_contributions.get(row.year)
        if current is None:
            contributions.create(
                scenario_id=scenario_id, year=row.year, net_amount_minor=row.amount_minor
            )
        elif current.net_amount_minor != row.amount_minor:
            contributions.update(current.id, net_amount_minor=row.amount_minor)
    for year, current in existing_contributions.items():
        if year not in wanted_years:
            contributions.delete(current)

    events = FireEventRepository(session, ctx)
    existing_events = {row.id: row for row in events.for_scenario(scenario_id)}
    for row in validated.events:
        if row.id is None:
            events.create(
                scenario_id=scenario_id,
                year=row.year,
                kind=row.kind,
                amount_minor=row.amount_minor,
                label=row.label,
            )
            continue
        current = existing_events.pop(row.id, None)
        if current is None:
            raise FireError("events", "invalid_event")
        if (
            current.year,
            current.kind,
            current.amount_minor,
            current.label,
        ) != (row.year, row.kind, row.amount_minor, row.label):
            events.update(
                current.id,
                year=row.year,
                kind=row.kind,
                amount_minor=row.amount_minor,
                label=row.label,
            )
    for current in existing_events.values():
        events.delete(current)


def _copy_rows(session: Session, ctx: Context, source: UUID, target: UUID) -> None:
    contributions = FireContributionRepository(session, ctx)
    for row in contributions.for_scenario(source):
        contributions.create(
            scenario_id=target, year=row.year, net_amount_minor=row.net_amount_minor
        )
    events = FireEventRepository(session, ctx)
    for row in events.for_scenario(source):
        events.create(
            scenario_id=target,
            year=row.year,
            kind=row.kind,
            amount_minor=row.amount_minor,
            label=row.label,
        )


def _promote_default(
    session: Session, ctx: Context, repository: FireScenarioRepository, row: FireScenario
) -> None:
    current = repository.active_default()
    if current is not None and current.id == row.id:
        return
    if current is not None:
        repository.update(current.id, is_default=False)
    repository.update(row.id, is_default=True)


def _profile(session: Session, ctx: Context) -> tuple[int | None, int]:
    user = session.get(User, ctx.user.id)
    if user is None:
        return None, DEFAULT_LIFE_EXPECTANCY
    return user.birth_year, default_horizon_age(user.life_expectancy)


def scenario_defaults(factory: sessionmaker[Session], ctx: Context) -> FireScenarioDefaults:
    """The new-scenario form's prefill: the profile's birth year and life expectancy."""
    _require_viewer(ctx)
    with factory() as session:
        birth_year, life_expectancy = _profile(session, ctx)
        # The Context is a request-time snapshot; read the current row for the form's currency.
        currency = CurrentWorkspaceRepository(session, ctx).get().base_currency
    today = ctx.clock.today(ctx.workspace.timezone)
    return FireScenarioDefaults(
        start_year=today.year,
        currency=currency,
        birth_year=birth_year,
        life_expectancy=life_expectancy,
        inflation_bp=ILLUSTRATIVE_INFLATION_BP,
        real_return_bp=ILLUSTRATIVE_REAL_RETURN_BP,
        income_growth_bp=ILLUSTRATIVE_INCOME_GROWTH_BP,
        swr_bp=ILLUSTRATIVE_SWR_BP,
    )


def list_fire_scenarios(factory: sessionmaker[Session], ctx: Context) -> FireScenarioList:
    """Active and archived scenarios with their row counts (four queries, no N+1)."""
    _require_viewer(ctx)
    with factory() as session:
        repository = FireScenarioRepository(session, ctx)
        active = repository.active()
        archived = repository.archived()
        contribution_counts = FireContributionRepository(session, ctx).counts_by_scenario()
        event_counts = FireEventRepository(session, ctx).counts_by_scenario()
    return FireScenarioList(
        active=tuple(
            _view(
                row,
                contribution_count=contribution_counts.get(row.id, 0),
                event_count=event_counts.get(row.id, 0),
            )
            for row in active
        ),
        archived=tuple(
            _view(
                row,
                contribution_count=contribution_counts.get(row.id, 0),
                event_count=event_counts.get(row.id, 0),
            )
            for row in archived
        ),
    )


def get_fire_scenario(
    factory: sessionmaker[Session], ctx: Context, scenario_id: UUID
) -> FireScenarioDetail:
    """One scenario with its contribution overrides and events; viewers may read it."""
    _require_viewer(ctx)
    with factory() as session:
        row = FireScenarioRepository(session, ctx).get_or_raise(scenario_id)
        contributions = FireContributionRepository(session, ctx).for_scenario(row.id)
        events = FireEventRepository(session, ctx).for_scenario(row.id)
        view = _view(row, contribution_count=len(contributions), event_count=len(events))
        return FireScenarioDetail(
            scenario=view,
            contributions=tuple(
                ContributionView(
                    id=item.id,
                    year=item.year,
                    amount=Money(item.net_amount_minor, row.currency),
                )
                for item in contributions
            ),
            events=tuple(
                EventView(
                    id=item.id,
                    year=item.year,
                    kind=item.kind,
                    amount=Money(item.amount_minor, row.currency),
                    label=item.label,
                )
                for item in events
            ),
        )


def create_fire_scenario(
    factory: sessionmaker[Session], ctx: Context, scenario: FireScenarioInput
) -> UUID:
    """Create an active scenario; the first one becomes the default."""
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> UUID:
        require_role_now(session, ctx, Role.EDITOR)
        workspace = CurrentWorkspaceRepository(session, ctx)
        workspace.lock(exclusive=True)
        # The Context is a request-time snapshot; the locked row is the current base currency.
        currency = workspace.get().base_currency
        start_year = ctx.clock.today(ctx.workspace.timezone).year
        _, profile_horizon = _profile(session, ctx)
        validated = _validate(
            scenario,
            start_year=start_year,
            currency=currency,
            default_horizon=profile_horizon,
            previous=None,
        )
        repository = FireScenarioRepository(session, ctx)
        if repository.active_by_name_key(validated.name_key) is not None:
            raise FireError("name", "duplicate_name")
        row = repository.create(
            **{
                **_scenario_values(validated),
                "currency": currency,
                "start_year": start_year,
                "is_default": repository.active_default() is None,
            }
        )
        _reconcile_rows(session, ctx, row.id, validated)
        _audit(session, ctx, "fire_scenario.created", row)
        return row.id

    return run_immediate(factory, write)


def update_fire_scenario(
    factory: sessionmaker[Session], ctx: Context, scenario_id: UUID, scenario: FireScenarioInput
) -> UUID:
    """Replace an active scenario's inputs and both child lists in one write."""
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> UUID:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        repository = FireScenarioRepository(session, ctx)
        row = repository.get_or_raise(scenario_id)
        if row.archived_at is not None:
            raise FireError("scenario", "archived")
        validated = _validate(
            scenario,
            start_year=row.start_year,
            currency=row.currency,
            default_horizon=row.horizon_age,
            previous=row,
        )
        existing = repository.active_by_name_key(validated.name_key)
        if existing is not None and existing.id != row.id:
            raise FireError("name", "duplicate_name")
        repository.update(row.id, **_scenario_values(validated))
        _reconcile_rows(session, ctx, row.id, validated)
        _audit(session, ctx, "fire_scenario.updated", row)
        return row.id

    return run_immediate(factory, write)


def duplicate_fire_scenario(
    factory: sessionmaker[Session], ctx: Context, scenario_id: UUID
) -> UUID:
    """Copy a scenario (active or archived) under a free ``(copy)`` name."""
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> UUID:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        repository = FireScenarioRepository(session, ctx)
        source = repository.get_or_raise(scenario_id)
        name = _free_copy_name(source.name, repository.active_name_keys())
        row = repository.create(
            name=name,
            name_key=text_key(name),
            currency=source.currency,
            birth_year=source.birth_year,
            start_year=source.start_year,
            inflation_bp=source.inflation_bp,
            real_return_bp=source.real_return_bp,
            income_growth_bp=source.income_growth_bp,
            stop_working_age=source.stop_working_age,
            horizon_age=source.horizon_age,
            rule=source.rule,
            swr_bp=source.swr_bp,
            income_mode=source.income_mode,
            income_manual_minor=source.income_manual_minor,
            expenses_mode=source.expenses_mode,
            expenses_manual_minor=source.expenses_manual_minor,
            portfolio_mode=source.portfolio_mode,
            portfolio_manual_minor=source.portfolio_manual_minor,
            is_default=False,
        )
        _copy_rows(session, ctx, source.id, row.id)
        _audit(session, ctx, "fire_scenario.duplicated", row)
        return row.id

    return run_immediate(factory, write)


def archive_fire_scenario(factory: sessionmaker[Session], ctx: Context, scenario_id: UUID) -> UUID:
    """Archive an active scenario; the next active one becomes the default if needed."""
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> UUID:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        repository = FireScenarioRepository(session, ctx)
        row = repository.get_or_raise(scenario_id)
        if row.archived_at is not None:
            return row.id
        was_default = row.is_default
        repository.update(row.id, archived_at=ctx.clock.now(), is_default=False)
        if was_default:
            next_row = repository.first_active()
            if next_row is not None:
                _promote_default(session, ctx, repository, next_row)
        _audit(session, ctx, "fire_scenario.archived", row)
        return row.id

    return run_immediate(factory, write)


def restore_fire_scenario(factory: sessionmaker[Session], ctx: Context, scenario_id: UUID) -> UUID:
    """Restore an archived scenario; it becomes the default when no active one is left."""
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> UUID:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        repository = FireScenarioRepository(session, ctx)
        row = repository.get_or_raise(scenario_id)
        if row.archived_at is None:
            return row.id
        active_keys = repository.active_name_keys()
        name = row.name
        if row.name_key in active_keys:
            name = _free_copy_name(row.name, active_keys)
        repository.update(
            row.id,
            archived_at=None,
            is_default=False,
            name=name,
            name_key=text_key(name),
        )
        if repository.active_default() is None:
            _promote_default(session, ctx, repository, row)
        _audit(session, ctx, "fire_scenario.restored", row)
        return row.id

    return run_immediate(factory, write)


def set_default_fire_scenario(
    factory: sessionmaker[Session], ctx: Context, scenario_id: UUID
) -> UUID:
    """Mark one active scenario as the default (the previous default is cleared)."""
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> UUID:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        repository = FireScenarioRepository(session, ctx)
        row = repository.get_or_raise(scenario_id)
        if row.archived_at is not None:
            raise FireError("scenario", "archived")
        if not row.is_default:
            _promote_default(session, ctx, repository, row)
            _audit(session, ctx, "fire_scenario.default_changed", row)
        return row.id

    return run_immediate(factory, write)


__all__ = [
    "ContributionInput",
    "ContributionView",
    "EventInput",
    "EventView",
    "FireError",
    "FireScenarioDefaults",
    "FireScenarioDetail",
    "FireScenarioInput",
    "FireScenarioList",
    "FireScenarioView",
    "archive_fire_scenario",
    "create_fire_scenario",
    "duplicate_fire_scenario",
    "get_fire_scenario",
    "list_fire_scenarios",
    "restore_fire_scenario",
    "scenario_defaults",
    "set_default_fire_scenario",
    "update_fire_scenario",
]
