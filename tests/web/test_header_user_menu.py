"""Page header and top-right user menu; sidebar version and the opt-in update notice."""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib.metadata import version
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update

from assetboard.db.models.identity import AuditEvent, InstanceSettings, Membership
from tests.conftest import ADMIN_PASSWORD, OwnerApp

VERSION = version("assetboard")


@contextmanager
def signed_in_as(owner: OwnerApp, key: str) -> Iterator[TestClient]:
    with TestClient(owner.app) as client:
        response = owner.web.sign_in(client, email=f"{key}@example.com")
        assert response.status_code == 303, response.text
        yield client


def _menu(page: str) -> str:
    start = page.index("data-user-menu>")
    return page[start : page.index("</details>", start)]


def _header(page: str) -> str:
    return page[page.index("<header") : page.index("</header>")]


def _sidebar(page: str) -> str:
    return page[page.index("data-sidebar") : page.index("</aside>")]


def _menu_links(page: str) -> list[str]:
    return re.findall(r"data-([a-z-]+-link|sign-out)\b", _menu(page))


def _set_update(owner: OwnerApp, *, enabled: bool, tag: str | None) -> None:
    with owner.db() as session, session.begin():
        session.execute(
            update(InstanceSettings).values(
                check_for_updates=enabled,
                latest_release_tag=tag,
                update_checked_at=datetime(2026, 10, 30, tzinfo=UTC) if tag else None,
            )
        )


_PARTS = [int(p) for p in re.findall(r"\d+", VERSION)[:3]]
SAME = "v" + ".".join(map(str, _PARTS))
NEWER = "v" + ".".join(map(str, [*_PARTS[:-1], _PARTS[-1] + 1]))
OLDER = "v0.0.0"


# Header


def test_the_header_shows_the_workspace_name_not_a_generic_eyebrow(owner: OwnerApp) -> None:
    page = owner.client.get("/").text
    header = _header(page)
    assert "Personal finance" not in page
    assert "data-current-workspace>Home</p>" in header
    # No workspace pill or select switcher in the header any more.
    assert "<select" not in header and "data-workspace-switcher" not in header
    assert ">Workspace <" not in header


def test_the_header_has_no_eyebrow_without_a_workspace(owner: OwnerApp) -> None:
    with owner.db() as session, session.begin():
        session.execute(delete(Membership))
    page = owner.client.get("/account").text
    assert "data-current-workspace" not in page
    assert "Your account" not in page
    assert "data-user-menu>" in page  # the menu still offers the account pages


def test_the_header_eyebrow_is_escaped(owner: OwnerApp) -> None:
    second = owner.add_workspace("<b>Bills</b>")
    owner.join(owner.admin_id, second, "owner")
    owner.post("/workspace/switch", {"workspace_id": str(second)})
    page = owner.client.get("/").text
    assert "data-current-workspace>&lt;b&gt;Bills&lt;/b&gt;</p>" in page


# User menu


def test_the_user_menu_is_at_the_top_right_with_the_display_name(owner: OwnerApp) -> None:
    page = owner.client.get("/").text
    assert "data-user-menu" not in _sidebar(page)
    menu = _menu(page)
    assert "<details" in menu and "data-popover" in menu
    assert re.search(r"data-user-name>[^<]+</span>", menu)


def test_owner_and_instance_admin_menu_items_in_groups(owner: OwnerApp) -> None:
    page = owner.client.get("/").text
    assert _menu_links(page) == [
        "new-workspace-link",
        "categories-link",
        "owner-link",
        "owner-link",
        "audit-workspace-link",
        "workspace-data-link",
        "admin-link",
        "admin-settings-link",
        "audit-instance-link",
        "profile-link",
        "account-security-link",
        "sign-out",
    ]
    menu = _menu(page)
    for group in ("data-user-menu-workspace", "data-user-menu-admin", "data-user-menu-account"):
        assert group in menu
    assert "divide-y" in menu  # separators between the groups


@pytest.mark.parametrize(
    ("role", "links"),
    [
        ("editor", ["categories-link", "workspace-data-link"]),
        ("viewer", ["categories-link"]),
    ],
)
def test_member_menu_items_follow_the_role(owner: OwnerApp, role: str, links: list[str]) -> None:
    bea = owner.add_user("bea")
    owner.join(bea, owner.home_id, role)
    with owner.db() as session, session.begin():
        session.execute(update(InstanceSettings).values(allow_workspace_creation=False))
    with signed_in_as(owner, "bea") as client:
        page = client.get("/").text
    assert _menu_links(page) == [*links, "profile-link", "account-security-link", "sign-out"]
    assert "data-user-menu-admin" not in page


def test_the_menu_marks_the_current_page(owner: OwnerApp) -> None:
    page = owner.client.get("/account").text
    assert 'data-profile-link aria-current="page"' in _menu(page)
    page = owner.client.get("/admin/settings").text
    assert 'data-admin-settings-link aria-current="page"' in _menu(page)


def test_switching_workspace_from_the_menu(owner: OwnerApp) -> None:
    second = owner.add_workspace("Second")
    owner.join(owner.admin_id, second, "viewer")
    page = owner.client.get("/").text
    menu = _menu(page)
    assert "data-workspace-switcher" in menu
    # The current workspace is marked; every other one is a CSRF-protected POST form.
    assert re.search(r'aria-current="true" data-workspace-choice="[^"]+"', menu)
    form = re.search(
        r'<form method="post" action="/workspace/switch">\s*'
        r'<input type="hidden" name="csrf_token" value="([^"]+)">\s*'
        r'<input type="hidden" name="workspace_id" value="([^"]+)">\s*'
        r'<button type="submit" [^>]*data-workspace-choice="[^"]+">Second</button>',
        menu,
    )
    assert form is not None
    assert form.group(2) == str(second)
    response = owner.client.post(
        "/workspace/switch",
        data={"csrf_token": form.group(1), "workspace_id": form.group(2)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = owner.client.get("/").text
    assert "data-current-workspace>Second</p>" in page


def test_a_switch_without_the_csrf_token_is_refused(owner: OwnerApp) -> None:
    second = owner.add_workspace("Second")
    owner.join(owner.admin_id, second, "viewer")
    response = owner.client.post(
        "/workspace/switch", data={"workspace_id": str(second)}, follow_redirects=False
    )
    assert response.status_code == 403
    assert "data-current-workspace>Home</p>" in owner.client.get("/").text


# Sidebar footer: version and update notice


def test_every_signed_in_user_sees_the_version_in_the_sidebar(owner: OwnerApp) -> None:
    page = owner.client.get("/").text
    sidebar = _sidebar(page)
    assert f"data-app-version>v{VERSION}</span>" in sidebar
    assert "data-theme-toggle" in sidebar
    bea = owner.add_user("bea")
    owner.join(bea, owner.home_id, "viewer")
    with signed_in_as(owner, "bea") as client:
        assert f"data-app-version>v{VERSION}</span>" in client.get("/").text


def test_the_update_notice_is_for_admins_and_only_for_a_newer_release(owner: OwnerApp) -> None:
    newer = NEWER
    _set_update(owner, enabled=True, tag=newer)
    page = owner.client.get("/").text
    notice = re.search(r'<a href="([^"]+)"[^>]*data-update-available>([^<]+)</a>', page)
    assert notice is not None
    assert notice.group(1) == f"https://github.com/LexanderPL/AssetBoard/releases/tag/{newer}"
    assert notice.group(2) == f"Update available: {newer.removeprefix('v')}"
    # Admin pages without a workspace context show it too.
    assert "data-update-available" in owner.client.get("/admin/settings").text
    bea = owner.add_user("bea")
    owner.join(bea, owner.home_id, "owner")
    with signed_in_as(owner, "bea") as client:
        assert "data-update-available" not in client.get("/").text

    for tag in (SAME, OLDER):
        _set_update(owner, enabled=True, tag=tag)
        assert "data-update-available" not in owner.client.get("/").text, tag
    # Turned off: a stored newer release is not shown.
    _set_update(owner, enabled=False, tag=newer)
    assert "data-update-available" not in owner.client.get("/").text


def test_the_update_notice_is_translated(owner: OwnerApp) -> None:
    _set_update(owner, enabled=True, tag=NEWER)
    owner.client.headers["Accept-Language"] = "pl"
    page = owner.client.get("/").text
    assert "Dostępna aktualizacja:" in page


# Instance setting


def _save(owner: OwnerApp, check: str | None) -> Any:
    data = {
        "allow_workspace_creation": "on",
        "allow_registration": "off",
        "confirm_password": ADMIN_PASSWORD,
    }
    if check is not None:
        data["check_for_updates"] = check
    return owner.post("/admin/settings", data, token_from="/admin/settings")


def _check_setting(owner: OwnerApp) -> bool:
    with owner.db() as session:
        row = session.get(InstanceSettings, 1)
        assert row is not None
        return row.check_for_updates


def test_the_update_check_setting_is_off_by_default_and_audited(owner: OwnerApp) -> None:
    page = owner.client.get("/admin/settings").text
    assert 'name="check_for_updates" value="on" aria-describedby' in page
    assert "Once a day, ask GitHub for the latest release." in page
    assert _check_setting(owner) is False
    saved = _save(owner, "on")
    assert saved.status_code == 200 and "Instance settings saved." in saved.text
    assert 'name="check_for_updates" value="on" checked' in saved.text
    assert _check_setting(owner) is True
    assert _save(owner, None).status_code == 200  # unticked: absent from the form
    assert _check_setting(owner) is False
    assert _save(owner, "yes").status_code == 400
    with owner.db() as session:
        events = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "instance.settings_updated")
        ).all()
    assert [e.data for e in events] == [{"check_for_updates": True}, {"check_for_updates": False}]
    page = owner.client.get("/audit?scope=instance&group=instance_settings").text
    assert "Check for updates: yes" in page


def test_turning_the_check_on_asks_for_a_check_soon(
    owner: OwnerApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested: list[bool] = []
    jobs = owner.app.state.recurring_jobs
    monkeypatch.setattr(jobs, "request_update_check", lambda: requested.append(True))
    _save(owner, "on")
    assert requested == [True]
    _save(owner, "on")  # unchanged
    _save(owner, None)  # turned off
    assert requested == [True]
