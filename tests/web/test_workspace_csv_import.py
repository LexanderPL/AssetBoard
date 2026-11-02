"""F4.3 through the web app: lone CSV uploads, mapping UI, templates and refusals."""

from __future__ import annotations

from collections.abc import Mapping
from io import BytesIO
from typing import Any, cast
from uuid import UUID
from zipfile import ZipFile

from sqlalchemy import select

from assetboard.db.models.import_batch import ImportBatch, ImportCategoryMapping
from assetboard.db.models.ledger import Category, Transaction
from assetboard.db.repositories import CategoryRepository
from assetboard.domain.categories import PALETTE, name_key
from assetboard.domain.import_csv import TEMPLATE_FILES
from assetboard.web.workspace_routes import _mapping_options_from_form
from tests.conftest import OwnerApp
from tests.tenant.harness import context_for


def _ctx(owner: OwnerApp):
    return context_for(owner.home_id, owner.app.state.clock)


def _category(
    owner: OwnerApp,
    name: str,
    *,
    kind: str = "expense",
    treatment: str = "regular",
    color: str = PALETTE[0],
    archived: bool = False,
) -> UUID:
    with owner.db() as session, session.begin():
        repository = CategoryRepository(session, _ctx(owner))
        category = repository.create(
            name=name,
            name_key=name_key(name),
            kind=kind,
            treatment=treatment,
            color=color,
            sort_order=repository.next_sort_order(kind),
            archived_at=owner.app.state.clock.now() if archived else None,
        )
        return category.id


def _upload(
    owner: OwnerApp,
    content: bytes,
    *,
    name: str = "upload.csv",
    delimiter: str | None = None,
    decimal: str | None = None,
) -> str:
    data = {"csrf_token": owner.token("/workspace/data")}
    if delimiter is not None:
        data["delimiter"] = delimiter
    if decimal is not None:
        data["decimal"] = decimal
    response = owner.client.post(
        "/workspace/data/import",
        data=data,
        files={"upload": (name, content, "text/csv")},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return response.headers["location"].split("batch=", 1)[1]


def _batch(owner: OwnerApp, batch_id: str) -> ImportBatch:
    with owner.db() as session:
        batch = session.get(ImportBatch, UUID(batch_id))
        assert batch is not None
        return batch


def _transactions(owner: OwnerApp) -> list[Transaction]:
    with owner.db() as session:
        return list(
            session.scalars(
                select(Transaction)
                .where(Transaction.source == "import")
                .order_by(Transaction.date, Transaction.created_at, Transaction.id)
            )
        )


def _categories(owner: OwnerApp) -> list[Category]:
    with owner.db() as session:
        return CategoryRepository(session, _ctx(owner)).ordered()


def _mappings(owner: OwnerApp) -> list[ImportCategoryMapping]:
    with owner.db() as session:
        return list(session.scalars(select(ImportCategoryMapping)))


def _mapping_form(batch: ImportBatch, changes: Mapping[int, Mapping[str, str]] | None = None):
    assert batch.report is not None
    data: dict[str, str] = {}
    for index, entry in enumerate(batch.report["mapping"]):
        values = {
            "action": str(entry["action"]),
            "category": str(entry["category_id"]),
            "name": str(entry["create_name"]),
            "treatment": str(entry["create_treatment"]),
            "color": str(entry["create_color"] or "#16a34a"),
        }
        values.update((changes or {}).get(index, {}))
        data[f"mapping_kind_{index}"] = str(entry["kind"])
        data[f"mapping_key_{index}"] = str(entry["source_name_key"])
        data[f"mapping_action_{index}"] = values["action"]
        data[f"mapping_category_{index}"] = values["category"]
        data[f"mapping_name_{index}"] = values["name"]
        data[f"mapping_treatment_{index}"] = values["treatment"]
        data[f"mapping_color_{index}"] = values["color"]
    return data


def _apply(owner: OwnerApp, batch_id: str, data: dict[str, str] | None = None) -> Any:
    return owner.post(
        f"/workspace/data/import/{batch_id}/apply",
        token_from="/workspace/data",
        data=data or {},
    )


def test_default_mapping_is_applied_in_one_step(owner: OwnerApp) -> None:
    owner.clear_categories()
    batch_id = _upload(
        owner,
        b"date,direction,amount,category,description\n"
        b"2026-01-02,expense,12.50,Food,Lunch\n"
        b"2026-01-03,income,3000,Salary,Pay\n",
    )

    page = owner.client.get(f"/workspace/data?batch={batch_id}")
    assert page.status_code == 200
    assert "Category mapping" in page.text
    assert "Update preview" in page.text
    assert "Apply import" in page.text
    assert "Remember these mappings for later imports" in page.text
    assert "transactions.csv" in page.text

    batch = _batch(owner, batch_id)
    assert batch.report is not None
    assert [entry["action"] for entry in batch.report["mapping"]] == ["create", "create"]
    response = _apply(owner, batch_id, _mapping_form(batch))

    assert response.status_code == 303
    assert _batch(owner, batch_id).status == "applied"
    assert [row.amount_minor for row in _transactions(owner)] == [1250, 300000]
    assert {category.name for category in _categories(owner)} == {"Food", "Salary"}


def test_mapping_form_matches_choices_by_key_not_row_position() -> None:
    report = {
        "mapping": [
            {
                "kind": "expense",
                "source_name_key": "food",
                "action": "map",
                "category_id": "11111111-1111-4111-8111-111111111111",
                "create_name": "Food",
                "create_treatment": "regular",
                "create_color": "#16a34a",
            },
            {
                "kind": "income",
                "source_name_key": "salary",
                "action": "create",
                "category_id": "",
                "create_name": "Salary",
                "create_treatment": "regular",
                "create_color": "#f97316",
            },
        ]
    }
    form = {
        "mapping_kind_0": "income",
        "mapping_key_0": "salary",
        "mapping_action_0": "ignore",
        "mapping_kind_1": "expense",
        "mapping_key_1": "food",
        "mapping_action_1": "create",
        "mapping_name_1": "Groceries",
        "mapping_kind_2": "expense",
        "mapping_key_2": "gone",
        "mapping_action_2": "ignore",
    }

    options = _mapping_options_from_form(form, report)
    choices = cast("list[dict[str, object]]", options["mappings"])
    by_key = {(str(choice["kind"]), str(choice["source_name_key"])): choice for choice in choices}

    assert by_key[("income", "salary")]["action"] == "ignore"
    assert by_key[("expense", "food")]["action"] == "create"
    assert by_key[("expense", "food")]["create_name"] == "Groceries"
    assert ("expense", "gone") not in by_key


def test_mapping_form_disappears_after_apply(owner: OwnerApp) -> None:
    owner.clear_categories()
    _category(owner, "Food")
    batch_id = _upload(
        owner,
        b"date,direction,amount,category\n2026-01-02,expense,10,Food\n",
    )

    assert _apply(owner, batch_id).status_code == 303

    page = owner.client.get(f"/workspace/data?batch={batch_id}")
    assert page.status_code == 200
    assert "Category mapping" not in page.text
    assert "Update preview" not in page.text


def test_update_preview_saves_choices_and_ignored_rows_are_skipped(owner: OwnerApp) -> None:
    owner.clear_categories()
    _category(owner, "Food")
    batch_id = _upload(
        owner,
        b"date,direction,amount,category\n"
        b"2026-01-02,expense,10,Food\n"
        b"2026-01-03,expense,20,Other\n",
    )
    batch = _batch(owner, batch_id)
    data = _mapping_form(batch, {1: {"action": "ignore"}})

    preview = owner.post(
        f"/workspace/data/import/{batch_id}/preview",
        token_from="/workspace/data",
        data=data,
    )

    assert preview.status_code == 303
    batch = _batch(owner, batch_id)
    assert batch.report is not None
    assert batch.report["counts"]["ignored_categories_skipped"] == 1
    assert batch.report["mapping"][1]["action"] == "ignore"
    assert _transactions(owner) == []
    page = owner.client.get(f"/workspace/data?batch={batch_id}")
    assert "Rows of ignored categories skipped" in page.text
    assert ">1<" in page.text

    assert _apply(owner, batch_id, data).status_code == 303
    assert [row.amount_minor for row in _transactions(owner)] == [1000]


def test_invalid_mapping_disables_apply_and_is_refused_server_side(owner: OwnerApp) -> None:
    owner.clear_categories()
    _category(owner, "Food")
    batch_id = _upload(
        owner,
        b"date,direction,amount,category\n2026-01-02,expense,10,Food\n2026-01-03,expense,20,New\n",
    )
    batch = _batch(owner, batch_id)
    data = _mapping_form(batch, {1: {"name": "Food"}})

    preview = owner.post(
        f"/workspace/data/import/{batch_id}/preview",
        token_from="/workspace/data",
        data=data,
    )

    assert preview.status_code == 303
    batch = _batch(owner, batch_id)
    assert batch.report is not None
    assert batch.report["counts"]["mapping_invalid"] == 1
    page = owner.client.get(f"/workspace/data?batch={batch_id}")
    assert "Fix the category mappings above before applying." in page.text
    assert "A category with this name already exists." in page.text
    assert "disabled" in page.text

    refused = _apply(owner, batch_id, data)

    assert refused.status_code == 303
    assert _batch(owner, batch_id).status == "planned"
    assert _transactions(owner) == []


def test_template_downloads_are_exact_and_editor_only(owner: OwnerApp) -> None:
    for filename in ("transactions.csv", "budget_lines.csv"):
        response = owner.client.get(f"/workspace/data/import/templates/{filename}")
        assert response.status_code == 200
        assert response.content == TEMPLATE_FILES[filename]
        assert response.headers["content-type"].startswith("text/csv")
        assert filename in response.headers["content-disposition"]
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"

    assert owner.client.get("/workspace/data/import/templates/other.csv").status_code == 404

    owner.set_admin_role("viewer")
    assert owner.client.get("/workspace/data/import/templates/transactions.csv").status_code == 403


def test_bom_and_crlf_are_accepted(owner: OwnerApp) -> None:
    owner.clear_categories()
    _category(owner, "Food")
    content = b"\xef\xbb\xbfdate,direction,amount,category\r\n2026-01-02,expense,12.50,Food\r\n"

    batch_id = _upload(owner, content)

    batch = _batch(owner, batch_id)
    assert batch.report is not None and batch.report["counts"]["to_create"] == 1
    assert _apply(owner, batch_id).status_code == 303
    assert [row.amount_minor for row in _transactions(owner)] == [1250]


def test_semicolon_and_decimal_comma_upload_options_are_used(owner: OwnerApp) -> None:
    owner.clear_categories()
    _category(owner, "Food")
    batch_id = _upload(
        owner,
        b"date;direction;amount;category\n2026-01-02;expense;12,50;Food\n",
        delimiter="semicolon",
        decimal="comma",
    )

    batch = _batch(owner, batch_id)
    assert batch.options == {"delimiter": "semicolon", "decimal": "comma"}
    assert batch.report is not None and batch.report["counts"]["to_create"] == 1
    assert _apply(owner, batch_id).status_code == 303
    assert [row.amount_minor for row in _transactions(owner)] == [1250]


def test_unknown_columns_warn_and_missing_columns_fail_on_the_page(owner: OwnerApp) -> None:
    owner.clear_categories()
    batch_id = _upload(
        owner,
        b"date,direction,amount,category,colour\n2026-01-02,expense,10,Food,red\n",
    )
    page = owner.client.get(f"/workspace/data?batch={batch_id}")
    assert "Warnings" in page.text
    assert "An unknown CSV column was ignored." in page.text

    missing = _upload(owner, b"date,direction,amount\n2026-01-02,expense,10\n")
    page = owner.client.get(f"/workspace/data?batch={missing}")
    assert "A required column is missing." in page.text
    assert _batch(owner, missing).status == "failed"
    errors = owner.client.get(f"/workspace/data/import/{missing}/errors.csv")
    assert "missing_column" in errors.text


def test_lone_months_and_workspace_json_are_refused(owner: OwnerApp) -> None:
    message = "This file is only part of a full restore."
    for content in (
        b"month,closed_at,plan_started_at\n2026-01,,\n",
        b'{"name": "Home", "base_currency": "EUR"}',
    ):
        batch_id = _upload(owner, content)
        batch = _batch(owner, batch_id)
        assert batch.status == "failed"
        assert batch.report is not None
        assert batch.report["errors"][0]["code"] == "partial_restore"
        page = owner.client.get(f"/workspace/data?batch={batch_id}")
        assert message in page.text


def test_zip_without_a_manifest_is_refused(owner: OwnerApp) -> None:
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("transactions.csv", b"date,direction,amount,category\n")

    batch_id = _upload(owner, output.getvalue())

    batch = _batch(owner, batch_id)
    assert batch.status == "failed"
    assert batch.report is not None
    assert batch.report["errors"][0]["code"] == "missing_members"


def test_categories_csv_has_no_mapping_step_and_applies_directly(owner: OwnerApp) -> None:
    owner.clear_categories()
    batch_id = _upload(
        owner,
        b"kind,name,treatment,color\nexpense,Food,regular,#111111\nincome,Salary,regular,\n",
    )

    batch = _batch(owner, batch_id)
    assert batch.report is not None
    assert batch.report["mapping"] == []
    assert batch.report["counts"]["to_create"] == 2
    page = owner.client.get(f"/workspace/data?batch={batch_id}")
    assert "Category mapping" not in page.text

    assert _apply(owner, batch_id).status_code == 303
    categories = {category.name: category for category in _categories(owner)}
    assert set(categories) == {"Food", "Salary"}
    assert categories["Food"].color == "#111111"
    assert categories["Salary"].color == PALETTE[0]


def test_future_dated_rows_are_counted_and_listed(owner: OwnerApp) -> None:
    owner.clear_categories()
    _category(owner, "Food")
    batch_id = _upload(
        owner,
        b"date,direction,amount,category\n2099-01-01,expense,10,Food\n",
    )

    batch = _batch(owner, batch_id)
    assert batch.report is not None
    assert batch.report["counts"]["future_dates_skipped"] == 1
    page = owner.client.get(f"/workspace/data?batch={batch_id}")
    assert "Future-dated rows skipped" in page.text
    errors = owner.client.get(f"/workspace/data/import/{batch_id}/errors.csv")
    assert "future_date" in errors.text
    assert "The date is in the future; it is not imported as an actual entry." in errors.text

    assert _apply(owner, batch_id).status_code == 303
    assert _transactions(owner) == []


def test_remember_mappings_checkbox_saves_them_on_apply(owner: OwnerApp) -> None:
    owner.clear_categories()
    _category(owner, "Food")
    batch_id = _upload(
        owner,
        b"date,direction,amount,category\n"
        b"2026-01-02,expense,10,Food\n"
        b"2026-01-03,expense,20,New stuff\n",
    )
    batch = _batch(owner, batch_id)
    data = _mapping_form(batch)
    data["remember_mappings"] = "true"

    preview = owner.post(
        f"/workspace/data/import/{batch_id}/preview",
        token_from="/workspace/data",
        data=data,
    )
    assert preview.status_code == 303
    page = owner.client.get(f"/workspace/data?batch={batch_id}")
    assert "Remember these mappings for later imports" in page.text
    assert 'name="remember_mappings" value="true" checked' in page.text

    assert _apply(owner, batch_id, data).status_code == 303

    mappings = {mapping.source_name_key: mapping for mapping in _mappings(owner)}
    assert set(mappings) == {name_key("Food"), name_key("New stuff")}
    created = next(category for category in _categories(owner) if category.name == "New stuff")
    assert mappings[name_key("New stuff")].category_id == created.id
