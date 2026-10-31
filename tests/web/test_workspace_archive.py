from __future__ import annotations

from io import BytesIO
from zipfile import ZipFile

from sqlalchemy import select

from assetboard.db.models.identity import AuditEvent
from assetboard.domain.archive import parse_archive
from tests.conftest import OwnerApp


def test_editor_can_download_and_audit_workspace_archive(owner: OwnerApp) -> None:
    page = owner.client.get("/workspace/data")
    assert page.status_code == 200
    assert "data-workspace-data-link" in owner.client.get("/").text

    response = owner.post("/workspace/data/export", token_from="/workspace/data")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["cache-control"] == "no-store"
    assert "assetboard-home-" in response.headers["content-disposition"]
    parsed, errors = parse_archive(response.content)
    assert errors == () and parsed is not None
    with ZipFile(BytesIO(response.content)) as archive:
        assert archive.namelist() == [
            "manifest.json",
            "workspace.json",
            "categories.csv",
            "budget_lines.csv",
            "transactions.csv",
            "months.csv",
        ]
    with owner.db() as session:
        event = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "workspace.exported")
        ).one()
        assert event.workspace_id == owner.home_id
        assert set(event.data) == {"categories", "budget_lines", "transactions", "months"}
        assert "amount" not in event.data


def test_viewer_cannot_open_or_download_workspace_archive(owner: OwnerApp) -> None:
    owner.set_admin_role("viewer")
    assert owner.client.get("/workspace/data").status_code == 403
    response = owner.post("/workspace/data/export", token_from="/")
    assert response.status_code == 403


def test_export_requires_csrf_and_allows_editor(owner: OwnerApp) -> None:
    owner.set_admin_role("editor")
    assert owner.client.get("/workspace/data").status_code == 200
    assert owner.client.post("/workspace/data/export").status_code == 403
    assert owner.post("/workspace/data/export", token_from="/workspace/data").status_code == 200
