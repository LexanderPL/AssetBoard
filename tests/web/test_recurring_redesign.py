"""The Recurring page redesign (F3.1/F3.2): the list in Expenses and Income with plain-sentence
schedules, and the guided form with its live "Next dates" panel."""

import re
from datetime import date
from typing import Any

from sqlalchemy import select

from assetboard.clock import FixedClock
from assetboard.db.models.scheduled import ScheduledItem


def _categories(owner) -> dict[str, str]:
    """Category id by kind: the first expense and the first income category of the form."""
    page = owner.client.get("/recurring/new").text
    result = {}
    for kind in ("expense", "income"):
        group = page.split(f'data-kind="{kind}"')[1].split("</optgroup>")[0]
        match = re.search(r'<option value="([a-f0-9-]{36})"', group)
        assert match
        result[kind] = match.group(1)
    return result


def guided(owner, **overrides: Any) -> dict[str, Any]:
    """A post of the guided form: monthly on the 10th, no end."""
    return {
        "guided": "1",
        "name": "Rent",
        "direction": "expense",
        "category_id": _categories(owner)["expense"],
        "amount": "1200",
        "repeat": "monthly",
        "interval_weeks": "1",
        "interval_months": "2",
        "interval_years": "1",
        "month_day": "10",
        "year_day": "1",
        "month": "1",
        "weekend_policy": "none",
        "start_date": "2026-01-10",
        "ends": "never",
        "end_date": "",
        "max_count": "",
        **overrides,
    }


def _item(owner, name: str) -> ScheduledItem:
    with owner.db() as session:
        item = session.scalars(select(ScheduledItem).where(ScheduledItem.name == name)).one()
        session.expunge(item)
        return item


def _row(page: str, item_id) -> str:
    return page.split(f'data-scheduled-item="{item_id}"')[1].split("</tr>")[0]


def test_list_has_expense_and_income_sections_with_plain_sentences(owner):
    owner.app.state.clock = FixedClock("2026-10-31T12:00:00+00:00")
    income = _categories(owner)["income"]
    for data in (
        guided(owner),
        guided(owner, name="Water", repeat="every_months", interval_months="3",
               month_day="15", start_date="2027-01-01", estimate="1"),
        guided(owner, name="Insurance", repeat="yearly", year_day="31", month="3",
               ends="on_date", end_date="2027-12-31"),
        guided(owner, name="Salary", direction="income", category_id=income,
               repeat="weekly", weekday="0"),
    ):  # fmt: skip
        assert owner.post("/recurring", data).status_code == 303
    page = owner.client.get("/recurring").text
    expenses = page.split('data-recurring-section="expense"')[1].split("</section>")[0]
    incomes = page.split('data-recurring-section="income"')[1].split("</section>")[0]
    assert ">Expenses</h2>" in expenses and ">Income</h2>" in incomes
    assert "Rent" in expenses and "Salary" not in expenses and "Salary" in incomes
    rent, water, insurance, salary = (
        _item(owner, name) for name in ("Rent", "Water", "Insurance", "Salary")
    )
    assert "Monthly on the 10th" in _row(page, rent.id)
    assert '<time datetime="2026-11-10">Nov 10, 2026</time>' in _row(page, rent.id)
    assert "Every 3 months on the 15th, from January 2027" in _row(page, water.id)
    assert "data-estimate" in _row(page, water.id) and "data-estimate" not in _row(page, rent.id)
    assert "Yearly on 31 March, until December 2027" in _row(page, insurance.id)
    assert "Weekly on Monday" in _row(page, salary.id)
    # Category swatch and name.
    assert "<circle" in _row(page, rent.id) and "data-category" in _row(page, rent.id)
    # Paused items get a chip and Resume; no next date.
    assert owner.post(f"/recurring/{rent.id}/pause").status_code == 303
    page = owner.client.get("/recurring").text
    assert "data-paused" in _row(page, rent.id) and "data-resume" in _row(page, rent.id)
    assert "<time" not in _row(page, rent.id)


def test_list_hides_archived_items_and_offers_actions_by_role(owner):
    assert owner.post("/recurring", guided(owner)).status_code == 303
    assert owner.post("/recurring", guided(owner, name="Gym")).status_code == 303
    rent, gym = _item(owner, "Rent"), _item(owner, "Gym")
    assert owner.post(f"/recurring/{gym.id}/archive").status_code == 303
    page = owner.client.get("/recurring").text
    assert f'data-scheduled-item="{gym.id}"' not in page and "Archived" not in page
    row = _row(page, rent.id)
    for marker in ("data-edit", "data-pause", "data-delete"):
        assert marker in row
    assert "data-new-recurring" in page
    delete_form = row.split('/delete"')[1].split(">")[0]
    assert "Payments already recorded or skipped stay in your history." in delete_form
    owner.set_admin_role("viewer")
    page = owner.client.get("/recurring").text
    row = _row(page, rent.id)
    assert "data-edit" not in row and "data-delete" not in row and "data-pause" not in row
    assert "data-new-recurring" not in page


def test_empty_state_has_a_single_new_button(owner):
    page = owner.client.get("/recurring").text
    assert "data-recurring-empty" in page and page.count("data-new-recurring") == 1
    assert "data-recurring-section" not in page


def test_polish_list_and_form(owner):
    owner.app.state.clock = FixedClock("2026-10-31T12:00:00+00:00")
    assert owner.post("/recurring", guided(owner)).status_code == 303
    owner.client.headers["accept-language"] = "pl"
    page = owner.client.get("/recurring").text
    assert "Wydatki" in page and "Co miesiąc, 10. dnia" in page
    form = owner.client.get("/recurring/new").text
    for heading in ("Co", "Ile", "Jak często", "Kiedy"):
        assert f">{heading}</legend>" in form


def test_each_frequency_saves_its_own_fields(owner):
    owner.app.state.clock = FixedClock("2026-03-01T12:00:00+00:00")
    cases = {
        "Weekly": (
            {"repeat": "weekly", "interval_weeks": "2", "weekday": ["0", "3"], "month_day": "31"},
            ("weekly", 2, [0, 3]),
        ),
        "Monthly": ({"repeat": "monthly", "interval_months": "5"}, ("monthly", 1, [])),
        "Quarterly": ({"repeat": "every_months", "interval_months": "3"}, ("monthly", 3, [])),
        "Yearly": (
            {"repeat": "yearly", "year_day": "29", "month": "2", "interval_years": "1"},
            ("yearly", 1, []),
        ),
    }
    for name, (overrides, expected) in cases.items():
        response = owner.post("/recurring", guided(owner, name=name, **overrides))
        assert response.status_code == 303, name
        item = _item(owner, name)
        assert (item.freq, item.interval, item.weekdays) == expected, name
    assert (_item(owner, "Yearly").month, _item(owner, "Yearly").month_day) == (2, "29")
    assert _item(owner, "Monthly").month_day == "10"
    # Estimate checkbox and the "Ends" choice.
    power_data = guided(
        owner, name="Power", estimate="1", ends="after", max_count="12", end_date="2027-01-01"
    )
    assert owner.post("/recurring", power_data).status_code == 303
    power = _item(owner, "Power")
    assert (power.amount_mode, power.max_count, power.end_date) == ("estimate", 12, None)
    phone_data = guided(owner, name="Phone", ends="on_date", max_count="3", end_date="2026-12-31")
    assert owner.post("/recurring", phone_data).status_code == 303
    phone = _item(owner, "Phone")
    assert (phone.amount_mode, phone.max_count, phone.end_date) == (
        "fixed",
        None,
        date(2026, 12, 31),
    )


def test_guided_form_shows_fields_per_frequency_and_errors_next_to_them(owner):
    page = owner.client.get("/recurring/new").text
    assert 'name="guided" value="1"' in page
    assert 'name="repeat" value="monthly" class="mt-1 h-4 w-4 accent-primary" checked' in page
    for choice in ("weekly", "monthly", "every_months", "yearly"):
        assert f'data-repeat-option="{choice}"' in page
    assert 'data-repeat-fields="weekly"' in page and 'data-hidden-when="repeat!=weekly"' in page
    assert 'data-hidden-when="repeat!=monthly|every_months"' in page
    assert 'data-hidden-when="repeat!=yearly"' in page
    assert 'data-filter-by-kind="direction"' in page
    # The month select names the months.
    assert '<option value="3">March</option>' in page

    missing = owner.post("/recurring", guided(owner, repeat="every_months", interval_months=""))
    assert missing.status_code == 400
    field = missing.text.split('id="interval_months"')[1].split(">")[0]
    assert 'aria-invalid="true" aria-describedby="interval-error"' in field
    assert 'id="interval-error"' in missing.text
    # Values are kept.
    assert 'value="Rent"' in missing.text
    assert 'value="every_months" class="mt-1 h-4 w-4 accent-primary" checked' in missing.text

    no_end = owner.post("/recurring", guided(owner, ends="on_date", end_date=""))
    assert no_end.status_code == 400 and 'id="end_date-error"' in no_end.text
    assert 'value="on_date" class="h-4 w-4 accent-primary" checked' in no_end.text
    no_count = owner.post("/recurring", guided(owner, ends="after", max_count=""))
    assert no_count.status_code == 400 and 'id="max_count-error"' in no_count.text
    no_days = owner.post("/recurring", guided(owner, repeat="weekly"))
    assert no_days.status_code == 400 and 'id="weekdays-error"' in no_days.text
    feb = owner.post("/recurring", guided(owner, repeat="yearly", year_day="31", month="2"))
    assert feb.status_code == 400
    assert 'aria-describedby="month_day-error"' in feb.text.split('id="year_day"')[1].split(">")[0]
    # The server checks the category's kind against the chosen one.
    mismatch = owner.post("/recurring", guided(owner, direction="income"))
    assert mismatch.status_code == 400 and 'id="category_id-error"' in mismatch.text


def test_edit_prefills_the_guided_form(owner):
    data = guided(
        owner,
        name="Water",
        repeat="every_months",
        interval_months="3",
        month_day="-1",
        estimate="1",
        ends="after",
        max_count="8",
        weekend_policy="previous_working_day",
    )
    assert owner.post("/recurring", data).status_code == 303
    water = _item(owner, "Water")
    page = owner.client.get(f"/recurring/{water.id}/edit").text
    assert 'value="every_months" class="mt-1 h-4 w-4 accent-primary" checked' in page
    assert (
        'id="interval_months" name="interval_months" type="number" min="1" max="999" value="3"'
        in page
    )
    assert '<option value="-1" selected>' in page
    assert 'name="estimate" value="1" checked' in page
    assert 'value="after" class="h-4 w-4 accent-primary" checked' in page and 'value="8"' in page
    assert '<option value="previous_working_day" selected>' in page
    assert 'name="include_past"' not in page
    yearly = guided(owner, name="Tax", repeat="yearly", year_day="15", month="4",
                    interval_years="2")  # fmt: skip
    assert owner.post("/recurring", yearly).status_code == 303
    page = owner.client.get(f"/recurring/{_item(owner, 'Tax').id}/edit").text
    assert 'value="yearly" class="mt-1 h-4 w-4 accent-primary" checked' in page
    assert '<option value="4" selected>April</option>' in page
    assert page.split('id="year_day"')[1].split("</select>")[0].count("selected") == 1
    assert '<option value="15" selected>' in page.split('id="year_day"')[1].split("</select>")[0]
    assert 'name="interval_years" type="number" min="1" max="999" value="2"' in page


def test_preview_shows_the_sentence_and_five_dates(owner):
    owner.app.state.clock = FixedClock("2026-10-31T12:00:00+00:00")
    page = owner.client.get("/recurring/new").text
    assert (
        'hx-trigger="load, change from:#schedule-form delay:150ms, '
        'input from:#schedule-form delay:300ms"' in page
    )
    preview = owner.post(
        "/recurring/preview", guided(owner, repeat="weekly", weekday=["0", "4"], interval_weeks="2")
    )
    assert "Every 2 weeks on Monday and on Friday, from" in preview.text
    assert preview.text.count("<time") == 5
    invalid = owner.post(
        "/recurring/preview", guided(owner, repeat="every_months", interval_months="0")
    )
    assert "data-preview-invalid" in invalid.text
    owner.client.headers["accept-language"] = "pl"
    polish = owner.post(
        "/recurring/preview", guided(owner, repeat="yearly", year_day="31", month="3")
    )
    assert "Co roku, 31 marca" in polish.text


def test_next_date_beyond_the_planning_horizon_comes_from_the_rule(owner):
    owner.app.state.clock = FixedClock("2026-10-31T12:00:00+00:00")
    data = guided(owner, name="Far", start_date="2029-05-01", month_day="1")
    assert owner.post("/recurring", data).status_code == 303
    page = owner.client.get("/recurring").text
    assert '<time datetime="2029-05-01">' in _row(page, _item(owner, "Far").id)
