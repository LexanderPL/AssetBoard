"""ORM model declarations imported for Alembic metadata registration."""

from assetboard.db.models.identity import (
    AuditEvent,
    AuthThrottle,
    EmailToken,
    ExternalIdentity,
    InstanceSettings,
    Invitation,
    Membership,
    SetupState,
    User,
    UserSession,
    Workspace,
)
from assetboard.db.models.income import IncomeSource, IncomeSourceTaxRule, WorkDay, WorkLog
from assetboard.db.models.ledger import BudgetLine, Category, MonthState, Transaction
from assetboard.db.models.notifications import NotificationChannel, NotificationLog
from assetboard.db.models.scheduled import ScheduledItem, ScheduledOccurrence
from assetboard.db.models.tax import TaxRule, TaxRuleVersion
from assetboard.db.models.totp import TotpFactor, TotpRecoveryCode

__all__ = [
    "NotificationChannel",
    "NotificationLog",
    "ScheduledItem",
    "ScheduledOccurrence",
    "IncomeSource",
    "IncomeSourceTaxRule",
    "WorkDay",
    "WorkLog",
    "TaxRule",
    "TaxRuleVersion",
    "AuditEvent",
    "AuthThrottle",
    "BudgetLine",
    "Category",
    "EmailToken",
    "ExternalIdentity",
    "InstanceSettings",
    "Invitation",
    "Membership",
    "MonthState",
    "SetupState",
    "Transaction",
    "TotpFactor",
    "TotpRecoveryCode",
    "User",
    "UserSession",
    "Workspace",
]
