from __future__ import annotations

from datetime import date

import pytest

from assetboard.db.repositories import LoanRepository
from assetboard.domain.loans import (
    DayCount,
    LoanValidationError,
    OverpaymentMode,
    OverpaymentStrategy,
    RateMode,
    summarize_schedule,
)
from assetboard.domain.money import Money
from assetboard.services import loan_schedule, loans
from tests.tenant.workspace_db import WorkspaceDb, workspace_db


@pytest.fixture
def team(tmp_path):
    for db in workspace_db(tmp_path):
        db.add_member("owner", "owner")
        db.add_member("viewer", "viewer")
        yield db


def _money(minor: int, currency: str = "EUR") -> Money:
    return Money(minor, currency)


def _create(team: WorkspaceDb, **overrides):
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


def test_loan_schedule_maps_the_service_and_baseline(team: WorkspaceDb) -> None:
    loan_id = _create(
        team,
        overpayment_mode=OverpaymentMode.FIXED_EXTRA,
        overpayment_amount=_money(100_000),
        overpayment_strategy=OverpaymentStrategy.REDUCE_TERM,
    )
    ctx = team.ctx("owner", "owner")
    loans.add_cost_component(
        team.factory,
        ctx,
        loan_id,
        name="Bank fee",
        mode="fixed",
        rate_bp=None,
        fixed=_money(2_500),
        effective_from=date(2026, 1, 1),
        effective_to=None,
    )
    loans.add_overpayment(
        team.factory,
        ctx,
        loan_id,
        date_value=date(2026, 7, 1),
        amount=_money(500_000),
        note="Bonus",
    )
    data = loan_schedule.loan_schedule(team.factory, ctx, loan_id)
    assert data.error_code is None
    assert data.schedule is not None and data.baseline is not None
    assert data.summary is not None
    assert data.summary == summarize_schedule(data.schedule, data.summary.as_of)
    assert data.has_overpayments
    assert data.schedule.rows != data.baseline.rows
    assert data.summary.total_costs.amount_minor > 0
    assert data.summary.total_overpayments.amount_minor > 0
    assert data.view.day_count is DayCount.MONTHLY
    assert data.view.reference_rate_values == ()


def test_viewer_can_read_a_loan_schedule(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    data = loan_schedule.loan_schedule(team.factory, team.ctx("viewer", "viewer"), loan_id)
    assert data.error_code is None
    assert data.schedule is not None and data.schedule.rows


def test_variable_rate_values_plus_margin_form_the_timeline(team: WorkspaceDb) -> None:
    ctx = team.ctx("owner", "owner")
    rate_id = loans.create_reference_rate(team.factory, ctx, name="WIBOR 3M")
    loans.add_reference_rate_value(
        team.factory,
        ctx,
        rate_id,
        effective_from=date(2026, 1, 1),
        value_bp=400,
        high_rate_confirmed=False,
    )
    loans.add_reference_rate_value(
        team.factory,
        ctx,
        rate_id,
        effective_from=date(2027, 1, 1),
        value_bp=500,
        high_rate_confirmed=False,
    )
    loan_id = _create(
        team,
        name="Variable mortgage",
        rate_mode=RateMode.VARIABLE,
        fixed_rate_bp=None,
        reference_rate_id=rate_id,
        margin_bp=150,
    )
    data = loan_schedule.loan_schedule(team.factory, ctx, loan_id)
    assert data.error_code is None and data.schedule is not None
    assert data.view.reference_rate_values
    assert [row.annual_rate_bp for row in data.schedule.rows[:11]] == [550] * 11
    assert data.schedule.rows[11].annual_rate_bp == 650


def test_a_schedule_error_returns_the_code_and_no_schedule(team: WorkspaceDb) -> None:
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
    data = loan_schedule.loan_schedule(team.factory, ctx, loan_id)
    assert data.schedule is None and data.summary is None
    assert data.error_code == "missing_rate"


def test_day_count_is_passed_to_the_engine(team: WorkspaceDb) -> None:
    loan_id = _create(team, day_count="actual_365")
    data = loan_schedule.loan_schedule(team.factory, team.ctx("owner", "owner"), loan_id)
    assert data.view.day_count is DayCount.ACTUAL_365
    assert data.schedule is not None and data.schedule.rows
    # The first actual/365 period runs from 1 Jan to 1 Feb: 31 days.
    expected = (30_000_000 * 550 * 31 * 2 + 3_650_000) // (2 * 3_650_000)
    assert data.schedule.rows[0].interest.amount_minor == expected


def test_invalid_day_count_is_refused(team: WorkspaceDb) -> None:
    with pytest.raises(LoanValidationError):
        _create(team, day_count="weekly")


def test_a_fixed_loan_can_switch_to_a_reference_rate_and_back(team: WorkspaceDb) -> None:
    ctx = team.ctx("owner", "owner")
    rate_id = loans.create_reference_rate(team.factory, ctx, name="WIBOR 3M")
    loans.add_reference_rate_value(
        team.factory,
        ctx,
        rate_id,
        effective_from=date(2026, 1, 1),
        value_bp=400,
        high_rate_confirmed=False,
    )
    loans.add_reference_rate_value(
        team.factory,
        ctx,
        rate_id,
        effective_from=date(2026, 7, 1),
        value_bp=450,
        high_rate_confirmed=False,
    )
    loans.add_reference_rate_value(
        team.factory,
        ctx,
        rate_id,
        effective_from=date(2028, 1, 1),
        value_bp=500,
        high_rate_confirmed=False,
    )
    loan_id = _create(team, term_months=48)
    loans.add_rate_change(
        team.factory,
        ctx,
        loan_id,
        effective_from=date(2027, 1, 1),
        mode="variable",
        reference_rate_id=rate_id,
        margin_bp=150,
        high_rate_confirmed=False,
    )
    loans.add_rate_change(
        team.factory,
        ctx,
        loan_id,
        effective_from=date(2029, 1, 1),
        rate_bp=700,
        high_rate_confirmed=False,
    )
    data = loan_schedule.loan_schedule(team.factory, ctx, loan_id)
    assert data.error_code is None and data.schedule is not None
    rates = {(row.date.year, row.date.month): row.annual_rate_bp for row in data.schedule.rows}
    assert rates[(2026, 12)] == 550
    assert rates[(2027, 1)] == 600
    assert rates[(2028, 1)] == 650
    assert rates[(2029, 1)] == 700
    assert [change.mode for change in data.view.rate_changes] == [
        RateMode.FIXED,
        RateMode.VARIABLE,
        RateMode.FIXED,
    ]
    assert data.view.rate_changes[1].effective_rate_bp == 600

    used_by = loans.list_reference_rates(team.factory, ctx).rates[0].used_by
    assert [reference.id for reference in used_by] == [loan_id]
    with pytest.raises(loans.LoanError) as error:
        loans.delete_reference_rate(team.factory, ctx, rate_id)
    assert error.value.code == "reference_rate_in_use"


def test_a_new_reference_value_reprices_its_variable_segment(team: WorkspaceDb) -> None:
    ctx = team.ctx("owner", "owner")
    rate_id = loans.create_reference_rate(team.factory, ctx, name="WIBOR 3M")
    loans.add_reference_rate_value(
        team.factory,
        ctx,
        rate_id,
        effective_from=date(2026, 1, 1),
        value_bp=400,
        high_rate_confirmed=False,
    )
    loan_id = _create(team, term_months=36)
    loans.add_rate_change(
        team.factory,
        ctx,
        loan_id,
        effective_from=date(2027, 1, 1),
        mode="variable",
        reference_rate_id=rate_id,
        margin_bp=150,
        high_rate_confirmed=False,
    )
    loans.add_reference_rate_value(
        team.factory,
        ctx,
        rate_id,
        effective_from=date(2027, 7, 1),
        value_bp=500,
        high_rate_confirmed=False,
    )
    data = loan_schedule.loan_schedule(team.factory, ctx, loan_id)
    assert data.error_code is None and data.schedule is not None
    rates = {(row.date.year, row.date.month): row.annual_rate_bp for row in data.schedule.rows}
    assert rates[(2027, 6)] == 550
    assert rates[(2027, 7)] == 650
