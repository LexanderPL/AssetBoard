"""F3.4/F3.7 work log locks: a received month is soft-locked (it changes only with an explicit
override), a month paid in a closed month is hard-locked, and "Update the transaction" sets the
posted amount to what the logged units give now."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select

from assetboard.db.models.identity import AuditEvent
from assetboard.db.models.ledger import Transaction
from assetboard.domain.money import Money
from assetboard.domain.months import YearMonth
from assetboard.services import income
from assetboard.services.context import Context, PermissionDenied
from assetboard.services.months import close_month, reopen_month
from assetboard.services.occurrences import post_occurrence
from tests.tenant.workspace_db import WorkspaceDb, workspace_db
from tests.unit.test_income_service import _occurrence, _source, categories

AUGUST = date(2026, 8, 1)  # paid on 10 September (lag 1)


@pytest.fixture
def team(tmp_path):
    for db in workspace_db(tmp_path):
        db.add_member("owner", "owner")
        db.add_member("editor", "editor")
        db.add_member("viewer", "viewer")
        yield db


def _received(
    team: WorkspaceDb, *, calendar: bool = False, month: date = AUGUST
) -> tuple[Context, UUID, UUID]:
    """A source whose ``month`` income (2 units of 100.00) is received on its pay day."""
    ctx = team.ctx("owner", "owner")
    income_category, _expense = categories(team, ctx)
    source_id = _source(team, ctx, income_category)
    if calendar:
        income.toggle_work_day(team.factory, ctx, source_id, month.replace(day=5))
        income.toggle_work_day(team.factory, ctx, source_id, month.replace(day=6))
    else:
        income.log_work(team.factory, ctx, source_id, month, Decimal(2))
    occurrence = _occurrence(team, ctx, source_id, month)
    assert occurrence is not None
    transaction_id = post_occurrence(
        team.factory, ctx, occurrence.id, amount_minor=20_000, day=occurrence.due_date
    )
    return ctx, source_id, transaction_id


def _actions(team: WorkspaceDb) -> list[str]:
    with team.factory() as session:
        return list(session.scalars(select(AuditEvent.action).order_by(AuditEvent.ts)).all())


def _amount(team: WorkspaceDb, transaction_id: UUID) -> int:
    with team.factory() as session:
        row = session.get_one(Transaction, transaction_id)
        return row.amount_minor


def test_received_log_is_refused_without_override(team: WorkspaceDb) -> None:
    ctx, source_id, transaction_id = _received(team)
    with pytest.raises(income.WorkLogLocked) as locked:
        income.log_work(team.factory, ctx, source_id, AUGUST, Decimal(3))
    assert locked.value.code == "posted_locked"
    assert locked.value.transaction_id == transaction_id
    assert "work_log.updated_after_posting" not in _actions(team)


def test_override_saves_the_log_audits_and_keeps_the_transaction(team: WorkspaceDb) -> None:
    ctx, source_id, transaction_id = _received(team)
    editor = team.ctx("editor", "editor")
    assert income.log_work(team.factory, editor, source_id, AUGUST, Decimal(3), override=True)
    listed = next(row for row in income.list_sources(team.factory, ctx) if row.id == source_id)
    assert [log.units for log in listed.work_logs] == [Decimal(3)]
    assert "work_log.updated_after_posting" in _actions(team)
    occurrence = _occurrence(team, ctx, source_id, AUGUST)
    assert occurrence is not None and occurrence.status == "posted"
    assert occurrence.expected_minor == 20_000
    assert _amount(team, transaction_id) == 20_000

    change = income.posted_income_change(team.factory, ctx, source_id, AUGUST)
    assert change is not None and change.differs
    assert change.transaction_id == transaction_id
    assert change.transaction_date == date(2026, 9, 10)
    assert change.received == Money(20_000, "PLN")
    assert change.recalculated == Money(30_000, "PLN")


def test_an_open_month_returns_false_and_has_no_posted_change(team: WorkspaceDb) -> None:
    ctx = team.ctx("owner", "owner")
    income_category, _expense = categories(team, ctx)
    source_id = _source(team, ctx, income_category)
    assert not income.log_work(team.factory, ctx, source_id, AUGUST, Decimal(2), override=True)
    assert income.posted_income_change(team.factory, ctx, source_id, AUGUST) is None
    with pytest.raises(income.IncomeError) as error:
        income.update_posted_income(team.factory, ctx, source_id, AUGUST)
    assert error.value.code == "not_received"


def test_received_calendar_month_is_refused_without_override(team: WorkspaceDb) -> None:
    ctx, source_id, transaction_id = _received(team, calendar=True)
    calendar = income.work_calendar(team.factory, ctx, source_id, AUGUST)
    assert calendar.lock.state == "received" and calendar.editable
    assert calendar.lock.transaction_id == transaction_id
    for call in (
        lambda **kw: income.toggle_work_day(team.factory, ctx, source_id, date(2026, 8, 7), **kw),
        lambda **kw: income.set_work_day(
            team.factory, ctx, source_id, date(2026, 8, 3), Decimal("0.5"), **kw
        ),
        lambda **kw: income.fill_working_days(team.factory, ctx, source_id, AUGUST, **kw),
        lambda **kw: income.clear_work_month(team.factory, ctx, source_id, AUGUST, **kw),
    ):
        with pytest.raises(income.WorkLogLocked):
            call()
    assert income.work_calendar(team.factory, ctx, source_id, AUGUST).total == Decimal(2)
    assert "work_log.updated_after_posting" not in _actions(team)

    assert income.toggle_work_day(team.factory, ctx, source_id, date(2026, 8, 7), override=True)
    assert income.work_calendar(team.factory, ctx, source_id, AUGUST).total == Decimal(3)
    assert "work_log.updated_after_posting" in _actions(team)
    assert _amount(team, transaction_id) == 20_000


def test_closed_month_refuses_changes_even_with_override(team: WorkspaceDb) -> None:
    january = date(2026, 1, 1)  # received on 10 February; the test clock is in March
    ctx, source_id, transaction_id = _received(team, calendar=True, month=january)
    close_month(team.factory, ctx, YearMonth(2026, 2))
    calls = (
        lambda: income.log_work(team.factory, ctx, source_id, january, Decimal(3), override=True),
        lambda: income.toggle_work_day(
            team.factory, ctx, source_id, date(2026, 1, 7), override=True
        ),
        lambda: income.clear_work_month(team.factory, ctx, source_id, january, override=True),
        lambda: income.update_posted_income(team.factory, ctx, source_id, january),
    )
    for call in calls:
        with pytest.raises(income.WorkMonthClosed) as error:
            call()
        assert error.value.month == YearMonth(2026, 2)
    calendar = income.work_calendar(team.factory, ctx, source_id, january)
    assert calendar.lock.state == "closed" and not calendar.editable
    listed = next(row for row in income.list_sources(team.factory, ctx) if row.id == source_id)
    assert january in listed.closed_months
    assert calendar.total == Decimal(2)
    assert _amount(team, transaction_id) == 20_000

    reopen_month(team.factory, ctx, YearMonth(2026, 2))
    assert income.toggle_work_day(team.factory, ctx, source_id, date(2026, 1, 7), override=True)


def test_closed_month_without_an_occurrence_uses_the_pay_date(team: WorkspaceDb) -> None:
    ctx = team.ctx("owner", "owner")
    income_category, _expense = categories(team, ctx)
    source_id = _source(team, ctx, income_category)
    close_month(team.factory, ctx, YearMonth(2026, 2))  # January is paid on 10 February
    with pytest.raises(income.WorkMonthClosed):
        income.log_work(team.factory, ctx, source_id, date(2026, 1, 1), Decimal(1))


def test_update_the_transaction_sets_the_recalculated_amount(team: WorkspaceDb) -> None:
    ctx, source_id, transaction_id = _received(team)
    income.log_work(team.factory, ctx, source_id, AUGUST, Decimal(3), override=True)
    editor = team.ctx("editor", "editor")
    change = income.update_posted_income(team.factory, editor, source_id, AUGUST)
    assert change.recalculated == Money(30_000, "PLN")
    assert _amount(team, transaction_id) == 30_000
    actions = _actions(team)
    assert "income.posted_amount_updated" in actions
    assert "transaction.updated" in actions
    after = income.posted_income_change(team.factory, ctx, source_id, AUGUST)
    assert after is not None and not after.differs

    # Nothing to change: no second audit event.
    income.update_posted_income(team.factory, editor, source_id, AUGUST)
    assert _actions(team).count("income.posted_amount_updated") == 1


def test_viewers_cannot_override_or_update_the_transaction(team: WorkspaceDb) -> None:
    _ctx, source_id, transaction_id = _received(team)
    viewer = team.ctx("viewer", "viewer")
    with pytest.raises(PermissionDenied):
        income.log_work(team.factory, viewer, source_id, AUGUST, Decimal(3), override=True)
    with pytest.raises(PermissionDenied):
        income.toggle_work_day(team.factory, viewer, source_id, date(2026, 8, 7), override=True)
    with pytest.raises(PermissionDenied):
        income.update_posted_income(team.factory, viewer, source_id, AUGUST)
    assert _amount(team, transaction_id) == 20_000
