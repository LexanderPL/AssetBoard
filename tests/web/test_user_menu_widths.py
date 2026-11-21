"""Every destination reached from the user menu uses one max-w-4xl page frame.

Focused create/confirm/TOTP flows stay narrow. Tables on the 4xl pages keep their own
horizontal scroll regions; the page frame never widens the document.
"""

from __future__ import annotations

import re

import pytest

from tests.conftest import OwnerApp

PAGES = [
    ("/categories", 'id="categories-body"'),
    ("/workspace/settings", "data-settings-menu"),
    ("/workspace/members", 'id="members-heading"'),
    ("/audit?scope=workspace", "data-audit-filters"),
    ("/audit?scope=instance", "data-audit-filters"),
    ("/workspace/data", 'id="import-upload"'),
    ("/notifications", "data-new-channel-open"),
    ("/account", "data-profile-form"),
    ("/account/password", "data-account-totp"),
    ("/admin/users", "data-users"),
    ("/admin/settings", "data-instance-settings"),
]

FOCUSED = [
    ("/workspaces/new", "max-w-2xl"),
    ("/workspace/delete", "max-w-2xl"),
    ("/account/totp", "max-w-2xl"),
    ("/admin/users/new", "max-w-xl"),
    ("/admin/backups/now", "max-w-xl"),
]


@pytest.mark.parametrize(("path", "marker"), PAGES)
def test_user_menu_page_has_one_4xl_frame(owner: OwnerApp, path: str, marker: str) -> None:
    response = owner.client.get(path)
    assert response.status_code == 200, (path, response.text)
    page = response.text
    assert marker in page, path

    wrappers = re.findall(r'<div[^>]*class="max-w-4xl space-y-6"[^>]*>', page)
    assert len(wrappers) == 1, (path, wrappers)
    assert 'data-page-width="4xl"' in wrappers[0], path
    assert page.count("max-w-4xl") == 1, path

    # No panel inside the frame may reintroduce a narrower page cap.
    assert re.search(r"\bmax-w-(?:xl|2xl|3xl)\b", page) is None, path
    assert 'class="panel mt-6' not in page, path
    assert 'class="panel mb-6' not in page, path


@pytest.mark.parametrize(("path", "width_class"), FOCUSED)
def test_focused_flows_keep_their_narrow_frame(
    owner: OwnerApp, path: str, width_class: str
) -> None:
    response = owner.client.get(path)
    assert response.status_code == 200, (path, response.text)
    page = response.text
    assert width_class in page, path
    assert 'data-page-width="4xl"' not in page, path


def _upload_csv(owner: OwnerApp, content: bytes) -> str:
    response = owner.client.post(
        "/workspace/data/import",
        data={"csrf_token": owner.token("/workspace/data")},
        files={"upload": ("ledger.csv", content, "text/csv")},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return response.headers["location"].split("batch=", 1)[1]


def test_import_rollback_keeps_its_narrow_frame(owner: OwnerApp) -> None:
    batch_id = _upload_csv(
        owner,
        b"date,direction,amount,category,description\n2026-10-02,expense,12.50,Food,Lunch\n",
    )
    applied = owner.post(f"/workspace/data/import/{batch_id}/apply", token_from="/workspace/data")
    assert applied.status_code == 303, applied.text

    page = owner.client.get(f"/workspace/data/import/{batch_id}/rollback")
    assert page.status_code == 200
    assert "Confirm import rollback" in page.text
    assert "max-w-2xl" in page.text
    assert 'data-page-width="4xl"' not in page.text


def test_admin_confirm_keeps_its_narrow_frame(owner: OwnerApp) -> None:
    target = owner.add_user("width-confirm")
    page = owner.client.get(f"/admin/users/{target}/deactivate")
    assert page.status_code == 200
    assert "data-admin-confirm" in page.text
    assert "max-w-xl" in page.text
    assert 'data-page-width="4xl"' not in page.text


def test_admin_user_created_keeps_its_narrow_frame(owner: OwnerApp) -> None:
    response = owner.client.post(
        "/admin/users/new",
        data={
            "csrf_token": owner.token("/admin/users/new"),
            "email": "width-created@example.com",
            "display_name": "Width Created",
            "confirm_password": owner.web.admin_password,
        },
        follow_redirects=False,
    )
    assert response.status_code == 200, response.text
    assert "data-admin-created" in response.text
    assert "max-w-xl" in response.text
    assert 'data-page-width="4xl"' not in response.text
