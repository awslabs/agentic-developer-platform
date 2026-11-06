"""Read an independently approved allocation cap without rewriting original admission."""

import json
from datetime import UTC, datetime
from decimal import Decimal

from .artifacts import canonical, native_lifecycle_phase
from .runtime_config import LifecycleRefused


async def approved_budget(
    operation_connect,
    domain_connect,
    *,
    operation_id,
    org_id,
    workspace_id,
    plan_digest,
    parameters,
):
    async with domain_connect() as connection:
        rows = await connection.fetch(
            "SELECT * FROM operation_reauthorizations "
            "WHERE operation_id=$1 AND org_id=$2 AND workspace_id=$3 "
            "AND scope='planning-budget' AND result='allowed-once'",
            operation_id,
            org_id,
            workspace_id,
        )
    if not rows:
        return None
    if len(rows) != 1:
        raise LifecycleRefused("ambiguous planning budget amendment")
    row = dict(rows[0])
    async with domain_connect() as connection:
        authority = await connection.fetchrow(
            "SELECT a.revoked AS original_revoked,a.result AS original_result,"
            "a.plan_digest AS original_digest,w.budget_max_daily_usd AS workspace_daily "
            "FROM operation_approvals a JOIN workspaces w ON w.id::text=$2 AND w.org_id::text=$3 "
            "WHERE a.approval_id=$1 AND a.workspace_id=$2 AND a.org_id=$3",
            row["original_approval_id"],
            workspace_id,
            org_id,
        )
    if authority is None:
        raise LifecycleRefused(
            "planning budget source approval or workspace unavailable"
        )
    row.update(authority)
    terms = json.loads(row["amendment_json"])
    async with operation_connect() as connection:
        original = await connection.fetchrow(
            "SELECT c.approval_id,c.requester,c.approved_by,o.plan_digest,o.version "
            "FROM harness_approval_consumption c JOIN harness_operations o USING(operation_id) "
            "WHERE c.operation_id=$1 AND o.org_id=$2 AND o.workspace_id=$3",
            operation_id,
            org_id,
            workspace_id,
        )
        receipt = await connection.fetchrow(
            "SELECT * FROM harness_provider_retries WHERE operation_id=$1 "
            "ORDER BY source_fence DESC LIMIT 1",
            operation_id,
        )
    confirmation = await _consumed_confirmation(
        domain_connect, row, terms, original, receipt
    )
    now = datetime.now(UTC)
    if (
        original is None
        or row["revoked"]
        or row["original_revoked"]
        or row["original_result"] != "allowed-once"
        or row["decided_at"] is None
        or row["decided_at"] > now
        or (now >= row["expires_at"] and confirmation is None)
        or row["plan_digest"] != plan_digest
        or row["original_digest"] != plan_digest
        or original["plan_digest"] != plan_digest
        or original["approval_id"] != row["original_approval_id"]
        or original["requester"] != row["requester"]
        or original["approved_by"] != row["approved_by"]
        or native_lifecycle_phase(parameters) != "prepare-infrastructure"
        or parameters.get("lifecycle_artifact_id")
        or set(terms)
        != {
            "version",
            "previous_daily_micros",
            "daily_micros",
            "previous_workspace_daily_micros",
        }
        or any(type(terms[k]) is not int for k in terms)
        or terms["version"] != 1
        or terms["previous_daily_micros"]
        != int(parameters["lifecycle_allocation_max_cost_micros"])
        or not terms["previous_daily_micros"] < terms["daily_micros"] <= 99999999990000
        or Decimal(row["workspace_daily"] or 0) * 1000000 < terms["daily_micros"]
    ):
        raise LifecycleRefused(
            "planning budget amendment is not current original authority"
        )
    return {
        "reauthorization_id": row["reauthorization_id"],
        "source_operation_id": operation_id,
        "source_plan_digest": plan_digest,
        "requester": row["requester"],
        "approved_by": row["approved_by"],
        "decided_at": row["decided_at"].isoformat(),
        **terms,
        **({"planning_budget_confirmation": confirmation} if confirmation else {}),
    }


async def _consumed_confirmation(domain_connect, budget, terms, original, receipt):
    # Pending/reviewed decisions cannot confer worker authority. Only the latest
    # immutable provider-retry receipt identifies the recovery actually consumed.
    if receipt is None or receipt["recovery_scope"] not in {
        "unstarted-planning",
        "interrupted-planning",
        "saved-plan-completion",
    }:
        return None
    async with domain_connect() as connection:
        confirmation = await connection.fetchrow(
            "SELECT * FROM operation_reauthorizations WHERE reauthorization_id=$1",
            receipt["reauthorization_id"],
        )
    if confirmation is None or confirmation["amendment_json"] is None:
        return None
    expected = {
        "version": 1,
        "budget_reauthorization_id": budget["reauthorization_id"],
        "daily_micros": terms["daily_micros"],
    }
    reviewed = json.loads(confirmation["amendment_json"])
    now = datetime.now(UTC)
    if (
        original is None
        or confirmation["scope"] != receipt["recovery_scope"]
        or confirmation["revoked"]
        or confirmation["result"] != "allowed-once"
        or confirmation["decided_at"] is None
        or not confirmation["created_at"]
        <= confirmation["decided_at"]
        <= now
        < confirmation["expires_at"]
        or reviewed != expected
        or type(reviewed.get("version")) is not int
        or type(reviewed.get("daily_micros")) is not int
        or confirmation["expected_version"] != receipt["expected_version"]
        or receipt["restored_version"] != receipt["expected_version"] + 1
        or original["version"] < receipt["restored_version"]
        or any(
            confirmation[key] != budget[key] or receipt[key] != budget[key]
            for key in (
                "operation_id",
                "org_id",
                "workspace_id",
                "plan_digest",
                "original_approval_id",
                "requester",
                "approved_by",
            )
        )
    ):
        raise LifecycleRefused(
            "consumed planning budget confirmation is not current authority"
        )
    return {
        "reauthorization_id": confirmation["reauthorization_id"],
        "decided_at": confirmation["decided_at"].isoformat(),
        **expected,
    }


def amended_review(parameters, amendment, request, authorization):
    from .preview import preview_workspace

    public_request = json.loads(parameters["lifecycle_inputs"])
    public_request["budget_max_daily_usd"] = str(
        Decimal(amendment["daily_micros"]) / 1000000
    )
    preview = preview_workspace(
        request,
        authorization=authorization,
        requested_capacity={
            "max_resource_units": int(
                parameters["lifecycle_allocation_max_resource_units"]
            ),
            "max_runtime_seconds": int(
                parameters["lifecycle_allocation_max_runtime_seconds"]
            ),
            "max_cost_micros": amendment["daily_micros"],
            "request": public_request,
            "allocation_id": parameters.get(
                "lifecycle_root_allocation_id", parameters["allocation_id"]
            ),
            "policy_revision": parameters["lifecycle_policy_sha256"],
        },
        cost_estimate=None,
        approval_required=True,
    )
    return {
        **amendment,
        "next_plan_revision": preview.revision,
        "next_lifecycle_inputs": canonical(public_request),
    }


async def verify_budget_artifact(row, operation_connect, domain_connect):
    metadata = json.loads(row["artifact_metadata_json"])
    amendment = metadata.get("daily_budget_amendment")
    if amendment is None:
        return
    approved = await approved_budget(
        operation_connect,
        domain_connect,
        operation_id=row["source_operation_id"],
        org_id=row["org_id"],
        workspace_id=row["workspace_id"],
        plan_digest=row["source_payload_digest"],
        parameters=json.loads(row["parameters_json"]),
    )
    if approved is None or any(amendment.get(k) != v for k, v in approved.items()):
        raise LifecycleRefused("recorded budget amendment is unavailable or changed")
