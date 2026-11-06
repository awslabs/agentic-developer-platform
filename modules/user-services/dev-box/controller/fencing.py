"""Lease and admission fences shared by delayed host mutations."""

import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import or_, select, update

from src.cloud_desktop.capacity import check_capacity
from src.shared.models.dev_box import (
    DevBox,
    DevBoxOperation,
    DevBoxPolicy,
    DevBoxTemplate,
)
from src.shared.models.onboarding import TenantMembership


async def claim(session_factory, operation_id, worker_id, actions, *, lease_seconds=60):
    async with session_factory() as db:
        generation = await db.scalar(
            update(DevBoxOperation)
            .where(
                DevBoxOperation.id == operation_id,
                DevBoxOperation.action.in_(actions),
                DevBoxOperation.status.in_(("pending", "unknown")),
                or_(DevBoxOperation.lease_expires_at.is_(None), DevBoxOperation.lease_expires_at <= datetime.now(UTC)),
            )
            .values(lease_owner=worker_id, lease_expires_at=datetime.now(UTC) + timedelta(seconds=lease_seconds),
                    retries=DevBoxOperation.retries + 1)
            .returning(DevBoxOperation.retries)
        )
        if generation is None:
            await db.rollback()
            return None
        await db.commit()
        return generation


async def current(db, operation_id, worker_id, lease_generation, *, cleanup=False):
    operation = await db.scalar(
        select(DevBoxOperation).where(
            DevBoxOperation.id == operation_id,
            DevBoxOperation.lease_owner == worker_id,
            DevBoxOperation.retries == lease_generation,
            DevBoxOperation.status.in_(("pending", "unknown")),
        ).with_for_update()
    )
    if operation is None:
        return None, None, "stale_lease"
    box = await db.scalar(select(DevBox).where(DevBox.id == operation.box_id).with_for_update())
    if (box is None or box.machine_generation != operation.machine_generation
            or (box.user_id, box.org_id, box.deployment_id) != (operation.user_id, operation.org_id, operation.deployment_id)
            or box.desired_state != {"setup": "running", "start": "running", "stop": "stopped", "replace": "running", "release": "released"}.get(operation.action)):
        return operation, box, "stale_generation"
    if cleanup and operation.action == "replace" and operation.progress == "restore_cleanup":
        return operation, box, None
    member = await db.scalar(select(TenantMembership.id).where(
        TenantMembership.user_id == box.user_id, TenantMembership.tenant_id == box.org_id,
        TenantMembership.revoked_at.is_(None),
    ))
    if member is None:
        return operation, box, "owner_membership_revoked"
    policy = await db.scalar(select(DevBoxPolicy).where(
        DevBoxPolicy.org_id == box.org_id, DevBoxPolicy.deployment_id == box.deployment_id,
    ))
    template = await db.get(DevBoxTemplate, box.template_id)
    if not policy or not template or template.revision != box.template_revision:
        return operation, box, "entitlement_revoked"
    if operation.action in ("setup", "start", "replace"):
        if not template.approved:
            return operation, box, "entitlement_revoked"
        if (not policy.enabled or template.instance_size not in policy.allowed_sizes
                or template.disk_gb > policy.max_disk_gb):
            return operation, box, "entitlement_revoked"
        try:
            await check_capacity(db, policy, box.deployment_id, new_box=False, admitted=True)
        except HTTPException:
            return operation, box, "quota_changed"
    return operation, box, None


def finish(operation, error=None):
    if error:
        operation.status = "unknown"
        operation.error_class = str(error)
    operation.lease_owner = None
    operation.lease_expires_at = None


async def renew(session_factory, operation_id, worker_id, lease_generation, lease_seconds=60):
    async with session_factory() as db:
        renewed = await db.scalar(
            update(DevBoxOperation)
            .where(DevBoxOperation.id == operation_id,
                   DevBoxOperation.lease_owner == worker_id,
                   DevBoxOperation.retries == lease_generation,
                   DevBoxOperation.status.in_(("pending", "unknown")))
            .values(lease_expires_at=datetime.now(UTC) + timedelta(seconds=lease_seconds))
            .returning(DevBoxOperation.id)
        )
        await db.commit()
        return renewed is not None




@asynccontextmanager
async def maintained_lease(session_factory, operation_id, worker_id, lease_generation, lease_seconds=60):
    async def heartbeat():
        while True:
            await asyncio.sleep(lease_seconds / 3)
            if not await renew(session_factory, operation_id, worker_id, lease_generation, lease_seconds):
                return

    task = asyncio.create_task(heartbeat())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def require_access_fenced(box, action, fence=None, *, db=None):
    """Refuse a stop/replace until live host authority is revoked.

    ``fence(db, box, action)`` runs INSIDE the caller's transaction on the box row the
    caller already holds ``FOR UPDATE`` (``current()`` locks it). A fence that opened its
    own session and locked the same row would wait on its caller forever — dev
    2026-10-19: every controller tick hung in that wait, kept the lock, and every API
    call that locks the box timed out. SQLite tests cannot see row locks, so the
    contract is enforced by signature: the fence receives the session and the box.
    """
    if box.auth_readiness in ("unknown", "fenced"):
        return None
    if fence is None or db is None or await fence(db, box, action) is not True:
        return "identity_fence_required"
    return None
