"""Persist non-overlapping infrastructure usage from verified resource intervals."""

from datetime import UTC, timedelta
from decimal import Decimal

from sqlalchemy import select

from src.shared.models.base import new_uuid
from src.shared.models.dev_box import DevBoxResource, DevBoxUsage

PRECISION = Decimal("0.000001")
# Longest span one usage row may cover when contiguous observations are coalesced (#9548).
COALESCE_WINDOW = timedelta(hours=1)


def utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def record_interval(db, resource_id, start, end, rate, source_revision):
    resource = await db.scalar(select(DevBoxResource).where(DevBoxResource.id == resource_id).with_for_update())
    if resource is None or resource.resource_kind not in ("instance", "workspace_disk", "snapshot"):
        raise ValueError("A recorded infrastructure resource is required")
    if (not resource.provider_resource_id or resource.provider_resource_id.startswith("intent:")
            or resource.deletion_state == "verified_deleted"):
        raise ValueError("Provider ownership must be verified before accounting")
    start, end, rate = utc(start), utc(end), Decimal(str(rate))
    if end <= start or not rate.is_finite() or rate <= 0 or not source_revision or len(source_revision) > 128:
        raise ValueError("A bounded, priced provider interval is required")
    if resource.resource_kind != "instance" and (resource.size_gb is None or resource.size_gb <= 0):
        raise ValueError("Storage size is unknown")
    recorded = list((await db.scalars(select(DevBoxUsage).where(
        DevBoxUsage.resource_id == resource.id, DevBoxUsage.interval_end > start, DevBoxUsage.interval_start < end,
    ).order_by(DevBoxUsage.interval_start))).all())
    cursor = start
    additions = []
    for interval in recorded:
        covered_from = utc(interval.interval_start)
        covered_to = utc(interval.interval_end)
        if covered_from > cursor:
            additions.append((cursor, min(covered_from, end)))
        cursor = max(cursor, covered_to)
        if cursor >= end:
            break
    if cursor < end:
        additions.append((cursor, end))
    # #9548: one row per observation pass (every ~7 s on dev) grew to 939 232 rows for one box in two
    # weeks. A new interval that starts exactly where the latest unreconciled, same-rate row of this
    # resource ends extends that row instead, up to COALESCE_WINDOW per row, so a resource accrues at
    # most a handful of rows per hour while sums, non-overlap and the hourly alignment stay exact.
    # Reconciled rows, rate changes and gaps always start a new row.
    extended = 0
    if additions:
        first_start, first_end = additions[0]
        # The row ending exactly at the new start is not in `recorded` (no overlap); fetch it.
        latest = await db.scalar(select(DevBoxUsage).where(
            DevBoxUsage.resource_id == resource.id, DevBoxUsage.interval_end == first_start,
        ).order_by(DevBoxUsage.interval_start.desc()).limit(1))
        if (latest is not None and not latest.reconciled and latest.rate_usd == rate
                and latest.source_revision == source_revision
                and (first_end - utc(latest.interval_start)) <= COALESCE_WINDOW):
            hours = Decimal(str((first_end - utc(latest.interval_start)).total_seconds())) / Decimal("3600")
            units = hours * (resource.size_gb if resource.resource_kind != "instance" else 1)
            latest.interval_end = first_end
            latest.units = units.quantize(PRECISION)
            latest.cost_usd = (units * rate).quantize(PRECISION)
            additions = additions[1:]
            extended = 1
    for interval_start, interval_end in additions:
        hours = Decimal(str((interval_end - interval_start).total_seconds())) / Decimal("3600")
        units = hours * (resource.size_gb if resource.resource_kind != "instance" else 1)
        db.add(DevBoxUsage(
            id=new_uuid(), box_id=resource.box_id, resource_id=resource.id, user_id=resource.user_id,
            org_id=resource.org_id, deployment_id=resource.deployment_id,
            machine_generation=resource.machine_generation, interval_start=interval_start,
            interval_end=interval_end, units=units.quantize(PRECISION),
            unit_name="gb_hour" if resource.resource_kind != "instance" else "hour",
            rate_usd=rate, cost_usd=(units * rate).quantize(PRECISION),
            source_revision=source_revision, reconciled=False, currency="USD",
        ))
    await db.flush()
    return len(additions) + extended
