from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from assetboard.clock import FixedClock
from assetboard.config import Settings
from assetboard.db.models.identity import AuditEvent
from assetboard.db.models.ledger import Transaction
from assetboard.db.repositories import CategoryRepository, NotFound
from assetboard.db.repositories.scheduled import ScheduledOccurrenceRepository
from assetboard.domain.money import Money
from assetboard.domain.months import YearMonth
from assetboard.domain.recurrence import RecurrenceRule
from assetboard.domain.scheduled import ItemAction
from assetboard.domain.transactions import Source, TransactionDirection
from assetboard.services import occurrences, scheduled, transactions
from assetboard.services.context import PermissionDenied
from assetboard.services.months import MonthClosed, close_month
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


def test_occurrence_actions_require_a_current_editor(team):
    team.add_member("viewer", "viewer")
    item_id = create_item(team)
    occurrence = occurrence_rows(team, item_id)[0]
    actions = (
        lambda ctx: occurrences.post_occurrence(
            team.factory, ctx, occurrence.id, amount_minor=100, day=occurrence.due_date
        ),
        lambda ctx: occurrences.skip_occurrence(team.factory, ctx, occurrence.id),
        lambda ctx: occurrences.reschedule_occurrence(
            team.factory, ctx, occurrence.id, date(2026, 3, 9)
        ),
        lambda ctx: occurrences.edit_occurrence_amount(team.factory, ctx, occurrence.id, 100),
        lambda ctx: occurrences.undo_post(team.factory, ctx, occurrence.id),
    )
    # A viewer, and a viewer whose request still carries a stale editor role.
    for ctx in (team.ctx("viewer", "viewer"), team.ctx("viewer", "editor")):
        for action in actions:
            with pytest.raises(PermissionDenied):
                action(ctx)
    stored = occurrence_rows(team, item_id)[0]
    assert stored.status == "pending" and stored.due_date == occurrence.due_date
    assert stored.expected_minor == occurrence.expected_minor
    with team.factory() as session:
        assert session.scalars(select(Transaction)).all() == []


def test_concurrent_double_submit_creates_one_transaction(team):
    ctx = team.ctx("owner", "owner")
    item_id = create_item(team)
    occurrence = occurrence_rows(team, item_id)[0]
    barrier = threading.Barrier(2)

    def submit() -> object:
        barrier.wait()
        return occurrences.post_occurrence(
            team.factory, ctx, occurrence.id, amount_minor=10000, day=occurrence.due_date
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: submit(), range(2)))
    assert results[0] == results[1]
    with team.factory() as session:
        assert len(session.scalars(select(Transaction)).all()) == 1


def test_posted_occurrence_is_immune_to_rule_edits(team):
    ctx = team.ctx("owner", "owner")
    values = item_data(team)
    item_id = scheduled.save_item(team.factory, ctx, values, Settings(_env_file=None))
    occurrence = occurrence_rows(team, item_id)[0]
    transaction_id = occurrences.post_occurrence(
        team.factory, ctx, occurrence.id, amount_minor=9900, day=date(2026, 3, 2)
    )

    scheduled.save_item(
        team.factory,
        ctx,
        replace(
            values,
            name="New rent",
            amount=Money(20000, "EUR"),
            rule=RecurrenceRule(date(2026, 3, 1), month_day=15),
        ),
        Settings(_env_file=None),
        item_id=item_id,
    )

    rows = occurrence_rows(team, item_id)
    posted = next(row for row in rows if row.id == occurrence.id)
    assert posted.status == "posted" and posted.transaction_id == transaction_id
    assert (posted.nominal_date, posted.due_date) == (date(2026, 3, 1), date(2026, 3, 1))
    assert (posted.label, posted.expected_minor) == ("Rent", 10000)
    assert all(row.due_date.day == 15 for row in rows if row.status == "pending")
    with team.factory() as session:
        transaction = session.get(Transaction, transaction_id)
        assert transaction is not None and transaction.deleted_at is None
        assert (transaction.amount_minor, transaction.date) == (9900, date(2026, 3, 2))


def test_rescheduled_overdue_occurrence_survives_generation(team):
    ctx = team.ctx("owner", "owner")
    values = replace(item_data(team), rule=RecurrenceRule(date(2026, 1, 1), month_day=1))
    settings = Settings(_env_file=None)
    item_id = scheduled.save_item(team.factory, ctx, values, settings, include_past=True)
    january = next(row for row in occurrence_rows(team, item_id) if row.due_date.month == 1)

    occurrences.reschedule_occurrence(team.factory, ctx, january.id, date(2026, 3, 10))
    scheduled.generate_workspace(team.factory, ctx, team.clock, settings)
    scheduled.save_item(team.factory, ctx, replace(values, name="Flat"), settings, item_id=item_id)

    rows = occurrence_rows(team, item_id)
    moved = next(row for row in rows if row.id == january.id)
    assert (moved.nominal_date, moved.due_date) == (date(2026, 1, 1), date(2026, 3, 10))
    assert moved.status == "pending" and moved.label == "Flat"
    assert len({row.nominal_date for row in rows}) == len(rows)


def test_auto_post_ignores_paused_items(team):
    ctx = team.ctx("owner", "owner")
    item_id = scheduled.save_item(
        team.factory,
        ctx,
        replace(
            item_data(team, auto_post=True),
            rule=RecurrenceRule(date(2026, 1, 1), month_day=1),
        ),
        Settings(_env_file=None),
        include_past=True,
    )
    scheduled.change_state(team.factory, ctx, item_id, ItemAction.PAUSE, Settings(_env_file=None))
    assert [row.due_date for row in occurrence_rows(team, item_id)] == [
        date(2026, 1, 1),
        date(2026, 2, 1),
    ]

    assert occurrences.auto_post_workspace(team.factory, ctx, team.clock) == 0
    assert all(row.status == "pending" for row in occurrence_rows(team, item_id))


# Deleting a posted transaction from the ledger (F3.2 fix)


def post_first(team: WorkspaceDb, *, auto_post: bool = False):
    ctx = team.ctx("owner", "owner")
    item_id = create_item(team, auto_post=auto_post)
    occurrence = occurrence_rows(team, item_id)[0]
    if auto_post:
        assert occurrences.auto_post_workspace(team.factory, ctx, team.clock) == 1
        transaction_id = occurrence_rows(team, item_id)[0].transaction_id
        assert transaction_id is not None
    else:
        transaction_id = occurrences.post_occurrence(
            team.factory, ctx, occurrence.id, amount_minor=12345, day=occurrence.due_date
        )
    return item_id, occurrence, transaction_id


def test_ledger_delete_returns_occurrence_to_pending_and_it_can_be_posted_again(team):
    ctx = team.ctx("owner", "owner")
    item_id = create_item(team)
    occurrence = occurrence_rows(team, item_id)[0]
    occurrences.reschedule_occurrence(team.factory, ctx, occurrence.id, date(2026, 3, 5))
    occurrences.edit_occurrence_amount(team.factory, ctx, occurrence.id, 25000)
    first = occurrences.post_occurrence(
        team.factory, ctx, occurrence.id, amount_minor=25000, day=date(2026, 3, 5)
    )

    transactions.delete_transaction(team.factory, ctx, first)

    stored = occurrence_rows(team, item_id)[0]
    assert stored.status == "pending" and stored.transaction_id is None
    assert stored.previous_transaction_id == first
    assert stored.nominal_date == date(2026, 3, 1) and stored.due_date == date(2026, 3, 5)
    assert stored.expected_minor == 25000 and stored.amount_overridden
    listed = [
        row.id
        for week in occurrences.upcoming(team.factory, ctx, days=30)
        for row in week.occurrences
    ]
    assert occurrence.id in listed
    with team.factory() as session:
        events = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "scheduled_occurrence.unposted")
        ).all()
        assert [event.entity_id for event in events] == [str(occurrence.id)]

    second = occurrences.post_occurrence(
        team.factory, ctx, occurrence.id, amount_minor=25000, day=date(2026, 3, 5)
    )
    assert second != first
    stored = occurrence_rows(team, item_id)[0]
    assert stored.status == "posted" and stored.transaction_id == second


def test_double_delete_is_idempotent(team):
    ctx = team.ctx("owner", "owner")
    item_id, occurrence, transaction_id = post_first(team)
    transactions.delete_transaction(team.factory, ctx, transaction_id)
    occurrences.post_occurrence(
        team.factory, ctx, occurrence.id, amount_minor=100, day=occurrence.due_date
    )
    # Deleting the old (already deleted) row again leaves the re-posted occurrence alone.
    transactions.delete_transaction(team.factory, ctx, transaction_id)
    stored = occurrence_rows(team, item_id)[0]
    assert stored.status == "posted" and stored.transaction_id not in (None, transaction_id)
    with team.factory() as session:
        actions = list(
            session.scalars(
                select(AuditEvent.action).where(
                    AuditEvent.action.in_(("transaction.deleted", "scheduled_occurrence.unposted"))
                )
            )
        )
        assert sorted(actions) == ["scheduled_occurrence.unposted", "transaction.deleted"]


def test_ledger_delete_by_a_viewer_is_refused_and_changes_nothing(team):
    team.add_member("viewer", "viewer")
    item_id, _occurrence, transaction_id = post_first(team)
    for ctx in (team.ctx("viewer", "viewer"), team.ctx("viewer", "editor")):
        with pytest.raises(PermissionDenied):
            transactions.delete_transaction(team.factory, ctx, transaction_id)
    stored = occurrence_rows(team, item_id)[0]
    assert stored.status == "posted" and stored.transaction_id == transaction_id


def test_ledger_delete_is_scoped_to_the_workspace(team):
    other = team.add_workspace("Other")
    team.add_member("owner", "owner", other)
    item_id, _occurrence, transaction_id = post_first(team)
    with pytest.raises(NotFound):
        transactions.delete_transaction(
            team.factory, team.ctx("owner", "owner", other), transaction_id
        )
    stored = occurrence_rows(team, item_id)[0]
    assert stored.status == "posted" and stored.transaction_id == transaction_id


def test_ledger_delete_is_blocked_while_the_occurrence_month_is_closed(team):
    ctx = team.ctx("owner", "owner")
    item_id = create_item(team)
    occurrence = occurrence_rows(team, item_id)[0]
    transaction_id = occurrences.post_occurrence(
        team.factory, ctx, occurrence.id, amount_minor=100, day=date(2026, 4, 2)
    )
    close_month(team.factory, ctx, YearMonth(2026, 3))
    with pytest.raises(MonthClosed):
        transactions.delete_transaction(team.factory, ctx, transaction_id)
    with team.factory() as session:
        row = session.get(Transaction, transaction_id)
        assert row is not None and row.deleted_at is None
    assert occurrence_rows(team, item_id)[0].status == "posted"


def test_restore_relinks_only_a_still_pending_unlinked_occurrence(team):
    ctx = team.ctx("owner", "owner")
    item_id, occurrence, transaction_id = post_first(team)
    transactions.delete_transaction(team.factory, ctx, transaction_id)
    transactions.undo_delete(team.factory, ctx, transaction_id)
    stored = occurrence_rows(team, item_id)[0]
    assert stored.status == "posted" and stored.transaction_id == transaction_id

    transactions.delete_transaction(team.factory, ctx, transaction_id)
    occurrences.skip_occurrence(team.factory, ctx, occurrence.id)
    transactions.undo_delete(team.factory, ctx, transaction_id)
    stored = occurrence_rows(team, item_id)[0]
    assert stored.status == "skipped" and stored.transaction_id is None
    with team.factory() as session:
        row = session.get(Transaction, transaction_id)
        assert row is not None and row.deleted_at is None  # a plain transaction now


def test_auto_post_does_not_repost_an_occurrence_returned_to_pending(team):
    ctx = team.ctx("owner", "owner")
    item_id, occurrence, transaction_id = post_first(team, auto_post=True)
    transactions.delete_transaction(team.factory, ctx, transaction_id)

    assert occurrences.auto_post_workspace(team.factory, ctx, team.clock) == 0
    stored = occurrence_rows(team, item_id)[0]
    assert stored.status == "pending" and stored.transaction_id is None
    # The user can still post it by hand.
    occurrences.post_occurrence(
        team.factory, ctx, occurrence.id, amount_minor=100, day=occurrence.due_date
    )
    assert occurrence_rows(team, item_id)[0].status == "posted"


def test_unskip_returns_a_skipped_date_to_pending_and_is_audited(team):
    ctx = team.ctx("owner", "owner")
    item_id = create_item(team)
    occurrence = occurrence_rows(team, item_id)[0]
    occurrences.skip_occurrence(team.factory, ctx, occurrence.id)
    march = YearMonth(2026, 3)
    assert [row.id for row in occurrences.skipped_for_month(team.factory, ctx, march)] == [
        occurrence.id
    ]
    assert occurrence.id not in {
        row.id for row in occurrences.pending_for_month(team.factory, ctx, march)
    }

    occurrences.unskip_occurrence(team.factory, ctx, occurrence.id)
    stored = occurrence_rows(team, item_id)[0]
    assert stored.status == "pending"
    assert (stored.due_date, stored.expected_minor) == (occurrence.due_date, 10000)
    assert occurrences.skipped_for_month(team.factory, ctx, march) == ()
    # Idempotent: a pending occurrence stays pending.
    occurrences.unskip_occurrence(team.factory, ctx, occurrence.id)
    with team.factory() as session:
        actions = session.scalars(
            select(AuditEvent.action).where(AuditEvent.entity_id == str(occurrence.id))
        ).all()
    assert actions.count("scheduled_occurrence.unskipped") == 1

    # A posted occurrence cannot be "unskipped".
    occurrences.post_occurrence(
        team.factory, ctx, occurrence.id, amount_minor=10000, day=occurrence.due_date
    )
    with pytest.raises(occurrences.OccurrenceError, match="not_skipped"):
        occurrences.unskip_occurrence(team.factory, ctx, occurrence.id)


def test_unskip_needs_a_current_editor_an_open_month_and_an_active_item(team):
    team.add_member("viewer", "viewer")
    owner = team.ctx("owner", "owner")
    item_id = create_item(team)
    first, second = occurrence_rows(team, item_id)[:2]
    occurrences.skip_occurrence(team.factory, owner, first.id)
    occurrences.skip_occurrence(team.factory, owner, second.id)
    for ctx in (team.ctx("viewer", "viewer"), team.ctx("viewer", "editor")):
        with pytest.raises(PermissionDenied):
            occurrences.unskip_occurrence(team.factory, ctx, first.id)

    close_month(team.factory, owner, YearMonth.from_date(first.due_date))
    with pytest.raises(occurrences.OccurrenceError, match="month_closed"):
        occurrences.unskip_occurrence(team.factory, owner, first.id)

    scheduled.change_state(
        team.factory, owner, item_id, ItemAction.ARCHIVE, Settings(_env_file=None)
    )
    with pytest.raises(occurrences.OccurrenceError, match="archived_item"):
        occurrences.unskip_occurrence(team.factory, owner, second.id)
    statuses = {row.id: row.status for row in occurrence_rows(team, item_id)}
    assert statuses[first.id] == statuses[second.id] == "skipped"
