"""Protected provenance for an explicitly reauthorized, settled bootstrap.

This is a proof reader, not an operation reset. The caller holds the operation,
lease and provider-dispatch locks before consuming the snapshot. Historical
closure is retained in the immutable retry receipt before the live projection
can advance to a new execution fence.
"""

import hashlib
import json
from datetime import UTC, datetime

from .identity import OperationRefused
from .store import stored_outcome

KIND = "settled-bootstrap-v1"
OPERATION_FIELDS = (
    "operation_id",
    "org_id",
    "workspace_id",
    "job_id",
    "attempt_id",
    "action",
    "state",
    "version",
    "created_at",
    "updated_at",
    "detail",
    "cancel_requested_at",
    "cleanup_required",
    "request_payload",
    "plan_digest",
)


def snapshot(value):
    return json.loads(json.dumps(value, default=lambda v: v.isoformat()))


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def review_terms(value, runtime_amendment):
    from .bootstrap_provenance_assumption import FIELD, metadata

    closure_sha256 = digest(validate(value))
    assumption = metadata(closure_sha256)
    return {
        "version": 1,
        "kind": "settled-bootstrap-review-v1",
        "closure_sha256": closure_sha256,
        "runtime_amendment": runtime_amendment,
        **({FIELD: assumption} if assumption is not None else {}),
    }


def review_runtime(terms):
    from .bootstrap_provenance_assumption import FIELD, validate_review

    require(
        isinstance(terms, dict)
        and set(terms) - {FIELD}
        == {"version", "kind", "closure_sha256", "runtime_amendment"}
    )
    require(terms["version"] == 1 and terms["kind"] == "settled-bootstrap-review-v1")
    require(
        isinstance(terms["closure_sha256"], str) and len(terms["closure_sha256"]) == 64
    )
    try:
        validate_review(terms)
    except ValueError:
        require(False)
    return terms["runtime_amendment"]


def require(value):
    if not value:
        raise OperationRefused("settled bootstrap provenance unavailable")


def validate(value):
    """Validate retained history without treating it as current authority."""
    from . import settled_bootstrap_repeat as repeated

    if isinstance(value, dict) and value.get("kind") == repeated.KIND:
        return repeated.validate(value)
    require(
        isinstance(value, dict)
        and set(value)
        == {
            "kind",
            "operation",
            "lease",
            "call",
            "settlement_audit",
            "source_audit",
            "settlement",
            "claims",
        }
    )
    require(value["kind"] == KIND)
    operation, lease, call = (value[k] for k in ("operation", "lease", "call"))
    require(operation["state"] == "unknown" and operation["action"] == "provision")
    require(
        operation["cancel_requested_at"] is None and not operation["cleanup_required"]
    )
    require(lease["closed_reason"] == "recovery: unknown" and lease["closed_at"])
    require(
        all(
            lease[k] is None
            for k in (
                "holder",
                "attempt_id",
                "acquired_at",
                "expires_at",
                "runtime_deadline",
            )
        )
    )
    require(lease["closed_holder"] and lease["closed_attempt_id"])
    require(
        call["stage"] == "unresolved" and stored_outcome(call["outcome"]) == "unknown"
    )
    require(call["provider_ref"] is None)
    require(
        call["provider"] == "superplane-lifecycle"
        and call["operation_kind"] == "bootstrap-workspace"
    )
    require(
        all(
            operation[k] == lease[k] == call[k]
            for k in ("operation_id", "org_id", "workspace_id")
        )
    )
    require(call["job_id"] == operation["job_id"])
    require(1 <= call["fence_token"] < lease["fence_token"] <= 32)
    audit = value["settlement_audit"]
    require(
        audit["event"] == "recovery.settled"
        and audit["allowed"]
        and audit["detail"] == "unknown"
    )
    require(
        audit["actor"] == lease["closed_holder"]
        and audit["attempt_id"] == lease["closed_attempt_id"]
    )
    require(audit["fence_token"] == lease["fence_token"])
    require(audit["recorded_at"] == operation["updated_at"])
    require(
        datetime.fromisoformat(audit["recorded_at"])
        <= datetime.fromisoformat(lease["closed_at"])
    )
    source = value["source_audit"]
    require(source["event"] == "lease.acquire" and source["allowed"])
    require(
        source["attempt_id"] == call["attempt_id"]
        and source["fence_token"] == call["fence_token"]
    )
    require(
        all(
            a[k] == operation[k]
            for a in (source, audit)
            for k in ("operation_id", "org_id", "workspace_id")
        )
    )
    claims = value["claims"]
    require(
        [c["fence_token"] for c in claims]
        == list(range(call["fence_token"] + 1, lease["fence_token"] + 1))
    )
    require(
        all(
            c[k] == operation[k]
            for c in claims
            for k in ("operation_id", "org_id", "workspace_id")
        )
    )
    require(
        claims[-1]["holder"] == lease["closed_holder"]
        and claims[-1]["attempt_id"] == lease["closed_attempt_id"]
    )
    settlement = value["settlement"]
    require(settlement is not None)
    if settlement is not None:
        require(
            all(
                settlement[k] == operation[k]
                for k in (
                    "operation_id",
                    "org_id",
                    "workspace_id",
                    "job_id",
                    "attempt_id",
                )
            )
        )
        require(
            settlement["claim_holder"] == lease["closed_holder"]
            and settlement["claim_attempt_id"] == lease["closed_attempt_id"]
        )
        require(
            settlement["claim_fence_token"] == lease["fence_token"]
            and settlement["delivered_at"]
        )
        accounting = settlement["accounting"]
        if isinstance(accounting, str):
            accounting = json.loads(accounting)
        require(
            accounting["action"] == "unknown"
            and accounting["budget"] == "retain"
            and accounting["release_permitted"] is False
        )
    return value


def journal_bound(value, row):
    """The journal plan remains byte-identical after its native revocation."""
    validate(value)
    accounting = value["settlement"]["accounting"]
    if isinstance(accounting, str):
        accounting = json.loads(accounting)
    partial = accounting.get("partial_lifecycle", {})
    require(
        partial.get("phase") == "bootstrap-workspace"
        and partial.get("workflow_complete") is False
    )
    require(partial.get("source_artifact_id") == value["call"]["target"])
    generations = partial.get("authority_generations", [])
    require(len(generations) == 1)
    source = generations[0]
    require(all(source[k] == row[k] for k in ("generation", "claim")))
    # Partial lifecycle accounting hashes the stored JSON string itself.
    require(source["plan_sha256"] == digest(row["plan_json"]))
    require(
        all(
            row[k] == value["operation"][k]
            for k in ("operation_id", "org_id", "workspace_id")
        )
    )
    return row


async def read(connection, operation, lease, call):
    from . import settled_bootstrap_repeat as repeated
    from .provider_retry import (
        active_call_rows,
        receipts_for,
        verify_partial_source_claim,
    )

    require(lease is not None and call is not None)
    current = await connection.fetchrow(
        "SELECT * FROM harness_operations WHERE operation_id=$1",
        operation["operation_id"],
    )
    current_lease = await connection.fetchrow(
        "SELECT * FROM harness_operation_leases WHERE operation_id=$1",
        operation["operation_id"],
    )
    calls = await connection.fetch(
        "SELECT * FROM harness_provider_call_intent WHERE operation_id=$1",
        operation["operation_id"],
    )
    receipts = await receipts_for(connection, operation["operation_id"])
    prior = None
    if receipts:
        prior = await repeated.predecessor(connection, operation, receipts)
        require(len(calls) == len(receipts) + 1)
        calls = await active_call_rows(connection, operation["operation_id"], calls)
    require(current is not None and current_lease is not None and len(calls) == 1)
    require(all(current[k] == operation[k] for k in OPERATION_FIELDS))
    require(dict(current_lease) == dict(lease) and dict(calls[0]) == dict(call))
    await verify_partial_source_claim(connection, lease, call, closed=True)
    # An original closure remains v1. The distinct successor boundary above
    # verifies the immutable component retry and its retained first settlement.
    audits = await connection.fetch(
        "SELECT * FROM harness_execution_audit WHERE operation_id=$1 AND allowed "
        "AND ((event='recovery.settled' AND fence_token=$2) "
        "OR (event='lease.acquire' AND fence_token=$3)) ORDER BY id",
        operation["operation_id"],
        lease["fence_token"],
        call["fence_token"],
    )
    require(len(audits) == 2)
    sources = [dict(a) for a in audits if a["event"] == "lease.acquire"]
    endings = [dict(a) for a in audits if a["event"] == "recovery.settled"]
    require(len(sources) == len(endings) == 1)
    settlements = await connection.fetch(
        "SELECT * FROM harness_recovery_settlements WHERE operation_id=$1 AND "
        "claim_fence_token=$2",
        operation["operation_id"],
        lease["fence_token"],
    )
    require(len(settlements) == 1)
    settlement = settlements[0] if settlements else None
    claims = await connection.fetch(
        "SELECT * FROM harness_recovery_claim_bindings WHERE operation_id=$1 "
        "AND fence_token>$2 AND fence_token<=$3 ORDER BY fence_token",
        operation["operation_id"],
        call["fence_token"],
        lease["fence_token"],
    )
    value = validate(
        snapshot(
            {
                "kind": repeated.KIND if prior is not None else KIND,
                **({"predecessor": prior} if prior is not None else {}),
                "operation": {k: operation[k] for k in OPERATION_FIELDS},
                "lease": dict(lease),
                "call": dict(call),
                "settlement_audit": endings[0],
                "source_audit": sources[0],
                "settlement": dict(settlement) if settlement else None,
                "claims": [dict(c) for c in claims],
            }
        )
    )
    require(datetime.fromisoformat(value["lease"]["closed_at"]) <= datetime.now(UTC))
    return value
