"""Claim, resume and finish bounded source verification without a background owner."""

from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.products.models import ProductOperation, ProductSourceBinding
from src.shared.models.base import new_uuid, utcnow

LEASE_DURATION = timedelta(seconds=60)
RETRY_DELAY = timedelta(seconds=5)
MAX_ATTEMPTS = 10


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def tombstone_binding(db: AsyncSession, binding: ProductSourceBinding) -> None:
    """Fence outstanding attempts before tombstoning the selected source."""
    now = utcnow()
    await db.execute(
        update(ProductOperation)
        .where(
            ProductOperation.tenant_id == binding.tenant_id,
            ProductOperation.binding_id == binding.id,
            ProductOperation.binding_generation == binding.generation,
            ProductOperation.state.in_(("pending", "running", "failed")),
        )
        .values(
            state="cancelled",
            receipt={"coverage": "denied"},
            lease_owner=None,
            lease_until=None,
            retry_at=None,
        )
        .execution_options(synchronize_session=False)
    )
    changed = await db.execute(
        update(ProductSourceBinding)
        .where(
            ProductSourceBinding.tenant_id == binding.tenant_id,
            ProductSourceBinding.id == binding.id,
            ProductSourceBinding.generation == binding.generation,
            ProductSourceBinding.unlinked_at.is_(None),
        )
        .values(
            status="unlinked",
            unlinked_at=now,
            updated_at=now,
            generation=ProductSourceBinding.generation + 1,
            revision=ProductSourceBinding.revision + 1,
        )
        .execution_options(synchronize_session=False)
    )
    if changed.rowcount != 1:
        raise HTTPException(409, detail={"code": "binding_conflict"})


def operation_view(operation: ProductOperation) -> dict:
    receipt = operation.receipt or {}
    return {"operation_id": operation.id, "status": operation.state, "coverage": receipt.get("coverage")}


async def run_verification(
    db: AsyncSession,
    actor,
    operation: ProductOperation,
    binding: ProductSourceBinding,
    observe,
    connection_access,
) -> dict:
    """Only the current lease may commit a readback against its pinned binding generation."""
    now = utcnow()
    if _aware(operation.expires_at) <= now:
        raise HTTPException(410, detail={"code": "operation_expired"})
    lease_owner = new_uuid()
    operation_id, binding_id = operation.id, binding.id
    claimed = await db.execute(
        update(ProductOperation)
        .where(
            ProductOperation.tenant_id == actor.org_id,
            ProductOperation.id == operation_id,
            ProductOperation.actor_id == operation.actor_id,
            ProductOperation.binding_id == binding_id,
            ProductOperation.binding_generation == binding.generation,
            ProductOperation.expires_at > now,
            ProductOperation.attempt_count < MAX_ATTEMPTS,
            or_(
                ProductOperation.state == "pending",
                and_(ProductOperation.state == "running", ProductOperation.lease_until <= now),
                and_(ProductOperation.state == "failed", ProductOperation.retry_at <= now),
            ),
        )
        .values(
            state="running",
            lease_owner=lease_owner,
            lease_until=now + LEASE_DURATION,
            attempt_count=ProductOperation.attempt_count + 1,
            retry_at=None,
        )
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        await db.rollback()
        await db.refresh(operation)
        if _aware(operation.expires_at) <= now:
            raise HTTPException(410, detail={"code": "operation_expired"})
        return operation_view(operation)
    await db.commit()
    installation = await connection_access(db, actor, binding)
    aws_authority = None
    jenkins_authority = None
    if installation is None:
        result = {"coverage": "denied", "status": "revoked", "reason": "source_access_denied"}
    else:
        from src.shared.models.vault import ChannelTenantMap

        if binding.provider == "aws":
            from src.auth.aws_connection_authority import verified_connection_evidence

            aws_authority = verified_connection_evidence(installation)
        if binding.provider in {"jenkins", "sonarqube", "argocd", "gitlab", "pagerduty", "linear", "slack"}:
            from src.product_onboarding.discovery import connection_fence

            jenkins_authority = connection_fence(installation)
        result = await observe(
            binding,
            installation
            if isinstance(installation, ChannelTenantMap)
            or binding.provider in {"jira", "aws", "jenkins", "sonarqube", "argocd", "gitlab", "pagerduty", "linear", "slack"}
            else None,
        )
    db.expire_all()
    locked = await db.scalar(
        select(ProductOperation)
        .where(
            ProductOperation.tenant_id == actor.org_id,
            ProductOperation.id == operation_id,
        )
        .with_for_update()
    )
    finished_at = utcnow()
    if locked.lease_owner != lease_owner or _aware(locked.lease_until) <= finished_at:
        view = operation_view(locked)
        await db.rollback()
        return view
    current = await db.get(ProductSourceBinding, (actor.org_id, binding_id))
    can_finish = (
        current is not None
        and current.generation == locked.binding_generation
        and current.unlinked_at is None
        and await connection_access(db, actor, current) is not None
    )
    if can_finish and aws_authority is not None:
        connection = await connection_access(db, actor, current, lock=True)
        can_finish = connection is not None and verified_connection_evidence(connection) == aws_authority
    if can_finish and jenkins_authority is not None:
        connection = await connection_access(db, actor, current, lock=True)
        can_finish = connection is not None and connection_fence(connection) == jenkins_authority
    if _aware(locked.expires_at) <= finished_at:
        locked.state = "cancelled"
        locked.receipt = {"coverage": "unavailable", "reason": "operation_expired"}
    elif not can_finish:
        locked.state = "cancelled"
        locked.receipt = {"coverage": "denied"}
    else:
        if result["coverage"] == "complete":
            changed = await db.execute(
                update(ProductSourceBinding)
                .where(
                    ProductSourceBinding.tenant_id == actor.org_id,
                    ProductSourceBinding.id == binding_id,
                    ProductSourceBinding.generation == locked.binding_generation,
                    ProductSourceBinding.unlinked_at.is_(None),
                )
                .values(observed=result["observed"], status="healthy", updated_at=finished_at)
            )
            if changed.rowcount != 1:
                locked.state = "cancelled"
                locked.receipt = {"coverage": "denied"}
            else:
                locked.state = "succeeded"
                locked.receipt = {"coverage": "complete"}
        elif result["coverage"] == "denied":
            await db.execute(
                update(ProductSourceBinding)
                .where(
                    ProductSourceBinding.tenant_id == actor.org_id,
                    ProductSourceBinding.id == binding_id,
                    ProductSourceBinding.generation == locked.binding_generation,
                    ProductSourceBinding.unlinked_at.is_(None),
                )
                .values(status="revoked", updated_at=finished_at)
            )
            locked.state = "failed"
            locked.receipt = {"coverage": "denied"}
        else:
            locked.state = "failed"
            locked.receipt = {"coverage": "unavailable", "reason": result.get("reason", "provider_read_unavailable")}
            locked.retry_at = finished_at + RETRY_DELAY
    locked.lease_owner = None
    locked.lease_until = None
    await db.commit()
    return operation_view(locked)
