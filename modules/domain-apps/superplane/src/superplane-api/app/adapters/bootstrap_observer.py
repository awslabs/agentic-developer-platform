"""Publish one native observation task while paid admission remains disabled.

The original authenticated requester supplies the operation via ApprovalService.
The producer signs the ordinary recovery dispatch request. Gateway independently
rechecks original humans, the expired lease, budget and deterministic generation;
this function never creates a reservation, decides a ticket or reopens a lease.
"""

from datetime import UTC, datetime

from harness_jobs.identity import decode_payload, payload_digest

from app.adapters.operation_dispatch import _RECOVERABLE
from app.operation_activation import dispatch_enabled
from app.services.operation_approvals import ApprovalDenied

FIELDS = ("operation_id", "job_id", "attempt_id", "org_id", "workspace_id")


async def publish(dispatcher, original, *, expected_version, expected_fence):
    if dispatcher is None or dispatcher.enabled or dispatch_enabled():
        raise ApprovalDenied("bounded observer requires paused paid dispatch")
    async with dispatcher.connect() as connection:
        row = await connection.fetchrow(
            _RECOVERABLE
            + " AND o.operation_id=$1 AND o.org_id=$2 AND o.workspace_id=$3",
            original["operation_id"],
            original["org_id"],
            original["workspace_id"],
        )
        lease = await connection.fetchrow(
            "SELECT * FROM harness_operation_leases WHERE operation_id=$1",
            original["operation_id"],
        )
    if (
        row is None
        or lease is None
        or row["mode"] != "recovery"
        or row["state"] != "running"
        or row["version"] != expected_version
        or lease["fence_token"] != expected_fence
        or lease["closed_at"] is not None
        or lease["holder"] is None
        or min(lease["expires_at"], lease["runtime_deadline"]) > datetime.now(UTC)
        or row["cleanup_required"]
        or row["cancel_requested_at"] is not None
        or any(
            row[k] != original[k] for k in (*FIELDS, "plan_digest", "request_payload")
        )
    ):
        raise ApprovalDenied("original expired bootstrap changed")
    request = decode_payload(row["request_payload"])
    if (
        request.action != "provision"
        or request.parameters.get("lifecycle_phase") != "bootstrap-workspace"
        or payload_digest(request) != row["plan_digest"]
    ):
        raise ApprovalDenied("observer requires the original bootstrap admission")
    registration = await dispatcher._domain_registration(row)
    policy = await dispatcher._policy(row["org_id"])
    if (
        registration is None
        or registration["adp_org_id"] != policy.adp_org_id
        or not await dispatcher._budget_current(row)
        or not await dispatcher._registration(row)
    ):
        raise ApprovalDenied("original bootstrap registration or budget changed")
    body = {"domain": "superplane", "mode": "recovery", **{k: row[k] for k in FIELDS}}
    # No admission-enable override: only this fixed recovery mode reaches the
    # existing protected producer. A lost response retains its deterministic map.
    data = await dispatcher.transport.post(
        "/dispatch",
        {
            **body,
            "expected_version": expected_version,
            "expected_fence": expected_fence,
        },
    )
    try:
        if (
            any(data.get(k) != v for k, v in body.items())
            or data.get("version") != 1
            or data.get("status") != "pending"
            or data.get("domain_org_id") != row["org_id"]
            or data.get("adp_org_id") != policy.adp_org_id
            or not isinstance(data.get("invocation_id"), str)
            or not data["invocation_id"]
            or data.get("principal") != data["invocation_id"] + "#1"
            or datetime.fromisoformat(data["not_after"]) <= datetime.now(UTC)
        ):
            raise ValueError()
    except (KeyError, ValueError, TypeError):
        raise RuntimeError("observe the original recovery dispatch outcome") from None
    return {
        **data,
        "observation_only": True,
        "source_fence": expected_fence,
        "source_version": expected_version,
        "selector": {k: row[k] for k in FIELDS},
    }
