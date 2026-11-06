"""User-facing deployment steps projected from durable workspace records."""

import json
from datetime import UTC, datetime


STEPS = (
    ("request", "Request accepted"),
    ("plan", "Prepare deployment plan"),
    ("infrastructure", "Provision EKS cluster"),
    ("configure", "Configure workspace"),
    ("verify", "Verify access and health"),
    ("ready", "Workspace ready"),
)


def project_progress(*, workspace, artifacts, journals, operation, now):
    """Expose milestones, never raw grants, plan outputs or credentials."""
    metadata = [json.loads(row["artifact_metadata_json"]) for row in artifacts]
    progress = [json.loads(row["progress_json"]) for row in journals]
    next_phases = {item.get("next_phase") for item in metadata}
    active = workspace.status == "Active" and workspace.cluster_id is not None
    configured = active or any(
        item.get("component_inventory_complete") is True
        and item.get("retain_workspace") is True
        and item.get("complete") is True
        and item.get("phase") == "revoked"
        for item in progress
    )
    completed = [
        bool(workspace.provisioning_operation_id),
        active or bool(next_phases & {"apply-infrastructure", "bootstrap-workspace", "complete"}),
        active or bool(next_phases & {"bootstrap-workspace", "complete"}),
        configured,
        active,
        active,
    ]
    steps = [
        {"id": key, "label": label, "state": "completed" if done else "pending"}
        for (key, label), done in zip(STEPS, completed)
    ]
    current = next((step for step in steps if step["state"] != "completed"), None)
    message = "Workspace deployment is complete." if active else "Waiting to start."
    if current:
        state = operation.get("state") if operation else None
        expiry = operation.get("expires_at") if operation else None
        if expiry and expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        if state in {"failed", "cancelled"} or workspace.status == "Failed":
            current["state"] = "failed"
            message = "Deployment stopped. Completed steps have been retained."
        elif state == "running" and expiry and expiry <= now:
            current["state"] = "paused"
            message = "Deployment needs attention. No worker is currently executing this step."
        elif state == "running" and expiry and expiry > now:
            current["state"] = "running"
            message = current["label"] + " is in progress."
        elif state in {"succeeded", "pending"}:
            current["state"] = "waiting"
            message = (
                "Review and approve the next deployment phase."
                if state == "succeeded" else "Waiting for a deployment worker."
            )
        elif state is None or state in {"running", "unknown"}:
            current["state"] = "unknown"
            message = "Current execution status is unavailable. Completed steps are retained."
    count = sum(step["state"] == "completed" for step in steps)
    return {
        "steps": steps,
        "completed": count,
        "total": len(steps),
        "current_step": current["id"] if current else None,
        "message": message,
        "observed_at": now.isoformat(),
    }


async def read_progress(composition, workspace):
    scope = (str(workspace.org_id), str(workspace.id))
    async with composition.domain_connect() as connection:
        artifacts = await connection.fetch(
            "SELECT artifact_metadata_json FROM workspace_lifecycle_artifacts "
            "WHERE org_id=$1 AND workspace_id=$2 ORDER BY created_at LIMIT 101",
            *scope,
        )
        journals = await connection.fetch(
            "SELECT progress_json FROM workspace_bootstrap_authority "
            "WHERE org_id=$1 AND workspace_id=$2 LIMIT 101",
            *scope,
        )
    if len(artifacts) > 100 or len(journals) > 100:
        raise ValueError("workspace progress history exceeds its read limit")
    async with composition.operation_connect() as connection:
        operation = await connection.fetchrow(
            "SELECT o.state,l.expires_at FROM harness_operations o "
            "LEFT JOIN harness_operation_leases l USING(operation_id) "
            "WHERE o.org_id=$1 AND o.workspace_id=$2 AND o.operation_id=$3",
            *scope, workspace.provisioning_operation_id,
        )
    return project_progress(
        workspace=workspace, artifacts=artifacts, journals=journals,
        operation=operation, now=datetime.now(UTC),
    )
