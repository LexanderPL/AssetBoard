from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from assetboard.clock import FixedClock
from assetboard.config import Settings
from assetboard.db.models.identity import AuditEvent, Membership, Workspace
from assetboard.db.repositories import CategoryRepository, NotFound
from assetboard.db.repositories.scheduled import (
    ScheduledOccurrenceRepository,
)
from assetboard.domain.money import Money
from assetboard.domain.months import YearMonth
from assetboard.domain.recurrence import Frequency, MonthDay, RecurrenceRule, WeekendPolicy
from assetboard.domain.scheduled import ItemAction
from assetboard.domain.transactions import TransactionDirection
from assetboard.services import occurrences, scheduled
from assetboard.services.categories import (
    KindChangeBlocked,
    archive_category,
    delete_category,
    update_category,
)
from assetboard.services.context import PermissionDenied
from assetboard.services.months import close_month
from assetboard.services.workspaces import (
    BaseCurrencyLocked,
    InvalidWorkspaceSettings,
    update_settings,
)
from tests.tenant.factories import make_category
from tests.tenant.workspace_db import WorkspaceDb, workspace_db


@pytest.fixture
def db(tmp_path):
    yield from workspace_db(tmp_path)


@pytest.fixture
def team(db: WorkspaceDb):
    db.add_member("owner", "owner")
    db.add_member("viewer", "viewer")
    return db


def data(db: WorkspaceDb, **changes):
    with db.factory.begin() as session:
        category = CategoryRepository(session, db.ctx("owner", "owner")).add(
            make_category(session, db.workspace_id)
        )
    return replace(
        scheduled.ScheduledInput(
            "Rent",
            TransactionDirection.EXPENSE,
            category.id,
            Money(10000, "EUR"),
            RecurrenceRule(date(2026, 1, 1), month_day=1),
        ),
        **changes,
    )


def rows(db, item_id):
    with db.factory() as session:
        return ScheduledOccurrenceRepository(session, db.ctx("owner", "owner")).for_item(item_id)


def test_generator_horizon_past_opt_in_and_idempotence(team):
    settings = Settings(_env_file=None)
    ctx = team.ctx("owner", "owner")
    item = scheduled.save_item(team.factory, ctx, data(team), settings)
    assert [row.due_date for row in rows(team, item)] == [
        date(2026 + month // 12, month % 12 + 1, 1) for month in range(2, 16)
    ]
    assert scheduled.generate_workspace(team.factory, ctx, team.clock, settings) == 0
    past = scheduled.save_item(team.factory, ctx, data(team), settings, include_past=True)
    assert rows(team, past)[0].due_date == date(2026, 1, 1)
    limited = scheduled.save_item(
        team.factory, ctx, data(team), settings.model_copy(update={"recurrence_horizon_months": 1})
    )
    assert [row.due_date for row in rows(team, limited)] == [date(2026, 3, 1), date(2026, 4, 1)]
    assert scheduled.get_item(team.factory, ctx, item).name == "Rent"
    assert len(scheduled.list_items(team.factory, team.ctx("viewer", "viewer"))) == 3


def test_edit_preserves_history_posted_and_skipped(team):
    ctx = team.ctx("owner", "owner")
    settings = Settings(_env_file=None)
    values = data(team)
    item = scheduled.save_item(team.factory, ctx, values, settings, include_past=True)
    before = rows(team, item)
    with team.factory.begin() as session:
        repository = ScheduledOccurrenceRepository(session, ctx)
        repository.update(before[3].id, status="posted")
        repository.update(before[4].id, status="skipped")
    scheduled.save_item(
        team.factory,
        ctx,
        replace(
            values,
            name="New rent",
            amount=Money(12000, "EUR"),
            rule=replace(values.rule, month_day=15),
        ),
        settings,
        item_id=item,
        include_past=True,
    )
    after = rows(team, item)
    preserved = {row.id: row for row in after}
    assert all(row.id in preserved for row in (before[0], before[1], before[3], before[4]))
    assert before[2].id not in preserved
    assert all(
        preserved[row.id].expected_minor == 10000
        for row in (before[0], before[1], before[3], before[4])
    )
    assert any(row.expected_minor == 12000 and row.due_date == date(2026, 3, 15) for row in after)


def test_pause_resume_archive_and_delete_history(team):
    ctx, settings = team.ctx("owner", "owner"), Settings(_env_file=None)
    item = scheduled.save_item(team.factory, ctx, data(team), settings, include_past=True)
    scheduled.change_state(team.factory, ctx, item, ItemAction.PAUSE, settings)
    assert [row.due_date for row in rows(team, item)] == [date(2026, 1, 1), date(2026, 2, 1)]
    assert scheduled.generate_workspace(team.factory, ctx, team.clock, settings) == 0
    scheduled.change_state(team.factory, ctx, item, ItemAction.RESUME, settings)
    assert len(rows(team, item)) > 2
    posted = rows(team, item)[1]
    with team.factory.begin() as session:
        ScheduledOccurrenceRepository(session, ctx).update(posted.id, status="posted")
    scheduled.change_state(team.factory, ctx, item, ItemAction.DELETE, settings)
    assert scheduled.list_items(team.factory, ctx)[0].archived
    # Only history remains: the overdue pending January row went with the item.
    assert [row.id for row in rows(team, item)] == [posted.id]
    with team.factory() as session:
        actions = set(
            session.scalars(select(AuditEvent.action).where(AuditEvent.entity_id == str(item)))
        )
        assert "scheduled.archive" in actions
        assert "scheduled.delete" not in actions
    with pytest.raises(ValueError):
        scheduled.change_state(team.factory, ctx, item, ItemAction.RESUME, settings)
    empty = scheduled.save_item(team.factory, ctx, data(team), settings)
    scheduled.change_state(team.factory, ctx, empty, ItemAction.ARCHIVE, settings)
    assert rows(team, empty) == []
    fresh = scheduled.save_item(team.factory, ctx, data(team), settings)
    scheduled.change_state(team.factory, ctx, fresh, ItemAction.DELETE, settings)
    with pytest.raises(NotFound):
        scheduled.get_item(team.factory, ctx, fresh)
    with team.factory() as session:
        actions = set(
            session.scalars(select(AuditEvent.action).where(AuditEvent.entity_id == str(fresh)))
        )
        assert "scheduled.delete" in actions


def test_delete_with_only_past_pending_rows_deletes_the_item(team):
    ctx, settings = team.ctx("owner", "owner"), Settings(_env_file=None)
    item = scheduled.save_item(team.factory, ctx, data(team), settings, include_past=True)
    assert rows(team, item)[0].due_date == date(2026, 1, 1)
    scheduled.change_state(team.factory, ctx, item, ItemAction.DELETE, settings)
    with pytest.raises(NotFound):
        scheduled.get_item(team.factory, ctx, item)
    assert rows(team, item) == []


def test_archive_removes_past_pending_rows_and_keeps_skipped(team):
    ctx, settings = team.ctx("owner", "owner"), Settings(_env_file=None)
    item = scheduled.save_item(team.factory, ctx, data(team), settings, include_past=True)
    skipped = rows(team, item)[0]
    with team.factory.begin() as session:
        ScheduledOccurrenceRepository(session, ctx).update(skipped.id, status="skipped")
    scheduled.change_state(team.factory, ctx, item, ItemAction.ARCHIVE, settings)
    assert [row.id for row in rows(team, item)] == [skipped.id]


def test_roles_validation_and_tenant_isolation(team):
    ctx, settings = team.ctx("owner", "owner"), Settings(_env_file=None)
    values = data(team)
    with pytest.raises(PermissionDenied):
        scheduled.save_item(team.factory, team.ctx("viewer", "viewer"), values, settings)
    with team.factory.begin() as session:
        member = session.get(Membership, team.memberships["viewer"])
        member.role = "editor"
    stale = team.ctx("viewer", "editor")
    with team.factory.begin() as session:
        session.get(Membership, team.memberships["viewer"]).role = "viewer"
    with pytest.raises(PermissionDenied):
        scheduled.save_item(team.factory, stale, values, settings)
    for changed in (
        replace(values, name=""),
        replace(values, amount=Money(1, "PLN")),
        replace(values, direction=TransactionDirection.INCOME),
    ):
        with pytest.raises(ValueError):
            scheduled.save_item(team.factory, ctx, changed, settings)
    other = team.add_workspace("Other")
    team.add_member("owner", "owner", other)
    other_ctx = team.ctx("owner", "owner", other)
    item = scheduled.save_item(team.factory, ctx, values, settings)
    assert scheduled.list_items(team.factory, other_ctx) == []
    for action in (
        lambda: scheduled.get_item(team.factory, other_ctx, item),
        lambda: scheduled.save_item(team.factory, other_ctx, values, settings),
        lambda: scheduled.change_state(team.factory, other_ctx, item, ItemAction.PAUSE, settings),
    ):
        with pytest.raises(NotFound):
            action()


def test_country_holidays_timezone_and_category_currency_guards(team):
    ctx, settings = team.ctx("owner", "owner"), Settings(_env_file=None)
    with team.factory.begin() as session:
        workspace = session.get(Workspace, team.workspace_id)
        workspace.country_code, workspace.timezone = "PL", "Europe/Warsaw"
    clock = FixedClock("2026-04-30T23:30:00+00:00")
    ctx = replace(ctx, clock=clock)
    values = data(team, rule=RecurrenceRule(date(2026, 5, 1), weekend_policy=WeekendPolicy.NEXT))
    assert scheduled.preview(team.factory, ctx, values.rule)[0] == date(2026, 5, 4)
    item = scheduled.save_item(team.factory, ctx, values, settings)
    assert rows(team, item)[0].due_date == date(2026, 5, 4)
    with pytest.raises(KindChangeBlocked):
        update_category(
            team.factory,
            ctx,
            values.category_id,
            name="Other",
            kind="income",
            treatment="regular",
            color="#16a34a",
        )
    assert delete_category(team.factory, ctx, values.category_id) == "archived"
    with pytest.raises(ValueError):
        scheduled.save_item(team.factory, ctx, values, settings)
    with pytest.raises(BaseCurrencyLocked):
        update_settings(
            team.factory,
            ctx,
            name="Home",
            base_currency="PLN",
            country_code="PL",
            timezone="Europe/Warsaw",
            locale="en",
            auto_copy_plan=False,
        )
    with team.factory() as session:
        assert scheduled.category_usage(session, ctx)[values.category_id].references > 1
        assert scheduled.workspace_has_scheduled_amounts(session, ctx)
        assert {event.action for event in session.scalars(select(AuditEvent))} >= {
            "scheduled.created"
        }


def test_missing_and_unsupported_country_holiday_rules(team):
    ctx = team.ctx("owner", "owner")
    rule = RecurrenceRule(date(2026, 5, 1), weekend_policy=WeekendPolicy.NEXT)
    assert scheduled.preview(team.factory, ctx, rule)[0] == date(2026, 5, 1)
    with team.factory.begin() as session:
        session.get(Workspace, team.workspace_id).country_code = "ZZ"
    with pytest.raises(ValueError, match="unsupported_holidays"):
        scheduled.preview(team.factory, ctx, rule)
    with pytest.raises(ValueError, match="unsupported_holidays"):
        scheduled.preview(
            team.factory,
            ctx,
            replace(rule, month_day=MonthDay.FIRST_WORKING, weekend_policy=WeekendPolicy.NONE),
        )
    assert scheduled.preview(team.factory, ctx, replace(rule, weekend_policy=WeekendPolicy.NONE))[
        0
    ] == date(2026, 5, 1)
    weekly = replace(
        rule,
        freq=Frequency.WEEKLY,
        weekdays=(0,),
        month_day=MonthDay.FIRST_WORKING,
        weekend_policy=WeekendPolicy.NONE,
    )
    assert scheduled.preview(team.factory, ctx, weekly)[0] == date(2026, 5, 4)


def test_simultaneous_generation_never_duplicates(team):
    ctx, settings = team.ctx("owner", "owner"), Settings(_env_file=None)
    item = scheduled.save_item(team.factory, ctx, data(team), settings)
    later = FixedClock("2026-04-01T12:00:00+00:00")
    with ThreadPoolExecutor(max_workers=2) as pool:
        counts = list(
            pool.map(
                lambda _: scheduled.generate_workspace(team.factory, ctx, later, settings), range(2)
            )
        )
    assert sorted(counts) == [0, 1]
    dates = [row.due_date for row in rows(team, item)]
    assert len(dates) == len(set(dates))


# Regeneration when the calendar changes (review of #128)

OCTOBER = FixedClock("2026-10-24T12:00:00+00:00")


def working_day_values(db):
    return data(db, rule=RecurrenceRule(date(2026, 1, 1), weekend_policy=WeekendPolicy.NEXT))


def settings_with_country(db, ctx, country):
    return update_settings(
        db.factory,
        ctx,
        name="Home",
        base_currency="EUR",
        country_code=country,
        timezone="UTC",
        locale="en",
        auto_copy_plan=False,
    )


def test_country_change_regenerates_working_day_items_without_duplicates(team):
    ctx, settings = replace(team.ctx("owner", "owner"), clock=OCTOBER), Settings(_env_file=None)
    item = scheduled.save_item(team.factory, ctx, working_day_values(team), settings)
    assert len(rows(team, item)) == 13
    settings_with_country(team, ctx, "PL")
    dates = [row.due_date for row in rows(team, item)]
    assert len(dates) == 13
    # Polish holidays apply at once: 1 January 2027 is a Friday holiday, so the 4th.
    assert date(2027, 1, 4) in dates and date(2027, 1, 1) not in dates
    assert scheduled.generate_workspace(team.factory, ctx, OCTOBER, settings) == 0
    assert len(rows(team, item)) == 13


def test_country_changed_outside_the_form_is_fixed_by_the_next_run(team):
    ctx, settings = replace(team.ctx("owner", "owner"), clock=OCTOBER), Settings(_env_file=None)
    item = scheduled.save_item(team.factory, ctx, working_day_values(team), settings)
    with team.factory.begin() as session:
        session.get(Workspace, team.workspace_id).country_code = "PL"
    scheduled.generate_workspace(team.factory, ctx, OCTOBER, settings)
    dates = [row.due_date for row in rows(team, item)]
    assert len(dates) == 13 == len({(day.year, day.month) for day in dates})


def test_holiday_data_change_never_duplicates(team, monkeypatch):
    ctx, settings = replace(team.ctx("owner", "owner"), clock=OCTOBER), Settings(_env_file=None)
    item = scheduled.save_item(team.factory, ctx, working_day_values(team), settings)
    before = {row.due_date for row in rows(team, item)}
    # New holiday data: every first-of-month working day (and the 2nd) is now a holiday.
    changed = frozenset(day + timedelta(days=extra) for day in before for extra in (0, 1))
    monkeypatch.setattr(scheduled, "_holidays", lambda country, rule: changed)
    scheduled.generate_workspace(team.factory, ctx, OCTOBER, settings)
    after = rows(team, item)
    dates = [row.due_date for row in after]
    assert len(dates) == len(set(dates)) == 13
    assert set(dates).isdisjoint(changed)
    assert scheduled.generate_workspace(team.factory, ctx, OCTOBER, settings) == 0


def test_country_without_holiday_data_is_refused_while_working_day_items_exist(team):
    ctx, settings = team.ctx("owner", "owner"), Settings(_env_file=None)
    plain = scheduled.save_item(team.factory, ctx, data(team), settings)
    settings_with_country(team, ctx, "AC")  # Ascension Island: no holiday data
    settings_with_country(team, ctx, None)
    scheduled.save_item(team.factory, ctx, working_day_values(team), settings)
    scheduled.change_state(team.factory, ctx, plain, ItemAction.PAUSE, settings)
    with pytest.raises(InvalidWorkspaceSettings) as refused:
        settings_with_country(team, ctx, "AC")
    assert refused.value.code == "country_without_holidays"
    with team.factory() as session:
        assert session.get(Workspace, team.workspace_id).country_code is None


# Archived categories


def test_generator_skips_items_whose_category_is_archived(team):
    ctx, settings = team.ctx("owner", "owner"), Settings(_env_file=None)
    values = data(team)
    item = scheduled.save_item(team.factory, ctx, values, settings, include_past=True)
    archive_category(team.factory, ctx, values.category_id)
    later = FixedClock("2026-05-10T12:00:00+00:00")
    assert scheduled.generate_workspace(team.factory, ctx, later, settings) == 0
    # Past rows stay; pending rows from the run's today onward are gone, none are added.
    assert [row.due_date for row in rows(team, item)] == [
        date(2026, month, 1) for month in range(1, 6)
    ]


# Closed months (F2.5)


def close(db, ctx, year, month):
    close_month(db.factory, ctx, YearMonth(year, month))


def test_backfill_skips_closed_months(team):
    ctx, settings = team.ctx("owner", "owner"), Settings(_env_file=None)
    close(team, ctx, 2026, 2)
    item = scheduled.save_item(team.factory, ctx, data(team), settings, include_past=True)
    dates = [row.due_date for row in rows(team, item)]
    assert date(2026, 1, 1) in dates and date(2026, 2, 1) not in dates


def test_pause_and_edit_leave_closed_month_rows_alone(team):
    ctx, settings = team.ctx("owner", "owner"), Settings(_env_file=None)
    values = data(team)
    item = scheduled.save_item(team.factory, ctx, values, settings)
    march = rows(team, item)[0]
    assert march.due_date == date(2026, 3, 1)
    close(team, ctx, 2026, 3)
    scheduled.save_item(
        team.factory,
        ctx,
        replace(values, amount=Money(12000, "EUR"), rule=replace(values.rule, month_day=2)),
        settings,
        item_id=item,
    )
    after = rows(team, item)
    assert after[0].id == march.id and after[0].expected_minor == 10000
    assert after[1].due_date == date(2026, 4, 2) and after[1].expected_minor == 12000
    assert date(2026, 3, 2) not in {row.due_date for row in after}
    scheduled.change_state(team.factory, ctx, item, ItemAction.PAUSE, settings)
    assert [row.id for row in rows(team, item)] == [march.id]


def test_archive_and_delete_keep_closed_month_rows(team):
    ctx, settings = team.ctx("owner", "owner"), Settings(_env_file=None)
    item = scheduled.save_item(team.factory, ctx, data(team), settings, include_past=True)
    january = rows(team, item)[0]
    close(team, ctx, 2026, 1)
    scheduled.change_state(team.factory, ctx, item, ItemAction.DELETE, settings)
    # The closed January row is history now, so Delete archives instead.
    assert scheduled.list_items(team.factory, ctx)[0].archived
    assert [row.id for row in rows(team, item)] == [january.id]
    other = scheduled.save_item(team.factory, ctx, data(team), settings)
    close(team, ctx, 2026, 3)
    scheduled.change_state(team.factory, ctx, other, ItemAction.ARCHIVE, settings)
    assert [row.due_date for row in rows(team, other)] == [date(2026, 3, 1)]


def test_regeneration_leaves_closed_month_rows_alone(team, monkeypatch):
    ctx, settings = team.ctx("owner", "owner"), Settings(_env_file=None)
    item = scheduled.save_item(team.factory, ctx, working_day_values(team), settings)
    march = rows(team, item)[0]
    close(team, ctx, 2026, 3)
    monkeypatch.setattr(scheduled, "_holidays", lambda country, rule: frozenset({march.due_date}))
    scheduled.generate_workspace(team.factory, ctx, team.clock, settings)
    after = rows(team, item)
    assert after[0].id == march.id
    assert [row.due_date for row in after if row.due_date < date(2026, 4, 1)] == [march.due_date]


# Failure isolation


def test_one_failing_item_does_not_stop_the_workspace_run(team, monkeypatch, caplog):
    ctx, settings = team.ctx("owner", "owner"), Settings(_env_file=None)
    bad = scheduled.save_item(team.factory, ctx, data(team, name="Bad"), settings)
    good = scheduled.save_item(team.factory, ctx, data(team, name="Good"), settings)
    original = scheduled._generate

    def failing(session, scope, clock, item, *args, **kwargs):
        if item.name == "Bad":
            raise RuntimeError("amount 12345")
        return original(session, scope, clock, item, *args, **kwargs)

    monkeypatch.setattr(scheduled, "_generate", failing)
    later = FixedClock("2026-06-15T12:00:00+00:00")
    with caplog.at_level(logging.ERROR, logger="assetboard.services.scheduled"):
        assert scheduled.generate_workspace(team.factory, ctx, later, settings) == 3
    assert max(row.due_date for row in rows(team, good)) > max(
        row.due_date for row in rows(team, bad)
    )
    [record] = caplog.records
    assert str(bad) in record.getMessage() and "12345" not in record.getMessage()
    assert record.exc_info is not None


def one_time(day: date, **changes) -> RecurrenceRule:
    return RecurrenceRule(day, Frequency.ONCE, **changes)


def test_one_time_item_is_created_normalised_and_regenerated_on_a_date_edit(team):
    ctx = team.ctx("owner", "owner")
    settings = Settings(_env_file=None)
    values = data(
        team,
        rule=one_time(
            date(2026, 5, 2),  # a Saturday
            interval=4,
            max_count=3,
            end_date=date(2026, 6, 1),
            weekend_policy=WeekendPolicy.NEXT,
        ),
    )
    item = scheduled.save_item(team.factory, ctx, values, settings)
    stored = scheduled.get_item(team.factory, ctx, item).rule
    assert stored == one_time(date(2026, 5, 2), weekend_policy=WeekendPolicy.NEXT)
    [row] = rows(team, item)
    assert (row.nominal_date, row.due_date, row.status) == (
        date(2026, 5, 2),
        date(2026, 5, 4),
        "pending",
    )
    scheduled.save_item(
        team.factory, ctx, replace(values, rule=one_time(date(2026, 7, 15))), settings, item_id=item
    )
    assert [(r.nominal_date, r.due_date) for r in rows(team, item)] == [
        (date(2026, 7, 15), date(2026, 7, 15))
    ]
    assert scheduled.generate_workspace(team.factory, ctx, team.clock, settings) == 0
    [view] = scheduled.list_items(team.factory, ctx)
    assert not view.done


def test_one_time_item_waits_for_the_horizon_and_follows_include_past(team):
    ctx = team.ctx("owner", "owner")
    settings = Settings(_env_file=None).model_copy(update={"recurrence_horizon_months": 2})
    later = scheduled.save_item(
        team.factory, ctx, data(team, rule=one_time(date(2026, 6, 1))), settings
    )
    assert rows(team, later) == []
    april = FixedClock("2026-04-01T12:00:00+00:00")  # 1 June is inside the horizon now
    assert scheduled.generate_workspace(team.factory, ctx, april, settings) == 1
    assert [row.due_date for row in rows(team, later)] == [date(2026, 6, 1)]
    past = data(team, rule=one_time(date(2026, 2, 10)))
    skipped = scheduled.save_item(team.factory, ctx, past, settings)
    assert rows(team, skipped) == []
    included = scheduled.save_item(team.factory, ctx, past, settings, include_past=True)
    assert [row.due_date for row in rows(team, included)] == [date(2026, 2, 10)]


def test_posting_a_one_time_occurrence_marks_the_item_done(team):
    ctx = team.ctx("owner", "owner")
    settings = Settings(_env_file=None)
    values = data(team, rule=one_time(date(2026, 3, 1)))
    item = scheduled.save_item(team.factory, ctx, values, settings)
    [row] = rows(team, item)
    occurrences.post_occurrence(team.factory, ctx, row.id, amount_minor=10000, day=row.due_date)
    [view] = scheduled.list_items(team.factory, ctx)
    assert view.done and not view.archived
    # A date edit after posting adds no second occurrence.
    scheduled.save_item(
        team.factory, ctx, replace(values, rule=one_time(date(2026, 9, 1))), settings, item_id=item
    )
    assert [r.status for r in rows(team, item)] == ["posted"]
    scheduled.change_state(team.factory, ctx, item, ItemAction.ARCHIVE, settings)
    assert scheduled.list_items(team.factory, ctx)[0].archived
