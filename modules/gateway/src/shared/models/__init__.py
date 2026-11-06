"""
Shared SQLAlchemy ORM Models.

All models are automatically created on app startup by importing them
in src/app.py lifespan and calling Base.metadata.create_all().
"""

from src.shared.models.atlassian_system import AtlassianSystemConnection
from src.shared.models.base import Base, TenantMixin, new_uuid, utcnow
from src.shared.models.bedrock_routing import (
    MAPPING_SCOPE_TYPES,
    BedrockAccountMapping,
    BedrockDestinationRegistry,
)
from src.shared.models.budget import BudgetConfig, BudgetUsage, PersonBudgetConfig
from src.shared.models.cli_auth import CliAuthRequest
from src.shared.models.crew_checkpoint import CrewCheckpoint
from src.shared.models.crew_conversation import CrewConversation
from src.shared.models.crew_executor import CrewExecutor, CrewOwnerGeneration
from src.shared.models.dev_box import (
    DevBox,
    DevBoxAccessSession,
    DevBoxDevice,
    DevBoxEvent,
    DevBoxInstance,
    DevBoxOperation,
    DevBoxPolicy,
    DevBoxResource,
    DevBoxTemplate,
    DevBoxUsage,
)
from src.shared.models.metering import (
    MeteringAttempt,
    MeteringAttemptScope,
    MeteringLegacyAdmission,
    MeteringReconciliationEvent,
    MeteringScopeAlias,
    MeteringScopeGate,
)
from src.shared.models.native_credentials import NativeCredentialRequest
from src.shared.models.oauth_setup import OAuthGrantRevocation, OAuthRecoveryJob, OAuthSetupState
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import (
    Department,
    Organization,
    ServiceAccount,
    Team,
    User,
)
from src.shared.models.platform_admin import PlatformAdminGrant
from src.shared.models.provenance import ActionProvenance
from src.shared.models.usage import (
    BedrockPoolAccount,
    ModelAlias,
    ModelPricing,
    RateLimitConfig,
    UsageLog,
)
from src.shared.models.vault import (
    ChannelTenantMap,
    CredentialType,
    IdentityProvider,
    UserCredential,
    UserIdentity,
    VerificationMethod,
)

__all__ = [
    "AtlassianSystemConnection",
    "PlatformAdminGrant",
    "MeteringAttempt",
    "MeteringAttemptScope",
    "MeteringLegacyAdmission",
    "MeteringReconciliationEvent",
    "MeteringScopeAlias",
    "MeteringScopeGate",
    "NativeCredentialRequest",
    "CliAuthRequest",
    "CrewCheckpoint",
    "CrewConversation",
    "CrewExecutor",
    "CrewOwnerGeneration",
    "DevBox",
    "DevBoxAccessSession",
    "DevBoxDevice",
    "DevBoxEvent",
    "DevBoxInstance",
    "DevBoxOperation",
    "DevBoxPolicy",
    "DevBoxResource",
    "DevBoxTemplate",
    "DevBoxUsage",
    "ActionProvenance",
    "Base",
    "TenantMixin",
    "new_uuid",
    "utcnow",
    "MAPPING_SCOPE_TYPES",
    "BedrockAccountMapping",
    "BedrockDestinationRegistry",
    "BudgetConfig",
    "BudgetUsage",
    "PersonBudgetConfig",
    "Department",
    "Organization",
    "ServiceAccount",
    "Team",
    "TenantMembership",
    "OAuthGrantRevocation",
    "OAuthRecoveryJob",
    "OAuthSetupState",
    "User",
    "BedrockPoolAccount",
    "ModelAlias",
    "ModelPricing",
    "RateLimitConfig",
    "UsageLog",
    "ChannelTenantMap",
    "CredentialType",
    "IdentityProvider",
    "UserCredential",
    "UserIdentity",
    "VerificationMethod",
]
