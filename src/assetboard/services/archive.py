"""Workspace archive export (F4.1)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib.metadata import version

from sqlalchemy.orm import Session, sessionmaker

from assetboard import branding
from assetboard.db.engine import run_immediate
from assetboard.db.models.ledger import Category
from assetboard.db.repositories import (
    AuditEventRepository,
    AuditFilter,
    BudgetLineRepository,
    CategoryRepository,
    CurrentWorkspaceRepository,
    IncomeSourceRepository,
    IncomeSourceTaxRuleRepository,
    MonthStateRepository,
    ScheduledItemRepository,
    ScheduledOccurrenceRepository,
    TaxRuleRepository,
    TaxRuleVersionRepository,
    TransactionRepository,
    WorkDayRepository,
    WorkLogRepository,
)
from assetboard.domain.archive import (
    ArchiveRows,
    BudgetLineRow,
    CategoryRow,
    MonthRow,
    TransactionRow,
    lineage_id,
    timestamp_text,
    write_archive,
)
from assetboard.domain.categories import SEED, clean_name
from assetboard.domain.ledger_csv import plain_amount
from assetboard.i18n import gettext
from assetboard.i18n.locale import SUPPORTED_LOCALES
from assetboard.services.categories import usage_by_category
from assetboard.services.context import Context, Role, require_role, require_role_now

ARCHIVE_INCLUDED_MODELS = frozenset({"Category", "BudgetLine", "Transaction", "MonthState"})
ARCHIVE_EXCLUDED_MODELS = {
    "Membership": "workspace membership is instance access control",
    "Invitation": "invitations are not financial workspace data",
    "AuditEvent": "audit history is intentionally not portable",
    "ImportBatch": "temporary import state and user reports are not portable",
    "ImportCategoryMapping": (
        "remembered import choices are workspace import preferences, not ledger data"
    ),
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


# A restore target must hold no workspace data. The untouched suggested set is the one
# exception: it is deleted by the restore itself. Later milestones add their tables here, as
# ``services.workspaces.BASE_CURRENCY_LOCKS`` does for amounts.
def _expected_suggested_categories(locale: str) -> set[tuple[str, str, str, str, int]]:
    positions: dict[str, int] = {}
    expected: set[tuple[str, str, str, str, int]] = set()
    for seed in SEED:
        name = clean_name(gettext(seed.name, locale))
        kind = seed.kind.value
        position = positions.get(kind, 0)
        positions[kind] = position + 1
        expected.add((name, kind, seed.treatment.value, seed.color, position))
    return expected


def suggested_categories_to_remove(session: Session, ctx: Context) -> list[Category]:
    """The untouched suggested categories a restore may delete before writing its own.

    "Untouched" means the exact suggested set (in a supported locale), unedited and unreferenced,
    with the automatic seed audit event proving it came from workspace creation. Anything else
    makes the workspace non-empty.
    """
    categories = CategoryRepository(session, ctx).ordered()
    if len(categories) != len(SEED):
        return []
    if any(category.archived_at is not None for category in categories):
        return []
    if any(category.updated_at != category.created_at for category in categories):
        return []
    seed_events = AuditEventRepository(session, ctx).page(
        AuditFilter(actions={"category.seeded"}), limit=100
    )
    automatic = [event for event in seed_events if event.data.get("automatic") is True]
    if not automatic:
        return []
    seeded_at = automatic[0].ts
    # A later manual "Add suggested categories" makes the set a user action, not the untouched
    # automatic seed, even if its rows happen to look the same.
    if any(
        event.ts > seeded_at for event in seed_events if event.data.get("automatic") is not True
    ):
        return []
    # Seeding creates the rows and then the audit event in one transaction; a real clock may
    # tick between the two, so allow a small window instead of exact equality.
    if any(
        not (seeded_at <= category.created_at <= seeded_at + timedelta(seconds=5))
        for category in categories
    ):
        return []
    usage = usage_by_category(session, ctx)
    if any(usage.get(category.id) is not None for category in categories):
        return []
    actual = {
        (category.name, category.kind, category.treatment, category.color, category.sort_order)
        for category in categories
    }
    if not any(actual == _expected_suggested_categories(locale) for locale in SUPPORTED_LOCALES):
        return []
    return categories


def _has_unexpected_categories(session: Session, ctx: Context) -> bool:
    categories = CategoryRepository(session, ctx).list()
    if not categories:
        return False
    return not suggested_categories_to_remove(session, ctx)


def _has_transactions(session: Session, ctx: Context) -> bool:
    return TransactionRepository(session, ctx).any_exists()


def _has_budget_lines(session: Session, ctx: Context) -> bool:
    return BudgetLineRepository(session, ctx).any_exists()


def _has_month_states(session: Session, ctx: Context) -> bool:
    return MonthStateRepository(session, ctx).any_exists()


def _has_scheduled_items(session: Session, ctx: Context) -> bool:
    return ScheduledItemRepository(session, ctx).any_exists()


def _has_scheduled_occurrences(session: Session, ctx: Context) -> bool:
    return ScheduledOccurrenceRepository(session, ctx).any_exists()


def _has_income_sources(session: Session, ctx: Context) -> bool:
    return IncomeSourceRepository(session, ctx).any_exists()


def _has_income_source_tax_rules(session: Session, ctx: Context) -> bool:
    return IncomeSourceTaxRuleRepository(session, ctx).any_exists()


def _has_work_logs(session: Session, ctx: Context) -> bool:
    return WorkLogRepository(session, ctx).any_exists()


def _has_work_days(session: Session, ctx: Context) -> bool:
    return WorkDayRepository(session, ctx).any_exists()


def _has_tax_rules(session: Session, ctx: Context) -> bool:
    return TaxRuleRepository(session, ctx).any_exists()


def _has_tax_rule_versions(session: Session, ctx: Context) -> bool:
    return TaxRuleVersionRepository(session, ctx).any_exists()


EMPTINESS_CHECKS = (
    _has_unexpected_categories,
    _has_transactions,
    _has_budget_lines,
    _has_month_states,
    _has_scheduled_items,
    _has_scheduled_occurrences,
    _has_income_sources,
    _has_income_source_tax_rules,
    _has_work_logs,
    _has_work_days,
    _has_tax_rules,
    _has_tax_rule_versions,
)


def workspace_is_empty(session: Session, ctx: Context) -> bool:
    """Whether a restore may target this workspace (untouched suggested categories aside)."""
    return not any(check(session, ctx) for check in EMPTINESS_CHECKS)


def restore_available(factory: sessionmaker[Session], ctx: Context) -> bool:
    """Whether the data page should offer restore mode (owners only, empty workspace)."""
    if ctx.role is not Role.OWNER:
        return False
    with factory() as session:
        return workspace_is_empty(session, ctx)


@dataclass(frozen=True, slots=True)
class WorkspaceArchive:
    content: bytes
    filename: str


def export_workspace(factory: sessionmaker[Session], ctx: Context) -> WorkspaceArchive:
    """Export the complete ledger and record a content-free audit event."""
    require_role(ctx, Role.EDITOR)

    def export(session: Session) -> tuple[ArchiveRows, datetime, str]:
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
                lineage_id(row.id, row.import_key),  # the exported id, stable across restores
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
                archived_at=timestamp_text(category.archived_at),
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
                deleted_at=timestamp_text(row.deleted_at),
            )
            for row in transactions
        )
        month_rows = tuple(
            MonthRow(
                month=row.month.strftime("%Y-%m"),
                closed_at=timestamp_text(row.closed_at),
                plan_started_at=timestamp_text(row.plan_started_at),
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
                "taxes_enabled": workspace.taxes_enabled,
                "upcoming_days": workspace.upcoming_days,
            },
            categories=category_rows,
            budget_lines=budget_rows,
            transactions=transaction_rows,
            months=month_rows,
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
        return rows, exported_at, slug

    rows, exported_at, slug = run_immediate(factory, export)
    # Compress after commit: the rows are a consistent snapshot, and on SQLite the immediate
    # transaction blocks every writer of the instance while it is open.
    archive = write_archive(
        rows,
        app_name=branding.APP_NAME,
        app_version=version("assetboard"),
        exported_at=exported_at,
        base_currency=str(rows.workspace["base_currency"]),
    )
    filename = f"{branding.CLI_NAME}-{slug}-{exported_at.strftime('%Y%m%d-%H%M%S')}Z.zip"
    return WorkspaceArchive(content=archive, filename=filename)
