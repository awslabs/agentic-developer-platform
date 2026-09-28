"""Read paid workload reservations without interpreting them as provider bills."""

from datetime import UTC, datetime

from fastapi import HTTPException
from harness_jobs.identity import decode_payload, payload_digest

from app.adapters.operation_authority_source import GrantBackedAuthority
from app.database import async_session_factory
from app.services.deployment_operations import composition, intent_for, stored_preview


async def accounting(request, db, org_id, workspace_id, deployment_id, *, kind):
    authority = GrantBackedAuthority(async_session_factory)

    async def permitted():
        if (
            await authority.resolve(
                org_id=str(org_id),
                workspace_id=str(workspace_id),
                permission="workspace:read",
            )
            is None
        ):
            raise HTTPException(403, "workload accounting access refused")

    await permitted()
    intent = await intent_for(
        db, org_id, workspace_id, deployment_id, workload_kind=kind
    )
    original = stored_preview(intent)
    allocation_id = original.request.parameters["allocation_id"]
    owner = composition(request)
    async with owner.operation_connect() as connection:
        # One snapshot across both ledgers and operations. These are reads only;
        # no provider query, settlement or quota update occurs in this request.
        async with connection.transaction(isolation="repeatable_read", readonly=True):
            rows = await connection.fetch(
                "SELECT r.action,r.operation_id,r.source_operation_id,r.allocation_id,r.request_id,"
                "r.plan_digest,o.request_payload,o.state AS operation_state,"
                "a.reservation_state AS shared_state,a.max_cost_micros AS approved_cost,"
                "a.max_resource_units AS approved_units,a.max_runtime_seconds AS approved_runtime,"
                "b.state AS budget_state,b.max_cost_micros,b.max_resource_units,b.max_runtime_seconds,b.updated_at "
                "FROM controller_deployment_operations r JOIN harness_operations o "
                "ON o.operation_id=r.operation_id AND o.org_id=r.org_id AND o.workspace_id=r.workspace_id AND o.plan_digest=r.plan_digest "
                "JOIN harness_approval_consumption a ON a.operation_id=o.operation_id "
                "AND a.org_id=o.org_id AND a.workspace_id=o.workspace_id AND a.plan_digest=o.plan_digest "
                "JOIN operation_budget_reservations b ON b.reservation_id=a.reservation_id "
                "AND b.org_id=o.org_id AND b.workspace_id=o.workspace_id AND b.job_id=o.job_id AND b.attempt_id=o.attempt_id "
                "WHERE r.deployment_id=$1 AND r.org_id=$2 AND r.workspace_id=$3 ORDER BY r.action LIMIT 3",
                str(deployment_id),
                str(org_id),
                str(workspace_id),
            )
            registered = await connection.fetchval(
                "SELECT count(*) FROM controller_deployment_operations WHERE deployment_id=$1 AND org_id=$2 AND workspace_id=$3",
                str(deployment_id),
                str(org_id),
                str(workspace_id),
            )
            source = next((row for row in rows if row["action"] == "provision"), None)
            if source is None or len(rows) > 2 or len(rows) != registered:
                raise HTTPException(503, "original workload accounting is unavailable")
            operations = []
            for row in rows:
                payload = decode_payload(row["request_payload"])
                if (
                    row["allocation_id"] != allocation_id
                    or payload_digest(payload) != row["plan_digest"]
                    or payload.action != row["action"]
                    or payload.idempotency_key != row["request_id"]
                    or payload.parameters.get("allocation_id") != allocation_id
                    or (row["action"] == "provision" and payload != original.request)
                    or (
                        row["action"] == "teardown"
                        and (
                            row["source_operation_id"] != source["operation_id"]
                            or payload.parameters.get("controller_source_operation_id")
                            != source["operation_id"]
                        )
                    )
                    or row["budget_state"]
                    not in {"reserved", "confirmed", "released", "retained"}
                    or row["shared_state"]
                    not in {"reserved", "confirmed", "released", "retained"}
                    or any(
                        row[column] != row[approved]
                        for column, approved in (
                            ("max_cost_micros", "approved_cost"),
                            ("max_resource_units", "approved_units"),
                            ("max_runtime_seconds", "approved_runtime"),
                        )
                    )
                ):
                    raise HTTPException(503, "workload accounting binding changed")
                # A lost ledger acknowledgement may leave different states. Keep
                # that distinction visible and conservatively retain the ceiling.
                released = row["budget_state"] == row["shared_state"] == "released"
                operations.append(
                    {
                        "action": row["action"],
                        "operation_id": row["operation_id"],
                        "operation_state": row["operation_state"],
                        "approved_max_cost_micros": str(row["approved_cost"]),
                        "max_resource_units": row["approved_units"],
                        "max_runtime_seconds": row["approved_runtime"],
                        "budget_state": row["budget_state"],
                        "shared_reservation_state": row["shared_state"],
                        "budget_held_micros": str(
                            0 if released else row["approved_cost"]
                        ),
                        "accounting_consistent": row["budget_state"]
                        == row["shared_state"],
                        "updated_at": row["updated_at"],
                    }
                )
            resources = await connection.fetch(
                "SELECT CASE WHEN kind IN ('compute','storage','network') THEN kind ELSE 'other' END AS category,count(*) AS count "
                "FROM harness_allocation_resource WHERE org_id=$1 AND workspace_id=$2 AND allocation_id=$3 GROUP BY category ORDER BY category",
                str(org_id),
                str(workspace_id),
                allocation_id,
            )
            total = await connection.fetchval(
                "SELECT COALESCE(sum(max_cost_micros),0) FROM operation_budget_reservations WHERE org_id=$1 AND workspace_id=$2 AND state IN ('reserved','confirmed','retained')",
                str(org_id),
                str(workspace_id),
            )
    limits = await authority.budget_limits_for(
        org_id=str(org_id), workspace_id=str(workspace_id)
    )
    await permitted()
    await db.refresh(intent)
    cap = limits.max_cost_micros if limits is not None else None
    return {
        "workspace_id": str(workspace_id),
        "deployment_id": str(deployment_id),
        "kind": kind,
        "allocation_id": allocation_id,
        "checked_at": datetime.now(UTC),
        "operations": operations,
        "recorded_resources": [
            {"kind": row["category"], "count": row["count"]} for row in resources
        ],
        # PostgreSQL SUM(bigint) is numeric and asyncpg may return Decimal with
        # exponent notation. The sum is integral micros; emit canonical digits
        # without converting through float or rejecting valid large balances.
        "workspace_committed_budget_micros": str(int(total)),
        "workspace_reservation_cap_micros": str(cap) if cap is not None else None,
        "workspace_budget_state": "unconfigured"
        if cap is None
        else ("exhausted" if total >= cap else "available"),
        "estimated_cost_micros": None,
        "observed_cost_micros": None,
        "cost_reconciliation": "unavailable",
        "cleanup_status": "confirmed"
        if intent.status == "Deleted"
        else (
            "not-required"
            if intent.status == "CancelledBeforeDispatch"
            else "unconfirmed"
        ),
    }
