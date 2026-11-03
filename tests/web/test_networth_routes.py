"""Net-worth page over HTTP (F5.1): tabs, inline forms, history and bulk add."""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select

from assetboard.db.models.networth import ItemValuation, NetWorthCategory, NetWorthItem
from tests.conftest import OwnerApp

NO_PERMISSION = "You do not have permission to view this page."


def _category_id(owner: OwnerApp, side: str, name: str) -> UUID:
    with owner.db() as session:
        return session.scalars(
            select(NetWorthCategory.id).where(
                NetWorthCategory.workspace_id == owner.home_id,
                NetWorthCategory.side == side,
                NetWorthCategory.name == name,
            )
        ).one()


def _clear_side(owner: OwnerApp, side: str) -> None:
    with owner.db() as session, session.begin():
        session.execute(
            delete(NetWorthCategory).where(
                NetWorthCategory.workspace_id == owner.home_id,
                NetWorthCategory.side == side,
            )
        )


def _create_item(
    owner: OwnerApp,
    *,
    name: str = "Car",
    value: str = "12000",
    side: str = "asset",
    category: str = "Vehicles",
    currency: str = "EUR",
    notes: str = "",
) -> UUID:
    response = owner.post(
        "/net-worth/items",
        {
            "side": side,
            "category_id": str(_category_id(owner, side, category)),
            "name": name,
            "value": value,
            "currency": currency,
            "notes": notes,
            "include_in_net_worth": "1",
        },
        token_from="/net-worth",
    )
    assert response.status_code == 303, response.text
    with owner.db() as session:
        return session.scalars(
            select(NetWorthItem.id).where(
                NetWorthItem.workspace_id == owner.home_id,
                NetWorthItem.name == name,
            )
        ).one()


def _htmx(owner: OwnerApp, path: str, data: dict[str, str] | None = None) -> Any:
    headers = {"HX-Request": "true", "X-CSRF-Token": owner.token("/net-worth")}
    return owner.client.post(path, data=data or {}, headers=headers, follow_redirects=False)


def _create_category(owner: OwnerApp, name: str, side: str = "asset") -> UUID:
    response = owner.post(
        "/net-worth/categories",
        {"side": side, "name": name, "color": "#16a34a"},
        token_from="/net-worth",
    )
    assert response.status_code == 303, response.text
    return _category_id(owner, side, name)


def test_the_sidebar_links_to_the_page(owner: OwnerApp) -> None:
    page = owner.client.get("/").text
    assert "data-net-worth-link" in page
    assert owner.client.get("/net-worth").status_code == 200


def test_the_page_has_assets_and_liabilities_tabs(owner: OwnerApp) -> None:
    page = owner.client.get("/net-worth").text
    assert 'data-side-tab="asset"' in page and 'data-side-tab="liability"' in page
    assert 'aria-selected="true"' in page
    assert "Vehicles" in page and "Mortgage" not in page
    liability = owner.client.get("/net-worth?side=liability").text
    assert "Mortgage" in liability and "Vehicles" not in liability
    assert re.search(r'aria-selected="true"[^>]*data-side-tab="liability"', liability) is not None


def test_the_seed_form_works_for_an_empty_side(owner: OwnerApp) -> None:
    _clear_side(owner, "liability")
    page = owner.client.get("/net-worth?side=liability").text
    assert "data-seed-offer" in page and "data-seed" in page
    response = owner.post(
        "/net-worth/categories/seed",
        {"side": "liability"},
        token_from="/net-worth?side=liability",
    )
    assert response.status_code == 303
    assert response.headers["location"] == ("/net-worth?side=liability&notice=categories_seeded")
    with owner.db() as session:
        names = session.scalars(
            select(NetWorthCategory.name)
            .where(
                NetWorthCategory.workspace_id == owner.home_id,
                NetWorthCategory.side == "liability",
            )
            .order_by(NetWorthCategory.sort_order)
        ).all()
    assert names == ["Installments", "Mortgage", "Other"]
    assert (
        "Suggested categories added."
        in owner.client.get("/net-worth?side=liability&notice=categories_seeded").text
    )


def test_create_item_shows_the_value_and_history(owner: OwnerApp) -> None:
    item = _create_item(owner, name="Car", value="12000", notes="Garage")
    page = owner.client.get("/net-worth").text
    assert "Car" in page and "Garage" in page
    assert "12 000,00" in page or "12,000.00" in page
    assert "data-current-value" in page
    assert "data-valuation=" in page
    assert "<polyline" in page
    with owner.db() as session:
        assert (
            len(session.scalars(select(ItemValuation).where(ItemValuation.item_id == item)).all())
            == 1
        )


def test_too_many_decimals_are_shown_on_the_item_form(owner: OwnerApp) -> None:
    response = owner.post(
        "/net-worth/items",
        {
            "side": "asset",
            "category_id": str(_category_id(owner, "asset", "Vehicles")),
            "name": "Car",
            "value": "12.345",
            "currency": "EUR",
        },
        token_from="/net-worth",
    )
    assert response.status_code == 400
    assert "The amount has too many decimal places." in response.text
    assert 'value="12.345"' in response.text


def test_add_a_backdated_value_and_see_it_in_the_history(owner: OwnerApp) -> None:
    item = _create_item(owner)
    response = owner.post(
        f"/net-worth/items/{item}/valuations",
        {"side": "asset", "value": "9000", "as_of": "2020-01-01", "note": "Old value"},
        token_from="/net-worth",
    )
    assert response.status_code == 303
    page = owner.client.get("/net-worth").text
    assert "Old value" in page
    assert "2020" in page
    assert "data-valuation" in page
    with owner.db() as session:
        assert (
            len(session.scalars(select(ItemValuation).where(ItemValuation.item_id == item)).all())
            == 2
        )


def test_edit_item_is_inline_and_does_not_append_a_valuation(owner: OwnerApp) -> None:
    item = _create_item(owner)
    edit = owner.client.get("/net-worth?edit_item=" + str(item)).text
    assert "data-editing" in edit and "Better car" not in edit
    response = owner.post(
        f"/net-worth/items/{item}",
        {
            "side": "asset",
            "category_id": str(_category_id(owner, "asset", "Vehicles")),
            "name": "Better car",
            "currency": "EUR",
            "notes": "Updated",
            "include_in_net_worth": "1",
        },
        token_from="/net-worth",
    )
    assert response.status_code == 303
    page = owner.client.get("/net-worth").text
    assert "Better car" in page and "Updated" in page
    with owner.db() as session:
        assert (
            len(session.scalars(select(ItemValuation).where(ItemValuation.item_id == item)).all())
            == 1
        )


def test_archive_and_restore_item_keep_the_history(owner: OwnerApp) -> None:
    item = _create_item(owner)
    archived = _htmx(owner, f"/net-worth/items/{item}/archive", {"side": "asset"})
    assert archived.status_code == 200
    assert "Item archived." in archived.text
    assert "data-archived-items" in archived.text
    restored = _htmx(owner, f"/net-worth/items/{item}/restore", {"side": "asset"})
    assert "Item restored." in restored.text
    assert 'data-item="' + str(item) + '"' in restored.text


def test_category_crud_reorder_archive_restore_and_delete(owner: OwnerApp) -> None:
    kind = _create_category(owner, "Garden")
    other = _create_category(owner, "Tools")
    page = owner.client.get("/net-worth").text
    assert "Garden" in page and "Tools" in page
    moved = _htmx(owner, f"/net-worth/categories/{kind}/move", {"side": "asset", "direction": "up"})
    assert moved.status_code == 200
    updated = _htmx(
        owner,
        f"/net-worth/categories/{kind}",
        {"side": "asset", "name": "Outdoor", "color": "#0ea5e9"},
    )
    assert "Outdoor" in updated.text
    archived = _htmx(owner, f"/net-worth/categories/{other}/archive", {"side": "asset"})
    assert "Category archived." in archived.text
    restored = _htmx(owner, f"/net-worth/categories/{other}/restore", {"side": "asset"})
    assert "Category restored." in restored.text
    deleted = owner.post(
        f"/net-worth/categories/{other}/delete",
        {"side": "asset"},
        token_from="/net-worth",
    )
    assert deleted.headers["location"] == "/net-worth?side=asset&notice=category_deleted"


def test_bulk_preview_and_create(owner: OwnerApp) -> None:
    text = "Car; 12000; Vehicles\nBike; 1 200,50; Electronics"
    preview = _htmx(owner, "/net-worth/bulk/preview", {"side": "asset", "text": text})
    assert preview.status_code == 200
    assert "data-bulk-preview" in preview.text and "data-bulk-create" in preview.text
    assert "1200.50" in preview.text or "1 200,50" in preview.text
    created = _htmx(owner, "/net-worth/bulk", {"side": "asset", "text": text})
    assert created.status_code == 200
    assert "Items added." in created.text
    assert "Car" in created.text and "Bike" in created.text
    with owner.db() as session:
        assert len(session.scalars(select(NetWorthItem)).all()) == 2


def test_bulk_preview_reports_an_unknown_category_and_cannot_create(owner: OwnerApp) -> None:
    preview = _htmx(
        owner,
        "/net-worth/bulk/preview",
        {"side": "asset", "text": "Car; 10; Missing"},
    )
    assert preview.status_code == 200
    assert "No active category with this name." in preview.text
    assert "data-bulk-create" not in preview.text
    created = _htmx(owner, "/net-worth/bulk", {"side": "asset", "text": "Car; 10; Missing"})
    assert created.status_code == 400
    assert "No active category with this name." in created.text


def test_bulk_preview_shows_a_decimal_error(owner: OwnerApp) -> None:
    preview = _htmx(
        owner,
        "/net-worth/bulk/preview",
        {"side": "asset", "text": "Car; 12.345; Vehicles"},
    )
    assert preview.status_code == 200
    assert "The amount has too many decimal places." in preview.text
    assert "data-bulk-create" not in preview.text


def test_bulk_limits_show_a_form_error(owner: OwnerApp) -> None:
    too_many = "\n".join("Car; 1; Vehicles" for _ in range(501))
    response = _htmx(owner, "/net-worth/bulk/preview", {"side": "asset", "text": too_many})
    assert response.status_code == 400
    assert "Paste at most 500 lines." in response.text
    too_large = "Car; 1; Vehicles\n" + "x" * (64 * 1024)
    response = _htmx(owner, "/net-worth/bulk/preview", {"side": "asset", "text": too_large})
    assert response.status_code == 400
    assert "Paste at most 64 KiB of text." in response.text


def test_htmx_edit_and_plain_post_fallback(owner: OwnerApp) -> None:
    item = _create_item(owner)
    edit = owner.client.get(f"/net-worth/items/{item}/edit", headers={"HX-Request": "true"})
    assert edit.status_code == 200 and edit.text.lstrip().startswith('<div id="networth-body"')
    row = owner.client.get(f"/net-worth/items/{item}/row", headers={"HX-Request": "true"})
    assert "data-editing" not in row.text
    redirect = owner.client.get(f"/net-worth/items/{item}/edit", follow_redirects=False)
    assert redirect.status_code == 303
    assert redirect.headers["location"] == f"/net-worth?side=asset&edit_item={item}"


def test_writes_need_csrf(owner: OwnerApp) -> None:
    response = owner.client.post(
        "/net-worth/items",
        data={
            "side": "asset",
            "category_id": str(_category_id(owner, "asset", "Vehicles")),
            "name": "Car",
            "value": "1",
            "currency": "EUR",
        },
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert "Car" not in owner.client.get("/net-worth").text


def test_viewers_see_no_controls_and_cannot_write(owner: OwnerApp) -> None:
    item = _create_item(owner)
    owner.set_admin_role("viewer")
    page = owner.client.get("/net-worth").text
    assert "Car" in page
    for marker in ("data-add-item", "data-bulk", "data-edit", "data-archive", "data-delete"):
        assert marker not in page
    response = owner.post(
        f"/net-worth/items/{item}/archive",
        {"side": "asset"},
        token_from="/net-worth",
    )
    assert response.status_code == 403
    assert NO_PERMISSION in response.text


def test_another_workspaces_item_is_404(owner: OwnerApp) -> None:
    item = _create_item(owner)
    other = owner.add_workspace("Other")
    owner.join(owner.admin_id, other, "owner")
    owner.post("/workspace/switch", {"workspace_id": str(other)})
    for path in (
        f"/net-worth/items/{item}/edit",
        f"/net-worth/items/{item}/row",
    ):
        response = owner.client.get(path, headers={"HX-Request": "true"})
        assert response.status_code == 404, path
    for path, data in (
        (f"/net-worth/items/{item}/archive", {"side": "asset"}),
        (f"/net-worth/items/{item}/valuations", {"side": "asset", "value": "1"}),
    ):
        response = owner.post(path, data, token_from="/net-worth")
        assert response.status_code == 404, path


def test_markup_is_csp_safe_and_escaped(owner: OwnerApp) -> None:
    _create_item(owner, name="<b>Car</b> & boat")
    page = owner.client.get("/net-worth").text
    assert "<b>Car</b>" not in page
    assert "&lt;b&gt;Car&lt;/b&gt; &amp; boat" in page
    assert " style=" not in page
    assert re.search(r"<script(?![^>]*\ssrc=)", page) is None
    assert "x-data" not in page and "@click" not in page
    assert 'stroke="currentColor"' in page


def test_networth_events_appear_in_the_audit_log(owner: OwnerApp) -> None:
    _create_item(owner, name="Car")
    page = owner.client.get("/audit?scope=workspace&group=net_worth").text
    assert "Net-worth item added" in page
    assert "Net-worth categories added" in page or "Suggested net-worth categories added" in page
    owner.client.headers["Accept-Language"] = "pl"
    page = owner.client.get("/audit?scope=workspace&group=net_worth").text
    assert "Dodano pozycję majątku" in page


def test_the_page_is_translated(owner: OwnerApp) -> None:
    owner.client.headers["Accept-Language"] = "pl"
    page = owner.client.get("/net-worth").text
    assert "Majątek netto" in page
    assert "Aktywa" in page and "Zobowiązania" in page
    assert "Dodaj pozycję" in page


def test_estimate_between_defaults_by_side_and_unchecking_reaches_the_service(
    owner: OwnerApp,
) -> None:
    asset_form = owner.client.get("/net-worth?side=asset").text
    assert re.search(
        r'name="estimate_between" value="1" checked[^>]*> Estimate values between entries',
        asset_form,
    )
    liability_form = owner.client.get("/net-worth?side=liability").text
    assert not re.search(
        r'name="estimate_between" value="1" checked[^>]*> Estimate values between entries',
        liability_form,
    )

    response = owner.post(
        "/net-worth/items",
        {
            "side": "asset",
            "category_id": str(_category_id(owner, "asset", "Vehicles")),
            "name": "Car",
            "value": "12000",
            "currency": "EUR",
            "estimate_between": "1",
            "include_in_net_worth": "1",
        },
        token_from="/net-worth",
    )
    assert response.status_code == 303
    with owner.db() as session:
        item = session.scalars(
            select(NetWorthItem).where(
                NetWorthItem.workspace_id == owner.home_id, NetWorthItem.name == "Car"
            )
        ).one()
        assert item.estimate_between is True
        item_id = item.id

    # An unchecked checkbox sends no field; it must still reach the service as False.
    response = owner.post(
        f"/net-worth/items/{item_id}",
        {
            "side": "asset",
            "category_id": str(_category_id(owner, "asset", "Vehicles")),
            "name": "Car",
            "currency": "EUR",
            "include_in_net_worth": "1",
        },
        token_from="/net-worth",
    )
    assert response.status_code == 303
    with owner.db() as session:
        item = session.get(NetWorthItem, item_id)
        assert item is not None and item.estimate_between is False
