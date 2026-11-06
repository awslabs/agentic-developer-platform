"""Shared durable capacity checks for admission and delayed provider actions."""

import os
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from sqlalchemy import func, select

from src.shared.models.dev_box import DevBox, DevBoxPolicy, DevBoxResource, DevBoxTemplate, DevBoxUsage


def deployment_limit(name: str) -> int:
    value = os.getenv(name, "")
    if not value.isdecimal() or int(value) <= 0:
        raise HTTPException(403, "Dev Box deployment capacity is not configured")
    return int(value)


async def check_capacity(db, policy: DevBoxPolicy, deployment: str, *, new_box: bool, admitted: bool = False, requested_disk_gb: int = 0) -> None:
    live_limit = deployment_limit("BG_DEV_BOX_MAX_DEPLOYMENT_LIVE_BOXES")
    running_limit = deployment_limit("BG_DEV_BOX_MAX_DEPLOYMENT_RUNNING_BOXES")
    live = await db.scalar(
        select(func.count(DevBox.id)).where(
            DevBox.org_id == policy.org_id,
            DevBox.deployment_id == deployment,
            DevBox.status != "released",
        )
    )
    running = await db.scalar(
        select(func.count(DevBox.id)).where(
            DevBox.org_id == policy.org_id,
            DevBox.deployment_id == deployment,
            DevBox.status != "released",
            (DevBox.desired_state == "running") | (DevBox.observed_state.not_in(("stopped", "terminated"))),
        )
    )
    deployment_live = await db.scalar(
        select(func.count(DevBox.id)).where(
            DevBox.deployment_id == deployment,
            DevBox.status != "released",
        )
    )
    deployment_running = await db.scalar(
        select(func.count(DevBox.id)).where(
            DevBox.deployment_id == deployment,
            DevBox.status != "released",
            (DevBox.desired_state == "running") | (DevBox.observed_state.not_in(("stopped", "terminated"))),
        )
    )
    retained = await db.scalar(
        select(func.count(func.distinct(DevBoxResource.box_id)))
        .join(DevBox, DevBox.id == DevBoxResource.box_id)
        .where(
            DevBox.org_id == policy.org_id,
            DevBox.deployment_id == deployment,
            DevBox.status == "released",
            DevBoxResource.retention_state == "retained",
            DevBoxResource.deletion_state != "verified_deleted",
        )
    )
    deployment_retained = await db.scalar(
        select(func.count(func.distinct(DevBoxResource.box_id)))
        .join(DevBox, DevBox.id == DevBoxResource.box_id)
        .where(
            DevBox.deployment_id == deployment,
            DevBox.status == "released",
            DevBoxResource.retention_state == "retained",
            DevBoxResource.deletion_state != "verified_deleted",
        )
    )
    if new_box and (live + retained >= policy.max_live_boxes or deployment_live + deployment_retained >= live_limit):
        raise HTTPException(403, "Dev Box live capacity reached")
    if (
        (running > policy.max_running_boxes or deployment_running > running_limit)
        if admitted
        else (running >= policy.max_running_boxes or deployment_running >= running_limit)
    ):
        raise HTTPException(403, "Dev Box running capacity reached")
    try:
        deployment_budget = Decimal(str(os.getenv("BG_DEV_BOX_MAX_DEPLOYMENT_BUDGET_USD", "0")))
    except InvalidOperation:
        raise HTTPException(403, "Dev Box deployment budget is not configured") from None
    for org_id, storage_limit, time_limit, budget_limit in (
        (policy.org_id, policy.max_storage_gb, policy.max_running_hours, policy.budget_usd),
        (
            None,
            deployment_limit("BG_DEV_BOX_MAX_DEPLOYMENT_STORAGE_GB"),
            deployment_limit("BG_DEV_BOX_MAX_DEPLOYMENT_RUNNING_HOURS"),
            deployment_budget,
        ),
    ):
        if not storage_limit or not time_limit or not budget_limit or budget_limit <= 0:
            raise HTTPException(403, "Dev Box storage, time or budget policy is incomplete")
        box_scope = (DevBox.deployment_id == deployment,)
        if org_id is not None:
            box_scope += (DevBox.org_id == org_id,)
        reserved_disk = await db.scalar(
            select(func.coalesce(func.sum(DevBoxTemplate.disk_gb), 0))
            .join(DevBox, DevBox.template_id == DevBoxTemplate.id)
            .where(*box_scope, DevBox.status != "released")
        )
        extra_disk = await db.scalar(
            select(func.coalesce(func.sum(DevBoxResource.size_gb), 0))
            .join(DevBox, DevBox.id == DevBoxResource.box_id)
            .where(
                *box_scope,
                DevBoxResource.deletion_state != "verified_deleted",
                (DevBox.status == "released") | (DevBoxResource.resource_kind == "snapshot"),
            )
        )
        unknown_size = await db.scalar(
            select(func.count(DevBoxResource.id))
            .join(DevBox, DevBox.id == DevBoxResource.box_id)
            .where(
                *box_scope,
                DevBoxResource.deletion_state != "verified_deleted",
                DevBoxResource.resource_kind.in_(("workspace_disk", "snapshot")),
                DevBoxResource.size_gb.is_(None),
            )
        )
        if unknown_size:
            raise HTTPException(403, "Dev Box retained storage size is unknown")
        storage = Decimal(reserved_disk + extra_disk + requested_disk_gb)
        charged_cost = await db.scalar(
            select(func.coalesce(func.sum(DevBoxUsage.cost_usd), 0)).join(DevBox, DevBox.id == DevBoxUsage.box_id).where(*box_scope)
        )
        charged_hours = await db.scalar(
            select(func.coalesce(func.sum(DevBoxUsage.units), 0))
            .join(DevBox, DevBox.id == DevBoxUsage.box_id)
            .join(DevBoxResource, DevBoxResource.id == DevBoxUsage.resource_id)
            .where(*box_scope, DevBoxResource.resource_kind == "instance")
        )
        rates = (policy.max_lease_minutes, policy.instance_hour_rate_usd, policy.disk_gb_hour_rate_usd, policy.snapshot_gb_hour_rate_usd)
        if org_id is None:
            rates = (
                await db.execute(
                    select(
                        func.max(DevBoxPolicy.max_lease_minutes),
                        func.max(DevBoxPolicy.instance_hour_rate_usd),
                        func.max(DevBoxPolicy.disk_gb_hour_rate_usd),
                        func.max(DevBoxPolicy.snapshot_gb_hour_rate_usd),
                    ).where(DevBoxPolicy.deployment_id == deployment)
                )
            ).one()
        lease_minutes, instance_rate, disk_rate, snapshot_rate = rates
        if any(value is None or value <= 0 for value in rates):
            raise HTTPException(403, "Dev Box deployment pricing is incomplete")
        current_running = running if org_id is not None else deployment_running
        pending_hours = Decimal(current_running + (0 if admitted else 1)) * Decimal(lease_minutes) / Decimal(60)
        if storage > storage_limit or Decimal(charged_hours) + pending_hours > time_limit:
            raise HTTPException(403, "Dev Box storage or running-time capacity reached")
        allowance = pending_hours * instance_rate + storage * Decimal(24) * max(disk_rate, snapshot_rate)
        if Decimal(charged_cost) + allowance > budget_limit:
            raise HTTPException(403, "Dev Box budget allowance exhausted")
