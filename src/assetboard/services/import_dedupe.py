"""Shared dedupe for import adapters (F4.2): lineage ids, content keys and budget lines.

Adapters (F4.3, F4.4) resolve each file row to its target category first, then ask these
helpers which rows to create. Both helpers only read, through the scoped repositories, so the
same call serves the dry-run plan and the re-plan inside the apply transaction.

* A transaction row with an ``id`` is matched by lineage: a workspace row with that id or with
  ``import_key = id:<id>`` (soft-deleted rows included). A deleted match is a duplicate
  ("imported before and deleted", never brought back); a live match with other values is a
  conflict ("changed since the export"). The same id twice in one file: the later row is a
  duplicate.
* A row without an ``id`` gets the content key ``c1:`` (``domain.import_keys``) with its
  occurrence number among identical rows of the file, so two identical rows are both created
  and a later file with one more identical row creates only that row.
* Budget lines dedupe on ``(month, category)``: the same amount is a duplicate, another amount
  a conflict (the stored value is kept). A later row of the file for the same pair is compared
  with the earlier one the same way.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from sqlalchemy.orm import Session

from assetboard.db.models.ledger import Transaction
from assetboard.db.repositories import BudgetLineRepository, TransactionRepository
from assetboard.db.repositories.base import WorkspaceScope
from assetboard.domain.import_keys import (
    DedupeAction,
    classify_budget_line,
    classify_import_match,
    lineage_import_key,
    transaction_content_key,
)
from assetboard.domain.months import YearMonth


@dataclass(frozen=True, slots=True)
class IncomingTransaction:
    """One valid file row, already resolved to a category of the workspace."""

    line: int
    source_id: UUID | None
    day: date
    direction: str
    amount_minor: int
    currency: str
    category_id: UUID
    source_category_name_key: str  # the category name key as in the file, before mapping
    description: str
    description_key: str


@dataclass(frozen=True, slots=True)
class TransactionMatch:
    row: IncomingTransaction
    import_key: str
    action: DedupeAction
    code: str | None  # report code of a skipped row; None for "create"
    existing_id: UUID | None


@dataclass(frozen=True, slots=True)
class IncomingBudgetLine:
    line: int
    month: YearMonth
    category_id: UUID
    planned_minor: int


@dataclass(frozen=True, slots=True)
class BudgetLineMatch:
    row: IncomingBudgetLine
    action: DedupeAction
    code: str | None
    existing_id: UUID | None


def _same_values(existing: Transaction, row: IncomingTransaction) -> bool:
    return (
        existing.date == row.day
        and existing.direction == row.direction
        and existing.amount_minor == row.amount_minor
        and existing.currency == row.currency
        and existing.category_id == row.category_id
        and existing.description == row.description
    )


def match_transactions(
    session: Session, scope: WorkspaceScope, rows: Sequence[IncomingTransaction]
) -> list[TransactionMatch]:
    """Classify ``rows`` (in file order) as create, duplicate or conflict."""
    repository = TransactionRepository(session, scope)
    occurrences: dict[tuple[object, ...], int] = {}
    keys: list[str] = []
    for row in rows:
        if row.source_id is not None:
            keys.append(lineage_import_key(row.source_id))
            continue
        identity = (
            row.day,
            row.direction,
            row.amount_minor,
            row.currency,
            row.source_category_name_key,
            row.description_key,
        )
        occurrences[identity] = occurrences.get(identity, 0) + 1
        keys.append(
            transaction_content_key(
                day=row.day,
                direction=row.direction,
                amount_minor=row.amount_minor,
                currency=row.currency,
                source_category_name_key=row.source_category_name_key,
                description_key=row.description_key,
                occurrence=occurrences[identity],
            )
        )

    source_ids = {row.source_id for row in rows if row.source_id is not None}
    by_lineage: dict[str, Transaction] = {}
    for existing in repository.by_lineage_ids(source_ids):
        if existing.import_key is not None and existing.import_key.startswith("id:"):
            by_lineage.setdefault(existing.import_key, existing)
        if existing.id in source_ids:
            by_lineage.setdefault(lineage_import_key(existing.id), existing)
    content_keys = [key for key in keys if key.startswith("c1:")]
    by_content = {
        existing.import_key: existing
        for existing in repository.by_import_keys(content_keys)
        if existing.import_key is not None
    }

    matches: list[TransactionMatch] = []
    seen: set[str] = set()
    for row, key in zip(rows, keys, strict=True):
        if key in seen:
            # The same lineage id earlier in this file (content keys never repeat).
            matches.append(TransactionMatch(row, key, "duplicate", "duplicate_in_file", None))
            continue
        seen.add(key)
        existing = by_lineage.get(key) if row.source_id is not None else by_content.get(key)
        decision = classify_import_match(
            identity_found=existing is not None,
            values_match=existing is not None and _same_values(existing, row),
            soft_deleted=existing is not None and existing.deleted_at is not None,
        )
        matches.append(
            TransactionMatch(
                row,
                key,
                decision.action,
                decision.code,
                existing.id if existing is not None else None,
            )
        )
    return matches


def match_budget_lines(
    session: Session, scope: WorkspaceScope, rows: Sequence[IncomingBudgetLine]
) -> list[BudgetLineMatch]:
    """Classify planned values (in file order) against the stored lines."""
    stored = {
        (YearMonth.from_date(line.month), line.category_id): line
        for line in BudgetLineRepository(session, scope).list()
    }
    earlier: dict[tuple[YearMonth, UUID], int] = {}
    matches: list[BudgetLineMatch] = []
    for row in rows:
        pair = (row.month, row.category_id)
        if pair in earlier:
            decision = classify_budget_line(earlier[pair], row.planned_minor)
            code = "duplicate_in_file" if decision.action == "duplicate" else "conflict_in_file"
            matches.append(BudgetLineMatch(row, decision.action, code, None))
            continue
        earlier[pair] = row.planned_minor
        line = stored.get(pair)
        decision = classify_budget_line(
            line.planned_minor if line is not None else None, row.planned_minor
        )
        matches.append(
            BudgetLineMatch(
                row, decision.action, decision.code, line.id if line is not None else None
            )
        )
    return matches
