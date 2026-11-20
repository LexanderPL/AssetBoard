"""Small per-process cache for the Home net-worth and portfolio series (F11.12).

The Home shell fetches every widget in its own request, so the derived series are cached by
``(widget, workspace, workspace-local date, range)``. Any committed write to a table that can
change those series bumps the cache generation and drops the entries; a five-minute TTL only
bounds entries written through a path that bypasses SQLAlchemy (for example a future CLI that
edits the database directly). The app is documented to run one web worker, so the process-local
cache never has to coordinate with another worker.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timedelta
from threading import Lock

from sqlalchemy import Delete, Insert, Update, event
from sqlalchemy.orm import ORMExecuteState, Session

__all__ = [
    "cache_generation",
    "cache_get",
    "cache_put",
    "clear_home_cache",
]

# Entries are cheap to rebuild; the cap keeps a long-lived process from holding every workspace.
_MAX_ENTRIES = 32
_TTL = timedelta(minutes=5)
_DIRTY = "assetboard.home_cache_dirty"

CacheKey = tuple[object, ...]

# Table names (not model classes) so this service does not reference workspace-owned models:
# everything that can change a derived net-worth or portfolio value.
_RELEVANT_TABLES = frozenset(
    {
        "users",
        "workspaces",
        "networth_categories",
        "networth_items",
        "item_valuations",
        "asset_classes",
        "risk_target_policies",
        "instruments",
        "instrument_allocations",
        "investment_transactions",
        "bond_terms",
        "price_points",
        "fx_rates",
        "reference_rates",
        "reference_rate_values",
        "loans",
        "loan_transfers",
        "loan_rate_changes",
        "loan_cost_components",
        "loan_overpayments",
        "loan_balance_checkpoints",
        "loan_payments",
        "installment_plans",
    }
)

_lock = Lock()
_generation = 0
_entries: OrderedDict[CacheKey, tuple[datetime, int, object]] = OrderedDict()


def cache_generation() -> int:
    """The generation a caller captures before computing an entry."""
    with _lock:
        return _generation


def cache_get(key: CacheKey, *, now: datetime) -> object | None:
    """The fresh entry for ``key``, or ``None``; expired/stale entries are dropped."""
    with _lock:
        entry = _entries.get(key)
        if entry is None:
            return None
        created_at, generation, value = entry
        if generation != _generation or now - created_at >= _TTL:
            _entries.pop(key, None)
            return None
        _entries.move_to_end(key)
        return value


def cache_put(key: CacheKey, value: object, *, generation: int, now: datetime) -> None:
    """Store ``value`` unless a write happened while it was being computed."""
    with _lock:
        if generation != _generation:
            return
        _entries[key] = (now, generation, value)
        _entries.move_to_end(key)
        while len(_entries) > _MAX_ENTRIES:
            _entries.popitem(last=False)


def clear_home_cache() -> None:
    """Drop every entry and bump the generation; tests and explicit invalidation use this."""
    global _generation
    with _lock:
        _generation += 1
        _entries.clear()


def _invalidate() -> None:
    global _generation
    with _lock:
        _generation += 1
        _entries.clear()


@event.listens_for(Session, "do_orm_execute")
def _mark_statement(execute_state: ORMExecuteState) -> None:
    """Bulk/Core inserts, updates and deletes bypass the ORM flush hooks; mark those too."""
    statement = execute_state.statement
    if not isinstance(statement, (Insert, Update, Delete)):
        return
    table = getattr(statement, "table", None)
    if table is not None and table.name in _RELEVANT_TABLES:
        execute_state.session.info[_DIRTY] = True


@event.listens_for(Session, "after_flush")
def _mark_changed(session: Session, _flush_context: object) -> None:
    if session.info.get(_DIRTY):
        return
    for obj in (*session.new, *session.dirty, *session.deleted):
        if getattr(obj, "__tablename__", None) in _RELEVANT_TABLES:
            session.info[_DIRTY] = True
            return


@event.listens_for(Session, "after_commit")
def _committed(session: Session) -> None:
    if session.info.pop(_DIRTY, False):
        _invalidate()
