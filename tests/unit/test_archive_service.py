"""Full exports through real scoped repositories, with synthetic tenant data."""

from collections.abc import Iterator
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from assetboard.db.models.identity import AuditEvent, Membership
from assetboard.db.models.ledger import BudgetLine, Category, MonthState, Transaction
from assetboard.domain.archive import lineage_id, parse_archive
from assetboard.services.archive import export_workspace
from assetboard.services.context import PermissionDenied
from tests.tenant.workspace_db import WorkspaceDb, workspace_db


@pytest.fixture
def team(tmp_path: Path) -> Iterator[WorkspaceDb]:
    for db in workspace_db(tmp_path):
        db.add_member("editor", "editor")
        db.add_member("viewer", "viewer")
        yield db


def test_export_complete_scoped_deterministic_and_lineage(team: WorkspaceDb) -> None:
    origin_category, origin_transaction = uuid4(), uuid4()
    other = team.add_workspace("Private")
    with team.factory.begin() as session:
        categories = []
        for workspace_id in (team.workspace_id, other):
            category = Category(
                workspace_id=workspace_id,
                name="Food",
                name_key="food",
                kind="expense",
                treatment="regular",
                color="#16a34a",
                sort_order=0,
                archived_at=team.clock.now(),
                import_key=f"id:{origin_category}",
            )
            session.add(category)
            session.flush()
            categories.append(category.id)
            session.add(
                Transaction(
                    workspace_id=workspace_id,
                    direction="expense",
                    date=date(2026, 2, 3),
                    amount_minor=1234,
                    currency="EUR",
                    category_id=category.id,
                    description="Bakery",
                    description_key="bakery",
                    source="import",
                    import_key=f"id:{origin_transaction}",
                    deleted_at=team.clock.now(),
                )
            )
            session.add(
                BudgetLine(
                    workspace_id=workspace_id,
                    month=date(2026, 2, 1),
                    category_id=category.id,
                    planned_minor=5000,
                    currency="EUR",
                )
            )
            session.add(
                MonthState(
                    workspace_id=workspace_id,
                    month=date(2026, 2, 1),
                    closed_at=team.clock.now(),
                    plan_started_at=team.clock.now(),
                )
            )
    ctx = team.ctx("editor", "editor")
    result = export_workspace(team.factory, ctx)
    assert result.filename == "assetboard-home-20260301-120000Z.zip"
    assert result.content == export_workspace(team.factory, ctx).content
    parsed, errors = parse_archive(result.content)
    assert not errors and parsed is not None
    rows = parsed.rows
    assert (
        len(rows.categories)
        == len(rows.transactions)
        == len(rows.months)
        == len(rows.budget_lines)
        == 1
    )
    assert rows.categories[0].id == str(origin_category)
    assert rows.categories[0].archived_at == "2026-03-01T12:00:00Z"
    assert rows.transactions[0].id == str(origin_transaction)
    assert rows.transactions[0].category_id == str(origin_category)
    assert rows.transactions[0].deleted_at == "2026-03-01T12:00:00Z"
    assert rows.transactions[0].amount == "12.34"
    assert rows.budget_lines[0].category_id == str(origin_category)
    assert rows.budget_lines[0].planned == "50.00"
    assert rows.months[0].closed_at == rows.months[0].plan_started_at == "2026-03-01T12:00:00Z"
    assert rows.workspace["name"] == "Home"
    with team.factory() as session:
        events = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "workspace.exported")
        ).all()
        assert len(events) == 2
        assert all(event.workspace_id == team.workspace_id for event in events)
        assert events[0].data == {
            "categories": 1,
            "transactions": 1,
            "budget_lines": 1,
            "months": 1,
        }
    # Uniqueness is per workspace, including deleted/archived rows; NULLs remain allowed.
    with pytest.raises(IntegrityError), team.factory.begin() as session:
        session.add(
            Category(
                workspace_id=team.workspace_id,
                name="Duplicate",
                name_key="duplicate",
                kind="income",
                treatment="regular",
                color="#16a34a",
                sort_order=0,
                import_key=f"id:{origin_category}",
            )
        )


def test_export_refuses_viewer_and_stale_role(team: WorkspaceDb) -> None:
    with pytest.raises(PermissionDenied):
        export_workspace(team.factory, team.ctx("viewer", "viewer"))
    ctx = team.ctx("editor", "editor")
    with team.factory.begin() as session:
        member = session.get(Membership, team.memberships["editor"])
        assert member is not None
        member.role = "viewer"
    with pytest.raises(PermissionDenied):
        export_workspace(team.factory, ctx)
    with team.factory() as session:
        assert not session.scalars(select(AuditEvent)).all()


def test_lineage_uses_origin_or_local_id() -> None:
    local, origin = uuid4(), uuid4()
    assert lineage_id(local, f"id:{origin}") == origin
    assert lineage_id(local, "c1:" + "a" * 64) == local
    assert lineage_id(local, None) == local


def test_export_orders_categories_and_entries(team: WorkspaceDb) -> None:
    from datetime import timedelta
    from uuid import UUID

    with team.factory.begin() as session:
        category_ids = []
        # Insert in the opposite order to the archive's kind/archive/order/name ordering.
        for index, (name, kind, archived, order) in enumerate(
            [
                ("Salary", "income", False, 0),
                ("Archived", "expense", True, 0),
                ("Zebra", "expense", False, 1),
                ("Apple", "expense", False, 1),
            ]
        ):
            row = Category(
                workspace_id=team.workspace_id,
                id=UUID(int=index + 1),
                name=name,
                name_key=name.lower(),
                kind=kind,
                treatment="regular",
                color="#16a34a",
                sort_order=order,
                archived_at=team.clock.now() if archived else None,
            )
            session.add(row)
            category_ids.append(row.id)
        session.flush()
        for index in (3, 2, 1):
            session.add(
                Transaction(
                    workspace_id=team.workspace_id,
                    id=UUID(int=index + 10),
                    direction="expense",
                    date=date(2026, 2, 3) if index > 1 else date(2026, 2, 2),
                    amount_minor=index,
                    currency="EUR",
                    category_id=category_ids[-1],
                    description=str(index),
                    description_key=str(index),
                    source="manual",
                    created_at=team.clock.now(),
                )
            )
        for month in (2, 1):
            for category_id in category_ids:
                session.add(
                    BudgetLine(
                        workspace_id=team.workspace_id,
                        month=date(2026, month, 1),
                        category_id=category_id,
                        planned_minor=10,
                        currency="EUR",
                    )
                )
        session.flush()
        # Explicit entry timestamp takes precedence over UUID when dates match.
        transaction = session.get(Transaction, UUID(int=12))
        assert transaction is not None
        transaction.created_at = team.clock.now() + timedelta(seconds=1)
    result = export_workspace(team.factory, team.ctx("editor", "editor"))
    parsed, errors = parse_archive(result.content)
    assert not errors and parsed is not None
    names = ["Apple", "Zebra", "Archived", "Salary"]
    assert [row.name for row in parsed.rows.categories] == names
    assert [row.description for row in parsed.rows.transactions] == ["1", "3", "2"]
    assert [(row.month, row.category) for row in parsed.rows.budget_lines] == [
        (month, name) for month in ("2026-01", "2026-02") for name in names
    ]


def test_export_breaks_category_ties_by_the_exported_id(team: WorkspaceDb) -> None:
    from uuid import UUID

    # Two archived "Old" categories differ only by id; the local ids sort the other way round
    # from the lineage ids, which are what a restore keeps.
    with team.factory.begin() as session:
        for local, origin in ((1, 9), (2, 8)):
            session.add(
                Category(
                    workspace_id=team.workspace_id,
                    id=UUID(int=local),
                    name="Old",
                    name_key="old",
                    kind="expense",
                    treatment="regular",
                    color="#16a34a",
                    sort_order=0,
                    archived_at=team.clock.now(),
                    import_key=f"id:{UUID(int=origin)}",
                )
            )
    result = export_workspace(team.factory, team.ctx("editor", "editor"))
    parsed, errors = parse_archive(result.content)
    assert not errors and parsed is not None
    assert [row.id for row in parsed.rows.categories] == [str(UUID(int=8)), str(UUID(int=9))]


def test_export_locks_workspace_before_reading_ledger(team: WorkspaceDb) -> None:
    from sqlalchemy import event
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.orm import ORMExecuteState, Session
    from sqlalchemy.sql import ClauseElement

    statements: list[str] = []

    def record(state: ORMExecuteState) -> None:
        assert isinstance(state.statement, ClauseElement)
        statements.append(str(state.statement.compile(dialect=postgresql.dialect())))

    event.listen(Session, "do_orm_execute", record)
    try:
        export_workspace(team.factory, team.ctx("editor", "editor"))
    finally:
        event.remove(Session, "do_orm_execute", record)
    lock = next(i for i, sql in enumerate(statements) if "FOR UPDATE" in sql)
    ledger = next(i for i, sql in enumerate(statements) if "FROM categories" in sql)
    assert lock < ledger
    assert "workspaces.id =" in statements[lock]


def test_export_includes_the_taxes_switch_and_upcoming_days(team: WorkspaceDb) -> None:
    from assetboard.db.models.identity import Workspace

    parsed, _ = parse_archive(export_workspace(team.factory, team.ctx("editor", "editor")).content)
    assert parsed is not None
    assert parsed.manifest["format_version"] == "1.1"
    assert parsed.rows.workspace["taxes_enabled"] is False
    assert parsed.rows.workspace["upcoming_days"] is None

    with team.factory.begin() as session:
        workspace = session.get_one(Workspace, team.workspace_id)
        workspace.taxes_enabled = True
        workspace.upcoming_days = 30
    parsed, errors = parse_archive(
        export_workspace(team.factory, team.ctx("editor", "editor")).content
    )
    assert errors == () and parsed is not None and parsed.warnings == ()
    assert parsed.rows.workspace["taxes_enabled"] is True
    assert parsed.rows.workspace["upcoming_days"] == 30
