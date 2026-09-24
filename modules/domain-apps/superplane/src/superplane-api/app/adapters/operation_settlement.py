"""Domain ledger receiver for immutable shared recovery settlement receipts."""

import hashlib
import json
from contextlib import asynccontextmanager

from harness_jobs.admission import BudgetDenied
from harness_jobs.identity import (
    OperationState,
    TERMINAL_STATES,
    decode_payload,
    payload_digest as request_digest,
)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _document(value):
    return json.loads(value) if isinstance(value, str) else value


async def allocation_reservation(connection, original, accounting, identities):
    """Only a teardown admitted against an immutable apply artifact can release it."""
    source_id = accounting.get("allocation_source_operation_id")
    if source_id is None:
        return None
    artifact_id = accounting.get("lifecycle_artifact_id")
    request = decode_payload(original["request_payload"])
    parameters = request.parameters
    if (
        request_digest(request) != original["plan_digest"]
        or request.action != "teardown"
        or source_id == identities["operation_id"]
        or not isinstance(source_id, str)
        or parameters.get("allocation_source_operation_id") != source_id
        or parameters.get("lifecycle_artifact_id") != artifact_id
        or not parameters.get("allocation_id")
        or parameters.get("allocation_id") != accounting.get("allocation_id")
    ):
        raise BudgetDenied("retirement allocation was not bound by original admission")
    from workspace_provisioning.artifacts import read_artifact

    @asynccontextmanager
    async def connect():
        yield connection

    try:
        artifact = await read_artifact(
            connect,
            artifact_id=artifact_id,
            org_id=identities["org_id"],
            workspace_id=identities["workspace_id"],
            require_fresh=False,
        )
    except Exception:
        raise BudgetDenied("retirement allocation artifact unavailable") from None
    source = await connection.fetchrow(
        "SELECT * FROM harness_operations WHERE operation_id=$1 FOR UPDATE",
        source_id,
    )
    metadata = json.loads(artifact["artifact_metadata_json"])
    if (
        source is None
        or source["state"] != "succeeded"
        or source["org_id"] != identities["org_id"]
        or source["workspace_id"] != identities["workspace_id"]
        or artifact["source_operation_id"] != source_id
        or metadata.get("allocation_source_operation_id") != source_id
        or artifact["source_job_id"] != source["job_id"]
        or artifact["source_attempt_id"] != source["attempt_id"]
        or artifact["source_payload_digest"] != source["plan_digest"]
        or artifact["source_request_payload"] != source["request_payload"]
    ):
        raise BudgetDenied("retirement allocation original admission mismatch")
    source_request = decode_payload(source["request_payload"])
    if (
        source_request.action != "provision"
        or request_digest(source_request) != source["plan_digest"]
        or source_request.parameters.get("lifecycle_phase") != "apply-infrastructure"
        or source_request.parameters.get("allocation_id") != parameters["allocation_id"]
    ):
        raise BudgetDenied("retirement allocation is not the approved apply operation")
    reservation = await connection.fetchrow(
        "SELECT r.*,c.reservation_id AS consumed_reservation_id,c.org_id AS consumed_org_id,"
        "c.workspace_id AS consumed_workspace_id,c.max_resource_units AS approved_units,"
        "c.max_runtime_seconds AS approved_runtime,c.max_cost_micros AS approved_cost, "
        "c.plan_digest AS consumed_plan_digest,c.reservation_state AS consumed_state "
        "FROM operation_budget_reservations r JOIN harness_approval_consumption c ON c.operation_id=$1 "
        "WHERE r.job_id=$2 AND r.attempt_id=$3 FOR UPDATE OF r",
        source_id,
        source["job_id"],
        source["attempt_id"],
    )
    if reservation is None or any(
        [
            reservation["reservation_id"] != reservation["consumed_reservation_id"],
            reservation["consumed_plan_digest"] != source["plan_digest"],
            reservation["consumed_state"] not in {"confirmed", "retained", "released"},
            reservation["state"] not in {"confirmed", "retained", "released"},
            reservation["org_id"] != identities["org_id"],
            reservation["workspace_id"] != identities["workspace_id"],
            reservation["consumed_org_id"] != identities["org_id"],
            reservation["consumed_workspace_id"] != identities["workspace_id"],
            reservation["max_resource_units"] != reservation["approved_units"],
            reservation["max_runtime_seconds"] != reservation["approved_runtime"],
            reservation["max_cost_micros"] != reservation["approved_cost"],
        ]
    ):
        raise BudgetDenied("retirement allocation reservation mismatch")
    return reservation


async def accept_settlement(
    connection,
    *,
    receipt_id,
    payload_digest,
    operation_id,
    job_id,
    attempt_id,
    org_id,
    workspace_id,
    accounting,
):
    """Caller owns the transaction; no caller-supplied disposition is trusted.

    Lock the original operation, terminal lease and shared immutable receipt, then
    the reservation. All replicas use that order, so concurrent delivery serializes
    before any budget effect. The sender may acknowledge only after commit.
    """
    try:
        encoded = canonical(accounting)
    except (TypeError, ValueError):
        raise BudgetDenied("settlement accounting is malformed") from None
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    if payload_digest != digest or receipt_id != "recovery:" + digest:
        raise BudgetDenied("settlement receipt digest mismatch")
    original = await connection.fetchrow(
        "SELECT o.state,o.job_id,o.attempt_id,o.org_id,o.workspace_id,o.request_payload,o.plan_digest,"
        "l.closed_at,l.closed_holder,l.closed_attempt_id,l.fence_token,"
        "l.org_id AS lease_org_id,l.workspace_id AS lease_workspace_id,"
        "r.receipt_id,r.payload_digest,r.accounting,r.claim_holder,"
        "r.claim_attempt_id,r.claim_fence_token,r.org_id AS receipt_org_id,"
        "r.workspace_id AS receipt_workspace_id,r.job_id AS receipt_job_id,"
        "r.attempt_id AS receipt_attempt_id "
        "FROM harness_operations o JOIN harness_operation_leases l USING(operation_id) "
        "JOIN harness_recovery_settlements r USING(operation_id) "
        "WHERE o.operation_id=$1 FOR UPDATE OF o,l,r",
        operation_id,
    )
    identities = {
        "operation_id": operation_id,
        "job_id": job_id,
        "attempt_id": attempt_id,
        "org_id": org_id,
        "workspace_id": workspace_id,
    }
    if original is None or not isinstance(accounting, dict):
        raise BudgetDenied("authoritative settlement receipt is unavailable")
    claim = {
        "holder": original["closed_holder"],
        "attempt_id": original["closed_attempt_id"],
        "fence_token": original["fence_token"],
    }
    if (
        any(accounting.get(key) != value for key, value in identities.items())
        or any(
            original[key] != identities[key]
            for key in ("job_id", "attempt_id", "org_id", "workspace_id")
        )
        or any(
            original["receipt_" + key] != identities[key]
            for key in ("job_id", "attempt_id", "org_id", "workspace_id")
        )
        or original["lease_org_id"] != org_id
        or original["lease_workspace_id"] != workspace_id
        or original["closed_at"] is None
        or not claim["holder"]
        or not claim["attempt_id"]
        or accounting.get("claim") != claim
        or {
            "holder": original["claim_holder"],
            "attempt_id": original["claim_attempt_id"],
            "fence_token": original["claim_fence_token"],
        }
        != claim
        or original["receipt_id"] != receipt_id
        or original["payload_digest"] != digest
        or canonical(_document(original["accounting"])) != encoded
        or accounting.get("version") != 1
        or accounting.get("action") != original["state"]
        or OperationState(original["state"]) not in TERMINAL_STATES
    ):
        raise BudgetDenied("settlement does not match the original closed claim")
    if accounting.get("budget") not in {"release", "retain", "settle"}:
        raise BudgetDenied("settlement budget disposition is invalid")
    # SETTLE means resources incurred cost; without provider billing evidence the
    # entire envelope remains committed. It must never become unused budget.
    released = (
        accounting["budget"] == "release"
        and accounting.get("inventory_complete") is True
        and accounting.get("release_permitted") is True
        and accounting.get("may_mark_released") is True
        and accounting.get("exposure") == "none"
        and accounting.get("unresolved_resources") == []
        and isinstance(accounting.get("resource_dispositions"), dict)
        and all(
            value == "release" for value in accounting["resource_dispositions"].values()
        )
    )
    disposition = "released" if released else "retained"
    reservation = await connection.fetchrow(
        "SELECT * FROM operation_budget_reservations WHERE job_id=$1 AND attempt_id=$2 FOR UPDATE",
        job_id,
        attempt_id,
    )
    consumption = await connection.fetchrow(
        "SELECT reservation_id,org_id,workspace_id,plan_digest,reservation_state, "
        "max_resource_units,max_runtime_seconds,max_cost_micros "
        "FROM harness_approval_consumption WHERE operation_id=$1",
        operation_id,
    )
    if (
        reservation is None
        or reservation["state"] not in {"confirmed", "retained", "released"}
        or consumption is None
        or any(
            reservation[key] != identities[key]
            for key in ("job_id", "attempt_id", "org_id", "workspace_id")
        )
        or any(
            consumption[key] != identities[key] for key in ("org_id", "workspace_id")
        )
        or consumption["reservation_id"] != reservation["reservation_id"]
        or consumption["plan_digest"] != original["plan_digest"]
        or consumption["reservation_state"] not in {"confirmed", "retained", "released"}
        or any(
            consumption[key] != reservation[key]
            for key in ("max_resource_units", "max_runtime_seconds", "max_cost_micros")
        )
    ):
        raise BudgetDenied("settlement original reservation mismatch")
    allocation = await allocation_reservation(
        connection, original, accounting, identities
    )
    accepted = await connection.fetchrow(
        "SELECT * FROM operation_settlement_receipts WHERE operation_id=$1",
        operation_id,
    )
    if accepted is not None:
        if (
            accepted["receipt_id"] != receipt_id
            or accepted["payload_digest"] != digest
            or accepted["payload_json"] != encoded
            or accepted["disposition"] != disposition
            or accepted["reservation_id"] != reservation["reservation_id"]
        ):
            raise BudgetDenied("operation already has a different settlement receipt")
        return receipt_id
    if reservation["state"] == "released" and disposition != "released":
        raise BudgetDenied("reservation was released before authoritative settlement")
    await connection.execute(
        "UPDATE operation_budget_reservations SET state=$1,reason=$2,updated_at=now() "
        "WHERE reservation_id=$3",
        disposition,
        "authoritative recovery settlement " + receipt_id,
        reservation["reservation_id"],
    )
    if allocation is not None:
        if allocation["state"] == "released" and disposition != "released":
            raise BudgetDenied(
                "allocation was released before authoritative retirement"
            )
        await connection.execute(
            "UPDATE operation_budget_reservations SET state=$1,reason=$2,updated_at=now() WHERE reservation_id=$3",
            disposition,
            "authoritative allocation retirement " + receipt_id,
            allocation["reservation_id"],
        )
    await connection.execute(
        "INSERT INTO operation_settlement_receipts(receipt_id,operation_id,reservation_id,"
        "payload_digest,payload_json,disposition,accepted_at) VALUES($1,$2,$3,$4,$5,$6,now())",
        receipt_id,
        operation_id,
        reservation["reservation_id"],
        digest,
        encoded,
        disposition,
    )
    return receipt_id
