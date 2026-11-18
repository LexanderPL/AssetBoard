"""Role × route authorization matrix (F9.6).

The app authenticates and authorizes in layers: ``AccessGateMiddleware`` decides whether a
request is signed in, and services call ``require_role`` before doing work. This test checks
that the two combine as documented: a viewer may read every workspace page and manage only
their own preferences; an editor may change workspace data; an owner may also change the
workspace and its members; an instance administrator may reach the instance pages. A route
must never answer 200 to a role that is below its required role.

The matrix is representative rather than exhaustive per feature: the feature suites assert
their own role rules, and ``test_csrf_origin.py`` walks every unsafe route for CSRF.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from tests.conftest import ADMIN_PASSWORD

if TYPE_CHECKING:
    from conftest import OwnerApp, WebHarness

NO_PERMISSION = "You do not have permission to view this page."

# Every signed-in member, whatever their workspace role.
READ_FOR_ALL = (
    "/",
    "/m/2026-01",
    "/transactions",
    "/transactions/2026-01",
    "/history",
    "/categories",
    "/income",
    "/investments",
    "/investments/instruments",
    "/investments/transactions",
    "/investments/portfolio",
    "/investments/performance",
    "/net-worth",
    "/net-worth/summary",
    "/net-worth/history",
    "/recurring",
    "/upcoming",
    "/loans",
    "/loans/installments",
    "/fire",
    "/notifications",
    "/account",
    "/account/password",
    "/account/totp",
    "/dashboard/customise",
)

# Export/import is for editors and owners (the archive may include every transaction).
READ_EDITORS = ("/workspace/data",)

# Owner settings, membership, deletion, the workspace audit log and provider credentials.
READ_OWNERS = (
    "/workspace/settings",
    "/workspace/members",
    "/workspace/delete",
    "/investments/providers",
    "/audit?scope=workspace",
)

# Instance administration is separate from any workspace role.
READ_ADMINS = (
    "/admin/users",
    "/admin/users/new",
    "/admin/settings",
    "/audit?scope=instance",
)

# (path, form, minimum role). The form is deliberately invalid for the allowed role so the
# test never creates data; a refusal by the role guard happens before validation and is a 403.
WRITES = (
    ("POST", "/categories", {"name": ""}, "editor", 400),
    ("POST", "/workspace/settings", {"name": ""}, "owner", 400),
    ("POST", "/workspace/invitations", {"role": "no-such-role"}, "owner", 400),
    ("POST", "/admin/settings", {}, "admin", 400),
)

ROLE_RANK = {"viewer": 1, "editor": 2, "owner": 3, "admin": 4}


@contextmanager
def signed_in_as(owner: OwnerApp, email: str) -> Iterator[TestClient]:
    with TestClient(owner.app) as client:
        response = owner.web.sign_in(client, email=email, password=ADMIN_PASSWORD)
        assert response.status_code == 303, response.text
        yield client


@pytest.fixture
def principals(owner: OwnerApp) -> Iterator[dict[str, TestClient]]:
    """A viewer, an editor, a regular owner and the instance administrator, all signed in."""
    created: dict[str, str] = {}
    for key, role in (("viewer", "viewer"), ("editor", "editor"), ("owner", "owner")):
        user_id = owner.add_user(key)
        owner.join(user_id, owner.home_id, role)
        created[key] = f"{key}@example.com"

    with ExitStack() as stack:
        clients = {
            key: stack.enter_context(signed_in_as(owner, email)) for key, email in created.items()
        }
        # The fixture's client is the first-run administrator: workspace owner and instance admin.
        clients["admin"] = owner.client
        yield clients


def _allowed(role: str, minimum: str) -> bool:
    return ROLE_RANK[role] >= ROLE_RANK[minimum]


def _post(client: TestClient, web: WebHarness, path: str, data: dict[str, str]) -> Response:
    token = web.csrf(client.get("/").text)
    return client.post(path, data={"csrf_token": token, **data}, follow_redirects=False)


def test_read_matrix(principals: dict[str, TestClient]) -> None:
    for path in READ_FOR_ALL:
        for role, client in principals.items():
            response = client.get(path)
            assert response.status_code == 200, (role, path, response.status_code)

    for path, minimum in (
        *((path, "editor") for path in READ_EDITORS),
        *((path, "owner") for path in READ_OWNERS),
        *((path, "admin") for path in READ_ADMINS),
    ):
        for role, client in principals.items():
            response = client.get(path)
            if _allowed(role, minimum):
                assert response.status_code == 200, (role, path, response.status_code)
            else:
                assert response.status_code == 403, (role, path, response.status_code)
                assert NO_PERMISSION in response.text or "instance administrator" in response.text


def test_bare_audit_scope_depends_on_the_role(principals: dict[str, TestClient]) -> None:
    # An owner sees the workspace log; an instance administrator without ownership would see
    # the instance log; a viewer/editor sees neither and gets 403.
    for role, client in principals.items():
        response = client.get("/audit")
        if role in {"owner", "admin"}:
            assert response.status_code == 200, (role, response.status_code)
        else:
            assert response.status_code == 403, (role, response.status_code)


def test_write_matrix(principals: dict[str, TestClient], owner: OwnerApp) -> None:
    for _method, path, data, minimum, allowed_status in WRITES:
        for role, client in principals.items():
            response = _post(client, owner.web, path, data)
            if _allowed(role, minimum):
                assert response.status_code == allowed_status, (role, path, response.status_code)
            else:
                assert response.status_code == 403, (role, path, response.status_code)
                assert NO_PERMISSION in response.text or "instance administrator" in response.text


def test_personal_preferences_are_open_to_viewers(
    principals: dict[str, TestClient], owner: OwnerApp
) -> None:
    for role, client in principals.items():
        response = _post(client, owner.web, "/dashboard/customise", {})
        assert response.status_code == 303, (role, response.status_code)
        assert response.headers["location"].startswith("/")


def test_an_instance_admin_with_a_viewer_membership_keeps_only_admin_pages(owner: OwnerApp) -> None:
    """Instance administration never implies workspace ownership (and vice versa)."""
    other = owner.add_workspace("Admin as viewer")
    owner.join(owner.admin_id, other, "viewer")
    switched = owner.post("/workspace/switch", {"workspace_id": str(other)})
    assert switched.status_code == 303
    try:
        assert owner.client.get("/admin/users").status_code == 200
        assert owner.client.get("/audit").status_code == 200  # no workspace ownership: instance
        assert owner.client.get("/workspace/settings").status_code == 403
        assert owner.client.get("/audit?scope=workspace").status_code == 403
    finally:
        owner.post("/workspace/switch", {"workspace_id": str(owner.home_id)})
