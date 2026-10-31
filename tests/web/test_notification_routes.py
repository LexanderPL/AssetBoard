from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select

from assetboard.clock import FixedClock
from assetboard.db.models.notifications import NotificationChannel
from assetboard.db.models.scheduled import ScheduledItem
from tests.fake_notifier import FakeNotifier
from tests.web.test_scheduled_routes import form as recurring_form

URL = "ntfys://ntfy.example.com/very-secret-topic"


def channel_form(**overrides: str) -> dict[str, str]:
    return {
        "name": "Phone",
        "url": URL,
        "mode": "digest",
        "digest_time": "07:30",
        "default_lead_days": "5",
        "notify_due_soon": "1",
        "notify_overdue": "1",
        "notify_work_logs": "1",
        "enabled": "1",
        **overrides,
    }


def fake(owner) -> FakeNotifier:
    notifier = FakeNotifier()
    owner.app.state.notifier = notifier
    return notifier


def channel_id(owner):
    with owner.db() as session:
        return session.scalars(select(NotificationChannel.id)).one()


def test_create_mask_test_send_edit_and_delete(owner):
    notifier = fake(owner)
    page = owner.client.get("/notifications")
    assert page.status_code == 200
    assert "No notification channels yet." in page.text
    assert "Include amounts" in page.text and "pass through the service you choose" in page.text
    # Not in the account menu any more: in Workspace settings (owners) and on the Profile page.
    assert "data-notifications-link" not in page.text

    created = owner.post("/notifications", channel_form())
    assert created.status_code == 303 and created.headers["location"].endswith("notice=created")
    listing = owner.client.get("/notifications")
    assert "ntfys://••••••" in listing.text
    assert "very-secret-topic" not in listing.text and "ntfy.example.com" not in listing.text
    with owner.db() as session:
        row = session.scalars(select(NotificationChannel)).one()
        assert (row.mode, row.digest_time.isoformat(), row.default_lead_days) == (
            "digest",
            "07:30:00",
            5,
        )
        assert row.include_amounts is False and row.enabled is True

    identifier = channel_id(owner)
    tested = owner.post(f"/notifications/{identifier}/test")
    assert tested.headers["location"].endswith("notice=test_sent")
    assert [message.url for message in notifier.sent] == [URL]
    assert "Test message sent" in owner.client.get(tested.headers["location"]).text

    notifier.fail_with = "timeout"
    failed = owner.post(f"/notifications/{identifier}/test")
    assert failed.headers["location"].endswith("error=timeout")
    assert "did not answer in time" in owner.client.get(failed.headers["location"]).text

    edited = owner.post(
        f"/notifications/{identifier}",
        channel_form(url="", name="Phone 2", mode="per_item", include_amounts="1"),
    )
    assert edited.status_code == 303
    with owner.db() as session:
        row = session.scalars(select(NotificationChannel)).one()
        assert (row.name, row.mode, row.include_amounts, row.scheme) == (
            "Phone 2",
            "per_item",
            True,
            "ntfys",
        )

    assert owner.post(f"/notifications/{identifier}/delete").status_code == 303
    assert "No notification channels yet." in owner.client.get("/notifications").text


def test_rejected_urls_are_not_echoed_and_forms_need_csrf(owner):
    fake(owner)
    refused = owner.post("/notifications", channel_form(url="json://10.0.0.1/hook?token=abc"))
    assert refused.headers["location"].endswith("error=scheme_not_allowed")
    assert "token=abc" not in refused.headers["location"]
    page = owner.client.get(refused.headers["location"])
    assert "not allowed on this server" in page.text and "token=abc" not in page.text
    bad_time = owner.post("/notifications", channel_form(digest_time="25:00"))
    assert bad_time.headers["location"].endswith("error=invalid_time")
    assert owner.client.post("/notifications", data=channel_form()).status_code == 403
    assert owner.post(f"/notifications/{uuid4()}/delete").status_code == 404
    assert owner.post("/notifications/not-a-uuid/test").status_code == 404


def test_viewers_manage_their_own_channels(owner):
    fake(owner)
    owner.set_admin_role("viewer")
    assert owner.post("/notifications", channel_form()).status_code == 303
    assert "ntfys://••••••" in owner.client.get("/notifications").text


def test_channels_belong_to_the_current_workspace(owner):
    fake(owner)
    owner.post("/notifications", channel_form())
    identifier = channel_id(owner)
    other = owner.add_workspace("Other")
    owner.join(owner.admin_id, other, "owner")
    owner.post("/workspace/switch", {"workspace_id": str(other)})
    assert "ntfys://••••••" not in owner.client.get("/notifications").text
    assert owner.post(f"/notifications/{identifier}/test").status_code == 404


def test_recurring_items_take_their_own_reminder_lead(owner):
    owner.app.state.clock = FixedClock("2026-03-01T12:00:00+00:00")
    data = recurring_form(owner, reminder_lead_days="14")
    assert 'name="reminder_lead_days"' in owner.client.get("/recurring/new").text
    assert owner.post("/recurring", data).status_code == 303
    with owner.db() as session:
        item = session.scalars(select(ScheduledItem)).one()
        assert item.reminder_lead_days == 14
    edit = owner.client.get(f"/recurring/{item.id}/edit")
    assert 'value="14"' in edit.text
    assert owner.post("/recurring", data | {"reminder_lead_days": "61"}).status_code == 400
    assert (
        owner.post(f"/recurring/{item.id}/edit", data | {"reminder_lead_days": ""}).status_code
        == 303
    )
    with owner.db() as session:
        assert session.get(ScheduledItem, item.id).reminder_lead_days is None  # type: ignore[union-attr]


def test_the_page_is_translated(owner):
    owner.client.headers["accept-language"] = "pl"
    page = owner.client.get("/notifications").text
    assert "Powiadomienia" in page and "Nowy kanał" in page and "Dołączaj kwoty" in page


def test_notifications_are_linked_from_workspace_settings_and_the_profile(owner):
    settings = owner.client.get("/workspace/settings").text
    section = settings.split("data-settings-notifications", 1)[1].split("</section>", 1)[0]
    assert 'href="/notifications" data-notifications-link' in section
    assert 'href="/notifications" data-notifications-link' in owner.client.get("/account").text
    # Editors and viewers keep their own channels: the Profile page links them.
    for role in ("editor", "viewer"):
        owner.set_admin_role(role)
        assert owner.client.get("/workspace/settings").status_code == 403
        profile = owner.client.get("/account").text
        assert 'href="/notifications" data-notifications-link' in profile
        assert owner.client.get("/notifications").status_code == 200
