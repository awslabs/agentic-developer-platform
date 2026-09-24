"""Re-read the domain's persisted approval and exact human grant boundary."""

import json
from datetime import UTC, datetime

from fastapi import HTTPException

from src.internal.domain_operation_store import harness


async def current_approval(connection, operation, request):
    try:
        return await _current_approval(connection, operation, request)
    except (ValueError, KeyError, TypeError):
        # Malformed persisted approval is an authority refusal, including when a
        # deciding human is no longer one of its selected approvers. Never leak a
        # shared contract exception through the execution dependency boundary.
        raise HTTPException(403, "current domain approval invalid") from None


async def _current_approval(connection, operation, request):
    row = await connection.fetchrow(
        "SELECT * FROM operation_approvals WHERE approval_id=$1 AND org_id=$2 AND workspace_id=$3 AND requester=$4 AND plan_digest=$5",
        operation["approval_id"],
        operation["org_id"],
        operation["workspace_id"],
        operation["requester"],
        operation["plan_digest"],
    )
    if row is None or row["decided_by"] != operation["approved_by"]:
        raise HTTPException(403, "original domain approval unavailable")
    identity, approval = harness("identity"), harness("approval")
    if identity.payload_digest(identity.decode_payload(row["request_payload"])) != operation["plan_digest"]:
        raise HTTPException(403, "domain approval request changed")
    workspace_rows = await connection.fetch(
        "SELECT principal,permissions,revoked_at FROM workspace_grants WHERE workspace_id::text=$1 AND org_id::text=$2 AND principal_type='human'",
        operation["workspace_id"],
        operation["org_id"],
    )
    statuses = {
        grant["principal"]: approval.ApproverStatus(
            grant["principal"], True, frozenset(grant["permissions"].split()), grant["revoked_at"] is not None
        )
        for grant in workspace_rows
    }
    exists = await connection.fetchval("SELECT EXISTS(SELECT 1 FROM workspaces WHERE id::text=$1)", operation["workspace_id"])
    # Preserve the domain's precedence: any explicit workspace grant, including a
    # revoked one, answers first. Organization creation scope was persisted before
    # the workspace existed and does not disappear when registration inserts it.
    if row["organization_scope"] or not exists:
        org_rows = await connection.fetch(
            "SELECT principal,permissions,revoked_at FROM organization_grants WHERE org_id::text=$1 AND principal_type='human'",
            operation["org_id"],
        )
        for grant in org_rows:
            if grant["principal"] not in statuses and "organization:administer" in grant["permissions"].split():
                statuses[grant["principal"]] = approval.ApproverStatus(
                    grant["principal"], True, frozenset({"workspace:administer"}), grant["revoked_at"] is not None
                )
    requester = statuses.get(operation["requester"])
    if requester is None or requester.revoked or not requester.permissions.intersection({"workspace:administer", "workspace:provision"}):
        raise HTTPException(403, "original domain requester authority withdrawn")
    record = approval.ApprovalRecord(
        approval_id=row["approval_id"],
        binding=approval.ApprovalBinding(row["org_id"], row["workspace_id"], row["plan_digest"], row["requester"]),
        envelope=approval.SpendEnvelope(row["max_resource_units"], row["max_runtime_seconds"], row["max_cost_micros"]),
        result=approval.ApprovalResult(row["result"]),
        approvers=frozenset(json.loads(row["approvers_json"])),
        decided_by=row["decided_by"],
        decided_at=row["decided_at"],
        expires_at=row["expires_at"],
        revoked=row["revoked"],
    )
    decision = approval.evaluate_approval(
        record,
        principal=identity.ResolvedPrincipal(operation["org_id"], operation["workspace_id"], operation["requester"], requester.permissions),
        request=request,
        requested_envelope=approval.SpendEnvelope(operation["max_resource_units"], operation["max_runtime_seconds"], operation["max_cost_micros"]),
        approver_statuses=statuses,
        now=datetime.now(UTC),
    )
    if not decision.permitted:
        raise HTTPException(403, "current domain approval refused")
    return record.expires_at
