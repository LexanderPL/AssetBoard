"""Live FIRE loan resolution (F8.12): annual payments, payoff years and visible states."""

from __future__ import annotations

from datetime import date

import pytest

from assetboard.domain.loans import ScheduleError
from assetboard.domain.money import Money
from assetboard.services import fire_loans, loan_schedule, loans
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
        "principal_original": _money(12_000_000),
        "current_balance": None,
        "balance_date": None,
        "origination_date": date(2026, 1, 1),
        "first_payment_date": date(2026, 2, 1),
        "term_months": 24,
        "payment_day": 1,
        "rate_mode": "fixed",
        "fixed_rate_bp": 600,
        "reference_rate_id": None,
        "margin_bp": None,
        "high_rate_confirmed": False,
        "installment_override": None,
        "overpayment_mode": "none",
        "overpayment_amount": None,
        "overpayment_strategy": "reduce_term",
        "drawdown_mode": "full_at_origination",
        "interest_only_months": 0,
    }
    values.update(overrides)
    return loans.create_loan(team.factory, team.ctx("owner", "owner"), **values)


def _load(team: WorkspaceDb, **overrides) -> fire_loans.FireLoanData:
    return fire_loans.load_fire_loans(
        team.factory,
        team.ctx("viewer", "viewer"),
        currency="EUR",
        **overrides,
    )


def _row(data: fire_loans.FireLoanData, loan_id) -> fire_loans.FireLoanRow:
    return next(row for row in data.rows if row.loan_id == loan_id)


def test_a_plain_loan_annualises_the_next_scheduled_payment(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    data = _load(team)
    row = _row(data, loan_id)
    assert row.state is fire_loans.LoanPlanState.INCLUDED
    assert row.eligible
    assert row.monthly_payment is not None and row.annual_payment is not None
    assert row.annual_payment.amount_minor == row.monthly_payment.amount_minor * 12
    assert row.payoff_year == 2028
    assert data.included_annual_total == row.annual_payment
    assert data.ignored_annual_total == _money(0)
    assert row.current_balance is not None and row.current_balance.amount_minor > 0


def test_regular_overpayment_is_part_of_the_annual_payment(team: WorkspaceDb) -> None:
    plain_id = _create(team)
    extra_id = _create(
        team,
        name="Extra",
        overpayment_mode="fixed_extra",
        overpayment_amount=_money(100_000),
    )
    data = _load(team)
    plain = _row(data, plain_id)
    extra = _row(data, extra_id)
    assert plain.annual_payment is not None and extra.annual_payment is not None
    assert extra.annual_payment.amount_minor > plain.annual_payment.amount_minor
    assert extra.annual_payment.amount_minor > plain.annual_payment.amount_minor
    assert extra.monthly_payment is not None and extra.annual_payment is not None
    assert extra.annual_payment.amount_minor == extra.monthly_payment.amount_minor * 12


def test_a_future_one_off_changes_payoff_not_the_annual_payment(team: WorkspaceDb) -> None:
    base_id = _create(team)
    one_off_id = _create(team, name="One-off")
    ctx = team.ctx("owner", "owner")
    loans.add_overpayment(
        team.factory,
        ctx,
        one_off_id,
        date_value=date(2026, 9, 1),
        amount=_money(3_000_000),
        note="Bonus",
    )
    data = _load(team)
    base = _row(data, base_id)
    one_off = _row(data, one_off_id)
    assert base.annual_payment == one_off.annual_payment
    assert base.monthly_payment == one_off.monthly_payment
    assert one_off.payoff_year is not None and base.payoff_year is not None
    assert one_off.payoff_year < base.payoff_year


def test_ignored_loan_keeps_its_payment_out_of_the_drop(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    data = _load(team, ignored_loan_ids=frozenset({loan_id}))
    row = _row(data, loan_id)
    assert row.state is fire_loans.LoanPlanState.IGNORED
    assert not row.included
    assert row.eligible
    assert data.included_annual_total == _money(0)
    assert data.ignored_annual_total == row.annual_payment


def test_preview_override_turns_ignored_loan_back_on(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    data = _load(
        team,
        ignored_loan_ids=frozenset({loan_id}),
        included_overrides={loan_id: True},
    )
    row = _row(data, loan_id)
    assert row.state is fire_loans.LoanPlanState.INCLUDED
    assert data.included_annual_total == row.annual_payment


def test_paid_off_loan_is_visible_and_excluded(team: WorkspaceDb) -> None:
    loan_id = _create(
        team,
        name="Small",
        principal_original=_money(100_000),
        term_months=1,
        first_payment_date=date(2026, 2, 1),
    )
    data = _load(team)
    row = _row(data, loan_id)
    assert row.state is fire_loans.LoanPlanState.PAID_OFF
    assert not row.eligible
    assert row.annual_payment is None
    assert row.payoff_year == 2026


def test_archived_loan_is_visible_and_excluded(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    loans.archive_loan(team.factory, team.ctx("owner", "owner"), loan_id)
    data = _load(team)
    row = _row(data, loan_id)
    assert row.state is fire_loans.LoanPlanState.ARCHIVED
    assert not row.eligible
    assert row.annual_payment is None


def _remove_anchor(team: WorkspaceDb, loan_id) -> None:
    from assetboard.db.repositories import LoanRepository

    with team.factory.begin() as session:
        loan = LoanRepository(session, team.ctx("owner", "owner")).get_or_raise(loan_id)
        loan.origination_date = None
        loan.principal_original_minor = None


def test_a_broken_schedule_is_a_reason_not_a_crash(team: WorkspaceDb) -> None:
    loan_id = _create(team, name="Broken")
    _remove_anchor(team, loan_id)
    data = _load(team)
    row = _row(data, loan_id)
    assert row.state is fire_loans.LoanPlanState.SCHEDULE_ERROR
    assert row.error_code is not None
    assert not row.eligible


def test_currency_mismatch_is_excluded_without_conversion(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    with team.factory.begin() as session:
        from assetboard.db.repositories import LoanRepository

        loan = LoanRepository(session, team.ctx("owner", "owner")).get_or_raise(loan_id)
        loan.currency = "USD"
    data = _load(team)
    row = _row(data, loan_id)
    assert row.state is fire_loans.LoanPlanState.CURRENCY_MISMATCH
    assert not row.eligible
    assert row.annual_payment is None
    assert data.included_annual_total == _money(0)


def test_installment_plan_is_listed_as_excluded(team: WorkspaceDb) -> None:
    from tests.tenant.factories import make_installment_plan

    with team.factory.begin() as session:
        plan = make_installment_plan(session, team.workspace_id)
        session.add(plan)
        loan_id = plan.loan_id
    data = _load(team)
    row = _row(data, loan_id)
    assert row.state is fire_loans.LoanPlanState.INSTALLMENT_PLAN
    assert not row.eligible
    assert row.monthly_payment == _money(10_000)
    assert row.payoff_year == 2026


def test_the_next_payment_is_the_recurring_row_not_a_twelve_row_sum(
    team: WorkspaceDb,
) -> None:
    loan_id = _create(team, overpayment_mode="fixed_extra", overpayment_amount=_money(50_000))
    data = _load(team)
    row = _row(data, loan_id)
    view = loans.get_loan(team.factory, team.ctx("owner", "owner"), loan_id)
    actual = loan_schedule.schedule_inputs_from_view(view, date(2026, 3, 1))
    schedule = loan_schedule.build_inputs_schedule(actual, overpayments=[])
    next_row = next(item for item in schedule.rows if item.date > date(2026, 3, 1))
    expected = (
        next_row.interest.amount_minor
        + next_row.principal.amount_minor
        + next_row.costs.amount_minor
        + next_row.overpayment.amount_minor
    ) * 12
    assert row.annual_payment is not None
    assert row.annual_payment.amount_minor == expected
    twelve_rows = sum(
        item.installment_total.amount_minor
        for item in schedule.rows
        if item.date > date(2026, 3, 1)
    )
    assert twelve_rows != row.annual_payment.amount_minor


def test_a_broken_schedule_never_becomes_a_zero(team: WorkspaceDb) -> None:
    loan_id = _create(team, name="Broken")
    _remove_anchor(team, loan_id)
    with pytest.raises((ScheduleError, loans.LoanError)):
        # The M7 path still rejects an anchorless loan; the FIRE resolver catches it above.
        view = loans.get_loan(team.factory, team.ctx("owner", "owner"), loan_id)
        loan_schedule.schedule_inputs_from_view(view, date(2026, 3, 1))
