"""FIRE expense exclusions as a category usage source (F8.4).

A scenario's exclusion list points at regular expense categories. While an exclusion exists the
category cannot be deleted (the generic usage rule archives it instead) and cannot become a
non-regular or income category, because that would silently change what the scenario excludes.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from assetboard.db.repositories import FireExpenseExclusionRepository
from assetboard.services.category_usage import CategoryUsage
from assetboard.services.context import Context


def category_usage(session: Session, ctx: Context) -> dict[UUID, CategoryUsage]:
    counts = FireExpenseExclusionRepository(session, ctx).counts_by_category()
    return {
        category_id: CategoryUsage(references=count, requires_regular_expense=True)
        for category_id, count in counts.items()
    }


__all__ = ["category_usage"]
