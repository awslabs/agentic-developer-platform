"""Direct human authority for live controls; no delegated identity is synthesized."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.execution import ExecutionStatus
from src.shared.identity import resolve_canonical_user_id
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import User
from src.shared.schemas.auth import TokenContext


@dataclass(frozen=True)
class HumanControlSession:
    user_id: str
    tenant_id: str
    expires_at: datetime


async def require_live_human_membership(db: AsyncSession, *, user_id: str, tenant_id: str) -> None:
    """Read current membership, without an authorization cache or admin bypass."""
    member = await db.scalar(
        select(User.id)
        .join(TenantMembership, TenantMembership.user_id == User.id)
        .where(
            User.id == user_id,
            User.user_kind == "human",
            User.is_shadow.is_(False),
            TenantMembership.tenant_id == tenant_id,
            TenantMembership.is_active.is_(True),
                    TenantMembership.revoked_at.is_(None),
        )
    )
    if member is None:
        raise BootstrapRefusedError("human control membership is unavailable")


async def authorize_human_session(context: TokenContext, db: AsyncSession, *, now: datetime | None = None) -> HumanControlSession:
    """Only the JWT-authenticated human route can enter this path.

    Service/IAM contexts and billing attribution cannot become human authority.
    The envelope lifetime is subsequently capped by this session's expiry.
    """
    current = now or datetime.now(UTC)
    if context.account_type != "human" or context.auth_source != "jwt" or not context.org_id or context.expires_at <= current:
        raise BootstrapRefusedError("direct human session required")
    user_id = await resolve_canonical_user_id(db, context.user_id, org_id=context.org_id)
    await require_live_human_membership(db, user_id=user_id, tenant_id=context.org_id)
    return HumanControlSession(user_id=user_id, tenant_id=context.org_id, expires_at=context.expires_at)


def require_protected_human_owner(store, *, user_id: str, tenant_id: str, run_id: str, generation: int, now: datetime) -> None:
    """Verify ownership from gateway-owned authority, not worker-writable rows.

    A target's grant records who authorized its launch. Reading that protected
    fact proves ownership; it does not delegate the target's permissions to the
    human or put a grant/epoch in their envelope.
    """
    record = store.authority.load_execution(invocation_id=run_id, tenant_id=tenant_id)
    if record is None or record.status != ExecutionStatus.ACTIVE:
        raise BootstrapRefusedError("target execution is unavailable")
    grant = store.live_grant(invocation_id=run_id, tenant_id=tenant_id, attempt=record.current_attempt, now=now)
    registration = store._read(f"TENANT#{tenant_id}", f"REG#{run_id}#{record.current_attempt}")
    if (
        record.invocation_id != run_id
        or record.tenant_id != tenant_id
        or grant.authority.human_id != user_id
        or grant.authority.org_id != tenant_id
        or not registration
        or registration.get("generation") != {"N": str(generation)}
    ):
        raise BootstrapRefusedError("human does not own this target")
