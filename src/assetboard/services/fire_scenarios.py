"""The one FIRE plan of a workspace, its chooser and its one-time migration (F8.8).

A plan is a current-year projection: no start year is stored, no name, no linked mode and no
default/archive lifecycle. The plan keeps a currency snapshot and ``needs_review``. Old v1
scenarios stay readable in a temporary ``legacy`` JSON payload until an editor chooses one; the
chosen row is migrated in place and the others are hard-deleted with an audit event.

Validation lives here, not in the route: roles, birth year, rates, age horizon, saving-override
uniqueness and the event year windows. Writes run under the workspace lock and re-check the role
with ``require_role_now``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from assetboard.db.engine import run_immediate
from assetboard.db.models.fire import FireEvent, FireScenario
from assetboard.db.models.identity import User
from assetboard.db.repositories import (
    AuditEventRepository,
    CurrentWorkspaceRepository,
    FireEventRepository,
    FireScenarioRepository,
    NotFound,
)
from assetboard.domain.fire import (
    DEFAULT_LIFE_EXPECTANCY,
    ILLUSTRATIVE_INCOME_GROWTH_BP,
    ILLUSTRATIVE_INFLATION_BP,
    ILLUSTRATIVE_REAL_RETURN_BP,
    ILLUSTRATIVE_SWR_BP,
    MAX_EVENT_ROWS,
    MAX_HORIZON_AGE,
    MAX_STOP_WORKING_AGE,
    MAX_YEAR,
    MIN_HORIZON_AGE,
    MIN_YEAR,
    RATE_HARD_MAX_BP,
    RATE_HARD_MIN_BP,
    SWR_HARD_MAX_BP,
    SWR_HARD_MIN_BP,
    EventKind,
    Rule,
    age_at_start,
    clean_event_label,
    default_horizon_age,
    extreme_rate_change,
)
from assetboard.domain.money import Money
from assetboard.domain.transactions import MAX_AMOUNT_MINOR
from assetboard.services.context import Context, Role, require_role, require_role_now
from assetboard.services.fire_linked import LinkedInputs, linked_inputs_in_session

LEGACY_NAME = "FIRE plan"


class FireError(Exception):
    """Invalid plan input; ``field`` names the form control and ``code`` the message."""

    def __init__(self, field: str, code: str) -> None:
        super().__init__(code)
        self.field = field
        self.code = code


@dataclass(frozen=True, slots=True)
class EventInput:
    year: int
    kind: str
    amount_minor: int
    label: str
    # The saved row this editor row came from; ``None`` creates a new event.
    id: UUID | None = None


@dataclass(frozen=True, slots=True)
class FirePlanInput:
    birth_year: int | None = None
    inflation_bp: int = ILLUSTRATIVE_INFLATION_BP
    real_return_bp: int = ILLUSTRATIVE_REAL_RETURN_BP
    income_growth_bp: int = ILLUSTRATIVE_INCOME_GROWTH_BP
    stop_working_age: int = 0
    horizon_age: int | None = None
    rule: str = Rule.RETURNS_COVER_EXPENSES.value
    swr_bp: int | None = None
    income: Money | None = None
    expenses: Money | None = None
    portfolio: Money | None = None
    extreme_rates_confirmed: bool = False
    events: tuple[EventInput, ...] = ()


@dataclass(frozen=True, slots=True)
class FirePlanDefaults:
    current_year: int
    currency: str
    birth_year: int | None
    life_expectancy: int
    inflation_bp: int = ILLUSTRATIVE_INFLATION_BP
    real_return_bp: int = ILLUSTRATIVE_REAL_RETURN_BP
    income_growth_bp: int = ILLUSTRATIVE_INCOME_GROWTH_BP
    swr_bp: int = ILLUSTRATIVE_SWR_BP


@dataclass(frozen=True, slots=True)
class EventView:
    id: UUID
    year: int
    kind: str
    amount: Money
    label: str
    archived: bool = False


@dataclass(frozen=True, slots=True)
class FirePlanView:
    id: UUID
    currency: str
    birth_year: int
    current_year: int
    age_at_start: int
    inflation_bp: int
    real_return_bp: int
    income_growth_bp: int
    stop_working_age: int
    horizon_age: int
    rule: str
    swr_bp: int | None
    income: Money
    expenses: Money
    portfolio: Money
    needs_review: bool
    currency_mismatch: bool
    event_count: int
    archived_event_count: int
    import_key: str | None = None

    @property
    def last_year(self) -> int:
        return self.birth_year + self.horizon_age

    @property
    def stop_after_horizon(self) -> bool:
        return self.stop_working_age > self.horizon_age

    @property
    def income_stopped(self) -> bool:
        """The current age is at or beyond the stop age: base income is zero."""
        return self.stop_working_age <= self.age_at_start

    @property
    def horizon_reached(self) -> bool:
        return self.horizon_age <= self.age_at_start


@dataclass(frozen=True, slots=True)
class FirePlanDetail:
    plan: FirePlanView
    events: tuple[EventView, ...]
    active_events: tuple[EventView, ...]
    archived_events: tuple[EventView, ...]


@dataclass(frozen=True, slots=True)
class LegacyScenarioView:
    id: UUID
    name: str
    start_year: int
    age_at_start: int
    currency: str
    is_default: bool
    archived: bool
    event_count: int
    invalid: bool = False


@dataclass(frozen=True, slots=True)
class FirePlanMigrationResult:
    plan_id: UUID
    legacy_name: str
    deleted_count: int
    zeroed_fields: tuple[str, ...]
    archived_event_count: int
    already_migrated: bool = False


@dataclass(frozen=True, slots=True)
class PlanEntry:
    plan: FirePlanView | None
    legacy: tuple[LegacyScenarioView, ...] = ()
    migration: FirePlanMigrationResult | None = None


@dataclass(frozen=True, slots=True)
class ValidatedPlan:
    birth_year: int
    horizon_age: int
    stop_working_age: int
    inflation_bp: int
    real_return_bp: int
    income_growth_bp: int
    rule: Rule
    swr_bp: int | None
    income_minor: int
    expenses_minor: int
    portfolio_minor: int
    events: tuple[EventInput, ...]
    needs_extreme_confirmation: bool = False


RateTuple = tuple[int, int, int, int | None]


def _require_viewer(ctx: Context) -> None:
    require_role(ctx, Role.VIEWER)


def _integer(value: object, field: str, code: str) -> int:
    if type(value) is not int:
        raise FireError(field, code)
    return value


def _rate(value: object, field: str, code: str) -> int:
    rate = _integer(value, field, code)
    if not RATE_HARD_MIN_BP <= rate <= RATE_HARD_MAX_BP:
        raise FireError(field, code)
    return rate


def _amount(field: str, amount: Money | None, currency: str) -> int:
    if amount is None or amount.currency != currency:
        raise FireError(field, "invalid_amount")
    if not 0 <= amount.amount_minor <= MAX_AMOUNT_MINOR:
        raise FireError(field, "invalid_amount")
    return amount.amount_minor


def validate_plan_input(
    plan: FirePlanInput,
    *,
    current_year: int,
    currency: str,
    default_horizon: int,
    previous_rates: RateTuple | None = None,
    saved_events: dict[UUID, int] | None = None,
    preview: bool = False,
    allow_events: bool = True,
) -> ValidatedPlan:
    """Check every field, amount, row and rate of a plan.

    ``saved_events`` maps saved event ids to their saved year. It lets an old event that became
    past (or a saving override that became the current year after rollover) stay on the form
    without blocking an unrelated save; a new row still may not be created in the past.
    """
    if plan.birth_year is None:
        raise FireError("birth_year", "birth_year_required")
    birth_year = _integer(plan.birth_year, "birth_year", "invalid_birth_year")
    if not MIN_YEAR <= birth_year <= MAX_YEAR or birth_year > current_year:
        raise FireError("birth_year", "invalid_birth_year")
    age = age_at_start(current_year, birth_year)

    horizon_age = _integer(
        plan.horizon_age if plan.horizon_age is not None else default_horizon,
        "horizon_age",
        "invalid_horizon",
    )
    if not MIN_HORIZON_AGE <= horizon_age <= MAX_HORIZON_AGE:
        raise FireError("horizon_age", "invalid_horizon")
    if horizon_age <= age:
        raise FireError("horizon_age", "horizon_not_after_age")
    if birth_year + horizon_age > MAX_YEAR:
        raise FireError("horizon_age", "invalid_horizon")

    stop_working_age = _integer(plan.stop_working_age, "stop_working_age", "invalid_stop_age")
    if not 0 <= stop_working_age <= MAX_STOP_WORKING_AGE:
        raise FireError("stop_working_age", "invalid_stop_age")

    inflation_bp = _rate(plan.inflation_bp, "inflation", "invalid_rate")
    real_return_bp = _rate(plan.real_return_bp, "real_return", "invalid_rate")
    income_growth_bp = _rate(plan.income_growth_bp, "income_growth", "invalid_rate")

    try:
        rule = Rule(plan.rule)
    except ValueError:
        raise FireError("rule", "invalid_rule") from None
    swr_bp: int | None = None
    if rule is Rule.SWR:
        swr_bp = _integer(plan.swr_bp, "swr", "invalid_swr")
        if not SWR_HARD_MIN_BP <= swr_bp <= SWR_HARD_MAX_BP:
            raise FireError("swr", "invalid_swr")
    else:
        swr_bp = None

    income_minor = _amount("income", plan.income, currency)
    expenses_minor = _amount("expenses", plan.expenses, currency)
    portfolio_minor = _amount("portfolio", plan.portfolio, currency)

    needs_confirmation = extreme_rate_change(
        inflation_bp=inflation_bp,
        real_return_bp=real_return_bp,
        income_growth_bp=income_growth_bp,
        swr_bp=swr_bp,
        previous=previous_rates,
    )
    if needs_confirmation and not plan.extreme_rates_confirmed and not preview:
        raise FireError("extreme_rates_confirmed", "extreme_rate_confirmation_required")

    if not allow_events and plan.events:
        raise FireError("events", "events_not_allowed_on_create")
    if len(plan.events) > MAX_EVENT_ROWS:
        raise FireError("events", "too_many_events")
    last_year = birth_year + horizon_age
    events: list[EventInput] = []
    seen_ids: set[UUID] = set()
    override_years: set[int] = set()
    for row in plan.events:
        year = _integer(row.year, "events", "invalid_year")
        if not MIN_YEAR <= year <= last_year:
            raise FireError("events", "rows_outside_window")
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
        if row.id is not None:
            if row.id in seen_ids:
                raise FireError("events", "duplicate_event")
            seen_ids.add(row.id)
            saved_year = (saved_events or {}).get(row.id)
            if saved_year is None:
                raise FireError("events", "invalid_event")
            if kind is EventKind.SAVING_OVERRIDE and year <= current_year and year != saved_year:
                raise FireError("events", "saving_override_current_year")
        else:
            if year < current_year:
                raise FireError("events", "rows_outside_window")
            if kind is EventKind.SAVING_OVERRIDE and year <= current_year:
                raise FireError("events", "saving_override_current_year")
        if kind is EventKind.SAVING_OVERRIDE:
            if year in override_years:
                raise FireError("events", "duplicate_saving_override")
            override_years.add(year)
        events.append(
            EventInput(
                year=year,
                kind=kind.value,
                amount_minor=row.amount_minor,
                label=label,
                id=row.id,
            )
        )

    return ValidatedPlan(
        birth_year=birth_year,
        horizon_age=horizon_age,
        stop_working_age=stop_working_age,
        inflation_bp=inflation_bp,
        real_return_bp=real_return_bp,
        income_growth_bp=income_growth_bp,
        rule=rule,
        swr_bp=swr_bp,
        income_minor=income_minor,
        expenses_minor=expenses_minor,
        portfolio_minor=portfolio_minor,
        events=tuple(events),
        needs_extreme_confirmation=needs_confirmation,
    )


def _active(event: FireEvent | EventView, current_year: int, last_year: int) -> bool:
    if event.year > last_year:
        return False
    if event.kind == EventKind.SAVING_OVERRIDE.value:
        return event.year >= current_year + 1
    return event.year >= current_year


def _event_views(
    rows: tuple[FireEvent, ...], current_year: int, last_year: int, currency: str
) -> tuple[tuple[EventView, ...], tuple[EventView, ...]]:
    active: list[EventView] = []
    archived: list[EventView] = []
    for row in rows:
        view = EventView(
            id=row.id,
            year=row.year,
            kind=row.kind,
            amount=Money(row.amount_minor, currency),
            label=row.label,
            archived=not _active(row, current_year, last_year),
        )
        (archived if view.archived else active).append(view)
    return tuple(active), tuple(archived)


def _view(
    row: FireScenario,
    *,
    current_year: int,
    base_currency: str,
    event_count: int,
    archived_event_count: int,
) -> FirePlanView:
    currency = row.currency
    return FirePlanView(
        id=row.id,
        currency=currency,
        birth_year=row.birth_year,
        current_year=current_year,
        age_at_start=age_at_start(current_year, row.birth_year),
        inflation_bp=row.inflation_bp,
        real_return_bp=row.real_return_bp,
        income_growth_bp=row.income_growth_bp,
        stop_working_age=row.stop_working_age,
        horizon_age=row.horizon_age,
        rule=row.rule,
        swr_bp=row.swr_bp,
        income=Money(row.income_minor or 0, currency),
        expenses=Money(row.expenses_minor or 0, currency),
        portfolio=Money(row.portfolio_minor or 0, currency),
        needs_review=row.needs_review,
        currency_mismatch=currency != base_currency,
        event_count=event_count,
        archived_event_count=archived_event_count,
        import_key=row.import_key,
    )


def _detail_in_session(
    session: Session, ctx: Context, row: FireScenario, *, current_year: int, base_currency: str
) -> FirePlanDetail:
    event_rows = tuple(FireEventRepository(session, ctx).for_plan(row.id))
    active, archived = _event_views(
        event_rows, current_year, row.birth_year + row.horizon_age, row.currency
    )
    return FirePlanDetail(
        plan=_view(
            row,
            current_year=current_year,
            base_currency=base_currency,
            event_count=len(event_rows),
            archived_event_count=len(archived),
        ),
        events=active + archived,
        active_events=active,
        archived_events=archived,
    )


def _parse_legacy(row: FireScenario) -> dict[str, Any]:
    if not row.legacy:
        raise FireError("plan", "legacy_invalid")
    try:
        loaded = json.loads(row.legacy)
    except ValueError:
        raise FireError("plan", "legacy_invalid") from None
    if not isinstance(loaded, dict):
        raise FireError("plan", "legacy_invalid")
    data = cast("dict[str, Any]", loaded)
    if data.get("v") != 1:
        raise FireError("plan", "legacy_invalid")
    modes = (data.get("income_mode"), data.get("expenses_mode"), data.get("portfolio_mode"))
    if not all(isinstance(mode, str) for mode in modes):
        raise FireError("plan", "legacy_invalid")
    for key in ("name", "name_key"):
        if not isinstance(data.get(key), str):
            raise FireError("plan", "legacy_invalid")
    if type(data.get("start_year")) is not int or type(data.get("is_default")) is not bool:
        raise FireError("plan", "legacy_invalid")
    if data.get("archived_at") is not None and not isinstance(data.get("archived_at"), str):
        raise FireError("plan", "legacy_invalid")
    for key in (
        "income_manual_minor",
        "expenses_manual_minor",
        "portfolio_manual_minor",
    ):
        value = data.get(key)
        if value is not None and type(value) is not int:
            raise FireError("plan", "legacy_invalid")
    exclusions = data.get("expense_exclusion_ids", [])
    if not isinstance(exclusions, list) or not all(
        isinstance(item, str) for item in cast("list[object]", exclusions)
    ):
        raise FireError("plan", "legacy_invalid")
    return data


def _legacy_summary(row: FireScenario, current_year: int, event_count: int) -> LegacyScenarioView:
    try:
        data = _parse_legacy(row)
    except FireError:
        return LegacyScenarioView(
            id=row.id,
            name=LEGACY_NAME,
            start_year=current_year,
            age_at_start=age_at_start(current_year, row.birth_year),
            currency=row.currency,
            is_default=False,
            archived=False,
            event_count=event_count,
            invalid=True,
        )
    start_year = cast("int", data["start_year"])
    archived_at = data.get("archived_at")
    return LegacyScenarioView(
        id=row.id,
        name=str(data["name"]),
        start_year=start_year,
        age_at_start=age_at_start(start_year, row.birth_year),
        currency=row.currency,
        is_default=bool(data["is_default"]),
        archived=bool(archived_at),
        event_count=event_count,
    )


def _legacy_rank(row: FireScenario) -> tuple[int, str, str]:
    try:
        data = _parse_legacy(row)
    except FireError:
        return (0, "", str(row.id))
    return (1 if data.get("archived_at") else 0, str(data.get("name_key") or ""), str(row.id))


def _profile(session: Session, ctx: Context) -> tuple[int | None, int]:
    user = session.get(User, ctx.user.id)
    if user is None:
        return None, DEFAULT_LIFE_EXPECTANCY
    return user.birth_year, default_horizon_age(user.life_expectancy)


def _current_year(ctx: Context) -> int:
    return ctx.clock.today(ctx.workspace.timezone).year


def plan_defaults(factory: sessionmaker[Session], ctx: Context) -> FirePlanDefaults:
    """The create form's prefill: the profile's birth year and life expectancy."""
    _require_viewer(ctx)
    with factory() as session:
        birth_year, life_expectancy = _profile(session, ctx)
        currency = CurrentWorkspaceRepository(session, ctx).get().base_currency
    return FirePlanDefaults(
        current_year=_current_year(ctx),
        currency=currency,
        birth_year=birth_year,
        life_expectancy=life_expectancy,
        inflation_bp=ILLUSTRATIVE_INFLATION_BP,
        real_return_bp=ILLUSTRATIVE_REAL_RETURN_BP,
        income_growth_bp=ILLUSTRATIVE_INCOME_GROWTH_BP,
        swr_bp=ILLUSTRATIVE_SWR_BP,
    )


def _plan_entry_in_session(session: Session, ctx: Context, *, current_year: int) -> PlanEntry:
    repository = FireScenarioRepository(session, ctx)
    live = repository.live()
    base_currency = CurrentWorkspaceRepository(session, ctx).get().base_currency
    if live is not None:
        detail = _detail_in_session(
            session, ctx, live, current_year=current_year, base_currency=base_currency
        )
        return PlanEntry(plan=detail.plan)
    legacy_rows = sorted(repository.legacy(), key=_legacy_rank)
    if not legacy_rows:
        return PlanEntry(plan=None)
    counts = FireEventRepository(session, ctx).counts_by_scenario()
    return PlanEntry(
        plan=None,
        legacy=tuple(
            _legacy_summary(row, current_year, counts.get(row.id, 0)) for row in legacy_rows
        ),
    )


def plan_entry(
    factory: sessionmaker[Session], ctx: Context, *, auto_migrate: bool = True
) -> PlanEntry:
    """The `/fire` entry point: live plan, legacy chooser or an empty workspace."""
    _require_viewer(ctx)
    current_year = _current_year(ctx)
    with factory() as session:
        entry = _plan_entry_in_session(session, ctx, current_year=current_year)
    if entry.plan is not None or not entry.legacy:
        return entry
    if len(entry.legacy) == 1 and auto_migrate and ctx.role is not Role.VIEWER:
        migration = migrate_legacy_plan(factory, ctx, None)
        return PlanEntry(plan=None, legacy=(), migration=migration)
    return entry


def get_fire_plan(factory: sessionmaker[Session], ctx: Context) -> FirePlanDetail:
    detail = get_fire_plan_if_any(factory, ctx)
    if detail is None:
        raise NotFound
    return detail


def get_fire_plan_if_any(factory: sessionmaker[Session], ctx: Context) -> FirePlanDetail | None:
    _require_viewer(ctx)
    with factory() as session:
        row = FireScenarioRepository(session, ctx).live()
        if row is None:
            return None
        base_currency = CurrentWorkspaceRepository(session, ctx).get().base_currency
        return _detail_in_session(
            session, ctx, row, current_year=_current_year(ctx), base_currency=base_currency
        )


def _check_currency(session: Session, ctx: Context, row: FireScenario) -> None:
    if row.currency != CurrentWorkspaceRepository(session, ctx).get().base_currency:
        raise FireError("plan", "plan_currency_mismatch")


def _audit(
    session: Session,
    ctx: Context,
    action: str,
    row: FireScenario,
    data: dict[str, str | int | bool] | None = None,
) -> None:
    AuditEventRepository(session, ctx).record(
        actor_user_id=ctx.user.id,
        action=action,
        entity_type="fire_plan",
        entity_id=str(row.id),
        ts=ctx.clock.now(),
        data=data or {},
    )


def create_fire_plan(factory: sessionmaker[Session], ctx: Context, plan: FirePlanInput) -> UUID:
    """Create the single plan; only when no plan or legacy row exists."""
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> UUID:
        require_role_now(session, ctx, Role.EDITOR)
        workspace = CurrentWorkspaceRepository(session, ctx)
        workspace.lock(exclusive=True)
        repository = FireScenarioRepository(session, ctx)
        if repository.live() is not None:
            raise FireError("plan", "plan_exists")
        if repository.legacy_count():
            raise FireError("plan", "migration_required")
        currency = workspace.get().base_currency
        current_year = _current_year(ctx)
        _, profile_horizon = _profile(session, ctx)
        validated = validate_plan_input(
            plan,
            current_year=current_year,
            currency=currency,
            default_horizon=profile_horizon,
            allow_events=False,
        )
        row = repository.create(
            currency=currency,
            birth_year=validated.birth_year,
            stop_working_age=validated.stop_working_age,
            horizon_age=validated.horizon_age,
            inflation_bp=validated.inflation_bp,
            real_return_bp=validated.real_return_bp,
            income_growth_bp=validated.income_growth_bp,
            rule=validated.rule.value,
            swr_bp=validated.swr_bp,
            income_minor=validated.income_minor,
            expenses_minor=validated.expenses_minor,
            portfolio_minor=validated.portfolio_minor,
            needs_review=False,
            is_live=True,
            legacy=None,
        )
        _audit(session, ctx, "fire_plan.created", row)
        return row.id

    return run_immediate(factory, write)


def _reconcile_events(
    session: Session, ctx: Context, plan_id: UUID, validated: ValidatedPlan
) -> None:
    events = FireEventRepository(session, ctx)
    existing = {row.id: row for row in events.for_plan(plan_id)}
    for row in validated.events:
        if row.id is None:
            events.create(
                scenario_id=plan_id,
                year=row.year,
                kind=row.kind,
                amount_minor=row.amount_minor,
                label=row.label,
            )
            continue
        current = existing.pop(row.id, None)
        if current is None:
            raise FireError("events", "invalid_event")
        if (current.year, current.kind, current.amount_minor, current.label) != (
            row.year,
            row.kind,
            row.amount_minor,
            row.label,
        ):
            events.update(
                current.id,
                year=row.year,
                kind=row.kind,
                amount_minor=row.amount_minor,
                label=row.label,
            )
    for row in existing.values():
        events.delete(row)


def update_fire_plan(factory: sessionmaker[Session], ctx: Context, plan: FirePlanInput) -> UUID:
    """Replace the live plan's inputs and event list; clears ``needs_review``."""
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> UUID:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        repository = FireScenarioRepository(session, ctx)
        row = repository.live()
        if row is None:
            raise NotFound
        _check_currency(session, ctx, row)
        saved_events = {
            event.id: event.year for event in FireEventRepository(session, ctx).for_plan(row.id)
        }
        validated = validate_plan_input(
            plan,
            current_year=_current_year(ctx),
            currency=row.currency,
            default_horizon=row.horizon_age,
            previous_rates=(
                row.inflation_bp,
                row.real_return_bp,
                row.income_growth_bp,
                row.swr_bp,
            ),
            saved_events=saved_events,
        )
        repository.update(
            row.id,
            birth_year=validated.birth_year,
            stop_working_age=validated.stop_working_age,
            horizon_age=validated.horizon_age,
            inflation_bp=validated.inflation_bp,
            real_return_bp=validated.real_return_bp,
            income_growth_bp=validated.income_growth_bp,
            rule=validated.rule.value,
            swr_bp=validated.swr_bp,
            income_minor=validated.income_minor,
            expenses_minor=validated.expenses_minor,
            portfolio_minor=validated.portfolio_minor,
            needs_review=False,
        )
        _reconcile_events(session, ctx, row.id, validated)
        _audit(session, ctx, "fire_plan.updated", row)
        return row.id

    return run_immediate(factory, write)


def delete_fire_plan(
    factory: sessionmaker[Session], ctx: Context, *, confirmed: bool = False
) -> None:
    """Hard-delete the plan and its events, after the UI's typed confirmation."""
    require_role(ctx, Role.EDITOR)
    if not confirmed:
        raise FireError("plan", "confirmation_required")

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        repository = FireScenarioRepository(session, ctx)
        row = repository.live()
        if row is None:
            raise NotFound
        events = FireEventRepository(session, ctx)
        rows = events.for_plan(row.id)
        count = len(rows)
        for event in rows:
            events.delete(event)
        repository.delete(row)
        _audit(session, ctx, "fire_plan.deleted", row, {"events_deleted": count})

    run_immediate(factory, write)


def _linked_amount(
    linked: LinkedInputs | None, field: str, direction: str
) -> tuple[int | None, bool]:
    if linked is not None and any(block.field == field for block in linked.blocks):
        return None, True
    if linked is None:
        return None, True
    if direction == "portfolio":
        total = linked.portfolio.total if linked.portfolio is not None else None
    else:
        series = linked.income if direction == "income" else linked.expenses
        total = series.annual_total if series is not None else None
    return (total.amount_minor if total is not None else None), total is None


def migrate_legacy_plan(
    factory: sessionmaker[Session], ctx: Context, scenario_id: UUID | None
) -> FirePlanMigrationResult:
    """Choose one old scenario (or auto-migrate the only one), migrate and delete the rest."""
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> FirePlanMigrationResult:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        repository = FireScenarioRepository(session, ctx)
        live = repository.live()
        if live is not None:
            base_currency = CurrentWorkspaceRepository(session, ctx).get().base_currency
            detail = _detail_in_session(
                session, ctx, live, current_year=_current_year(ctx), base_currency=base_currency
            )
            return FirePlanMigrationResult(
                plan_id=live.id,
                legacy_name="",
                deleted_count=0,
                zeroed_fields=(),
                archived_event_count=len(detail.archived_events),
                already_migrated=True,
            )
        rows = sorted(repository.legacy(), key=_legacy_rank)
        if scenario_id is None:
            if len(rows) != 1:
                raise FireError("plan", "choice_required")
            chosen = rows[0]
        else:
            chosen = next((row for row in rows if row.id == scenario_id), None)
            if chosen is None:
                raise NotFound
        legacy = _parse_legacy(chosen)
        current_year = _current_year(ctx)
        modes = (
            str(legacy["income_mode"]),
            str(legacy["expenses_mode"]),
            str(legacy["portfolio_mode"]),
        )
        linked: LinkedInputs | None = None
        if any(mode != "manual" for mode in modes):
            raw_exclusions = cast("list[object]", legacy.get("expense_exclusion_ids", []))
            exclusion_ids = tuple(UUID(str(item)) for item in raw_exclusions)
            linked = linked_inputs_in_session(
                session,
                ctx,
                currency=chosen.currency,
                start_year=current_year,
                income_mode=modes[0],
                expenses_mode=modes[1],
                portfolio_mode=modes[2],
                expense_exclusion_ids=exclusion_ids,
            )
        zeroed: list[str] = []

        def amount(mode: str, manual: object, field: str, direction: str) -> int:
            if mode == "manual":
                if type(manual) is not int:
                    zeroed.append(field)
                    return 0
                return manual
            resolved, missing = _linked_amount(linked, field, direction)
            if missing or resolved is None:
                zeroed.append(field)
                return 0
            return resolved

        income_minor = amount(
            str(legacy["income_mode"]), legacy.get("income_manual_minor"), "income_manual", "income"
        )
        expenses_minor = amount(
            str(legacy["expenses_mode"]),
            legacy.get("expenses_manual_minor"),
            "expenses_manual",
            "expenses",
        )
        portfolio_minor = amount(
            str(legacy["portfolio_mode"]),
            legacy.get("portfolio_manual_minor"),
            "portfolio_manual",
            "portfolio",
        )
        repository.update(
            chosen.id,
            income_minor=income_minor,
            expenses_minor=expenses_minor,
            portfolio_minor=portfolio_minor,
            is_live=True,
            legacy=None,
            needs_review=True,
        )
        deleted = 0
        for row in rows:
            if row.id == chosen.id:
                continue
            repository.delete(row)
            deleted += 1
        event_rows = tuple(FireEventRepository(session, ctx).for_plan(chosen.id))
        archived_count = sum(
            1
            for event in event_rows
            if not _active(event, current_year, chosen.birth_year + chosen.horizon_age)
        )
        _audit(
            session,
            ctx,
            "fire_plan.migrated",
            chosen,
            {
                "legacy_name": str(legacy["name"]),
                "deleted_scenarios": deleted,
                "zeroed_fields": ", ".join(zeroed),
                "archived_event_count": archived_count,
            },
        )
        return FirePlanMigrationResult(
            plan_id=chosen.id,
            legacy_name=str(legacy["name"]),
            deleted_count=deleted,
            zeroed_fields=tuple(zeroed),
            archived_event_count=archived_count,
        )

    return run_immediate(factory, write)


__all__ = [
    "EventInput",
    "EventView",
    "FireError",
    "FirePlanDefaults",
    "FirePlanDetail",
    "FirePlanInput",
    "FirePlanMigrationResult",
    "FirePlanView",
    "LegacyScenarioView",
    "PlanEntry",
    "RateTuple",
    "ValidatedPlan",
    "create_fire_plan",
    "delete_fire_plan",
    "get_fire_plan",
    "get_fire_plan_if_any",
    "migrate_legacy_plan",
    "plan_defaults",
    "plan_entry",
    "update_fire_plan",
    "validate_plan_input",
]
