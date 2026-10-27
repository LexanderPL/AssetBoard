from uuid import uuid4

from assetboard.clock import FixedClock
from assetboard.db.models.scheduled import ScheduledItem
from assetboard.web.scheduled_routes import _rule


def form(owner, **overrides):
    page = owner.client.get("/recurring/new")
    # Seeded categories are expense first; choose the option from the actual form.
    import re

    category = re.search(r'<option value="([a-f0-9-]{36})"', page.text)
    assert category
    return {
        "name": "Car insurance",
        "category_id": category.group(1),
        "amount": "1800",
        "amount_mode": "estimate",
        "start_date": "2026-01-01",
        "freq": "monthly",
        "interval": "1",
        "month_day": "31",
        "month": "1",
        "weekend_policy": "none",
        **overrides,
    }


def test_category_picker_groups_expense_and_income_categories_without_direction(owner):
    page = owner.client.get("/recurring/new").text
    assert 'name="direction"' not in page
    assert '<optgroup label="Expense categories">' in page
    assert '<optgroup label="Income categories">' in page


def test_crud_preview_pause_resume_archive_and_delete(owner):
    owner.app.state.clock = FixedClock("2026-03-01T12:00:00+00:00")
    data = form(owner)
    response = owner.post("/recurring/preview", data)
    assert response.status_code == 200
    for day in ("2026-03-31", "2026-04-30", "2026-05-31", "2026-06-30", "2026-07-31", "2026-08-31"):
        assert day in response.text
    created = owner.post("/recurring", data)
    assert created.status_code == 303
    listing = owner.client.get("/recurring")
    assert "Car insurance" in listing.text and "Estimate" in listing.text
    with owner.db() as session:
        from sqlalchemy import select

        row = session.scalars(select(ScheduledItem)).one()
        item_id = row.id
    edit = owner.client.get(f"/recurring/{item_id}/edit")
    assert edit.status_code == 200 and "1800" in edit.text
    assert owner.post(f"/recurring/{item_id}/edit", data | {"name": "Updated"}).status_code == 303
    for action in ("pause", "resume", "archive"):
        assert owner.post(f"/recurring/{item_id}/{action}").status_code == 303
    assert "Archived" in owner.client.get("/recurring").text
    assert owner.post("/recurring", data | {"name": "Delete me"}).status_code == 303
    with owner.db() as session:
        delete_id = session.scalars(
            select(ScheduledItem.id).where(ScheduledItem.name == "Delete me")
        ).one()
    assert owner.post(f"/recurring/{delete_id}/delete").status_code == 303
    assert "Delete me" not in owner.client.get("/recurring").text


def test_invalid_input_csrf_roles_and_other_workspace_ids(owner):
    data = form(owner)
    for change in (
        {"name": ""},
        {"amount": "nan"},
        {"interval": "0"},
        {"start_date": "bad"},
        {"category_id": ""},
    ):
        assert owner.post("/recurring", data | change).status_code == 400
    assert owner.post("/recurring/preview", data | {"freq": "weekly"}).status_code == 200
    assert (
        "Choose valid recurrence dates"
        in owner.post("/recurring/preview", data | {"freq": "weekly"}).text
    )
    assert owner.client.post("/recurring", data=data).status_code == 403
    assert owner.client.post("/recurring/preview", data=data).status_code == 403
    assert owner.client.get("/recurring/bad/edit").status_code == 404
    assert owner.post(f"/recurring/{uuid4()}/delete").status_code == 404
    assert owner.post(f"/recurring/{uuid4()}/unexpected").status_code == 404
    other = owner.add_workspace("Other")
    owner.join(owner.admin_id, other, "owner")
    assert owner.post("/recurring", data).status_code == 303
    with owner.db() as session:
        from sqlalchemy import select

        item_id = session.scalars(select(ScheduledItem.id)).one()
    owner.post("/workspace/switch", {"workspace_id": str(other)})
    assert "Car insurance" not in owner.client.get("/recurring").text
    assert owner.client.get(f"/recurring/{item_id}/edit").status_code == 404
    assert owner.post(f"/recurring/{item_id}/pause").status_code == 404
    owner.post("/workspace/switch", {"workspace_id": str(owner.home_id)})
    owner.set_admin_role("viewer")
    assert owner.client.get("/recurring").status_code == 200
    assert owner.client.get("/recurring/new").status_code == 403
    assert owner.post("/recurring", data).status_code == 403
    assert owner.post(f"/recurring/{item_id}/pause").status_code == 403


def test_locale_amount_and_weekly_form(owner):
    owner.client.headers["accept-language"] = "pl"
    assert "Nowa pozycja cykliczna" in owner.client.get("/recurring/new").text
    data = form(
        owner, amount="1 234,56", freq="weekly", weekday="0", max_count="2", end_date="2026-12-31"
    )
    owner.app.state.clock = FixedClock("2026-03-01T12:00:00+00:00")
    assert owner.post("/recurring", data).status_code == 303
    with owner.db() as session:
        from sqlalchemy import select

        item = session.scalars(select(ScheduledItem)).one()
        assert item.amount_minor == 123456 and item.weekdays == [0]
    assert _rule(data | {"weekdays": "0"}).weekdays == (0,)


def test_field_errors_are_linked_and_number_fields_have_minimums(owner):
    data = form(owner)
    page = owner.client.get("/recurring/new").text
    assert 'id="interval"' in page and 'min="1"' in page.split('id="interval"')[1].split(">")[0]
    assert 'min="1"' in page.split('id="max_count"')[1].split(">")[0]
    assert page.count('class="h-4 w-4 rounded border-line accent-primary"') == 9
    assert 'name="auto_post"' in page
    response = owner.post("/recurring", data | {"interval": "0"})
    assert response.status_code == 400
    assert 'aria-invalid="true" aria-describedby="interval-error"' in response.text
    assert 'id="interval-error"' in response.text
    april = owner.post("/recurring", data | {"freq": "yearly", "month": "4", "month_day": "31"})
    assert april.status_code == 400
    assert 'id="month_day-error"' in april.text and "does not exist" in april.text
    weekly = owner.post("/recurring", data | {"freq": "weekly"})
    assert weekly.status_code == 400 and 'id="weekdays-error"' in weekly.text
    assert 'id="name-error"' in owner.post("/recurring", data | {"name": " "}).text
    assert 'id="amount-error"' in owner.post("/recurring", data | {"amount": "-5"}).text


def test_preview_is_live_and_locale_formatted(owner):
    owner.app.state.clock = FixedClock("2026-03-01T12:00:00+00:00")
    page = owner.client.get("/recurring/new").text
    assert 'id="schedule-preview"' in page and 'aria-live="polite"' in page
    preview = owner.post("/recurring/preview", form(owner)).text
    assert '<time datetime="2026-03-31">Mar 31, 2026</time>' in preview
    owner.client.headers["accept-language"] = "pl"
    assert '<time datetime="2026-03-31">31 mar 2026</time>' in (
        owner.post("/recurring/preview", form(owner)).text
    )


def test_list_navigation_delete_confirmation_and_action_feedback(owner):
    assert owner.post("/recurring", form(owner)).status_code == 303
    listing = owner.client.get("/recurring").text
    assert 'data-recurring-link aria-current="page"' in listing
    delete_form = listing.split('/delete"')[1].split(">")[0]
    assert 'data-confirm="Delete the recurring item Car insurance?' in delete_form
    with owner.db() as session:
        from sqlalchemy import select

        item_id = session.scalars(select(ScheduledItem.id)).one()
    assert owner.post(f"/recurring/{item_id}/archive").status_code == 303
    again = owner.post(f"/recurring/{item_id}/pause")
    assert again.headers["location"] == "/recurring?error=archived_item"
    shown = owner.client.get(again.headers["location"]).text
    assert "data-action-error" in shown and "is archived and cannot change" in shown


def test_edit_form_keeps_an_archived_category_visible(owner):
    data = form(owner)
    assert owner.post("/recurring", data).status_code == 303
    assert owner.post(f"/categories/{data['category_id']}/archive").status_code in (200, 303)
    with owner.db() as session:
        from sqlalchemy import select

        item_id = session.scalars(select(ScheduledItem.id)).one()
    page = owner.client.get(f"/recurring/{item_id}/edit").text
    option = page.split("data-archived-category")[0].rsplit("<option", 1)[1]
    assert data["category_id"] in option and "selected disabled" in option
    assert "data-archived-notice" in page
    # The disabled option is not submitted: saving asks for an active category.
    unchanged = {key: value for key, value in data.items() if key != "category_id"}
    response = owner.post(f"/recurring/{item_id}/edit", unchanged)
    assert response.status_code == 400 and 'id="category_id-error"' in response.text


def test_country_without_holiday_data_is_explained_on_the_settings_page(owner):
    assert (
        owner.post("/recurring", form(owner, weekend_policy="next_working_day")).status_code == 303
    )
    response = owner.post(
        "/workspace/settings",
        {
            "name": "Home",
            "base_currency": "EUR",
            "country_code": "AC",
            "timezone": "UTC",
            "locale": "en",
        },
        token_from="/workspace/settings",
    )
    assert response.status_code == 400 and "Holiday data is not available" in response.text


def test_one_time_item_form_ignores_repeat_fields_and_lists_its_date(owner):
    owner.app.state.clock = FixedClock("2026-03-01T12:00:00+00:00")
    page = owner.client.get("/recurring/new")
    assert '<option value="once">One time</option>' in page.text
    assert 'data-hidden-when="freq=once"' in page.text
    # The repeat fields would be invalid for a recurring rule; a one-time item ignores them.
    data = form(
        owner,
        name="Wedding gift",
        freq="once",
        start_date="2026-05-02",
        weekend_policy="next_working_day",
        end_date="2026-01-01",
        max_count="0",
        interval="0",
    )
    preview = owner.post("/recurring/preview", data)
    assert "2026-05-04" in preview.text and "2026-06" not in preview.text
    assert owner.post("/recurring", data).status_code == 303
    from sqlalchemy import select

    with owner.db() as session:
        item = session.scalars(select(ScheduledItem)).one()
        assert (item.freq, item.interval, item.end_date, item.max_count, item.month_day) == (
            "once",
            1,
            None,
            None,
            "1",
        )
    listing = owner.client.get("/recurring")
    assert "One time on" in listing.text and "Active" in listing.text
    edit = owner.client.get(f"/recurring/{item.id}/edit")
    assert '<option value="once" selected>One time</option>' in edit.text
