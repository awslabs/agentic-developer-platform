"""One-tick Identity grant cleanup; only an exact-grant operation may retire a job."""

from datetime import UTC, datetime

from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from src.shared.identity.oauth_grant_revocation import (
    claim_due_revocation,
    has_revocation_lease,
    retire_revocation,
    retry_revocation,
)
from src.shared.models.oauth_setup import OAuthGrantRevocation
from src.shared.models.vault import UserCredential


async def process_revocation_once(sessions, *, owner, now, revoke_exact=None, clock=None):
    clock = clock or (lambda: datetime.now(UTC))
    async with sessions() as db:
        async with db.begin():
            job_id = await claim_due_revocation(db, owner=owner, now=now)
            if job_id is None:
                return "idle"
    async with sessions() as db:
        job = await db.get(OAuthGrantRevocation, job_id)
        active = await db.scalar(
            select(UserCredential.id).where(
                UserCredential.org_id == job.tenant_id,
                UserCredential.user_id == job.owner_ref,
                UserCredential.service == "atlassian-mcp",
                UserCredential.provider == "agentcore-identity",
                UserCredential.agent_consent.is_(True),
            )
        )
        grant = {
            "tenant_id": job.tenant_id,
            "owner_ref": job.owner_ref,
            "grant_revision": job.grant_revision,
            "source_kind": job.source_kind,
            "connection_id": job.connection_id,
            "provider_account_id": job.provider_account_id,
        }
    if active is not None:
        status, category = "blocked", "generation_changed"
    elif revoke_exact is None:
        status, category = "blocked", "unsupported"
    else:
        async with sessions() as db:
            async with db.begin():
                leased = await has_revocation_lease(db, job_id=job_id, owner=owner, now=clock())
        if not leased:
            return "lost_lease"
        try:
            verified = await run_in_threadpool(revoke_exact, grant)
        except Exception:
            status, category = "uncertain", "transport"
        else:
            status, category = ("resolved", None) if verified is True else ("uncertain", "uncertain")
    async with sessions() as db:
        async with db.begin():
            if status == "resolved":
                retained = await retire_revocation(db, job_id=job_id, owner=owner, now=clock())
            else:
                retained = await retry_revocation(db, job_id=job_id, owner=owner, now=clock(), category=category)
    return status if retained else "lost_lease"
