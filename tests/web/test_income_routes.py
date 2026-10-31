from __future__ import annotations

from datetime import date

from sqlalchemy import select

from assetboard.db.models.income import IncomeSource
from assetboard.db.models.ledger import Category
from assetboard.db.models.scheduled import ScheduledOccurrence


def _income_category(owner) -> str:
    with owner.db() as session:
        return str(
            session.scalars(
                select(Category.id).where(
                    Category.workspace_id == owner.home_id,
                    Category.kind == "income",
                    Category.archived_at.is_(None),
                )
            ).first()
        )


def _form(category_id: str) -> dict[str, str]:
    return {
        "name": "Contract work",
        "kind": "variable",
        "category_id": category_id,
        "rate_amount": "1,230.00",
        "unit_label": "day",
        "pay_day": "10",
        "pay_lag_months": "1",
        "expected_mode": "manual",
        "expected_units": "",
        "average_months": "3",
        "active_from_month": "1",
        "active_from_year": "2026",
        "active_to_month": "",
        "active_to_year": "",
    }


def test_income_source_and_work_log_forms(owner):
    created = owner.post("/income", _form(_income_category(owner)), token_from="/income")
    assert created.status_code == 303
    assert "error" not in created.headers["location"]
    with owner.db() as session:
        source = session.scalars(select(IncomeSource)).one()
        assert source.rate_minor == 123_000

    logged = owner.post(
        f"/income/{source.id}/work-logs",
        {"work_month_month": "8", "work_month_year": "2026", "units": "7.5"},
        token_from="/income",
    )
    assert logged.status_code == 303
    assert "error" not in logged.headers["location"]

    page = owner.client.get("/income")
    assert page.status_code == 200
    assert "Contract work" in page.text
    assert 'name="record_as"' not in page.text
    assert "August 2026 · 7.5" in page.text


def test_income_forms_require_csrf(owner):
    response = owner.client.post(
        "/income", data=_form(_income_category(owner)), follow_redirects=False
    )
    assert response.status_code == 403
    with owner.db() as session:
        assert session.scalars(select(IncomeSource)).first() is None


def test_expected_income_rows_and_banner_on_the_transactions_page(owner):
    form = _form(_income_category(owner)) | {"active_from_year": "2020"}
    assert owner.post("/income", form, token_from="/income").status_code == 303
    with owner.db() as session:
        source = session.scalars(select(IncomeSource)).one()
    owner.post(
        f"/income/{source.id}/work-logs",
        {"work_month_month": "8", "work_month_year": "2026", "units": "2"},
        token_from="/income",
    )
    with owner.db() as session:
        occurrence = session.scalars(
            select(ScheduledOccurrence).where(
                ScheduledOccurrence.generated_by == "income",
                ScheduledOccurrence.work_month == date(2026, 8, 1),
            )
        ).one()

    page = owner.client.get("/transactions/2026-09")
    assert f'data-pending-occurrence="{occurrence.id}"' in page.text
    confirmed = owner.post(
        f"/upcoming/{occurrence.id}/post",
        {"day": "2026-09-10", "amount": "2,460.00", "return_month": "2026-09"},
        token_from="/transactions/2026-09",
    )
    assert confirmed.status_code == 303
    assert confirmed.headers["location"].split("?")[0].endswith("/transactions/2026-09")
    after = owner.client.get("/transactions/2026-09").text
    assert f'data-pending-occurrence="{occurrence.id}"' not in after

    # February 2020's work (paid 2020-03-10) has no log: both March pages show the banner.
    for path in ("/transactions/2020-03", "/m/2020-03"):
        assert f'data-missing-work-log="{source.id}"' in owner.client.get(path).text


def _expense_category(owner) -> str:
    with owner.db() as session:
        return str(
            session.scalars(
                select(Category.id).where(
                    Category.workspace_id == owner.home_id,
                    Category.kind == "expense",
                    Category.treatment != "transfer",
                    Category.archived_at.is_(None),
                )
            ).first()
        )


def test_work_month_pickers_store_the_first_day_and_render_the_choice(owner):
    form = _form(_income_category(owner)) | {
        "active_from_month": "3",
        "active_to_month": "11",
        "active_to_year": "2027",
    }
    assert "error" not in owner.post("/income", form, token_from="/income").headers["location"]
    with owner.db() as session:
        source = session.scalars(select(IncomeSource)).one()
        assert (source.active_from, source.active_to) == (date(2026, 3, 1), date(2027, 11, 1))
    edit = owner.client.get(f"/income/{source.id}/edit").text
    # Month names come from CLDR; the stored month and year are selected.
    assert '<option value="3" selected>March</option>' in edit
    assert '<option value="11" selected>November</option>' in edit
    assert '<option value="2027" selected>2027</option>' in edit
    assert 'type="date"' not in edit
    page = owner.client.get("/income").text
    assert 'name="work_month_month"' in page and 'name="work_month_year"' in page

    # Clearing "Active through" and the plain ISO date of older clients both still work.
    cleared = form | {"active_to_month": "", "active_to_year": ""}
    assert owner.post(f"/income/{source.id}", cleared, token_from="/income").status_code == 303
    legacy = {k: v for k, v in form.items() if not k.startswith("active_")}
    legacy |= {"active_from": "2025-02-01"}
    saved = owner.post(f"/income/{source.id}", legacy, token_from="/income")
    assert "error" not in saved.headers["location"]
    with owner.db() as session:
        source = session.scalars(select(IncomeSource)).one()
        assert (source.active_from, source.active_to) == (date(2025, 2, 1), None)
    edit = owner.client.get(f"/income/{source.id}/edit").text
    assert '<option value="2025" selected>2025</option>' in edit


def test_a_half_chosen_month_reopens_the_edit_form_with_an_error(owner):
    form = _form(_income_category(owner))
    assert owner.post("/income", form, token_from="/income").status_code == 303
    with owner.db() as session:
        source_id = session.scalars(select(IncomeSource.id)).one()
    response = owner.post(
        f"/income/{source_id}", form | {"active_to_month": "5"}, token_from="/income"
    )
    assert response.headers["location"].endswith(
        f"/income/{source_id}/edit?error=invalid_work_month"
    )
    page = owner.client.get(response.headers["location"]).text
    assert "Choose a valid work month." in page and "data-income-edit" in page
