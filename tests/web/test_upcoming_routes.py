from __future__ import annotations

from datetime import date

from sqlalchemy import select

from assetboard.clock import FixedClock
from assetboard.db.models.ledger import MonthState, Transaction
from assetboard.db.models.scheduled import ScheduledOccurrence


def create_recurring(owner, **changes):
    page = owner.client.get("/recurring/new")
    import re

    category = re.search(r'<option value="([a-f0-9-]{36})"', page.text)
    assert category
    return owner.post(
        "/recurring",
        {
            "name": "Rent",
            "direction": "expense",
            "category_id": category.group(1),
            "amount": "100",
            "amount_mode": "estimate",
            "start_date": "2026-01-01",
            "freq": "monthly",
            "interval": "1",
            "month_day": "1",
            "month": "1",
            "weekend_policy": "none",
            **changes,
        },
    )


def fixed_clock(owner):
    clock = FixedClock("2026-03-01T12:00:00+00:00")
    owner.app.state.clock = clock
    owner.app.state.session_factory.configure(info={"clock": clock})


def test_upcoming_post_undo_and_filtering(owner):
    fixed_clock(owner)
    assert create_recurring(owner, include_past="1").status_code == 303
    with owner.db() as session:
        occurrence = session.scalars(select(ScheduledOccurrence)).first()
        assert occurrence is not None
        occurrence_id = occurrence.id

    response = owner.client.get("/upcoming")
    assert response.status_code == 200
    assert "Rent" in response.text and "Overdue" in response.text
    assert "Mark paid or received" in response.text
    assert owner.client.get("/upcoming?direction=income").status_code == 200
    assert owner.client.get("/upcoming?direction=bogus").status_code == 400

    posted = owner.post(
        f"/upcoming/{occurrence_id}/post",
        {"day": "2026-03-01", "amount": "123.45"},
        token_from="/upcoming",
    )
    assert posted.status_code == 303 and f"posted={occurrence_id}" in posted.headers["location"]
    inbox = owner.client.get(posted.headers["location"])
    assert "data-post-undo" in inbox.text
    assert "Undo" in inbox.text
    assert owner.post(f"/upcoming/{occurrence_id}/undo", token_from="/upcoming").status_code == 303

    with owner.db() as session:
        transaction = session.scalars(select(Transaction)).one()
        occurrence = session.get(ScheduledOccurrence, occurrence_id)
        assert occurrence is not None
        assert transaction.deleted_at is not None
        assert occurrence.status == "pending" and occurrence.transaction_id is None


def test_occurrence_actions_preserve_rule_and_closed_month_blocks_post(owner):
    fixed_clock(owner)
    assert create_recurring(owner).status_code == 303
    with owner.db() as session:
        occurrence = session.scalars(select(ScheduledOccurrence)).first()
        assert occurrence is not None
        occurrence_id = occurrence.id
        workspace_id = session.scalar(select(ScheduledOccurrence.workspace_id).limit(1))
        assert workspace_id is not None
    rescheduled = owner.post(
        f"/upcoming/{occurrence_id}/reschedule",
        {"day": "2026-03-05"},
        token_from="/upcoming",
    )
    assert rescheduled.status_code == 303
    edited = owner.post(
        f"/upcoming/{occurrence_id}/amount",
        {"amount": "250"},
        token_from="/upcoming",
    )
    assert edited.status_code == 303
    with owner.db() as session:
        occurrence = session.get(ScheduledOccurrence, occurrence_id)
        assert occurrence is not None
        assert occurrence.nominal_date == date(2026, 3, 1)
        assert occurrence.due_date == date(2026, 3, 5)
        assert occurrence.expected_minor == 25000 and occurrence.amount_overridden

    assert owner.post(f"/upcoming/{occurrence_id}/skip", token_from="/upcoming").status_code == 303
    with owner.db() as session:
        occurrence = session.get(ScheduledOccurrence, occurrence_id)
        assert occurrence is not None and occurrence.status == "skipped"

    assert create_recurring(owner, name="Closed rent").status_code == 303
    with owner.db() as session:
        occurrence = session.scalars(
            select(ScheduledOccurrence).where(
                ScheduledOccurrence.label == "Closed rent",
                ScheduledOccurrence.due_date == date(2026, 3, 1),
                ScheduledOccurrence.status == "pending",
            )
        ).first()
        assert occurrence is not None
        other_id = occurrence.id

    with owner.db() as session, session.begin():
        session.add(
            MonthState(
                workspace_id=workspace_id,
                month=date(2026, 3, 1),
                closed_at=FixedClock("2026-03-01T12:00:00+00:00").now(),
            )
        )
    blocked = owner.post(
        f"/upcoming/{other_id}/post",
        {"day": "2026-03-01", "amount": "100"},
        token_from="/upcoming",
    )
    assert blocked.status_code == 303 and "month_closed" in blocked.headers["location"]
    assert "month is closed" in owner.client.get(blocked.headers["location"]).text

    still_blocked = owner.post(
        f"/upcoming/{other_id}/post",
        {"day": "2026-04-01", "amount": "100"},
        token_from="/upcoming",
    )
    assert "month_closed" in still_blocked.headers["location"]

    assert (
        owner.post(
            f"/upcoming/{other_id}/reschedule",
            {"day": "2026-04-01"},
            token_from="/upcoming",
        ).status_code
        == 303
    )
    posted = owner.post(
        f"/upcoming/{other_id}/post",
        {"day": "2026-04-01", "amount": "100"},
        token_from="/upcoming",
    )
    assert posted.status_code == 303 and f"posted={other_id}" in posted.headers["location"]
