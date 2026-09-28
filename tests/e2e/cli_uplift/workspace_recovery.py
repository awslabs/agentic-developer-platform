"""Caller-side workspace create intent; E18 provisioning stays guarded.

This is a recovery prerequisite, not a metadata-only provisioning adapter.
The callback must use the exact retained CLI arguments and original principal.
No resource deletion or compute authorization is inferred from this intent.
"""

import hashlib
import json
import uuid

from .config import no_secrets, require
from .report import redact


def create_intent(
    *,
    evaluation_id,
    gateway_url,
    tenant_id,
    principal_id,
    session_secret_name,
    operation_id,
    name,
    isolation="namespace",
):
    fields = (
        evaluation_id,
        gateway_url,
        tenant_id,
        principal_id,
        session_secret_name,
        name,
    )
    require(
        all(isinstance(value, str) and value for value in fields),
        "Workspace recovery requires original scope and fixture references",
    )
    require(
        gateway_url.startswith("https://"),
        "Workspace recovery needs the selected HTTPS gateway",
    )
    require(
        isolation in {"dedicated", "namespace"},
        "Unknown workspace isolation",
    )
    require(
        not session_secret_name.startswith("arn:")
        and "://" not in session_secret_name
        and redact(session_secret_name) == session_secret_name,
        "Workspace recovery requires a secret name, not credentials or a URL",
    )
    try:
        operation_id = str(uuid.UUID(operation_id))
    except (ValueError, TypeError, AttributeError):
        raise ValueError(
            "Workspace recovery requires an explicit operation UUID"
        ) from None
    plan = {
        "schema": "superplane-workspace-create-intent-v1",
        "evaluation_id": evaluation_id,
        "gateway_url": gateway_url,
        "tenant_id": tenant_id,
        "principal_id": principal_id,
        "ordinary_session_secret_name": session_secret_name,
        "operation_id": operation_id,
        "resource_kind": "superplane_workspace",
        "request": {
            "name": name,
            "isolation_mode": isolation,
            "operation_id": operation_id,
        },
        "argv": [
            "superplane",
            "workspace",
            "create",
            "--name",
            name,
            "--isolation",
            isolation,
            "--operation-id",
            operation_id,
            "--yes",
        ],
    }
    no_secrets(plan)
    # Secret NAME is an allowed reference in configuration, not bearer material.
    scrubbed = dict(plan)
    scrubbed.pop("ordinary_session_secret_name")
    require(
        redact(scrubbed) == scrubbed, "Workspace intent contains credential material"
    )
    plan["request_sha256"] = hashlib.sha256(
        json.dumps(plan["request"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return plan


def dispatch_create(manifest, plan, dispatch):
    """Persist immutable input before handing it to a transport.

    A failed sink forbids dispatch. A lost transport response leaves the external
    plan available for exact-key reconciliation with a reauthenticated fixture.
    This helper does not remove E18's live recovery/provisioning guard.
    """
    original = create_intent(
        evaluation_id=plan["evaluation_id"],
        gateway_url=plan["gateway_url"],
        tenant_id=plan["tenant_id"],
        principal_id=plan["principal_id"],
        session_secret_name=plan["ordinary_session_secret_name"],
        operation_id=plan["operation_id"],
        name=plan["request"]["name"],
        isolation=plan["request"]["isolation_mode"],
    )
    require(
        original == plan, "Workspace recovery intent differs from the original request"
    )
    manifest.record_diagnostic("superplane_workspace", plan)
    return dispatch(plan)
