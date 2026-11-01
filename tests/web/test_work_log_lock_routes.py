"""F3.4/F3.7 "Edit anyway": routes pass the override flag, show the received-payment notice and
"Update the transaction"; a closed month refuses changes with a link to the Month page."""

from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import select

from assetboard.clock import FixedClock
from assetboard.db.models.identity import AuditEvent
from assetboard.db.models.income import IncomeSource, WorkLog
from assetboard.db.models.ledger import Transaction
from assetboard.db.models.scheduled import ScheduledOccurrence
from tests.web.test_income_routes import _form, _income_category

HTMX = {"HX-Request": "true"}
AUGUST = {"work_month_month": "8", "work_month_year": "2026"}


def _received(owner, *, calendar: bool = False) -> tuple[str, str]:
    """August 2026 work (2 days at 1,230.00, due 10 September) received early, on 5 September,
    which is today: the month is still in the Income payments table."""
    owner.app.state.clock = FixedClock("2026-09-05T12:00:00+00:00")
    form = _form(_income_category(owner)) | {"active_from_year": "2020"}
    assert owner.post("/income", form, token_from="/income").status_code == 303
    with owner.db() as session:
        source_id = str(session.scalars(select(IncomeSource.id)).one())
    if calendar:
        for day in ("2026-08-03", "2026-08-04"):
            owner.post(f"/income/{source_id}/calendar/days/{day}/toggle", token_from="/income")
    else:
        owner.post(f"/income/{source_id}/work-logs", AUGUST | {"units": "2"}, token_from="/income")
    with owner.db() as session:
        occurrence = session.scalars(
            select(ScheduledOccurrence).where(
                ScheduledOccurrence.generated_by == "income",
                ScheduledOccurrence.work_month == date(2026, 8, 1),
            )
        ).one()
    owner.post(
        f"/upcoming/{occurrence.id}/post",
        {"day": "2026-09-05", "amount": "2,460.00"},
        token_from="/upcoming",
    )
    with owner.db() as session:
        posted = session.get(ScheduledOccurrence, occurrence.id)
        assert posted is not None and posted.transaction_id is not None
        return source_id, str(posted.transaction_id)


def _amount(owner, transaction_id: str) -> int:
    with owner.db() as session:
        row = session.get_one(Transaction, UUID(transaction_id))
        return row.amount_minor


def _actions(owner) -> list[str]:
    with owner.db() as session:
        return list(session.scalars(select(AuditEvent.action)).all())


def test_income_log_refused_then_edit_anyway_then_update_the_transaction(owner):
    source_id, transaction_id = _received(owner)
    page = owner.client.get("/income").text
    assert "data-log-locked>received</span>" in page
    assert 'data-log-edit-anyway="2026-08"' in page
    assert 'data-log-edit="2026-08"' not in page

    refused = owner.post(
        f"/income/{source_id}/work-logs", AUGUST | {"units": "3"}, token_from="/income"
    )
    location = refused.headers["location"]
    assert "error=posted_locked" in location and "work_month=2026-08" in location
    page = owner.client.get(location.split("#")[0]).text
    assert "already received. Edit the transaction instead." in page
    anyway = f"/income?log={source_id}&amp;month=2026-08&amp;override=1#income-source-{source_id}"
    assert anyway in page
    reopened = owner.client.get(anyway.replace("&amp;", "&").split("#")[0]).text
    assert 'name="override" value="1" data-log-override' in reopened

    saved = owner.post(
        f"/income/{source_id}/work-logs",
        AUGUST | {"units": "3", "override": "1"},
        token_from="/income",
    )
    location = saved.headers["location"]
    assert location.endswith(f"/income?changed={source_id}&month=2026-08#posted-change")
    with owner.db() as session:
        assert session.scalars(select(WorkLog.units)).one() == 3
    assert "work_log.updated_after_posting" in _actions(owner)
    assert _amount(owner, transaction_id) == 246_000

    notice = owner.client.get(location.split("#")[0]).text
    assert "Logged days changed. The received payment was" in notice
    assert "2,460.00; the new days give" in notice and "3,690.00." in notice
    assert "Update the transaction to" in notice and "data-update-transaction" in notice
    assert f"/transactions/2026-09?edit={transaction_id}#tx-{transaction_id}" in notice

    path = f"/income/{source_id}/work-logs/2026-08/update-transaction"
    no_csrf = owner.client.post(path, follow_redirects=False)
    assert no_csrf.status_code == 403
    assert _amount(owner, transaction_id) == 246_000

    updated = owner.post(path, token_from="/income")
    assert updated.status_code == 303
    assert "notice=transaction_updated" in updated.headers["location"]
    assert _amount(owner, transaction_id) == 369_000
    assert "income.posted_amount_updated" in _actions(owner)
    after = owner.client.get(f"/income?changed={source_id}&month=2026-08").text
    assert "data-posted-change" in after and "data-update-transaction" not in after


def test_viewers_cannot_override_or_update_the_transaction(owner):
    source_id, transaction_id = _received(owner)
    owner.set_admin_role("viewer")
    page = owner.client.get(f"/income?changed={source_id}&month=2026-08").text
    assert "data-update-transaction" not in page and "data-log-edit-anyway" not in page
    for path, data in (
        (f"/income/{source_id}/work-logs", AUGUST | {"units": "3", "override": "1"}),
        (f"/income/{source_id}/work-logs/2026-08/update-transaction", {}),
    ):
        response = owner.post(path, data, token_from="/income")
        assert response.status_code == 403
    assert _amount(owner, transaction_id) == 246_000


def test_calendar_received_month_is_read_only_until_edit_anyway(owner):
    source_id, transaction_id = _received(owner, calendar=True)
    base = f"/income/{source_id}/calendar"
    page = owner.client.get(f"{base}?month=2026-08").text
    assert "data-calendar-received" in page and "data-edit-anyway" in page
    assert "/days/2026-08-05/toggle" not in page  # read-only cells
    assert f"/transactions/2026-09?edit={transaction_id}" in page

    form = {"csrf_token": owner.token("/income")}
    refused = owner.client.post(
        f"{base}/days/2026-08-05/toggle", data=form, headers=HTMX, follow_redirects=False
    )
    assert refused.status_code == 409 and "data-calendar-received" in refused.text

    editable = owner.client.get(f"{base}?month=2026-08&override=1", headers=HTMX).text
    assert "data-calendar-overriding" in editable
    assert 'name="override" value="1"' in editable and "/days/2026-08-05/toggle" in editable

    changed = owner.client.post(
        f"{base}/days/2026-08-05/toggle",
        data=form | {"override": "1"},
        headers=HTMX,
        follow_redirects=False,
    )
    assert changed.status_code == 200
    assert "data-posted-change" in changed.text and "data-update-transaction" in changed.text
    assert "Month total: 3 (day)" in changed.text
    assert _amount(owner, transaction_id) == 246_000

    # Without JavaScript the redirect keeps the override and asks for the notice.
    plain = owner.post(f"{base}/days/2026-08-06/toggle", {"override": "1"}, token_from="/income")
    assert plain.headers["location"].endswith("?month=2026-08&override=1&changed=1")

    updated = owner.post(
        f"/income/{source_id}/work-logs/2026-08/update-transaction",
        {"return_to": "calendar"},
        token_from="/income",
    )
    assert updated.headers["location"].endswith(f"{base}?month=2026-08&notice=transaction_updated")
    assert _amount(owner, transaction_id) == 492_000
    assert "data-transaction-updated" in owner.client.get(updated.headers["location"]).text


def test_a_closed_month_refuses_changes_even_with_override(owner):
    source_id, transaction_id = _received(owner, calendar=True)
    assert owner.post("/m/2026-09/close", token_from="/m/2026-09").status_code == 303
    base = f"/income/{source_id}/calendar"
    page = owner.client.get(f"{base}?month=2026-08").text
    assert "This month is closed. Reopen it to make changes." in page
    assert 'href="/m/2026-09"' in page and "data-edit-anyway" not in page
    assert "/days/2026-08-05/toggle" not in page

    form = {"csrf_token": owner.token("/income"), "override": "1"}
    refused = owner.client.post(
        f"{base}/days/2026-08-05/toggle", data=form, headers=HTMX, follow_redirects=False
    )
    assert refused.status_code == 409 and "data-calendar-closed" in refused.text

    logged = owner.post(
        f"/income/{source_id}/work-logs",
        AUGUST | {"units": "3", "override": "1"},
        token_from="/income",
    )
    location = logged.headers["location"]
    assert "error=work_month_closed" in location and "closed=2026-09" in location
    income_page = owner.client.get(location.split("#")[0]).text
    assert "This month is closed. Reopen it to make changes." in income_page
    assert 'href="/m/2026-09" class="font-semibold underline" data-reopen-link' in income_page
    assert 'data-log-closed title="This month is closed.' in owner.client.get("/income").text

    update = owner.post(
        f"/income/{source_id}/work-logs/2026-08/update-transaction", token_from="/income"
    )
    assert "error=work_month_closed" in update.headers["location"]
    assert _amount(owner, transaction_id) == 246_000
