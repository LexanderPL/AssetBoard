"""The F8.3 adapter between saved FIRE scenarios and the pure projection."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import ORMExecuteState, Session

from assetboard.db.models.fire import FireContribution, FireEvent, FireScenario
from assetboard.db.models.identity import AuditEvent
from assetboard.db.repositories import NotFound
from assetboard.domain.fire import Rule
from assetboard.domain.money import Money
from assetboard.services import fire_projection, fire_scenarios
from assetboard.services.context import PermissionDenied
from assetboard.services.fire_scenarios import (
    ContributionInput,
    EventInput,
    FireError,
    FireScenarioInput,
    validate_scenario_input,
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


def _input(**overrides: object) -> FireScenarioInput:
    values: dict[str, object] = {
        "name": "Retire at 55",
        "birth_year": 1985,
        "inflation_bp": 250,
        "real_return_bp": 500,
        "income_growth_bp": 300,
        "stop_working_age": 55,
        "horizon_age": 90,
        "rule": Rule.RETURNS_COVER_EXPENSES.value,
        "swr_bp": None,
        "income_mode": "manual",
        "income_manual": _money(5_000_000),
        "expenses_mode": "manual",
        "expenses_manual": _money(3_000_000),
        "portfolio_mode": "manual",
        "portfolio_manual": _money(100_000_000),
    }
    values.update(overrides)
    return FireScenarioInput(**values)  # type: ignore[arg-type]


def _create(team: WorkspaceDb, **overrides: object) -> UUID:
    return fire_scenarios.create_fire_scenario(
        team.factory, team.ctx("owner", "owner"), _input(**overrides)
    )


def test_results_map_the_scenario_children_and_metrics(team: WorkspaceDb) -> None:
    scenario_id = _create(
        team,
        contributions=(ContributionInput(year=2027, amount_minor=1_200_000),),
        events=(
            EventInput(
                year=2028,
                kind="expense_change",
                amount_minor=-500_000,
                label="Mortgage repaid",
            ),
        ),
    )
    results = fire_projection.scenario_results(
        team.factory, team.ctx("viewer", "viewer"), scenario_id
    )
    assert results.blocked is None
    assert results.preview is False
    assert results.detail.scenario.id == scenario_id
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
    assert projection.inputs.rule is Rule.RETURNS_COVER_EXPENSES
    assert projection.inputs.swr_bp is None
    assert projection.inputs.start_income == _money(5_000_000)
    assert projection.inputs.start_expenses == _money(3_000_000)
    assert projection.inputs.start_portfolio == _money(100_000_000)
    assert [(row.year, row.amount) for row in projection.inputs.contributions] == [
        (2027, _money(1_200_000))
    ]
    assert [(row.year, row.kind.value, row.amount) for row in projection.inputs.events] == [
        (2028, "expense_change", _money(-500_000))
    ]
    assert projection.fire_age is not None
    assert results.show_cash is False


def test_results_map_the_withdrawal_rate_rule(team: WorkspaceDb) -> None:
    scenario_id = _create(team, rule=Rule.SWR.value, swr_bp=400)
    results = fire_projection.scenario_results(
        team.factory, team.ctx("viewer", "viewer"), scenario_id
    )
    assert results.projection is not None
    assert results.projection.inputs.rule is Rule.SWR
    assert results.projection.inputs.swr_bp == 400


def test_results_show_cash_only_when_a_cash_event_exists(team: WorkspaceDb) -> None:
    scenario_id = _create(
        team,
        events=(EventInput(year=2028, kind="cash", amount_minor=250_000, label="Bonus"),),
    )
    results = fire_projection.scenario_results(
        team.factory, team.ctx("viewer", "viewer"), scenario_id
    )
    assert results.show_cash is True


def test_a_preview_ignores_another_scenarios_name(team: WorkspaceDb) -> None:
    first = _create(team, name="First")
    _create(team, name="Second")
    detail = fire_scenarios.get_fire_scenario(team.factory, team.ctx("editor", "editor"), first)
    preview = fire_projection.preview_scenario_results(
        team.ctx("editor", "editor"), detail, _input(name="Second")
    )
    assert preview.projection is not None


def test_a_preview_with_unchanged_inputs_equals_the_saved_projection(team: WorkspaceDb) -> None:
    scenario_id = _create(
        team,
        contributions=(ContributionInput(year=2027, amount_minor=1_200_000),),
        events=(EventInput(year=2028, kind="cash", amount_minor=250_000, label="Bonus"),),
    )
    saved = fire_projection.scenario_results(
        team.factory, team.ctx("editor", "editor"), scenario_id
    )
    preview = fire_projection.preview_scenario_results(
        team.ctx("editor", "editor"),
        saved.detail,
        _input(
            contributions=(ContributionInput(year=2027, amount_minor=1_200_000),),
            events=(EventInput(year=2028, kind="cash", amount_minor=250_000, label="Bonus"),),
        ),
    )
    assert preview.preview is True
    assert preview.blocked is None
    assert preview.projection == saved.projection


def test_a_preview_writes_nothing(team: WorkspaceDb) -> None:
    scenario_id = _create(team)
    detail = fire_scenarios.get_fire_scenario(
        team.factory, team.ctx("editor", "editor"), scenario_id
    )

    def snapshot() -> tuple[datetime, datetime, int, int, int]:
        with team.factory() as session:
            row = session.get_one(FireScenario, scenario_id)
            audits = session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action.like("fire_scenario.%"))
            )
            contributions = session.scalar(
                select(func.count())
                .select_from(FireContribution)
                .where(FireContribution.scenario_id == scenario_id)
            )
            events = session.scalar(
                select(func.count())
                .select_from(FireEvent)
                .where(FireEvent.scenario_id == scenario_id)
            )
            assert audits is not None and contributions is not None and events is not None
            return (row.created_at, row.updated_at, audits, contributions, events)

    before = snapshot()
    preview = fire_projection.preview_scenario_results(
        team.ctx("editor", "editor"), detail, _input(name="Changed only in the preview")
    )
    assert preview.projection is not None
    assert snapshot() == before


def test_an_unusual_rate_warns_in_a_preview_and_blocks_a_save(team: WorkspaceDb) -> None:
    scenario_id = _create(team)
    detail = fire_scenarios.get_fire_scenario(
        team.factory, team.ctx("editor", "editor"), scenario_id
    )
    unusual = _input(inflation_bp=1_200, extreme_rates_confirmed=False)
    preview = fire_projection.preview_scenario_results(
        team.ctx("editor", "editor"), detail, unusual
    )
    assert preview.projection is not None
    assert preview.needs_extreme_confirmation is True
    with pytest.raises(FireError) as exc:
        validate_scenario_input(
            unusual,
            start_year=detail.scenario.start_year,
            currency=detail.scenario.currency,
            default_horizon=detail.scenario.horizon_age,
            previous_rates=(
                detail.scenario.inflation_bp,
                detail.scenario.real_return_bp,
                detail.scenario.income_growth_bp,
                detail.scenario.swr_bp,
            ),
        )
    assert (exc.value.field, exc.value.code) == (
        "extreme_rates_confirmed",
        "extreme_rate_confirmation_required",
    )
    confirmed = fire_projection.preview_scenario_results(
        team.ctx("editor", "editor"),
        detail,
        _input(inflation_bp=1_200, extreme_rates_confirmed=True),
    )
    assert confirmed.projection is not None
    assert confirmed.needs_extreme_confirmation is False


def test_viewers_read_and_editors_preview_but_archived_plans_never_preview(
    team: WorkspaceDb,
) -> None:
    scenario_id = _create(team)
    detail = fire_scenarios.get_fire_scenario(team.factory, team.ctx("owner", "owner"), scenario_id)
    assert (
        fire_projection.preview_scenario_results(
            team.ctx("owner", "owner"), detail, _input()
        ).projection
        is not None
    )
    with pytest.raises(PermissionDenied):
        fire_projection.preview_scenario_results(team.ctx("viewer", "viewer"), detail, _input())
    fire_scenarios.archive_fire_scenario(team.factory, team.ctx("owner", "owner"), scenario_id)
    archived = fire_scenarios.get_fire_scenario(
        team.factory, team.ctx("owner", "owner"), scenario_id
    )
    with pytest.raises(FireError) as exc:
        fire_projection.preview_scenario_results(team.ctx("owner", "owner"), archived, _input())
    assert (exc.value.field, exc.value.code) == ("scenario", "archived")


def test_another_workspace_cannot_read_the_scenario(team: WorkspaceDb) -> None:
    scenario_id = _create(team)
    other = team.add_workspace("Other")
    team.add_member("viewer", "viewer", workspace_id=other)
    with pytest.raises(NotFound):
        fire_projection.scenario_results(
            team.factory, team.ctx("viewer", "viewer", workspace_id=other), scenario_id
        )


def test_a_linked_starting_value_blocks_instead_of_becoming_zero(team: WorkspaceDb) -> None:
    scenario_id = _create(team)
    with team.factory() as session, session.begin():
        row = session.get_one(FireScenario, scenario_id)
        row.income_mode = "trailing_12m"
        row.income_manual_minor = None
    results = fire_projection.scenario_results(
        team.factory, team.ctx("viewer", "viewer"), scenario_id
    )
    assert results.projection is None
    assert results.blocked is not None
    assert (results.blocked.code, results.blocked.field) == (
        "no_income_data",
        "income_manual",
    )


def test_an_invalid_saved_plan_is_a_block_not_a_server_error(team: WorkspaceDb) -> None:
    scenario_id = _create(team)
    with team.factory() as session, session.begin():
        row = session.get_one(FireScenario, scenario_id)
        row.birth_year = 2_190
        row.start_year = 2_199
        row.horizon_age = 100
        row.stop_working_age = 100
    results = fire_projection.scenario_results(
        team.factory, team.ctx("viewer", "viewer"), scenario_id
    )
    assert results.projection is None
    assert results.blocked is not None
    assert (results.blocked.code, results.blocked.field) == ("invalid_projection", "horizon_age")


def _statements(operation: Callable[[], Any]) -> int:
    seen = 0

    def record(_state: ORMExecuteState) -> None:
        nonlocal seen
        seen += 1

    event.listen(Session, "do_orm_execute", record)
    try:
        operation()
    finally:
        event.remove(Session, "do_orm_execute", record)
    return seen


def test_the_results_service_uses_a_bounded_number_of_queries(team: WorkspaceDb) -> None:
    scenario_id = _create(team)
    count = _statements(
        lambda: fire_projection.scenario_results(
            team.factory, team.ctx("viewer", "viewer"), scenario_id
        )
    )
    # Detail (scenario, contributions, events, exclusions) plus the loan-suggestion load.
    assert count == 5
