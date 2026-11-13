"""ORM model declarations imported for Alembic metadata registration."""

from assetboard.db.models.fire import FireContribution, FireEvent, FireScenario
from assetboard.db.models.identity import (
    AuditEvent,
    AuthThrottle,
    EmailToken,
    ExternalIdentity,
    InstanceSettings,
    Invitation,
    LoginTicket,
    Membership,
    SetupState,
    User,
    UserSession,
    Workspace,
)
from assetboard.db.models.import_batch import ImportBatch, ImportCategoryMapping
from assetboard.db.models.income import IncomeSource, IncomeSourceTaxRule, WorkDay, WorkLog
from assetboard.db.models.investments import (
    AssetClass,
    Instrument,
    InstrumentAllocation,
    InvestmentTransaction,
)
from assetboard.db.models.ledger import BudgetLine, Category, MonthState, Transaction
from assetboard.db.models.loans import (
    InstallmentPlan,
    Loan,
    LoanBalanceCheckpoint,
    LoanCostComponent,
    LoanOverpayment,
    LoanPayment,
    LoanRateChange,
    LoanScenario,
    ReferenceRate,
    ReferenceRateValue,
)
from assetboard.db.models.networth import (
    ItemValuation,
    NetWorthCategory,
    NetWorthItem,
    NetWorthSnapshot,
)
from assetboard.db.models.notifications import NotificationChannel, NotificationLog
from assetboard.db.models.prices import (
    FxRate,
    PricePoint,
    PriceSourceConfig,
    ProviderError,
)
from assetboard.db.models.scheduled import ScheduledItem, ScheduledOccurrence
from assetboard.db.models.tax import TaxRule, TaxRuleVersion
from assetboard.db.models.totp import TotpFactor, TotpRecoveryCode

__all__ = [
    "FireContribution",
    "FireEvent",
    "FireScenario",
    "NotificationChannel",
    "NotificationLog",
    "FxRate",
    "PricePoint",
    "PriceSourceConfig",
    "ProviderError",
    "ScheduledItem",
    "ScheduledOccurrence",
    "IncomeSource",
    "IncomeSourceTaxRule",
    "ImportBatch",
    "ImportCategoryMapping",
    "AssetClass",
    "Instrument",
    "InstrumentAllocation",
    "InvestmentTransaction",
    "ItemValuation",
    "InstallmentPlan",
    "Loan",
    "LoanBalanceCheckpoint",
    "LoanCostComponent",
    "LoanOverpayment",
    "LoanPayment",
    "LoanRateChange",
    "LoanScenario",
    "ReferenceRate",
    "ReferenceRateValue",
    "NetWorthCategory",
    "NetWorthItem",
    "NetWorthSnapshot",
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
    "LoginTicket",
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
