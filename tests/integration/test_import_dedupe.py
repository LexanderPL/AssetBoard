"""F4.2 dedupe helpers against the database (``services.import_dedupe``)."""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from assetboard.db.models.identity import Workspace
from assetboard.db.models.ledger import BudgetLine, Category, Transaction
from assetboard.domain.months import YearMonth
from assetboard.services.import_dedupe import (
    IncomingBudgetLine,
    IncomingTransaction,
    match_budget_lines,
    match_transactions,
)
from tests.conftest import FixedClock
from tests.tenant.harness import context_for


@pytest.fixture
def scope(db_session: Session, fixed_clock: FixedClock) -> Any:
    workspace = Workspace(name="Dedupe", base_currency="EUR", locale="en", timezone="UTC")
    db_session.add(workspace)
    db_session.flush()
    return context_for(workspace.id, fixed_clock)


@pytest.fixture
def category(db_session: Session, scope: Any) -> Category:
    row = Category(
        workspace_id=scope.workspace.id,
        name="Food",
        name_key="food",
        kind="expense",
        treatment="regular",
        color="#16a34a",
        sort_order=0,
    )
    db_session.add(row)
    db_session.flush()
    return row


def _row(category_id: UUID, line: int, **changes: Any) -> IncomingTransaction:
    values: dict[str, Any] = {
        "line": line,
        "source_id": None,
        "day": date(2026, 10, 3),
        "direction": "expense",
        "amount_minor": 425,
        "currency": "EUR",
        "category_id": category_id,
        "source_category_name_key": "food",
        "description": "Coffee",
        "description_key": "coffee",
    }
    values.update(changes)
    return IncomingTransaction(**values)


def _store(session: Session, scope: Any, row: IncomingTransaction, key: str, **extra: Any) -> Any:
    stored = Transaction(
        workspace_id=scope.workspace.id,
        direction=row.direction,
        date=row.day,
        amount_minor=row.amount_minor,
        currency=row.currency,
        category_id=row.category_id,
        description=row.description,
        description_key=row.description_key,
        source="import",
        import_key=key,
        created_by_user_id=None,
        **extra,
    )
    session.add(stored)
    session.flush()
    return stored


def test_identical_rows_in_one_file_get_their_own_occurrence(
    db_session: Session, scope: Any, category: Category
) -> None:
    rows = [_row(category.id, 2), _row(category.id, 3)]

    first = match_transactions(db_session, scope, rows)

    assert [match.action for match in first] == ["create", "create"]
    assert first[0].import_key != first[1].import_key
    _store(db_session, scope, rows[0], first[0].import_key)
    _store(db_session, scope, rows[1], first[1].import_key)

    # The same file again creates nothing; one more identical row adds only that row.
    again = match_transactions(db_session, scope, [*rows, _row(category.id, 4)])
    assert [(m.action, m.code) for m in again] == [
        ("duplicate", "already_imported"),
        ("duplicate", "already_imported"),
        ("create", None),
    ]


def test_deleted_import_is_not_brought_back(
    db_session: Session, scope: Any, category: Category, fixed_clock: FixedClock
) -> None:
    row = _row(category.id, 2)
    key = match_transactions(db_session, scope, [row])[0].import_key
    _store(db_session, scope, row, key, deleted_at=fixed_clock.now())

    [match] = match_transactions(db_session, scope, [row])

    assert (match.action, match.code) == ("duplicate", "imported_before_deleted")


def test_lineage_ids_match_local_ids_and_import_keys(
    db_session: Session, scope: Any, category: Category
) -> None:
    local = _store(db_session, scope, _row(category.id, 0), "c1:" + "0" * 64)
    exported_elsewhere = uuid4()
    _store(
        db_session,
        scope,
        _row(category.id, 0, description="Bread", description_key="bread"),
        f"id:{exported_elsewhere}",
    )
    rows = [
        _row(category.id, 2, source_id=local.id),
        _row(category.id, 3, source_id=exported_elsewhere, description="Rolls"),
        _row(category.id, 4, source_id=uuid4()),
        _row(category.id, 5, source_id=local.id),
    ]

    matches = match_transactions(db_session, scope, rows)

    assert [(m.action, m.code) for m in matches] == [
        ("duplicate", "already_imported"),
        ("conflict", "changed_since_export"),
        ("create", None),
        ("duplicate", "duplicate_in_file"),
    ]
    assert matches[0].existing_id == local.id
    assert matches[2].import_key == f"id:{rows[2].source_id}"


def test_lookups_ignore_other_workspaces(
    db_session: Session, scope: Any, category: Category, fixed_clock: FixedClock
) -> None:
    other = Workspace(name="Other", base_currency="EUR", locale="en", timezone="UTC")
    db_session.add(other)
    db_session.flush()
    other_scope = context_for(other.id, fixed_clock)
    row = _row(category.id, 2)
    key = match_transactions(db_session, scope, [row])[0].import_key
    stored = _store(db_session, scope, row, key)

    assert match_transactions(db_session, other_scope, [row])[0].action == "create"
    lineage = _row(category.id, 2, source_id=stored.id)
    assert match_transactions(db_session, other_scope, [lineage])[0].action == "create"


def test_budget_lines_dedupe_on_month_and_category(
    db_session: Session, scope: Any, category: Category
) -> None:
    for month, planned in ((date(2026, 9, 1), 4000), (date(2026, 10, 1), 5000)):
        db_session.add(
            BudgetLine(
                workspace_id=scope.workspace.id,
                month=month,
                category_id=category.id,
                planned_minor=planned,
                currency="EUR",
            )
        )
    db_session.flush()
    september, october, november = YearMonth(2026, 9), YearMonth(2026, 10), YearMonth(2026, 11)
    rows = [
        IncomingBudgetLine(1, september, category.id, 4500),
        IncomingBudgetLine(2, october, category.id, 5000),
        IncomingBudgetLine(3, october, category.id, 6000),
        IncomingBudgetLine(4, november, category.id, 7000),
        IncomingBudgetLine(5, november, category.id, 7000),
        IncomingBudgetLine(6, november, category.id, 8000),
    ]

    matches = match_budget_lines(db_session, scope, rows)

    assert [(m.action, m.code) for m in matches] == [
        ("conflict", "planned_value_differs"),
        ("duplicate", "planned_value_exists"),
        ("conflict", "conflict_in_file"),
        ("create", None),
        ("duplicate", "duplicate_in_file"),
        ("conflict", "conflict_in_file"),
    ]
