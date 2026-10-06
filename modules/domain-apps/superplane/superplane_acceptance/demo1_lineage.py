"""Bind the current lifecycle operation to the original submitted browser request."""

from datetime import UTC, datetime

from .demo1_evidence import EvidenceError, digest, identifier
from .demo1_report import reference


def observe_lineage(
    reader,
    checkpoint,
    original_operation,
    current_operation,
    max_runtime_seconds,
    *,
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
    runtime = reader.observe(max_runtime_seconds, lineage_scope=scope)
    observed = runtime.get("lineage")
    if (
        not isinstance(observed, dict)
        or set(observed)
        != set(scope)
        | {"status", "current_request_id", "current_phase", "artifact_ids"}
        or observed["status"] != "OBSERVED"
        or any(observed[key] != value for key, value in scope.items())
        or observed["current_phase"]
        not in ("apply-infrastructure", "bootstrap-workspace")
        or observed["current_request_id"] == selected.request_id
    ):
        raise EvidenceError(
            "lineage: immutable continuation ancestry unavailable or mismatched"
        )
    identifier(observed["current_request_id"], "continuation request")
    artifacts = observed["artifact_ids"]
    depth = 1 if observed["current_phase"] == "apply-infrastructure" else 2
    if not isinstance(artifacts, list) or len(artifacts) != depth:
        raise EvidenceError("lineage: incomplete continuation ancestry")
    for artifact in artifacts:
        digest(artifact, "lineage artifact")
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
