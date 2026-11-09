"""F4.4 web flows: restore mode, merge mode, refusals and restore rollback."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select

from assetboard.db.models.identity import AuditEvent, Workspace
from assetboard.db.models.ledger import Category, Transaction
from assetboard.db.repositories import CategoryRepository, TransactionRepository
from assetboard.domain.archive import (
    ArchiveRows,
    BudgetLineRow,
    CategoryRow,
    MonthRow,
    TransactionRow,
    write_archive,
)
from assetboard.web.workspace_routes import _mapping_options_from_form
from tests.conftest import OwnerApp
from tests.tenant.harness import context_for

EXPORTED_AT = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
FOOD = UUID("11111111-1111-4111-8111-111111111111")
CAR = UUID("22222222-2222-4222-8222-222222222222")
SALARY = UUID("33333333-3333-4333-8333-333333333333")


def _archive(*, currency: str = "EUR", name: str = "Archive Home") -> bytes:
    rows = ArchiveRows(
        workspace={
            "name": name,
            "base_currency": currency,
            "country_code": "PL",
            "timezone": "Europe/Warsaw",
            "locale": "pl",
            "auto_copy_plan": True,
            "taxes_enabled": True,
            "upcoming_days": 14,
        },
        categories=(
            CategoryRow(str(FOOD), "expense", "Food", "regular", "#4f9d69", 0, None),
            CategoryRow(
                str(CAR),
                "expense",
                "Car",
                "regular",
                "#a35d3b",
                1,
                "2025-12-31T18:05:00Z",
            ),
            CategoryRow(str(SALARY), "income", "Salary", "regular", "#2f7f5f", 0, None),
        ),
        budget_lines=(
            BudgetLineRow("2026-02", "expense", "Food", "50.00", currency, str(FOOD)),
            BudgetLineRow("2026-02", "expense", "Car", "20.00", currency, str(CAR)),
        ),
        transactions=(
            TransactionRow(
                "2026-02-01",
                "income",
                "100.00",
                currency,
                "Salary",
                "February",
                str(SALARY),
                str(uuid4()),
                None,
            ),
            TransactionRow(
                "2026-02-02",
                "expense",
                "12.50",
                currency,
                "Food",
                "Bakery",
                str(FOOD),
                str(uuid4()),
                None,
            ),
        ),
        months=(MonthRow("2026-02", "2026-02-28T20:00:00Z", "2026-02-01T08:00:00Z"),),
    )
    return write_archive(
        rows,
        app_name="AssetBoard",
        app_version="test",
        exported_at=EXPORTED_AT,
        base_currency=currency,
    )


def _upload(owner: OwnerApp, content: bytes, *, mode: str = "restore") -> Any:
    return owner.client.post(
        "/workspace/data/import",
        data={"csrf_token": owner.token("/workspace/data"), "mode": mode},
        files={"upload": ("archive.zip", content, "application/zip")},
        follow_redirects=False,
    )


def _batch_url(response: Any) -> str:
    return response.headers["location"].split("batch=", 1)[1]


def test_owner_can_restore_and_roll_back_over_the_suggested_set(owner: OwnerApp) -> None:
    page = owner.client.get("/workspace/data")
    assert "Restore into this workspace" in page.text
    assert "Replaces settings, categories, transactions" in page.text

    uploaded = _upload(owner, _archive())
    assert uploaded.status_code == 303
    batch_id = _batch_url(uploaded)
    report = owner.client.get(f"/workspace/data?batch={batch_id}")
    assert report.status_code == 200
    assert "Restore plan" in report.text
    assert "Removed before restoring" in report.text
    assert "Months that will be restored as closed" in report.text
    assert "Settings the restore will replace" in report.text

    workspace_id = owner.home_id
    applied = owner.post(f"/workspace/data/import/{batch_id}/apply", token_from="/workspace/data")
    assert applied.status_code == 303
    with owner.db() as session:
        workspace = session.get(Workspace, workspace_id)
        assert workspace is not None
        assert workspace.name == "Archive Home"
        assert workspace.taxes_enabled is True
        assert workspace.upcoming_days == 14
        names = {row.name for row in session.scalars(select(Category)).all()}
        assert names == {"Food", "Car", "Salary"}
        actions = {row.action for row in session.scalars(select(AuditEvent)).all()}
        assert "workspace.restored" in actions
        assert "import.applied" not in actions

    report = owner.client.get(f"/workspace/data?batch={batch_id}").text
    assert "data-close-imported-months" not in report
    assert "Close past imported months" not in report

    preview = owner.client.get(f"/workspace/data/import/{batch_id}/rollback")
    assert preview.status_code == 200
    assert "Settings to restore" in preview.text
    rolled_back = owner.post(
        f"/workspace/data/import/{batch_id}/rollback", token_from="/workspace/data"
    )
    assert rolled_back.status_code == 303
    with owner.db() as session:
        assert session.scalars(select(Category)).all() == []
        workspace = session.get(Workspace, owner.home_id)
        assert workspace is not None and workspace.name == "Home"


def test_restore_apply_explains_a_workspace_that_became_non_empty(owner: OwnerApp) -> None:
    uploaded = _upload(owner, _archive())
    batch_id = _batch_url(uploaded)
    ctx = context_for(owner.home_id, owner.app.state.clock)
    with owner.db() as session, session.begin():
        CategoryRepository(session, ctx).create(
            name="Added elsewhere",
            name_key="added elsewhere",
            kind="expense",
            treatment="regular",
            color="#111111",
            sort_order=0,
        )
    assert (
        owner.post(
            f"/workspace/data/import/{batch_id}/apply", token_from="/workspace/data"
        ).status_code
        == 303
    )
    page = owner.client.get(f"/workspace/data?batch={batch_id}")
    assert "Restore is available only in an empty workspace" in page.text
    refused = owner.post(f"/workspace/data/import/{batch_id}/apply", token_from="/workspace/data")
    assert refused.status_code == 409
    assert "empty workspace" in refused.json()["detail"]


def test_editor_does_not_get_restore_mode(owner: OwnerApp) -> None:
    owner.set_admin_role("editor")
    page = owner.client.get("/workspace/data")
    assert page.status_code == 200
    assert "Restore into this workspace" not in page.text
    assert 'name="mode" value="merge"' in page.text
    refused = _upload(owner, _archive())
    assert refused.status_code == 403


def test_restore_refuses_a_non_empty_workspace_and_a_lone_csv(owner: OwnerApp) -> None:
    ctx = context_for(owner.home_id, owner.app.state.clock)
    with owner.db() as session, session.begin():
        category = CategoryRepository(session, ctx).ordered()[0]
        TransactionRepository(session, ctx).create(
            direction="expense",
            day=date(2026, 1, 2),
            amount_minor=100,
            currency="EUR",
            category_id=category.id,
            description="Manual",
            description_key="manual",
            source="manual",
            created_by_user_id=owner.admin_id,
        )
    refused = _upload(owner, _archive())
    assert refused.status_code == 409
    assert "empty workspace" in refused.json()["detail"]

    with owner.db() as session, session.begin():
        session.query(Transaction).delete()
    owner.clear_categories()
    not_archive = owner.client.post(
        "/workspace/data/import",
        data={"csrf_token": owner.token("/workspace/data"), "mode": "restore"},
        files={"upload": ("history.csv", b"date,direction,amount,category\n", "text/csv")},
        follow_redirects=False,
    )
    assert not_archive.status_code == 409
    assert "complete workspace archive" in not_archive.json()["detail"]


def test_merge_archive_reports_but_does_not_apply_settings_and_months(owner: OwnerApp) -> None:
    uploaded = _upload(owner, _archive(), mode="merge")
    assert uploaded.status_code == 303
    batch_id = _batch_url(uploaded)
    page = owner.client.get(f"/workspace/data?batch={batch_id}")
    assert "Settings that differ (not applied in merge mode)" in page.text
    assert "Closed months in the archive (not applied in merge mode)" in page.text
    applied = owner.post(f"/workspace/data/import/{batch_id}/apply", token_from="/workspace/data")
    assert applied.status_code == 303
    with owner.db() as session:
        workspace = session.get(Workspace, owner.home_id)
        assert workspace is not None and workspace.name == "Home"
        categories = {row.name for row in session.scalars(select(Category)).all()}
        assert "Car" in categories
        transactions = session.scalars(select(Transaction)).all()
        assert {row.description for row in transactions} == {"February", "Bakery"}


def test_editor_cannot_roll_back_a_restore(owner: OwnerApp) -> None:
    uploaded = _upload(owner, _archive())
    batch_id = _batch_url(uploaded)
    assert (
        owner.post(
            f"/workspace/data/import/{batch_id}/apply", token_from="/workspace/data"
        ).status_code
        == 303
    )
    owner.set_admin_role("editor")
    assert owner.client.get(f"/workspace/data/import/{batch_id}/rollback").status_code == 403
    refused = owner.post(
        f"/workspace/data/import/{batch_id}/rollback", token_from="/workspace/data"
    )
    assert refused.status_code == 403


def test_mapping_form_distinguishes_archive_categories_with_one_name() -> None:
    active = "11111111-1111-4111-8111-111111111111"
    archived = "22222222-2222-4222-8222-222222222222"
    report = {
        "mapping": [
            {
                "source_id": active,
                "kind": "expense",
                "source_name_key": "car",
                "action": "create",
                "category_id": "",
                "create_name": "Car",
                "create_treatment": "regular",
                "create_color": "#111111",
            },
            {
                "source_id": archived,
                "kind": "expense",
                "source_name_key": "car",
                "action": "ignore",
                "category_id": "",
                "create_name": "Car",
                "create_treatment": "regular",
                "create_color": "#222222",
            },
        ]
    }
    form = {
        "mapping_source_id_0": active,
        "mapping_kind_0": "expense",
        "mapping_key_0": "car",
        "mapping_action_0": "map",
        "mapping_category_0": "33333333-3333-4333-8333-333333333333",
        "mapping_source_id_1": archived,
        "mapping_kind_1": "expense",
        "mapping_key_1": "car",
        "mapping_action_1": "ignore",
    }
    options = _mapping_options_from_form(form, report)
    mappings = options["mappings"]
    assert isinstance(mappings, list)
    assert {entry["source_id"] for entry in mappings} == {active, archived}
    assert {entry["action"] for entry in mappings} == {"map", "ignore"}


def test_merge_refuses_base_currency_mismatch(owner: OwnerApp) -> None:
    uploaded = _upload(owner, _archive(currency="PLN"), mode="merge")
    assert uploaded.status_code == 303
    batch_id = _batch_url(uploaded)
    page = owner.client.get(f"/workspace/data?batch={batch_id}")
    assert "base currency differs" in page.text
    apply_button = page.text.split("Apply import", 1)[0].rsplit("<button", 1)[1]
    assert "disabled" in apply_button
    refused = owner.post(f"/workspace/data/import/{batch_id}/apply", token_from="/workspace/data")
    assert refused.status_code == 409
