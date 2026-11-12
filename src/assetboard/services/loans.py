"""Loan configuration use cases (F7.1).

Reads are open to viewers; writes need ``editor`` or ``owner`` and re-check the role inside the
write transaction. Every write locks the workspace row first, reads the base currency from the
workspace row (never the possibly stale Context) and records an audit event. The schedule engine
is F7.2: only ``first_installment`` is used here to validate a target-total overpayment.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Literal
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from assetboard.db.engine import run_immediate
from assetboard.db.models.loans import (
    Loan,
    LoanBalanceCheckpoint,
    LoanCostComponent,
    LoanOverpayment,
    LoanRateChange,
    ReferenceRate,
    ReferenceRateValue,
)
from assetboard.db.repositories import (
    AuditEventRepository,
    CurrentWorkspaceRepository,
    LoanBalanceCheckpointRepository,
    LoanCostComponentRepository,
    LoanOverpaymentRepository,
    LoanRateChangeRepository,
    LoanRepository,
    NotFound,
    ReferenceRateRepository,
    ReferenceRateValueRepository,
)
from assetboard.domain.loans import (
    MAX_LOAN_RATE_BP,
    CostBasis,
    DayCount,
    LoanKind,
    OverpaymentMode,
    OverpaymentStrategy,
    RateMode,
    clean_loan_name,
    clean_note,
    first_installment,
    latest_value_on_or_before,
    payment_date_matches_day,
    requires_high_rate_confirmation,
    valid_date,
    validate_cost_dates,
    validate_cost_rate_bp,
    validate_day_count,
    validate_margin_bp,
    validate_payment_day,
    validate_rate_bp,
    validate_term_months,
)
from assetboard.domain.money import Money
from assetboard.domain.percent_or_fixed import PercentOrFixed
from assetboard.domain.text import text_key
from assetboard.services.context import Context, Role, require_role, require_role_now

EntryPath = Literal["original", "current"]


class LoanError(Exception):
    def __init__(self, field: str, code: str) -> None:
        super().__init__(code)
        self.field = field
        self.code = code


@dataclass(frozen=True, slots=True)
class LoanRateChangeView:
    id: UUID
    effective_from: date
    rate_bp: int


@dataclass(frozen=True, slots=True)
class LoanCostComponentView:
    id: UUID
    name: str
    value: PercentOrFixed
    effective_from: date
    effective_to: date | None
    basis: str


@dataclass(frozen=True, slots=True)
class LoanOverpaymentView:
    id: UUID
    date: date
    amount: Money
    note: str


@dataclass(frozen=True, slots=True)
class LoanCheckpointView:
    id: UUID
    as_of: date
    remaining_principal: Money
    note: str


@dataclass(frozen=True, slots=True)
class LoanView:
    id: UUID
    name: str
    kind: LoanKind
    currency: str
    entry_path: EntryPath
    principal_original: Money | None
    current_balance: Money | None
    balance_date: date | None
    origination_date: date | None
    first_payment_date: date
    term_months: int
    payment_day: int
    rate_mode: RateMode
    day_count: DayCount
    margin_bp: int | None
    reference_rate_id: UUID | None
    reference_rate_name: str | None
    reference_rate_values: tuple[ReferenceRateValueView, ...]
    current_rate_bp: int | None
    installment_override: Money | None
    overpayment_mode: OverpaymentMode
    overpayment_amount: Money | None
    overpayment_strategy: OverpaymentStrategy
    archived: bool
    rate_changes: tuple[LoanRateChangeView, ...]
    cost_components: tuple[LoanCostComponentView, ...]
    overpayments: tuple[LoanOverpaymentView, ...]
    checkpoints: tuple[LoanCheckpointView, ...]
    first_installment: Money | None


@dataclass(frozen=True, slots=True)
class LoanPage:
    active: tuple[LoanView, ...]
    archived: tuple[LoanView, ...]


@dataclass(frozen=True, slots=True)
class ReferenceRateValueView:
    id: UUID
    effective_from: date
    value_bp: int
    editable: bool


@dataclass(frozen=True, slots=True)
class ReferenceRateView:
    id: UUID
    name: str
    notes: str
    values: tuple[ReferenceRateValueView, ...]
    current_value_bp: int | None


@dataclass(frozen=True, slots=True)
class ReferenceRatesPage:
    rates: tuple[ReferenceRateView, ...]


def _write[T](factory: sessionmaker[Session], operation: Callable[[Session], T]) -> T:
    return run_immediate(factory, operation)


def _money(minor: int | None, currency: str) -> Money | None:
    return None if minor is None else Money(minor, currency)


def _require_money(value: Money | None, currency: str, field: str) -> int | None:
    if value is None:
        return None
    if value.currency != currency:
        raise LoanError(field, "invalid_currency")
    return value.amount_minor


def _audit(
    session: Session,
    ctx: Context,
    action: str,
    entity_type: str,
    entity_id: UUID,
    data: dict[str, str | int | bool],
) -> None:
    AuditEventRepository(session, ctx).record(
        actor_user_id=ctx.user.id,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        ts=ctx.clock.now(),
        data=data,
    )


def _locked_workspace_currency(session: Session, ctx: Context) -> str:
    repository = CurrentWorkspaceRepository(session, ctx)
    repository.lock(exclusive=True)
    return repository.get().base_currency


def _loan_start(loan: Loan, checkpoints: list[LoanBalanceCheckpoint]) -> date:
    if loan.origination_date is not None:
        return loan.origination_date
    if checkpoints:
        return min(checkpoint.as_of for checkpoint in checkpoints)
    raise LoanError("origination_date", "invalid_date")


def _entry_path(loan: Loan) -> EntryPath:
    return "original" if loan.origination_date is not None else "current"


def _fixed_rate_at(changes: list[LoanRateChange], day: date) -> int | None:
    chosen: int | None = None
    for change in changes:
        if change.effective_from <= day:
            chosen = change.rate_bp
    return chosen


def _reference_rate_at(values: list[ReferenceRateValue], day: date) -> int | None:
    return latest_value_on_or_before(
        [(value.effective_from, value.value_bp) for value in values], day
    )


def _loan_rate_at(
    loan: Loan,
    changes: list[LoanRateChange],
    reference_values: list[ReferenceRateValue],
    day: date,
) -> int | None:
    if loan.rate_mode == RateMode.FIXED.value:
        return _fixed_rate_at(changes, day)
    reference = _reference_rate_at(reference_values, day)
    if reference is None or loan.margin_bp is None:
        return None
    return reference + loan.margin_bp


def _validate_rate_timeline(
    loan: Loan,
    changes: list[LoanRateChange],
    start: date,
    *,
    first_payment_date: date | None = None,
) -> None:
    if loan.rate_mode != RateMode.FIXED.value:
        return
    if not changes:
        raise LoanError("rate_bp", "rate_change_missing")
    if any(change.effective_from < start for change in changes):
        raise LoanError("effective_from", "rate_change_before_start")
    first = first_payment_date or loan.first_payment_date
    if not any(change.effective_from <= first for change in changes):
        raise LoanError("rate_bp", "rate_change_missing")


def _base_principal(loan: Loan, checkpoints: list[LoanBalanceCheckpoint]) -> Money | None:
    if loan.origination_date is not None:
        return _money(loan.principal_original_minor, loan.currency)
    if checkpoints:
        latest = max(checkpoints, key=lambda row: (row.as_of, row.id))
        return Money(latest.remaining_principal_minor, loan.currency)
    return None


def _target_installment(
    loan: Loan,
    changes: list[LoanRateChange],
    reference_values: list[ReferenceRateValue],
    checkpoints: list[LoanBalanceCheckpoint],
) -> Money | None:
    if loan.installment_override_minor is not None:
        return Money(loan.installment_override_minor, loan.currency)
    principal = _base_principal(loan, checkpoints)
    rate = _loan_rate_at(loan, changes, reference_values, loan.first_payment_date)
    if principal is None or rate is None:
        return None
    return first_installment(principal, rate, loan.term_months, LoanKind(loan.kind))


def _validate_target(
    target: Money, installment: Money | None, field: str = "overpayment_amount"
) -> None:
    if installment is None:
        raise LoanError(field, "missing_rate")
    if target.amount_minor < installment.amount_minor:
        raise LoanError(field, "target_below_installment")


def _validate_high_rate(rate_bp: int, confirmed: bool) -> None:
    if requires_high_rate_confirmation(rate_bp) and not confirmed:
        raise LoanError("rate_bp", "high_rate_confirmation_required")


def _validate_current_target(session: Session, ctx: Context, loan: Loan) -> None:
    """A rate change must not make a target-total overpayment below the first installment."""
    if (
        loan.overpayment_mode != OverpaymentMode.TARGET_TOTAL.value
        or loan.overpayment_amount_minor is None
    ):
        return
    changes = LoanRateChangeRepository(session, ctx).for_loan(loan.id)
    checkpoints = LoanBalanceCheckpointRepository(session, ctx).for_loan(loan.id)
    reference_values = (
        ReferenceRateValueRepository(session, ctx).for_rate(loan.reference_rate_id)
        if loan.reference_rate_id is not None
        else []
    )
    _validate_target(
        Money(loan.overpayment_amount_minor, loan.currency),
        _target_installment(loan, changes, reference_values, checkpoints),
    )


def _validate_reference_targets(session: Session, ctx: Context, rate_id: UUID) -> None:
    """Re-check target-total loans that use a changed reference-rate series."""
    for loan in LoanRepository(session, ctx).ordered():
        if loan.reference_rate_id == rate_id:
            _validate_current_target(session, ctx, loan)


def _view(
    loan: Loan,
    changes: list[LoanRateChange],
    costs: list[LoanCostComponent],
    overpayments: list[LoanOverpayment],
    checkpoints: list[LoanBalanceCheckpoint],
    reference_rate: ReferenceRate | None,
    reference_values: list[ReferenceRateValue],
    today: date,
) -> LoanView:
    entry = _entry_path(loan)
    latest_checkpoint = None
    if checkpoints:
        latest_checkpoint = max(checkpoints, key=lambda row: (row.as_of, row.id))
    current_rate = _loan_rate_at(loan, changes, reference_values, today)
    return LoanView(
        id=loan.id,
        name=loan.name,
        kind=LoanKind(loan.kind),
        currency=loan.currency,
        entry_path=entry,
        principal_original=_money(loan.principal_original_minor, loan.currency),
        current_balance=(
            Money(latest_checkpoint.remaining_principal_minor, loan.currency)
            if latest_checkpoint is not None
            else None
        ),
        balance_date=latest_checkpoint.as_of if latest_checkpoint is not None else None,
        origination_date=loan.origination_date,
        first_payment_date=loan.first_payment_date,
        term_months=loan.term_months,
        payment_day=loan.payment_day,
        rate_mode=RateMode(loan.rate_mode),
        day_count=DayCount(loan.day_count),
        margin_bp=loan.margin_bp,
        reference_rate_id=loan.reference_rate_id,
        reference_rate_name=reference_rate.name if reference_rate is not None else None,
        reference_rate_values=tuple(
            ReferenceRateValueView(
                id=row.id,
                effective_from=row.effective_from,
                value_bp=row.value_bp,
                editable=row.effective_from > today,
            )
            for row in reference_values
        ),
        current_rate_bp=current_rate,
        installment_override=_money(loan.installment_override_minor, loan.currency),
        overpayment_mode=OverpaymentMode(loan.overpayment_mode),
        overpayment_amount=_money(loan.overpayment_amount_minor, loan.currency),
        overpayment_strategy=OverpaymentStrategy(loan.overpayment_strategy),
        archived=loan.archived_at is not None,
        rate_changes=tuple(
            LoanRateChangeView(id=row.id, effective_from=row.effective_from, rate_bp=row.rate_bp)
            for row in changes
        ),
        cost_components=tuple(
            LoanCostComponentView(
                id=row.id,
                name=row.name,
                value=(
                    PercentOrFixed.percent(row.rate_bp)
                    if row.mode == "percent" and row.rate_bp is not None
                    else PercentOrFixed.fixed_amount(Money(row.fixed_minor or 0, loan.currency))
                ),
                effective_from=row.effective_from,
                effective_to=row.effective_to,
                basis=row.basis,
            )
            for row in costs
        ),
        overpayments=tuple(
            LoanOverpaymentView(
                id=row.id,
                date=row.date,
                amount=Money(row.amount_minor, loan.currency),
                note=row.note,
            )
            for row in overpayments
        ),
        checkpoints=tuple(
            LoanCheckpointView(
                id=row.id,
                as_of=row.as_of,
                remaining_principal=Money(row.remaining_principal_minor, loan.currency),
                note=row.note,
            )
            for row in checkpoints
        ),
        first_installment=_target_installment(loan, changes, reference_values, checkpoints),
    )


def _load_loan_view(session: Session, ctx: Context, loan: Loan, today: date) -> LoanView:
    changes = LoanRateChangeRepository(session, ctx).for_loan(loan.id)
    costs = LoanCostComponentRepository(session, ctx).for_loan(loan.id)
    overpayments = LoanOverpaymentRepository(session, ctx).for_loan(loan.id)
    checkpoints = LoanBalanceCheckpointRepository(session, ctx).for_loan(loan.id)
    reference = None
    reference_values: list[ReferenceRateValue] = []
    if loan.reference_rate_id is not None:
        reference = ReferenceRateRepository(session, ctx).get(loan.reference_rate_id)
        reference_values = ReferenceRateValueRepository(session, ctx).for_rate(
            loan.reference_rate_id
        )
    return _view(
        loan,
        changes,
        costs,
        overpayments,
        checkpoints,
        reference,
        reference_values,
        today,
    )


def list_loans(factory: sessionmaker[Session], ctx: Context) -> LoanPage:
    require_role(ctx, Role.VIEWER)
    today = ctx.clock.today(ctx.workspace.timezone)
    with factory() as session:
        rows = LoanRepository(session, ctx).ordered()
        views = [_load_loan_view(session, ctx, row, today) for row in rows]
    active = tuple(view for view in views if not view.archived)
    archived = tuple(view for view in views if view.archived)
    return LoanPage(active=active, archived=archived)


def get_loan(factory: sessionmaker[Session], ctx: Context, loan_id: UUID) -> LoanView:
    require_role(ctx, Role.VIEWER)
    today = ctx.clock.today(ctx.workspace.timezone)
    with factory() as session:
        loan = LoanRepository(session, ctx).get_or_raise(loan_id)
        return _load_loan_view(session, ctx, loan, today)


def _validated_terms(
    *,
    entry_path: EntryPath,
    name: str,
    kind: str,
    principal_original: Money | None,
    current_balance: Money | None,
    balance_date: date | None,
    origination_date: date | None,
    first_payment_date: date,
    term_months: int,
    payment_day: int,
    installment_override: Money | None,
    currency: str,
) -> tuple[
    str,
    LoanKind,
    int | None,
    int | None,
    date | None,
    date,
    int,
    int,
    int | None,
]:
    clean_name = clean_loan_name(name)
    try:
        wanted_kind = LoanKind(kind)
    except ValueError:
        raise LoanError("kind", "invalid_kind") from None
    validate_term_months(term_months)
    validate_payment_day(payment_day)
    valid_date(first_payment_date, "first_payment_date")
    if not payment_date_matches_day(first_payment_date, payment_day):
        raise LoanError("first_payment_date", "invalid_first_payment")
    principal_minor = _require_money(principal_original, currency, "principal_original")
    if principal_minor is not None and principal_minor <= 0:
        raise LoanError("principal_original", "invalid_principal")
    override_minor = _require_money(installment_override, currency, "installment_override")
    if override_minor is not None and override_minor <= 0:
        raise LoanError("installment_override", "invalid_override")
    if wanted_kind is LoanKind.EQUAL_PRINCIPAL and override_minor is not None:
        raise LoanError("installment_override", "override_annuity_only")

    if entry_path == "original":
        if origination_date is None:
            raise LoanError("origination_date", "invalid_date")
        valid_date(origination_date, "origination_date")
        if first_payment_date <= origination_date:
            raise LoanError("first_payment_date", "invalid_first_payment")
        if principal_minor is None:
            raise LoanError("principal_original", "invalid_principal")
        if current_balance is not None or balance_date is not None:
            raise LoanError("current_balance", "invalid_balance")
    else:
        if origination_date is not None:
            raise LoanError("origination_date", "invalid_date")
        if current_balance is None or balance_date is None:
            raise LoanError("current_balance", "invalid_balance")
        valid_date(balance_date, "balance_date")
        balance_minor = _require_money(current_balance, currency, "current_balance")
        if balance_minor is None or balance_minor <= 0:
            raise LoanError("current_balance", "invalid_balance")
        if first_payment_date <= balance_date:
            raise LoanError("first_payment_date", "invalid_first_payment")
        if principal_minor is not None and principal_minor < balance_minor:
            raise LoanError("principal_original", "invalid_principal")
    return (
        clean_name,
        wanted_kind,
        principal_minor,
        None,
        origination_date,
        first_payment_date,
        term_months,
        payment_day,
        override_minor,
    )


def _validated_rate_config(
    *,
    rate_mode: str,
    fixed_rate_bp: int | None,
    reference_rate_id: UUID | None,
    margin_bp: int | None,
    high_rate_confirmed: bool,
    start: date,
    session: Session,
    ctx: Context,
) -> tuple[RateMode, int | None, UUID | None, int | None]:
    try:
        wanted_mode = RateMode(rate_mode)
    except ValueError:
        raise LoanError("rate_mode", "invalid_rate_mode") from None
    if wanted_mode is RateMode.FIXED:
        if fixed_rate_bp is None:
            raise LoanError("rate_bp", "invalid_rate")
        validate_rate_bp(fixed_rate_bp)
        _validate_high_rate(fixed_rate_bp, high_rate_confirmed)
        if reference_rate_id is not None or margin_bp is not None:
            raise LoanError("rate_mode", "invalid_rate_mode")
        return wanted_mode, fixed_rate_bp, None, None
    if reference_rate_id is None or margin_bp is None:
        raise LoanError("reference_rate_id", "invalid_reference_rate")
    validate_margin_bp(margin_bp)
    reference = ReferenceRateRepository(session, ctx).get(reference_rate_id)
    if reference is None:
        raise LoanError("reference_rate_id", "invalid_reference_rate")
    values = ReferenceRateValueRepository(session, ctx).for_rate(reference_rate_id)
    reference_value = _reference_rate_at(values, start)
    if reference_value is None:
        raise LoanError("reference_rate_id", "missing_reference_value")
    total = reference_value + margin_bp
    if total > MAX_LOAN_RATE_BP:
        raise LoanError("margin_bp", "invalid_rate")
    _validate_high_rate(total, high_rate_confirmed)
    return wanted_mode, None, reference_rate_id, margin_bp


def _validated_variable_config(
    *,
    reference_rate_id: UUID | None,
    margin_bp: int | None,
    high_rate_confirmed: bool,
    start: date,
    session: Session,
    ctx: Context,
) -> tuple[UUID, int]:
    if reference_rate_id is None or margin_bp is None:
        raise LoanError("reference_rate_id", "invalid_reference_rate")
    validate_margin_bp(margin_bp)
    reference = ReferenceRateRepository(session, ctx).get(reference_rate_id)
    if reference is None:
        raise LoanError("reference_rate_id", "invalid_reference_rate")
    values = ReferenceRateValueRepository(session, ctx).for_rate(reference_rate_id)
    reference_value = _reference_rate_at(values, start)
    if reference_value is None:
        raise LoanError("reference_rate_id", "missing_reference_value")
    total = reference_value + margin_bp
    if total > MAX_LOAN_RATE_BP:
        raise LoanError("margin_bp", "invalid_rate")
    _validate_high_rate(total, high_rate_confirmed)
    return reference_rate_id, margin_bp


def _validated_overpayment(
    *,
    mode: str,
    amount: Money | None,
    strategy: str,
    currency: str,
) -> tuple[OverpaymentMode, int | None, OverpaymentStrategy]:
    try:
        wanted_mode = OverpaymentMode(mode)
        wanted_strategy = OverpaymentStrategy(strategy)
    except ValueError:
        raise LoanError("overpayment_mode", "invalid_overpayment") from None
    amount_minor = _require_money(amount, currency, "overpayment_amount")
    if wanted_mode is OverpaymentMode.NONE:
        if amount_minor is not None:
            raise LoanError("overpayment_amount", "invalid_overpayment")
    else:
        if amount_minor is None or amount_minor <= 0:
            raise LoanError("overpayment_amount", "invalid_overpayment")
    return wanted_mode, amount_minor, wanted_strategy


def create_loan(
    factory: sessionmaker[Session],
    ctx: Context,
    *,
    entry_path: EntryPath,
    name: str,
    kind: str,
    principal_original: Money | None,
    current_balance: Money | None,
    balance_date: date | None,
    origination_date: date | None,
    first_payment_date: date,
    term_months: int,
    payment_day: int,
    rate_mode: str,
    fixed_rate_bp: int | None,
    reference_rate_id: UUID | None,
    margin_bp: int | None,
    high_rate_confirmed: bool,
    installment_override: Money | None,
    overpayment_mode: str,
    overpayment_amount: Money | None,
    overpayment_strategy: str,
    day_count: str = "monthly",
) -> UUID:
    require_role(ctx, Role.EDITOR)
    if entry_path not in ("original", "current"):
        raise LoanError("entry_path", "invalid_entry_path")

    def write(session: Session) -> UUID:
        require_role_now(session, ctx, Role.EDITOR)
        currency = _locked_workspace_currency(session, ctx)
        (
            clean_name,
            wanted_kind,
            principal_minor,
            _,
            wanted_origination,
            first_date,
            term,
            day,
            override_minor,
        ) = _validated_terms(
            entry_path=entry_path,
            name=name,
            kind=kind,
            principal_original=principal_original,
            current_balance=current_balance,
            balance_date=balance_date,
            origination_date=origination_date,
            first_payment_date=first_payment_date,
            term_months=term_months,
            payment_day=payment_day,
            installment_override=installment_override,
            currency=currency,
        )
        start = wanted_origination if wanted_origination is not None else balance_date
        assert start is not None
        wanted_rate_mode, fixed_rate, rate_id, margin = _validated_rate_config(
            rate_mode=rate_mode,
            fixed_rate_bp=fixed_rate_bp,
            reference_rate_id=reference_rate_id,
            margin_bp=margin_bp,
            high_rate_confirmed=high_rate_confirmed,
            start=start,
            session=session,
            ctx=ctx,
        )
        wanted_overpayment, overpayment_minor, wanted_strategy = _validated_overpayment(
            mode=overpayment_mode,
            amount=overpayment_amount,
            strategy=overpayment_strategy,
            currency=currency,
        )
        wanted_day_count = validate_day_count(day_count)
        loan = LoanRepository(session, ctx).create(
            name=clean_name,
            kind=wanted_kind.value,
            currency=currency,
            principal_original_minor=principal_minor,
            origination_date=wanted_origination,
            first_payment_date=first_date,
            term_months=term,
            payment_day=day,
            rate_mode=wanted_rate_mode.value,
            day_count=wanted_day_count.value,
            margin_bp=margin,
            reference_rate_id=rate_id,
            installment_override_minor=override_minor,
            overpayment_mode=wanted_overpayment.value,
            overpayment_amount_minor=overpayment_minor,
            overpayment_strategy=wanted_strategy.value,
        )
        if entry_path == "current":
            assert current_balance is not None and balance_date is not None
            LoanBalanceCheckpointRepository(session, ctx).create(
                loan_id=loan.id,
                as_of=balance_date,
                remaining_principal_minor=current_balance.amount_minor,
                note="",
            )
        if wanted_rate_mode is RateMode.FIXED:
            assert fixed_rate is not None
            LoanRateChangeRepository(session, ctx).create(
                loan_id=loan.id,
                effective_from=start,
                rate_bp=fixed_rate,
            )
        if wanted_overpayment is OverpaymentMode.TARGET_TOTAL:
            checkpoints = LoanBalanceCheckpointRepository(session, ctx).for_loan(loan.id)
            changes = LoanRateChangeRepository(session, ctx).for_loan(loan.id)
            reference_values = (
                ReferenceRateValueRepository(session, ctx).for_rate(rate_id)
                if rate_id is not None
                else []
            )
            installment = _target_installment(loan, changes, reference_values, checkpoints)
            if overpayment_minor is not None:
                _validate_target(Money(overpayment_minor, currency), installment)
        _audit(
            session,
            ctx,
            "loan.created",
            "loan",
            loan.id,
            {"entry_path": entry_path, "kind": wanted_kind.value, "currency": currency},
        )
        return loan.id

    return _write(factory, write)


def _get_active_loan(session: Session, ctx: Context, loan_id: UUID) -> Loan:
    loan = LoanRepository(session, ctx).get_or_raise(loan_id)
    if loan.archived_at is not None:
        raise LoanError("loan", "archived")
    return loan


def update_loan_terms(
    factory: sessionmaker[Session],
    ctx: Context,
    loan_id: UUID,
    *,
    name: str,
    kind: str,
    principal_original: Money | None,
    current_balance: Money | None,
    balance_date: date | None,
    origination_date: date | None,
    first_payment_date: date,
    term_months: int,
    payment_day: int,
    installment_override: Money | None,
    reference_rate_id: UUID | None = None,
    margin_bp: int | None = None,
    high_rate_confirmed: bool = False,
    day_count: str | None = None,
) -> None:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        currency = _locked_workspace_currency(session, ctx)
        loan = _get_active_loan(session, ctx, loan_id)
        wanted_day_count = validate_day_count(
            day_count if day_count is not None else loan.day_count
        )
        entry = _entry_path(loan)
        (
            clean_name,
            wanted_kind,
            principal_minor,
            _,
            wanted_origination,
            first_date,
            term,
            day,
            override_minor,
        ) = _validated_terms(
            entry_path=entry,
            name=name,
            kind=kind,
            principal_original=principal_original,
            current_balance=current_balance,
            balance_date=balance_date,
            origination_date=origination_date,
            first_payment_date=first_payment_date,
            term_months=term_months,
            payment_day=payment_day,
            installment_override=installment_override,
            currency=currency,
        )
        checkpoints = LoanBalanceCheckpointRepository(session, ctx).for_loan(loan.id)
        start = wanted_origination if wanted_origination is not None else balance_date
        assert start is not None
        if loan.rate_mode == RateMode.VARIABLE.value:
            rate_id, margin = _validated_variable_config(
                reference_rate_id=reference_rate_id,
                margin_bp=margin_bp,
                high_rate_confirmed=high_rate_confirmed,
                start=start,
                session=session,
                ctx=ctx,
            )
        else:
            if reference_rate_id is not None or margin_bp is not None:
                raise LoanError("rate_mode", "invalid_rate_mode")
            rate_id, margin = None, None
        changes = LoanRateChangeRepository(session, ctx).for_loan(loan.id)
        _validate_rate_timeline(loan, changes, start, first_payment_date=first_date)
        if entry == "current":
            if current_balance is None or balance_date is None:
                raise LoanError("current_balance", "invalid_balance")
            if len(checkpoints) != 1:
                raise LoanError("current_balance", "invalid_balance")
            LoanBalanceCheckpointRepository(session, ctx).update(
                checkpoints[0].id,
                as_of=balance_date,
                remaining_principal_minor=current_balance.amount_minor,
            )
        LoanRepository(session, ctx).update(
            loan.id,
            name=clean_name,
            kind=wanted_kind.value,
            principal_original_minor=principal_minor,
            origination_date=wanted_origination,
            first_payment_date=first_date,
            term_months=term,
            payment_day=day,
            installment_override_minor=override_minor,
            reference_rate_id=rate_id,
            margin_bp=margin,
            day_count=wanted_day_count.value,
        )
        if loan.overpayment_mode == OverpaymentMode.TARGET_TOTAL.value:
            updated_checkpoints = LoanBalanceCheckpointRepository(session, ctx).for_loan(loan.id)
            reference_values = (
                ReferenceRateValueRepository(session, ctx).for_rate(loan.reference_rate_id)
                if loan.reference_rate_id is not None
                else []
            )
            installment = _target_installment(loan, changes, reference_values, updated_checkpoints)
            if loan.overpayment_amount_minor is not None:
                _validate_target(Money(loan.overpayment_amount_minor, currency), installment)
        _audit(session, ctx, "loan.updated", "loan", loan.id, {"kind": wanted_kind.value})

    _write(factory, write)


def set_overpayment_settings(
    factory: sessionmaker[Session],
    ctx: Context,
    loan_id: UUID,
    *,
    mode: str,
    amount: Money | None,
    strategy: str,
) -> None:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        currency = _locked_workspace_currency(session, ctx)
        loan = _get_active_loan(session, ctx, loan_id)
        wanted_mode, amount_minor, wanted_strategy = _validated_overpayment(
            mode=mode, amount=amount, strategy=strategy, currency=currency
        )
        if wanted_mode is OverpaymentMode.TARGET_TOTAL and amount_minor is not None:
            changes = LoanRateChangeRepository(session, ctx).for_loan(loan.id)
            checkpoints = LoanBalanceCheckpointRepository(session, ctx).for_loan(loan.id)
            reference_values = (
                ReferenceRateValueRepository(session, ctx).for_rate(loan.reference_rate_id)
                if loan.reference_rate_id is not None
                else []
            )
            installment = _target_installment(loan, changes, reference_values, checkpoints)
            _validate_target(Money(amount_minor, currency), installment)
        LoanRepository(session, ctx).update(
            loan.id,
            overpayment_mode=wanted_mode.value,
            overpayment_amount_minor=amount_minor,
            overpayment_strategy=wanted_strategy.value,
        )
        _audit(
            session,
            ctx,
            "loan.overpayment_updated",
            "loan",
            loan.id,
            {"mode": wanted_mode.value, "strategy": wanted_strategy.value},
        )

    _write(factory, write)


def archive_loan(factory: sessionmaker[Session], ctx: Context, loan_id: UUID) -> None:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        _locked_workspace_currency(session, ctx)
        loan = LoanRepository(session, ctx).get_or_raise(loan_id)
        if loan.archived_at is None:
            LoanRepository(session, ctx).update(loan.id, archived_at=ctx.clock.now())
            _audit(session, ctx, "loan.archived", "loan", loan.id, {})

    _write(factory, write)


def restore_loan(factory: sessionmaker[Session], ctx: Context, loan_id: UUID) -> None:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        _locked_workspace_currency(session, ctx)
        loan = LoanRepository(session, ctx).get_or_raise(loan_id)
        if loan.archived_at is not None:
            LoanRepository(session, ctx).update(loan.id, archived_at=None)
            _audit(session, ctx, "loan.restored", "loan", loan.id, {})

    _write(factory, write)


def add_rate_change(
    factory: sessionmaker[Session],
    ctx: Context,
    loan_id: UUID,
    *,
    effective_from: date,
    rate_bp: int,
    high_rate_confirmed: bool,
) -> None:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        _locked_workspace_currency(session, ctx)
        loan = _get_active_loan(session, ctx, loan_id)
        if loan.rate_mode != RateMode.FIXED.value:
            raise LoanError("rate_bp", "rate_mode_locked")
        valid_date(effective_from, "effective_from")
        validate_rate_bp(rate_bp)
        _validate_high_rate(rate_bp, high_rate_confirmed)
        checkpoints = LoanBalanceCheckpointRepository(session, ctx).for_loan(loan.id)
        if effective_from < _loan_start(loan, checkpoints):
            raise LoanError("effective_from", "rate_change_before_start")
        repository = LoanRateChangeRepository(session, ctx)
        if repository.exists_on(loan.id, effective_from):
            raise LoanError("effective_from", "rate_change_exists")
        repository.create(loan_id=loan.id, effective_from=effective_from, rate_bp=rate_bp)
        _validate_rate_timeline(loan, repository.for_loan(loan.id), _loan_start(loan, checkpoints))
        _validate_current_target(session, ctx, loan)
        _audit(
            session,
            ctx,
            "loan_rate_change.created",
            "loan",
            loan.id,
            {"effective_from": effective_from.isoformat(), "rate_bp": rate_bp},
        )

    _write(factory, write)


def update_rate_change(
    factory: sessionmaker[Session],
    ctx: Context,
    loan_id: UUID,
    change_id: UUID,
    *,
    effective_from: date,
    rate_bp: int,
    high_rate_confirmed: bool,
) -> None:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        _locked_workspace_currency(session, ctx)
        loan = _get_active_loan(session, ctx, loan_id)
        if loan.rate_mode != RateMode.FIXED.value:
            raise LoanError("rate_bp", "rate_mode_locked")
        valid_date(effective_from, "effective_from")
        validate_rate_bp(rate_bp)
        _validate_high_rate(rate_bp, high_rate_confirmed)
        repository = LoanRateChangeRepository(session, ctx)
        change = repository.get_for_loan(loan.id, change_id)
        if change is None:
            raise NotFound
        checkpoints = LoanBalanceCheckpointRepository(session, ctx).for_loan(loan.id)
        start = _loan_start(loan, checkpoints)
        if effective_from < start:
            raise LoanError("effective_from", "rate_change_before_start")
        if repository.exists_on(loan.id, effective_from, exclude_id=change.id):
            raise LoanError("effective_from", "rate_change_exists")
        repository.update(change.id, effective_from=effective_from, rate_bp=rate_bp)
        _validate_rate_timeline(loan, repository.for_loan(loan.id), start)
        _validate_current_target(session, ctx, loan)
        _audit(
            session,
            ctx,
            "loan_rate_change.updated",
            "loan",
            loan.id,
            {"effective_from": effective_from.isoformat(), "rate_bp": rate_bp},
        )

    _write(factory, write)


def delete_rate_change(
    factory: sessionmaker[Session], ctx: Context, loan_id: UUID, change_id: UUID
) -> None:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        _locked_workspace_currency(session, ctx)
        loan = _get_active_loan(session, ctx, loan_id)
        repository = LoanRateChangeRepository(session, ctx)
        change = repository.get_for_loan(loan.id, change_id)
        if change is None:
            raise NotFound
        checkpoints = LoanBalanceCheckpointRepository(session, ctx).for_loan(loan.id)
        remaining = [row for row in repository.for_loan(loan.id) if row.id != change.id]
        _validate_rate_timeline(loan, remaining, _loan_start(loan, checkpoints))
        repository.delete(change)
        _validate_current_target(session, ctx, loan)
        _audit(
            session,
            ctx,
            "loan_rate_change.deleted",
            "loan",
            loan.id,
            {"effective_from": change.effective_from.isoformat()},
        )

    _write(factory, write)


def _cost_value(
    *,
    mode: str,
    rate_bp: int | None,
    fixed: Money | None,
    currency: str,
) -> tuple[str, int | None, int | None]:
    if mode == "percent":
        if rate_bp is None or fixed is not None:
            raise LoanError("rate_bp", "invalid_cost")
        validate_cost_rate_bp(rate_bp)
        return "percent", rate_bp, None
    if mode == "fixed":
        fixed_minor = _require_money(fixed, currency, "fixed_amount")
        if rate_bp is not None or fixed_minor is None or fixed_minor <= 0:
            raise LoanError("fixed_amount", "invalid_cost")
        return "fixed", None, fixed_minor
    raise LoanError("mode", "invalid_cost")


def add_cost_component(
    factory: sessionmaker[Session],
    ctx: Context,
    loan_id: UUID,
    *,
    name: str,
    mode: str,
    rate_bp: int | None,
    fixed: Money | None,
    effective_from: date,
    effective_to: date | None,
) -> None:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        currency = _locked_workspace_currency(session, ctx)
        loan = _get_active_loan(session, ctx, loan_id)
        clean_name = clean_loan_name(name)
        validate_cost_dates(effective_from, effective_to)
        wanted_mode, wanted_rate, fixed_minor = _cost_value(
            mode=mode, rate_bp=rate_bp, fixed=fixed, currency=currency
        )
        component = LoanCostComponentRepository(session, ctx).create(
            loan_id=loan.id,
            name=clean_name,
            mode=wanted_mode,
            rate_bp=wanted_rate,
            fixed_minor=fixed_minor,
            basis=CostBasis.OPENING_BALANCE.value,
            effective_from=effective_from,
            effective_to=effective_to,
        )
        _audit(
            session,
            ctx,
            "loan_cost_component.created",
            "loan",
            loan.id,
            {"component_id": str(component.id), "mode": wanted_mode},
        )

    _write(factory, write)


def update_cost_component(
    factory: sessionmaker[Session],
    ctx: Context,
    loan_id: UUID,
    component_id: UUID,
    *,
    name: str,
    mode: str,
    rate_bp: int | None,
    fixed: Money | None,
    effective_from: date,
    effective_to: date | None,
) -> None:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        currency = _locked_workspace_currency(session, ctx)
        loan = _get_active_loan(session, ctx, loan_id)
        repository = LoanCostComponentRepository(session, ctx)
        component = repository.get_for_loan(loan.id, component_id)
        if component is None:
            raise NotFound
        clean_name = clean_loan_name(name)
        validate_cost_dates(effective_from, effective_to)
        wanted_mode, wanted_rate, fixed_minor = _cost_value(
            mode=mode, rate_bp=rate_bp, fixed=fixed, currency=currency
        )
        repository.update(
            component.id,
            name=clean_name,
            mode=wanted_mode,
            rate_bp=wanted_rate,
            fixed_minor=fixed_minor,
            basis=CostBasis.OPENING_BALANCE.value,
            effective_from=effective_from,
            effective_to=effective_to,
        )
        _audit(
            session,
            ctx,
            "loan_cost_component.updated",
            "loan",
            loan.id,
            {"component_id": str(component.id), "mode": wanted_mode},
        )

    _write(factory, write)


def delete_cost_component(
    factory: sessionmaker[Session], ctx: Context, loan_id: UUID, component_id: UUID
) -> None:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        _locked_workspace_currency(session, ctx)
        loan = _get_active_loan(session, ctx, loan_id)
        repository = LoanCostComponentRepository(session, ctx)
        component = repository.get_for_loan(loan.id, component_id)
        if component is None:
            raise NotFound
        repository.delete(component)
        _audit(
            session,
            ctx,
            "loan_cost_component.deleted",
            "loan",
            loan.id,
            {"component_id": str(component.id)},
        )

    _write(factory, write)


def add_overpayment(
    factory: sessionmaker[Session],
    ctx: Context,
    loan_id: UUID,
    *,
    date_value: date,
    amount: Money,
    note: str,
) -> None:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        currency = _locked_workspace_currency(session, ctx)
        loan = _get_active_loan(session, ctx, loan_id)
        valid_date(date_value, "date")
        amount_minor = _require_money(amount, currency, "amount")
        if amount_minor is None or amount_minor <= 0:
            raise LoanError("amount", "invalid_overpayment")
        clean = clean_note(note)
        overpayment = LoanOverpaymentRepository(session, ctx).create(
            loan_id=loan.id,
            date=date_value,
            amount_minor=amount_minor,
            note=clean,
        )
        _audit(
            session,
            ctx,
            "loan_overpayment.created",
            "loan",
            loan.id,
            {"overpayment_id": str(overpayment.id)},
        )

    _write(factory, write)


def update_overpayment(
    factory: sessionmaker[Session],
    ctx: Context,
    loan_id: UUID,
    overpayment_id: UUID,
    *,
    date_value: date,
    amount: Money,
    note: str,
) -> None:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        currency = _locked_workspace_currency(session, ctx)
        loan = _get_active_loan(session, ctx, loan_id)
        repository = LoanOverpaymentRepository(session, ctx)
        overpayment = repository.get_for_loan(loan.id, overpayment_id)
        if overpayment is None:
            raise NotFound
        valid_date(date_value, "date")
        amount_minor = _require_money(amount, currency, "amount")
        if amount_minor is None or amount_minor <= 0:
            raise LoanError("amount", "invalid_overpayment")
        repository.update(
            overpayment.id,
            date=date_value,
            amount_minor=amount_minor,
            note=clean_note(note),
        )
        _audit(
            session,
            ctx,
            "loan_overpayment.updated",
            "loan",
            loan.id,
            {"overpayment_id": str(overpayment.id)},
        )

    _write(factory, write)


def delete_overpayment(
    factory: sessionmaker[Session], ctx: Context, loan_id: UUID, overpayment_id: UUID
) -> None:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        _locked_workspace_currency(session, ctx)
        loan = _get_active_loan(session, ctx, loan_id)
        repository = LoanOverpaymentRepository(session, ctx)
        overpayment = repository.get_for_loan(loan.id, overpayment_id)
        if overpayment is None:
            raise NotFound
        repository.delete(overpayment)
        _audit(
            session,
            ctx,
            "loan_overpayment.deleted",
            "loan",
            loan.id,
            {"overpayment_id": str(overpayment.id)},
        )

    _write(factory, write)


def workspace_has_loans(session: Session, ctx: Context) -> bool:
    """Part of ``services.workspaces.BASE_CURRENCY_LOCKS``."""
    return LoanRepository(session, ctx).any_exists()


def _reference_name_key(name: str) -> str:
    key = text_key(name)
    if len(key) > 255:
        raise LoanError("name", "invalid_name")
    return key


def _reference_view(
    rate: ReferenceRate,
    values: list[ReferenceRateValue],
    today: date,
) -> ReferenceRateView:
    return ReferenceRateView(
        id=rate.id,
        name=rate.name,
        notes=rate.notes,
        values=tuple(
            ReferenceRateValueView(
                id=value.id,
                effective_from=value.effective_from,
                value_bp=value.value_bp,
                editable=value.effective_from > today,
            )
            for value in values
        ),
        current_value_bp=latest_value_on_or_before(
            [(value.effective_from, value.value_bp) for value in values], today
        ),
    )


def list_reference_rates(factory: sessionmaker[Session], ctx: Context) -> ReferenceRatesPage:
    require_role(ctx, Role.VIEWER)
    today = ctx.clock.today(ctx.workspace.timezone)
    with factory() as session:
        rates = ReferenceRateRepository(session, ctx).ordered()
        values = ReferenceRateValueRepository(session, ctx)
        return ReferenceRatesPage(
            rates=tuple(_reference_view(rate, values.for_rate(rate.id), today) for rate in rates)
        )


def get_reference_rate(
    factory: sessionmaker[Session], ctx: Context, rate_id: UUID
) -> ReferenceRateView:
    require_role(ctx, Role.VIEWER)
    today = ctx.clock.today(ctx.workspace.timezone)
    with factory() as session:
        rate = ReferenceRateRepository(session, ctx).get_or_raise(rate_id)
        values = ReferenceRateValueRepository(session, ctx).for_rate(rate.id)
        return _reference_view(rate, values, today)


def create_reference_rate(
    factory: sessionmaker[Session], ctx: Context, *, name: str, notes: str = ""
) -> UUID:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> UUID:
        require_role_now(session, ctx, Role.EDITOR)
        _locked_workspace_currency(session, ctx)
        clean_name = clean_loan_name(name)
        clean = clean_note(notes)
        repository = ReferenceRateRepository(session, ctx)
        key = _reference_name_key(clean_name)
        if repository.by_name_key(key) is not None:
            raise LoanError("name", "reference_rate_exists")
        rate = repository.create(name=clean_name, name_key=key, notes=clean)
        _audit(
            session,
            ctx,
            "reference_rate.created",
            "reference_rate",
            rate.id,
            {"name": clean_name},
        )
        return rate.id

    return _write(factory, write)


def update_reference_rate(
    factory: sessionmaker[Session],
    ctx: Context,
    rate_id: UUID,
    *,
    name: str,
    notes: str = "",
) -> None:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        _locked_workspace_currency(session, ctx)
        repository = ReferenceRateRepository(session, ctx)
        rate = repository.get_or_raise(rate_id)
        clean_name = clean_loan_name(name)
        key = _reference_name_key(clean_name)
        clash = repository.by_name_key(key)
        if clash is not None and clash.id != rate.id:
            raise LoanError("name", "reference_rate_exists")
        repository.update(
            rate.id,
            name=clean_name,
            name_key=key,
            notes=clean_note(notes),
        )
        _audit(
            session,
            ctx,
            "reference_rate.updated",
            "reference_rate",
            rate.id,
            {"name": clean_name},
        )

    _write(factory, write)


def delete_reference_rate(factory: sessionmaker[Session], ctx: Context, rate_id: UUID) -> None:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        _locked_workspace_currency(session, ctx)
        repository = ReferenceRateRepository(session, ctx)
        rate = repository.get_or_raise(rate_id)
        if repository.used_by_loan(rate.id):
            raise LoanError("reference_rate_id", "reference_rate_in_use")
        values = ReferenceRateValueRepository(session, ctx)
        for value in values.for_rate(rate.id):
            values.delete(value)
        repository.delete(rate)
        _audit(
            session,
            ctx,
            "reference_rate.deleted",
            "reference_rate",
            rate.id,
            {"name": rate.name},
        )

    _write(factory, write)


def add_reference_rate_value(
    factory: sessionmaker[Session],
    ctx: Context,
    rate_id: UUID,
    *,
    effective_from: date,
    value_bp: int,
    high_rate_confirmed: bool,
) -> None:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        _locked_workspace_currency(session, ctx)
        rate = ReferenceRateRepository(session, ctx).get_or_raise(rate_id)
        valid_date(effective_from, "effective_from")
        validate_rate_bp(value_bp)
        _validate_high_rate(value_bp, high_rate_confirmed)
        repository = ReferenceRateValueRepository(session, ctx)
        if repository.exists_on(rate.id, effective_from):
            raise LoanError("effective_from", "value_exists")
        repository.create(
            reference_rate_id=rate.id,
            effective_from=effective_from,
            value_bp=value_bp,
        )
        _validate_reference_targets(session, ctx, rate.id)
        _audit(
            session,
            ctx,
            "reference_rate_value.created",
            "reference_rate",
            rate.id,
            {"effective_from": effective_from.isoformat(), "value_bp": value_bp},
        )

    _write(factory, write)


def _future_reference_value(
    session: Session,
    ctx: Context,
    rate_id: UUID,
    value_id: UUID,
    today: date,
) -> ReferenceRateValue:
    value = ReferenceRateValueRepository(session, ctx).get_for_rate(rate_id, value_id)
    if value is None:
        raise NotFound
    if value.effective_from <= today:
        raise LoanError("effective_from", "value_not_future")
    return value


def update_reference_rate_value(
    factory: sessionmaker[Session],
    ctx: Context,
    rate_id: UUID,
    value_id: UUID,
    *,
    effective_from: date,
    value_bp: int,
    high_rate_confirmed: bool,
) -> None:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        _locked_workspace_currency(session, ctx)
        rate = ReferenceRateRepository(session, ctx).get_or_raise(rate_id)
        today = ctx.clock.today(ctx.workspace.timezone)
        value = _future_reference_value(session, ctx, rate.id, value_id, today)
        valid_date(effective_from, "effective_from")
        if effective_from <= today:
            raise LoanError("effective_from", "value_not_future")
        validate_rate_bp(value_bp)
        _validate_high_rate(value_bp, high_rate_confirmed)
        repository = ReferenceRateValueRepository(session, ctx)
        if repository.exists_on(rate.id, effective_from, exclude_id=value.id):
            raise LoanError("effective_from", "value_exists")
        repository.update(value.id, effective_from=effective_from, value_bp=value_bp)
        _validate_reference_targets(session, ctx, rate.id)
        _audit(
            session,
            ctx,
            "reference_rate_value.updated",
            "reference_rate",
            rate.id,
            {"effective_from": effective_from.isoformat(), "value_bp": value_bp},
        )

    _write(factory, write)


def delete_reference_rate_value(
    factory: sessionmaker[Session], ctx: Context, rate_id: UUID, value_id: UUID
) -> None:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        _locked_workspace_currency(session, ctx)
        rate = ReferenceRateRepository(session, ctx).get_or_raise(rate_id)
        today = ctx.clock.today(ctx.workspace.timezone)
        value = _future_reference_value(session, ctx, rate.id, value_id, today)
        ReferenceRateValueRepository(session, ctx).delete(value)
        _validate_reference_targets(session, ctx, rate.id)
        _audit(
            session,
            ctx,
            "reference_rate_value.deleted",
            "reference_rate",
            rate.id,
            {"effective_from": value.effective_from.isoformat()},
        )

    _write(factory, write)


__all__ = [
    "LoanCheckpointView",
    "LoanCostComponentView",
    "LoanError",
    "LoanOverpaymentView",
    "LoanPage",
    "LoanRateChangeView",
    "LoanView",
    "ReferenceRateValueView",
    "ReferenceRateView",
    "ReferenceRatesPage",
    "add_cost_component",
    "add_overpayment",
    "add_rate_change",
    "add_reference_rate_value",
    "archive_loan",
    "create_loan",
    "create_reference_rate",
    "delete_cost_component",
    "delete_overpayment",
    "delete_rate_change",
    "delete_reference_rate",
    "delete_reference_rate_value",
    "get_loan",
    "get_reference_rate",
    "list_loans",
    "list_reference_rates",
    "restore_loan",
    "set_overpayment_settings",
    "update_cost_component",
    "update_loan_terms",
    "update_overpayment",
    "update_rate_change",
    "update_reference_rate",
    "update_reference_rate_value",
    "workspace_has_loans",
]
