"""Real estate in net worth (F5.7) over HTTP: the new seed and the "Add missing suggested
categories" action (per side, editors and owners, CSRF, idempotent)."""

from __future__ import annotations

from sqlalchemy import delete, select

from assetboard.db.models.identity import AuditEvent
from assetboard.db.models.networth import NetWorthCategory
from tests.conftest import OwnerApp

PAGE = "/net-worth"
SEED = "/net-worth/categories/seed"


def _names(owner: OwnerApp, side: str = "asset") -> list[str]:
    with owner.db() as session:
        return list(
            session.scalars(
                select(NetWorthCategory.name)
                .where(
                    NetWorthCategory.workspace_id == owner.home_id,
                    NetWorthCategory.side == side,
                )
                .order_by(NetWorthCategory.sort_order)
            )
        )


def _drop_real_estate(owner: OwnerApp) -> None:
    """A workspace seeded before F5.7: the asset side has no Real estate."""
    with owner.db() as session, session.begin():
        session.execute(
            delete(NetWorthCategory).where(
                NetWorthCategory.workspace_id == owner.home_id,
                NetWorthCategory.name == "Real estate",
            )
        )


def test_a_new_workspace_has_real_estate_first_and_nothing_missing(owner: OwnerApp) -> None:
    assert _names(owner)[0] == "Real estate"
    assert len(_names(owner)) == 9
    page = owner.client.get(PAGE).text
    assert "Real estate" in page
    assert "data-add-missing-suggested" not in page


def test_an_older_workspace_adds_real_estate_once(owner: OwnerApp) -> None:
    _drop_real_estate(owner)
    before = _names(owner)
    page = owner.client.get(PAGE).text
    assert "data-add-missing-suggested" in page
    assert "Add missing suggested categories" in page

    response = owner.post(SEED, {"side": "asset"}, token_from=PAGE)

    assert response.status_code == 303
    assert response.headers["location"] == f"{PAGE}?side=asset&notice=categories_seeded"
    assert _names(owner) == [*before, "Real estate"]  # appended, nothing reordered
    assert "data-add-missing-suggested" not in owner.client.get(PAGE).text

    again = owner.post(SEED, {"side": "asset"}, token_from=PAGE)

    assert again.headers["location"] == f"{PAGE}?side=asset&notice=categories_complete"
    assert _names(owner) == [*before, "Real estate"]
    assert (
        "Every suggested category is already on this side."
        in owner.client.get(f"{PAGE}?side=asset&notice=categories_complete").text
    )
    with owner.db() as session:
        manual = [
            event
            for event in session.scalars(
                select(AuditEvent).where(
                    AuditEvent.workspace_id == owner.home_id,
                    AuditEvent.action == "networth_category.seeded",
                )
            )
            if not event.data.get("automatic")
        ]
    assert [event.data for event in manual] == [
        {"count": 1, "side": "asset", "names": "Real estate"}
    ]


def test_the_action_needs_csrf_and_an_editor(owner: OwnerApp) -> None:
    _drop_real_estate(owner)
    before = _names(owner)
    refused = owner.client.post(SEED, data={"side": "asset"}, follow_redirects=False)
    assert refused.status_code == 403
    owner.set_admin_role("viewer")
    page = owner.client.get(PAGE).text
    assert "data-add-missing-suggested" not in page
    forbidden = owner.post(SEED, {"side": "asset"}, token_from=PAGE)
    assert forbidden.status_code == 403
    assert _names(owner) == before


def test_the_polish_seed_has_nieruchomosci_first(owner: OwnerApp) -> None:
    with owner.db() as session, session.begin():
        session.execute(
            delete(NetWorthCategory).where(
                NetWorthCategory.workspace_id == owner.home_id,
                NetWorthCategory.side == "asset",
            )
        )
    owner.client.headers["Accept-Language"] = "pl"
    response = owner.post(SEED, {"side": "asset"}, token_from=PAGE)
    assert response.status_code == 303
    assert _names(owner)[:2] == ["Nieruchomości", "Pojazdy"]
    assert len(_names(owner)) == 9
