"""The F8.1 FIRE scenario settings pages."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select

from assetboard.db.models.fire import FireContribution, FireEvent, FireScenario
from assetboard.db.models.identity import User


def _form(**overrides: str) -> dict[str, str]:
    data = {
        "name": "Retire at 55",
        "birth_year": "1985",
        "inflation": "2.5",
        "real_return": "5",
        "income_growth": "3",
        "stop_working_age": "55",
        "horizon_age": "90",
        "rule": "returns_cover_expenses",
        "swr": "4",
        "income_mode": "manual",
        "income_manual": "50000",
        "expenses_mode": "manual",
        "expenses_manual": "30000",
        "portfolio_mode": "manual",
        "portfolio_manual": "1000000",
        "contribution_year_1": "",
        "contribution_amount_1": "",
        "event_year_1": "",
        "event_kind_1": "cash",
        "event_amount_1": "",
        "event_label_1": "",
    }
    data.update(overrides)
    return data


def _create(owner, **overrides: str) -> UUID:
    response = owner.post("/fire", _form(**overrides), token_from="/fire/new")
    assert response.status_code == 303, response.text
    return UUID(response.headers["location"].split("?")[0].rsplit("/", 1)[-1])


def _scenario_ids(owner) -> list[UUID]:
    with owner.db() as session:
        return list(
            session.scalars(
                select(FireScenario.id).where(FireScenario.workspace_id == owner.home_id)
            )
        )


def test_the_nav_link_the_empty_list_and_the_new_form(owner) -> None:
    home = owner.client.get("/").text
    assert 'href="/fire"' in home and "data-fire-link" in home
    assert "Unavailable" not in home

    page = owner.client.get("/fire")
    assert page.status_code == 200
    assert "data-fire-empty" in page.text
    assert "data-fire-new" in page.text
    assert "not financial advice" in page.text

    form = owner.client.get("/fire/new")
    assert form.status_code == 200
    for marker in (
        'name="birth_year"',
        'name="horizon_age"',
        'name="stop_working_age"',
        'name="inflation"',
        'name="real_return"',
        'name="income_growth"',
        'name="rule"',
        'name="swr"',
        'name="income_manual"',
        'name="expenses_manual"',
        'name="portfolio_manual"',
        "data-contribution-paste",
        "data-contribution-template",
        "data-event-template",
        "data-fire-form",
        "js/fire.js",
    ):
        assert marker in form.text, marker
    assert 'value="2026"' in form.text
    assert "readonly" in form.text
    assert "not financial advice" in form.text
    assert "The yearly rise in prices" in form.text
    assert "The age at which earned income stops" in form.text


def test_the_new_form_prefills_the_profile_birth_year_and_life_expectancy(owner) -> None:
    with owner.db() as session, session.begin():
        user = session.get_one(User, owner.admin_id)
        user.birth_year = 1980
        user.life_expectancy = 95
    form = owner.client.get("/fire/new").text
    assert 'name="birth_year" value="1980"' in form
    assert 'name="horizon_age" value="95"' in form


def test_create_edit_duplicate_default_archive_and_restore(owner) -> None:
    scenario_id = _create(
        owner,
        contribution_year_1="2027",
        contribution_amount_1="12000",
        event_year_1="2028",
        event_kind_1="expense_change",
        event_amount_1="-1200",
        event_label_1="Mortgage repaid",
    )
    detail = owner.client.get(f"/fire/{scenario_id}")
    assert detail.status_code == 200
    assert "data-fire-overview" in detail.text
    assert "Retire at 55" in detail.text
    assert "Mortgage repaid" in detail.text
    assert "data-fire-contributions" in detail.text and "12,000" in detail.text

    edit = owner.client.get(f"/fire/{scenario_id}/edit")
    assert edit.status_code == 200
    assert 'value="Retire at 55"' in edit.text
    assert 'value="12000' in edit.text

    response = owner.post(
        f"/fire/{scenario_id}",
        _form(name="Retire at 56", stop_working_age="56"),
        token_from=f"/fire/{scenario_id}/edit",
    )
    assert response.status_code == 303, response.text
    assert "Retire at 56" in owner.client.get(f"/fire/{scenario_id}").text

    duplicated = owner.post(f"/fire/{scenario_id}/duplicate", token_from=f"/fire/{scenario_id}")
    assert duplicated.status_code == 303
    copy_id = duplicated.headers["location"].rsplit("/", 1)[-1].split("?")[0]
    copy_page = owner.client.get(f"/fire/{copy_id}")
    assert "Retire at 56 (copy)" in copy_page.text

    default = owner.post(f"/fire/{copy_id}/default", token_from=f"/fire/{copy_id}")
    assert default.status_code == 303
    assert "Default" in owner.client.get(f"/fire/{copy_id}").text

    archived = owner.post(f"/fire/{scenario_id}/archive", token_from=f"/fire/{scenario_id}")
    assert archived.status_code == 303
    list_page = owner.client.get("/fire").text
    assert "data-fire-archived" in list_page
    active_part = list_page.split("data-fire-archived")[0]
    assert f'data-fire-scenario="{scenario_id}"' not in active_part

    archived_detail = owner.client.get(f"/fire/{scenario_id}").text
    assert "read-only" in archived_detail
    edit_archived = owner.client.get(f"/fire/{scenario_id}/edit", follow_redirects=False)
    assert edit_archived.status_code == 303
    assert "error=archived" in edit_archived.headers["location"]

    restored = owner.post(f"/fire/{scenario_id}/restore", token_from="/fire")
    assert restored.status_code == 303
    assert "read-only" not in owner.client.get(f"/fire/{scenario_id}").text


def test_contribution_and_event_rows_are_parsed_with_sparse_indices(owner) -> None:
    scenario_id = _create(
        owner,
        contribution_year_2="2029",
        contribution_amount_2="-1250.50",
        contribution_year_5="2028",
        contribution_amount_5="1000",
        event_year_3="2030",
        event_kind_3="cash",
        event_amount_3="25000",
        event_label_3="Inheritance",
        event_year_7="2031",
        event_kind_7="income_change",
        event_amount_7="-300",
        event_label_7="Rent ends",
    )
    with owner.db() as session:
        contributions = list(
            session.scalars(
                select(FireContribution)
                .where(FireContribution.scenario_id == scenario_id)
                .order_by(FireContribution.year)
            )
        )
        events = list(
            session.scalars(
                select(FireEvent)
                .where(FireEvent.scenario_id == scenario_id)
                .order_by(FireEvent.year)
            )
        )
    assert [(row.year, row.net_amount_minor) for row in contributions] == [
        (2028, 100_000),
        (2029, -125_050),
    ]
    assert [(row.year, row.kind, row.amount_minor, row.label) for row in events] == [
        (2030, "cash", 2_500_000, "Inheritance"),
        (2031, "income_change", -30_000, "Rent ends"),
    ]


def test_rows_outside_the_window_are_refused_and_kept_on_the_form(owner) -> None:
    response = owner.post(
        "/fire",
        _form(contribution_year_1="2026", contribution_amount_1="12000"),
        token_from="/fire/new",
    )
    assert response.status_code == 400
    assert "fall inside the plan" in response.text
    assert 'value="2026"' in response.text
    assert _scenario_ids(owner) == []


def test_a_missing_birth_year_points_to_the_profile_and_writes_nothing(owner) -> None:
    response = owner.post("/fire", _form(birth_year=""), token_from="/fire/new")
    assert response.status_code == 400
    assert "birth year" in response.text
    assert "Open your profile" in response.text
    assert 'href="/account"' in response.text
    assert _scenario_ids(owner) == []


def test_unusual_rates_need_the_confirmation_box(owner) -> None:
    response = owner.post("/fire", _form(inflation="12"), token_from="/fire/new")
    assert response.status_code == 400
    assert "outside the usual range" in response.text
    assert _scenario_ids(owner) == []
    scenario_id = _create(owner, inflation="12", extreme_rates_confirmed="1")
    assert scenario_id in _scenario_ids(owner)


def test_linked_modes_are_refused_with_a_clear_message(owner) -> None:
    response = owner.post(
        "/fire",
        _form(income_mode="trailing_12m"),
        token_from="/fire/new",
    )
    assert response.status_code == 400
    assert "later update" in response.text
    assert _scenario_ids(owner) == []


def test_duplicate_names_are_marked_on_the_name_field(owner) -> None:
    _create(owner)
    response = owner.post("/fire", _form(name="  retire AT 55 "), token_from="/fire/new")
    assert response.status_code == 400
    assert "already has this name" in response.text
    assert 'aria-invalid="true"' in response.text
    assert len(_scenario_ids(owner)) == 1


def test_blank_event_label_and_kind_are_refused(owner) -> None:
    response = owner.post(
        "/fire",
        _form(
            event_year_1="2028",
            event_kind_1="cash",
            event_amount_1="100",
            event_label_1="",
        ),
        token_from="/fire/new",
    )
    assert response.status_code == 400
    assert "short description" in response.text
    assert _scenario_ids(owner) == []


def test_viewers_read_but_see_no_write_controls_and_cannot_post(owner) -> None:
    scenario_id = _create(owner)
    owner.set_admin_role("viewer")
    listing = owner.client.get("/fire")
    assert listing.status_code == 200
    assert "Retire at 55" in listing.text
    for marker in ("data-fire-new", "data-fire-edit", "data-fire-archive", "data-fire-duplicate"):
        assert marker not in listing.text
    detail = owner.client.get(f"/fire/{scenario_id}")
    assert detail.status_code == 200
    assert "data-fire-edit" not in detail.text
    assert "data-fire-archive" not in detail.text
    response = owner.post("/fire", _form(), token_from="/fire")
    assert response.status_code == 403
    assert len(_scenario_ids(owner)) == 1


def test_the_form_stores_percentages_as_basis_points(owner) -> None:
    scenario_id = _create(
        owner,
        inflation="2.5",
        real_return="5",
        income_growth="3",
        rule="swr",
        swr="4",
    )
    with owner.db() as session:
        row = session.get_one(FireScenario, scenario_id)
    assert (row.inflation_bp, row.real_return_bp, row.income_growth_bp, row.swr_bp) == (
        250,
        500,
        300,
        400,
    )
