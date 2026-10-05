"""Live authority adapters for delegated chat data access.

No routes are enabled here. Trusted admission must write the session's explicit
chatLease and immutable launch before use; a missing lease never grants access.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from anyio import from_thread
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select

from src.agentauth.bootstrap import BootstrapRefusedError, BootstrapStore
from src.agentauth.chat_capability import ChatCapabilityService, ChatLaunch, ChatLaunchStore, Identifier
from src.agentauth.execution import ExecutionStateError, evaluate_execution_state
from src.agentauth.workload import KubernetesWorkloadVerifier, WorkloadRefusedError, WorkloadUnavailableError
from src.orchestration.chat_data_migration import _owns_context_row
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Team, TeamMembership, User


class ChatSessionLease(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: Identifier
    sandbox_uid: Identifier
    generation: int = Field(strict=True, ge=1)
    expires_at: int = Field(strict=True, ge=1)

    @field_validator("generation", "expires_at", mode="before")
    @classmethod
    def stored_integer(cls, value):
        if isinstance(value, bool) or not isinstance(value, int | Decimal) or int(value) != value:
            raise ValueError("lease requires integral numeric values")
        return int(value)


async def current_chat_member(db, tenant_id: str, user_id: str, team_id: str) -> bool:
    """Resolve canonical identities only, with current revocation taking priority.

    A legacy org-local human without a tenant-membership row retains that org's
    least-privilege membership. Team access always requires an actual membership
    row, never the cached primary-team pointer on the user.
    """
    user = await db.scalar(select(User.id).where(User.id == user_id, User.org_id == tenant_id, User.user_kind == "human"))
    if user is None:
        return False
    revoked = await db.scalar(
        select(TenantMembership.id).where(
            TenantMembership.user_id == user_id,
            TenantMembership.tenant_id == tenant_id,
            TenantMembership.revoked_at.is_not(None),
        )
    )
    if revoked is not None:
        return False
    if team_id == "":
        return True
    team = await db.scalar(
        select(TeamMembership.team_id)
        .join(Team, (Team.id == TeamMembership.team_id) & (Team.org_id == TeamMembership.org_id))
        .where(TeamMembership.user_id == user_id, TeamMembership.org_id == tenant_id, TeamMembership.team_id == team_id)
    )
    return team is not None


class ChatRuntimeAuthority:
    def __init__(self, store: BootstrapStore, context_table, workloads: KubernetesWorkloadVerifier):
        self.store = store
        self.context_table = context_table
        self.workloads = workloads

    def current(self, launch: ChatLaunch, now: int) -> bool:
        """Called in a worker thread; re-read every authority, without caching."""
        instant = datetime.fromtimestamp(now, UTC)
        try:
            execution = evaluate_execution_state(
                record=self.store.authority.load_execution(invocation_id=launch.run_id, tenant_id=launch.tenant_id),
                invocation_id=launch.run_id,
                tenant_id=launch.tenant_id,
                attempt=launch.attempt,
                credential_epoch=launch.credential_epoch,
                presented_workload_binding=launch.sandbox_uid,
                now=instant,
            )
            if execution.workload_binding != launch.sandbox_uid or execution.repo != f"chat/{launch.session_id}":
                return False
            if self.store.authority.abort_intent(invocation_id=launch.run_id, tenant_id=launch.tenant_id) is not None:
                return False
            grant = self.store.live_grant(invocation_id=launch.run_id, tenant_id=launch.tenant_id, attempt=launch.attempt, now=instant)
            if (
                grant.authority.kind != "chat_event"
                or grant.authority.human_id != launch.user_id
                or grant.grant_id != launch.grant_id
                or grant.revocation_epoch != launch.grant_epoch
                or execution.repo not in grant.repo_scope
                or grant.expires_at is None
                or launch.expires_at > grant.expires_at.timestamp()
            ):
                return False
            row = self.context_table.get_item(Key={"PK": f"session#{launch.session_id}", "SK": "header"}, ConsistentRead=True).get("Item")
            if not row or not _owns_context_row(row, (launch.tenant_id, launch.team_id, launch.user_id)) or row.get("status") != "active":
                return False
            ttl = row.get("ttl")
            if isinstance(ttl, bool) or not isinstance(ttl, int | Decimal) or int(ttl) != ttl or ttl <= now:
                return False
            lease = ChatSessionLease.model_validate(row.get("chatLease"))
            if (
                lease.run_id != launch.run_id
                or lease.sandbox_uid != launch.sandbox_uid
                or lease.generation != launch.lease_generation
                or lease.expires_at <= now
            ):
                return False
            metadata = self.store._read(f"TENANT#{launch.tenant_id}", f"EXEC#{launch.run_id}") or {}
            pod = self.workloads.verify_bound(name=metadata.get("pod_name", {}).get("S", ""), uid=launch.sandbox_uid)
            return pod.uid == launch.sandbox_uid and pod.image_digest == launch.image_digest
        except (BootstrapRefusedError, ExecutionStateError, ValidationError):
            return False
        except WorkloadUnavailableError:
            raise
        except WorkloadRefusedError:
            return False


def chat_capabilities(*, authority: ChatRuntimeAuthority, session_factory, env: dict[str, str] | None = None) -> ChatCapabilityService:
    """Build production callbacks for calls made through run_in_threadpool.

    The async SQL lookup runs on the event loop with a new session per check;
    boto/Kubernetes calls stay in the worker thread. No principal state is cached.
    """

    async def member(tenant_id: str, user_id: str, team_id: str) -> bool:
        async with session_factory() as db:
            return await current_chat_member(db, tenant_id, user_id, team_id)

    return ChatCapabilityService(
        ChatLaunchStore(authority.store),
        current=authority.current,
        member=lambda tenant, user, team: from_thread.run(member, tenant, user, team),
        env=env,
    )
