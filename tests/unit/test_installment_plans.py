from __future__ import annotations

from datetime import date
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from assetboard.db.models.identity import AuditEvent
from assetboard.db.repositories import (
    CategoryRepository,
    InstallmentPlanRepository,
    LoanPaymentRepository,
    LoanRepository,
    NetWorthCategoryRepository,
    ScheduledItemRepository,
    ScheduledOccurrenceRepository,
)
from assetboard.domain.loans import LoanKind, LoanValidationError, Money
from assetboard.domain.scheduled import AmountMode
from assetboard.services import installment_plans, loan_links, loan_scheduled, loans, occurrences
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


def _category(team: WorkspaceDb, kind: str = "expense", name: str = "Housing") -> UUID:
    ctx = team.ctx("owner", "owner")
    with team.factory.begin() as session:
        suffix = uuid4().hex[:6]
        row = CategoryRepository(session, ctx).create(
            name=f"{name} {kind} {suffix}",
            name_key=f"{name} {kind} {suffix}".casefold(),
            kind=kind,
            treatment="regular",
            color="#16a34a",
            sort_order=0,
        )
        return row.id


def _create(team: WorkspaceDb, **overrides) -> UUID:
    values = {
        "name": "Laptop",
        "installment": _money(100_000),
        "remaining_count": 3,
        "payment_day": 5,
        "category_id": _category(team),
    }
    values.update(overrides)
    return installment_plans.create_installment_plan(
        team.factory, team.ctx("owner", "owner"), **values
    )


def test_create_next_due_end_date_and_scheduled_item(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    ctx = team.ctx("owner", "owner")
    view = installment_plans.get_installment_plan(team.factory, ctx, loan_id)
    assert view.name == "Laptop"
    assert view.installment == _money(100_000)
    assert view.remaining_count == 3
    assert view.total_remaining == _money(300_000)
    assert view.next_due == date(2026, 3, 5)
    assert view.end_date == date(2026, 5, 5)
    assert view.loan.kind is LoanKind.INSTALLMENT_PLAN

    with team.factory() as session:
        item = ScheduledItemRepository(session, ctx).ordered()[0]
        rows = ScheduledOccurrenceRepository(session, ctx).for_item(item.id)
    assert item.loan_id == loan_id
    assert item.amount_mode == AmountMode.FROM_LOAN.value
    assert item.amount_minor == 0
    assert [row.due_date for row in rows] == [
        date(2026, 3, 5),
        date(2026, 4, 5),
        date(2026, 5, 5),
    ]
    assert all(row.expected_minor == 100_000 for row in rows)
    assert all(row.amount_state == "estimated" for row in rows)


def test_payment_day_31_clamps_to_month_end(team: WorkspaceDb) -> None:
    loan_id = _create(team, payment_day=31, remaining_count=2)
    view = installment_plans.get_installment_plan(team.factory, team.ctx("owner", "owner"), loan_id)
    assert view.next_due == date(2026, 3, 31)
    assert view.end_date == date(2026, 4, 30)


def test_update_recalculates_plan_and_occurrences(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    ctx = team.ctx("owner", "owner")
    category_id = _category(team, name="Office")
    installment_plans.update_installment_plan(
        team.factory,
        ctx,
        loan_id,
        name="Laptop Pro",
        installment=_money(150_000),
        remaining_count=4,
        payment_day=10,
        category_id=category_id,
    )
    view = installment_plans.get_installment_plan(team.factory, ctx, loan_id)
    assert view.name == "Laptop Pro"
    assert view.installment == _money(150_000)
    assert view.remaining_count == 4
    assert view.total_remaining == _money(600_000)
    assert view.next_due == date(2026, 3, 10)
    assert view.end_date == date(2026, 6, 10)
    with team.factory() as session:
        item = ScheduledItemRepository(session, ctx).ordered()[0]
        rows = ScheduledOccurrenceRepository(session, ctx).for_item(item.id)
    assert all(row.expected_minor == 150_000 for row in rows)


def test_validation(team: WorkspaceDb) -> None:
    with pytest.raises(loans.LoanError):
        _create(team, installment=_money(0))
    with pytest.raises(loans.LoanError):
        _create(team, remaining_count=0)
    with pytest.raises((loans.LoanError, LoanValidationError)):
        _create(team, payment_day=0)
    with pytest.raises((loans.LoanError, LoanValidationError)):
        _create(team, name="")
    income = _category(team, kind="income", name="Salary")
    with pytest.raises(loans.LoanError) as raised:
        _create(team, category_id=income)
    assert raised.value.code == "invalid_category"


def test_posted_occurrence_decrements_once_and_closes_at_zero(team: WorkspaceDb) -> None:
    loan_id = _create(team, remaining_count=1)
    ctx = team.ctx("owner", "owner")
    with team.factory() as session:
        occurrence = ScheduledOccurrenceRepository(session, ctx).for_item(
            ScheduledItemRepository(session, ctx).ordered()[0].id
        )[0]
    transaction_id = occurrences.post_occurrence(
        team.factory,
        ctx,
        occurrence.id,
        amount_minor=100_000,
        day=date(2026, 3, 1),
    )
    occurrences.post_occurrence(
        team.factory,
        ctx,
        occurrence.id,
        amount_minor=100_000,
        day=date(2026, 3, 1),
    )
    view = installment_plans.get_installment_plan(team.factory, ctx, loan_id)
    assert view.remaining_count == 0
    assert view.closed
    with team.factory() as session:
        plan = InstallmentPlanRepository(session, ctx).for_loan(loan_id)
        loan = LoanRepository(session, ctx).get(loan_id)
        payments = LoanPaymentRepository(session, ctx).for_loan(loan_id)
    assert plan is not None and plan.closed_at is not None
    assert loan is not None and loan.archived_at is not None
    assert payments == []
    assert transaction_id


def test_archive_and_restore(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    ctx = team.ctx("owner", "owner")
    installment_plans.archive_installment_plan(team.factory, ctx, loan_id)
    closed = installment_plans.get_installment_plan(team.factory, ctx, loan_id)
    assert closed.closed and closed.loan.archived
    installment_plans.restore_installment_plan(team.factory, ctx, loan_id)
    restored = installment_plans.get_installment_plan(team.factory, ctx, loan_id)
    assert not restored.closed and not restored.loan.archived
    assert restored.remaining_count == 3


def test_list_active_and_closed(team: WorkspaceDb) -> None:
    first = _create(team, name="First")
    second = _create(team, name="Second")
    ctx = team.ctx("owner", "owner")
    installment_plans.update_installment_plan(
        team.factory,
        ctx,
        second,
        name="Second",
        installment=_money(100_000),
        remaining_count=0,
        payment_day=5,
        category_id=installment_plans.get_installment_plan(team.factory, ctx, second).category_id,
    )
    page = installment_plans.list_installment_plans(team.factory, team.ctx("viewer", "viewer"))
    assert [row.loan.id for row in page.active] == [first]
    assert [row.loan.id for row in page.closed] == [second]


def test_plan_balance_and_installments_liability(team: WorkspaceDb) -> None:
    loan_id = _create(team, remaining_count=3)
    ctx = team.ctx("owner", "owner")
    with team.factory() as session:
        assert loan_links.loan_balance_at(session, ctx, loan_id, date(2026, 3, 1)) == _money(
            300_000
        )
    liability = loan_links.create_loan_liability_item(team.factory, ctx, loan_id)
    again = loan_links.create_loan_liability_item(team.factory, ctx, loan_id)
    assert liability.id == again.id
    with team.factory() as session:
        categories = NetWorthCategoryRepository(session, ctx).ordered("liability")
    assert any(category.name == "Installments" for category in categories)


def test_scheduled_generation_amounts_and_disable(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    ctx = team.ctx("owner", "owner")
    view = loan_scheduled.loan_schedule_item_view(team.factory, ctx, loan_id)
    assert view is not None
    assert view.payoff_date == date(2026, 5, 5)
    loan_scheduled.disable_loan_schedule_item(team.factory, ctx, loan_id)
    assert loan_scheduled.loan_schedule_item_view(team.factory, ctx, loan_id) is None
    with team.factory() as session:
        rows = ScheduledOccurrenceRepository(session, ctx).for_item(view.item_id)
    assert rows == []


def test_audit_has_no_amounts(team: WorkspaceDb) -> None:
    _create(team)
    with team.factory() as session:
        data = list(session.scalars(select(AuditEvent.data)))
    assert all("amount" not in str(item) for item in data)
