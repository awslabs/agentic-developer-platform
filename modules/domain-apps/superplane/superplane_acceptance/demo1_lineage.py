"""Bind the current lifecycle operation to the original submitted browser request."""

from datetime import UTC, datetime

from .demo1_evidence import EvidenceError, identifier
from .demo1_report import reference


def observe_lineage(
    reader,
    checkpoint,
    original_operation,
    current_operation,
    max_runtime_seconds,
    *,
    transport,
    now=None,
):
    selected = reader.selected
    now = now or datetime.now(UTC)
    if (
        checkpoint is None
        or not checkpoint.submitted
        or checkpoint.request_id != selected.request_id
        or checkpoint.plan_revision != selected.plan_revision
        or not selected.authorized_at <= now < selected.deadline
    ):
        raise EvidenceError(
            "lineage: original submitted checkpoint and current read authority required"
        )
    scope = {
        "org_id": selected.org_id,
        "workspace_id": identifier(checkpoint.workspace_id, "lineage workspace"),
        "request_id": selected.request_id,
        "plan_revision": selected.plan_revision,
        "account": selected.account,
        "region": selected.region,
        "authorized_at": selected.authorized_at.isoformat(),
        "observed_at": now.isoformat(),
        "original_operation_id": identifier(original_operation, "original operation"),
        "current_operation_id": identifier(current_operation, "current operation"),
    }
    # The API performs the immutable ancestry validation. Runtime observation
    # binds its deployed release; it never injects a database probe into a pod.
    from .demo1_browser import PREFIX, _native_reentry, _response

    reader.observe(max_runtime_seconds)
    operation = _response(
        transport, "GET", PREFIX + f"/operations/by-idempotency/{selected.request_id}"
    )
    workspace = _response(
        transport, "GET", PREFIX + f"/workspaces/{checkpoint.workspace_id}"
    )
    if (
        operation.get("request_id"),
        operation.get("workspace_id"),
        operation.get("provisioning_operation_id"),
        workspace.get("id"),
        workspace.get("org_id"),
        workspace.get("provisioning_operation_id"),
    ) != (
        selected.request_id,
        checkpoint.workspace_id,
        original_operation,
        checkpoint.workspace_id,
        selected.org_id,
        current_operation,
    ):
        raise EvidenceError("lineage: original or current operation scope differs")
    proof = operation.get("lifecycle_lineage")
    if not isinstance(proof, dict) or set(proof) != {
        "version",
        "org_id",
        "workspace_id",
        "root_request_id",
        "root_operation_id",
        "current_operation_id",
        "plan_revision",
        "phases",
    }:
        raise EvidenceError("lineage: authenticated native proof unavailable")
    phases = proof["phases"]
    if (
        not isinstance(phases, list)
        or not 2 <= len(phases) <= 3
        or any(
            not isinstance(phase, dict)
            or set(phase)
            != {
                "phase",
                "request_id",
                "operation_id",
                "state",
                "payload_digest",
                "source_artifact_id",
            }
            for phase in phases
        )
    ):
        raise EvidenceError("lineage: incomplete continuation ancestry")
    operation_id, request_id, _, operations = _native_reentry(
        operation, workspace, selected, checkpoint
    )
    if operation_id != current_operation or request_id == selected.request_id:
        raise EvidenceError("lineage: current request differs from native ancestry")
    current = _response(transport, "GET", PREFIX + f"/operations/{current_operation}")
    if (
        current.get("provisioning_operation_id"),
        current.get("request_id"),
        current.get("workspace_id"),
    ) != (current_operation, request_id, checkpoint.workspace_id):
        raise EvidenceError("lineage: current operation differs from native ancestry")
    refreshed = _response(
        transport, "GET", PREFIX + f"/workspaces/{checkpoint.workspace_id}"
    )
    if any(
        refreshed.get(key) != workspace.get(key)
        for key in ("id", "org_id", "provisioning_operation_id")
    ):
        raise EvidenceError("lineage: workspace changed during observation")
    observed = {
        **scope,
        "status": "OBSERVED",
        "current_request_id": request_id,
        "current_phase": phases[-1]["phase"],
        "artifact_ids": [phase["source_artifact_id"] for phase in reversed(phases[1:])],
        "operations": operations,
    }
    return observed


def lineage_report(observed):
    return {
        "status": "OBSERVED",
        "original_request_ref": reference(observed["request_id"]),
        "original_operation_ref": reference(observed["original_operation_id"]),
        "current_request_ref": reference(observed["current_request_id"]),
        "current_operation_ref": reference(observed["current_operation_id"]),
        "current_phase": observed["current_phase"],
        "artifact_refs": [reference(value) for value in observed["artifact_ids"]],
        "observed_at": observed["observed_at"],
        "scope": "admitted ancestry only; no new approval, Ready or cleanup proof",
    }
