"""FIRE inputs linked to the app's own data (F8.4).

The service resolves the three linked starting values in one request/session without a per-row
N+1: the investable portfolio (M6 look-through plus investable manual M5 items), trailing-12-
full-month regular income and expenses from the ledger, per-scenario expense-category exclusions
and one-click loan-payoff suggestions from the M7 schedules.

Reads never fall back to zero silently: missing data, a currency mismatch or an all-excluded
expense list becomes a :class:`ResultsBlock`. Warnings cover fewer than twelve months of data,
data older than the window, stale prices/FX and a source-backed portfolio that sums to zero.

All amounts are integer minor units and rates are basis points. Trailing totals are scaled to a
year once, half away from zero, from the **distinct months that have included rows**; gaps are
not treated as zero months.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from assetboard.db.engine import run_immediate
from assetboard.db.models.ledger import Category
from assetboard.db.repositories import (
    AuditEventRepository,
    CategoryRepository,
    CurrentWorkspaceRepository,
    FireEventRepository,
    FireScenarioRepository,
    ItemValuationRepository,
    NetWorthCategoryRepository,
    NetWorthItemRepository,
    RegularWindowTotal,
    TransactionRepository,
)
from assetboard.domain.fire import (
    MAX_AMOUNT_MINOR,
    TrailingWindow,
    annualize_minor,
    trailing_window,
)
from assetboard.domain.loans import LoanValidationError, ScheduleError, summarize_schedule
from assetboard.domain.money import Money, minor_unit_for
from assetboard.domain.networth import (
    ValuationPoint,
    current_valuation,
)
from assetboard.i18n import gettext
from assetboard.services.context import Context, Role, require_role, require_role_now
from assetboard.services.loan_schedule import (
    build_inputs_schedule,
    schedule_inputs_from_view,
)
from assetboard.services.loans import load_loan_views_in_session
from assetboard.services.portfolio import current_portfolio_data_in_session
from assetboard.services.prices import latest_fx_rates_in_session

SourceKind = Literal["portfolio_class", "portfolio_unallocated", "networth_item"]
SuggestionState = Literal[
    "available",
    "already_paid_off",
    "archived",
    "schedule_error",
    "out_of_window",
    "currency_mismatch",
    "amount_invalid",
    "already_added",
]


class LinkedError(Exception):
    """A linked-input write/read error with a form-friendly code."""

    def __init__(self, field: str, code: str) -> None:
        super().__init__(code)
        self.field = field
        self.code = code


class ExpenseExclusionError(ValueError):
    """An exclusion id is not a regular expense category of this workspace."""

    field = "expense_exclusions"
    code = "invalid_expense_exclusion"


# (year, kind, label) of an existing event, for the duplicate-payoff check.
EventKey = tuple[int, str, str]


@dataclass(frozen=True, slots=True)
class ResultsBlock:
    """Why a linked input cannot be projected, and which form field it belongs to."""

    code: str
    field: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class LinkedWarning:
    """A non-blocking linked-input warning; ``code`` selects the translated message."""

    code: str
    series: str = ""
    detail: str = ""
    months: int = 0
    window_start: date | None = None
    window_end: date | None = None
    month: date | None = None
    start_year: int | None = None
    current_year: int | None = None
    count: int = 0


@dataclass(frozen=True, slots=True)
class CategoryTotal:
    category_id: UUID
    name: str
    color: str
    archived: bool
    amount: Money


@dataclass(frozen=True, slots=True)
class TrailingSeries:
    """One direction's trailing-window totals, with a per-category breakdown."""

    direction: str
    window: TrailingWindow
    currency: str
    months_available: int
    raw_total: Money
    annual_total: Money
    categories: tuple[CategoryTotal, ...]
    excluded_categories: tuple[CategoryTotal, ...]
    excluded_total: Money
    latest_month: date | None

    @property
    def scaled(self) -> bool:
        return 0 < self.months_available < 12

    @property
    def has_exclusions(self) -> bool:
        return bool(self.excluded_categories)


@dataclass(frozen=True, slots=True)
class PortfolioSourceRow:
    name: str
    amount: Money
    source_kind: SourceKind
    source_id: UUID | None
    color: str | None
    as_of: date


@dataclass(frozen=True, slots=True)
class LinkedPortfolio:
    as_of: date
    total: Money
    rows: tuple[PortfolioSourceRow, ...]
    non_investable_classes: int
    skipped_linked_items: int


@dataclass(frozen=True, slots=True)
class LinkedInputs:
    as_of: date
    window: TrailingWindow
    portfolio: LinkedPortfolio | None
    income: TrailingSeries | None
    expenses: TrailingSeries | None
    warnings: tuple[LinkedWarning, ...]
    blocks: tuple[ResultsBlock, ...]


@dataclass(frozen=True, slots=True)
class ExpenseExclusionOption:
    category_id: UUID
    name: str
    color: str
    archived: bool
    selected: bool


@dataclass(frozen=True, slots=True)
class LoanPayoffSuggestion:
    """One loan's one-click payoff suggestion in the scenario's start money.

    ``annual_amount`` is the sum of the next up to twelve contractual rows' interest, principal
    and costs (overpayments excluded); ``None`` when the state has no usable amount.
    """

    loan_id: UUID
    loan_name: str
    currency: str
    state: SuggestionState
    payoff_year: int | None
    annual_amount: Money | None
    months_covered: int
    as_of: date
    schedule_error: str | None = None

    @property
    def can_add(self) -> bool:
        return self.state == "available"

    @property
    def label(self) -> str:
        return f"Loan repaid: {self.loan_name}"


def _require_viewer(ctx: Context) -> None:
    require_role(ctx, Role.VIEWER)


def _category_map(session: Session, ctx: Context) -> dict[UUID, Category]:
    return {row.id: row for row in CategoryRepository(session, ctx).ordered()}


def _category_totals(
    totals: dict[UUID, int],
    categories: dict[UUID, Category],
    currency: str,
) -> tuple[CategoryTotal, ...]:
    rows = [
        CategoryTotal(
            category_id=category_id,
            name=(categories[category_id].name if category_id in categories else str(category_id)),
            color=(categories[category_id].color if category_id in categories else "#94a3b8"),
            archived=(
                category_id in categories and categories[category_id].archived_at is not None
            ),
            amount=Money(amount, currency),
        )
        for category_id, amount in totals.items()
    ]
    rows.sort(key=lambda row: (-row.amount.amount_minor, row.name.casefold(), str(row.category_id)))
    return tuple(rows)


def _series(
    session: Session,
    ctx: Context,
    *,
    direction: str,
    currency: str,
    window: TrailingWindow,
    categories: dict[UUID, Category],
    excluded_category_ids: Collection[UUID],
) -> tuple[TrailingSeries | None, list[ResultsBlock], list[LinkedWarning]]:
    field = "income_manual" if direction == "income" else "expenses_manual"
    no_data_code = "no_income_data" if direction == "income" else "no_expense_data"
    blocks: list[ResultsBlock] = []
    warnings: list[LinkedWarning] = []
    rows = TransactionRepository(session, ctx).regular_window_totals(
        direction=direction, start=window.first_day, end=window.last_day
    )
    currencies = {row.currency for row in rows}
    if currencies - {currency}:
        others = ", ".join(sorted(currencies - {currency}))
        blocks.append(ResultsBlock("ledger_currency_mismatch", field, others))
    currency_rows = [row for row in rows if row.currency == currency]
    if not rows:
        blocks.append(ResultsBlock(no_data_code, field))
        return None, blocks, warnings

    excluded: set[UUID] = set(excluded_category_ids) if direction == "expense" else set()
    included_rows = [row for row in currency_rows if row.category_id not in excluded]
    excluded_rows = [row for row in currency_rows if row.category_id in excluded]
    if direction == "expense" and currency_rows and not included_rows:
        blocks.append(ResultsBlock("expenses_all_excluded", field))
    if not included_rows:
        series = TrailingSeries(
            direction=direction,
            window=window,
            currency=currency,
            months_available=0,
            raw_total=Money(0, currency),
            annual_total=Money(0, currency),
            categories=(),
            excluded_categories=_category_totals(_totals(excluded_rows), categories, currency),
            excluded_total=Money(sum(row.amount_minor for row in excluded_rows), currency),
            latest_month=None,
        )
        return series, blocks, warnings

    months = {row.month for row in included_rows}
    raw_total = sum(row.amount_minor for row in included_rows)
    annual_total = annualize_minor(raw_total, len(months))
    included_totals = _totals(included_rows)
    excluded_totals = _totals(excluded_rows)
    latest = max(months)
    series = TrailingSeries(
        direction=direction,
        window=window,
        currency=currency,
        months_available=len(months),
        raw_total=Money(raw_total, currency),
        annual_total=Money(annual_total, currency),
        categories=_category_totals(included_totals, categories, currency),
        excluded_categories=_category_totals(excluded_totals, categories, currency),
        excluded_total=Money(sum(row.amount_minor for row in excluded_rows), currency),
        latest_month=latest.first_day,
    )
    if len(months) < 12:
        warnings.append(
            LinkedWarning(
                code="scaled_months",
                series=direction,
                months=len(months),
                window_start=window.first_day,
                window_end=window.last_day,
            )
        )
    if latest < window.end:
        warnings.append(
            LinkedWarning(
                code="linked_data_older",
                series=direction,
                month=latest.first_day,
                window_end=window.last_day,
            )
        )
    return series, blocks, warnings


def _totals(rows: Sequence[RegularWindowTotal]) -> dict[UUID, int]:
    totals: dict[UUID, int] = {}
    for row in rows:
        totals[row.category_id] = totals.get(row.category_id, 0) + row.amount_minor
    return totals


def _portfolio(
    session: Session,
    ctx: Context,
    *,
    currency: str,
    today: date,
) -> tuple[LinkedPortfolio | None, list[ResultsBlock], list[LinkedWarning]]:
    blocks: list[ResultsBlock] = []
    warnings: list[LinkedWarning] = []
    data = current_portfolio_data_in_session(session, ctx)
    if data.currency != currency:
        # Defensive: the caller blocks a scenario currency mismatch before this point.
        blocks.append(ResultsBlock("scenario_currency_mismatch", "portfolio_manual", currency))
        return None, blocks, warnings

    classes = {row.id: row for row in data.classes}
    instruments = {row.id: row for row in data.instruments}
    class_totals = {
        total.asset_class_id: total.value.amount_minor for total in data.valuation.classes
    }
    allocations: dict[UUID, list[UUID]] = {}
    for allocation in data.allocations:
        allocations.setdefault(allocation.instrument_id, []).append(allocation.asset_class_id)

    related_holdings = {
        row.instrument_id
        for row in data.valuation.rows
        if not allocations.get(row.instrument_id)
        or any(
            classes.get(class_id) is not None and classes[class_id].investable
            for class_id in allocations[row.instrument_id]
        )
    }
    for row in data.valuation.rows:
        if row.instrument_id not in related_holdings or row.missing is None:
            continue
        instrument = instruments.get(row.instrument_id)
        code = "portfolio_price_missing" if row.missing.value == "price" else "portfolio_fx_missing"
        blocks.append(
            ResultsBlock(code, "portfolio_manual", instrument.name if instrument else None)
        )

    rows: list[PortfolioSourceRow] = []
    non_investable = 0
    for class_id, total in class_totals.items():
        if class_id is None:
            rows.append(
                PortfolioSourceRow(
                    name="",
                    amount=Money(total, currency),
                    source_kind="portfolio_unallocated",
                    source_id=None,
                    color=None,
                    as_of=today,
                )
            )
            continue
        asset_class = classes.get(class_id)
        if asset_class is None:
            continue
        if not asset_class.investable:
            non_investable += 1
            continue
        rows.append(
            PortfolioSourceRow(
                name=asset_class.name,
                amount=Money(total, currency),
                source_kind="portfolio_class",
                source_id=asset_class.id,
                color=asset_class.color,
                as_of=today,
            )
        )

    for instrument_id in sorted(related_holdings):
        if instrument_id not in {row.instrument_id for row in data.valuation.rows}:
            continue
        price = data.prices.get(instrument_id)
        if price is not None and price.stale:
            instrument = instruments.get(instrument_id)
            warnings.append(
                LinkedWarning(
                    code="stale_price",
                    series="portfolio",
                    detail=instrument.name if instrument else "",
                )
            )
        instrument = instruments.get(instrument_id)
        if instrument is not None and instrument.quote_currency != currency:
            fx = data.fx.get(instrument.quote_currency)
            if fx is not None and fx.stale:
                warnings.append(
                    LinkedWarning(
                        code="stale_fx",
                        series="portfolio",
                        detail=instrument.quote_currency,
                    )
                )

    item_repository = NetWorthItemRepository(session, ctx)
    item_rows = [
        row
        for row in item_repository.ordered()
        if row.archived_at is None
        and row.side == "asset"
        and row.investable
        and row.include_in_net_worth
    ]
    skipped_linked = sum(1 for row in item_rows if row.valuation_mode == "linked")
    manual_items = [row for row in item_rows if row.valuation_mode == "manual"]
    valuations: dict[UUID, list[ValuationPoint]] = {}
    if manual_items:
        for valuation in ItemValuationRepository(session, ctx).for_items(
            [row.id for row in manual_items]
        ):
            valuations.setdefault(valuation.item_id, []).append(
                ValuationPoint(
                    id=valuation.id,
                    as_of=valuation.as_of,
                    created_at=valuation.created_at,
                    value_minor=valuation.value_minor,
                )
            )
    targets = {row.id: current_valuation(valuations.get(row.id, ()), today) for row in manual_items}
    currencies = {row.currency for row in manual_items} - {currency}
    fx = latest_fx_rates_in_session(session, ctx, currencies, today) if currencies else {}
    item_categories = (
        {row.id: row for row in NetWorthCategoryRepository(session, ctx).ordered()}
        if manual_items
        else {}
    )
    for item in manual_items:
        valuation = targets[item.id]
        if valuation is None:
            blocks.append(ResultsBlock("item_value_missing", "portfolio_manual", item.name))
            continue
        if item.currency == currency:
            rate = Decimal(1)
        else:
            fx_at = fx.get(item.currency)
            if fx_at is None or fx_at.rate is None:
                blocks.append(ResultsBlock("item_fx_missing", "portfolio_manual", item.name))
                continue
            rate = fx_at.rate
            if fx_at.stale:
                warnings.append(
                    LinkedWarning(
                        code="stale_fx",
                        series="portfolio",
                        detail=f"{item.name} ({item.currency})",
                    )
                )
        amount = Money.from_decimal(
            Decimal(valuation.value_minor).scaleb(-minor_unit_for(item.currency)) * rate,
            currency,
        )
        category = item_categories.get(item.category_id)
        rows.append(
            PortfolioSourceRow(
                name=item.name,
                amount=amount,
                source_kind="networth_item",
                source_id=item.id,
                color=category.color if category is not None else None,
                as_of=valuation.as_of,
            )
        )

    total = sum((row.amount.amount_minor for row in rows), 0)
    investable_class_ids = {class_id for class_id, row in classes.items() if row.investable}
    source_seen = bool(
        manual_items
        or related_holdings
        or None in class_totals
        or investable_class_ids.intersection(class_totals)
    )
    if not rows:
        if not source_seen:
            blocks.append(ResultsBlock("no_linked_portfolio_data", "portfolio_manual"))
    elif total == 0:
        warnings.append(LinkedWarning(code="linked_portfolio_zero", series="portfolio"))
    if non_investable:
        warnings.append(
            LinkedWarning(
                code="non_investable_classes",
                series="portfolio",
                count=non_investable,
            )
        )
    if skipped_linked:
        warnings.append(
            LinkedWarning(
                code="linked_items_skipped",
                series="portfolio",
                count=skipped_linked,
            )
        )

    portfolio = LinkedPortfolio(
        as_of=today,
        total=Money(total, currency),
        rows=tuple(rows),
        non_investable_classes=non_investable,
        skipped_linked_items=skipped_linked,
    )
    return portfolio, blocks, warnings


def linked_inputs_in_session(
    session: Session,
    ctx: Context,
    *,
    currency: str,
    start_year: int,
    income_mode: str,
    expenses_mode: str,
    portfolio_mode: str,
    expense_exclusion_ids: Collection[UUID] = (),
) -> LinkedInputs:
    """Resolve the linked modes the scenario uses; manual modes are left untouched."""
    _require_viewer(ctx)
    today = ctx.clock.today(ctx.workspace.timezone)
    window = trailing_window(today)
    base_currency = CurrentWorkspaceRepository(session, ctx).get().base_currency
    linked_fields = {
        "income_manual": income_mode == "trailing_12m",
        "expenses_manual": expenses_mode == "trailing_12m",
        "portfolio_manual": portfolio_mode == "linked_investable",
    }
    linked = [field for field, active in linked_fields.items() if active]
    if not linked:
        return LinkedInputs(
            as_of=today,
            window=window,
            portfolio=None,
            income=None,
            expenses=None,
            warnings=(),
            blocks=(),
        )
    if currency != base_currency:
        block = ResultsBlock("scenario_currency_mismatch", linked[0], currency)
        return LinkedInputs(
            as_of=today,
            window=window,
            portfolio=None,
            income=None,
            expenses=None,
            warnings=(),
            blocks=(block,),
        )

    categories = (
        _category_map(session, ctx)
        if income_mode == "trailing_12m" or expenses_mode == "trailing_12m"
        else {}
    )
    blocks: list[ResultsBlock] = []
    warnings: list[LinkedWarning] = []
    income: TrailingSeries | None = None
    expenses: TrailingSeries | None = None
    portfolio: LinkedPortfolio | None = None

    if income_mode == "trailing_12m":
        income, found, found_warnings = _series(
            session,
            ctx,
            direction="income",
            currency=currency,
            window=window,
            categories=categories,
            excluded_category_ids=(),
        )
        blocks.extend(found)
        warnings.extend(found_warnings)
    if expenses_mode == "trailing_12m":
        expenses, found, found_warnings = _series(
            session,
            ctx,
            direction="expense",
            currency=currency,
            window=window,
            categories=categories,
            excluded_category_ids=expense_exclusion_ids,
        )
        blocks.extend(found)
        warnings.extend(found_warnings)
    if portfolio_mode == "linked_investable":
        portfolio, found, found_warnings = _portfolio(session, ctx, currency=currency, today=today)
        blocks.extend(found)
        warnings.extend(found_warnings)

    if start_year != today.year:
        warnings.append(
            LinkedWarning(
                code="start_year_not_current",
                start_year=start_year,
                current_year=today.year,
            )
        )
    return LinkedInputs(
        as_of=today,
        window=window,
        portfolio=portfolio,
        income=income,
        expenses=expenses,
        warnings=tuple(warnings),
        blocks=tuple(blocks),
    )


def linked_inputs(
    factory: sessionmaker[Session],
    ctx: Context,
    *,
    currency: str,
    start_year: int,
    income_mode: str,
    expenses_mode: str,
    portfolio_mode: str,
    expense_exclusion_ids: Collection[UUID] = (),
) -> LinkedInputs:
    """The public read wrapper for :func:`linked_inputs_in_session`."""
    _require_viewer(ctx)
    with factory() as session:
        return linked_inputs_in_session(
            session,
            ctx,
            currency=currency,
            start_year=start_year,
            income_mode=income_mode,
            expenses_mode=expenses_mode,
            portfolio_mode=portfolio_mode,
            expense_exclusion_ids=expense_exclusion_ids,
        )


def expense_exclusion_options(
    factory: sessionmaker[Session],
    ctx: Context,
    *,
    selected_ids: Collection[UUID] = (),
) -> tuple[ExpenseExclusionOption, ...]:
    """Regular expense categories offered as exclusions; archived ones only when selected."""
    _require_viewer(ctx)
    selected = set(selected_ids)
    with factory() as session:
        rows = CategoryRepository(session, ctx).ordered(kind="expense")
        options = [
            ExpenseExclusionOption(
                category_id=row.id,
                name=row.name,
                color=row.color,
                archived=row.archived_at is not None,
                selected=row.id in selected,
            )
            for row in rows
            if row.treatment == "regular" and (row.archived_at is None or row.id in selected)
        ]
    return tuple(options)


def validate_expense_exclusions_in_session(
    session: Session, ctx: Context, category_ids: Collection[UUID]
) -> tuple[UUID, ...]:
    """Deduplicate and validate exclusion ids (regular expense categories, archived allowed)."""
    wanted = list(dict.fromkeys(category_ids))
    if not wanted:
        return ()
    categories = _category_map(session, ctx)
    for category_id in wanted:
        category = categories.get(category_id)
        if category is None or category.kind != "expense" or category.treatment != "regular":
            raise ExpenseExclusionError
    return tuple(wanted)


def validate_expense_exclusions(
    factory: sessionmaker[Session], ctx: Context, category_ids: Collection[UUID]
) -> tuple[UUID, ...]:
    _require_viewer(ctx)
    with factory() as session:
        return validate_expense_exclusions_in_session(session, ctx, category_ids)


def loan_payoff_suggestions_in_session(
    session: Session,
    ctx: Context,
    *,
    currency: str,
    start_year: int,
    last_year: int,
    existing_events: Sequence[EventKey] = (),
) -> tuple[LoanPayoffSuggestion, ...]:
    """One suggestion per non-archived schedule loan, in a bounded number of queries."""
    _require_viewer(ctx)
    today = ctx.clock.today(ctx.workspace.timezone)
    views = load_loan_views_in_session(session, ctx, today=today, include_archived=False)
    suggestions: list[LoanPayoffSuggestion] = []
    for view in views:
        if view.currency != currency:
            suggestions.append(
                LoanPayoffSuggestion(
                    loan_id=view.id,
                    loan_name=view.name,
                    currency=view.currency,
                    state="currency_mismatch",
                    payoff_year=None,
                    annual_amount=None,
                    months_covered=0,
                    as_of=today,
                )
            )
            continue
        try:
            inputs = schedule_inputs_from_view(view, today)
            schedule = build_inputs_schedule(inputs)
            summary = summarize_schedule(schedule, today)
        except (ScheduleError, LoanValidationError) as exc:
            suggestions.append(
                LoanPayoffSuggestion(
                    loan_id=view.id,
                    loan_name=view.name,
                    currency=view.currency,
                    state="schedule_error",
                    payoff_year=None,
                    annual_amount=None,
                    months_covered=0,
                    as_of=today,
                    schedule_error=exc.code,
                )
            )
            continue
        payoff_year = summary.payoff_date.year if summary.payoff_date is not None else None
        future = [row for row in schedule.rows if row.date > today]
        covered = future[:12]
        annual = sum(
            row.interest.amount_minor + row.principal.amount_minor + row.costs.amount_minor
            for row in covered
        )
        if summary.remaining_principal.amount_minor == 0:
            state: SuggestionState = "already_paid_off"
        elif payoff_year is None or annual <= 0 or annual > MAX_AMOUNT_MINOR:
            state = "amount_invalid"
        elif payoff_year < start_year or payoff_year > last_year:
            state = "out_of_window"
        else:
            state = "available"
        if state == "available":
            label = gettext("Loan repaid: %(name)s", ctx.workspace.locale) % {"name": view.name}
            if any(
                event_year == payoff_year
                and event_kind == "expense_change"
                and event_label == label
                for event_year, event_kind, event_label in existing_events
            ):
                state = "already_added"
        suggestion = LoanPayoffSuggestion(
            loan_id=view.id,
            loan_name=view.name,
            currency=view.currency,
            state=state,
            payoff_year=payoff_year,
            annual_amount=Money(annual, currency) if annual > 0 else None,
            months_covered=len(covered),
            as_of=today,
            schedule_error=None,
        )
        suggestions.append(suggestion)
    return tuple(suggestions)


def loan_payoff_suggestions(
    factory: sessionmaker[Session],
    ctx: Context,
    *,
    currency: str,
    start_year: int,
    last_year: int,
    existing_events: Sequence[EventKey] = (),
) -> tuple[LoanPayoffSuggestion, ...]:
    _require_viewer(ctx)
    with factory() as session:
        return loan_payoff_suggestions_in_session(
            session,
            ctx,
            currency=currency,
            start_year=start_year,
            last_year=last_year,
            existing_events=existing_events,
        )


def add_loan_payoff_event(
    factory: sessionmaker[Session], ctx: Context, scenario_id: UUID, loan_id: UUID
) -> Literal["added", "exists"]:
    """Create the negative annual-expense-change event for a suggested loan, once."""
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> Literal["added", "exists"]:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        scenario = FireScenarioRepository(session, ctx).get_or_raise(scenario_id)
        if scenario.archived_at is not None:
            raise LinkedError("scenario", "archived")
        existing = FireEventRepository(session, ctx).for_scenario(scenario.id)
        suggestions = loan_payoff_suggestions_in_session(
            session,
            ctx,
            currency=scenario.currency,
            start_year=scenario.start_year,
            last_year=scenario.birth_year + scenario.horizon_age,
            existing_events=tuple((event.year, event.kind, event.label) for event in existing),
        )
        suggestion = next((item for item in suggestions if item.loan_id == loan_id), None)
        if suggestion is None:
            raise LinkedError("loan", "loan_payoff_unavailable")
        if suggestion.state == "already_added":
            return "exists"
        if suggestion.state != "available" or suggestion.annual_amount is None:
            raise LinkedError("loan", "loan_payoff_unavailable")
        if suggestion.payoff_year is None:
            raise LinkedError("loan", "loan_payoff_unavailable")
        FireEventRepository(session, ctx).create(
            scenario_id=scenario.id,
            year=suggestion.payoff_year,
            kind="expense_change",
            amount_minor=-suggestion.annual_amount.amount_minor,
            label=suggestion.label,
        )
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action="fire_scenario.loan_payoff_event",
            entity_type="fire_scenario",
            entity_id=str(scenario.id),
            ts=ctx.clock.now(),
            data={"loan": suggestion.loan_name, "year": suggestion.payoff_year},
        )
        return "added"

    return run_immediate(factory, write)


__all__ = [
    "CategoryTotal",
    "ExpenseExclusionError",
    "ExpenseExclusionOption",
    "LinkedError",
    "LinkedInputs",
    "LinkedPortfolio",
    "LinkedWarning",
    "LoanPayoffSuggestion",
    "PortfolioSourceRow",
    "ResultsBlock",
    "TrailingSeries",
    "add_loan_payoff_event",
    "expense_exclusion_options",
    "linked_inputs",
    "linked_inputs_in_session",
    "loan_payoff_suggestions",
    "loan_payoff_suggestions_in_session",
    "validate_expense_exclusions",
    "validate_expense_exclusions_in_session",
]
