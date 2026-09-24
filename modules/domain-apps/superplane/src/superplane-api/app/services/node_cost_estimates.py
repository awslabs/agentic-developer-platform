"""Estimates for recorded node intervals; never provider-reconciled charges."""

from dataclasses import dataclass
from datetime import timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import HTTPException
from sqlalchemy import or_, select

from app.models.node import Node


@dataclass(frozen=True)
class NodeCostEstimate:
    values: dict[str, Any]
    total_usd: Decimal | None


def utc(value):
    if value is None:
        return None
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def window(start, end, now):
    start, end, now = utc(start), utc(end), utc(now)
    end = min(end, now) if end is not None else now
    if start is not None and start >= end:
        raise HTTPException(422, "Cost window start must precede its end")
    return start, end


async def workspace_nodes(db, workspace, start, end):
    if not workspace.cluster_id:
        return []
    conditions = [
        Node.org_id == workspace.org_id,
        Node.cluster_id == workspace.cluster_id,
        or_(Node.created_at.is_(None), Node.created_at <= end),
    ]
    if start is not None:
        # Include nodes created before the window that continued running in it.
        conditions.append(
            or_(
                Node.created_at.is_(None),
                Node.terminated_at.is_(None),
                Node.terminated_at >= start,
                Node.terminated_at < Node.created_at,
            )
        )
    result = await db.execute(
        select(Node).where(*conditions).order_by(Node.created_at.desc())
    )
    return list(result.scalars().all())


def money(value):
    return str(value.quantize(Decimal("0.01"))) if value is not None else None


def estimate(nodes, start, end):
    total = Decimal("0")
    missing = 0
    details = []
    gpu, cloud = {}, {}
    for node in nodes:
        created, terminated = utc(node.created_at), utc(node.terminated_at)
        rate = node.hourly_cost_usd
        if rate is not None:
            rate = Decimal(str(rate))
            if not rate.is_finite() or rate < 0:
                rate = None
        hours = None
        if created is not None and (terminated is None or terminated >= created):
            begin = max(created, start) if start is not None else created
            finish = min(terminated, end) if terminated is not None else end
            duration = max(finish - begin, timedelta(0))
            micros = (
                duration.days * 86400 + duration.seconds
            ) * 1_000_000 + duration.microseconds
            hours = Decimal(micros) / Decimal(3_600_000_000)
        cost = rate * hours if rate is not None and hours is not None else None
        if cost is None:
            missing += 1
        else:
            total += cost
        for buckets, key in (
            (gpu, node.gpu_type or "unknown"),
            (cloud, node.cloud or "unknown"),
        ):
            previous = buckets.get(key, Decimal("0"))
            buckets[key] = (
                previous + cost if previous is not None and cost is not None else None
            )
        details.append(
            {
                "node_id": str(node.id),
                "name": node.k8s_node_name or node.instance_id or str(node.id),
                "gpu_type": node.gpu_type,
                "gpu_count": node.gpu_count,
                "cloud": node.cloud,
                "region": node.region,
                "hourly_cost_usd": str(rate) if rate is not None else None,
                "hours_running": money(hours),
                "total_cost_usd": money(cost),
                "status": node.status,
                "created_at": created.isoformat() if created is not None else None,
                "terminated_at": terminated.isoformat()
                if terminated is not None
                else None,
            }
        )
    available = bool(nodes) and missing == 0
    values = {
        "total_cost_usd": money(total) if available else None,
        "known_subtotal_usd": money(total),
        "estimate_status": "available"
        if available
        else ("partial" if len(nodes) > missing else "unavailable"),
        "cost_basis": "recorded_node_rates",
        "observed_cost_usd": None,
        "cost_reconciliation": "unavailable",
        "unestimated_node_count": missing,
        "node_count": len(nodes),
        "nodes": details,
        "breakdown_by_gpu": {key: money(value) for key, value in gpu.items()},
        "breakdown_by_cloud": {key: money(value) for key, value in cloud.items()},
    }

    return NodeCostEstimate(values, total if available else None)
