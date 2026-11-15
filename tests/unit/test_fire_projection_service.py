"""The F8.8 adapter between the saved one-plan model and the pure projection."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import event, func, select

from assetboard.db.models.fire import FireEvent, FireScenario
from assetboard.db.repositories import NotFound
from assetboard.domain.fire import Rule
from assetboard.domain.money import Money
from assetboard.services import fire_projection, fire_scenarios, loans
from assetboard.services.context import PermissionDenied
from assetboard.services.fire_scenarios import (
    EventInput,
    FireError,
    FirePlanInput,
    LoanSettingInput,
)
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


def _input(**overrides: object) -> FirePlanInput:
    values: dict[str, object] = {
        "birth_year": 1985,
        "inflation_bp": 250,
        "real_return_bp": 500,
        "income_growth_bp": 300,
        "stop_working_age": 55,
        "horizon_age": 90,
        "rule": Rule.RETURNS_COVER_EXPENSES.value,
        "swr_bp": None,
        "income": _money(5_000_000),
        "expenses": _money(3_000_000),
        "portfolio": _money(100_000_000),
    }
    values.update(overrides)
    return FirePlanInput(**values)  # type: ignore[arg-type]


def _create(team: WorkspaceDb, **overrides: object) -> UUID:
    return fire_scenarios.create_fire_plan(
        team.factory, team.ctx("owner", "owner"), _input(**overrides)
    )


def _update(team: WorkspaceDb, plan_id: UUID, **overrides: object) -> UUID:
    return fire_scenarios.update_fire_plan(
        team.factory, team.ctx("owner", "owner"), _input(**overrides)
    )


def _create_loan(team: WorkspaceDb, **overrides: object) -> UUID:
    values: dict[str, Any] = {
        "entry_path": "original",
        "name": "Mortgage",
        "kind": "annuity",
        "principal_original": _money(5_000_000),
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
    return loans.create_loan(  # type: ignore[arg-type]
        team.factory, team.ctx("owner", "owner"), **values
    )


def test_results_map_the_plan_and_events_to_the_projection(team: WorkspaceDb) -> None:
    plan_id = _create(team)
    _update(
        team,
        plan_id,
        events=(
            EventInput(
                year=2027, kind="saving_override", amount_minor=1_200_000, label="Sabbatical"
            ),
            EventInput(
                year=2028,
                kind="expense_change",
                amount_minor=-500_000,
                label="Mortgage repaid",
            ),
        ),
    )
    results = fire_projection.scenario_results(team.factory, team.ctx("viewer", "viewer"), plan_id)
    assert results.blocked is None
    assert results.preview is False
    assert results.detail.plan.id == plan_id
    projection = results.projection
    assert projection is not None
    assert projection.inputs.currency == "EUR"
    assert projection.inputs.birth_year == 1985
    assert projection.inputs.start_year == 2026
    assert projection.inputs.horizon_age == 90
    assert projection.inputs.stop_working_age == 55
    assert projection.inputs.inflation_bp == 250
    assert projection.inputs.real_return_bp == 500
    assert projection.inputs.income_growth_bp == 300
    assert projection.inputs.start_income == _money(5_000_000)
    assert projection.inputs.start_expenses == _money(3_000_000)
    assert projection.inputs.start_portfolio == _money(100_000_000)
    assert [row.year for row in results.detail.active_events] == [2027, 2028]
    assert results.detail.archived_events == ()
    assert projection.rows[0].year == 2026


def test_results_map_the_withdrawal_rate_rule(team: WorkspaceDb) -> None:
    plan_id = _create(team, rule=Rule.SWR.value, swr_bp=400)
    results = fire_projection.scenario_results(team.factory, team.ctx("viewer", "viewer"), plan_id)
    assert results.projection is not None
    assert results.projection.inputs.rule is Rule.SWR
    assert results.projection.inputs.swr_bp == 400


def test_results_show_cash_only_when_a_cash_event_exists(team: WorkspaceDb) -> None:
    plan_id = _create(team)
    results = fire_projection.scenario_results(team.factory, team.ctx("viewer", "viewer"), plan_id)
    assert results.show_cash is False
    _update(
        team,
        plan_id,
        events=(EventInput(year=2027, kind="cash", amount_minor=100_000, label="Bonus"),),
    )
    results = fire_projection.scenario_results(team.factory, team.ctx("viewer", "viewer"), plan_id)
    assert results.show_cash is True


def test_past_events_are_archived_and_not_applied(team: WorkspaceDb) -> None:
    plan_id = _create(team)
    with team.factory.begin() as session:
        old = FireEvent(
            workspace_id=team.workspace_id,
            scenario_id=plan_id,
            year=2020,
            kind="cash",
            amount_minor=100_000,
            label="Old bonus",
        )
        session.add(old)
        session.flush()
        old_id = old.id
    _update(
        team,
        plan_id,
        events=(
            EventInput(
                id=old_id,
                year=2020,
                kind="cash",
                amount_minor=100_000,
                label="Old bonus",
            ),
            EventInput(year=2027, kind="cash", amount_minor=200_000, label="New bonus"),
        ),
    )
    results = fire_projection.scenario_results(team.factory, team.ctx("viewer", "viewer"), plan_id)
    assert [row.year for row in results.detail.active_events] == [2027]
    assert [row.year for row in results.detail.archived_events] == [2020]
    assert results.projection is not None
    assert results.projection.rows[0].cash == _money(0)


def test_a_preview_ignores_archived_events_like_the_saved_projection(team: WorkspaceDb) -> None:
    plan_id = _create(team)
    with team.factory.begin() as session:
        old = FireEvent(
            workspace_id=team.workspace_id,
            scenario_id=plan_id,
            year=2020,
            kind="cash",
            amount_minor=100_000,
            label="Old bonus",
        )
        override = FireEvent(
            workspace_id=team.workspace_id,
            scenario_id=plan_id,
            year=2026,
            kind="saving_override",
            amount_minor=200_000,
            label="Became current",
        )
        session.add_all((old, override))
        session.flush()
        old_id, override_id = old.id, override.id
    detail = fire_scenarios.get_fire_plan(team.factory, team.ctx("editor", "editor"))
    saved = fire_projection.results_from_detail(detail)
    assert saved.projection is not None
    assert [row.year for row in detail.archived_events] == [2020, 2026]
    preview = fire_projection.preview_plan_results(
        team.ctx("editor", "editor"),
        detail,
        _input(
            events=(
                EventInput(
                    id=old_id, year=2020, kind="cash", amount_minor=100_000, label="Old bonus"
                ),
                EventInput(
                    id=override_id,
                    year=2026,
                    kind="saving_override",
                    amount_minor=200_000,
                    label="Became current",
                ),
            )
        ),
    )
    assert preview.projection == saved.projection


def test_a_preview_includes_an_archived_event_that_the_new_horizon_covers(
    team: WorkspaceDb,
) -> None:
    plan_id = _create(team, horizon_age=80)
    with team.factory.begin() as session:
        session.add(
            FireEvent(
                workspace_id=team.workspace_id,
                scenario_id=plan_id,
                year=2070,
                kind="cash",
                amount_minor=100_000,
                label="Later bonus",
            )
        )
    detail = fire_scenarios.get_fire_plan(team.factory, team.ctx("editor", "editor"))
    assert [row.year for row in detail.archived_events] == [2070]
    preview = fire_projection.preview_plan_results(
        team.ctx("editor", "editor"), detail, _input(horizon_age=90)
    )
    assert preview.projection is not None
    cash = {row.year: row.cash for row in preview.projection.rows}
    assert cash[2070] == _money(100_000)
    fire_scenarios.update_fire_plan(
        team.factory, team.ctx("editor", "editor"), _input(horizon_age=90)
    )
    saved = fire_projection.results_from_detail(
        fire_scenarios.get_fire_plan(team.factory, team.ctx("editor", "editor"))
    )
    assert preview.projection == saved.projection


def test_a_preview_refuses_a_partial_post(team: WorkspaceDb) -> None:
    plan_id = _create(team)
    with team.factory.begin() as session:
        session.add(
            FireEvent(
                workspace_id=team.workspace_id,
                scenario_id=plan_id,
                year=2027,
                kind="cash",
                amount_minor=100_000,
                label="Bonus",
            )
        )
    detail = fire_scenarios.get_fire_plan(team.factory, team.ctx("editor", "editor"))
    with pytest.raises(FireError) as error:
        fire_projection.preview_plan_results(team.ctx("editor", "editor"), detail, _input())
    assert error.value.code == "invalid_event"


def test_a_preview_with_unchanged_inputs_equals_the_saved_projection(team: WorkspaceDb) -> None:
    _create(team)
    detail = fire_scenarios.get_fire_plan(team.factory, team.ctx("editor", "editor"))
    saved = fire_projection.results_from_detail(detail)
    preview = fire_projection.preview_plan_results(team.ctx("editor", "editor"), detail, _input())
    assert preview.preview is True
    assert preview.projection == saved.projection


def test_a_preview_writes_nothing(team: WorkspaceDb) -> None:
    plan_id = _create(team)
    detail = fire_scenarios.get_fire_plan(team.factory, team.ctx("editor", "editor"))
    with team.factory() as session:
        before = session.get_one(FireScenario, plan_id).updated_at
        count_before = session.scalar(select(func.count()).select_from(FireEvent))
    fire_projection.preview_plan_results(
        team.ctx("editor", "editor"),
        detail,
        _input(events=(EventInput(year=2027, kind="cash", amount_minor=1, label="Preview"),)),
    )
    with team.factory() as session:
        assert session.get_one(FireScenario, plan_id).updated_at == before
        assert session.scalar(select(func.count()).select_from(FireEvent)) == count_before


def test_a_preview_can_remove_a_row_without_writing(team: WorkspaceDb) -> None:
    plan_id = _create(team)
    with team.factory.begin() as session:
        event = FireEvent(
            workspace_id=team.workspace_id,
            scenario_id=plan_id,
            year=2026,
            kind="cash",
            amount_minor=100_000,
            label="Bonus",
        )
        session.add(event)
        session.flush()
        event_id = event.id
    detail = fire_scenarios.get_fire_plan(team.factory, team.ctx("editor", "editor"))
    preview = fire_projection.preview_plan_results(
        team.ctx("editor", "editor"),
        detail,
        _input(removed_event_ids=(event_id,)),
    )
    assert preview.projection is not None
    assert preview.projection.rows[0].cash == _money(0)
    with team.factory() as session:
        assert session.get_one(FireEvent, event_id).amount_minor == 100_000


def test_an_unusual_rate_warns_in_a_preview_and_blocks_a_save(team: WorkspaceDb) -> None:
    plan_id = _create(team)
    detail = fire_scenarios.get_fire_plan(team.factory, team.ctx("editor", "editor"))
    preview = fire_projection.preview_plan_results(
        team.ctx("editor", "editor"), detail, _input(real_return_bp=1_500)
    )
    assert preview.needs_extreme_confirmation is True
    with pytest.raises(FireError) as error:
        _update(team, plan_id, real_return_bp=1_500)
    assert error.value.code == "extreme_rate_confirmation_required"


def test_viewers_read_but_cannot_preview(team: WorkspaceDb) -> None:
    _create(team)
    detail = fire_scenarios.get_fire_plan(team.factory, team.ctx("viewer", "viewer"))
    with pytest.raises(PermissionDenied):
        fire_projection.preview_plan_results(team.ctx("viewer", "viewer"), detail, _input())


def test_another_workspace_cannot_read_the_plan(team: WorkspaceDb) -> None:
    other = team.add_workspace("Other")
    team.add_member("other-owner", "owner", workspace_id=other)
    ctx = team.ctx("other-owner", "owner", workspace_id=other)
    with pytest.raises(NotFound):
        fire_scenarios.get_fire_plan(team.factory, ctx)


def test_a_currency_mismatch_blocks_the_projection(team: WorkspaceDb) -> None:
    plan_id = _create(team)
    with team.factory.begin() as session:
        row = session.get_one(FireScenario, plan_id)
        row.currency = "USD"
    results = fire_projection.scenario_results(team.factory, team.ctx("viewer", "viewer"), plan_id)
    assert results.projection is None
    assert results.blocked is not None
    assert results.blocked.code == "plan_currency_mismatch"
    detail = fire_scenarios.get_fire_plan(team.factory, team.ctx("editor", "editor"))
    with pytest.raises(FireError) as error:
        fire_projection.preview_plan_results(team.ctx("editor", "editor"), detail, _input())
    assert error.value.code == "plan_currency_mismatch"


def test_an_invalid_saved_plan_is_a_block_not_a_server_error(team: WorkspaceDb) -> None:
    plan_id = _create(team)
    with team.factory.begin() as session:
        row = session.get_one(FireScenario, plan_id)
        row.horizon_age = 41
    results = fire_projection.scenario_results(team.factory, team.ctx("viewer", "viewer"), plan_id)
    assert results.projection is None
    assert results.blocked is not None
    assert results.blocked.code == "horizon_not_after_age"


def _statements(team: WorkspaceDb, operation: Callable[[], Any]) -> int:
    seen = 0

    def count(*_args: object) -> None:
        nonlocal seen
        seen += 1

    event.listen(team.engine, "before_cursor_execute", count)
    try:
        operation()
    finally:
        event.remove(team.engine, "before_cursor_execute", count)
    return seen


def test_the_results_service_uses_a_bounded_number_of_queries(team: WorkspaceDb) -> None:
    plan_id = _create(team)
    ctx = team.ctx("viewer", "viewer")
    statements = _statements(
        team, lambda: fire_projection.scenario_results(team.factory, ctx, plan_id)
    )
    assert statements <= 12


def _event_count(team: WorkspaceDb) -> int:
    with team.factory() as session:
        return session.scalar(select(func.count()).select_from(FireEvent)) or 0


def test_a_live_loan_is_in_expenses_and_drops_at_its_payoff_year(team: WorkspaceDb) -> None:
    plan_id = _create(team)
    loan_id = _create_loan(team)
    results = fire_projection.scenario_results(team.factory, team.ctx("viewer", "viewer"), plan_id)
    assert results.projection is not None
    assert results.loans is not None
    row = next(item for item in results.loans.rows if item.loan_id == loan_id)
    assert row.annual_payment is not None
    annual = row.annual_payment.amount_minor
    by_year = {item.year: item for item in results.projection.rows}
    assert by_year[2026].loan_payment.amount_minor == annual
    assert by_year[2027].loan_payment.amount_minor == annual
    assert by_year[2028].loan_payment.amount_minor == 0
    drop = by_year[2027].expenses.amount_minor - by_year[2028].expenses.amount_minor
    assert 0 < drop < annual
    assert drop > annual * 9 // 10


def test_ignoring_a_loan_keeps_its_payment_in_the_projection(team: WorkspaceDb) -> None:
    plan_id = _create(team)
    loan_id = _create_loan(team)
    _update(team, plan_id, loan_settings=(LoanSettingInput(loan_id=loan_id, included=False),))
    results = fire_projection.scenario_results(team.factory, team.ctx("viewer", "viewer"), plan_id)
    assert results.projection is not None
    assert results.loans is not None
    row = next(item for item in results.loans.rows if item.loan_id == loan_id)
    assert row.state.value == "ignored"
    assert row.annual_payment is not None
    annual = row.annual_payment.amount_minor
    assert all(item.loan_payment.amount_minor == annual for item in results.projection.rows)
    assert results.loans.included_annual_total.amount_minor == 0
    assert results.loans.ignored_annual_total.amount_minor == annual


def test_loan_settings_are_saved_cleared_and_do_not_touch_events(team: WorkspaceDb) -> None:
    plan_id = _create(team)
    loan_id = _create_loan(team)
    before = _event_count(team)
    _update(team, plan_id, loan_settings=(LoanSettingInput(loan_id=loan_id, included=False),))
    detail = fire_scenarios.get_fire_plan(team.factory, team.ctx("viewer", "viewer"))
    assert detail.ignored_loan_ids == frozenset({loan_id})
    assert _event_count(team) == before
    _update(team, plan_id, loan_settings=(LoanSettingInput(loan_id=loan_id, included=True),))
    detail = fire_scenarios.get_fire_plan(team.factory, team.ctx("viewer", "viewer"))
    assert detail.ignored_loan_ids == frozenset()
    assert _event_count(team) == before


def test_changing_a_loan_changes_the_next_projection_without_an_event(team: WorkspaceDb) -> None:
    plan_id = _create(team)
    loan_id = _create_loan(team)
    ctx = team.ctx("viewer", "viewer")
    before = fire_projection.scenario_results(team.factory, ctx, plan_id)
    assert before.loans is not None
    before_row = next(item for item in before.loans.rows if item.loan_id == loan_id)
    before_payoff = before_row.payoff_year
    events_before = _event_count(team)
    loans.add_overpayment(
        team.factory,
        team.ctx("owner", "owner"),
        loan_id,
        date_value=date(2026, 9, 1),
        amount=_money(1_500_000),
        note="Bonus",
    )
    after = fire_projection.scenario_results(team.factory, ctx, plan_id)
    assert after.loans is not None
    after_row = next(item for item in after.loans.rows if item.loan_id == loan_id)
    assert after_row.payoff_year is not None and before_payoff is not None
    assert after_row.payoff_year < before_payoff
    assert _event_count(team) == events_before


def test_preview_uses_posted_loan_toggles_and_never_writes(team: WorkspaceDb) -> None:
    _create(team)
    loan_id = _create_loan(team)
    detail = fire_scenarios.get_fire_plan(team.factory, team.ctx("editor", "editor"))
    before = _event_count(team)
    result = fire_projection.preview_plan_results(
        team.ctx("editor", "editor"),
        detail,
        _input(loan_settings=(LoanSettingInput(loan_id=loan_id, included=False),)),
        factory=team.factory,
    )
    assert result.loans is not None
    row = next(item for item in result.loans.rows if item.loan_id == loan_id)
    assert row.state.value == "ignored"
    assert _event_count(team) == before
    assert detail.ignored_loan_ids == frozenset()


def test_the_results_query_count_does_not_grow_with_loans(team: WorkspaceDb) -> None:
    plan_id = _create(team)
    _create_loan(team)
    ctx = team.ctx("viewer", "viewer")
    one = _statements(team, lambda: fire_projection.scenario_results(team.factory, ctx, plan_id))
    _create_loan(team, name="Second")
    _create_loan(team, name="Third")
    many = _statements(team, lambda: fire_projection.scenario_results(team.factory, ctx, plan_id))
    assert one == many
    assert many <= 16
