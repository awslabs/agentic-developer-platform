"""Read and fence the canonical node and current PR association for CLI recovery."""

import hashlib
import json
from datetime import UTC

from fastapi import HTTPException
from sqlalchemy import select

from .models import OrchestrationNode
from .pr_bindings import BindingError, active_binding_for_node, binding_summary


async def snapshot(db, *, org_id, node_id, flow_id=None, lock=False):
    statement = select(OrchestrationNode).where(OrchestrationNode.org_id == org_id, OrchestrationNode.id == node_id)
    if flow_id is not None:
        statement = statement.where(OrchestrationNode.flow_id == flow_id)
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    node = (await db.execute(statement)).scalar_one_or_none()
    if node is None:
        raise HTTPException(404, detail="No such recovery node in this tenant and flow.")
    try:
        binding = await active_binding_for_node(db, org_id=org_id, node_id=node.id, attempt=node.attempts)
    except BindingError as exc:
        raise HTTPException(409, detail=exc.message) from exc
    if binding is not None and lock:
        # Phase settlement can refresh the PR pointer without updating the node.
        # Match its binding lock as well as registration's node lock.
        await db.refresh(binding, with_for_update=True)
    updated = node.updated_at
    if updated is not None:
        updated = updated.astimezone(UTC) if updated.tzinfo else updated.replace(tzinfo=UTC)
    result = {
        "flow_id": node.flow_id,
        "node_id": node.id,
        "kind": node.kind,
        "state": node.state,
        "attempts": node.attempts,
        "title": node.title,
        "issue_ref": node.issue_ref,
        "updated_at": updated.isoformat() if updated else None,
        "bound_pull_request": binding_summary(binding) if binding else None,
        "contract": "node-recovery-v1",
    }
    fingerprint = dict(result)
    fingerprint["binding_identity"] = {"id": binding.id, "revision": binding.revision, "accepted_scope": binding.accepted_scope} if binding else None
    result["revision"] = hashlib.sha256(json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return node, result


async def require_snapshot(db, *, org_id, node_id, flow_id, expected_revision):
    node, result = await snapshot(db, org_id=org_id, node_id=node_id, flow_id=flow_id, lock=True)
    if result["revision"] != expected_revision:
        raise HTTPException(409, detail={"reason": "stale_revision", "message": "Recovery state changed; review the node again."})
    return node
