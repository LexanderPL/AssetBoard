"""The F8.8 adapter between the saved one-plan model and the pure projection."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import event, func, select

from assetboard.db.models.fire import FireEvent, FireScenario
from assetboard.db.repositories import NotFound
from assetboard.domain.fire import Rule
from assetboard.domain.money import Money
from assetboard.services import fire_projection, fire_scenarios
from assetboard.services.context import PermissionDenied
from assetboard.services.fire_scenarios import EventInput, FireError, FirePlanInput
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
    assert statements <= 6
