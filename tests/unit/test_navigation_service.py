"""Saving and resetting the personal sidebar (F9.12)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from assetboard.db.models.identity import User
from assetboard.domain.navigation import CANONICAL_KEYS
from assetboard.services.context import DemoRestricted
from assetboard.services.navigation import reset_sidebar, save_sidebar
from tests.tenant.workspace_db import WorkspaceDb, workspace_db

DEFAULT_ORDER = list(CANONICAL_KEYS)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[WorkspaceDb]:
    yield from workspace_db(tmp_path)


def add_member(db: WorkspaceDb, key: str = "ola", role: str = "owner") -> UUID:
    db.add_member(key, role)
    return db.users[key]


def add_demo_user(db: WorkspaceDb) -> UUID:
    with db.factory.begin() as session:
        user = User(
            email="demo@example.invalid",
            display_name="Demo",
            password_hash=None,
            is_demo=True,
        )
        session.add(user)
        session.flush()
        return user.id


def stored(db: WorkspaceDb, user_id: UUID) -> list[dict[str, object]] | None:
    with db.factory() as session:
        user = session.get(User, user_id)
        assert user is not None
        return user.sidebar_items


def test_default_is_null_and_saving_the_canonical_list_stores_null(db: WorkspaceDb) -> None:
    user_id = add_member(db)
    assert stored(db, user_id) is None
    save_sidebar(
        db.factory,
        user_id=user_id,
        order=DEFAULT_ORDER,
        visible=DEFAULT_ORDER,
    )
    assert stored(db, user_id) is None


def test_save_custom_order_and_visibility(db: WorkspaceDb) -> None:
    user_id = add_member(db)
    order = ["home", "loans", "month", "transactions", "history", "upcoming", "recurring",
             "income", "taxes", "net_worth", "investments", "fire"]  # fmt: skip
    visible = [key for key in order if key not in {"history", "taxes"}]
    result = save_sidebar(db.factory, user_id=user_id, order=order, visible=visible)
    assert [item.key for item in result] == order
    assert stored(db, user_id) == [
        {"key": "home", "visible": True},
        {"key": "loans", "visible": True},
        {"key": "month", "visible": True},
        {"key": "transactions", "visible": True},
        {"key": "history", "visible": False},
        {"key": "upcoming", "visible": True},
        {"key": "recurring", "visible": True},
        {"key": "income", "visible": True},
        {"key": "taxes", "visible": False},
        {"key": "net_worth", "visible": True},
        {"key": "investments", "visible": True},
        {"key": "fire", "visible": True},
    ]


def test_hidden_items_keep_their_position(db: WorkspaceDb) -> None:
    user_id = add_member(db)
    save_sidebar(
        db.factory,
        user_id=user_id,
        order=DEFAULT_ORDER,
        visible=[key for key in DEFAULT_ORDER if key != "loans"],
    )
    entries = stored(db, user_id) or []
    assert entries[10] == {"key": "loans", "visible": False}


def test_missing_keys_are_appended_visible(db: WorkspaceDb) -> None:
    user_id = add_member(db)
    save_sidebar(db.factory, user_id=user_id, order=["home"], visible=[])
    # Every missing destination is appended visible, which is the canonical list again.
    assert stored(db, user_id) is None


def test_move_is_applied_after_normalisation(db: WorkspaceDb) -> None:
    user_id = add_member(db)
    result = save_sidebar(
        db.factory,
        user_id=user_id,
        order=DEFAULT_ORDER,
        visible=DEFAULT_ORDER,
        move="income:up",
    )
    assert [item.key for item in result] == [
        "home", "month", "transactions", "history", "upcoming", "income", "recurring",
        "taxes", "net_worth", "investments", "loans", "fire",
    ]  # fmt: skip
    assert [entry["key"] for entry in stored(db, user_id) or []][:7] == [
        "home", "month", "transactions", "history", "upcoming", "income", "recurring",
    ]  # fmt: skip


@pytest.mark.parametrize("move", ["unknown:up", "income:sideways", "income", ":up", "home:up"])
def test_unknown_or_home_moves_are_ignored(db: WorkspaceDb, move: str) -> None:
    user_id = add_member(db)
    save_sidebar(db.factory, user_id=user_id, order=DEFAULT_ORDER, visible=DEFAULT_ORDER, move=move)
    assert stored(db, user_id) is None


def test_reset_stores_null(db: WorkspaceDb) -> None:
    user_id = add_member(db)
    save_sidebar(
        db.factory,
        user_id=user_id,
        order=["home", "loans", *DEFAULT_ORDER[1:-1]],
        visible=["home"],
    )
    assert stored(db, user_id) is not None
    reset_sidebar(db.factory, user_id=user_id)
    assert stored(db, user_id) is None


def test_saving_one_user_does_not_touch_another(db: WorkspaceDb) -> None:
    ola = add_member(db, "ola", "owner")
    piotr = add_member(db, "piotr", "viewer")
    save_sidebar(db.factory, user_id=ola, order=["home", "loans", *DEFAULT_ORDER[1:-1]],
                 visible=["home", "loans"])  # fmt: skip
    assert stored(db, ola) is not None
    assert stored(db, piotr) is None
    reset_sidebar(db.factory, user_id=ola)
    save_sidebar(db.factory, user_id=piotr, order=["home", "income", *DEFAULT_ORDER[1:-2]],
                 visible=["home", "income"])  # fmt: skip
    assert stored(db, ola) is None
    assert (stored(db, piotr) or [])[1] == {"key": "income", "visible": True}


@pytest.mark.parametrize("operation", ["save", "reset"])
def test_the_demo_user_is_refused_before_anything_is_written(
    db: WorkspaceDb, operation: str
) -> None:
    user_id = add_demo_user(db)
    with pytest.raises(DemoRestricted):
        if operation == "save":
            save_sidebar(
                db.factory,
                user_id=user_id,
                order=DEFAULT_ORDER,
                visible=DEFAULT_ORDER,
            )
        else:
            reset_sidebar(db.factory, user_id=user_id)
    assert stored(db, user_id) is None


def test_serialised_sidebar_is_json_ready(db: WorkspaceDb) -> None:
    user_id = add_member(db)
    save_sidebar(
        db.factory,
        user_id=user_id,
        order=DEFAULT_ORDER,
        visible=[key for key in DEFAULT_ORDER if key != "fire"],
    )
    with db.factory() as session:
        user = session.scalars(select(User).where(User.id == user_id)).one()
        assert user.sidebar_items is not None
        assert all(set(entry) == {"key", "visible"} for entry in user.sidebar_items)
        assert all(isinstance(entry["visible"], bool) for entry in user.sidebar_items)
