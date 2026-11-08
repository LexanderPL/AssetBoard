"""Workspace archive export (F4.1)."""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from typing import cast
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from assetboard import branding
from assetboard.db.engine import run_immediate
from assetboard.db.models.ledger import Category
from assetboard.db.models.networth import NetWorthCategory
from assetboard.db.repositories import (
    AuditEventRepository,
    AuditFilter,
    BudgetLineRepository,
    CategoryRepository,
    CurrentWorkspaceRepository,
    IncomeSourceRepository,
    IncomeSourceTaxRuleRepository,
    ItemValuationRepository,
    MonthStateRepository,
    NetWorthCategoryRepository,
    NetWorthItemRepository,
    NetWorthSnapshotRepository,
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
    ItemValuationRow,
    MonthRow,
    NetWorthCategoryRow,
    NetWorthItemRow,
    NetWorthSnapshotRow,
    TransactionRow,
    lineage_id,
    timestamp_text,
    write_archive,
)
from assetboard.domain.categories import SEED, clean_name
from assetboard.domain.ledger_csv import plain_amount
from assetboard.domain.networth import ASSET_SEEDS as NETWORTH_ASSET_SEEDS
from assetboard.domain.networth import LIABILITY_SEED as NETWORTH_LIABILITY_SEED
from assetboard.i18n import gettext
from assetboard.i18n.locale import SUPPORTED_LOCALES
from assetboard.services.categories import usage_by_category
from assetboard.services.context import Context, Role, require_role, require_role_now

ARCHIVE_INCLUDED_MODELS = frozenset(
    {
        "Category",
        "BudgetLine",
        "Transaction",
        "MonthState",
        "NetWorthCategory",
        "NetWorthItem",
        "ItemValuation",
        "NetWorthSnapshot",
    }
)
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
    "AssetClass": "asset classes require the F6.9 archive extension",
    "Instrument": "instruments require the F6.9 archive extension",
    "InstrumentAllocation": "allocation weights require the F6.9 archive extension",
    "InvestmentTransaction": "investment transactions require the F6.9 archive extension",
    "BondTerms": "bond terms require the F6.9 archive extension",
    "PricePoint": "manual prices require the F6.9 archive extension",
    "FxRate": "FX rates require the F6.9 archive extension",
    "PriceSourceConfig": "provider settings contain secrets (F6.9/F9.3)",
    "ProviderError": "provider error logs are operational and not portable",
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


def _expected_suggested_networth_categories(
    locale: str,
) -> list[set[tuple[str, str, str, int]]]:
    """Every shipped suggested set, so an old untouched seed still counts as empty."""
    candidates: list[set[tuple[str, str, str, int]]] = []
    for asset_seed in NETWORTH_ASSET_SEEDS:
        positions: dict[str, int] = {}
        expected: set[tuple[str, str, str, int]] = set()
        for seed in (*asset_seed, *NETWORTH_LIABILITY_SEED):
            name = clean_name(gettext(seed.name, locale))
            side = seed.side.value
            position = positions.get(side, 0)
            positions[side] = position + 1
            expected.add((name, side, seed.color, position))
        candidates.append(expected)
    return candidates


def suggested_networth_categories_to_remove(
    session: Session, ctx: Context
) -> list[NetWorthCategory]:
    """The untouched suggested net-worth categories a restore may delete.

    Mirrors the ledger rule: exact suggested set in a supported locale, unedited, unused, with
    the automatic ``networth_category.seeded`` event; a later manual seed makes it user data.
    """
    categories = NetWorthCategoryRepository(session, ctx).ordered()
    candidate_sizes = {len(asset) + len(NETWORTH_LIABILITY_SEED) for asset in NETWORTH_ASSET_SEEDS}
    if len(categories) not in candidate_sizes:
        return []
    if any(category.archived_at is not None for category in categories):
        return []
    if any(category.updated_at != category.created_at for category in categories):
        return []
    if (
        NetWorthItemRepository(session, ctx).any_exists()
        or ItemValuationRepository(session, ctx).any_exists()
        or NetWorthSnapshotRepository(session, ctx).any_exists()
    ):
        return []
    seed_events = AuditEventRepository(session, ctx).page(
        AuditFilter(actions={"networth_category.seeded"}), limit=100
    )
    automatic = [event for event in seed_events if event.data.get("automatic") is True]
    if not automatic:
        return []
    seeded_at = automatic[0].ts
    if any(
        event.ts > seeded_at for event in seed_events if event.data.get("automatic") is not True
    ):
        return []
    if any(
        not (seeded_at <= category.created_at <= seeded_at + timedelta(seconds=5))
        for category in categories
    ):
        return []
    actual = {
        (category.name, category.side, category.color, category.sort_order)
        for category in categories
    }
    if not any(
        actual == expected
        for locale in SUPPORTED_LOCALES
        for expected in _expected_suggested_networth_categories(locale)
    ):
        return []
    return list(categories)


def _has_unexpected_categories(session: Session, ctx: Context) -> bool:
    categories = CategoryRepository(session, ctx).list()
    if not categories:
        return False
    return not suggested_categories_to_remove(session, ctx)


def _has_unexpected_networth_categories(session: Session, ctx: Context) -> bool:
    categories = NetWorthCategoryRepository(session, ctx).list()
    if not categories:
        return False
    return not suggested_networth_categories_to_remove(session, ctx)


def _has_networth_items(session: Session, ctx: Context) -> bool:
    return NetWorthItemRepository(session, ctx).any_exists()


def _has_item_valuations(session: Session, ctx: Context) -> bool:
    return ItemValuationRepository(session, ctx).any_exists()


def _has_networth_snapshots(session: Session, ctx: Context) -> bool:
    return NetWorthSnapshotRepository(session, ctx).any_exists()


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
    _has_unexpected_networth_categories,
    _has_networth_items,
    _has_item_valuations,
    _has_networth_snapshots,
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


def _breakdown_for_export(breakdown: object, category_lineage: Mapping[UUID, UUID]) -> str:
    """Snapshot breakdown JSON with category ids replaced by their exported lineage ids.

    Unknown ids (a removed category, a later format) stay as they are. Compact and sorted so
    two exports of the same data are byte-identical.
    """
    data: dict[str, object] = {}
    if isinstance(breakdown, Mapping):
        source = cast("Mapping[object, object]", breakdown)
        data = {str(key): value for key, value in source.items()}
    categories = data.get("categories")
    if isinstance(categories, list):
        converted: list[object] = []
        for entry in cast("list[object]", categories):
            if not isinstance(entry, Mapping):
                converted.append(entry)
                continue
            row = dict(cast("Mapping[str, object]", entry))
            raw_id = row.get("category_id")
            if isinstance(raw_id, str):
                with contextlib.suppress(ValueError):
                    row["category_id"] = str(category_lineage.get(UUID(raw_id), UUID(raw_id)))
            converted.append(row)
        data["categories"] = converted
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


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
        networth_categories = NetWorthCategoryRepository(session, ctx).ordered()
        networth_category_rank = {
            category.id: position for position, category in enumerate(networth_categories)
        }
        networth_category_lineage = {
            category.id: lineage_id(category.id, category.import_key)
            for category in networth_categories
        }
        networth_category_rows = tuple(
            NetWorthCategoryRow(
                id=str(networth_category_lineage[category.id]),
                side=category.side,
                name=category.name,
                color=category.color,
                sort_order=category.sort_order,
                archived_at=timestamp_text(category.archived_at),
            )
            for category in networth_categories
        )
        networth_items = sorted(
            NetWorthItemRepository(session, ctx).ordered(),
            key=lambda row: (
                row.side,
                networth_category_rank[row.category_id],
                row.name,
                str(lineage_id(row.id, row.import_key)),
            ),
        )
        networth_items_by_id = {item.id: item for item in networth_items}
        networth_item_rank = {item.id: position for position, item in enumerate(networth_items)}
        networth_item_rows = tuple(
            NetWorthItemRow(
                id=str(lineage_id(item.id, item.import_key)),
                side=item.side,
                category=networth_categories[networth_category_rank[item.category_id]].name,
                category_id=str(networth_category_lineage[item.category_id]),
                name=item.name,
                currency=item.currency,
                notes=item.notes,
                acquired_on=item.acquired_on.isoformat() if item.acquired_on else None,
                valuation_mode=item.valuation_mode,
                link_type=item.link_type,
                link_id=str(item.link_id) if item.link_id else None,
                include_in_net_worth="true" if item.include_in_net_worth else "false",
                investable="true" if item.investable else "false",
                archived_at=timestamp_text(item.archived_at),
                estimate_between="true" if item.estimate_between else "false",
            )
            for item in networth_items
        )
        valuations = sorted(
            ItemValuationRepository(session, ctx).for_items(list(networth_items_by_id)),
            key=lambda row: (
                networth_item_rank[row.item_id],
                row.as_of,
                row.created_at,
                str(row.id),
            ),
        )
        item_valuation_rows = tuple(
            ItemValuationRow(
                id=str(lineage_id(row.id, row.import_key)),
                item_id=str(
                    lineage_id(
                        networth_items_by_id[row.item_id].id,
                        networth_items_by_id[row.item_id].import_key,
                    )
                ),
                item=networth_items_by_id[row.item_id].name,
                as_of=row.as_of.isoformat(),
                value=plain_amount(row.value_minor, row.currency),
                currency=row.currency,
                note=row.note,
                created_at=timestamp_text(row.created_at),
            )
            for row in valuations
        )
        snapshots = sorted(
            NetWorthSnapshotRepository(session, ctx).for_range(None),
            key=lambda row: (row.as_of, row.source, row.created_at, str(row.id)),
        )
        networth_snapshot_rows = tuple(
            NetWorthSnapshotRow(
                id=str(lineage_id(row.id, row.import_key)),
                as_of=row.as_of.isoformat(),
                assets=plain_amount(row.assets_minor, row.currency),
                liabilities=plain_amount(row.liabilities_minor, row.currency),
                currency=row.currency,
                breakdown=_breakdown_for_export(row.breakdown, networth_category_lineage),
                source=row.source,
            )
            for row in snapshots
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
            networth_categories=networth_category_rows,
            networth_items=networth_item_rows,
            item_valuations=item_valuation_rows,
            networth_snapshots=networth_snapshot_rows,
        )
        counts = {
            "categories": len(category_rows),
            "budget_lines": len(budget_rows),
            "transactions": len(transaction_rows),
            "months": len(month_rows),
            "networth_categories": len(networth_category_rows),
            "networth_items": len(networth_item_rows),
            "item_valuations": len(item_valuation_rows),
            "networth_snapshots": len(networth_snapshot_rows),
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
