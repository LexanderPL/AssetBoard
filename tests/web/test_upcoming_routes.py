from __future__ import annotations

import re
from datetime import date

from sqlalchemy import select

from assetboard.clock import FixedClock
from assetboard.db.models.identity import Workspace
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


def test_deleting_a_posted_transaction_in_the_ledger_returns_it_to_the_inbox(owner):
    fixed_clock(owner)
    assert create_recurring(owner).status_code == 303
    with owner.db() as session:
        occurrence = session.scalars(select(ScheduledOccurrence)).first()
        assert occurrence is not None
        occurrence_id = occurrence.id
    form = {"day": "2026-03-01", "amount": "100"}
    owner.post(f"/upcoming/{occurrence_id}/post", form, token_from="/upcoming")
    with owner.db() as session:
        transaction_id = session.scalars(select(Transaction.id)).one()

    response = owner.post(
        f"/m/2026-03/transactions/{transaction_id}/delete", token_from="/m/2026-03"
    )
    assert response.status_code == 303

    with owner.db() as session:
        occurrence = session.get(ScheduledOccurrence, occurrence_id)
        assert occurrence is not None
        assert occurrence.status == "pending" and occurrence.transaction_id is None
    assert "Mark paid or received" in owner.client.get("/upcoming").text
    reposted = owner.post(f"/upcoming/{occurrence_id}/post", form, token_from="/upcoming")
    assert reposted.status_code == 303
    with owner.db() as session:
        live = session.scalars(select(Transaction).where(Transaction.deleted_at.is_(None))).all()
        assert len(live) == 1


def test_upcoming_and_recurring_pages_have_no_links_to_other_sections(owner):
    upcoming = owner.client.get("/upcoming").text
    main = upcoming.split("<main", 1)[1]
    assert "<h1" in main and "Upcoming payments" in main
    assert "<title>Upcoming payments" in upcoming
    assert 'href="/recurring"' not in main and 'href="/notifications"' not in main
    assert "data-notifications-shortcut" not in upcoming
    recurring = owner.client.get("/recurring").text.split("<main", 1)[1]
    assert 'href="/upcoming"' not in recurring


def test_upcoming_page_shows_a_monthly_item_due_in_twenty_days(owner):
    fixed_clock(owner)  # 2026-03-01
    assert create_recurring(owner, start_date="2026-03-21", month_day="21").status_code == 303
    page = owner.client.get("/upcoming").text
    assert 'datetime="2026-03-21"' in page
    assert "the next 30 days" in page
    # Home uses the same window (it was a hard-coded week).
    home = owner.client.get("/").text
    assert re.search(r'<li [^>]*data-occurrence="[0-9a-f-]+"', home)
    assert 'datetime="2026-03-21"' in home


def _save_window(owner, days: str):
    form = {"name": "Home", "base_currency": "EUR", "timezone": "UTC", "locale": "en"}
    return owner.post(
        "/workspace/settings", form | {"upcoming_days": days}, token_from="/workspace/settings"
    )


def test_workspace_upcoming_window_applies_to_upcoming_and_home(owner):
    fixed_clock(owner)  # 2026-03-01
    assert create_recurring(owner, start_date="2026-03-21", month_day="21").status_code == 303
    settings = owner.client.get("/workspace/settings").text
    assert 'name="upcoming_days"' in settings and 'placeholder="30"' in settings
    assert "How far ahead Upcoming payments and the Home page look." in settings

    assert _save_window(owner, "10").status_code == 303
    with owner.db() as session:
        assert session.scalars(select(Workspace.upcoming_days)).one() == 10
    assert 'value="10"' in owner.client.get("/workspace/settings").text
    page = owner.client.get("/upcoming").text
    assert "the next 10 days" in page and 'datetime="2026-03-21"' not in page
    home = owner.client.get("/").text
    assert 'datetime="2026-03-21"' not in home
    assert "Nothing is overdue or due in the next 10 days." in home

    # 1-365, a whole number; empty goes back to the instance setting.
    for bad in ("0", "366", "abc", "7.5", "1000"):
        refused = _save_window(owner, bad)
        assert refused.status_code == 400, bad
        assert "Enter a number of days from 1 to 365" in refused.text
    assert _save_window(owner, "").status_code == 303
    with owner.db() as session:
        assert session.scalars(select(Workspace.upcoming_days)).one() is None
    assert "the next 30 days" in owner.client.get("/upcoming").text


def test_instance_upcoming_days_applies_without_a_workspace_value(owner):
    fixed_clock(owner)
    owner.app.state.settings = owner.app.state.settings.model_copy(update={"upcoming_days": 12})
    assert "the next 12 days" in owner.client.get("/upcoming").text
    assert 'placeholder="12"' in owner.client.get("/workspace/settings").text


def _categories(owner, kind: str) -> list[str]:
    from assetboard.db.models.ledger import Category

    with owner.db() as session:
        return [
            str(category_id)
            for category_id in session.scalars(
                select(Category.id).where(
                    Category.kind == kind,
                    Category.archived_at.is_(None),
                    Category.treatment != "transfer",
                )
            )
        ]


def test_add_edit_and_delete_a_one_time_payment_on_upcoming(owner):
    fixed_clock(owner)
    page = owner.client.get("/upcoming").text
    assert "data-add-one-time" in page and ">Add one-time payment</summary>" in page
    assert "data-add-one-time open" not in page
    assert "data-add-one-time open" in owner.client.get("/upcoming?form=one-time").text

    expense, income = _categories(owner, "expense")[0], _categories(owner, "income")[0]
    created = owner.post(
        "/upcoming/one-time",
        {
            "name": "Car insurance",
            "amount": "1,234.50",
            "direction": "expense",
            "category_id": expense,
            "day": "2026-03-20",
        },
        token_from="/upcoming",
    )
    assert created.status_code == 303
    assert created.headers["location"] == "/upcoming?added=2026-03-20"
    with owner.db() as session:
        occurrence = session.scalars(select(ScheduledOccurrence)).one()
        assert (occurrence.label, occurrence.expected_minor, occurrence.due_date) == (
            "Car insurance",
            123450,
            date(2026, 3, 20),
        )
        occurrence_id = occurrence.id
    page = owner.client.get(created.headers["location"]).text
    assert "One-time payment added for" in page
    card = re.search(rf'<article [^>]*data-occurrence="{occurrence_id}".*?</article>', page, re.S)
    assert card is not None
    # A one-time row: Mark paid, Edit and Delete; no Skip, Reschedule or "Edit rule".
    assert "Mark paid or received" in card.group(0)
    assert "data-one-time-edit" in card.group(0) and "data-delete" in card.group(0)
    assert ">Skip<" not in card.group(0) and "Edit rule" not in card.group(0)
    assert 'value="Car insurance"' in card.group(0)
    # The Recurring page does not list it.
    assert "Car insurance" not in owner.client.get("/recurring").text

    edited = owner.post(
        f"/upcoming/{occurrence_id}/one-time",
        {
            "name": "Car insurance 2026",
            "amount": "1300",
            "direction": "expense",
            "category_id": expense,
            "day": "2026-03-25",
        },
        token_from="/upcoming",
    )
    assert edited.headers["location"] == "/upcoming?notice=one_time_saved"
    assert "One-time payment saved." in owner.client.get(edited.headers["location"]).text
    with owner.db() as session:
        stored = session.get(ScheduledOccurrence, occurrence_id)
        assert stored is not None
        assert (stored.label, stored.expected_minor, stored.due_date) == (
            "Car insurance 2026",
            130000,
            date(2026, 3, 25),
        )

    # Errors re-render the form with the values and a message.
    mismatch = owner.post(
        f"/upcoming/{occurrence_id}/one-time",
        {
            "name": "Car insurance 2026",
            "amount": "1300",
            "direction": "income",
            "category_id": expense,
            "day": "2026-03-25",
        },
        token_from="/upcoming",
    )
    assert mismatch.status_code == 400
    assert "Choose an expense category for an expense" in mismatch.text
    assert "data-one-time-edit open" in mismatch.text
    missing = owner.post(
        "/upcoming/one-time",
        {"name": "", "amount": "5", "direction": "income", "category_id": income, "day": ""},
        token_from="/upcoming",
    )
    assert missing.status_code == 400
    assert "Enter a description." in missing.text and "data-add-one-time open" in missing.text

    deleted = owner.post(f"/upcoming/{occurrence_id}/delete", token_from="/upcoming")
    assert deleted.headers["location"] == "/upcoming?notice=planned_deleted"
    with owner.db() as session:
        assert session.scalars(select(ScheduledOccurrence)).first() is None


def test_a_far_one_time_payment_says_when_it_shows_and_viewers_cannot_add(owner):
    fixed_clock(owner)
    created = owner.post(
        "/upcoming/one-time",
        {
            "name": "Holiday",
            "amount": "800",
            "direction": "expense",
            "category_id": _categories(owner, "expense")[0],
            "day": "2026-12-01",
        },
        token_from="/upcoming",
    )
    page = owner.client.get(created.headers["location"]).text
    assert "It shows here when it is due within the next" in page
    owner.set_admin_role("viewer")
    page = owner.client.get("/upcoming").text
    assert "data-add-one-time" not in page
    refused = owner.post(
        "/upcoming/one-time",
        {"name": "X", "amount": "1", "direction": "expense", "day": "2026-04-01"},
        token_from="/upcoming",
    )
    assert refused.status_code == 403
