from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select, update

from assetboard.db.models.fire import FireContribution, FireEvent, FireScenario
from assetboard.db.models.identity import AuditEvent, User, Workspace
from assetboard.domain.fire import (
    MAX_CONTRIBUTION_ROWS,
    MAX_EVENT_ROWS,
    EventKind,
    ExpensesMode,
    IncomeMode,
    PortfolioMode,
    Rule,
)
from assetboard.domain.money import Money
from assetboard.services import fire_scenarios
from assetboard.services.archive import workspace_is_empty
from assetboard.services.context import PermissionDenied
from assetboard.services.fire_scenarios import (
    ContributionInput,
    EventInput,
    FireError,
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


def _money(minor: int, currency: str = "EUR") -> Money:
    return Money(minor, currency)


def _input(**overrides) -> FireScenarioInput:
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
        "income_mode": IncomeMode.MANUAL.value,
        "income_manual": _money(5_000_000),
        "expenses_mode": ExpensesMode.MANUAL.value,
        "expenses_manual": _money(3_000_000),
        "portfolio_mode": PortfolioMode.MANUAL.value,
        "portfolio_manual": _money(100_000_000),
    }
    values.update(overrides)
    return FireScenarioInput(**values)  # type: ignore[arg-type]


def _create(team: WorkspaceDb, member: str = "owner", **overrides) -> UUID:
    return fire_scenarios.create_fire_scenario(
        team.factory, team.ctx(member, member), _input(**overrides)
    )


def _codes(exc: pytest.ExceptionInfo[FireError]) -> tuple[str, str]:
    return exc.value.field, exc.value.code


def test_create_stores_the_start_year_currency_and_makes_the_first_default(
    team: WorkspaceDb,
) -> None:
    scenario_id = fire_scenarios.create_fire_scenario(
        team.factory,
        team.ctx("editor", "editor"),
        _input(
            contributions=(ContributionInput(year=2027, amount_minor=1_200_000),),
            events=(
                EventInput(
                    year=2028,
                    kind=EventKind.EXPENSE_CHANGE.value,
                    amount_minor=-500_000,
                    label="Mortgage repaid",
                ),
            ),
        ),
    )
    detail = fire_scenarios.get_fire_scenario(
        team.factory, team.ctx("viewer", "viewer"), scenario_id
    )
    scenario = detail.scenario
    assert (scenario.start_year, scenario.currency, scenario.is_default) == (2026, "EUR", True)
    assert scenario.age_at_start == 41
    assert scenario.income_manual == _money(5_000_000)
    assert scenario.expenses_manual == _money(3_000_000)
    assert scenario.portfolio_manual == _money(100_000_000)
    assert [(row.year, row.amount) for row in detail.contributions] == [(2027, _money(1_200_000))]
    assert [(row.year, row.kind, row.amount, row.label) for row in detail.events] == [
        (2028, "expense_change", _money(-500_000), "Mortgage repaid")
    ]
    with team.factory() as session:
        actions = list(
            session.scalars(
                select(AuditEvent.action).where(AuditEvent.action.like("fire_scenario.%"))
            )
        )
        assert actions == ["fire_scenario.created"]
        stored = session.get_one(FireScenario, scenario_id)
        assert stored.name_key == "retire at 55"


def test_active_names_are_unique_ignoring_case_and_whitespace(team: WorkspaceDb) -> None:
    _create(team, name="Retire at 55")
    with pytest.raises(FireError) as exc:
        _create(team, name="  retire   AT 55 ")
    assert _codes(exc) == ("name", "duplicate_name")


def test_duplicate_copies_the_children_and_uses_a_free_copy_name(team: WorkspaceDb) -> None:
    source = _create(
        team,
        contributions=(ContributionInput(year=2027, amount_minor=1_200_000),),
        events=(
            EventInput(year=2028, kind=EventKind.CASH.value, amount_minor=200_000, label="Bonus"),
        ),
    )
    first = fire_scenarios.duplicate_fire_scenario(team.factory, team.ctx("owner", "owner"), source)
    second = fire_scenarios.duplicate_fire_scenario(
        team.factory, team.ctx("owner", "owner"), source
    )
    first_detail = fire_scenarios.get_fire_scenario(team.factory, team.ctx("owner", "owner"), first)
    second_detail = fire_scenarios.get_fire_scenario(
        team.factory, team.ctx("owner", "owner"), second
    )
    assert first_detail.scenario.name == "Retire at 55 (copy)"
    assert second_detail.scenario.name == "Retire at 55 (copy 2)"
    assert first_detail.scenario.is_default is False
    assert [row.year for row in first_detail.contributions] == [2027]
    assert [row.label for row in first_detail.events] == ["Bonus"]
    with team.factory() as session:
        actions = list(
            session.scalars(
                select(AuditEvent.action).where(AuditEvent.action == "fire_scenario.duplicated")
            )
        )
        assert len(actions) == 2


def test_default_moves_to_the_next_active_scenario_and_comes_back_on_restore(
    team: WorkspaceDb,
) -> None:
    first = _create(team, name="First")
    second = _create(team, name="Second")
    ctx = team.ctx("owner", "owner")
    assert _default_id(team) == first
    fire_scenarios.set_default_fire_scenario(team.factory, ctx, second)
    assert _default_id(team) == second
    assert _is_default(team, first) is False

    fire_scenarios.archive_fire_scenario(team.factory, ctx, second)
    assert _default_id(team) == first
    fire_scenarios.restore_fire_scenario(team.factory, ctx, second)
    assert _default_id(team) == first
    assert _is_default(team, second) is False

    fire_scenarios.archive_fire_scenario(team.factory, ctx, first)
    assert _default_id(team) == second
    fire_scenarios.archive_fire_scenario(team.factory, ctx, second)
    assert _default_id(team) is None
    fire_scenarios.restore_fire_scenario(team.factory, ctx, first)
    assert _default_id(team) == first


def _default_id(team: WorkspaceDb) -> UUID | None:
    with team.factory() as session:
        row = session.scalars(
            select(FireScenario).where(
                FireScenario.workspace_id == team.workspace_id,
                FireScenario.is_default.is_(True),
                FireScenario.archived_at.is_(None),
            )
        ).one_or_none()
        return None if row is None else row.id


def _is_default(team: WorkspaceDb, scenario_id: UUID) -> bool:
    with team.factory() as session:
        return session.get_one(FireScenario, scenario_id).is_default


def test_archived_scenarios_are_read_only_and_restore_renames_a_conflict(
    team: WorkspaceDb,
) -> None:
    ctx = team.ctx("owner", "owner")
    archived = _create(team, name="Plan")
    fire_scenarios.archive_fire_scenario(team.factory, ctx, archived)
    with pytest.raises(FireError) as exc:
        fire_scenarios.update_fire_scenario(team.factory, ctx, archived, _input(name="Plan"))
    assert _codes(exc) == ("scenario", "archived")
    restorable = _create(team, name="Other")
    _create(team, name="Plan")
    fire_scenarios.archive_fire_scenario(team.factory, ctx, restorable)
    fire_scenarios.restore_fire_scenario(team.factory, ctx, archived)
    detail = fire_scenarios.get_fire_scenario(team.factory, ctx, archived)
    assert detail.scenario.name == "Plan (copy)"
    assert detail.scenario.archived_at is None


def test_unusual_rates_need_one_confirmation_only_when_changed(team: WorkspaceDb) -> None:
    ctx = team.ctx("owner", "owner")
    with pytest.raises(FireError) as exc:
        _create(team, inflation_bp=1200)
    assert _codes(exc) == ("extreme_rates_confirmed", "extreme_rate_confirmation_required")
    scenario_id = _create(team, inflation_bp=1200, extreme_rates_confirmed=True)

    # The unchanged unusual rate does not ask again.
    fire_scenarios.update_fire_scenario(
        team.factory,
        ctx,
        scenario_id,
        _input(inflation_bp=1200, stop_working_age=56),
    )
    with pytest.raises(FireError) as exc:
        fire_scenarios.update_fire_scenario(
            team.factory,
            ctx,
            scenario_id,
            _input(inflation_bp=1300, stop_working_age=56),
        )
    assert _codes(exc) == ("extreme_rates_confirmed", "extreme_rate_confirmation_required")
    fire_scenarios.update_fire_scenario(
        team.factory,
        ctx,
        scenario_id,
        _input(inflation_bp=1300, stop_working_age=56, extreme_rates_confirmed=True),
    )

    with pytest.raises(FireError) as exc:
        _create(
            team,
            name="SWR plan",
            rule=Rule.SWR.value,
            swr_bp=700,
        )
    assert _codes(exc) == ("extreme_rates_confirmed", "extreme_rate_confirmation_required")
    swr_id = _create(
        team, name="SWR plan", rule=Rule.SWR.value, swr_bp=700, extreme_rates_confirmed=True
    )
    assert fire_scenarios.get_fire_scenario(team.factory, ctx, swr_id).scenario.swr_bp == 700


@pytest.mark.parametrize(
    ("overrides", "field", "code"),
    [
        ({"inflation_bp": 5001}, "inflation", "invalid_rate"),
        ({"real_return_bp": -2001}, "real_return", "invalid_rate"),
        ({"income_growth_bp": 6000}, "income_growth", "invalid_rate"),
        ({"rule": Rule.SWR.value, "swr_bp": 49}, "swr", "invalid_swr"),
        ({"rule": Rule.SWR.value, "swr_bp": 2001}, "swr", "invalid_swr"),
        ({"horizon_age": 41}, "horizon_age", "horizon_not_after_age"),
        ({"horizon_age": 0}, "horizon_age", "invalid_horizon"),
        ({"stop_working_age": 40}, "stop_working_age", "stop_before_age"),
        ({"stop_working_age": 121}, "stop_working_age", "invalid_stop_age"),
        ({"birth_year": None}, "birth_year", "birth_year_required"),
        ({"birth_year": 2027}, "birth_year", "invalid_birth_year"),
        ({"income_manual": None}, "income_manual", "invalid_amount"),
        ({"income_manual": _money(-1)}, "income_manual", "invalid_amount"),
        ({"income_manual": Money(1, "USD")}, "income_manual", "invalid_amount"),
        ({"rule": "unknown"}, "rule", "invalid_rule"),
    ],
)
def test_validation_refuses_impossible_inputs(
    team: WorkspaceDb, overrides: dict[str, Any], field: str, code: str
) -> None:
    with pytest.raises(FireError) as exc:
        _create(team, **overrides)
    assert _codes(exc) == (field, code)


def test_stop_after_the_horizon_is_allowed_with_a_warning(team: WorkspaceDb) -> None:
    scenario_id = _create(team, stop_working_age=95)
    detail = fire_scenarios.get_fire_scenario(team.factory, team.ctx("owner", "owner"), scenario_id)
    assert detail.scenario.stop_after_horizon is True
    assert detail.scenario.stop_working_age == 95


def test_linked_modes_are_saved_without_a_manual_amount(team: WorkspaceDb) -> None:
    cases = (
        ("income_mode", IncomeMode.TRAILING_12M.value, "income_manual"),
        ("expenses_mode", ExpensesMode.TRAILING_12M.value, "expenses_manual"),
        ("portfolio_mode", PortfolioMode.LINKED_INVESTABLE.value, "portfolio_manual"),
    )
    for field_name, value, amount_field in cases:
        scenario_id = _create(team, name=f"Linked {field_name}", **{field_name: value})
        scenario = fire_scenarios.get_fire_scenario(
            team.factory, team.ctx("owner", "owner"), scenario_id
        ).scenario
        assert getattr(scenario, field_name) == value
        assert getattr(scenario, amount_field) is None


def test_contribution_and_event_windows_and_row_caps(team: WorkspaceDb) -> None:
    with pytest.raises(FireError) as exc:
        _create(team, contributions=(ContributionInput(year=2026, amount_minor=1),))
    assert _codes(exc) == ("contributions", "rows_outside_window")
    with pytest.raises(FireError) as exc:
        _create(
            team,
            contributions=(ContributionInput(year=2076, amount_minor=1),),
        )
    assert _codes(exc) == ("contributions", "rows_outside_window")
    with pytest.raises(FireError) as exc:
        _create(
            team,
            contributions=(ContributionInput(year=2027, amount_minor=1),) * 2,
        )
    assert _codes(exc) == ("contributions", "duplicate_contribution_year")
    with pytest.raises(FireError) as exc:
        _create(team, events=(EventInput(year=2076, amount_minor=1, kind="cash", label="x"),))
    assert _codes(exc) == ("events", "rows_outside_window")
    with pytest.raises(FireError) as exc:
        _create(
            team,
            events=tuple(
                EventInput(year=2027, amount_minor=1, kind="cash", label=f"e{index}")
                for index in range(MAX_EVENT_ROWS + 1)
            ),
        )
    assert _codes(exc) == ("events", "too_many_events")
    with pytest.raises(FireError) as exc:
        _create(
            team,
            contributions=tuple(
                ContributionInput(year=2027 + index, amount_minor=1)
                for index in range(MAX_CONTRIBUTION_ROWS + 1)
            ),
        )
    assert _codes(exc) == ("contributions", "too_many_contributions")
    with pytest.raises(FireError) as exc:
        _create(
            team,
            events=(EventInput(year=2027, amount_minor=1, kind="unknown", label="x"),),
        )
    assert _codes(exc) == ("events", "invalid_event_kind")


def test_update_reconciles_rows_and_keeps_unchanged_identities(team: WorkspaceDb) -> None:
    scenario_id = _create(
        team,
        contributions=(
            ContributionInput(year=2027, amount_minor=1),
            ContributionInput(year=2028, amount_minor=2),
            ContributionInput(year=2029, amount_minor=3),
        ),
        events=(
            EventInput(year=2028, kind="cash", amount_minor=100, label="Bonus"),
            EventInput(year=2029, kind="expense_change", amount_minor=-50, label="Rent ends"),
            EventInput(year=2030, kind="income_change", amount_minor=25, label="Side job"),
        ),
    )
    before = fire_scenarios.get_fire_scenario(team.factory, team.ctx("owner", "owner"), scenario_id)
    contribution_ids = {row.year: row.id for row in before.contributions}
    event_ids = {row.label: row.id for row in before.events}
    old = datetime(2020, 1, 1, tzinfo=UTC)
    with team.factory.begin() as session:
        session.execute(
            update(FireContribution)
            .where(FireContribution.scenario_id == scenario_id)
            .values(created_at=old, updated_at=old)
        )
        session.execute(
            update(FireEvent)
            .where(FireEvent.scenario_id == scenario_id)
            .values(created_at=old, updated_at=old)
        )

    fire_scenarios.update_fire_scenario(
        team.factory,
        team.ctx("owner", "owner"),
        scenario_id,
        _input(
            contributions=(
                ContributionInput(year=2027, amount_minor=1),
                ContributionInput(year=2028, amount_minor=20),
                ContributionInput(year=2031, amount_minor=4),
            ),
            events=(
                EventInput(
                    year=2028,
                    kind="cash",
                    amount_minor=100,
                    label="Bonus",
                    id=event_ids["Bonus"],
                ),
                EventInput(
                    year=2029,
                    kind="expense_change",
                    amount_minor=-500,
                    label="Rent ends",
                    id=event_ids["Rent ends"],
                ),
                EventInput(year=2032, kind="income_change", amount_minor=30, label="New job"),
            ),
        ),
    )

    after = fire_scenarios.get_fire_scenario(team.factory, team.ctx("owner", "owner"), scenario_id)
    after_contributions = {row.year: row for row in after.contributions}
    after_events = {row.label: row for row in after.events}
    assert set(after_contributions) == {2027, 2028, 2031}
    assert after_contributions[2027].id == contribution_ids[2027]
    assert after_contributions[2028].id == contribution_ids[2028]
    assert after_contributions[2028].amount.amount_minor == 20
    assert 2029 not in after_contributions
    assert after_contributions[2031].id not in contribution_ids.values()
    assert set(after_events) == {"Bonus", "Rent ends", "New job"}
    assert after_events["Bonus"].id == event_ids["Bonus"]
    assert after_events["Rent ends"].id == event_ids["Rent ends"]
    assert after_events["Rent ends"].amount.amount_minor == -500
    assert after_events["New job"].id not in event_ids.values()

    clock_now = datetime(2026, 3, 1, 12, tzinfo=UTC)
    with team.factory() as session:
        stored_contributions = {
            row.year: row
            for row in session.scalars(
                select(FireContribution).where(FireContribution.scenario_id == scenario_id)
            )
        }
        stored_events = {
            row.label: row
            for row in session.scalars(
                select(FireEvent).where(FireEvent.scenario_id == scenario_id)
            )
        }
    assert stored_contributions[2027].updated_at == old
    assert stored_contributions[2028].updated_at == clock_now
    assert stored_contributions[2031].updated_at == clock_now
    assert stored_events["Bonus"].updated_at == old
    assert stored_events["Rent ends"].updated_at == clock_now
    assert stored_events["New job"].updated_at == clock_now


def test_an_event_id_from_another_scenario_is_rejected_without_writing(team: WorkspaceDb) -> None:
    ctx = team.ctx("owner", "owner")
    first = _create(team, name="First")
    second = _create(
        team,
        name="Second",
        events=(EventInput(year=2028, kind="cash", amount_minor=1, label="Other"),),
    )
    other_event = fire_scenarios.get_fire_scenario(team.factory, ctx, second).events[0]
    before = fire_scenarios.get_fire_scenario(team.factory, ctx, first)

    with pytest.raises(FireError) as exc:
        fire_scenarios.update_fire_scenario(
            team.factory,
            ctx,
            first,
            _input(
                name="Changed name",
                events=(
                    EventInput(
                        year=2028,
                        kind="cash",
                        amount_minor=1,
                        label="Other",
                        id=other_event.id,
                    ),
                ),
            ),
        )
    assert _codes(exc) == ("events", "invalid_event")
    after = fire_scenarios.get_fire_scenario(team.factory, ctx, first)
    assert after.scenario.name == "First"
    assert after.events == before.events


def test_the_same_hidden_event_id_cannot_appear_twice(team: WorkspaceDb) -> None:
    event = EventInput(year=2028, kind="cash", amount_minor=1, label="Bonus")
    scenario_id = _create(team, events=(event,))
    saved = fire_scenarios.get_fire_scenario(
        team.factory, team.ctx("owner", "owner"), scenario_id
    ).events[0]
    duplicate = EventInput(year=2028, kind="cash", amount_minor=1, label="Bonus", id=saved.id)
    with pytest.raises(FireError) as exc:
        fire_scenarios.update_fire_scenario(
            team.factory,
            team.ctx("owner", "owner"),
            scenario_id,
            _input(events=(duplicate, duplicate)),
        )
    assert _codes(exc) == ("events", "duplicate_event")


def test_update_can_replace_every_contribution_and_event_row(team: WorkspaceDb) -> None:
    scenario_id = _create(
        team,
        contributions=(
            ContributionInput(year=2027, amount_minor=1),
            ContributionInput(year=2028, amount_minor=2),
        ),
    )
    fire_scenarios.update_fire_scenario(
        team.factory,
        team.ctx("owner", "owner"),
        scenario_id,
        _input(
            name="Renamed",
            contributions=(ContributionInput(year=2030, amount_minor=-500),),
            events=(EventInput(year=2029, kind="cash", amount_minor=300, label="Gift"),),
        ),
    )
    detail = fire_scenarios.get_fire_scenario(team.factory, team.ctx("owner", "owner"), scenario_id)
    assert detail.scenario.name == "Renamed"
    assert [(row.year, row.amount.amount_minor) for row in detail.contributions] == [(2030, -500)]
    assert [(row.year, row.amount.amount_minor) for row in detail.events] == [(2029, 300)]


def test_viewers_read_and_editors_write_but_stale_roles_are_refused(team: WorkspaceDb) -> None:
    scenario_id = _create(team)
    fire_scenarios.get_fire_scenario(team.factory, team.ctx("viewer", "viewer"), scenario_id)
    fire_scenarios.list_fire_scenarios(team.factory, team.ctx("viewer", "viewer"))
    with pytest.raises(PermissionDenied):
        _create(team, member="viewer")
    stale = team.ctx("viewer", "owner")
    with pytest.raises(PermissionDenied):
        fire_scenarios.update_fire_scenario(team.factory, stale, scenario_id, _input())


def test_defaults_come_from_the_profile_and_the_clock(team: WorkspaceDb) -> None:
    with team.factory.begin() as session:
        user = session.get_one(User, team.users["editor"])
        user.birth_year = 1990
        user.life_expectancy = 95
    defaults = fire_scenarios.scenario_defaults(team.factory, team.ctx("editor", "owner"))
    assert (defaults.start_year, defaults.currency) == (2026, "EUR")
    assert (defaults.birth_year, defaults.life_expectancy) == (1990, 95)
    assert defaults.inflation_bp == 250
    assert defaults.real_return_bp == 500
    assert defaults.income_growth_bp == 300
    assert defaults.swr_bp == 400


def test_defaults_and_create_use_the_current_workspace_currency_not_the_stale_context(
    team: WorkspaceDb,
) -> None:
    with team.factory.begin() as session:
        session.get_one(Workspace, team.workspace_id).base_currency = "PLN"
    ctx = team.ctx("owner", "owner")  # still carries the old EUR value
    defaults = fire_scenarios.scenario_defaults(team.factory, ctx)
    assert defaults.currency == "PLN"

    scenario_id = fire_scenarios.create_fire_scenario(
        team.factory,
        ctx,
        _input(
            income_manual=_money(5_000_000, "PLN"),
            expenses_manual=_money(3_000_000, "PLN"),
            portfolio_manual=_money(100_000_000, "PLN"),
        ),
    )
    detail = fire_scenarios.get_fire_scenario(team.factory, ctx, scenario_id)
    assert detail.scenario.currency == "PLN"


def test_create_without_a_horizon_uses_the_profile_life_expectancy(team: WorkspaceDb) -> None:
    with team.factory.begin() as session:
        session.get_one(User, team.users["editor"]).life_expectancy = 95
    scenario_id = fire_scenarios.create_fire_scenario(
        team.factory, team.ctx("editor", "editor"), _input(horizon_age=None)
    )
    detail = fire_scenarios.get_fire_scenario(
        team.factory, team.ctx("editor", "editor"), scenario_id
    )
    assert detail.scenario.horizon_age == 95


def test_create_without_a_horizon_or_profile_value_uses_ninety(team: WorkspaceDb) -> None:
    scenario_id = fire_scenarios.create_fire_scenario(
        team.factory, team.ctx("editor", "editor"), _input(horizon_age=None)
    )
    detail = fire_scenarios.get_fire_scenario(
        team.factory, team.ctx("editor", "editor"), scenario_id
    )
    assert detail.scenario.horizon_age == 90


def test_update_without_a_horizon_keeps_the_saved_one(team: WorkspaceDb) -> None:
    scenario_id = _create(team, horizon_age=95)
    fire_scenarios.update_fire_scenario(
        team.factory,
        team.ctx("owner", "owner"),
        scenario_id,
        _input(horizon_age=None, stop_working_age=56),
    )
    detail = fire_scenarios.get_fire_scenario(team.factory, team.ctx("owner", "owner"), scenario_id)
    assert detail.scenario.horizon_age == 95


def test_fire_rows_make_the_workspace_non_empty(team: WorkspaceDb) -> None:
    ctx = team.ctx("owner", "owner")
    assert workspace_is_empty_direct(team, ctx) is True
    scenario_id = _create(
        team,
        contributions=(ContributionInput(year=2027, amount_minor=1),),
        events=(EventInput(year=2027, kind="cash", amount_minor=1, label="x"),),
    )
    assert workspace_is_empty_direct(team, ctx) is False
    with team.factory.begin() as session:
        session.delete(session.get_one(FireContribution, _child_id(team, FireContribution)))
        session.delete(session.get_one(FireEvent, _child_id(team, FireEvent)))
    assert workspace_is_empty_direct(team, ctx) is False
    with team.factory.begin() as session:
        session.delete(session.get_one(FireScenario, scenario_id))
    assert workspace_is_empty_direct(team, ctx) is True


def _child_id(team: WorkspaceDb, model):
    with team.factory() as session:
        return session.scalars(
            select(model.id).where(model.workspace_id == team.workspace_id)
        ).one()


def workspace_is_empty_direct(team: WorkspaceDb, ctx) -> bool:
    with team.factory() as session:
        return workspace_is_empty(session, ctx)


def test_audit_actions_cover_every_transition(team: WorkspaceDb) -> None:
    ctx = team.ctx("owner", "owner")
    first = _create(team, name="First")
    second = _create(team, name="Second")
    fire_scenarios.update_fire_scenario(
        team.factory, ctx, first, _input(name="First", stop_working_age=56)
    )
    fire_scenarios.set_default_fire_scenario(team.factory, ctx, second)
    fire_scenarios.archive_fire_scenario(team.factory, ctx, second)
    fire_scenarios.restore_fire_scenario(team.factory, ctx, second)
    fire_scenarios.duplicate_fire_scenario(team.factory, ctx, first)
    with team.factory() as session:
        actions = list(
            session.scalars(
                select(AuditEvent.action)
                .where(AuditEvent.action.like("fire_scenario.%"))
                .order_by(AuditEvent.ts, AuditEvent.action)
            )
        )
    assert set(actions) == {
        "fire_scenario.created",
        "fire_scenario.updated",
        "fire_scenario.duplicated",
        "fire_scenario.archived",
        "fire_scenario.restored",
        "fire_scenario.default_changed",
    }


def test_negative_contributions_and_events_are_kept_after_round_trip(team: WorkspaceDb) -> None:
    scenario_id = _create(
        team,
        contributions=(ContributionInput(year=2027, amount_minor=-1_234_56),),
        events=(
            EventInput(year=2028, kind="income_change", amount_minor=-250_000, label="Rent ends"),
        ),
    )
    detail = fire_scenarios.get_fire_scenario(team.factory, team.ctx("owner", "owner"), scenario_id)
    assert detail.contributions[0].amount.amount_minor == -1_234_56
    assert detail.events[0].amount.amount_minor == -250_000


def test_zero_contribution_is_a_valid_explicit_override(team: WorkspaceDb) -> None:
    scenario_id = _create(team, contributions=(ContributionInput(year=2027, amount_minor=0),))
    detail = fire_scenarios.get_fire_scenario(team.factory, team.ctx("owner", "owner"), scenario_id)
    assert [row.year for row in detail.contributions] == [2027]
    assert detail.contributions[0].amount.amount_minor == 0


def test_archived_at_uses_the_injected_clock(team: WorkspaceDb) -> None:
    ctx = team.ctx("owner", "owner")
    scenario_id = _create(team)
    fire_scenarios.archive_fire_scenario(team.factory, ctx, scenario_id)
    detail = fire_scenarios.get_fire_scenario(team.factory, ctx, scenario_id)
    assert detail.scenario.archived_at == datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def test_the_database_refuses_a_second_active_default(team: WorkspaceDb) -> None:
    from sqlalchemy.exc import IntegrityError

    from assetboard.db.repositories import FireScenarioRepository

    _create(team, name="Default")
    with (
        pytest.raises(IntegrityError),
        team.factory.begin() as session,
    ):
        FireScenarioRepository(session, team.ctx("owner", "owner")).create(
            name="Second default",
            name_key="second default",
            currency="EUR",
            birth_year=1985,
            start_year=2026,
            inflation_bp=250,
            real_return_bp=500,
            income_growth_bp=300,
            stop_working_age=55,
            horizon_age=90,
            rule="returns_cover_expenses",
            swr_bp=None,
            income_mode="manual",
            income_manual_minor=500_000,
            expenses_mode="manual",
            expenses_manual_minor=300_000,
            portfolio_mode="manual",
            portfolio_manual_minor=10_000_000,
            is_default=True,
            archived_at=None,
        )


def test_the_database_refuses_a_duplicate_active_name(team: WorkspaceDb) -> None:
    from sqlalchemy.exc import IntegrityError

    from assetboard.db.repositories import FireScenarioRepository

    _create(team, name="Retire at 55")
    with (
        pytest.raises(IntegrityError),
        team.factory.begin() as session,
    ):
        FireScenarioRepository(session, team.ctx("owner", "owner")).create(
            name="RETIRE AT 55",
            name_key="retire at 55",
            currency="EUR",
            birth_year=1985,
            start_year=2026,
            inflation_bp=250,
            real_return_bp=500,
            income_growth_bp=300,
            stop_working_age=55,
            horizon_age=90,
            rule="returns_cover_expenses",
            swr_bp=None,
            income_mode="manual",
            income_manual_minor=500_000,
            expenses_mode="manual",
            expenses_manual_minor=300_000,
            portfolio_mode="manual",
            portfolio_manual_minor=10_000_000,
            is_default=False,
            archived_at=None,
        )
