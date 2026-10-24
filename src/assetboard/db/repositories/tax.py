from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import exists, select

from assetboard.db.models.tax import TaxRule, TaxRuleVersion
from assetboard.db.repositories.base import ScopedRepository


class TaxRuleRepository(ScopedRepository[TaxRule]):
    model = TaxRule

    def create(self, **values: object) -> TaxRule:
        return self.add(TaxRule(**values))

    def ordered(self, *, include_archived: bool = True) -> list[TaxRule]:
        statement = self._scoped_select()
        if not include_archived:
            statement = statement.where(TaxRule.archived_at.is_(None))
        return list(self._session.scalars(statement.order_by(TaxRule.name, TaxRule.id)))

    def effective_for_all(self, day: date) -> list[tuple[TaxRule, TaxRuleVersion]]:
        rows = self._session.execute(
            select(TaxRule, TaxRuleVersion)
            .join(
                TaxRuleVersion,
                (TaxRuleVersion.workspace_id == TaxRule.workspace_id)
                & (TaxRuleVersion.tax_rule_id == TaxRule.id),
            )
            .where(
                TaxRule.workspace_id == self._workspace_id,
                TaxRule.archived_at.is_(None),
                TaxRuleVersion.workspace_id == self._workspace_id,
                TaxRuleVersion.effective_from <= day,
            )
            .order_by(TaxRule.name, TaxRule.id, TaxRuleVersion.effective_from.desc())
        )
        result: list[tuple[TaxRule, TaxRuleVersion]] = []
        seen: set[UUID] = set()
        for rule, version in rows:
            if rule.id not in seen:
                result.append((rule, version))
                seen.add(rule.id)
        return result


class TaxRuleVersionRepository(ScopedRepository[TaxRuleVersion]):
    model = TaxRuleVersion
    unsupported = frozenset({"update", "delete"})

    def create(self, **values: object) -> TaxRuleVersion:
        return self.add(TaxRuleVersion(**values))

    def for_rule(self, tax_rule_id: UUID) -> list[TaxRuleVersion]:
        statement = self._scoped_select().where(TaxRuleVersion.tax_rule_id == tax_rule_id)
        return list(self._session.scalars(statement.order_by(TaxRuleVersion.effective_from)))

    def all_for_rules(self) -> list[TaxRuleVersion]:
        statement = self._scoped_select().order_by(
            TaxRuleVersion.tax_rule_id, TaxRuleVersion.effective_from
        )
        return list(self._session.scalars(statement))

    def any_fixed_amount(self) -> bool:
        return bool(
            self._session.execute(
                select(
                    exists().where(
                        TaxRuleVersion.workspace_id == self._workspace_id,
                        TaxRuleVersion.mode == "fixed",
                    )
                )
            ).scalar_one()
        )

    def effective(self, tax_rule_id: UUID, day: date) -> TaxRuleVersion | None:
        statement = (
            self._scoped_select()
            .where(
                TaxRuleVersion.tax_rule_id == tax_rule_id,
                TaxRuleVersion.effective_from <= day,
            )
            .order_by(TaxRuleVersion.effective_from.desc())
            .limit(1)
        )
        return self._session.scalars(statement).first()
