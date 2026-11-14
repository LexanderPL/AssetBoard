"""The F8.5 comparison service: order, blocks, currencies, linked inputs and query budget."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import ORMExecuteState, Session

from assetboard.db.models.fire import FireContribution, FireEvent, FireScenario
from assetboard.db.models.identity import AuditEvent
from assetboard.db.repositories import (
    CategoryRepository,
    CurrentWorkspaceRepository,
    FireEventRepository,
    NotFound,
    TransactionRepository,
)
from assetboard.domain.fire import Rule
from assetboard.domain.money import Money
from assetboard.services import fire_comparison, fire_projection, fire_scenarios
from assetboard.services.fire_scenarios import (
    ContributionInput,
    EventInput,
    FireScenarioInput,
)
from tests.tenant.workspace_db import WorkspaceDb, workspace_db


@pytest.fixture
def team(tmp_path):
    for db in workspace_db(tmp_path):
        db.add_member("owner", "owner")
        db.add_member("editor", "editor")
        db.add_member("viewer", "viewer")
        yield db


def _ctx(team: WorkspaceDb):
    return team.ctx("owner", "owner")


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
    return fire_scenarios.create_fire_scenario(team.factory, _ctx(team), _input(**overrides))


def _compare(team: WorkspaceDb, ids: tuple[UUID, ...], role: str = "viewer"):
    return fire_comparison.compare_fire_scenarios(team.factory, team.ctx(role, role), ids)


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


def _category(team: WorkspaceDb, name: str) -> UUID:
    with team.factory.begin() as session:
        row = CategoryRepository(session, _ctx(team)).create(
            name=name,
            name_key=name.casefold(),
            kind="expense",
            treatment="regular",
            color="#16a34a",
            sort_order=0,
        )
        return row.id


def _spend(team: WorkspaceDb, category_id: UUID, day: date, amount_minor: int) -> None:
    with team.factory.begin() as session:
        TransactionRepository(session, _ctx(team)).create(
            direction="expense",
            day=day,
            amount_minor=amount_minor,
            currency="EUR",
            category_id=category_id,
            description="Synthetic",
            description_key="synthetic",
            source="manual",
            created_by_user_id=None,
        )


def test_three_scenarios_compare_in_the_requested_order_with_matching_metrics(
    team: WorkspaceDb,
) -> None:
    first = _create(team, name="First", real_return_bp=400)
    second = _create(team, name="Second", real_return_bp=500)
    third = _create(team, name="Third", real_return_bp=600)
    comparison = _compare(team, (third, first, second))
    assert comparison.selected_ids == (third, first, second)
    assert comparison.error_code is None
    assert [row.scenario.id for row in comparison.rows] == [third, first, second]
    assert comparison.mixed_start_years is False
    for row in comparison.rows:
        saved = fire_projection.scenario_results(
            team.factory, team.ctx("viewer", "viewer"), row.scenario.id
        )
        assert saved.projection is not None
        assert row.projection == saved.projection
        assert row.fire_age == saved.projection.fire_age
        assert row.fire_year == saved.projection.fire_year
        assert row.years_to_fire == saved.projection.years_to_fire
        assert row.real_networth_at_fire == saved.projection.real_networth_at_fire
        assert row.depletion_age == saved.projection.depletion_age
        assert row.blocked is False
        assert row.never is False


def test_duplicate_ids_are_removed_while_the_first_position_is_kept(
    team: WorkspaceDb,
) -> None:
    first = _create(team, name="First")
    second = _create(team, name="Second")
    comparison = _compare(team, (second, first, second))
    assert comparison.selected_ids == (second, first)
    assert [row.scenario.id for row in comparison.rows] == [second, first]


def test_more_than_three_scenarios_is_an_error_and_loads_nothing(
    team: WorkspaceDb,
) -> None:
    ids = tuple(_create(team, name=f"Plan {index}") for index in range(4))
    statements = _statements(lambda: _compare(team, ids))
    comparison = _compare(team, ids)
    assert comparison.error_code == "too_many_scenarios"
    assert comparison.rows == ()
    assert comparison.selected_ids == ids
    assert statements == 0


def test_a_missing_or_cross_workspace_scenario_raises_not_found(
    team: WorkspaceDb,
) -> None:
    with pytest.raises(NotFound):
        _compare(team, (uuid4(),))
    other = team.add_workspace("Other")
    team.add_member("other-owner", "owner", workspace_id=other)
    foreign = fire_scenarios.create_fire_scenario(
        team.factory,
        team.ctx("other-owner", "owner", workspace_id=other),
        _input(name="Foreign"),
    )
    with pytest.raises(NotFound):
        _compare(team, (foreign,))


def test_mixed_currencies_are_a_page_error_without_rows(team: WorkspaceDb) -> None:
    eur = _create(team, name="Euro plan")
    with team.factory.begin() as session:
        CurrentWorkspaceRepository(session, _ctx(team)).update(base_currency="USD")
    usd = _create(
        team,
        name="Dollar plan",
        income_manual=_money(5_000_000, "USD"),
        expenses_manual=_money(3_000_000, "USD"),
        portfolio_manual=_money(100_000_000, "USD"),
    )
    comparison = _compare(team, (eur, usd))
    assert comparison.error_code == "currency_mismatch"
    assert comparison.rows == ()
    assert comparison.selected_ids == (eur, usd)


def test_mixed_start_years_project_and_are_flagged(team: WorkspaceDb) -> None:
    first = _create(team, name="First")
    second = _create(team, name="Second")
    with team.factory.begin() as session:
        row = session.get_one(FireScenario, second)
        row.start_year = 2025
        row.birth_year = 1984
    comparison = _compare(team, (first, second))
    assert comparison.error_code is None
    assert comparison.mixed_start_years is True
    assert {row.projection.inputs.start_year for row in comparison.rows if row.projection} == {
        2025,
        2026,
    }


def test_blocked_never_archived_and_viewer_rows(team: WorkspaceDb) -> None:
    blocked = _create(team, name="Blocked", income_mode="trailing_12m")
    with team.factory.begin() as session:
        row = session.get_one(FireScenario, blocked)
        row.income_manual_minor = None
    never = _create(
        team,
        name="Never",
        inflation_bp=0,
        real_return_bp=0,
        income_growth_bp=0,
        stop_working_age=41,
        income_manual=_money(0),
        expenses_manual=_money(3_000_000),
        portfolio_manual=_money(10_000_000),
    )
    archived = _create(team, name="Archived")
    fire_scenarios.archive_fire_scenario(team.factory, _ctx(team), archived)

    comparison = _compare(team, (blocked, never, archived), role="viewer")
    assert [row.scenario.id for row in comparison.rows] == [blocked, never, archived]
    blocked_row, never_row, archived_row = comparison.rows
    assert blocked_row.blocked is True
    assert blocked_row.projection is None
    assert blocked_row.blocks[0].code == "no_income_data"
    assert never_row.blocked is False
    assert never_row.never is True
    assert never_row.fire_age is None
    assert archived_row.scenario.archived is True
    assert archived_row.projection is not None
    assert archived_row.scenario.linked is False


def test_linked_inputs_and_exclusions_are_resolved_from_the_saved_plan(
    team: WorkspaceDb,
) -> None:
    counted = _category(team, "Food")
    excluded = _category(team, "Unusual")
    _spend(team, counted, date(2026, 2, 10), 1_000_000)
    _spend(team, excluded, date(2026, 2, 11), 500_000)
    scenario_id = _create(
        team,
        name="Linked expenses",
        expenses_mode="trailing_12m",
        expenses_manual=None,
        expense_exclusion_ids=(excluded,),
    )
    comparison = _compare(team, (scenario_id,))
    assert comparison.rows[0].projection is not None
    # One month with data, annualized x12: 1,000.00 -> 12,000.00.
    assert comparison.rows[0].projection.inputs.start_expenses == _money(12_000_000)
    saved = fire_projection.scenario_results(
        team.factory, team.ctx("viewer", "viewer"), scenario_id
    )
    assert comparison.rows[0].projection == saved.projection


def test_a_comparison_writes_nothing(team: WorkspaceDb) -> None:
    ids = (_create(team, name="First"), _create(team, name="Second"))

    def snapshot() -> tuple[tuple[datetime, ...], tuple[UUID, ...], tuple[UUID, ...], int]:
        with team.factory() as session:
            rows = [session.get_one(FireScenario, scenario_id) for scenario_id in ids]
            contributions = tuple(
                session.scalars(
                    select(FireContribution.id)
                    .where(FireContribution.scenario_id.in_(ids))
                    .order_by(FireContribution.id)
                )
            )
            events = tuple(
                session.scalars(
                    select(FireEvent.id)
                    .where(FireEvent.scenario_id.in_(ids))
                    .order_by(FireEvent.id)
                )
            )
            audits = session.scalar(select(func.count()).select_from(AuditEvent))
            assert audits is not None
            return (
                tuple(row.updated_at for row in rows),
                contributions,
                events,
                audits,
            )

    before = snapshot()
    _compare(team, ids)
    assert snapshot() == before


def test_the_query_count_does_not_grow_with_child_rows(team: WorkspaceDb) -> None:
    plain = _create(team, name="Plain")
    many = _create(
        team,
        name="Many rows",
        contributions=tuple(
            ContributionInput(year=2027 + index, amount_minor=100_000) for index in range(10)
        ),
        events=tuple(
            EventInput(
                year=2027 + index,
                kind="cash",
                amount_minor=50_000,
                label=f"Cash {index}",
            )
            for index in range(10)
        ),
    )
    other = _create(team, name="Other")
    ids = (plain, many, other)
    count = _statements(lambda: _compare(team, ids))
    with team.factory.begin() as session:
        for index in range(10, 30):
            FireEventRepository(session, _ctx(team)).create(
                scenario_id=many,
                year=2027 + index,
                kind="cash",
                amount_minor=1,
                label=f"Extra {index}",
            )
    louder = _statements(lambda: _compare(team, ids))
    assert count == louder == 4
