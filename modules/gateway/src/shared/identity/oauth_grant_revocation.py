"""Owner-bound revocation intents and bounded recovery leases, without user tokens."""

from datetime import timedelta

from sqlalchemy import or_, select

from src.shared.models.oauth_setup import OAuthGrantRevocation


async def enqueue_revocation(db, *, tenant_id, owner_ref, grant_revision, source_kind, connection_id, provider_account_id, now):
    if (
        not tenant_id
        or not owner_ref
        or not grant_revision
        or source_kind not in {"binding", "attempt"}
        or source_kind == "binding"
        and not connection_id
    ):
        raise ValueError("invalid grant revocation intent")
    existing = await db.scalar(
        select(OAuthGrantRevocation)
        .where(
            OAuthGrantRevocation.tenant_id == tenant_id,
            OAuthGrantRevocation.owner_ref == owner_ref,
            OAuthGrantRevocation.grant_revision == grant_revision,
        )
        .with_for_update()
    )
    if existing:
        if (existing.source_kind, existing.connection_id, existing.provider_account_id) != (source_kind, connection_id, provider_account_id):
            raise ValueError("grant generation changed")
        return existing.id
    job = OAuthGrantRevocation(
        tenant_id=tenant_id,
        owner_ref=owner_ref,
        grant_revision=grant_revision,
        source_kind=source_kind,
        connection_id=connection_id,
        provider_account_id=provider_account_id,
        state="pending",
        due_at=now,
        attempts=0,
    )
    db.add(job)
    await db.flush()
    return job.id


async def claim_due_revocation(db, *, owner, now):
    if not owner or len(owner) > 128:
        raise ValueError("invalid recovery owner")
    job = await db.scalar(
        select(OAuthGrantRevocation)
        .where(
            OAuthGrantRevocation.state.in_(("pending", "leased")),
            OAuthGrantRevocation.due_at <= now,
            or_(OAuthGrantRevocation.lease_expires_at.is_(None), OAuthGrantRevocation.lease_expires_at <= now),
        )
        .order_by(OAuthGrantRevocation.due_at, OAuthGrantRevocation.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if job is None:
        return None
    job.state = "leased"
    job.lease_owner = owner
    job.lease_expires_at = now + timedelta(seconds=45)
    job.attempts += 1
    await db.flush()
    return job.id


async def has_revocation_lease(db, *, job_id, owner, now):
    job = await db.get(OAuthGrantRevocation, job_id, with_for_update=True)
    return bool(job and job.state == "leased" and job.lease_owner == owner and job.lease_expires_at > now)


async def retry_revocation(db, *, job_id, owner, now, category):
    if category not in {"transport", "unsupported", "uncertain", "generation_changed"}:
        raise ValueError("revocation errors must be categorized without provider payloads")
    job = await db.get(OAuthGrantRevocation, job_id, with_for_update=True)
    if job is None or job.state != "leased" or job.lease_owner != owner or job.lease_expires_at <= now:
        return False
    job.state = "pending"
    job.lease_owner = None
    job.lease_expires_at = None
    job.last_error_category = category
    job.due_at = now + timedelta(seconds=min(60 * 2 ** min(job.attempts - 1, 4), 900))
    await db.flush()
    return True


async def retire_revocation(db, *, job_id, owner, now):
    job = await db.get(OAuthGrantRevocation, job_id, with_for_update=True)
    if job is None or job.state != "leased" or job.lease_owner != owner or job.lease_expires_at <= now:
        return False
    job.state = "done"
    job.terminal_at = now
    job.lease_owner = None
    job.lease_expires_at = None
    await db.flush()
    return True
