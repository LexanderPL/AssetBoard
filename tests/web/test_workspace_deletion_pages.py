"""Deleting a workspace over HTTP (ADR-0020): danger zone, confirmation page, CSRF, redirect."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from assetboard.db.models.identity import AuditEvent, Workspace
from assetboard.db.models.ledger import Category
from tests.conftest import OwnerApp

NO_PERMISSION = "You do not have permission to view this page."
MISMATCH = "Type the workspace name exactly as shown to confirm."


def _workspace_names(owner: OwnerApp) -> set[str]:
    with owner.db() as session:
        return set(session.scalars(select(Workspace.name)))


def test_the_settings_page_has_a_danger_zone_linking_to_the_confirmation(owner: OwnerApp) -> None:
    page = owner.client.get("/workspace/settings").text
    assert "data-danger-zone" in page
    assert 'href="/workspace/delete" ' in page and "data-delete-workspace-link" in page
    # The settings form comes first; the danger zone is the last section.
    assert page.index("data-danger-zone") > page.index("Save settings")


def test_the_confirmation_page_asks_for_the_name_and_recommends_an_export(owner: OwnerApp) -> None:
    second = owner.add_user("bea")
    owner.join(second, owner.home_id, "editor")
    response = owner.client.get("/workspace/delete")
    assert response.status_code == 200
    page = response.text
    assert "Delete Home permanently?" in page
    assert 'name="confirm_name"' in page and 'name="csrf_token"' in page
    assert 'method="post" action="/workspace/delete"' in page
    assert 'href="/transactions#export-heading"' in page and "data-export-link" in page
    assert "1 other member also loses access to it." in page
    assert _workspace_names(owner) == {"Home"}  # a GET changes nothing


@pytest.mark.parametrize("role", ["editor", "viewer"])
def test_only_owners_reach_the_page_and_delete(owner: OwnerApp, role: str) -> None:
    token = owner.token()
    owner.set_admin_role(role)
    assert "data-danger-zone" not in owner.client.get("/").text
    page = owner.client.get("/workspace/delete")
    assert page.status_code == 403 and NO_PERMISSION in page.text
    response = owner.client.post(
        "/workspace/delete", data={"csrf_token": token, "confirm_name": "Home"}
    )
    assert response.status_code == 403 and NO_PERMISSION in response.text
    assert _workspace_names(owner) == {"Home"}


def test_deleting_needs_a_csrf_token(owner: OwnerApp) -> None:
    response = owner.client.post("/workspace/delete", data={"confirm_name": "Home"})
    assert response.status_code == 403
    wrong = owner.client.post(
        "/workspace/delete", data={"csrf_token": "forged", "confirm_name": "Home"}
    )
    assert wrong.status_code == 403
    assert _workspace_names(owner) == {"Home"}


def test_a_wrong_name_shows_the_error_on_the_field(owner: OwnerApp) -> None:
    response = owner.post(
        "/workspace/delete", {"confirm_name": "home"}, token_from="/workspace/delete"
    )
    assert response.status_code == 400
    assert MISMATCH in response.text
    assert 'aria-describedby="confirm_name-error"' in response.text
    assert _workspace_names(owner) == {"Home"}


def test_deleting_moves_to_another_workspace(owner: OwnerApp) -> None:
    second = owner.add_workspace("Second")
    owner.join(owner.admin_id, second, "owner")
    home_id = owner.home_id
    switched = owner.post("/workspace/switch", {"workspace_id": str(home_id)})
    assert switched.status_code == 303

    response = owner.post(
        "/workspace/delete", {"confirm_name": "Home"}, token_from="/workspace/delete"
    )
    assert (response.status_code, response.headers["location"]) == (303, "/")
    assert _workspace_names(owner) == {"Second"}
    home = owner.client.get("/")
    assert home.status_code == 200 and "data-current-workspace>Second<" in home.text
    with owner.db() as session:
        count = select(func.count()).select_from(Category)
        assert session.scalar(count.where(Category.workspace_id == home_id)) == 0
        [event] = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "workspace.delete")
        ).all()
        assert event.workspace_id is None and event.data == {"name": "Home"}


def test_deleting_the_only_workspace_leads_to_the_no_workspace_page(owner: OwnerApp) -> None:
    response = owner.post(
        "/workspace/delete", {"confirm_name": "Home"}, token_from="/workspace/delete"
    )
    assert (response.status_code, response.headers["location"]) == (303, "/")
    assert _workspace_names(owner) == set()
    home = owner.client.get("/")
    assert home.status_code == 403 and "data-no-workspace" in home.text
    assert "data-create-workspace" in home.text  # creation is allowed by default
    assert owner.client.get("/account/password").status_code == 200  # account pages still work


def test_the_audit_view_shows_the_deletion_to_instance_administrators(owner: OwnerApp) -> None:
    second = owner.add_workspace("Second")
    owner.join(owner.admin_id, second, "owner")
    owner.post("/workspace/delete", {"confirm_name": "Home"}, token_from="/workspace/delete")
    page = owner.client.get("/audit?scope=instance&group=workspaces")
    assert page.status_code == 200
    assert "Workspace deleted" in page.text and "Home" in page.text


@pytest.mark.parametrize("locale", ["en", "pl"])
def test_the_deletion_pages_render_in_english_and_polish(owner: OwnerApp, locale: str) -> None:
    headers = {"Accept-Language": locale}
    settings = owner.client.get("/workspace/settings", headers=headers).text
    page = owner.client.get("/workspace/delete", headers=headers).text
    if locale == "pl":
        assert "Usuń obszar roboczy" in settings
        assert "Usunąć Home na zawsze?" in page
    else:
        assert "Delete workspace" in settings
        assert "Delete Home permanently?" in page
