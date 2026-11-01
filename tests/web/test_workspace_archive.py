from __future__ import annotations

import os
from collections.abc import Callable
from datetime import date, timedelta
from io import BytesIO
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5
from zipfile import ZipFile

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from assetboard.db.models.identity import AuditEvent
from assetboard.db.models.import_batch import ImportBatch
from assetboard.db.models.ledger import BudgetLine, Category, MonthState, Transaction
from assetboard.db.repositories import (
    BudgetLineRepository,
    CategoryRepository,
    MonthStateRepository,
    TransactionRepository,
)
from assetboard.domain.archive import parse_archive
from assetboard.domain.categories import name_key
from assetboard.domain.import_upload import ParsedUpload, ParseTimeout
from assetboard.domain.months import YearMonth
from assetboard.domain.transactions import description_key
from assetboard.services import imports as imports_service
from assetboard.services.context import Context
from assetboard.services.import_dedupe import (
    BudgetLineMatch,
    IncomingBudgetLine,
    IncomingTransaction,
    TransactionMatch,
    match_budget_lines,
    match_transactions,
)
from assetboard.services.imports import (
    ImportAdapter,
    ImportAdapterRegistry,
    cleanup_expired_imports,
    report_message,
)
from tests.conftest import OwnerApp
from tests.tenant.harness import context_for


class TrivialImportAdapter(ImportAdapter):
    """A synthetic test importer for ``transactions_csv`` uploads.

    Header ``date,direction,amount,category,description`` (optionally ``id``); amounts are
    minor units. ``direction=plan`` rows are planned values of an expense category. Unknown
    category names are created on apply (with the batch id). It plans with the framework's
    dedupe helpers and skips rows in closed months, so it exercises F4.2 end to end.
    """

    def __init__(self) -> None:
        self.applied = False
        self.extra_to_create = 0  # added to the planned count, to simulate a changed workspace

    def _rows(self, parsed: ParsedUpload) -> list[dict[str, str]]:
        header = parsed.csv_header
        return [dict(zip(header, row, strict=True)) for row in parsed.csv_rows]

    def _categories(self, session: Session, ctx: Context) -> dict[tuple[str, str], UUID]:
        return {
            (category.kind, category.name_key): category.id
            for category in CategoryRepository(session, ctx).list()
        }

    def _decide(
        self, session: Session, ctx: Context, parsed: ParsedUpload, base_currency: str
    ) -> tuple[list[TransactionMatch], list[BudgetLineMatch], list[dict[str, object]]]:
        known = self._categories(session, ctx)
        closed = MonthStateRepository(session, ctx).closed_months()
        transactions: list[IncomingTransaction] = []
        lines: list[IncomingBudgetLine] = []
        skipped: list[dict[str, object]] = []
        for number, row in enumerate(self._rows(parsed), start=2):
            day = date.fromisoformat(row["date"])
            if YearMonth.from_date(day) in closed:
                skipped.append(
                    {"file": "upload.csv", "line": number, "column": "date",
                     "code": "closed_month", "message": "month is closed"}
                )  # fmt: skip
                continue
            kind = "expense" if row["direction"] == "plan" else row["direction"]
            key = name_key(row["category"])
            category_id = known.get((kind, key)) or uuid5(NAMESPACE_URL, f"{kind}:{key}")
            if row["direction"] == "plan":
                lines.append(
                    IncomingBudgetLine(
                        number, YearMonth.from_date(day), category_id, int(row["amount"])
                    )
                )
                continue
            raw_id = row.get("id") or ""
            transactions.append(
                IncomingTransaction(
                    line=number,
                    source_id=UUID(raw_id) if raw_id else None,
                    day=day,
                    direction=kind,
                    amount_minor=int(row["amount"]),
                    currency=base_currency,
                    category_id=category_id,
                    source_category_name_key=key,
                    description=row["description"],
                    description_key=description_key(row["description"]),
                )
            )
        matches = match_transactions(session, ctx, transactions)
        line_matches = match_budget_lines(session, ctx, lines)
        for match in [*matches, *line_matches]:
            if match.action != "create":
                skipped.append(
                    {"file": "upload.csv", "line": match.row.line, "column": "",
                     "code": match.code, "message": str(match.code)}
                )  # fmt: skip
        return matches, line_matches, skipped

    def plan(
        self,
        session: Session,
        ctx: Context,
        batch: ImportBatch,
        parsed: ParsedUpload,
        *,
        base_currency: str,
        out_of_time: Callable[[], bool] = lambda: False,
    ) -> dict[str, Any]:
        matches, line_matches, skipped = self._decide(session, ctx, parsed, base_currency)
        every = [*matches, *line_matches]
        codes = [entry["code"] for entry in skipped]
        return {
            "counts": {
                "to_create": sum(m.action == "create" for m in every) + self.extra_to_create,
                "duplicates_skipped": sum(m.action == "duplicate" for m in every),
                "conflicts_skipped": sum(m.action == "conflict" for m in every),
                "closed_months_skipped": codes.count("closed_month"),
                "ignored_categories_skipped": 0,
                "invalid": parsed.invalid_row_count,
            },
            "months": [
                {
                    "month": "2026-10",
                    "expense_total": "4.25",
                    "income_total": "0.00",
                    "planned_total": "0.00",
                    "rows_to_create": 1,
                    "rows_skipped": 0,
                }
            ],  # fmt: skip
            "categories": [
                {"kind": "expense", "category": "Food", "total": "4.25", "rows_to_create": 1}
            ],
            "closed_months": [],
            "warnings": [],
            "skipped": skipped,
            "errors": [
                {
                    "file": e.file,
                    "line": e.line,
                    "column": e.column,
                    "code": "row_column_count",
                    "message": e.message,
                }
                for e in parsed.row_errors
            ],  # fmt: skip
        }

    def apply(
        self,
        session: Session,
        ctx: Context,
        batch: ImportBatch,
        parsed: ParsedUpload,
        *,
        base_currency: str,
        skip_invalid: bool,
    ) -> dict[str, int]:
        self.applied = True
        categories = CategoryRepository(session, ctx)
        known = self._categories(session, ctx)
        for row in self._rows(parsed):
            kind = "expense" if row["direction"] == "plan" else row["direction"]
            key = name_key(row["category"])
            if (kind, key) not in known:
                created = categories.create(
                    name=row["category"], name_key=key, kind=kind, treatment="regular",
                    color="#16a34a", sort_order=99, import_batch_id=batch.id,
                )  # fmt: skip
                known[(kind, key)] = created.id
        session.flush()
        matches, line_matches, skipped = self._decide(session, ctx, parsed, base_currency)
        transactions = TransactionRepository(session, ctx)
        for match in matches:
            if match.action == "create":
                row = match.row
                transactions.create_imported(
                    direction=row.direction, day=row.day, amount_minor=row.amount_minor,
                    currency=row.currency, category_id=row.category_id,
                    description=row.description, description_key=row.description_key,
                    import_batch_id=batch.id, import_key=match.import_key,
                    created_by_user_id=ctx.user.id,
                )  # fmt: skip
        lines = BudgetLineRepository(session, ctx)
        for line_match in line_matches:
            if line_match.action == "create":
                lines.create(
                    month=line_match.row.month, category_id=line_match.row.category_id,
                    planned_minor=line_match.row.planned_minor, currency=base_currency,
                    import_batch_id=batch.id,
                )  # fmt: skip
        every = [*matches, *line_matches]
        return {
            "created": sum(m.action == "create" for m in matches),
            "duplicates": sum(m.action == "duplicate" for m in every),
            "conflicts": sum(m.action == "conflict" for m in every),
            "skipped": len(skipped),
        }


HEADER = b"date,direction,amount,category,description\n"
COFFEE = b"2026-10-03,expense,425,Food,Coffee\n"


def _upload(owner: OwnerApp, content: bytes, name: str = "ledger.csv") -> str:
    response = owner.client.post(
        "/workspace/data/import",
        data={"csrf_token": owner.token("/workspace/data")},
        files={"upload": (name, content, "text/csv")},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return response.headers["location"].split("batch=", 1)[1]


def _apply(owner: OwnerApp, batch_id: str, **data: str) -> Any:
    return owner.post(
        f"/workspace/data/import/{batch_id}/apply", token_from="/workspace/data", data=data
    )


def _batch(owner: OwnerApp, batch_id: str) -> ImportBatch:
    with owner.db() as session:
        return session.scalars(select(ImportBatch).where(ImportBatch.id == UUID(batch_id))).one()


def _imported(owner: OwnerApp) -> list[Transaction]:
    with owner.db() as session:
        return list(
            session.scalars(
                select(Transaction)
                .where(Transaction.source == "import")
                .order_by(Transaction.created_at, Transaction.id)
            )
        )


def _install(owner: OwnerApp, adapter: ImportAdapter | None = None) -> TrivialImportAdapter:
    installed = adapter or TrivialImportAdapter()
    owner.app.state.import_adapters = ImportAdapterRegistry({"transactions_csv": installed})
    return installed  # type: ignore[return-value]


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
    _install(owner)
    batch_id = _upload(owner, HEADER + COFFEE, name="../Łódź ledger.csv")

    page = owner.client.get(f"/workspace/data?batch={batch_id}")
    assert page.status_code == 200
    assert "Lodz ledger.csv" in page.text
    assert "Import report" in page.text
    assert "Food" in page.text
    assert "2026-10" in page.text
    assert "could not be planned" not in page.text
    batch = _batch(owner, batch_id)
    path = owner.app.state.settings.data_dir / "imports" / str(owner.home_id) / batch_id
    assert batch.status == "planned"
    assert batch.source == "transactions_csv"
    assert batch.filename == "Lodz ledger.csv"
    assert batch.options == {"delimiter": "comma", "decimal": "dot"}
    assert batch.report is not None and batch.report["counts"]["to_create"] == 1
    assert path.read_bytes() == HEADER + COFFEE
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
        files={"upload": ("ledger.csv", HEADER, "text/csv")},
        follow_redirects=False,
    )

    assert response.status_code == 422


def test_invalid_rows_need_confirmation_and_are_in_the_errors_csv(owner: OwnerApp) -> None:
    adapter = _install(owner)
    batch_id = _upload(owner, HEADER + COFFEE + b"2026-10-04,expense\n")

    batch = _batch(owner, batch_id)
    assert batch.status == "planned"
    assert batch.report is not None and batch.report["counts"]["invalid"] == 1
    page = owner.client.get(f"/workspace/data?batch={batch_id}")
    assert "Skip 1 invalid rows" in page.text
    assert "could not be planned" not in page.text
    errors = owner.client.get(f"/workspace/data/import/{batch_id}/errors.csv")
    assert errors.status_code == 200
    assert errors.text.startswith("file,line,column,code,message")
    assert "row_column_count" in errors.text
    assert "different number of columns" in errors.text

    assert _apply(owner, batch_id).status_code == 409
    assert not adapter.applied
    assert _apply(owner, batch_id, skip_invalid="true").status_code == 303
    assert len(_imported(owner)) == 1


def test_failed_upload_shows_translated_errors(owner: OwnerApp) -> None:
    _install(owner)
    batch_id = _upload(owner, b"\xff\xfe not text")

    assert _batch(owner, batch_id).status == "failed"
    page = owner.client.get(f"/workspace/data?batch={batch_id}")
    assert "Invalid UTF-8 encoding." in page.text
    assert "could not be planned" in page.text
    errors = owner.client.get(f"/workspace/data/import/{batch_id}/errors.csv")
    assert "invalid_utf8" in errors.text


def test_re_import_is_idempotent_and_counts_identical_rows(owner: OwnerApp) -> None:
    _install(owner)
    two_coffees = HEADER + COFFEE + COFFEE + b"2026-10-05,income,90000,Salary,October\n"

    first = _upload(owner, two_coffees)
    assert _apply(owner, first).status_code == 303
    imported = _imported(owner)
    assert len(imported) == 3  # both identical coffees are imported
    assert {row.import_batch_id for row in imported} == {UUID(first)}
    assert all(row.import_key and row.import_key.startswith("c1:") for row in imported)
    with owner.db() as session:
        event = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "import.applied")
        ).one()
        assert event.data == {
            "source": "transactions_csv",
            "mode": "merge",
            "transactions_created": 3,
            "duplicates": 0,
            "conflicts": 0,
            "rows_skipped": 0,
        }
        assert "ledger.csv" not in str(event.data)

    second = _upload(owner, two_coffees)
    report = _batch(owner, second).report
    assert report is not None
    assert report["counts"]["to_create"] == 0
    assert report["counts"]["duplicates_skipped"] == 3
    assert _apply(owner, second).status_code == 303
    assert len(_imported(owner)) == 3
    errors = owner.client.get(f"/workspace/data/import/{second}/errors.csv")
    assert errors.text.count("already_imported") == 3
    assert "Already imported." in errors.text

    # A later file that repeats the month with one more identical coffee adds only that row.
    three = _upload(owner, HEADER + COFFEE + COFFEE + COFFEE)
    assert _apply(owner, three).status_code == 303
    assert len(_imported(owner)) == 4


def test_deleted_imported_row_is_not_brought_back(owner: OwnerApp) -> None:
    _install(owner)
    first = _upload(owner, HEADER + COFFEE)
    _apply(owner, first)
    [row] = _imported(owner)
    with owner.db() as session, session.begin():
        stored = session.get(Transaction, row.id)
        assert stored is not None
        stored.deleted_at = owner.app.state.clock.now()

    again = _upload(owner, HEADER + COFFEE)
    report = _batch(owner, again).report
    assert report is not None
    assert report["counts"]["to_create"] == 0
    assert [entry["code"] for entry in report["skipped"]] == ["imported_before_deleted"]
    _apply(owner, again)
    assert [r.deleted_at is not None for r in _imported(owner)] == [True]


def test_changed_import_plan_requires_confirmation(owner: OwnerApp) -> None:
    adapter = _install(owner)
    batch_id = _upload(owner, HEADER + COFFEE)
    adapter.extra_to_create = 1  # the workspace "changed" since the dry-run

    changed = _apply(owner, batch_id)

    assert changed.status_code == 303
    assert not adapter.applied
    batch = _batch(owner, batch_id)
    assert batch.status == "planned"
    assert batch.report is not None and batch.report["counts"]["to_create"] == 2
    assert _apply(owner, batch_id).status_code == 303
    assert adapter.applied


def test_rollback_removes_the_batch_and_keeps_later_changes(owner: OwnerApp) -> None:
    _install(owner)
    content = (
        HEADER
        + COFFEE
        + b"2026-10-04,expense,300,Imported only,Bus\n"
        + b"2026-10-01,plan,5000,Planned only,Plan\n"
        + b"2026-11-01,plan,7000,Planned only,Plan\n"
    )
    batch_id = _upload(owner, content)
    assert _apply(owner, batch_id).status_code == 303
    with owner.db() as session, session.begin():
        lines = session.scalars(
            select(BudgetLine).where(BudgetLine.import_batch_id == UUID(batch_id))
        ).all()
        assert len(lines) == 2
        november = next(line for line in lines if line.month == date(2026, 11, 1))
        november.planned_minor = 7500  # changed after the import: kept on rollback
        november.updated_at = owner.app.state.clock.now() + timedelta(minutes=5)
        coffee = session.scalars(
            select(Transaction).where(Transaction.description == "Coffee")
        ).one()
        coffee.description = "Coffee (edited)"
        coffee.updated_at = owner.app.state.clock.now() + timedelta(minutes=5)

    preview = owner.client.get(f"/workspace/data/import/{batch_id}/rollback")
    assert preview.status_code == 200
    assert "Confirm import rollback" in preview.text
    assert "1 of the transactions were edited" in preview.text

    rolled_back = owner.post(
        f"/workspace/data/import/{batch_id}/rollback", token_from="/workspace/data"
    )

    assert rolled_back.status_code == 303
    assert all(row.deleted_at is not None for row in _imported(owner))
    with owner.db() as session:
        remaining = session.scalars(
            select(BudgetLine).where(BudgetLine.import_batch_id == UUID(batch_id))
        ).all()
        assert [line.planned_minor for line in remaining] == [7500]
        created = {
            category.name: category.archived_at is not None
            for category in session.scalars(
                select(Category).where(Category.import_batch_id == UUID(batch_id))
            )
        }
        # "Imported only" keeps its soft-deleted row, "Planned only" its kept line: archived.
        assert created == {"Imported only": True, "Planned only": True}
        batch = session.get(ImportBatch, UUID(batch_id))
        assert batch is not None and batch.status == "rolled_back"
        event = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "import.rolled_back")
        ).one()
        assert event.data == {
            "source": "transactions_csv",
            "mode": "merge",
            "transactions_deleted": 2,
            "transactions_edited": 1,
            "budget_lines": 1,
            "budget_lines_kept": 1,
            "categories_deleted": 0,
            "categories_archived": 2,
        }


def test_rollback_deletes_a_created_category_nothing_uses_any_more(owner: OwnerApp) -> None:
    _install(owner)
    batch_id = _upload(owner, HEADER + b"2026-10-01,plan,5000,Plan only,Plan\n")
    _apply(owner, batch_id)

    owner.post(f"/workspace/data/import/{batch_id}/rollback", token_from="/workspace/data")

    with owner.db() as session:
        assert session.scalars(select(Category).where(Category.name == "Plan only")).all() == []
        assert session.scalars(select(BudgetLine)).all() == []


def test_rollback_is_refused_as_a_whole_when_a_month_is_closed(owner: OwnerApp) -> None:
    _install(owner)
    batch_id = _upload(owner, HEADER + COFFEE + b"2026-11-02,expense,100,Food,Tea\n")
    _apply(owner, batch_id)
    with owner.db() as session, session.begin():
        session.add(
            MonthState(
                workspace_id=owner.home_id,
                month=date(2026, 10, 1),
                closed_at=owner.app.state.clock.now(),
            )
        )

    preview = owner.client.get(f"/workspace/data/import/{batch_id}/rollback")
    assert "2026-10" in preview.text
    assert "Confirm rollback" not in preview.text
    refused = owner.post(
        f"/workspace/data/import/{batch_id}/rollback", token_from="/workspace/data"
    )

    assert refused.status_code == 409
    assert "2026-10" in refused.json()["detail"]
    assert all(row.deleted_at is None for row in _imported(owner))
    assert _batch(owner, batch_id).status == "applied"


def test_rows_in_closed_months_are_skipped_and_listed(owner: OwnerApp) -> None:
    _install(owner)
    with owner.db() as session, session.begin():
        session.add(
            MonthState(
                workspace_id=owner.home_id,
                month=date(2026, 10, 1),
                closed_at=owner.app.state.clock.now(),
            )
        )
    batch_id = _upload(owner, HEADER + COFFEE + b"2026-11-02,expense,100,Food,Tea\n")

    report = _batch(owner, batch_id).report
    assert report is not None
    assert report["counts"]["closed_months_skipped"] == 1
    _apply(owner, batch_id)
    assert [row.description for row in _imported(owner)] == ["Tea"]


def test_stale_import_cleanup_removes_private_file_and_marks_batch_failed(
    owner: OwnerApp,
) -> None:
    batch_id = _upload(owner, HEADER)
    with owner.db() as session, session.begin():
        batch = session.get(ImportBatch, UUID(batch_id))
        assert batch is not None
        batch.created_at = owner.app.state.clock.now() - timedelta(days=2)
    imports = owner.app.state.settings.data_dir / "imports" / str(owner.home_id)
    path = imports / batch_id
    orphan = imports / str(uuid4())
    fresh_orphan = imports / str(uuid4())
    orphan.write_bytes(b"left behind")
    fresh_orphan.write_bytes(b"upload in progress")
    old = (owner.app.state.clock.now() - timedelta(hours=2)).timestamp()
    os.utime(orphan, (old, old))
    assert path.exists()

    expired = cleanup_expired_imports(
        owner.app.state.session_factory,
        context_for(owner.home_id, owner.app.state.clock),
        owner.app.state.settings.data_dir,
        owner.app.state.clock,
    )

    assert expired == 1
    assert not path.exists()
    assert not orphan.exists()
    assert fresh_orphan.exists()
    batch = _batch(owner, batch_id)
    assert batch.status == "failed"
    assert batch.report is not None and batch.report["warnings"][0]["code"] == "expired"


def test_planned_batch_can_be_cancelled_and_unavailable_apply_is_refused(
    owner: OwnerApp,
) -> None:
    batch_id = _upload(owner, HEADER)
    page = owner.client.get(f"/workspace/data?batch={batch_id}")
    assert "no importer for this kind of file yet" in page.text

    assert _apply(owner, batch_id).status_code == 409
    cancelled = owner.post(
        f"/workspace/data/import/{batch_id}/cancel", token_from="/workspace/data"
    )
    assert cancelled.status_code == 303
    assert _batch(owner, batch_id).status == "cancelled"
    path = owner.app.state.settings.data_dir / "imports" / str(owner.home_id) / batch_id
    assert not path.exists()


def test_apply_refuses_a_stored_upload_whose_checksum_changed(owner: OwnerApp) -> None:
    adapter = _install(owner)
    batch_id = _upload(owner, HEADER + COFFEE)
    path = owner.app.state.settings.data_dir / "imports" / str(owner.home_id) / batch_id
    path.write_bytes(HEADER + b"2026-10-03,expense,999,Food,Changed\n")

    refused = _apply(owner, batch_id)

    assert refused.status_code == 409
    assert not adapter.applied
    batch = _batch(owner, batch_id)
    assert batch.status == "failed"
    assert batch.report is not None
    assert batch.report["errors"][0]["code"] == "stored_checksum_mismatch"


def test_apply_with_a_missing_stored_file_fails_the_batch(owner: OwnerApp) -> None:
    adapter = _install(owner)
    batch_id = _upload(owner, HEADER + COFFEE)
    path = owner.app.state.settings.data_dir / "imports" / str(owner.home_id) / batch_id
    path.unlink()

    refused = _apply(owner, batch_id)

    assert refused.status_code == 409
    assert not adapter.applied
    batch = _batch(owner, batch_id)
    assert batch.status == "failed"
    assert batch.report is not None
    assert batch.report["errors"][0]["code"] == "stored_file_missing"


def test_planning_over_the_time_limit_fails_and_stops_the_worker(
    owner: OwnerApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    class SlowAdapter(TrivialImportAdapter):
        stopped = False

        def plan(
            self, *args: Any, out_of_time: Callable[[], bool] = lambda: False, **kwargs: Any
        ) -> Any:  # noqa: E501
            assert out_of_time()  # the deadline has passed when planning starts
            SlowAdapter.stopped = True
            raise ParseTimeout

    ticks = iter([0.0, 1000.0, 1000.0, 1000.0])
    monkeypatch.setattr(imports_service, "_monotonic", lambda: next(ticks, 1000.0))
    _install(owner, SlowAdapter())

    batch_id = _upload(owner, HEADER + COFFEE)

    batch = _batch(owner, batch_id)
    assert SlowAdapter.stopped
    assert batch.status == "failed"
    assert batch.report is not None and batch.report["errors"][0]["code"] == "timeout"
    path = owner.app.state.settings.data_dir / "imports" / str(owner.home_id) / batch_id
    assert not path.exists()


def test_report_message_falls_back_to_the_entry_message() -> None:
    assert report_message({"code": "already_imported", "message": "x"}, "en") == (
        "Already imported."
    )
    assert report_message({"code": "adapter_specific", "message": "Kept as is"}, "en") == (
        "Kept as is"
    )
