"""The loan list as a small portfolio (F7 list UX)."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import delete

from assetboard.db.models.loans import LoanRateChange
from assetboard.domain.money import Money
from assetboard.services import loan_schedule, loans
from tests.tenant.workspace_db import WorkspaceDb, workspace_db


@pytest.fixture
def team(tmp_path):
    for db in workspace_db(tmp_path):
        db.add_member("owner", "owner")
        db.add_member("viewer", "viewer")
        yield db


def _money(minor: int) -> Money:
    return Money(minor, "EUR")


def _loan(team: WorkspaceDb, **overrides):
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


def _current_loan(team: WorkspaceDb, *, name: str, balance: int, rate_bp: int, **overrides):
    values = {
        "entry_path": "current",
        "name": name,
        "kind": "annuity",
        "principal_original": _money(balance),
        "current_balance": _money(balance),
        "balance_date": date(2026, 2, 1),
        "origination_date": None,
        "first_payment_date": date(2026, 4, 1),
        "term_months": 60,
        "payment_day": 1,
        "rate_mode": "fixed",
        "fixed_rate_bp": rate_bp,
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


def _page(team: WorkspaceDb) -> loans.LoanPage:
    return loans.list_loans(team.factory, team.ctx("viewer", "viewer"))


def test_empty_list_has_zero_portfolio_and_no_rows(team: WorkspaceDb) -> None:
    page = _page(team)
    assert page.active == ()
    assert page.archived == ()
    assert page.summary.currency == "EUR"
    assert page.summary.total_remaining == _money(0)
    assert page.summary.total_monthly == _money(0)
    assert page.summary.due_next_30_days == _money(0)
    assert page.summary.weighted_average_rate_bp is None
    assert page.summary.unavailable_count == 0


def test_active_paid_off_and_archived_rows(team: WorkspaceDb) -> None:
    active_id = _loan(team, name="Active mortgage")
    paid_id = _loan(
        team,
        name="Paid loan",
        principal_original=_money(100_000),
        origination_date=date(2025, 12, 1),
        first_payment_date=date(2026, 1, 1),
        term_months=1,
    )
    archived_id = _loan(team, name="Archived loan")
    loans.archive_loan(team.factory, team.ctx("owner", "owner"), archived_id)

    page = _page(team)
    active = {row.loan.id: row for row in page.active}
    archived = {row.loan.id: row for row in page.archived}
    assert set(active) == {active_id, paid_id}
    assert set(archived) == {archived_id}
    assert active[active_id].status == "active"
    assert active[paid_id].status == "paid_off"
    assert archived[archived_id].status == "archived"

    paid = active[paid_id].figures
    assert paid.remaining_principal == _money(0)
    assert paid.monthly_installment == _money(0)
    assert paid.percent_repaid_bp == 10_000
    assert paid.next_payment_date is None

    assert page.summary.total_remaining == active[active_id].figures.remaining_principal
    assert page.summary.total_monthly == active[active_id].figures.monthly_installment
    assert page.summary.weighted_average_rate_bp == 550


def test_portfolio_totals_and_balance_weighted_average_rate(team: WorkspaceDb) -> None:
    _current_loan(team, name="Large loan", balance=30_000_000, rate_bp=400)
    _current_loan(team, name="Small loan", balance=10_000_000, rate_bp=600)

    summary = _page(team).summary
    assert summary.total_remaining == _money(40_000_000)
    # (30,000,000 * 400 + 10,000,000 * 600) / 40,000,000 = 450 bp
    assert summary.weighted_average_rate_bp == 450
    assert summary.unavailable_count == 0
    assert summary.due_next_30_days == _money(0)


def test_next_30_days_uses_the_projected_installments(team: WorkspaceDb) -> None:
    due_id = _loan(
        team,
        name="Due soon",
        origination_date=date(2026, 1, 1),
        first_payment_date=date(2026, 3, 15),
        payment_day=15,
        term_months=12,
    )
    _loan(
        team,
        name="Due later",
        origination_date=date(2026, 1, 1),
        first_payment_date=date(2026, 5, 1),
        payment_day=1,
        term_months=12,
    )

    page = _page(team)
    row = next(item for item in page.active if item.loan.id == due_id)
    schedule = loan_schedule.loan_schedule(
        team.factory, team.ctx("viewer", "viewer"), due_id
    ).schedule
    assert schedule is not None
    first_due = schedule.rows[0]
    assert first_due.date == date(2026, 3, 15)
    assert row.figures.next_payment_date == first_due.date
    assert row.figures.monthly_installment == first_due.installment_total
    assert row.figures.due_next_30_days == first_due.installment_total
    assert page.summary.due_next_30_days == first_due.installment_total


def test_a_broken_schedule_is_skipped_with_a_count(team: WorkspaceDb) -> None:
    broken_id = _loan(team, name="Broken loan")
    good_id = _loan(
        team,
        name="Good loan",
        principal_original=_money(10_000_000),
    )
    with team.factory.begin() as session:
        session.execute(delete(LoanRateChange).where(LoanRateChange.loan_id == broken_id))

    page = _page(team)
    rows = {row.loan.id: row for row in page.active}
    broken = rows[broken_id]
    assert broken.status == "active"
    assert broken.figures.schedule_error == "missing_rate"
    assert broken.figures.remaining_principal is None
    assert broken.figures.monthly_installment is None
    assert broken.figures.percent_repaid_bp is None
    assert broken.figures.next_payment_date is None
    assert broken.figures.due_next_30_days == _money(0)

    good = rows[good_id]
    assert page.summary.unavailable_count == 1
    assert page.summary.total_remaining == good.figures.remaining_principal
    assert page.summary.total_monthly == good.figures.monthly_installment
