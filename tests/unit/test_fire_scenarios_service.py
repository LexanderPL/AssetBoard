"""The one-plan FIRE service: create/update/delete, chooser and legacy migration (F8.8)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from assetboard.db.models.fire import FireEvent, FireScenario
from assetboard.db.models.identity import AuditEvent
from assetboard.db.repositories import (
    CategoryRepository,
    FireEventRepository,
    FireScenarioRepository,
    NotFound,
    TransactionRepository,
)
from assetboard.domain.fire import Rule
from assetboard.domain.money import Money
from assetboard.services import fire_scenarios
from assetboard.services.context import PermissionDenied
from assetboard.services.fire_scenarios import (
    EventInput,
    FireError,
    FirePlanInput,
    plan_entry,
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


def _legacy(
    team: WorkspaceDb,
    *,
    name: str,
    start_year: int = 2026,
    is_default: bool = False,
    archived_at: str | None = None,
    income_mode: str = "manual",
    expenses_mode: str = "manual",
    portfolio_mode: str = "manual",
    income_manual_minor: int | None = 5_000_000,
    expenses_manual_minor: int | None = 3_000_000,
    portfolio_manual_minor: int | None = 100_000_000,
    exclusion_ids: list[str] | None = None,
) -> UUID:
    payload = {
        "v": 1,
        "name": name,
        "name_key": name.casefold(),
        "start_year": start_year,
        "is_default": is_default,
        "archived_at": archived_at,
        "income_mode": income_mode,
        "expenses_mode": expenses_mode,
        "portfolio_mode": portfolio_mode,
        "income_manual_minor": income_manual_minor,
        "expenses_manual_minor": expenses_manual_minor,
        "portfolio_manual_minor": portfolio_manual_minor,
        "expense_exclusion_ids": exclusion_ids or [],
    }
    with team.factory.begin() as session:
        row = FireScenarioRepository(session, team.ctx("owner", "owner")).create(
            currency="EUR",
            birth_year=1985,
            inflation_bp=250,
            real_return_bp=500,
            income_growth_bp=300,
            stop_working_age=55,
            horizon_age=90,
            rule="returns_cover_expenses",
            swr_bp=None,
            income_minor=income_manual_minor,
            expenses_minor=expenses_manual_minor,
            portfolio_minor=portfolio_manual_minor,
            needs_review=True,
            is_live=False,
            legacy=json.dumps(payload, sort_keys=True),
        )
        return row.id


def _add_event(team: WorkspaceDb, plan_id: UUID, year: int, amount_minor: int) -> UUID:
    with team.factory.begin() as session:
        row = FireEventRepository(session, team.ctx("owner", "owner")).create(
            scenario_id=plan_id,
            year=year,
            kind="cash",
            amount_minor=amount_minor,
            label="Event",
        )
        return row.id


def test_create_stores_the_current_year_currency_and_amounts(team: WorkspaceDb) -> None:
    plan_id = _create(team)
    with team.factory() as session:
        row = session.get_one(FireScenario, plan_id)
        assert row.is_live is True
        assert row.legacy is None
        assert row.needs_review is False
        assert row.currency == "EUR"
        assert row.income_minor == 5_000_000
        assert row.expenses_minor == 3_000_000
        assert row.portfolio_minor == 100_000_000
        assert row.import_key is None
    detail = fire_scenarios.get_fire_plan(team.factory, team.ctx("viewer", "viewer"))
    assert detail.plan.current_year == 2026
    assert detail.plan.age_at_start == 41
    assert detail.plan.currency == "EUR"
    assert detail.plan.currency_mismatch is False


def test_a_second_plan_is_refused(team: WorkspaceDb) -> None:
    _create(team)
    with pytest.raises(FireError) as error:
        _create(team)
    assert error.value.code == "plan_exists"


def test_a_plan_cannot_be_created_while_legacy_rows_wait(team: WorkspaceDb) -> None:
    _legacy(team, name="Old plan")
    with pytest.raises(FireError) as error:
        _create(team)
    assert error.value.code == "migration_required"


def test_the_database_refuses_a_second_live_plan(team: WorkspaceDb) -> None:
    _create(team)
    with pytest.raises(IntegrityError), team.factory.begin() as session:
        FireScenarioRepository(session, team.ctx("owner", "owner")).create(
            currency="EUR",
            birth_year=1985,
            inflation_bp=250,
            real_return_bp=500,
            income_growth_bp=300,
            stop_working_age=55,
            horizon_age=90,
            rule="returns_cover_expenses",
            swr_bp=None,
            income_minor=1,
            expenses_minor=1,
            portfolio_minor=1,
            needs_review=False,
            is_live=True,
            legacy=None,
        )


def test_update_clears_needs_review_and_reconciles_events(team: WorkspaceDb) -> None:
    plan_id = _create(team)
    with team.factory.begin() as session:
        session.get_one(FireScenario, plan_id).needs_review = True
    first = _add_event(team, plan_id, 2027, 100_000)
    second = _add_event(team, plan_id, 2028, 200_000)
    fire_scenarios.update_fire_plan(
        team.factory,
        team.ctx("editor", "editor"),
        _input(
            events=(
                EventInput(
                    id=first,
                    year=2027,
                    kind="cash",
                    amount_minor=150_000,
                    label="Updated",
                ),
            )
        ),
    )
    with team.factory() as session:
        rows = FireEventRepository(session, team.ctx("viewer", "viewer")).for_plan(plan_id)
        assert [(row.id, row.amount_minor) for row in rows] == [(first, 150_000)]
        assert session.get_one(FireScenario, plan_id).needs_review is False
    assert second


def test_event_ids_from_another_plan_are_refused(team: WorkspaceDb) -> None:
    plan_id = _create(team)
    other_event = uuid4()
    with team.factory.begin() as session:
        FireScenarioRepository(session, team.ctx("owner", "owner")).create(
            currency="EUR",
            birth_year=1985,
            inflation_bp=250,
            real_return_bp=500,
            income_growth_bp=300,
            stop_working_age=55,
            horizon_age=90,
            rule="returns_cover_expenses",
            swr_bp=None,
            income_minor=1,
            expenses_minor=1,
            portfolio_minor=1,
            needs_review=True,
            is_live=False,
            legacy='{"v":1,"name":"Old","name_key":"old","start_year":2026,"is_default":false,"archived_at":null,"income_mode":"manual","expenses_mode":"manual","portfolio_mode":"manual","income_manual_minor":1,"expenses_manual_minor":1,"portfolio_manual_minor":1,"expense_exclusion_ids":[]}',
        )
    with pytest.raises(FireError) as error:
        fire_scenarios.update_fire_plan(
            team.factory,
            team.ctx("editor", "editor"),
            _input(
                events=(
                    EventInput(
                        id=other_event,
                        year=2027,
                        kind="cash",
                        amount_minor=1,
                        label="Bad",
                    ),
                )
            ),
        )
    assert error.value.code == "invalid_event"
    assert plan_id


def test_saving_override_is_unique_and_cannot_start_in_the_current_year(
    team: WorkspaceDb,
) -> None:
    _create(team)
    with pytest.raises(FireError) as error:
        fire_scenarios.update_fire_plan(
            team.factory,
            team.ctx("editor", "editor"),
            _input(
                events=(EventInput(year=2026, kind="saving_override", amount_minor=1, label="Now"),)
            ),
        )
    assert error.value.code == "saving_override_current_year"
    with pytest.raises(FireError) as duplicate:
        fire_scenarios.update_fire_plan(
            team.factory,
            team.ctx("editor", "editor"),
            _input(
                events=(
                    EventInput(year=2027, kind="saving_override", amount_minor=1, label="A"),
                    EventInput(year=2027, kind="saving_override", amount_minor=2, label="B"),
                )
            ),
        )
    assert duplicate.value.code == "duplicate_saving_override"


def test_a_saving_override_that_becomes_current_is_allowed_on_a_later_save(
    team: WorkspaceDb,
) -> None:
    plan_id = _create(team)
    override_id = uuid4()
    with team.factory.begin() as session:
        session.add(
            FireEvent(
                id=override_id,
                workspace_id=team.workspace_id,
                scenario_id=plan_id,
                year=2027,
                kind="saving_override",
                amount_minor=100_000,
                label="Next year",
            )
        )
    team.clock._value = datetime(2027, 3, 1, tzinfo=UTC)
    fire_scenarios.update_fire_plan(
        team.factory,
        team.ctx("editor", "editor"),
        _input(
            events=(
                EventInput(
                    id=override_id,
                    year=2027,
                    kind="saving_override",
                    amount_minor=200_000,
                    label="Now current",
                ),
            )
        ),
    )
    detail = fire_scenarios.get_fire_plan(team.factory, team.ctx("viewer", "viewer"))
    assert [row.year for row in detail.archived_events] == [2027]
    assert detail.active_events == ()


def test_delete_hard_deletes_and_audits_without_amounts(team: WorkspaceDb) -> None:
    plan_id = _create(team)
    _add_event(team, plan_id, 2027, 100_000)
    fire_scenarios.delete_fire_plan(team.factory, team.ctx("editor", "editor"), confirmed=True)
    with team.factory() as session:
        assert session.get(FireScenario, plan_id) is None
        assert session.query(FireEvent).count() == 0
        audit = session.query(AuditEvent).filter_by(action="fire_plan.deleted").one()
        assert audit.data == {"events_deleted": 1}


def test_delete_requires_confirmation(team: WorkspaceDb) -> None:
    _create(team)
    with pytest.raises(FireError) as error:
        fire_scenarios.delete_fire_plan(team.factory, team.ctx("editor", "editor"))
    assert error.value.code == "confirmation_required"


def test_roles_and_tenant_checks(team: WorkspaceDb) -> None:
    plan_id = _create(team)
    other = team.add_workspace("Other")
    team.add_member("other-owner", "owner", workspace_id=other)
    other_ctx = team.ctx("other-owner", "owner", workspace_id=other)
    with pytest.raises(NotFound):
        fire_scenarios.get_fire_plan(team.factory, other_ctx)
    with pytest.raises(PermissionDenied):
        fire_scenarios.update_fire_plan(team.factory, team.ctx("viewer", "viewer"), _input())
    with pytest.raises(PermissionDenied):
        fire_scenarios.delete_fire_plan(team.factory, team.ctx("viewer", "viewer"), confirmed=True)
    assert plan_id


def test_a_single_legacy_plan_auto_migrates_on_an_editor_visit(team: WorkspaceDb) -> None:
    legacy_id = _legacy(team, name="Only old plan", is_default=True)
    entry = plan_entry(team.factory, team.ctx("editor", "editor"))
    assert entry.migration is not None
    assert entry.migration.plan_id == legacy_id
    with team.factory() as session:
        row = session.get_one(FireScenario, legacy_id)
        assert row.is_live is True
        assert row.legacy is None
        assert row.needs_review is True
    detail = fire_scenarios.get_fire_plan(team.factory, team.ctx("viewer", "viewer"))
    assert detail.plan.needs_review is True
    assert detail.plan.income == _money(5_000_000)


def test_a_viewer_sees_a_single_legacy_plan_but_does_not_migrate_it(
    team: WorkspaceDb,
) -> None:
    legacy_id = _legacy(team, name="Only old plan", is_default=True)
    entry = plan_entry(team.factory, team.ctx("viewer", "viewer"))
    assert entry.migration is None
    assert len(entry.legacy) == 1
    with team.factory() as session:
        assert session.get_one(FireScenario, legacy_id).is_live is False


def test_multiple_legacy_rows_show_a_chooser_and_the_choice_deletes_the_others(
    team: WorkspaceDb,
) -> None:
    first = _legacy(team, name="First plan", is_default=True)
    second = _legacy(team, name="Second plan")
    entry = plan_entry(team.factory, team.ctx("editor", "editor"))
    assert entry.migration is None
    assert {row.id for row in entry.legacy} == {first, second}
    result = fire_scenarios.migrate_legacy_plan(team.factory, team.ctx("editor", "editor"), second)
    assert result.deleted_count == 1
    with team.factory() as session:
        assert session.get(FireScenario, first) is None
        live = session.get_one(FireScenario, second)
        assert live.is_live is True
        audit = session.query(AuditEvent).filter_by(action="fire_plan.migrated").one()
        assert audit.data["deleted_scenarios"] == 1
        assert audit.data["legacy_name"] == "Second plan"


def test_an_archived_legacy_scenario_can_be_chosen(team: WorkspaceDb) -> None:
    _legacy(team, name="Active plan", is_default=True)
    archived = _legacy(
        team,
        name="Archived plan",
        archived_at="2026-01-01T00:00:00Z",
    )
    result = fire_scenarios.migrate_legacy_plan(
        team.factory, team.ctx("editor", "editor"), archived
    )
    assert result.plan_id == archived
    detail = fire_scenarios.get_fire_plan(team.factory, team.ctx("viewer", "viewer"))
    assert detail.plan.id == archived


def test_linked_legacy_values_are_resolved_once_and_zeroed_with_a_warning(
    team: WorkspaceDb,
) -> None:
    category = _category(team, "Salary", direction="income")
    with team.factory.begin() as session:
        TransactionRepository(session, team.ctx("owner", "owner")).create(
            direction="income",
            day=datetime(2026, 1, 10).date(),
            amount_minor=1_000_000,
            currency="EUR",
            category_id=category,
            description="Salary",
            description_key="salary",
            source="manual",
            created_by_user_id=None,
        )
    linked = _legacy(
        team,
        name="Linked plan",
        income_mode="trailing_12m",
        income_manual_minor=None,
    )
    blocked = _legacy(
        team,
        name="Blocked plan",
        income_mode="trailing_12m",
        expenses_mode="trailing_12m",
        income_manual_minor=None,
        expenses_manual_minor=None,
    )
    result = fire_scenarios.migrate_legacy_plan(team.factory, team.ctx("editor", "editor"), linked)
    assert result.zeroed_fields == ()
    detail = fire_scenarios.get_fire_plan(team.factory, team.ctx("viewer", "viewer"))
    assert detail.plan.income == _money(12_000_000)
    assert blocked


def test_a_blocked_linked_value_is_zeroed_and_reported(team: WorkspaceDb) -> None:
    plan_id = _legacy(
        team,
        name="Blocked plan",
        income_mode="trailing_12m",
        income_manual_minor=None,
    )
    result = fire_scenarios.migrate_legacy_plan(team.factory, team.ctx("editor", "editor"), plan_id)
    assert result.zeroed_fields == ("income_manual",)
    detail = fire_scenarios.get_fire_plan(team.factory, team.ctx("viewer", "viewer"))
    assert detail.plan.income == _money(0)
    assert detail.plan.needs_review is True


def test_archived_legacy_events_are_counted_in_the_migration_result(team: WorkspaceDb) -> None:
    plan_id = _legacy(team, name="Old plan")
    _add_event(team, plan_id, 2020, 100_000)
    result = fire_scenarios.migrate_legacy_plan(team.factory, team.ctx("editor", "editor"), plan_id)
    assert result.archived_event_count == 1


def test_migration_is_idempotent(team: WorkspaceDb) -> None:
    plan_id = _legacy(team, name="Old plan")
    first = fire_scenarios.migrate_legacy_plan(team.factory, team.ctx("editor", "editor"), plan_id)
    second = fire_scenarios.migrate_legacy_plan(team.factory, team.ctx("editor", "editor"), plan_id)
    assert first.plan_id == second.plan_id
    assert second.already_migrated is True


def test_audit_actions_cover_create_update_delete_and_migrate(team: WorkspaceDb) -> None:
    plan_id = _create(team)
    fire_scenarios.update_fire_plan(team.factory, team.ctx("editor", "editor"), _input())
    fire_scenarios.delete_fire_plan(team.factory, team.ctx("editor", "editor"), confirmed=True)
    _legacy(team, name="Old plan")
    plan_entry(team.factory, team.ctx("editor", "editor"))
    with team.factory() as session:
        actions = {row.action for row in session.query(AuditEvent).all()}
    assert {
        "fire_plan.created",
        "fire_plan.updated",
        "fire_plan.deleted",
        "fire_plan.migrated",
    } <= actions
    assert plan_id


def _category(team: WorkspaceDb, name: str, *, direction: str) -> UUID:
    with team.factory.begin() as session:
        row = CategoryRepository(session, team.ctx("owner", "owner")).create(
            name=name,
            name_key=name.casefold(),
            kind=direction,
            treatment="regular",
            color="#16a34a",
            sort_order=0,
        )
        return row.id


def test_query_budget_does_not_grow_with_events(team: WorkspaceDb) -> None:
    plan_id = _create(team)
    ctx = team.ctx("viewer", "viewer")

    def count(operation: Any) -> int:
        seen = 0

        def before(*_args: object) -> None:
            nonlocal seen
            seen += 1

        event.listen(team.engine, "before_cursor_execute", before)
        try:
            operation()
        finally:
            event.remove(team.engine, "before_cursor_execute", before)
        return seen

    quiet = count(lambda: fire_scenarios.get_fire_plan(team.factory, ctx))
    for index in range(50):
        _add_event(team, plan_id, 2027 + (index % 40), index + 1)
    loud = count(lambda: fire_scenarios.get_fire_plan(team.factory, ctx))
    assert quiet == loud
    assert quiet <= 4


def test_year_rollover_moves_the_anchor_and_archives_events(team: WorkspaceDb) -> None:
    plan_id = _create(team)
    _add_event(team, plan_id, 2026, 100_000)
    team.clock._value = datetime(2027, 3, 1, tzinfo=UTC)
    detail = fire_scenarios.get_fire_plan(team.factory, team.ctx("viewer", "viewer"))
    assert detail.plan.id == plan_id
    assert detail.plan.current_year == 2027
    assert detail.plan.age_at_start == 42
    assert detail.plan.last_year == 2075
    assert [row.year for row in detail.archived_events] == [2026]
    assert detail.active_events == ()
    from assetboard.services import fire_projection

    results = fire_projection.scenario_results(team.factory, team.ctx("viewer", "viewer"))
    assert results.projection is not None
    assert results.projection.inputs.start_year == 2027
    assert results.projection.rows[0].year == 2027
