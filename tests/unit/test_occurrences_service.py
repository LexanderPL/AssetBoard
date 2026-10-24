from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from assetboard.clock import FixedClock
from assetboard.config import Settings
from assetboard.db.models.identity import AuditEvent
from assetboard.db.models.ledger import Transaction
from assetboard.db.repositories import CategoryRepository
from assetboard.db.repositories.scheduled import ScheduledOccurrenceRepository
from assetboard.domain.money import Money
from assetboard.domain.months import YearMonth
from assetboard.domain.recurrence import RecurrenceRule
from assetboard.domain.transactions import Source, TransactionDirection
from assetboard.services import occurrences, scheduled
from assetboard.services.months import close_month
from tests.tenant.factories import make_category
from tests.tenant.workspace_db import WorkspaceDb, workspace_db


@pytest.fixture
def team(tmp_path):
    for db in workspace_db(tmp_path):
        db.add_member("owner", "owner")
        yield db


def item_data(team: WorkspaceDb, *, auto_post: bool = False) -> scheduled.ScheduledInput:
    ctx = team.ctx("owner", "owner")
    with team.factory.begin() as session:
        category = CategoryRepository(session, ctx).add(make_category(session, team.workspace_id))
    return scheduled.ScheduledInput(
        name="Rent",
        direction=TransactionDirection.EXPENSE,
        category_id=category.id,
        amount=Money(10000, "EUR"),
        rule=RecurrenceRule(date(2026, 3, 1), month_day=1),
        auto_post=auto_post,
    )


def occurrence_rows(team: WorkspaceDb, item_id) -> list:
    with team.factory() as session:
        return ScheduledOccurrenceRepository(session, team.ctx("owner", "owner")).for_item(item_id)


def create_item(team: WorkspaceDb, *, auto_post: bool = False):
    return scheduled.save_item(
        team.factory,
        team.ctx("owner", "owner"),
        item_data(team, auto_post=auto_post),
        Settings(_env_file=None),
    )


def test_upcoming_filters_and_groups_with_overdue_rows(team):
    ctx = team.ctx("owner", "owner")
    item_id = scheduled.save_item(
        team.factory,
        ctx,
        replace(item_data(team), rule=RecurrenceRule(date(2026, 1, 1), month_day=1)),
        Settings(_env_file=None),
        include_past=True,
    )

    weeks = occurrences.upcoming(team.factory, ctx, days=30)

    listed = [row for week in weeks for row in week.occurrences]
    assert [row.due_date for row in listed] == [
        date(2026, 1, 1),
        date(2026, 2, 1),
        date(2026, 3, 1),
    ]
    assert all(row.scheduled_item_id == item_id for row in listed)
    assert weeks[-1].monday == date(2026, 2, 23)
    assert occurrences.upcoming(team.factory, ctx, days=30, direction="income") == []
    assert occurrences.upcoming(team.factory, ctx, days=30, category_id=listed[0].category_id)
    with pytest.raises(ValueError, match="invalid_days"):
        occurrences.upcoming(team.factory, ctx, days=0)


def test_post_is_idempotent_links_transaction_and_undo_soft_deletes(team):
    ctx = team.ctx("owner", "owner")
    item_id = create_item(team)
    occurrence = occurrence_rows(team, item_id)[0]

    first = occurrences.post_occurrence(
        team.factory,
        ctx,
        occurrence.id,
        amount_minor=12345,
        day=date(2026, 3, 1),
    )
    second = occurrences.post_occurrence(
        team.factory,
        ctx,
        occurrence.id,
        amount_minor=12345,
        day=date(2026, 3, 1),
    )
    assert first == second
    with team.factory() as session:
        transaction = session.get(Transaction, first)
        stored = ScheduledOccurrenceRepository(session, ctx).get_or_raise(occurrence.id)
        assert transaction is not None
        assert transaction.source == Source.RECURRING
        assert transaction.amount_minor == 12345
        assert stored.status == "posted" and stored.transaction_id == first
        assert len(list(session.scalars(select(Transaction)))) == 1

    assert occurrences.recently_posted(team.factory, ctx, occurrence.id) is not None
    occurrences.undo_post(team.factory, ctx, occurrence.id)
    with team.factory() as session:
        transaction = session.get(Transaction, first)
        stored = ScheduledOccurrenceRepository(session, ctx).get_or_raise(occurrence.id)
        assert transaction is not None and transaction.deleted_at == ctx.clock.now()
        assert stored.status == "pending" and stored.transaction_id is None
        actions = set(session.scalars(select(AuditEvent.action)))
        assert {
            "transaction.created",
            "transaction.deleted",
            "scheduled_occurrence.post_undone",
        } <= actions
    assert occurrences.recently_posted(team.factory, ctx, occurrence.id) is None


def test_undo_expires_after_ten_seconds(team):
    ctx = team.ctx("owner", "owner")
    item_id = create_item(team)
    occurrence = occurrence_rows(team, item_id)[0]
    occurrences.post_occurrence(
        team.factory, ctx, occurrence.id, amount_minor=10000, day=occurrence.due_date
    )
    later = replace(ctx, clock=FixedClock((ctx.clock.now() + timedelta(seconds=11)).isoformat()))
    with pytest.raises(occurrences.UndoExpired):
        occurrences.undo_post(team.factory, later, occurrence.id)


def test_reschedule_and_amount_override_survive_rule_refresh(team):
    ctx = team.ctx("owner", "owner")
    values = item_data(team)
    item_id = scheduled.save_item(team.factory, ctx, values, Settings(_env_file=None))
    occurrence = occurrence_rows(team, item_id)[0]

    occurrences.reschedule_occurrence(team.factory, ctx, occurrence.id, date(2026, 3, 5))
    occurrences.edit_occurrence_amount(team.factory, ctx, occurrence.id, 25000)
    scheduled.save_item(
        team.factory,
        ctx,
        replace(values, amount=Money(20000, "EUR")),
        Settings(_env_file=None),
        item_id=item_id,
    )

    updated = next(row for row in occurrence_rows(team, item_id) if row.id == occurrence.id)
    assert updated.nominal_date == date(2026, 3, 1)
    assert updated.due_date == date(2026, 3, 5)
    assert updated.expected_minor == 25000
    assert updated.amount_overridden and updated.rescheduled_at is not None


def test_auto_post_uses_workspace_clock_and_closed_month_guard(team):
    ctx = team.ctx("owner", "owner")
    item_id = create_item(team, auto_post=True)
    occurrence = occurrence_rows(team, item_id)[0]

    assert occurrences.auto_post_workspace(team.factory, ctx, team.clock) == 1
    with team.factory() as session:
        transaction = session.scalars(select(Transaction)).one()
        stored = ScheduledOccurrenceRepository(session, ctx).get_or_raise(occurrence.id)
        assert transaction.source == Source.RECURRING_AUTO
        assert transaction.date == date(2026, 3, 1)
        assert stored.status == "posted" and stored.transaction_id == transaction.id
    assert occurrences.auto_post_workspace(team.factory, ctx, team.clock) == 0

    later_clock = FixedClock("2026-04-01T12:00:00+00:00")
    team.clock = later_clock
    team.factory.configure(info={"clock": later_clock})
    later_ctx = replace(ctx, clock=later_clock)
    later_item = scheduled.save_item(
        team.factory,
        later_ctx,
        replace(
            item_data(team, auto_post=True),
            rule=RecurrenceRule(date(2026, 4, 1), month_day=1),
        ),
        Settings(_env_file=None),
    )
    close_month(team.factory, later_ctx, YearMonth(2026, 4))
    assert occurrences.auto_post_workspace(team.factory, later_ctx, team.clock) == 0
    assert all(row.status == "pending" for row in occurrence_rows(team, later_item))
