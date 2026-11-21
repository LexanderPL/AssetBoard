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
    ("/workspace/new", "max-w-2xl"),
    ("/workspace/delete", "max-w-2xl"),
    ("/account/totp", "max-w-2xl"),
    ("/admin/users/new", "max-w-xl"),
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
    page = owner.client.get(path).text
    assert width_class in page, path
    assert 'data-page-width="4xl"' not in page, path
