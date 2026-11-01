"""Deterministic transaction import identity (F4.2)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from typing import Literal
from uuid import UUID

DedupeAction = Literal["create", "duplicate", "conflict"]


@dataclass(frozen=True, slots=True)
class DedupeDecision:
    """``code`` is the stable report code of a skipped row (``None`` for ``create``)."""

    action: DedupeAction
    code: str | None = None


def transaction_content_key(
    *,
    day: date,
    direction: str,
    amount_minor: int,
    currency: str,
    source_category_name_key: str,
    description_key: str,
    occurrence: int,
) -> str:
    if occurrence < 1:
        raise ValueError("occurrence must be positive")
    fields = (
        day.isoformat(),
        direction,
        str(amount_minor),
        currency,
        source_category_name_key,
        description_key,
        str(occurrence),
    )
    digest = hashlib.sha256("\x1f".join(fields).encode("utf-8")).hexdigest()
    return f"c1:{digest}"


def lineage_import_key(row_id: UUID) -> str:
    return f"id:{row_id}"


def matches_lineage(
    *,
    source_id: UUID,
    local_id: UUID,
    import_key: str | None,
) -> bool:
    return source_id == local_id or import_key == lineage_import_key(source_id)


def classify_import_match(
    *,
    identity_found: bool,
    values_match: bool,
    soft_deleted: bool = False,
) -> DedupeDecision:
    """A deleted row stays deleted whatever its values (it is never brought back)."""
    if not identity_found:
        return DedupeDecision("create")
    if soft_deleted:
        return DedupeDecision("duplicate", "imported_before_deleted")
    if not values_match:
        return DedupeDecision("conflict", "changed_since_export")
    return DedupeDecision("duplicate", "already_imported")


def classify_budget_line(existing_minor: int | None, incoming_minor: int) -> DedupeDecision:
    if existing_minor is None:
        return DedupeDecision("create")
    if existing_minor == incoming_minor:
        return DedupeDecision("duplicate", "planned_value_exists")
    return DedupeDecision("conflict", "planned_value_differs")
