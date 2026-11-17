"""The personal sidebar preference (F9.12).

The pure order/visibility rules live in ``domain/navigation.py``. These entry points read the
posted order, apply a move, normalise the result and store it on the user row. ``None`` is the
canonical order and every destination visible; saving the canonical list stores ``None`` again,
so a destination added in a later release appears at its canonical position for users who never
customised their sidebar.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from assetboard.db.engine import run_immediate
from assetboard.db.models.identity import User
from assetboard.domain.navigation import (
    DEFAULT_SIDEBAR,
    SidebarItem,
    move_sidebar,
    normalize_sidebar,
    serialize_sidebar,
)
from assetboard.services.context import DemoRestricted


def _ensure_not_demo(factory: sessionmaker[Session], user_id: UUID) -> None:
    # The public demo account is read-only (F9.11b): refuse before validating or writing.
    with factory() as session:
        if session.scalar(select(User.is_demo).where(User.id == user_id)):
            raise DemoRestricted


@dataclass(frozen=True, slots=True)
class SidebarMove:
    key: str
    direction: str


def parse_sidebar_move(value: object) -> SidebarMove | None:
    """``"<key>:up"`` / ``"<key>:down"``; anything else (or Home) is no move."""
    if not isinstance(value, str):
        return None
    key, separator, direction = value.partition(":")
    if not separator or not key or direction not in {"up", "down"}:
        return None
    return SidebarMove(key, direction)


def save_sidebar(
    factory: sessionmaker[Session],
    *,
    user_id: UUID,
    order: Sequence[str],
    visible: Sequence[str],
    move: str = "",
) -> tuple[SidebarItem, ...]:
    """Normalise the posted order, apply an optional move and persist the result.

    ``order`` is every row in displayed order (including hidden ones); ``visible`` names the
    checked keys. Unknown or missing keys follow the domain rules. Returns the stored order.
    """
    _ensure_not_demo(factory, user_id)
    visible_keys = set(visible)
    raw = [{"key": key, "visible": key in visible_keys} for key in order]
    items = normalize_sidebar(raw)
    parsed = parse_sidebar_move(move)
    if parsed is not None:
        items = move_sidebar(items, parsed.key, parsed.direction)
    stored = None if items == DEFAULT_SIDEBAR else serialize_sidebar(items)

    def save(session: Session) -> None:
        user = session.get(User, user_id)
        if user is None:
            return
        if user.is_demo:
            raise DemoRestricted
        user.sidebar_items = stored

    run_immediate(factory, save)
    return items


def reset_sidebar(factory: sessionmaker[Session], *, user_id: UUID) -> None:
    """Store ``None``: the canonical order with every destination visible."""
    _ensure_not_demo(factory, user_id)

    def reset(session: Session) -> None:
        user = session.get(User, user_id)
        if user is None:
            return
        if user.is_demo:
            raise DemoRestricted
        user.sidebar_items = None

    run_immediate(factory, reset)
