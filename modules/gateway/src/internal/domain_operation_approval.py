"""Re-read the domain's persisted approval and exact human grant boundary."""

import json
from datetime import UTC, datetime

from fastapi import HTTPException

from src.internal.domain_operation_store import harness


async def current_approval(connection, operation, request, *, pending_recovery=False):
    try:
        return await _current_approval(connection, operation, request, pending_recovery=pending_recovery)
    except (ValueError, KeyError, TypeError, PermissionError):
        # Malformed persisted approval is an authority refusal, including when a
        # deciding human is no longer one of its selected approvers. Never leak a
        # shared contract exception through the execution dependency boundary.
        raise HTTPException(403, "current domain approval invalid") from None


def bootstrap_runtime_review(operation, original, renewal, *, pending_recovery):
    """Bind a runtime amendment to its fresh decision and immutable retry receipt."""
    scope = dict(renewal).get("scope", "undispatched")
    raw = dict(renewal).get("amendment_json")
    terms = json.loads(raw) if raw is not None else None
    contract = harness("bootstrap_runtime_authorization")
    if scope == "unstarted-bootstrap":
        settled_review = None
        if isinstance(terms, dict) and terms.get("kind") == "settled-bootstrap-review-v1":
            settled_review = terms
            terms = harness("settled_bootstrap").review_runtime(terms)
            operation["bootstrap_settled_review"] = settled_review
        if terms is not None:
            if original["max_runtime_seconds"] != operation["max_runtime_seconds"]:
                raise ValueError("original runtime changed")
            if settled_review is not None:
                contract.settled_runtime_amendment(terms, operation["max_runtime_seconds"])
            else:
                contract.runtime_amendment(terms, operation["max_runtime_seconds"])
        if not pending_recovery:
            proof = json.loads(operation.get("provider_recovery_proof") or "{}")
            _, deadline = contract.runtime_authorization(proof, operation["max_runtime_seconds"], scope)
            if terms != proof.get("runtime_amendment") or (terms is not None and deadline != renewal["expires_at"]):
                raise ValueError("runtime receipt differs from current human decision")
            if settled_review is not None:
                closure = harness("settled_bootstrap").validate(proof.get("settled_bootstrap"))
                if harness("settled_bootstrap").digest(closure) != settled_review["closure_sha256"]:
                    raise ValueError("settled closure differs from current human decision")
                if proof.get("provenance_assumption") != settled_review.get("provenance_assumption"):
                    raise ValueError("diagnostic assumption differs from current human decision")
            elif "settled_bootstrap" in proof:
                raise ValueError("settled closure requires explicit review")
        operation["bootstrap_runtime_amendment"] = terms
    elif isinstance(terms, dict) and terms.get("kind") in {"bootstrap-runtime-extension-v1", contract.SETTLED_REAUTHORIZATION_KIND}:
        raise ValueError("runtime amendment requires bootstrap scope")


async def _current_approval(connection, operation, request, *, pending_recovery=False):
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
    row = dict(row)
    if operation.get("reauthorization_id"):
        renewal = await connection.fetchrow(
            "SELECT * FROM operation_reauthorizations WHERE reauthorization_id=$1",
            operation["reauthorization_id"],
        )
        if (
            renewal is None
            or row["revoked"]
            or row["result"] != "allowed-once"
            or renewal["revoked"]
            or renewal["result"] != "allowed-once"
            or renewal["original_approval_id"] != row["approval_id"]
            or renewal["operation_id"] != operation["operation_id"]
            or renewal["expected_version"] != operation.get("recovery_expected_version")
            or dict(renewal).get("scope", "undispatched") != operation.get("recovery_scope", "undispatched")
            or any(renewal[key] != operation[key] for key in ("org_id", "workspace_id", "requester", "approved_by", "plan_digest"))
        ):
            raise HTTPException(403, "current reauthorization refused")
        bootstrap_runtime_review(operation, row, renewal, pending_recovery=pending_recovery)
        if dict(renewal).get("scope") == "partial-apply-repair":
            terms = operation.get("partial_apply_review")
            if terms is None and operation.get("provider_recovery_proof"):
                proof = json.loads(operation["provider_recovery_proof"])
                from src.shared.domain_provider_contract import digest

                terms = {
                    "version": 1,
                    "owner_evidence_id": proof["owner_evidence_id"],
                    "owner_evidence_sha256": digest(proof["owner_evidence"]),
                    "repair": proof["repair"],
                }
            if terms is None or json.loads(renewal["amendment_json"]) != terms:
                raise HTTPException(403, "current exact repair plan approval refused")
        # Evaluate the actual fresh decision, while retaining the original ID in
        # paid admission/lineage. The old approval row and expiry are never edited.
        row.update(approval_id=renewal["reauthorization_id"], expires_at=renewal["expires_at"], decided_at=renewal["decided_at"])
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
        requires_distinct_approver=row.get("requires_distinct_approver", True),
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
