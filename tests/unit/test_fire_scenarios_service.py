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


def _add_event(
    team: WorkspaceDb,
    plan_id: UUID,
    year: int,
    amount_minor: int,
    *,
    kind: str = "cash",
    label: str = "Event",
    import_key: str | None = None,
) -> UUID:
    with team.factory.begin() as session:
        row = FireEventRepository(session, team.ctx("owner", "owner")).create(
            scenario_id=plan_id,
            year=year,
            kind=kind,
            amount_minor=amount_minor,
            label=label,
            import_key=import_key,
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
            ),
            removed_event_ids=(second,),
        ),
    )
    with team.factory() as session:
        rows = FireEventRepository(session, team.ctx("viewer", "viewer")).for_plan(plan_id)
        assert [(row.id, row.amount_minor) for row in rows] == [(first, 150_000)]
        assert session.get_one(FireScenario, plan_id).needs_review is False


def test_a_saved_active_row_cannot_be_deleted_by_omission(team: WorkspaceDb) -> None:
    plan_id = _create(team)
    first = _add_event(team, plan_id, 2027, 100_000)
    second = _add_event(team, plan_id, 2028, 200_000)
    with pytest.raises(FireError) as error:
        fire_scenarios.update_fire_plan(
            team.factory,
            team.ctx("editor", "editor"),
            _input(
                events=(
                    EventInput(
                        id=first,
                        year=2027,
                        kind="cash",
                        amount_minor=100_000,
                        label="Event",
                    ),
                )
            ),
        )
    assert error.value.code == "invalid_event"
    with team.factory() as session:
        rows = FireEventRepository(session, team.ctx("viewer", "viewer")).for_plan(plan_id)
        assert {row.id for row in rows} == {first, second}
        assert {row.amount_minor for row in rows} == {100_000, 200_000}


def test_removed_ids_must_be_posted_or_saved_and_not_both(team: WorkspaceDb) -> None:
    plan_id = _create(team)
    event_id = _add_event(team, plan_id, 2027, 100_000)
    with pytest.raises(FireError) as duplicate:
        fire_scenarios.update_fire_plan(
            team.factory,
            team.ctx("editor", "editor"),
            _input(
                events=(
                    EventInput(
                        id=event_id,
                        year=2027,
                        kind="cash",
                        amount_minor=100_000,
                        label="Event",
                    ),
                ),
                removed_event_ids=(event_id,),
            ),
        )
    assert duplicate.value.code == "duplicate_event"
    with pytest.raises(FireError) as unknown:
        fire_scenarios.update_fire_plan(
            team.factory,
            team.ctx("editor", "editor"),
            _input(removed_event_ids=(uuid4(),)),
        )
    assert unknown.value.code == "invalid_event"


def test_archived_events_stay_when_the_plan_is_saved_without_them(team: WorkspaceDb) -> None:
    plan_id = _create(team)
    old_cash = _add_event(team, plan_id, 2020, 100_000, label="Old bonus")
    current_override = _add_event(
        team,
        plan_id,
        2026,
        200_000,
        kind="saving_override",
        label="This year",
    )
    fire_scenarios.update_fire_plan(team.factory, team.ctx("editor", "editor"), _input())
    detail = fire_scenarios.get_fire_plan(team.factory, team.ctx("viewer", "viewer"))
    assert {row.id for row in detail.archived_events} == {old_cash, current_override}
    assert detail.active_events == ()


def test_archived_events_can_be_deleted_by_explicit_removal(team: WorkspaceDb) -> None:
    plan_id = _create(team)
    old = _add_event(team, plan_id, 2020, 100_000, label="Old bonus")
    active = _add_event(team, plan_id, 2027, 200_000, label="New bonus")
    fire_scenarios.update_fire_plan(
        team.factory,
        team.ctx("editor", "editor"),
        _input(
            events=(
                EventInput(
                    id=active,
                    year=2027,
                    kind="cash",
                    amount_minor=200_000,
                    label="New bonus",
                ),
            ),
            removed_event_ids=(old,),
        ),
    )
    detail = fire_scenarios.get_fire_plan(team.factory, team.ctx("viewer", "viewer"))
    assert [row.id for row in detail.active_events] == [active]
    assert detail.archived_events == ()


def test_archived_events_are_read_only_but_unchanged_posts_are_allowed(
    team: WorkspaceDb,
) -> None:
    plan_id = _create(team)
    old = _add_event(team, plan_id, 2020, 100_000, label="Old bonus")
    with pytest.raises(FireError) as error:
        fire_scenarios.update_fire_plan(
            team.factory,
            team.ctx("editor", "editor"),
            _input(
                events=(
                    EventInput(
                        id=old,
                        year=2020,
                        kind="cash",
                        amount_minor=150_000,
                        label="Old bonus",
                    ),
                )
            ),
        )
    assert error.value.code == "archived_event_read_only"
    assert error.value.row is None
    fire_scenarios.update_fire_plan(
        team.factory,
        team.ctx("editor", "editor"),
        _input(
            events=(
                EventInput(
                    id=old,
                    year=2020,
                    kind="cash",
                    amount_minor=100_000,
                    label="Old bonus",
                ),
            )
        ),
    )
    detail = fire_scenarios.get_fire_plan(team.factory, team.ctx("viewer", "viewer"))
    assert [row.id for row in detail.archived_events] == [old]


def test_horizon_shrink_archives_and_expand_restores_the_same_event(
    team: WorkspaceDb,
) -> None:
    plan_id = _create(team)
    event_id = _add_event(team, plan_id, 2070, 100_000, label="Later bonus")
    fire_scenarios.update_fire_plan(
        team.factory,
        team.ctx("editor", "editor"),
        _input(
            horizon_age=80,
            events=(
                EventInput(
                    id=event_id,
                    year=2070,
                    kind="cash",
                    amount_minor=100_000,
                    label="Later bonus",
                ),
            ),
        ),
    )
    detail = fire_scenarios.get_fire_plan(team.factory, team.ctx("viewer", "viewer"))
    assert [row.id for row in detail.archived_events] == [event_id]
    assert detail.active_events == ()
    fire_scenarios.update_fire_plan(
        team.factory,
        team.ctx("editor", "editor"),
        _input(horizon_age=90),
    )
    detail = fire_scenarios.get_fire_plan(team.factory, team.ctx("viewer", "viewer"))
    assert [row.id for row in detail.active_events] == [event_id]
    assert detail.archived_events == ()


def test_a_changed_year_must_fit_the_submitted_window(team: WorkspaceDb) -> None:
    plan_id = _create(team)
    event_id = _add_event(team, plan_id, 2027, 100_000, label="Bonus")
    with pytest.raises(FireError) as error:
        fire_scenarios.update_fire_plan(
            team.factory,
            team.ctx("editor", "editor"),
            _input(
                horizon_age=42,
                events=(
                    EventInput(
                        id=event_id,
                        year=2028,
                        kind="cash",
                        amount_minor=100_000,
                        label="Bonus",
                    ),
                ),
            ),
        )
    assert error.value.code == "rows_outside_window"


def test_an_archived_override_blocks_a_new_override_when_the_horizon_expands(
    team: WorkspaceDb,
) -> None:
    plan_id = _create(team, horizon_age=80)
    _add_event(
        team,
        plan_id,
        2070,
        100_000,
        kind="saving_override",
        label="Late override",
    )
    with pytest.raises(FireError) as error:
        fire_scenarios.update_fire_plan(
            team.factory,
            team.ctx("editor", "editor"),
            _input(
                horizon_age=90,
                events=(
                    EventInput(
                        year=2070,
                        kind="saving_override",
                        amount_minor=200_000,
                        label="New override",
                        index="7",
                    ),
                ),
            ),
        )
    assert error.value.code == "duplicate_saving_override"
    assert error.value.row == "7"
    assert error.value.column == "year"


def test_adjustment_row_errors_name_the_row_and_column(team: WorkspaceDb) -> None:
    _create(team)
    with pytest.raises(FireError) as duplicate:
        fire_scenarios.update_fire_plan(
            team.factory,
            team.ctx("editor", "editor"),
            _input(
                events=(
                    EventInput(
                        year=2027,
                        kind="saving_override",
                        amount_minor=1,
                        label="A",
                        index="4",
                    ),
                    EventInput(
                        year=2027,
                        kind="saving_override",
                        amount_minor=2,
                        label="B",
                        index="5",
                    ),
                )
            ),
        )
    assert duplicate.value.code == "duplicate_saving_override"
    assert duplicate.value.row == "5"
    assert duplicate.value.column == "year"
    with pytest.raises(FireError) as bad_year:
        fire_scenarios.update_fire_plan(
            team.factory,
            team.ctx("editor", "editor"),
            _input(
                events=(
                    EventInput(
                        year=2020,
                        kind="cash",
                        amount_minor=1,
                        label="Past",
                        index="new",
                    ),
                )
            ),
        )
    assert bad_year.value.code == "rows_outside_window"
    assert bad_year.value.row == "new"
    assert bad_year.value.column == "year"


def test_all_four_kinds_and_repeated_rows_are_accepted(team: WorkspaceDb) -> None:
    plan_id = _create(team)
    fire_scenarios.update_fire_plan(
        team.factory,
        team.ctx("editor", "editor"),
        _input(
            events=(
                EventInput(year=2027, kind="cash", amount_minor=100_000, label="Bonus"),
                EventInput(year=2027, kind="cash", amount_minor=50_000, label="Gift"),
                EventInput(year=2027, kind="income_change", amount_minor=20_000, label="Rent"),
                EventInput(year=2028, kind="expense_change", amount_minor=-30_000, label="Loan"),
                EventInput(
                    year=2029,
                    kind="saving_override",
                    amount_minor=-40_000,
                    label="Sabbatical",
                ),
                EventInput(year=2029, kind="cash", amount_minor=0, label="Zero is explicit"),
            )
        ),
    )
    with team.factory() as session:
        rows = FireEventRepository(session, team.ctx("viewer", "viewer")).for_plan(plan_id)
        assert {row.kind for row in rows} == {
            "cash",
            "income_change",
            "expense_change",
            "saving_override",
        }
        assert {row.amount_minor for row in rows if row.year == 2027} == {
            100_000,
            50_000,
            20_000,
        }
        assert sum(1 for row in rows if row.kind == "cash") == 3


def test_update_preserves_untouched_row_ids_timestamps_and_import_lineage(
    team: WorkspaceDb,
) -> None:
    plan_id = _create(team)
    event_id = _add_event(
        team,
        plan_id,
        2027,
        100_000,
        label="Imported",
        import_key="fire-event-1",
    )
    with team.factory() as session:
        row = session.get_one(FireEvent, event_id)
        created_at, updated_at = row.created_at, row.updated_at
    fire_scenarios.update_fire_plan(
        team.factory,
        team.ctx("editor", "editor"),
        _input(
            events=(
                EventInput(
                    id=event_id,
                    year=2027,
                    kind="cash",
                    amount_minor=100_000,
                    label="Imported",
                ),
            )
        ),
    )
    with team.factory() as session:
        row = session.get_one(FireEvent, event_id)
        assert row.id == event_id
        assert row.import_key == "fire-event-1"
        assert row.created_at == created_at
        assert row.updated_at == updated_at
    fire_scenarios.update_fire_plan(
        team.factory,
        team.ctx("editor", "editor"),
        _input(
            events=(
                EventInput(
                    id=event_id,
                    year=2027,
                    kind="cash",
                    amount_minor=150_000,
                    label="Imported",
                ),
            )
        ),
    )
    with team.factory() as session:
        row = session.get_one(FireEvent, event_id)
        assert row.id == event_id
        assert row.import_key == "fire-event-1"
        assert row.created_at == created_at
        assert row.amount_minor == 150_000


def test_an_archived_override_can_be_replaced_in_the_same_year(team: WorkspaceDb) -> None:
    plan_id = _create(team, horizon_age=80)
    old_override = _add_event(
        team,
        plan_id,
        2070,
        100_000,
        kind="saving_override",
        label="Late override",
    )
    fire_scenarios.update_fire_plan(
        team.factory,
        team.ctx("editor", "editor"),
        _input(
            horizon_age=90,
            events=(
                EventInput(year=2070, kind="saving_override", amount_minor=200_000, label="New"),
            ),
            removed_event_ids=(old_override,),
        ),
    )
    detail = fire_scenarios.get_fire_plan(team.factory, team.ctx("viewer", "viewer"))
    assert [(row.year, row.amount.amount_minor) for row in detail.active_events] == [
        (2070, 200_000)
    ]
    assert detail.archived_events == ()


def test_cross_workspace_removed_id_is_refused_without_touching_the_other_row(
    team: WorkspaceDb,
) -> None:
    _create(team)
    other = team.add_workspace("Other")
    team.add_member("other-owner", "owner", workspace_id=other)
    other_ctx = team.ctx("other-owner", "owner", workspace_id=other)
    with team.factory.begin() as session:
        other_plan = FireScenarioRepository(session, other_ctx).create(
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
        other_event = FireEventRepository(session, other_ctx).create(
            scenario_id=other_plan.id,
            year=2027,
            kind="cash",
            amount_minor=100_000,
            label="Other bonus",
        )
        other_event_id = other_event.id
    with pytest.raises(FireError) as error:
        fire_scenarios.update_fire_plan(
            team.factory,
            team.ctx("editor", "editor"),
            _input(removed_event_ids=(other_event_id,)),
        )
    assert error.value.code == "invalid_event"
    with team.factory() as session:
        assert FireEventRepository(session, other_ctx).for_plan(other_plan.id)[0].id == (
            other_event_id
        )


def test_the_event_limit_counts_archived_history(team: WorkspaceDb) -> None:
    plan_id = _create(team)
    with team.factory.begin() as session:
        repository = FireEventRepository(session, team.ctx("owner", "owner"))
        for index in range(200):
            repository.create(
                scenario_id=plan_id,
                year=2020,
                kind="cash",
                amount_minor=index + 1,
                label="Old",
            )
    with pytest.raises(FireError) as error:
        fire_scenarios.update_fire_plan(
            team.factory,
            team.ctx("editor", "editor"),
            _input(events=(EventInput(year=2027, kind="cash", amount_minor=1, label="New"),)),
        )
    assert error.value.code == "too_many_events"


def test_create_never_accepts_events_or_removals(team: WorkspaceDb) -> None:
    with pytest.raises(FireError) as event:
        _create(team, events=(EventInput(year=2027, kind="cash", amount_minor=1, label="A"),))
    assert event.value.code == "events_not_allowed_on_create"
    with pytest.raises(FireError) as removal:
        _create(team, removed_event_ids=(uuid4(),))
    assert removal.value.code == "events_not_allowed_on_create"


def test_one_update_writes_one_audit_event_without_amounts(team: WorkspaceDb) -> None:
    plan_id = _create(team)
    _add_event(team, plan_id, 2020, 100_000, label="Old")
    with team.factory() as session:
        before = session.query(AuditEvent).filter_by(action="fire_plan.updated").count()
    fire_scenarios.update_fire_plan(
        team.factory,
        team.ctx("editor", "editor"),
        _input(events=(EventInput(year=2027, kind="cash", amount_minor=1, label="New"),)),
    )
    with team.factory() as session:
        audits = (
            session.query(AuditEvent)
            .filter_by(action="fire_plan.updated")
            .order_by(AuditEvent.ts)
            .all()
        )
        assert len(audits) == before + 1
        assert audits[-1].data == {}


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


def test_the_chooser_cannot_migrate_another_workspaces_scenario(team: WorkspaceDb) -> None:
    other = team.add_workspace("Other")
    team.add_member("other-owner", "owner", workspace_id=other)
    other_ctx = team.ctx("other-owner", "owner", workspace_id=other)
    payload = {
        "v": 1,
        "name": "Their plan",
        "name_key": "their plan",
        "start_year": 2026,
        "is_default": True,
        "archived_at": None,
        "income_mode": "manual",
        "expenses_mode": "manual",
        "portfolio_mode": "manual",
        "income_manual_minor": 5_000_000,
        "expenses_manual_minor": 3_000_000,
        "portfolio_manual_minor": 100_000_000,
        "expense_exclusion_ids": [],
    }
    with team.factory.begin() as session:
        other_id = (
            FireScenarioRepository(session, other_ctx)
            .create(
                currency="EUR",
                birth_year=1985,
                inflation_bp=250,
                real_return_bp=500,
                income_growth_bp=300,
                stop_working_age=55,
                horizon_age=90,
                rule="returns_cover_expenses",
                swr_bp=None,
                income_minor=5_000_000,
                expenses_minor=3_000_000,
                portfolio_minor=100_000_000,
                needs_review=True,
                is_live=False,
                legacy=json.dumps(payload, sort_keys=True),
            )
            .id
        )
    with pytest.raises(NotFound):
        fire_scenarios.migrate_legacy_plan(team.factory, team.ctx("editor", "editor"), other_id)
    with team.factory() as session:
        assert session.get_one(FireScenario, other_id).is_live is False


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
    first_event = _add_event(team, first, 2030, 100_000)
    entry = plan_entry(team.factory, team.ctx("editor", "editor"))
    assert entry.migration is None
    assert {row.id for row in entry.legacy} == {first, second}
    result = fire_scenarios.migrate_legacy_plan(team.factory, team.ctx("editor", "editor"), second)
    assert result.deleted_count == 1
    with team.factory() as session:
        assert session.get(FireScenario, first) is None
        assert session.get(FireEvent, first_event) is None
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
    assert quiet <= 6


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


def test_plan_entry_returns_the_loaded_detail_for_the_view_page(team: WorkspaceDb) -> None:
    plan_id = _create(team)
    _add_event(team, plan_id, 2027, 100_000)
    entry = fire_scenarios.plan_entry(team.factory, team.ctx("viewer", "viewer"))
    assert entry.plan is not None and entry.plan.id == plan_id
    assert entry.detail is not None
    assert entry.detail.plan.id == plan_id
    assert [row.year for row in entry.detail.active_events] == [2027]
    assert entry.migration is None


def test_validate_amount_distinguishes_a_missing_wizard_value(team: WorkspaceDb) -> None:
    assert fire_scenarios.validate_amount("income", _money(0), "EUR", required=True) == 0
    with pytest.raises(FireError) as missing:
        fire_scenarios.validate_amount("income", None, "EUR", required=True)
    assert missing.value.code == "amount_required"
    with pytest.raises(FireError) as saved:
        fire_scenarios.validate_amount("income", None, "EUR")
    assert saved.value.code == "invalid_amount"


def test_validate_ages_and_rates_keep_the_existing_codes(team: WorkspaceDb) -> None:
    assert fire_scenarios.validate_ages(1985, 90, 55, current_year=2026, default_horizon=90) == (
        1985,
        90,
        55,
    )
    with pytest.raises(FireError) as horizon:
        fire_scenarios.validate_ages(1985, 40, 55, current_year=2026, default_horizon=90)
    assert horizon.value.code == "horizon_not_after_age"
    rates = fire_scenarios.validate_rates(
        inflation_bp=250,
        real_return_bp=500,
        income_growth_bp=300,
        rule=Rule.RETURNS_COVER_EXPENSES.value,
        swr_bp=None,
    )
    assert rates.needs_extreme_confirmation is False
    with pytest.raises(FireError) as swr:
        fire_scenarios.validate_rates(
            inflation_bp=250,
            real_return_bp=500,
            income_growth_bp=300,
            rule=Rule.SWR.value,
            swr_bp=None,
        )
    assert swr.value.code == "invalid_swr"
