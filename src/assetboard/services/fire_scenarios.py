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
    """Invalid plan input; ``field`` names the form control and ``code`` the message.

    ``row`` and ``column`` identify an adjustment row and one of its controls for the
    adjustments editor's inline errors. They are optional so plan-level errors keep the old
    two-argument shape.
    """

    def __init__(
        self,
        field: str,
        code: str,
        *,
        row: str | None = None,
        column: str | None = None,
    ) -> None:
        super().__init__(code)
        self.field = field
        self.code = code
        self.row = row
        self.column = column


@dataclass(frozen=True, slots=True)
class EventInput:
    year: int
    kind: str
    amount_minor: int
    label: str
    # The saved row this editor row came from; ``None`` creates a new event.
    id: UUID | None = None
    # The form row key, used only to attach an inline error; it is never persisted.
    index: str | None = None


@dataclass(frozen=True, slots=True)
class SavedEvent:
    """A saved adjustment as it stands before an update or preview.

    ``archived`` is computed by the caller from the **saved** plan's window and the injected
    clock, so the service never classifies an event against a horizon the user has not saved.
    """

    year: int
    kind: str
    amount_minor: int
    label: str
    archived: bool = False


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
    # Saved rows the user explicitly removed; empty means no removal.
    removed_event_ids: tuple[UUID, ...] = ()


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
    # The fully loaded plan and events, so the view page never loads the plan twice (F8.9).
    detail: FirePlanDetail | None = None


@dataclass(frozen=True, slots=True)
class ValidatedRates:
    """The plan's rate fields after validation; the caller applies the confirmation gate."""

    inflation_bp: int
    real_return_bp: int
    income_growth_bp: int
    rule: Rule
    swr_bp: int | None
    needs_extreme_confirmation: bool


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
    removed_event_ids: tuple[UUID, ...] = ()


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


def validate_amount(
    field: str, amount: Money | None, currency: str, *, required: bool = False
) -> int:
    """One non-negative manual amount; the wizard requires a value, the saved plan has one."""
    if amount is None:
        raise FireError(field, "amount_required" if required else "invalid_amount")
    if amount.currency != currency:
        raise FireError(field, "invalid_amount")
    if not 0 <= amount.amount_minor <= MAX_AMOUNT_MINOR:
        raise FireError(field, "invalid_amount")
    return amount.amount_minor


def validate_ages(
    birth_year: object | None,
    horizon_age: object | None,
    stop_working_age: object,
    *,
    current_year: int,
    default_horizon: int,
) -> tuple[int, int, int]:
    """Birth year, horizon and stop age, shared by the saved plan and the wizard steps."""
    if birth_year is None:
        raise FireError("birth_year", "birth_year_required")
    year = _integer(birth_year, "birth_year", "invalid_birth_year")
    if not MIN_YEAR <= year <= MAX_YEAR or year > current_year:
        raise FireError("birth_year", "invalid_birth_year")
    age = age_at_start(current_year, year)

    horizon = _integer(
        horizon_age if horizon_age is not None else default_horizon,
        "horizon_age",
        "invalid_horizon",
    )
    if not MIN_HORIZON_AGE <= horizon <= MAX_HORIZON_AGE:
        raise FireError("horizon_age", "invalid_horizon")
    if horizon <= age:
        raise FireError("horizon_age", "horizon_not_after_age")
    if year + horizon > MAX_YEAR:
        raise FireError("horizon_age", "invalid_horizon")

    stop_age = _integer(stop_working_age, "stop_working_age", "invalid_stop_age")
    if not 0 <= stop_age <= MAX_STOP_WORKING_AGE:
        raise FireError("stop_working_age", "invalid_stop_age")
    return year, horizon, stop_age


def validate_rates(
    *,
    inflation_bp: object,
    real_return_bp: object,
    income_growth_bp: object,
    rule: str,
    swr_bp: int | None,
    previous_rates: RateTuple | None = None,
) -> ValidatedRates:
    """The four rate fields and their hard bounds; the confirmation gate stays with the caller."""
    inflation = _rate(inflation_bp, "inflation", "invalid_rate")
    real_return = _rate(real_return_bp, "real_return", "invalid_rate")
    income_growth = _rate(income_growth_bp, "income_growth", "invalid_rate")
    try:
        independence_rule = Rule(rule)
    except ValueError:
        raise FireError("rule", "invalid_rule") from None
    swr: int | None = None
    if independence_rule is Rule.SWR:
        swr = _integer(swr_bp, "swr", "invalid_swr")
        if not SWR_HARD_MIN_BP <= swr <= SWR_HARD_MAX_BP:
            raise FireError("swr", "invalid_swr")
    needs_confirmation = extreme_rate_change(
        inflation_bp=inflation,
        real_return_bp=real_return,
        income_growth_bp=income_growth,
        swr_bp=swr,
        previous=previous_rates,
    )
    return ValidatedRates(
        inflation_bp=inflation,
        real_return_bp=real_return,
        income_growth_bp=income_growth,
        rule=independence_rule,
        swr_bp=swr,
        needs_extreme_confirmation=needs_confirmation,
    )


def validate_plan_input(
    plan: FirePlanInput,
    *,
    current_year: int,
    currency: str,
    default_horizon: int,
    previous_rates: RateTuple | None = None,
    saved_events: dict[UUID, SavedEvent] | None = None,
    preview: bool = False,
    allow_events: bool = True,
) -> ValidatedPlan:
    """Check every field, amount, row and rate of a plan.

    ``saved_events`` maps saved event ids to their saved row and whether it is archived under the
    saved plan's window. It supports three rules that keep an edit safe:

    - every saved **active** event must be posted or explicitly removed, so a partial POST can
      never delete a row by omission;
    - an archived row posted as an active row is read-only, except the F8.8 rollover case where a
      saving override has just become the current year;
    - an unchanged row may keep a year outside the newly submitted window, so shrinking or
      expanding the horizon archives or restores events by calculation.

    An archived event that becomes active under the submitted window is added unchanged to the
    validated events, so the preview and the save use the same projection input.
    """
    birth_year, horizon_age, stop_working_age = validate_ages(
        plan.birth_year,
        plan.horizon_age,
        plan.stop_working_age,
        current_year=current_year,
        default_horizon=default_horizon,
    )
    rates = validate_rates(
        inflation_bp=plan.inflation_bp,
        real_return_bp=plan.real_return_bp,
        income_growth_bp=plan.income_growth_bp,
        rule=plan.rule,
        swr_bp=plan.swr_bp,
        previous_rates=previous_rates,
    )

    income_minor = validate_amount("income_manual", plan.income, currency)
    expenses_minor = validate_amount("expenses_manual", plan.expenses, currency)
    portfolio_minor = validate_amount("portfolio_manual", plan.portfolio, currency)

    if rates.needs_extreme_confirmation and not plan.extreme_rates_confirmed and not preview:
        raise FireError("extreme_rates_confirmed", "extreme_rate_confirmation_required")

    if not allow_events and (plan.events or plan.removed_event_ids):
        raise FireError("events", "events_not_allowed_on_create")
    last_year = birth_year + horizon_age
    events: list[EventInput] = []
    posted_ids: set[UUID] = set()
    posted_rows_by_id: dict[UUID, str | None] = {}
    posted_override_rows: dict[int, str | None] = {}
    override_years: set[int] = set()
    new_count = 0
    saved = saved_events or {}

    for row in plan.events:
        row_key = row.index if isinstance(row.index, str) else None
        year = row.year
        if type(year) is not int:
            raise FireError("events", "invalid_year", row=row_key, column="year")
        try:
            kind = EventKind(row.kind)
        except (TypeError, ValueError):
            raise FireError("events", "invalid_event_kind", row=row_key, column="kind") from None
        amount_minor = row.amount_minor
        if type(amount_minor) is not int or abs(amount_minor) > MAX_AMOUNT_MINOR:
            raise FireError("events", "invalid_amount", row=row_key, column="amount")
        try:
            label = clean_event_label(row.label)
        except ValueError:
            raise FireError("events", "invalid_label", row=row_key, column="label") from None

        current = None
        if row.id is not None:
            if row.id in posted_ids:
                raise FireError("events", "duplicate_event", row=row_key)
            current = saved.get(row.id)
            if current is None:
                raise FireError("events", "invalid_event", row=row_key)
            posted_ids.add(row.id)
            posted_rows_by_id[row.id] = row_key
            unchanged = (year, kind.value, amount_minor, label) == (
                current.year,
                current.kind,
                current.amount_minor,
                current.label,
            )
            if current.archived:
                rollover = (
                    kind is EventKind.SAVING_OVERRIDE
                    and current.kind == EventKind.SAVING_OVERRIDE.value
                    and current.year == current_year
                    and year == current.year
                )
                if not unchanged and not rollover:
                    raise FireError(
                        "events", "archived_event_read_only", row=row_key, column="year"
                    )
                # An archived row may keep a year outside the submitted window; it stays
                # archived, or becomes active again if the horizon now covers it.
            elif year != current.year:
                # A changed year must fit the submitted window. An unchanged year is allowed
                # outside it so a horizon shrink can archive events by calculation.
                if kind is EventKind.SAVING_OVERRIDE and year <= current_year:
                    raise FireError(
                        "events", "saving_override_current_year", row=row_key, column="year"
                    )
                if not current_year <= year <= last_year:
                    raise FireError("events", "rows_outside_window", row=row_key, column="year")
        else:
            new_count += 1
            if not current_year <= year <= last_year:
                raise FireError("events", "rows_outside_window", row=row_key, column="year")
            if kind is EventKind.SAVING_OVERRIDE and year <= current_year:
                raise FireError(
                    "events", "saving_override_current_year", row=row_key, column="year"
                )

        if kind is EventKind.SAVING_OVERRIDE:
            if year in override_years:
                raise FireError("events", "duplicate_saving_override", row=row_key, column="year")
            override_years.add(year)
            posted_override_rows[year] = row_key
        events.append(
            EventInput(
                year=year,
                kind=kind.value,
                amount_minor=amount_minor,
                label=label,
                id=row.id,
                index=row_key,
            )
        )

    removed_ids: list[UUID] = []
    removed_set: set[UUID] = set()
    for event_id in plan.removed_event_ids:
        if event_id not in saved:
            raise FireError("events", "invalid_event")
        if event_id in removed_set:
            raise FireError("events", "duplicate_event")
        if event_id in posted_ids:
            raise FireError("events", "duplicate_event", row=posted_rows_by_id.get(event_id))
        removed_set.add(event_id)
        removed_ids.append(event_id)

    # Strict partial-POST protection: a missing active row is an error, never a deletion.
    expected_active = {event_id for event_id, event in saved.items() if not event.archived}
    if not expected_active <= posted_ids | removed_set:
        raise FireError("events", "invalid_event")

    # An archived event that the submitted window now covers is part of the projection even
    # though the editor still renders it as archived. Add it unchanged; reconciliation leaves
    # the saved row in place.
    newly_active_ids: set[UUID] = set()
    for event_id, event in saved.items():
        if event_id in posted_ids or event_id in removed_set or not event.archived:
            continue
        if not event_is_active(event.year, event.kind, current_year, last_year):
            continue
        kind = EventKind(event.kind)
        if kind is EventKind.SAVING_OVERRIDE:
            if event.year in override_years:
                raise FireError(
                    "events",
                    "duplicate_saving_override",
                    row=posted_override_rows.get(event.year),
                    column="year",
                )
            override_years.add(event.year)
        newly_active_ids.add(event_id)
        events.append(
            EventInput(
                year=event.year,
                kind=event.kind,
                amount_minor=event.amount_minor,
                label=event.label,
                id=event_id,
            )
        )

    # A saved override that stays archived still occupies its year in the unique index; a new
    # or moved override must not silently collide with it.
    preserved_override_years = {
        event.year
        for event_id, event in saved.items()
        if event_id not in posted_ids
        and event_id not in removed_set
        and event_id not in newly_active_ids
        and event.kind == EventKind.SAVING_OVERRIDE.value
    }
    for year, row_key in posted_override_rows.items():
        if year in preserved_override_years:
            raise FireError("events", "duplicate_saving_override", row=row_key, column="year")

    removed_saved_count = sum(1 for event_id in removed_ids if event_id in saved)
    total_events = len(saved) + new_count - removed_saved_count
    if total_events > MAX_EVENT_ROWS:
        raise FireError("events", "too_many_events")

    return ValidatedPlan(
        birth_year=birth_year,
        horizon_age=horizon_age,
        stop_working_age=stop_working_age,
        inflation_bp=rates.inflation_bp,
        real_return_bp=rates.real_return_bp,
        income_growth_bp=rates.income_growth_bp,
        rule=rates.rule,
        swr_bp=rates.swr_bp,
        income_minor=income_minor,
        expenses_minor=expenses_minor,
        portfolio_minor=portfolio_minor,
        events=tuple(events),
        needs_extreme_confirmation=rates.needs_extreme_confirmation,
        removed_event_ids=tuple(removed_ids),
    )


def event_is_active(year: int, kind: str, current_year: int, last_year: int) -> bool:
    """Whether an event applies to the projection; past events stay as archived history."""
    if year > last_year:
        return False
    if kind == EventKind.SAVING_OVERRIDE.value:
        return year >= current_year + 1
    return year >= current_year


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
            archived=not event_is_active(row.year, row.kind, current_year, last_year),
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
        return PlanEntry(plan=detail.plan, detail=detail)
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
    session: Session,
    ctx: Context,
    plan_id: UUID,
    validated: ValidatedPlan,
    *,
    existing_rows: tuple[FireEvent, ...] | None = None,
) -> None:
    """Apply explicit removals and posted rows; never delete a row by omission.

    Saved rows that are not posted are the archived history the editor did not touch. They stay
    exactly as they are, which also preserves their ids, timestamps and archive import lineage.
    """
    events = FireEventRepository(session, ctx)
    rows = existing_rows if existing_rows is not None else tuple(events.for_plan(plan_id))
    existing = {row.id: row for row in rows}
    for event_id in validated.removed_event_ids:
        current = existing.pop(event_id, None)
        if current is None:
            raise FireError("events", "invalid_event")
        events.delete(current)
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
        current_year = _current_year(ctx)
        saved_rows = tuple(FireEventRepository(session, ctx).for_plan(row.id))
        saved_events = {
            event.id: SavedEvent(
                year=event.year,
                kind=event.kind,
                amount_minor=event.amount_minor,
                label=event.label,
                archived=not event_is_active(
                    event.year, event.kind, current_year, row.birth_year + row.horizon_age
                ),
            )
            for event in saved_rows
        }
        validated = validate_plan_input(
            plan,
            current_year=current_year,
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
        _reconcile_events(session, ctx, row.id, validated, existing_rows=saved_rows)
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
            if not event_is_active(
                event.year,
                event.kind,
                current_year,
                chosen.birth_year + chosen.horizon_age,
            )
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
    "SavedEvent",
    "ValidatedPlan",
    "ValidatedRates",
    "create_fire_plan",
    "delete_fire_plan",
    "event_is_active",
    "get_fire_plan",
    "get_fire_plan_if_any",
    "migrate_legacy_plan",
    "plan_defaults",
    "plan_entry",
    "update_fire_plan",
    "validate_ages",
    "validate_amount",
    "validate_plan_input",
    "validate_rates",
]
