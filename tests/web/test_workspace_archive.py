from __future__ import annotations

from datetime import timedelta
from io import BytesIO
from zipfile import ZipFile

from sqlalchemy import select
from sqlalchemy.orm import Session

from assetboard.db.models.identity import AuditEvent
from assetboard.db.models.import_batch import ImportBatch
from assetboard.domain.archive import parse_archive
from assetboard.services.context import Context
from assetboard.services.imports import ImportAdapter, cleanup_expired_imports
from tests.conftest import OwnerApp


class TrivialImportAdapter(ImportAdapter):
    def __init__(self) -> None:
        self.plan_counts = [1]
        self.applied = False
        self.closed_months: tuple[str, ...] = ()

    def plan(
        self,
        session: Session,
        ctx: Context,
        batch: ImportBatch,
        parsed: object,
        *,
        base_currency: str,
    ) -> dict[str, object]:
        count = self.plan_counts.pop(0) if len(self.plan_counts) > 1 else self.plan_counts[0]
        row_errors = getattr(parsed, "row_errors", ())
        return {
            "source": batch.source,
            "counts": {
                "to_create": count,
                "duplicates_skipped": 0,
                "conflicts_skipped": 0,
                "closed_months_skipped": 0,
                "ignored_categories_skipped": 0,
                "invalid": getattr(parsed, "invalid_row_count", len(row_errors)),
            },
            "months": [
                {
                    "month": "2026-10",
                    "expense_total": "10.00 EUR",
                    "income_total": "0.00 EUR",
                    "planned_total": "8.00 EUR",
                    "rows_to_create": 1,
                    "rows_skipped": 0,
                }
            ],
            "categories": [
                {
                    "kind": "expense",
                    "category": "Food",
                    "total": "10.00 EUR",
                    "rows_to_create": 1,
                }
            ],
            "closed_months": [],
            "warnings": [],
            "errors": [
                {
                    "file": error.file,
                    "line": error.line,
                    "column": error.column,
                    "code": "invalid_upload",
                    "message": error.message,
                }
                for error in row_errors
            ],
        }

    def apply(
        self,
        session: Session,
        ctx: Context,
        batch: ImportBatch,
        parsed: object,
        *,
        base_currency: str,
        skip_invalid: bool,
    ) -> dict[str, int]:
        self.applied = True
        return {"created": 1, "duplicates": 0, "conflicts": 0, "skipped": 0}

    def rollback(
        self, session: Session, ctx: Context, batch: ImportBatch
    ) -> tuple[dict[str, int], tuple[str, ...]]:
        batch.report = {"synthetic": True}
        return {"deleted": 1, "kept": 0}, self.closed_months

    def rollback_preview(
        self, session: Session, ctx: Context, batch: ImportBatch
    ) -> dict[str, int]:
        return {"deleted": 1, "edited": 0, "budget_lines": 0, "categories_to_archive": 0}


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
    uploaded = owner.client.post(
        "/workspace/data/import",
        data={"csrf_token": owner.token("/")},
        files={"upload": ("ledger.csv", b"date,direction,amount\n", "text/csv")},
        follow_redirects=False,
    )
    assert uploaded.status_code == 403


def test_export_requires_csrf_and_allows_editor(owner: OwnerApp) -> None:
    owner.set_admin_role("editor")
    assert owner.client.get("/workspace/data").status_code == 200
    assert owner.client.post("/workspace/data/export").status_code == 403
    assert owner.post("/workspace/data/export", token_from="/workspace/data").status_code == 200
    assert owner.client.post("/workspace/data/import").status_code == 403


def test_import_upload_is_stored_and_planned(owner: OwnerApp) -> None:
    owner.app.state.import_adapter = TrivialImportAdapter()
    response = owner.client.post(
        "/workspace/data/import",
        data={"csrf_token": owner.token("/workspace/data")},
        files={"upload": ("../ledger.csv", b"date,direction,amount\n", "text/csv")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    location = response.headers["location"]
    page = owner.client.get(location)
    assert page.status_code == 200
    assert "ledger.csv" in page.text
    assert "Import report" in page.text
    assert "Food" in page.text
    assert "2026-10" in page.text
    with owner.db() as session:
        batch = session.scalars(select(ImportBatch)).one()
        path = owner.app.state.settings.data_dir / "imports" / str(owner.home_id) / str(batch.id)
        assert batch.status == "planned"
        assert batch.source == "transactions_csv"
        assert batch.options == {"delimiter": "comma", "decimal": "dot"}
        assert path.read_bytes() == b"date,direction,amount\n"
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
        assert path.parent.parent.stat().st_mode & 0o777 == 0o700


def test_import_rejects_decimal_comma_without_semicolon_delimiter(owner: OwnerApp) -> None:
    response = owner.client.post(
        "/workspace/data/import",
        data={
            "csrf_token": owner.token("/workspace/data"),
            "delimiter": "comma",
            "decimal": "comma",
        },
        files={"upload": ("ledger.csv", b"date,direction,amount\n", "text/csv")},
        follow_redirects=False,
    )

    assert response.status_code == 422


def test_invalid_import_has_downloadable_error_csv(owner: OwnerApp) -> None:
    owner.app.state.import_adapter = TrivialImportAdapter()
    response = owner.client.post(
        "/workspace/data/import",
        data={"csrf_token": owner.token("/workspace/data")},
        files={"upload": ("ledger.csv", b"date,amount\n2026-01-01\n", "text/csv")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    with owner.db() as session:
        batch = session.scalars(select(ImportBatch)).one()
        assert batch.status == "planned"
        assert batch.report is not None
        assert batch.report["counts"]["invalid"] == 1
        identifier = batch.id
        stored_path = (
            owner.app.state.settings.data_dir / "imports" / str(owner.home_id) / str(batch.id)
        )
        assert stored_path.exists()
    errors = owner.client.get(f"/workspace/data/import/{identifier}/errors.csv")
    assert errors.status_code == 200
    assert "file,line,column,code,message" in errors.text
    assert "different number of columns" in errors.text


def test_import_apply_and_rollback_run_through_adapter_and_audit(owner: OwnerApp) -> None:
    adapter = TrivialImportAdapter()
    owner.app.state.import_adapter = adapter
    uploaded = owner.client.post(
        "/workspace/data/import",
        data={"csrf_token": owner.token("/workspace/data")},
        files={"upload": ("ledger.csv", b"date,direction,amount\n", "text/csv")},
        follow_redirects=False,
    )
    batch_id = uploaded.headers["location"].split("batch=", 1)[1]

    applied = owner.post(
        f"/workspace/data/import/{batch_id}/apply",
        token_from="/workspace/data",
    )
    assert applied.status_code == 303
    assert adapter.applied
    with owner.db() as session:
        batch = session.scalars(select(ImportBatch)).one()
        assert batch.status == "applied"
        assert batch.applied_at is not None
        event = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "import.applied")
        ).one()
        assert event.data == {
            "source": "transactions_csv",
            "mode": "merge",
            "transactions_created": 1,
            "duplicates": 0,
            "conflicts": 0,
            "rows_skipped": 0,
        }

    preview = owner.client.get(f"/workspace/data/import/{batch_id}/rollback")
    assert preview.status_code == 200
    assert "Confirm import rollback" in preview.text
    assert "Transactions to remove" in preview.text

    rolled_back = owner.post(
        f"/workspace/data/import/{batch_id}/rollback",
        token_from="/workspace/data",
    )
    assert rolled_back.status_code == 303
    with owner.db() as session:
        batch = session.scalars(select(ImportBatch)).one()
        assert batch.status == "rolled_back"
        assert batch.rolled_back_at is not None


def test_changed_import_plan_requires_confirmation_and_closed_rollback_is_atomic(
    owner: OwnerApp,
) -> None:
    adapter = TrivialImportAdapter()
    adapter.plan_counts = [1, 2]
    owner.app.state.import_adapter = adapter
    uploaded = owner.client.post(
        "/workspace/data/import",
        data={"csrf_token": owner.token("/workspace/data")},
        files={"upload": ("ledger.csv", b"date,direction,amount\n", "text/csv")},
        follow_redirects=False,
    )
    batch_id = uploaded.headers["location"].split("batch=", 1)[1]

    changed = owner.post(
        f"/workspace/data/import/{batch_id}/apply",
        token_from="/workspace/data",
    )
    assert changed.status_code == 303
    assert not adapter.applied
    with owner.db() as session:
        batch = session.scalars(select(ImportBatch)).one()
        assert batch.status == "planned"
        assert batch.report is not None
        assert batch.report["counts"]["to_create"] == 2

    adapter.plan_counts = [2]
    owner.post(f"/workspace/data/import/{batch_id}/apply", token_from="/workspace/data")
    adapter.closed_months = ("2026-10",)
    refused = owner.post(
        f"/workspace/data/import/{batch_id}/rollback",
        token_from="/workspace/data",
    )
    assert refused.status_code == 409
    assert "2026-10" in refused.json()["detail"]
    with owner.db() as session:
        batch = session.scalars(select(ImportBatch)).one()
        assert batch.status == "applied"
        assert batch.report != {"synthetic": True}


def test_stale_import_cleanup_removes_private_file_and_marks_batch_failed(
    owner: OwnerApp,
) -> None:
    owner.client.post(
        "/workspace/data/import",
        data={"csrf_token": owner.token("/workspace/data")},
        files={"upload": ("ledger.csv", b"date,direction,amount\n", "text/csv")},
        follow_redirects=False,
    )
    with owner.db() as session, session.begin():
        batch = session.scalars(select(ImportBatch)).one()
        batch_id = batch.id
        batch.created_at = owner.app.state.clock.now() - timedelta(days=2)
    path = owner.app.state.settings.data_dir / "imports" / str(owner.home_id) / str(batch_id)
    assert path.exists()

    expired = cleanup_expired_imports(
        owner.app.state.session_factory,
        owner.app.state.settings.data_dir,
        owner.app.state.clock,
    )

    assert expired == 1
    assert not path.exists()
    with owner.db() as session:
        batch = session.scalars(select(ImportBatch)).one()
        assert batch.status == "failed"


def test_planned_batch_can_be_cancelled_and_unavailable_apply_is_refused(
    owner: OwnerApp,
) -> None:
    uploaded = owner.client.post(
        "/workspace/data/import",
        data={"csrf_token": owner.token("/workspace/data")},
        files={"upload": ("ledger.csv", b"date,direction,amount\n", "text/csv")},
        follow_redirects=False,
    )
    batch_id = uploaded.headers["location"].split("batch=", 1)[1]

    unavailable = owner.post(
        f"/workspace/data/import/{batch_id}/apply",
        token_from="/workspace/data",
    )
    assert unavailable.status_code == 409
    cancelled = owner.post(
        f"/workspace/data/import/{batch_id}/cancel",
        token_from="/workspace/data",
    )
    assert cancelled.status_code == 303
    with owner.db() as session:
        batch = session.scalars(select(ImportBatch)).one()
        assert batch.status == "cancelled"


def test_apply_refuses_a_stored_upload_whose_checksum_changed(owner: OwnerApp) -> None:
    adapter = TrivialImportAdapter()
    owner.app.state.import_adapter = adapter
    uploaded = owner.client.post(
        "/workspace/data/import",
        data={"csrf_token": owner.token("/workspace/data")},
        files={"upload": ("ledger.csv", b"date,direction,amount\n", "text/csv")},
        follow_redirects=False,
    )
    batch_id = uploaded.headers["location"].split("batch=", 1)[1]
    with owner.db() as session:
        batch = session.scalars(select(ImportBatch)).one()
        stored_path = (
            owner.app.state.settings.data_dir / "imports" / str(owner.home_id) / str(batch.id)
        )
    stored_path.write_bytes(b"date,direction,amount\nchanged\n")

    refused = owner.post(
        f"/workspace/data/import/{batch_id}/apply",
        token_from="/workspace/data",
    )

    assert refused.status_code == 409
    assert not adapter.applied
    with owner.db() as session:
        batch = session.scalars(select(ImportBatch)).one()
        assert batch.status == "failed"
        assert batch.report is not None
        assert batch.report["errors"][0]["code"] == "checksum_mismatch"
