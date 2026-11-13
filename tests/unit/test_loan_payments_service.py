from __future__ import annotations

from dataclasses import replace
from datetime import date
from uuid import UUID, uuid4

import pytest

from assetboard.db.models.loans import LoanPayment
from assetboard.db.repositories import CategoryRepository, LoanRepository, TransactionRepository
from assetboard.domain.loans import (
    DayCount,
    LoanPaymentKind,
    LoanScheduleSummary,
    LoanValidationError,
    Money,
    OverpaymentStrategy,
    ScheduleAnchor,
    ScheduleRow,
    build_schedule,
    summarize_schedule,
)
from assetboard.services import loan_payments, loan_schedule, loans
from assetboard.services.archive import _has_loan_payments, workspace_is_empty
from assetboard.services.context import PermissionDenied
from assetboard.services.loans import LoanView
from tests.tenant.workspace_db import WorkspaceDb, workspace_db


@pytest.fixture
def team(tmp_path):
    for db in workspace_db(tmp_path):
        db.add_member("owner", "owner")
        db.add_member("editor", "editor")
        db.add_member("viewer", "viewer")
        yield db


def _money(minor: int, currency: str = "EUR") -> Money:
    return Money(minor, currency)


def _create(team: WorkspaceDb, **overrides) -> UUID:
    values = {
        "entry_path": "original",
        "name": "Mortgage",
        "kind": "annuity",
        "principal_original": _money(30_000_000),
        "current_balance": None,
        "balance_date": None,
        "origination_date": date(2026, 1, 1),
        "first_payment_date": date(2026, 2, 1),
        "term_months": 360,
        "payment_day": 1,
        "rate_mode": "fixed",
        "fixed_rate_bp": 550,
        "reference_rate_id": None,
        "margin_bp": None,
        "high_rate_confirmed": False,
        "installment_override": None,
        "overpayment_mode": "none",
        "overpayment_amount": None,
        "overpayment_strategy": "reduce_term",
    }
    values.update(overrides)
    return loans.create_loan(team.factory, team.ctx("owner", "owner"), **values)


def _create_current(team: WorkspaceDb, **overrides) -> UUID:
    values = {
        "entry_path": "current",
        "name": "Car loan",
        "principal_original": _money(20_000_000),
        "current_balance": _money(15_000_000),
        "balance_date": date(2026, 2, 1),
        "origination_date": None,
        "first_payment_date": date(2026, 3, 1),
        "term_months": 60,
    }
    values.update(overrides)
    return _create(team, **values)


def _first_row(team: WorkspaceDb, loan_id: UUID) -> tuple[LoanView, ScheduleRow]:
    data = loan_schedule.loan_schedule(team.factory, team.ctx("owner", "owner"), loan_id)
    assert data.schedule is not None
    return data.view, data.schedule.rows[0]


def _transaction(
    team: WorkspaceDb,
    *,
    amount: int,
    direction: str = "expense",
    currency: str = "EUR",
    deleted: bool = False,
):
    ctx = team.ctx("owner", "owner")
    with team.factory.begin() as session:
        repository = CategoryRepository(session, ctx)
        suffix = uuid4().hex[:8]
        category = repository.create(
            name=f"Loan {direction} {suffix}",
            name_key=f"loan {direction} {suffix}",
            kind=direction,
            treatment="regular",
            color="#16a34a",
            sort_order=0,
        )
        transaction = TransactionRepository(session, ctx).create(
            direction=direction,
            day=date(2026, 1, 15),
            amount_minor=amount,
            currency=currency,
            category_id=category.id,
            description="Bank payment",
            description_key="bank payment",
            source="manual",
            created_by_user_id=None,
        )
        if deleted:
            TransactionRepository(session, ctx).update(transaction.id, deleted_at=ctx.clock.now())
        return transaction.id


def test_equal_total_uses_the_projected_row_components(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    view, row = _first_row(team, loan_id)
    payment_id = loan_payments.create_loan_payment(
        team.factory,
        team.ctx("owner", "owner"),
        loan_id,
        date_value=row.date,
        kind="installment",
        total=row.installment_total,
    )
    payment = loan_payments.get_loan_payment(
        team.factory, team.ctx("viewer", "viewer"), loan_id, payment_id
    )
    assert payment.total == row.installment_total
    assert payment.principal == row.principal
    assert payment.interest == row.interest
    assert payment.costs == row.costs
    assert payment.overpayment == row.overpayment
    assert payment.source.value == "manual"


def test_greater_total_adds_the_excess_to_overpayment(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    _, row = _first_row(team, loan_id)
    excess = 50_000
    payment_id = loan_payments.create_loan_payment(
        team.factory,
        team.ctx("owner", "owner"),
        loan_id,
        date_value=row.date,
        kind=LoanPaymentKind.INSTALLMENT,
        total=_money(row.installment_total.amount_minor + excess),
    )
    payment = loan_payments.get_loan_payment(
        team.factory, team.ctx("owner", "owner"), loan_id, payment_id
    )
    assert payment.principal == row.principal
    assert payment.interest == row.interest
    assert payment.overpayment.amount_minor == row.overpayment.amount_minor + excess
    assert payment.total.amount_minor == row.installment_total.amount_minor + excess


def test_smaller_total_fills_interest_then_principal(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    _, row = _first_row(team, loan_id)
    total = row.interest.amount_minor + 1_000
    payment_id = loan_payments.create_loan_payment(
        team.factory,
        team.ctx("owner", "owner"),
        loan_id,
        date_value=row.date,
        kind="installment",
        total=_money(total),
    )
    payment = loan_payments.get_loan_payment(
        team.factory, team.ctx("owner", "owner"), loan_id, payment_id
    )
    assert payment.interest.amount_minor == row.interest.amount_minor
    assert payment.principal.amount_minor == 1_000
    assert payment.costs.amount_minor == 0
    assert payment.overpayment.amount_minor == 0


def test_explicit_breakdown_and_overpayment_kind(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    _, row = _first_row(team, loan_id)
    payment_id = loan_payments.create_loan_payment(
        team.factory,
        team.ctx("owner", "owner"),
        loan_id,
        date_value=row.date,
        kind="installment",
        total=_money(10_000),
        principal=_money(6_000),
        interest=_money(3_000),
        costs=_money(500),
        overpayment=_money(500),
    )
    payment = loan_payments.get_loan_payment(
        team.factory, team.ctx("owner", "owner"), loan_id, payment_id
    )
    assert (
        payment.principal.amount_minor,
        payment.interest.amount_minor,
        payment.costs.amount_minor,
        payment.overpayment.amount_minor,
    ) == (6_000, 3_000, 500, 500)

    overpayment_id = loan_payments.create_loan_payment(
        team.factory,
        team.ctx("owner", "owner"),
        loan_id,
        date_value=row.date,
        kind="overpayment",
        total=_money(7_500),
        principal=_money(1),
        interest=_money(1),
        costs=_money(1),
        overpayment=_money(7_497),
    )
    overpayment = loan_payments.get_loan_payment(
        team.factory, team.ctx("owner", "owner"), loan_id, overpayment_id
    )
    assert overpayment.overpayment.amount_minor == 7_500
    assert overpayment.total.amount_minor == 7_500
    assert overpayment.principal.amount_minor == 0


def test_edit_resplits_when_the_total_changes_and_keeps_it_when_unchanged(
    team: WorkspaceDb,
) -> None:
    loan_id = _create(team)
    ctx = team.ctx("owner", "owner")
    _, row = _first_row(team, loan_id)
    payment_id = loan_payments.create_loan_payment(
        team.factory,
        ctx,
        loan_id,
        date_value=row.date,
        kind="installment",
        total=row.installment_total,
    )
    before = loan_payments.get_loan_payment(team.factory, ctx, loan_id, payment_id)
    loan_payments.update_loan_payment(
        team.factory,
        ctx,
        loan_id,
        payment_id,
        date_value=row.date,
        kind="installment",
        total=_money(row.installment_total.amount_minor + 20_000),
    )
    after = loan_payments.get_loan_payment(team.factory, ctx, loan_id, payment_id)
    assert after.principal == before.principal
    assert after.interest == before.interest
    assert after.overpayment.amount_minor == before.overpayment.amount_minor + 20_000

    loan_payments.update_loan_payment(
        team.factory,
        ctx,
        loan_id,
        payment_id,
        date_value=row.date,
        kind="installment",
        total=after.total,
    )
    same = loan_payments.get_loan_payment(team.factory, ctx, loan_id, payment_id)
    assert same.principal == after.principal
    assert same.overpayment == after.overpayment


def test_delete_payment_and_viewer_permissions(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    _, row = _first_row(team, loan_id)
    with pytest.raises(PermissionDenied):
        loan_payments.create_loan_payment(
            team.factory,
            team.ctx("viewer", "viewer"),
            loan_id,
            date_value=row.date,
            kind="installment",
            total=row.installment_total,
        )
    payment_id = loan_payments.create_loan_payment(
        team.factory,
        team.ctx("editor", "editor"),
        loan_id,
        date_value=row.date,
        kind="installment",
        total=row.installment_total,
    )
    assert loan_payments.list_loan_payments(team.factory, team.ctx("viewer", "viewer"), loan_id)
    with pytest.raises(PermissionDenied):
        loan_payments.delete_loan_payment(
            team.factory, team.ctx("viewer", "viewer"), loan_id, payment_id
        )
    loan_payments.delete_loan_payment(
        team.factory, team.ctx("editor", "editor"), loan_id, payment_id
    )
    assert (
        loan_payments.list_loan_payments(team.factory, team.ctx("viewer", "viewer"), loan_id) == ()
    )


def test_transaction_link_validation(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    _, row = _first_row(team, loan_id)
    total = _money(10_000)
    ctx = team.ctx("owner", "owner")
    valid = _transaction(team, amount=10_000)
    payment_id = loan_payments.create_loan_payment(
        team.factory,
        ctx,
        loan_id,
        date_value=row.date,
        kind="overpayment",
        total=total,
        transaction_id=valid,
    )
    payment = loan_payments.get_loan_payment(team.factory, ctx, loan_id, payment_id)
    assert payment.transaction_description == "Bank payment"

    wrong_amount = _transaction(team, amount=9_000)
    wrong_currency = _transaction(team, amount=10_000, currency="USD")
    income = _transaction(team, amount=10_000, direction="income")
    deleted = _transaction(team, amount=10_000, deleted=True)
    for transaction_id, code in (
        (wrong_amount, "transaction_amount"),
        (wrong_currency, "transaction_currency"),
        (income, "transaction_not_expense"),
        (deleted, "transaction_deleted"),
    ):
        with pytest.raises(loans.LoanError) as raised:
            loan_payments.create_loan_payment(
                team.factory,
                ctx,
                loan_id,
                date_value=row.date,
                kind="overpayment",
                total=total,
                transaction_id=transaction_id,
            )
        assert raised.value.code == code

    with pytest.raises(loans.LoanError):
        loan_payments.create_loan_payment(
            team.factory,
            ctx,
            loan_id,
            date_value=row.date,
            kind="overpayment",
            total=total,
            transaction_id=uuid4(),
        )


def test_invalid_total_and_breakdown(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    _, row = _first_row(team, loan_id)
    ctx = team.ctx("owner", "owner")
    with pytest.raises(loans.LoanError):
        loan_payments.create_loan_payment(
            team.factory, ctx, loan_id, date_value=row.date, kind="installment", total=_money(0)
        )
    with pytest.raises(LoanValidationError):
        loan_payments.create_loan_payment(
            team.factory,
            ctx,
            loan_id,
            date_value=row.date,
            kind="installment",
            total=_money(row.installment_total.amount_minor + 1),
            principal=_money(1),
            interest=_money(1),
            costs=_money(0),
            overpayment=_money(0),
        )


def test_checkpoint_variance_from_origination_and_previous_checkpoint(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    ctx = team.ctx("owner", "owner")
    first_id = loan_payments.create_loan_checkpoint(
        team.factory,
        ctx,
        loan_id,
        as_of=date(2026, 2, 1),
        remaining_principal=_money(29_800_000),
        note="February statement",
    )
    second_id = loan_payments.create_loan_checkpoint(
        team.factory,
        ctx,
        loan_id,
        as_of=date(2026, 3, 1),
        remaining_principal=_money(29_500_000),
        note="March statement",
    )
    rows = loan_payments.list_loan_checkpoints(team.factory, ctx, loan_id)
    assert [row.id for row in rows] == [first_id, second_id]
    assert all(not row.starting for row in rows)
    assert rows[0].projected is not None
    assert rows[0].variance is not None
    assert rows[1].projected is not None
    assert rows[1].variance is not None

    data = loan_schedule.loan_schedule(team.factory, ctx, loan_id)
    assert data.schedule is not None
    assert data.schedule.anchor_date == date(2026, 3, 1)


def test_checkpoint_variance_uses_the_remaining_term_after_a_checkpoint(
    team: WorkspaceDb,
) -> None:
    loan_id = _create(team, term_months=12)
    ctx = team.ctx("owner", "owner")
    loan_payments.create_loan_checkpoint(
        team.factory,
        ctx,
        loan_id,
        as_of=date(2026, 2, 1),
        remaining_principal=_money(29_800_000),
    )
    loan_payments.create_loan_checkpoint(
        team.factory,
        ctx,
        loan_id,
        as_of=date(2026, 3, 1),
        remaining_principal=_money(29_500_000),
    )
    inputs = loan_schedule.load_loan_schedule_inputs(team.factory, ctx, loan_id)
    terms = replace(inputs.terms, term_months=11)
    expected = build_schedule(
        terms,
        inputs.rate_timeline,
        inputs.costs,
        inputs.overpayments,
        ScheduleAnchor(date(2026, 2, 1), _money(29_800_000)),
        date(2026, 3, 1),
        OverpaymentStrategy.REDUCE_TERM,
        DayCount.MONTHLY,
    )
    expected_projected = next(row.closing for row in expected.rows if row.date == date(2026, 3, 1))
    rows = loan_payments.list_loan_checkpoints(team.factory, ctx, loan_id)
    assert rows[1].projected == expected_projected


def test_checkpoint_variance_signs(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    ctx = team.ctx("owner", "owner")
    checkpoint_id = loan_payments.create_loan_checkpoint(
        team.factory,
        ctx,
        loan_id,
        as_of=date(2026, 2, 1),
        remaining_principal=_money(29_800_000),
    )
    row = loan_payments.list_loan_checkpoints(team.factory, ctx, loan_id)[0]
    assert row.projected is not None
    loan_payments.update_loan_checkpoint(
        team.factory,
        ctx,
        loan_id,
        checkpoint_id,
        as_of=date(2026, 2, 1),
        remaining_principal=row.projected,
    )
    zero = loan_payments.list_loan_checkpoints(team.factory, ctx, loan_id)[0]
    assert zero.variance is not None and zero.variance.amount_minor == 0
    loan_payments.update_loan_checkpoint(
        team.factory,
        ctx,
        loan_id,
        checkpoint_id,
        as_of=date(2026, 2, 1),
        remaining_principal=_money(row.projected.amount_minor + 10_000),
    )
    positive = loan_payments.list_loan_checkpoints(team.factory, ctx, loan_id)[0]
    assert positive.variance is not None and positive.variance.amount_minor == 10_000
    loan_payments.update_loan_checkpoint(
        team.factory,
        ctx,
        loan_id,
        checkpoint_id,
        as_of=date(2026, 2, 1),
        remaining_principal=_money(row.projected.amount_minor - 10_000),
    )
    negative = loan_payments.list_loan_checkpoints(team.factory, ctx, loan_id)[0]
    assert negative.variance is not None and negative.variance.amount_minor == -10_000


def test_a_past_payment_splits_against_the_schedule_before_a_checkpoint(
    team: WorkspaceDb,
) -> None:
    loan_id = _create(team, term_months=12)
    _, expected = _first_row(team, loan_id)
    ctx = team.ctx("owner", "owner")
    loan_payments.create_loan_checkpoint(
        team.factory,
        ctx,
        loan_id,
        as_of=date(2026, 3, 1),
        remaining_principal=_money(29_000_000),
    )
    payment_id = loan_payments.create_loan_payment(
        team.factory,
        ctx,
        loan_id,
        date_value=expected.date,
        kind="installment",
        total=expected.installment_total,
    )
    with team.factory() as session:
        payment = session.get(LoanPayment, payment_id)
        assert payment is not None
        assert payment.principal_minor == expected.principal.amount_minor
        assert payment.interest_minor == expected.interest.amount_minor
        assert payment.costs_minor == expected.costs.amount_minor


def test_a_payment_after_the_schedule_has_no_row(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    with pytest.raises(loans.LoanError) as raised:
        loan_payments.create_loan_payment(
            team.factory,
            team.ctx("owner", "owner"),
            loan_id,
            date_value=date(2199, 1, 1),
            kind="installment",
            total=_money(10_000),
        )
    assert raised.value.code == "no_scheduled_payment"


def test_a_current_path_checkpoint_cannot_start_before_the_starting_balance(
    team: WorkspaceDb,
) -> None:
    loan_id = _create_current(team)
    ctx = team.ctx("owner", "owner")
    with pytest.raises(loans.LoanError) as raised:
        loan_payments.create_loan_checkpoint(
            team.factory,
            ctx,
            loan_id,
            as_of=date(2026, 1, 1),
            remaining_principal=_money(16_000_000),
        )
    assert raised.value.code == "checkpoint_before_start"


def test_checkpoint_projection_failure_marks_the_row(team: WorkspaceDb) -> None:
    ctx = team.ctx("owner", "owner")
    with team.factory.begin() as session:
        LoanRepository(session, ctx).create(
            name="No rate loan",
            kind="annuity",
            currency="EUR",
            principal_original_minor=100_000,
            origination_date=date(2026, 1, 1),
            first_payment_date=date(2026, 2, 1),
            term_months=12,
            payment_day=1,
            rate_mode="fixed",
            day_count="monthly",
            margin_bp=None,
            reference_rate_id=None,
            installment_override_minor=None,
            overpayment_mode="none",
            overpayment_amount_minor=None,
            overpayment_strategy="reduce_term",
            archived_at=None,
        )
        loan_id = LoanRepository(session, ctx).ordered()[0].id
    loan_payments.create_loan_checkpoint(
        team.factory,
        ctx,
        loan_id,
        as_of=date(2026, 2, 1),
        remaining_principal=_money(90_000),
    )
    rows = loan_payments.list_loan_checkpoints(team.factory, ctx, loan_id)
    assert rows[0].projected is None
    assert rows[0].variance is None
    assert rows[0].error_code == "missing_rate"


def test_current_path_starting_checkpoint_is_required(team: WorkspaceDb) -> None:
    ctx = team.ctx("owner", "owner")
    loan_id = _create(
        team,
        entry_path="current",
        principal_original=None,
        origination_date=None,
        current_balance=_money(20_000_000),
        balance_date=date(2026, 2, 1),
        first_payment_date=date(2026, 3, 1),
        term_months=120,
    )
    rows = loan_payments.list_loan_checkpoints(team.factory, ctx, loan_id)
    assert len(rows) == 1 and rows[0].starting
    assert rows[0].projected is None and rows[0].variance is None
    with pytest.raises(loans.LoanError) as raised:
        loan_payments.delete_loan_checkpoint(team.factory, ctx, loan_id, rows[0].id)
    assert raised.value.code == "starting_checkpoint_required"


def test_checkpoint_validation_and_duplicate_dates(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    ctx = team.ctx("owner", "owner")
    with pytest.raises(loans.LoanError) as raised:
        loan_payments.create_loan_checkpoint(
            team.factory, ctx, loan_id, as_of=date(2026, 4, 1), remaining_principal=_money(1)
        )
    assert raised.value.code == "checkpoint_future"
    with pytest.raises(loans.LoanError) as raised:
        loan_payments.create_loan_checkpoint(
            team.factory, ctx, loan_id, as_of=date(2025, 12, 1), remaining_principal=_money(1)
        )
    assert raised.value.code == "checkpoint_before_start"
    with pytest.raises(loans.LoanError) as raised:
        loan_payments.create_loan_checkpoint(
            team.factory, ctx, loan_id, as_of=date(2026, 2, 1), remaining_principal=_money(-1)
        )
    assert raised.value.code == "invalid_balance"
    checkpoint_id = loan_payments.create_loan_checkpoint(
        team.factory, ctx, loan_id, as_of=date(2026, 2, 1), remaining_principal=_money(0)
    )
    with pytest.raises(loans.LoanError) as raised:
        loan_payments.create_loan_checkpoint(
            team.factory, ctx, loan_id, as_of=date(2026, 2, 1), remaining_principal=_money(1)
        )
    assert raised.value.code == "checkpoint_exists"
    loan_payments.update_loan_checkpoint(
        team.factory,
        ctx,
        loan_id,
        checkpoint_id,
        as_of=date(2026, 2, 2),
        remaining_principal=_money(1),
        note="Updated",
    )
    rows = loan_payments.list_loan_checkpoints(team.factory, ctx, loan_id)
    assert rows[0].as_of == date(2026, 2, 2)
    assert rows[0].note == "Updated"
    loan_payments.delete_loan_checkpoint(team.factory, ctx, loan_id, checkpoint_id)
    assert loan_payments.list_loan_checkpoints(team.factory, ctx, loan_id) == ()


def test_archive_emptiness_sees_actual_payments(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    _, row = _first_row(team, loan_id)
    loan_payments.create_loan_payment(
        team.factory,
        team.ctx("owner", "owner"),
        loan_id,
        date_value=row.date,
        kind="installment",
        total=row.installment_total,
    )
    with team.factory() as session:
        ctx = team.ctx("owner", "owner")
        assert _has_loan_payments(session, ctx)
        assert not workspace_is_empty(session, ctx)


def test_schedule_actuals_and_reanchoring(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    ctx = team.ctx("owner", "owner")
    _, row = _first_row(team, loan_id)
    loan_payments.create_loan_payment(
        team.factory,
        ctx,
        loan_id,
        date_value=row.date,
        kind="installment",
        total=row.installment_total,
    )
    data = loan_schedule.loan_schedule(team.factory, ctx, loan_id)
    assert data.actual_by_date[row.date].total == row.installment_total
    assert data.actual_by_date[row.date].count == 1
    assert len(data.actual_payments) == 1

    before = data.schedule
    assert before is not None
    loan_payments.create_loan_checkpoint(
        team.factory,
        ctx,
        loan_id,
        as_of=date(2026, 3, 1),
        remaining_principal=_money(29_000_000),
    )
    after = loan_schedule.loan_schedule(team.factory, ctx, loan_id)
    assert after.schedule is not None
    assert after.schedule.anchor_date == date(2026, 3, 1)
    assert after.schedule.rows[0].date > before.rows[0].date
    assert after.summary is not None
    assert isinstance(after.summary, LoanScheduleSummary)
    assert after.summary == summarize_schedule(after.schedule, after.summary.as_of)


def test_plan_status_summarises_the_last_payment_and_checkpoint(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    ctx = team.ctx("owner", "owner")
    empty = loan_payments.plan_status(team.factory, team.ctx("viewer", "viewer"), loan_id)
    assert empty.last_payment is None
    assert empty.latest_checkpoint is None
    assert empty.status is None

    _, row = _first_row(team, loan_id)
    payment_id = loan_payments.create_loan_payment(
        team.factory,
        ctx,
        loan_id,
        date_value=row.date,
        kind="installment",
        total=row.installment_total,
    )
    loan_payments.create_loan_checkpoint(
        team.factory,
        ctx,
        loan_id,
        as_of=row.date,
        remaining_principal=_money(29_000_000),
    )
    status = loan_payments.plan_status(team.factory, team.ctx("viewer", "viewer"), loan_id)
    assert status.last_payment is not None and status.last_payment.id == payment_id
    assert status.latest_checkpoint is not None
    assert status.status == "ahead"
    assert status.variance is not None and status.variance.amount_minor > 0


def _payment_variance(
    team: WorkspaceDb, loan_id: UUID, date_value: date, total: int, **kwargs
) -> loan_payments.LoanPaymentView:
    ctx = team.ctx("owner", "owner")
    loan_payments.create_loan_payment(
        team.factory,
        ctx,
        loan_id,
        date_value=date_value,
        kind=kwargs.pop("kind", "installment"),
        total=_money(total),
        **kwargs,
    )
    return loan_payments.list_loan_payments(team.factory, ctx, loan_id)[0]


def test_payment_variance_equal_total_and_principal(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    _, row = _first_row(team, loan_id)
    payment = _payment_variance(team, loan_id, row.date, row.installment_total.amount_minor)
    assert payment.variance is not None
    assert payment.variance_error is None
    assert payment.variance.label == "equal"
    assert payment.variance.matched_date == row.date
    assert payment.variance.projected_total == row.installment_total
    assert payment.variance.total_variance.amount_minor == 0
    assert payment.variance.projected_principal == row.principal
    assert payment.variance.principal_variance.amount_minor == 0


def test_payment_variance_larger_total_keeps_the_projected_principal(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    _, row = _first_row(team, loan_id)
    excess = 50_000
    payment = _payment_variance(
        team, loan_id, row.date, row.installment_total.amount_minor + excess
    )
    assert payment.variance is not None
    assert payment.variance.label == "more"
    assert payment.variance.total_variance.amount_minor == excess
    assert payment.variance.principal_variance.amount_minor == 0


def test_payment_variance_smaller_total_reduces_the_principal(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    _, row = _first_row(team, loan_id)
    shortfall = 10_000
    payment = _payment_variance(
        team, loan_id, row.date, row.installment_total.amount_minor - shortfall
    )
    assert payment.variance is not None
    assert payment.variance.label == "less"
    assert payment.variance.total_variance.amount_minor == -shortfall
    assert payment.variance.principal_variance.amount_minor == -shortfall


def test_payment_variance_matches_the_first_later_row(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    data = loan_schedule.loan_schedule(team.factory, team.ctx("owner", "owner"), loan_id)
    assert data.schedule is not None
    expected = data.schedule.rows[1]
    payment = _payment_variance(
        team, loan_id, date(2026, 2, 15), expected.installment_total.amount_minor
    )
    assert payment.variance is not None
    assert payment.variance.matched_date == expected.date
    assert payment.variance.total_variance.amount_minor == 0


def test_payment_variance_handles_overpayment_only_and_no_row(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    ctx = team.ctx("owner", "owner")
    loan_payments.create_loan_payment(
        team.factory,
        ctx,
        loan_id,
        date_value=date(2026, 2, 1),
        kind="overpayment",
        total=_money(20_000),
    )
    payments = loan_payments.list_loan_payments(team.factory, ctx, loan_id)
    assert payments[0].kind is LoanPaymentKind.OVERPAYMENT
    assert payments[0].variance is None
    assert payments[0].variance_error is None

    loan_payments.create_loan_payment(
        team.factory,
        ctx,
        loan_id,
        date_value=date(2199, 1, 1),
        kind="installment",
        total=_money(10_000),
        principal=_money(10_000),
        interest=_money(0),
        costs=_money(0),
        overpayment=_money(0),
    )
    after_payoff = loan_payments.list_loan_payments(team.factory, ctx, loan_id)[0]
    assert after_payoff.variance is None
    assert after_payoff.variance_error == "no_scheduled_payment"


def test_payment_variance_handles_a_broken_schedule(team: WorkspaceDb) -> None:
    ctx = team.ctx("owner", "owner")
    with team.factory.begin() as session:
        LoanRepository(session, ctx).create(
            name="No rate loan",
            kind="annuity",
            currency="EUR",
            principal_original_minor=100_000,
            origination_date=date(2026, 1, 1),
            first_payment_date=date(2026, 2, 1),
            term_months=12,
            payment_day=1,
            rate_mode="fixed",
            day_count="monthly",
            margin_bp=None,
            reference_rate_id=None,
            installment_override_minor=None,
            overpayment_mode="none",
            overpayment_amount_minor=None,
            overpayment_strategy="reduce_term",
            archived_at=None,
        )
        loan_id = LoanRepository(session, ctx).ordered()[0].id
    loan_payments.create_loan_payment(
        team.factory,
        ctx,
        loan_id,
        date_value=date(2026, 2, 1),
        kind="installment",
        total=_money(10_000),
        principal=_money(10_000),
        interest=_money(0),
        costs=_money(0),
        overpayment=_money(0),
    )
    payment = loan_payments.list_loan_payments(team.factory, ctx, loan_id)[0]
    assert payment.variance is None
    assert payment.variance_error == "missing_rate"
