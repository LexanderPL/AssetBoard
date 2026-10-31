"""Tax kinds, tax payment schedules, the Taxes switch and other taxes (ADR-0022).

Revision ID: 0028_taxes_redesign
Revises: 0027_f41_lineage
Create Date: 2026-10-31 12:00:00.000000

Upgrade:
- ``tax_rules.effect`` becomes ``kind``: ``added`` stays ``added``; ``withheld`` becomes
  ``payer_withheld`` when an active (non-archived) income source it is attached to recorded
  take-home income, otherwise ``self_paid``.
- Each rule gets a default payment schedule from its first attachment (by creation time):
  ``due_day`` → ``payment_day``, offset 0 → ``same_month`` and any other offset →
  ``next_month``, and its payment category. A rule without attachments gets 10, next month
  and no category. Attachment values equal to the rule's defaults become NULL ("use the rule's
  default"); others stay as overrides.
- ``income_sources.record_as`` is dropped.
- ``workspaces.taxes_enabled`` is on for workspaces that have a tax rule, off otherwise.
- ``scheduled_items.is_tax`` (off).

Pending income occurrences are recalculated by the startup catch-up (``resync_pending_income``)
the next time the application starts.

Downgrade (lossy where the old model cannot express the new one):
- ``kind`` → ``effect`` (``added`` → ``added``, the others → ``withheld``).
- ``record_as`` comes back as ``take_home`` for sources with a payer-withheld tax attached and
  ``cash_received`` otherwise; the old ``net`` mode is never restored.
- NULL attachment values are filled from the rule's defaults; ``after_quarter`` becomes a
  one-month offset. An attachment with no category anywhere gets the workspace's first
  non-transfer expense category, or is deleted when there is none.
- ``taxes_enabled``, ``is_tax`` and the rules' schedule columns are dropped.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0028_taxes_redesign"
down_revision = "0027_f41_lineage"
branch_labels = None
depends_on = None

_rules = sa.table(
    "tax_rules",
    sa.column("id", sa.Uuid()),
    sa.column("workspace_id", sa.Uuid()),
    sa.column("effect", sa.String()),
    sa.column("kind", sa.String()),
    sa.column("payment_day", sa.Integer()),
    sa.column("payment_timing", sa.String()),
    sa.column("payment_category_id", sa.Uuid()),
)
_attachments = sa.table(
    "income_source_tax_rules",
    sa.column("id", sa.Uuid()),
    sa.column("workspace_id", sa.Uuid()),
    sa.column("income_source_id", sa.Uuid()),
    sa.column("tax_rule_id", sa.Uuid()),
    sa.column("payment_category_id", sa.Uuid()),
    sa.column("due_day", sa.Integer()),
    sa.column("due_month_offset", sa.Integer()),
    sa.column("created_at", sa.DateTime()),
)
_sources = sa.table(
    "income_sources",
    sa.column("id", sa.Uuid()),
    sa.column("record_as", sa.String()),
    sa.column("archived_at", sa.DateTime()),
)
_workspaces = sa.table(
    "workspaces", sa.column("id", sa.Uuid()), sa.column("taxes_enabled", sa.Boolean())
)
_categories = sa.table(
    "categories",
    sa.column("id", sa.Uuid()),
    sa.column("workspace_id", sa.Uuid()),
    sa.column("kind", sa.String()),
    sa.column("treatment", sa.String()),
    sa.column("archived_at", sa.DateTime()),
    sa.column("sort_order", sa.Integer()),
)
_TIMING_OFFSET = {"same_month": 0, "next_month": 1, "after_quarter": 1}


def upgrade() -> None:
    connection = op.get_bind()
    with op.batch_alter_table("tax_rules", schema=None) as batch_op:
        batch_op.add_column(sa.Column("kind", sa.String(length=16), nullable=True))
        batch_op.add_column(
            sa.Column("payment_day", sa.Integer(), nullable=False, server_default="10")
        )
        batch_op.add_column(
            sa.Column(
                "payment_timing",
                sa.String(length=16),
                nullable=False,
                server_default="next_month",
            )
        )
        batch_op.add_column(sa.Column("payment_category_id", sa.Uuid(), nullable=True))

    take_home_rules = {
        rule_id
        for (rule_id,) in connection.execute(
            sa.select(_attachments.c.tax_rule_id)
            .join(_sources, _sources.c.id == _attachments.c.income_source_id)
            .where(_sources.c.record_as == "take_home", _sources.c.archived_at.is_(None))
        )
    }
    for rule_id, effect in connection.execute(sa.select(_rules.c.id, _rules.c.effect)).all():
        if effect == "added":
            kind = "added"
        else:
            kind = "payer_withheld" if rule_id in take_home_rules else "self_paid"
        first = connection.execute(
            sa.select(
                _attachments.c.due_day,
                _attachments.c.due_month_offset,
                _attachments.c.payment_category_id,
            )
            .where(_attachments.c.tax_rule_id == rule_id)
            .order_by(_attachments.c.created_at, _attachments.c.id)
            .limit(1)
        ).first()
        if first is None:
            day, timing, category = 10, "next_month", None
        else:
            day = first.due_day
            timing = "same_month" if first.due_month_offset == 0 else "next_month"
            category = first.payment_category_id
        connection.execute(
            _rules.update()
            .where(_rules.c.id == rule_id)
            .values(kind=kind, payment_day=day, payment_timing=timing, payment_category_id=category)
        )

    with op.batch_alter_table("tax_rules", schema=None) as batch_op:
        # 0018 created these with doubled names (explicit name plus naming convention).
        batch_op.drop_constraint(op.f("ck_tax_rules_ck_tax_rules_added_base_valid"), type_="check")
        batch_op.drop_constraint(op.f("ck_tax_rules_ck_tax_rules_effect_valid"), type_="check")
        batch_op.drop_column("effect")
        batch_op.alter_column("kind", existing_type=sa.String(length=16), nullable=False)
        batch_op.create_check_constraint(
            op.f("ck_tax_rules_kind_valid"), "kind IN ('added', 'self_paid', 'payer_withheld')"
        )
        batch_op.create_check_constraint(
            op.f("ck_tax_rules_added_base_valid"), "kind != 'added' OR base = 'net'"
        )
        batch_op.create_check_constraint(
            op.f("ck_tax_rules_payment_day_valid"), "payment_day BETWEEN 1 AND 31"
        )
        batch_op.create_check_constraint(
            op.f("ck_tax_rules_payment_timing_valid"),
            "payment_timing IN ('next_month', 'same_month', 'after_quarter')",
        )
        batch_op.create_foreign_key(
            "fk_tax_rules_workspace_payment_category",
            "categories",
            ["workspace_id", "payment_category_id"],
            ["workspace_id", "id"],
        )

    with op.batch_alter_table("income_source_tax_rules", schema=None) as batch_op:
        batch_op.alter_column("payment_category_id", existing_type=sa.Uuid(), nullable=True)
        batch_op.alter_column("due_day", existing_type=sa.Integer(), nullable=True)
        batch_op.alter_column("due_month_offset", existing_type=sa.Integer(), nullable=True)
    rules = {
        row.id: row
        for row in connection.execute(
            sa.select(
                _rules.c.id,
                _rules.c.payment_day,
                _rules.c.payment_timing,
                _rules.c.payment_category_id,
            )
        )
    }
    for row in connection.execute(
        sa.select(
            _attachments.c.id,
            _attachments.c.tax_rule_id,
            _attachments.c.due_day,
            _attachments.c.due_month_offset,
            _attachments.c.payment_category_id,
        )
    ).all():
        rule = rules[row.tax_rule_id]
        changes: dict[str, object] = {}
        if row.due_day == rule.payment_day:
            changes["due_day"] = None
        if row.due_month_offset == _TIMING_OFFSET[rule.payment_timing]:
            changes["due_month_offset"] = None
        if row.payment_category_id == rule.payment_category_id:
            changes["payment_category_id"] = None
        if changes:
            connection.execute(
                _attachments.update().where(_attachments.c.id == row.id).values(**changes)
            )

    with op.batch_alter_table("income_sources", schema=None) as batch_op:
        batch_op.drop_constraint(
            op.f("ck_income_sources_ck_income_sources_record_as_valid"), type_="check"
        )
        batch_op.drop_column("record_as")

    with op.batch_alter_table("workspaces", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("taxes_enabled", sa.Boolean(), nullable=False, server_default=sa.false())
        )
    connection.execute(
        _workspaces.update()
        .where(_workspaces.c.id.in_(sa.select(_rules.c.workspace_id)))
        .values(taxes_enabled=True)
    )

    with op.batch_alter_table("scheduled_items", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("is_tax", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    connection = op.get_bind()
    with op.batch_alter_table("scheduled_items", schema=None) as batch_op:
        batch_op.drop_column("is_tax")
    with op.batch_alter_table("workspaces", schema=None) as batch_op:
        batch_op.drop_column("taxes_enabled")

    with op.batch_alter_table("income_sources", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "record_as",
                sa.String(length=24),
                nullable=False,
                server_default="cash_received",
            )
        )
    withheld_sources = sa.select(_attachments.c.income_source_id).join(
        _rules, _rules.c.id == _attachments.c.tax_rule_id
    )
    connection.execute(
        _sources.update()
        .where(_sources.c.id.in_(withheld_sources.where(_rules.c.kind == "payer_withheld")))
        .values(record_as="take_home")
    )
    with op.batch_alter_table("income_sources", schema=None) as batch_op:
        batch_op.create_check_constraint(
            op.f("ck_income_sources_ck_income_sources_record_as_valid"),
            "record_as IN ('cash_received', 'net', 'take_home')",
        )

    rules = {
        row.id: row
        for row in connection.execute(
            sa.select(
                _rules.c.id,
                _rules.c.payment_day,
                _rules.c.payment_timing,
                _rules.c.payment_category_id,
            )
        )
    }
    for row in connection.execute(
        sa.select(
            _attachments.c.id,
            _attachments.c.workspace_id,
            _attachments.c.tax_rule_id,
            _attachments.c.due_day,
            _attachments.c.due_month_offset,
            _attachments.c.payment_category_id,
        )
    ).all():
        rule = rules[row.tax_rule_id]
        category = row.payment_category_id or rule.payment_category_id
        if category is None:
            category = connection.execute(
                sa.select(_categories.c.id)
                .where(
                    _categories.c.workspace_id == row.workspace_id,
                    _categories.c.kind == "expense",
                    _categories.c.treatment != "transfer",
                )
                .order_by(_categories.c.archived_at.is_not(None), _categories.c.sort_order)
                .limit(1)
            ).scalar()
        if category is None:
            connection.execute(_attachments.delete().where(_attachments.c.id == row.id))
            continue
        connection.execute(
            _attachments.update()
            .where(_attachments.c.id == row.id)
            .values(
                due_day=row.due_day if row.due_day is not None else rule.payment_day,
                due_month_offset=row.due_month_offset
                if row.due_month_offset is not None
                else _TIMING_OFFSET[rule.payment_timing],
                payment_category_id=category,
            )
        )
    with op.batch_alter_table("income_source_tax_rules", schema=None) as batch_op:
        batch_op.alter_column("payment_category_id", existing_type=sa.Uuid(), nullable=False)
        batch_op.alter_column("due_day", existing_type=sa.Integer(), nullable=False)
        batch_op.alter_column("due_month_offset", existing_type=sa.Integer(), nullable=False)

    with op.batch_alter_table("tax_rules", schema=None) as batch_op:
        batch_op.add_column(sa.Column("effect", sa.String(length=16), nullable=True))
    connection.execute(
        _rules.update().values(
            effect=sa.case((_rules.c.kind == "added", "added"), else_="withheld")
        )
    )
    with op.batch_alter_table("tax_rules", schema=None) as batch_op:
        batch_op.drop_constraint("fk_tax_rules_workspace_payment_category", type_="foreignkey")
        batch_op.drop_constraint(op.f("ck_tax_rules_payment_timing_valid"), type_="check")
        batch_op.drop_constraint(op.f("ck_tax_rules_payment_day_valid"), type_="check")
        batch_op.drop_constraint(op.f("ck_tax_rules_added_base_valid"), type_="check")
        batch_op.drop_constraint(op.f("ck_tax_rules_kind_valid"), type_="check")
        batch_op.drop_column("payment_category_id")
        batch_op.drop_column("payment_timing")
        batch_op.drop_column("payment_day")
        batch_op.drop_column("kind")
        batch_op.alter_column("effect", existing_type=sa.String(length=16), nullable=False)
        batch_op.create_check_constraint(
            op.f("ck_tax_rules_ck_tax_rules_effect_valid"), "effect IN ('added', 'withheld')"
        )
        batch_op.create_check_constraint(
            op.f("ck_tax_rules_ck_tax_rules_added_base_valid"),
            "effect != 'added' OR base = 'net'",
        )
