"""Live authority adapters for delegated chat data access.

No routes are enabled here. Trusted admission must write the session's explicit
chatLease and immutable launch before use; a missing lease never grants access.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from anyio import from_thread
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import or_, select

from src.agentauth.bootstrap import BootstrapRefusedError, BootstrapStore
from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatCapabilityService, ChatLaunch, ChatLaunchStore, Identifier
from src.agentauth.chat_crew_run import current_crew_store
from src.agentauth.execution import ExecutionStateError, evaluate_execution_state
from src.agentauth.workload import KubernetesWorkloadVerifier, WorkloadRefusedError, WorkloadUnavailableError
from src.orchestration.chat_data_migration import _owns_context_row
from src.shared.identity.workspaces import person_user_ids
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


def current_tenant_member(tenant_id: str):
    """SQL predicate on ``User``: the user currently belongs to ``tenant_id``.

    Membership is the ``tenant_memberships`` row for this exact tenant with no
    ``revoked_at``; ``is_active`` only marks the selected workspace and never
    decides membership. A legacy org-local human without any membership row keeps
    that org's least-privilege membership. A revoked row always refuses, so the
    workspace claim in a token can never outlive revocation here.
    """
    membership = select(TenantMembership.id).where(TenantMembership.user_id == User.id, TenantMembership.tenant_id == tenant_id)
    return or_(
        membership.where(TenantMembership.revoked_at.is_(None)).exists(),
        (User.org_id == tenant_id) & ~membership.where(TenantMembership.revoked_at.is_not(None)).exists(),
    )


async def tenant_member_user(db, tenant_id: str, user_id: str) -> User | None:
    """The canonical human ``user_id`` if they currently belong to ``tenant_id``.

    Every chat, crew and sandbox surface that must answer "does this human belong
    to this tenant" resolves the user here, by canonical ``users.id`` only, with
    ``current_tenant_member``. ``users.org_id`` is the home org (a personal org for
    a GitHub-login human) and never scopes a tenant lookup on these paths.
    """
    return await db.scalar(select(User).where(User.id == user_id, User.user_kind == "human", current_tenant_member(tenant_id)))


CHAT_PRINCIPAL_UNKNOWN = "chat_principal_unknown"
CHAT_PRINCIPAL_AMBIGUOUS = "chat_principal_ambiguous"
CHAT_PRINCIPAL_NOT_TENANT_MEMBER = "chat_principal_not_tenant_member"


def principal_refusal_detail(reason: str, tenant_id: str) -> str:
    """Operator-facing phrase for a ``tenant_member_principal`` refusal (ids only)."""
    if reason == CHAT_PRINCIPAL_NOT_TENANT_MEMBER:
        return f"no member row in tenant {tenant_id}"
    if reason == CHAT_PRINCIPAL_AMBIGUOUS:
        return f"several member rows in tenant {tenant_id}; merge the duplicate users rows (#8011)"
    if reason == CHAT_PRINCIPAL_UNKNOWN:
        return "no human users row matches this login"
    return reason


async def tenant_member_principal(db, tenant_id: str, subject: str, *, username: str = "") -> User:
    """The tenant member ``users`` row for ``subject`` in ``tenant_id`` (#8011, #8108).

    This is THE owner resolver: chat admission (``external_roots.canonical_human``),
    the Crew owner (``crew.proxy.current_crew_owner``), the Dev Box owner
    (``cloud_desktop.routes.owner``) and host enrollment
    (``cloud_desktop.host_identity.current_host``) all derive their principal here so
    they agree on one ``users.id`` per person per tenant. ``username`` is the SIGNED
    Cognito username from the caller's token when there is one (``GitHub_<id>``);
    chat admission has only the subject and passes none.

    ``subject`` is the authenticated login the intake resolved (a Cognito sub or a
    canonical ``users.id``). One person can own several ``users`` rows (#8011): the
    Cognito-index canonical row has ``users.org_id`` = their *home* org (a personal
    org for a GitHub-login human) and carries no team in the chat tenant, while the
    tenant's own member row (the one ``GET /organizations/<tenant>/users`` lists)
    carries the team the budget/task hierarchy needs. The Cognito-index id is a
    login identity, not a tenant principal: binding it would hand admission,
    ``ChatDelivery``, the model-policy snapshot and the owner ledger a principal
    with ``team_id == ""`` and the turn is refused as
    ``task_identity_hierarchy_unavailable`` before it reaches the provider.

    Resolution, deterministic:

    1. the login rows (``users.id`` or ``cognito_sub`` equal to ``subject``,
       ``user_kind == "human"``) are fused into the person's row set with
       ``person_user_ids`` (same login, org-placement link, proven GitHub/directory
       identity). Two login rows that are not the same person are ambiguous and
       refused, as before;
    2. among the person's rows that are *current tenant members*
       (``current_tenant_member``), the row whose ``org_id == tenant_id`` is the
       tenant member principal; exactly one is required;
    3. a person with no org-local row in the tenant but exactly one membership-based
       row keeps that row (the single-row GitHub-login human, unchanged behaviour);
    4. a person with no current member row refuses ``chat_principal_not_tenant_member``.

    Raises ``ChatAuthorizationRefusedError`` with the reason as its message.
    """
    logins = (await db.scalars(select(User).where(User.user_kind == "human", or_(User.id == subject, User.cognito_sub == subject)))).all()
    if not logins:
        raise ChatAuthorizationRefusedError(CHAT_PRINCIPAL_UNKNOWN)
    person = await person_user_ids(db, logins[0], username=username)
    if any(login.id not in person for login in logins):
        # A users.id equal to another human's cognito_sub names two people.
        raise ChatAuthorizationRefusedError(CHAT_PRINCIPAL_AMBIGUOUS)
    members = (
        await db.scalars(select(User).where(User.id.in_(person), User.user_kind == "human", current_tenant_member(tenant_id)).order_by(User.id))
    ).all()
    local = [member for member in members if member.org_id == tenant_id]
    chosen = local if local else members
    if not chosen:
        raise ChatAuthorizationRefusedError(CHAT_PRINCIPAL_NOT_TENANT_MEMBER)
    if len(chosen) != 1:
        raise ChatAuthorizationRefusedError(CHAT_PRINCIPAL_AMBIGUOUS)
    return chosen[0]


async def current_chat_member(db, tenant_id: str, user_id: str, team_id: str) -> bool:
    """Resolve canonical identities only, with current revocation taking priority.

    Tenant membership follows ``current_tenant_member``: a GitHub-login human whose
    own ``users.org_id`` is a personal org is still a member of every tenant with a
    live membership row. Team access always requires an actual membership row,
    never the cached primary-team pointer on the user.
    """
    if await tenant_member_user(db, tenant_id, user_id) is None:
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
    def __init__(self, store: BootstrapStore, context_table, workloads: KubernetesWorkloadVerifier, *, crew_workloads=None):
        self.store = store
        self.context_table = context_table
        self.workloads = workloads
        self.crew_workloads = crew_workloads

    def workloads_for_run(self, run_id: str) -> KubernetesWorkloadVerifier:
        launch = self.store._read(f"CHAT-LAUNCH#{run_id}", "LAUNCH")
        if launch:
            run_id = ChatLaunchStore(self.store).load(run_id).session_run_id or run_id
        creation = self.store._read(f"CHAT-LAUNCH#{run_id}", "CREATION") or {}
        crew_run_id = creation.get("crew_run_id", {}).get("S")
        if not crew_run_id:
            return self.workloads
        row = current_crew_store().read_run(crew_run_id)
        if not row or row.get("kind") != "crew":
            raise WorkloadRefusedError("Crew binding unavailable")
        if row.get("executor_scope") == "session":
            return self.workloads
        binding = creation.get("binding", {}).get("M", {})
        if (
            row.get("executor_scope") != "user"
            or row.get("target_kind") != "eks-pod"
            or binding.get("tenant_id") != {"S": row.get("tenant_id")}
            or binding.get("user_id") != {"S": row.get("user_id")}
            or creation.get("crew_lease_generation") != {"N": str(row.get("lease_generation"))}
            or self.crew_workloads is None
        ):
            raise WorkloadRefusedError("Crew binding unavailable")
        return self.crew_workloads()

    def verify_workload(self, token: str):
        try:
            return self.workloads.verify(token)
        except WorkloadRefusedError:
            if self.crew_workloads is None:
                raise
        verifier = self.crew_workloads()
        pod = verifier.verify(token)
        binding = self.store._read(f"POD#{pod.uid}", "BINDING") or {}
        run_id = binding.get("invocation_id", {}).get("S", "")
        if not run_id or self.workloads_for_run(run_id) is not verifier:
            raise WorkloadRefusedError("Crew workload not admitted")
        return pod

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
            pod = self.workloads_for_run(launch.run_id).verify_bound(name=metadata.get("pod_name", {}).get("S", ""), uid=launch.sandbox_uid)
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
