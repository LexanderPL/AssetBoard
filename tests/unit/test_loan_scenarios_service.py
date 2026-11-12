from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from assetboard.db.models.identity import AuditEvent
from assetboard.db.repositories import LoanRepository, NotFound
from assetboard.domain.loans import (
    OverpaymentMode,
    OverpaymentStrategy,
)
from assetboard.domain.money import Money
from assetboard.services import loan_scenarios, loans
from assetboard.services.archive import _has_loan_scenarios, workspace_is_empty
from assetboard.services.context import PermissionDenied
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


def _params(**overrides) -> loan_scenarios.ScenarioParameters:
    values: dict[str, object] = {
        "name": None,
        "overpayment_mode": None,
        "overpayment_amount": None,
        "strategy": None,
        "rate_shift_bp": None,
        "rate_shift_from": None,
        "one_off_date": None,
        "one_off_amount": None,
    }
    values.update(overrides)
    return loan_scenarios.ScenarioParameters(**values)  # type: ignore[arg-type]


def test_comparison_has_baseline_current_and_ephemeral_rows(team: WorkspaceDb) -> None:
    loan_id = _create(
        team,
        overpayment_mode="fixed_extra",
        overpayment_amount=_money(100_000),
        overpayment_strategy="reduce_term",
    )
    comparison = loan_scenarios.compare_loan_scenarios(
        team.factory,
        team.ctx("owner", "owner"),
        loan_id,
        [
            _params(
                one_off_date=date(2026, 6, 1),
                one_off_amount=_money(500_000),
                strategy=OverpaymentStrategy.REDUCE_TERM,
            )
        ],
    )
    assert comparison.error_code is None
    assert comparison.baseline is not None and comparison.current is not None
    assert comparison.baseline.label == "Baseline"
    assert comparison.current.label == "Current plan"
    assert [row.label for row in comparison.ephemeral] == ["What-if 1"]
    assert len(comparison.rows) == 3
    assert comparison.baseline.total_interest is not None
    assert comparison.current.total_interest is not None
    what_if = comparison.ephemeral[0]
    assert what_if.total_interest is not None and what_if.total_paid is not None
    assert what_if.schedule is not None
    assert comparison.baseline.schedule is not None
    assert what_if.months_saved is not None and what_if.months_saved > 0
    assert what_if.interest_saved == Money(
        comparison.baseline.total_interest.amount_minor - what_if.total_interest.amount_minor,
        "EUR",
    )
    assert what_if.interest_saved is not None and what_if.interest_saved.amount_minor > 0
    assert (
        comparison.current.total_interest.amount_minor
        < comparison.baseline.total_interest.amount_minor
    )


def test_identical_parameters_produce_identical_results(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    parameters = _params(
        overpayment_mode=OverpaymentMode.FIXED_EXTRA,
        overpayment_amount=_money(75_000),
        strategy=OverpaymentStrategy.REDUCE_INSTALLMENT,
        rate_shift_bp=100,
        rate_shift_from=date(2026, 7, 1),
        one_off_date=date(2026, 9, 1),
        one_off_amount=_money(250_000),
    )
    first = loan_scenarios.compare_loan_scenarios(
        team.factory, team.ctx("owner", "owner"), loan_id, [parameters]
    )
    second = loan_scenarios.compare_loan_scenarios(
        team.factory, team.ctx("owner", "owner"), loan_id, [parameters]
    )
    assert first.ephemeral[0].schedule is not None
    assert second.ephemeral[0].schedule is not None
    assert first.ephemeral[0].schedule == second.ephemeral[0].schedule
    assert first.ephemeral[0].total_interest == second.ephemeral[0].total_interest
    assert first.ephemeral[0].payoff_date == second.ephemeral[0].payoff_date


def test_overpayment_strategy_lump_sum_and_rate_shift_are_applied(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    comparison = loan_scenarios.compare_loan_scenarios(
        team.factory,
        team.ctx("owner", "owner"),
        loan_id,
        [
            _params(
                overpayment_mode=OverpaymentMode.FIXED_EXTRA,
                overpayment_amount=_money(80_000),
                strategy=OverpaymentStrategy.REDUCE_INSTALLMENT,
                rate_shift_bp=150,
                rate_shift_from=date(2026, 7, 1),
                one_off_date=date(2026, 10, 1),
                one_off_amount=_money(300_000),
            )
        ],
    )
    row = comparison.ephemeral[0]
    assert row.error_code is None and row.schedule is not None
    assert row.parameters is not None
    assert row.parameters.overpayment_mode is OverpaymentMode.FIXED_EXTRA
    # The first row before the shift keeps 5.5%; the shift is effective from July.
    rows = row.schedule.rows
    assert rows[4].annual_rate_bp == 550
    assert rows[5].annual_rate_bp == 700
    assert any(item.overpayment.amount_minor for item in rows)
    # The lump sum is on top of the fixed extra, not a replacement.
    assert row.total_paid is not None and comparison.current is not None
    assert comparison.current.schedule is not None
    assert row.schedule.rows != comparison.current.schedule.rows


def test_a_scenario_error_becomes_a_row_error(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    comparison = loan_scenarios.compare_loan_scenarios(
        team.factory,
        team.ctx("owner", "owner"),
        loan_id,
        [
            _params(rate_shift_bp=-600, rate_shift_from=date(2026, 7, 1)),
            _params(rate_shift_bp=500, rate_shift_from=date(2026, 7, 1)),
        ],
    )
    assert comparison.error_code is None
    first, second = comparison.ephemeral
    assert first.error_code == "invalid_rate_shift" and first.schedule is None
    assert second.error_code is None and second.schedule is not None
    assert first.months_saved is None and first.interest_saved is None


def test_a_broken_loan_is_a_page_level_error(team: WorkspaceDb) -> None:
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
    comparison = loan_scenarios.compare_loan_scenarios(team.factory, ctx, loan_id, [])
    assert comparison.error_code == "missing_rate"
    assert comparison.rows == ()
    assert comparison.baseline is None and comparison.current is None


def test_saved_scenarios_upsert_list_and_delete_with_audit(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    editor = team.ctx("editor", "editor")
    scenario_id = loan_scenarios.create_or_update_loan_scenario(
        team.factory,
        editor,
        loan_id,
        _params(
            name="  Overpay  ",
            overpayment_mode=OverpaymentMode.FIXED_EXTRA,
            overpayment_amount=_money(50_000),
        ),
    )
    saved = loan_scenarios.list_loan_scenarios(team.factory, team.ctx("viewer", "viewer"), loan_id)
    assert len(saved) == 1
    assert saved[0].id == scenario_id
    assert saved[0].name == "Overpay"
    assert saved[0].parameters.overpayment_amount == _money(50_000)

    same = loan_scenarios.create_or_update_loan_scenario(
        team.factory,
        editor,
        loan_id,
        _params(
            name="overpay",
            overpayment_mode=OverpaymentMode.FIXED_EXTRA,
            overpayment_amount=_money(60_000),
            one_off_date=date(2026, 8, 1),
            one_off_amount=_money(10_000),
        ),
    )
    assert same == scenario_id
    saved = loan_scenarios.list_loan_scenarios(team.factory, editor, loan_id)
    assert len(saved) == 1
    assert saved[0].parameters.overpayment_amount == _money(60_000)
    assert saved[0].parameters.one_off_amount == _money(10_000)

    comparison = loan_scenarios.compare_loan_scenarios(team.factory, editor, loan_id, [])
    assert [row.name for row in comparison.saved] == ["overpay"]
    assert comparison.saved[0].scenario_id == scenario_id

    with team.factory() as session:
        actions = list(session.scalars(select(AuditEvent.action).order_by(AuditEvent.ts)))
    assert "loan_scenario.created" in actions
    assert "loan_scenario.updated" in actions
    with team.factory() as session:
        data = list(session.scalars(select(AuditEvent.data)))
    assert all("amount" not in str(item) for item in data)

    loan_scenarios.delete_loan_scenario(team.factory, editor, loan_id, scenario_id)
    assert loan_scenarios.list_loan_scenarios(team.factory, editor, loan_id) == ()
    with team.factory() as session:
        assert "loan_scenario.deleted" in set(session.scalars(select(AuditEvent.action)))


def test_viewers_read_but_cannot_write(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    viewer = team.ctx("viewer", "viewer")
    assert loan_scenarios.list_loan_scenarios(team.factory, viewer, loan_id) == ()
    assert (
        loan_scenarios.compare_loan_scenarios(team.factory, viewer, loan_id, []).error_code is None
    )
    with pytest.raises(PermissionDenied):
        loan_scenarios.create_or_update_loan_scenario(
            team.factory, viewer, loan_id, _params(name="Mine")
        )
    editor = team.ctx("editor", "editor")
    scenario_id = loan_scenarios.create_or_update_loan_scenario(
        team.factory, editor, loan_id, _params(name="Ours")
    )
    with pytest.raises(PermissionDenied):
        loan_scenarios.delete_loan_scenario(team.factory, viewer, loan_id, scenario_id)


def test_too_many_ephemeral_scenarios_are_refused(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    with pytest.raises(loans.LoanError) as raised:
        loan_scenarios.compare_loan_scenarios(
            team.factory,
            team.ctx("owner", "owner"),
            loan_id,
            [_params() for _ in range(4)],
        )
    assert raised.value.code == "too_many_scenarios"


def test_a_scenario_from_another_workspace_is_not_visible(team: WorkspaceDb) -> None:
    other_workspace = team.add_workspace("Other")
    team.add_member("other", "owner", other_workspace)
    other_ctx = team.ctx("other", "owner", other_workspace)
    other_loan = _create(team, name="Other mortgage")
    # The owner's workspace has a loan; the other workspace context cannot see it.
    with pytest.raises(NotFound):
        loan_scenarios.list_loan_scenarios(team.factory, other_ctx, other_loan)
    with pytest.raises(NotFound):
        loan_scenarios.compare_loan_scenarios(team.factory, other_ctx, other_loan, [])


def test_the_archive_emptiness_check_sees_scenarios(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    loan_scenarios.create_or_update_loan_scenario(
        team.factory,
        team.ctx("owner", "owner"),
        loan_id,
        _params(name="Saved"),
    )
    with team.factory() as session:
        assert _has_loan_scenarios(session, team.ctx("owner", "owner"))
        assert not workspace_is_empty(session, team.ctx("owner", "owner"))


def test_parameters_from_a_saved_model_keep_the_loan_currency(team: WorkspaceDb) -> None:
    loan_id = _create(team)
    loan_scenarios.create_or_update_loan_scenario(
        team.factory,
        team.ctx("owner", "owner"),
        loan_id,
        _params(
            name="Saved",
            overpayment_mode=OverpaymentMode.TARGET_TOTAL,
            overpayment_amount=_money(120_000),
        ),
    )
    saved = loan_scenarios.list_loan_scenarios(team.factory, team.ctx("owner", "owner"), loan_id)[0]
    assert saved.parameters.overpayment_amount == _money(120_000)
    assert saved.parameters.overpayment_mode is OverpaymentMode.TARGET_TOTAL
