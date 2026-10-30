"""Workspace archive export (F4.1)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from assetboard import branding
from assetboard.db.engine import run_immediate
from assetboard.db.repositories import (
    AuditEventRepository,
    BudgetLineRepository,
    CategoryRepository,
    CurrentWorkspaceRepository,
    MonthStateRepository,
    TransactionRepository,
)
from assetboard.domain.archive import (
    ArchiveRows,
    BudgetLineRow,
    CategoryRow,
    MonthRow,
    TransactionRow,
    write_archive,
)
from assetboard.domain.ledger_csv import plain_amount
from assetboard.services.context import Context, Role, require_role, require_role_now

ARCHIVE_INCLUDED_MODELS = frozenset({"Category", "BudgetLine", "Transaction", "MonthState"})
ARCHIVE_EXCLUDED_MODELS = {
    "Membership": "workspace membership is instance access control",
    "Invitation": "invitations are not financial workspace data",
    "AuditEvent": "audit history is intentionally not portable",
    "ScheduledItem": "recurring rules require the F3.8 archive extension",
    "ScheduledOccurrence": "occurrence links require the F3.8 archive extension",
    "IncomeSource": "income sources require the F3.8 archive extension",
    "IncomeSourceTaxRule": "income tax links require the F3.8 archive extension",
    "WorkLog": "work logs require the F3.8 archive extension",
    "WorkDay": "work calendars require the F3.8 archive extension",
    "TaxRule": "tax rules require the F3.8 archive extension",
    "TaxRuleVersion": "tax history requires the F3.8 archive extension",
    "NotificationChannel": "notification URLs contain secrets (F3.8/F9.3)",
    "NotificationLog": "notification delivery history is not portable (F3.8)",
}


@dataclass(frozen=True, slots=True)
class WorkspaceArchive:
    content: bytes
    filename: str


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return (
        value.astimezone(UTC)
        .isoformat(timespec="microseconds" if value.microsecond else "seconds")
        .replace("+00:00", "Z")
    )


def export_workspace(factory: sessionmaker[Session], ctx: Context) -> WorkspaceArchive:
    """Export the complete ledger and record a content-free audit event."""
    require_role(ctx, Role.EDITOR)

    def export(session: Session) -> WorkspaceArchive:
        require_role_now(session, ctx, Role.EDITOR)
        workspace_repository = CurrentWorkspaceRepository(session, ctx)
        # Ledger writers take this workspace lock too, keeping the files mutually consistent.
        workspace_repository.lock(exclusive=True)
        workspace = workspace_repository.get()
        categories = CategoryRepository(session, ctx).ordered()
        categories_by_id = {category.id: category for category in categories}
        export_categories = sorted(
            categories,
            key=lambda row: (
                row.kind,
                row.archived_at is not None,
                row.sort_order,
                row.name_key,
                row.id,
            ),
        )
        category_rank = {
            category.id: position for position, category in enumerate(export_categories)
        }

        category_rows = tuple(
            CategoryRow(
                id=str(lineage_id(category.id, category.import_key)),
                kind=category.kind,
                name=category.name,
                treatment=category.treatment,
                color=category.color,
                sort_order=category.sort_order,
                archived_at=_timestamp(category.archived_at),
            )
            for category in export_categories
        )
        budget_lines = sorted(
            BudgetLineRepository(session, ctx).list(),
            key=lambda line: (line.month, category_rank[line.category_id]),
        )
        budget_rows = tuple(
            BudgetLineRow(
                month=line.month.strftime("%Y-%m"),
                kind=categories_by_id[line.category_id].kind,
                category=categories_by_id[line.category_id].name,
                planned=plain_amount(line.planned_minor, line.currency),
                currency=line.currency,
                category_id=str(
                    lineage_id(
                        categories_by_id[line.category_id].id,
                        categories_by_id[line.category_id].import_key,
                    )
                ),
            )
            for line in budget_lines
        )
        transactions = sorted(
            TransactionRepository(session, ctx).list(),
            key=lambda row: (row.date, row.created_at, row.id),
        )
        transaction_rows = tuple(
            TransactionRow(
                date=row.date.isoformat(),
                direction=row.direction,
                amount=plain_amount(row.amount_minor, row.currency),
                currency=row.currency,
                category=categories_by_id[row.category_id].name,
                description=row.description,
                category_id=str(
                    lineage_id(
                        categories_by_id[row.category_id].id,
                        categories_by_id[row.category_id].import_key,
                    )
                ),
                id=str(lineage_id(row.id, row.import_key)),
                deleted_at=_timestamp(row.deleted_at),
            )
            for row in transactions
        )
        month_rows = tuple(
            MonthRow(
                month=row.month.strftime("%Y-%m"),
                closed_at=_timestamp(row.closed_at),
                plan_started_at=_timestamp(row.plan_started_at),
            )
            for row in sorted(MonthStateRepository(session, ctx).list(), key=lambda row: row.month)
        )
        exported_at = ctx.clock.now().astimezone(UTC)
        rows = ArchiveRows(
            workspace={
                "name": workspace.name,
                "base_currency": workspace.base_currency,
                "country_code": workspace.country_code,
                "timezone": workspace.timezone,
                "locale": workspace.locale,
                "auto_copy_plan": workspace.auto_copy_plan,
            },
            categories=category_rows,
            budget_lines=budget_rows,
            transactions=transaction_rows,
            months=month_rows,
        )
        archive = write_archive(
            rows,
            app_name=branding.APP_NAME,
            app_version=version("assetboard"),
            exported_at=exported_at,
            base_currency=workspace.base_currency,
        )
        counts = {
            "categories": len(category_rows),
            "budget_lines": len(budget_rows),
            "transactions": len(transaction_rows),
            "months": len(month_rows),
        }
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action="workspace.exported",
            entity_type="workspace",
            entity_id=str(ctx.workspace.id),
            ts=exported_at,
            data=counts,
        )
        slug = re.sub(r"[^a-z0-9]+", "-", workspace.name.lower()).strip("-") or "workspace"
        filename = f"{branding.CLI_NAME}-{slug}-{exported_at.strftime('%Y%m%d-%H%M%S')}Z.zip"
        return WorkspaceArchive(content=archive, filename=filename)

    return run_immediate(factory, export)


def lineage_id(row_id: UUID, import_key: str | None) -> UUID:
    """Preserve origin IDs across restore/export cycles; content keys use the local ID."""
    return UUID(import_key[3:]) if import_key and import_key.startswith("id:") else row_id
