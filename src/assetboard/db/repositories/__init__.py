"""Repositories. Importing this package registers every ``ScopedRepository`` subclass."""

from assetboard.db.repositories.audit import (
    AuditCursor,
    AuditEventRepository,
    AuditFilter,
    InstanceAuditReader,
)
from assetboard.db.repositories.base import (
    ForeignWorkspace,
    NotFound,
    RepositoryError,
    ScopedRepository,
    UnsupportedOperation,
)
from assetboard.db.repositories.budget_line import BudgetLineRepository, LineUsage
from assetboard.db.repositories.category import CategoryRepository
from assetboard.db.repositories.import_batch import ImportBatchRepository
from assetboard.db.repositories.import_category_mapping import ImportCategoryMappingRepository
from assetboard.db.repositories.income import (
    IncomeSourceRepository,
    IncomeSourceTaxRuleRepository,
    WorkDayRepository,
    WorkLogRepository,
)
from assetboard.db.repositories.investments import (
    AssetClassRepository,
    BondTermsRepository,
    InstrumentAllocationRepository,
    InstrumentRepository,
    InvestmentTransactionRepository,
    RiskTargetPolicyRepository,
)
from assetboard.db.repositories.invitation import (
    InvitationLocation,
    InvitationRepository,
    InvitationTokenReader,
)
from assetboard.db.repositories.loans import (
    LoanBalanceCheckpointRepository,
    LoanCostComponentRepository,
    LoanOverpaymentRepository,
    LoanRateChangeRepository,
    LoanRepository,
    LoanScenarioRepository,
    ReferenceRateRepository,
    ReferenceRateValueRepository,
)
from assetboard.db.repositories.membership import MembershipRepository, WorkspaceMember
from assetboard.db.repositories.month_state import MonthStateRepository
from assetboard.db.repositories.networth import (
    ItemValuationRepository,
    NetWorthCategoryRepository,
    NetWorthItemRepository,
    NetWorthSnapshotRepository,
)
from assetboard.db.repositories.notifications import (
    NotificationChannelRepository,
    NotificationLogRepository,
)
from assetboard.db.repositories.prices import (
    FxRateRepository,
    PricePointRepository,
    PriceSourceConfigRepository,
    ProviderErrorRepository,
)
from assetboard.db.repositories.scheduled import (
    ScheduledItemRepository,
    ScheduledOccurrenceRepository,
)
from assetboard.db.repositories.tax import TaxRuleRepository, TaxRuleVersionRepository
from assetboard.db.repositories.transaction import (
    CategoryCounts,
    DescriptionMatch,
    TransactionRepository,
)
from assetboard.db.repositories.user_memberships import MembershipView, UserMembershipReader
from assetboard.db.repositories.workspace import (
    UPDATABLE_FIELDS,
    CurrentWorkspaceRepository,
    NewWorkspaceScope,
    create_owned_workspace,
)

__all__ = [
    "NotificationChannelRepository",
    "NotificationLogRepository",
    "FxRateRepository",
    "PricePointRepository",
    "PriceSourceConfigRepository",
    "ProviderErrorRepository",
    "AssetClassRepository",
    "BondTermsRepository",
    "InstrumentRepository",
    "InstrumentAllocationRepository",
    "InvestmentTransactionRepository",
    "RiskTargetPolicyRepository",
    "ScheduledItemRepository",
    "ScheduledOccurrenceRepository",
    "IncomeSourceRepository",
    "IncomeSourceTaxRuleRepository",
    "LoanBalanceCheckpointRepository",
    "LoanCostComponentRepository",
    "LoanOverpaymentRepository",
    "LoanRateChangeRepository",
    "LoanScenarioRepository",
    "LoanRepository",
    "ReferenceRateRepository",
    "ReferenceRateValueRepository",
    "ItemValuationRepository",
    "NetWorthCategoryRepository",
    "NetWorthItemRepository",
    "NetWorthSnapshotRepository",
    "WorkDayRepository",
    "WorkLogRepository",
    "TaxRuleRepository",
    "TaxRuleVersionRepository",
    "UPDATABLE_FIELDS",
    "AuditCursor",
    "AuditEventRepository",
    "AuditFilter",
    "BudgetLineRepository",
    "CategoryCounts",
    "CategoryRepository",
    "InstanceAuditReader",
    "CurrentWorkspaceRepository",
    "DescriptionMatch",
    "ForeignWorkspace",
    "InvitationLocation",
    "InvitationRepository",
    "ImportBatchRepository",
    "ImportCategoryMappingRepository",
    "InvitationTokenReader",
    "LineUsage",
    "MembershipRepository",
    "MembershipView",
    "MonthStateRepository",
    "NewWorkspaceScope",
    "NotFound",
    "RepositoryError",
    "ScopedRepository",
    "TransactionRepository",
    "UnsupportedOperation",
    "UserMembershipReader",
    "WorkspaceMember",
    "create_owned_workspace",
]
