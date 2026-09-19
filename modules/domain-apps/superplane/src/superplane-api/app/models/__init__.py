"""SQLAlchemy models — import all models here so Alembic can discover them."""

from app.models.organization import Organization  # noqa: F401
from app.models.workspace import Workspace  # noqa: F401
from app.models.cluster import Cluster  # noqa: F401
from app.models.node_pool import NodePool  # noqa: F401
from app.models.node import Node  # noqa: F401
from app.models.deployment import Deployment  # noqa: F401
from app.models.event import Event  # noqa: F401
from app.models.credential import (  # noqa: F401
    CredentialRegistry,
    ClusterVaultAssignment,
    CredentialAuditLog,
)
from app.models.cloud_account import CloudAccount  # noqa: F401
from app.models.reconcile_lock import ReconcileLock  # noqa: F401
from app.models.observation import (  # noqa: F401
    ObservationReceipt,
    ObservationLease,
)
from app.models.provider_handle import ProviderOperation  # noqa: F401
from app.models.api_key import ApiKey  # noqa: F401
from app.models.research_finding import ResearchFinding  # noqa: F401
from app.models.research_proposal import ResearchProposal  # noqa: F401
from app.models.budget_alert import BudgetAlert  # noqa: F401
from app.models.user import User  # noqa: F401
from app.models.workspace_grant import WorkspaceGrantRecord  # noqa: F401
from app.models.provider_connection import (  # noqa: F401
    ProviderConnection,
    ProviderConnectionBinding,
)
