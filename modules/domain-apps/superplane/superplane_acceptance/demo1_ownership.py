"""Read historical applied ownership, without granting execution or cleanup authority."""

from datetime import UTC, datetime

from .demo1_aws import _resource
from .demo1_browser import PREFIX, _native_reentry, _response
from .demo1_evidence import EvidenceError, digest, identifier, instant
from .demo1_provider import InventoryQuery
from .demo1_report import reference


def observe_ownership(reader, checkpoint, max_runtime_seconds, *, transport, now=None):
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
            "ownership: original submitted checkpoint and current read authority required"
        )
    workspace = identifier(checkpoint.workspace_id, "ownership workspace")
    scope = {
        "org_id": selected.org_id,
        "workspace_id": workspace,
        "request_id": selected.request_id,
        "plan_revision": selected.plan_revision,
        "account": selected.account,
        "region": selected.region,
        "authorized_at": selected.authorized_at.isoformat(),
        "observed_at": now.isoformat(),
    }
    runtime = reader.observe(max_runtime_seconds)
    operation = _response(
        transport, "GET", PREFIX + f"/operations/by-idempotency/{selected.request_id}"
    )
    current = _response(transport, "GET", PREFIX + f"/workspaces/{workspace}")
    if (
        current.get("id"),
        current.get("org_id"),
        operation.get("request_id"),
        operation.get("workspace_id"),
    ) != (workspace, selected.org_id, selected.request_id, workspace):
        raise EvidenceError("ownership: original request or workspace differs")
    current_id, _, _, _ = _native_reentry(operation, current, selected, checkpoint)
    observed = operation.get("applied_ownership")
    if not isinstance(observed, dict) or observed.get("status") != "OBSERVED":
        raise EvidenceError(
            "ownership: immutable applied ownership unavailable or mismatched"
        )
    if (
        set(observed)
        != {
            "version",
            "status",
            "org_id",
            "workspace_id",
            "request_id",
            "original_operation_id",
            "current_operation_id",
            "apply_operation_id",
            "plan_revision",
            "account_id",
            "region",
            "artifact_id",
            "recorded_at",
            "owned_resources",
            "preserved_resources",
            "inventory_complete",
        }
        or any(
            observed[key] != scope[key]
            for key in ("org_id", "workspace_id", "request_id")
        )
        or observed["version"] != 1
        or observed["current_operation_id"] != current_id
        or observed["original_operation_id"] != operation["provisioning_operation_id"]
        or observed["plan_revision"] != selected.plan_revision
        or observed["account_id"] != selected.account
        or observed["region"] != selected.region
        or observed["inventory_complete"] is not False
        or not selected.authorized_at
        <= instant(observed["recorded_at"], "ownership time")
        <= now
    ):
        raise EvidenceError("ownership: applied observation scope or time mismatch")
    digest(observed["artifact_id"], "ownership artifact")
    for key in ("original_operation_id", "apply_operation_id"):
        identifier(observed[key], "ownership operation")
    if observed["original_operation_id"] == observed["apply_operation_id"]:
        raise EvidenceError("ownership: distinct applied continuation required")
    query = InventoryQuery(
        selected.connection_id,
        selected.role,
        selected.account,
        selected.region,
        workspace,
        (),
        selected.survivors,
    )
    for key in ("owned_resources", "preserved_resources"):
        if not isinstance(observed[key], list) or len(observed[key]) > 128:
            raise EvidenceError("ownership: bounded resource identities required")
        for resource in observed[key]:
            _resource(resource, query)
    resources = observed["owned_resources"] + observed["preserved_resources"]
    if (
        not observed["owned_resources"]
        or len(resources) != len(set(resources))
        or len(resources) > 128
    ):
        raise EvidenceError("ownership: unique nonempty resource identities required")
    refreshed = _response(transport, "GET", PREFIX + f"/workspaces/{workspace}")
    if any(
        refreshed.get(key) != current.get(key)
        for key in ("id", "org_id", "provisioning_operation_id")
    ):
        raise EvidenceError("ownership: workspace changed during observation")
    return runtime, observed


def ownership_report(observed):
    return {
        "status": "OBSERVED",
        "inventory_complete": False,
        "artifact_ref": reference(observed["artifact_id"]),
        "original_operation_ref": reference(observed["original_operation_id"]),
        "apply_operation_ref": reference(observed["apply_operation_id"]),
        "recorded_at": observed["recorded_at"],
        "owned_resource_refs": [
            reference(value) for value in observed["owned_resources"]
        ],
        "preserved_resource_refs": [
            reference(value) for value in observed["preserved_resources"]
        ],
        "scope": "historical applied cluster/network subset only; not current inventory, Ready or cleanup evidence",
    }
