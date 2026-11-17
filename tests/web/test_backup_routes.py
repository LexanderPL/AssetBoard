"""Instance settings backup panel and administrator backup routes (F9.4)."""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select, update

from assetboard.clock import FixedClock
from assetboard.config import Settings
from assetboard.db.models.backups import BackupRun
from assetboard.db.models.identity import AuditEvent, InstanceSettings, User
from assetboard.services.backups import build_panel
from tests.conftest import ADMIN_PASSWORD, OwnerApp


def _set_clock(owner: OwnerApp) -> FixedClock:
    clock = FixedClock("2026-03-01T12:00:00+00:00")
    owner.app.state.clock = clock
    return clock


def test_settings_page_shows_backup_status_and_retention_defaults(owner: OwnerApp) -> None:
    page = owner.client.get("/admin/settings")
    assert page.status_code == 200
    assert "data-backup-panel" in page.text
    assert "Back up now" in page.text
    assert "Backup retention" in page.text
    assert 'name="backup_daily_retention"' in page.text
    assert 'value="7"' in page.text
    assert 'value="4"' in page.text
    assert 'value="6"' in page.text


def test_back_up_now_creates_a_downloadable_copy(owner: OwnerApp) -> None:
    _set_clock(owner)
    response = owner.post("/admin/backups/now")
    assert response.status_code == 303
    assert response.headers["location"].endswith("?backup=created")
    page = owner.client.get(response.headers["location"])
    assert "Backup created." in page.text
    settings = owner.app.state.settings
    files = list((settings.data_dir / "backups").glob("assetboard-manual-*.db"))
    assert len(files) == 1
    with owner.db() as session:
        run = session.scalars(select(BackupRun).where(BackupRun.origin == "manual")).one()
        assert run.status == "ok"
        run_id = run.id
    assert f"/admin/backups/{run_id}" in page.text
    download = owner.client.get(f"/admin/backups/{run_id}")
    assert download.status_code == 200
    assert download.content.startswith(b"SQLite format 3\x00")
    disposition = download.headers["content-disposition"]
    assert "attachment" in disposition and files[0].name in disposition


def test_download_unknown_backup_is_404(owner: OwnerApp) -> None:
    assert owner.client.get(f"/admin/backups/{uuid4()}").status_code == 404


def test_retention_settings_are_saved_and_audited(owner: OwnerApp) -> None:
    token = owner.token("/admin/settings")
    response = owner.client.post(
        "/admin/settings",
        data={
            "csrf_token": token,
            "allow_workspace_creation": "on",
            "allow_registration": "off",
            "backup_daily_retention": "3",
            "backup_weekly_retention": "2",
            "backup_monthly_retention": "1",
            "confirm_password": ADMIN_PASSWORD,
        },
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "Instance settings saved." in response.text
    with owner.db() as session:
        row = session.get(InstanceSettings, 1)
        assert row is not None
        assert (
            row.backup_daily_retention,
            row.backup_weekly_retention,
            row.backup_monthly_retention,
        ) == (3, 2, 1)
        event = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "instance.settings_updated")
        ).one()
        assert event.data == {
            "backup_daily_retention": 3,
            "backup_weekly_retention": 2,
            "backup_monthly_retention": 1,
        }


def test_invalid_retention_is_refused(owner: OwnerApp) -> None:
    token = owner.token("/admin/settings")
    response = owner.client.post(
        "/admin/settings",
        data={
            "csrf_token": token,
            "allow_workspace_creation": "on",
            "allow_registration": "off",
            "backup_daily_retention": "999",
            "backup_weekly_retention": "4",
            "backup_monthly_retention": "6",
            "confirm_password": ADMIN_PASSWORD,
        },
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "Choose a value for each setting." in response.text
    with owner.db() as session:
        row = session.get(InstanceSettings, 1)
        assert row is not None
        assert row.backup_daily_retention == 7


def test_retention_cannot_drop_every_copy(owner: OwnerApp) -> None:
    token = owner.token("/admin/settings")
    response = owner.client.post(
        "/admin/settings",
        data={
            "csrf_token": token,
            "allow_workspace_creation": "on",
            "allow_registration": "off",
            "backup_daily_retention": "0",
            "backup_weekly_retention": "0",
            "backup_monthly_retention": "0",
            "confirm_password": ADMIN_PASSWORD,
        },
        follow_redirects=False,
    )
    assert response.status_code == 400
    with owner.db() as session:
        row = session.get(InstanceSettings, 1)
        assert row is not None
        assert row.backup_daily_retention == 7


def test_backup_routes_need_an_instance_administrator(owner: OwnerApp) -> None:
    with owner.db() as session, session.begin():
        session.execute(
            update(User).where(User.email == owner.web.admin_email).values(is_instance_admin=False)
        )
    try:
        assert owner.client.get("/admin/settings").status_code == 403
        assert owner.client.post("/admin/backups/now", data={}).status_code == 403
    finally:
        with owner.db() as session, session.begin():
            session.execute(
                update(User)
                .where(User.email == owner.web.admin_email)
                .values(is_instance_admin=True)
            )


def test_back_up_now_needs_a_csrf_token(owner: OwnerApp) -> None:
    response = owner.client.post("/admin/backups/now", data={}, follow_redirects=False)
    assert response.status_code == 403


def test_settings_page_does_not_show_backups_for_postgres(owner: OwnerApp) -> None:
    # The settings panel is built from the service, which reports a disabled panel for a
    # non-SQLite URL. The configuration layer refuses PostgreSQL today; this keeps the
    # documented graceful behaviour ready for when it accepts it.
    settings = Settings.model_construct(
        data_dir=owner.app.state.settings.data_dir,
        database_url="postgresql://user@host/assetboard",
        scheduler_enabled=True,
        notifications_enabled=True,
    )
    panel = build_panel(
        owner.app.state.session_factory,
        settings,
        FixedClock("2026-03-01T12:00:00+00:00"),
    )
    assert panel.available is False
    assert panel.reason == "postgresql"
    assert panel.runs == ()
