"""Every category <select> groups its categories under kind headings (owner, 2026-11-01): a
kind ("Expense categories", "Income categories", "Archived categories") is an <optgroup>
divider that cannot be chosen, never an option of its own. Only the empty placeholder option
("Choose a category", "All categories") may sit outside a group."""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser

import pytest
from sqlalchemy import select

from assetboard.clock import FixedClock
from assetboard.db.models.ledger import Category, Transaction
from tests.conftest import OwnerApp

NOW = "2026-10-14T00:30:00+00:00"
MONTH = "2026-10"
KIND_HEADINGS = {"Expense categories", "Income categories", "Archived categories"}


@dataclass
class Select:
    key: str
    options: list[tuple[str | None, str, str]] = field(default_factory=list)  # group, text, value


class _Selects(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.selects: dict[str, Select] = {}
        self._select: Select | None = None
        self._group: str | None = None
        self._option: tuple[str, list[str]] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {k: v or "" for k, v in attrs}
        if tag == "select":
            key = values.get("id") or values.get("name", "")
            self._select = self.selects.setdefault(key, Select(key))
        elif tag == "optgroup" and self._select is not None:
            self._group = values.get("label", "")
        elif tag == "option" and self._select is not None:
            self._option = (values.get("value", ""), [])

    def handle_data(self, data: str) -> None:
        if self._option is not None:
            self._option[1].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "option" and self._select is not None and self._option is not None:
            value, text = self._option
            self._select.options.append((self._group, "".join(text).strip(), value))
            self._option = None
        elif tag == "optgroup":
            self._group = None
        elif tag == "select":
            self._select = None


def _selects(html: str) -> dict[str, Select]:
    parser = _Selects()
    parser.feed(html)
    return parser.selects


def _assert_grouped(select: Select, headings: set[str]) -> None:
    """Every category option sits in a kind group; nothing else but a placeholder."""
    assert select.options, select.key
    for group, text, value in select.options:
        if group is None:
            assert value == "", (select.key, text)  # only the placeholder is ungrouped
        else:
            assert group in KIND_HEADINGS, (select.key, group)
            assert text not in KIND_HEADINGS | {"Expense", "Income"}, (select.key, text)
    groups = {group for group, _text, _value in select.options if group is not None}
    assert groups == headings, (select.key, groups)


@pytest.fixture
def app(owner: OwnerApp) -> OwnerApp:
    owner.app.state.clock = FixedClock(NOW)
    return owner


def _category_id(app: OwnerApp, name: str, kind: str) -> str:
    with app.db() as session:
        return str(
            session.scalars(
                select(Category.id).where(Category.name == name, Category.kind == kind)
            ).one()
        )


def test_the_transaction_entry_and_edit_selects_group_by_kind(app: OwnerApp) -> None:
    food = _category_id(app, "Food", "expense")
    form = {"direction": "expense", "day": "7", "amount": "12", "description": "",
            "category_id": food}  # fmt: skip
    assert app.post(f"/m/{MONTH}/transactions", form, token_from=f"/m/{MONTH}").status_code == 303
    selects = _selects(app.client.get(f"/transactions/{MONTH}").text)
    _assert_grouped(selects["entry-expense-category"], {"Expense categories"})
    _assert_grouped(selects["entry-income-category"], {"Income categories"})
    # "Other" exists in both kinds: each select shows only its own, inside its kind's group.
    others = [o for o in selects["entry-expense-category"].options if o[1] == "Other"]
    assert others == [("Expense categories", "Other", _category_id(app, "Other", "expense"))]
    with app.db() as session:
        entry = session.scalars(select(Transaction.id)).one()
    edit = _selects(app.client.get(f"/transactions/{MONTH}?edit={entry}").text)
    select_edit = edit[f"edit-{entry}-category"]
    _assert_grouped(select_edit, {"Expense categories"})
    assert ("Expense categories", "Food", food) in select_edit.options


def test_the_upcoming_filter_and_one_time_form_group_by_kind(app: OwnerApp) -> None:
    taxi = {"name": "Old taxi", "kind": "expense", "treatment": "regular", "color": "#16a34a"}
    assert app.post("/categories", taxi, token_from="/categories").status_code == 303
    old = _category_id(app, "Old taxi", "expense")
    assert app.post(f"/categories/{old}/archive", {}, token_from="/categories")
    selects = _selects(app.client.get("/upcoming").text)
    _assert_grouped(
        selects["upcoming-category"],
        {"Expense categories", "Income categories", "Archived categories"},
    )
    assert ("Archived categories", "Old taxi", old) in selects["upcoming-category"].options
    assert selects["upcoming-category"].options[0] == (None, "All categories", "")
    _assert_grouped(selects["one-time-category"], {"Expense categories", "Income categories"})
    page = app.client.get(f"/upcoming?category={old}").text
    assert f'<option value="{old}" selected>Old taxi</option>' in page


def test_the_other_category_selects_group_by_kind(app: OwnerApp) -> None:
    form = {"direction": "expense", "day": "7", "amount": "12", "description": "",
            "category_id": _category_id(app, "Food", "expense")}  # fmt: skip
    assert app.post(f"/m/{MONTH}/transactions", form, token_from=f"/m/{MONTH}").status_code == 303
    _assert_grouped(
        _selects(app.client.get("/history").text)["bulk-category"],
        {"Expense categories", "Income categories"},
    )
    _assert_grouped(
        _selects(app.client.get("/recurring/new").text)["category_id"],
        {"Expense categories", "Income categories"},
    )
    income = _selects(app.client.get("/income/new").text)["category_id"]
    _assert_grouped(income, {"Income categories"})
    assert app.post("/workspace/settings/menu", {"taxes_enabled": "1"}).status_code == 303
    payment = _selects(app.client.get("/taxes/new").text)["payment_category_id"]
    _assert_grouped(payment, {"Expense categories"})
    # The "Taxes" category stays preselected.
    taxes = _category_id(app, "Taxes", "expense")
    assert f'<option value="{taxes}" selected>Taxes</option>' in app.client.get("/taxes/new").text
    rules = app.client.get("/taxes").text  # the "Add another tax" form
    _assert_grouped(_selects(rules)["category_id"], {"Expense categories"})
    assert f'<option value="{taxes}" selected>Taxes</option>' in rules
