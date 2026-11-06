"""Owner-scoped infrastructure summaries without provider identifiers or credentials."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select

from src.shared.models.dev_box import DevBoxOperation, DevBoxPolicy, DevBoxResource, DevBoxUsage

PRECISION = Decimal("0.000001")


def _money(value) -> Decimal:
    """Usage rows are stored quantized to six places; keep the aggregate on the same scale on every backend."""
    return (Decimal(0) if value is None else Decimal(str(value))).quantize(PRECISION)


@dataclass(frozen=True)
class UsageGroup:
    reconciled: bool
    resource_kind: str | None
    cost_usd: Decimal
    through: datetime | None
    count: int


async def infrastructure_status(db, box):
    identity = (box.user_id, box.org_id, box.deployment_id)
    resources = list(
        (
            await db.scalars(
                select(DevBoxResource).where(
                    DevBoxResource.box_id == box.id,
                    DevBoxResource.user_id == identity[0],
                    DevBoxResource.org_id == identity[1],
                    DevBoxResource.deployment_id == identity[2],
                )
            )
        ).all()
    )
    # Usage is aggregated in SQL. The controller accrues one row per resource per observation pass
    # (about every 7 s on dev), so a two-week-old box owned 939 232 rows on 2026-11-03 and loading
    # them here made every GET /cloud-desktops read take ~52 s (#9548). Sums and maxima grouped by
    # reconciliation state and resource kind are everything the summary below ever used.
    usage_rows = (
        await db.execute(
            select(
                DevBoxUsage.reconciled,
                DevBoxResource.resource_kind,
                func.sum(DevBoxUsage.cost_usd),
                func.max(DevBoxUsage.interval_end),
                func.count(),
            )
            .outerjoin(DevBoxResource, DevBoxResource.id == DevBoxUsage.resource_id)
            .where(
                DevBoxUsage.box_id == box.id,
                DevBoxUsage.user_id == identity[0],
                DevBoxUsage.org_id == identity[1],
                DevBoxUsage.deployment_id == identity[2],
            )
            .group_by(DevBoxUsage.reconciled, DevBoxResource.resource_kind)
        )
    ).all()
    usage = [UsageGroup(bool(reconciled), kind, _money(cost), through, int(count)) for reconciled, kind, cost, through, count in usage_rows]
    estimates = _money(sum(group.cost_usd for group in usage if not group.reconciled))
    billed = [group for group in usage if group.reconciled]
    reconciled = _money(sum(group.cost_usd for group in billed))
    unknown = not resources or any(
        resource.coverage_gap_at is not None or (resource.deletion_state != "verified_deleted" and resource.last_observed_state in (None, "unknown"))
        for resource in resources
    )
    retained = [
        resource
        for resource in resources
        if resource.resource_kind in ("workspace_disk", "snapshot") and resource.deletion_state != "verified_deleted"
    ]
    operation = await db.scalar(
        select(DevBoxOperation)
        .where(
            DevBoxOperation.box_id == box.id,
            DevBoxOperation.user_id == identity[0],
            DevBoxOperation.org_id == identity[1],
            DevBoxOperation.deployment_id == identity[2],
        )
        .order_by(DevBoxOperation.created_at.desc(), DevBoxOperation.id.desc())
        .limit(1)
    )
    policy = await db.scalar(
        select(DevBoxPolicy).where(
            DevBoxPolicy.org_id == identity[1],
            DevBoxPolicy.deployment_id == identity[2],
        )
    )
    if any(resource.resource_kind == "snapshot" and resource.deletion_state != "verified_deleted" for resource in resources):
        unknown = unknown or (
            policy is None
            or policy.snapshot_gb_hour_rate_usd is None
            or any(resource.size_gb is None for resource in retained if resource.resource_kind == "snapshot")
        )
    pending = sum(
        resource.retention_state == "delete_requested"
        or resource.deletion_state == "deleting"
        or (box.desired_state == "released" and resource.deletion_state == "unknown")
        for resource in resources
        if resource.deletion_state != "verified_deleted"
    )
    if operation and operation.action == "release" and operation.status in ("pending", "unknown"):
        pending += 1
    breakdown = {}
    for kind in ("instance", "workspace_disk", "snapshot"):
        records = [group for group in usage if group.resource_kind == kind]
        breakdown[kind] = {
            "known_cost_usd": str(_money(sum(group.cost_usd for group in records))),
            "estimated_cost_usd": str(_money(sum(group.cost_usd for group in records if not group.reconciled))),
            "covered_until": max((group.through for group in records if group.through is not None), default=None),
        }
        if kind == "snapshot":
            active = [resource for resource in retained if resource.resource_kind == "snapshot"]
            breakdown[kind]["storage_gb"] = (
                sum(resource.size_gb for resource in active) if all(resource.size_gb is not None for resource in active) else None
            )
            breakdown[kind]["rate_usd_per_gb_hour"] = (
                str(policy.snapshot_gb_hour_rate_usd) if policy and policy.snapshot_gb_hour_rate_usd is not None else None
            )
    return {
        "known_cost_usd": str(estimates + reconciled),
        "estimated_cost_usd": str(estimates),
        "reconciled_cost_usd": str(reconciled) if billed else None,
        "total_cost_usd": None if unknown or not usage else str(estimates + reconciled),
        "coverage": "partial_unknown" if unknown else ("reconciled" if billed and not estimates else "estimated"),
        "observed_at": max((resource.last_observed_at for resource in resources if resource.last_observed_at), default=None),
        "estimated_through": max((group.through for group in usage if not group.reconciled and group.through is not None), default=None),
        "reconciled_through": max((group.through for group in billed if group.through is not None), default=None),
        "retained_resources": [
            {"kind": resource.resource_kind, "size_gb": resource.size_gb, "deletion_state": resource.deletion_state} for resource in retained
        ],
        "pending_cleanup_count": pending,
        "policy_denial_reason": (
            "admission_disabled"
            if not policy or not policy.enabled
            else operation.error_class
            if operation and operation.error_class in ("quota_changed", "entitlement_revoked", "workspace_policy_missing")
            else None
        ),
        "by_kind": breakdown,
    }
