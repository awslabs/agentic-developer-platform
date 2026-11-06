"""Transactional setup intent and bounded SQL recovery leases."""

import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.oauth_setup import OAuthRecoveryJob, OAuthSetupState, OAuthTenantPolicyObservation


async def enqueue_setup(db: AsyncSession, *, setup_key: str, config_digest: str, registration_mode: str, now: datetime) -> str:
    if (
        not setup_key
        or not re.fullmatch(r"[0-9a-f]{64}", config_digest)
        or registration_mode not in {"retained-deployment-client", "dynamic-client-registration"}
    ):
        raise ValueError("invalid deployment setup intent")
    state = (await db.execute(select(OAuthSetupState).where(OAuthSetupState.deployment_key == setup_key).with_for_update())).scalar_one_or_none()
    if state is None:
        state = OAuthSetupState(
            deployment_key=setup_key,
            tenant_id="platform",
            registration_mode=registration_mode,
            config_digest=config_digest,
            phase="pending",
            updated_at=now,
        )
        db.add(state)
        await db.flush()
    elif state.config_digest != config_digest or state.registration_mode != registration_mode:
        raise ValueError("deployment configuration differs from retained setup")
    existing = (
        await db.execute(
            select(OAuthRecoveryJob)
            .where(
                OAuthRecoveryJob.setup_key == setup_key,
                OAuthRecoveryJob.kind == "prepare_identity_provider",
                OAuthRecoveryJob.expected_revision == config_digest,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if existing:
        return existing.id
    job = OAuthRecoveryJob(
        setup_key=setup_key,
        tenant_id="platform",
        owner_ref=setup_key,
        kind="prepare_identity_provider",
        expected_revision=config_digest,
        state="pending",
        due_at=now,
        attempts=0,
    )
    db.add(job)
    await db.flush()
    return job.id


async def claim_due_setup(db: AsyncSession, *, owner: str, now: datetime) -> str | None:
    if not owner or len(owner) > 128:
        raise ValueError("invalid recovery owner")
    job = (
        await db.execute(
            select(OAuthRecoveryJob)
            .where(
                OAuthRecoveryJob.state.in_(("pending", "leased")),
                OAuthRecoveryJob.kind == "prepare_identity_provider",
                OAuthRecoveryJob.due_at <= now,
                or_(OAuthRecoveryJob.lease_expires_at.is_(None), OAuthRecoveryJob.lease_expires_at <= now),
            )
            .order_by(OAuthRecoveryJob.due_at, OAuthRecoveryJob.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
    ).scalar_one_or_none()
    if job is None:
        return None
    job.state = "leased"
    job.lease_owner = owner
    job.lease_expires_at = now + timedelta(seconds=45)
    job.attempts += 1
    await db.flush()
    return job.id


async def has_setup_lease(db: AsyncSession, *, job_id: str, owner: str, now: datetime) -> bool:
    job = (await db.execute(select(OAuthRecoveryJob).where(OAuthRecoveryJob.id == job_id).with_for_update())).scalar_one_or_none()
    return bool(job and job.state == "leased" and job.lease_owner == owner and job.lease_expires_at > now)


async def retry_setup(db: AsyncSession, *, job_id: str, owner: str, now: datetime, category: str) -> bool:
    if category not in {"transport", "persistence", "provider_policy", "uncertain"}:
        raise ValueError("recovery errors must be categorized without provider payloads")
    job = (await db.execute(select(OAuthRecoveryJob).where(OAuthRecoveryJob.id == job_id).with_for_update())).scalar_one_or_none()
    if job is None or job.state != "leased" or job.lease_owner != owner or job.lease_expires_at <= now:
        return False
    job.state = "pending"
    job.lease_owner = None
    job.lease_expires_at = None
    job.last_error_category = category
    job.due_at = now + timedelta(seconds=min(60 * 2 ** min(job.attempts - 1, 4), 900))
    await db.flush()
    return True


async def retire_setup(db: AsyncSession, *, job_id: str, owner: str, now: datetime) -> bool:
    job = (await db.execute(select(OAuthRecoveryJob).where(OAuthRecoveryJob.id == job_id).with_for_update())).scalar_one_or_none()
    if job is None or job.state != "leased" or job.lease_owner != owner or job.lease_expires_at <= now:
        return False
    job.state = "done"
    job.terminal_at = now
    job.lease_owner = None
    job.lease_expires_at = None
    await db.flush()
    return True


async def record_policy_observation(
    db: AsyncSession, *, setup_key: str, config_digest: str, result: str, evidence_digest: str, now: datetime
) -> None:
    if result not in {"allowed", "blocked"} or not re.fullmatch(r"[0-9a-f]{64}", evidence_digest):
        raise ValueError("policy observation requires a pinned provider evidence digest")
    state = (await db.execute(select(OAuthSetupState).where(OAuthSetupState.deployment_key == setup_key).with_for_update())).scalar_one()
    if state.config_digest != config_digest or state.phase != "prepared":
        raise ValueError("policy observation does not match prepared setup")
    state.policy_result = result
    state.policy_evidence_ref = evidence_digest
    state.updated_at = now
    await db.flush()


async def record_tenant_policy_observation(
    db: AsyncSession, *, setup_key: str, tenant_id: str, config_digest: str, result: str, evidence_digest: str, now: datetime
) -> None:
    if not tenant_id or len(tenant_id) > 255 or result not in {"allowed", "blocked"} or not re.fullmatch(r"[0-9a-f]{64}", evidence_digest):
        raise ValueError("policy observation requires tenant and pinned provider evidence")
    state = (await db.execute(select(OAuthSetupState).where(OAuthSetupState.deployment_key == setup_key).with_for_update())).scalar_one_or_none()
    if state is None or state.config_digest != config_digest or state.phase != "prepared":
        raise ValueError("policy observation does not match prepared setup")
    policy = await db.get(OAuthTenantPolicyObservation, (setup_key, tenant_id), with_for_update=True)
    if policy is None:
        db.add(
            OAuthTenantPolicyObservation(
                setup_key=setup_key,
                tenant_id=tenant_id,
                config_digest=config_digest,
                policy_result=result,
                evidence_digest=evidence_digest,
                observed_at=now,
            )
        )
    else:
        policy.config_digest = config_digest
        policy.policy_result = result
        policy.evidence_digest = evidence_digest
        policy.observed_at = now
    await db.flush()


async def read_tenant_policy_observation(db: AsyncSession, *, setup_key: str, tenant_id: str, now: datetime) -> str | None:
    if not tenant_id:
        return None
    state = await db.get(OAuthSetupState, setup_key)
    policy = await db.get(OAuthTenantPolicyObservation, (setup_key, tenant_id))
    observed_at = policy.observed_at if policy else None
    if observed_at is not None and observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    if (
        state is None
        or state.phase != "prepared"
        or policy is None
        or policy.config_digest != state.config_digest
        or not re.fullmatch(r"[0-9a-f]{64}", policy.evidence_digest)
        or not timedelta(0) <= now - observed_at <= timedelta(minutes=5)
    ):
        return None
    return policy.policy_result if policy.policy_result in {"allowed", "blocked"} else None
