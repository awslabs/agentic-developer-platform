"""Fail-closed authorization for a selected native DevOps space."""

from datetime import UTC, datetime

from fastapi import HTTPException

from l5_services.adapters.devops.spaces import SpaceSelection


def authorize_browser_launch(actor, member, configuration, version, receipt, *,
                             product_id: str, project_id: str, binding_id: str,
                             configuration_revision: int, relationship_revision: int,
                             action_allowed: bool, now: datetime | None = None,
                             owner_native_policy: dict | None = None) -> SpaceSelection:
    now = now or datetime.now(UTC)
    expiry = getattr(actor, "expires_at", None)
    if expiry is not None and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    if (getattr(actor, "account_type", None) != "human"
            or getattr(actor, "auth_source", None) != "jwt"
            or not getattr(actor, "user_id", None) or not getattr(actor, "org_id", None)
            or expiry is None or expiry <= now):
        raise HTTPException(401, detail={"code": "session_required"})

    if (member is None or member.id != actor.user_id
            or member.org_id != actor.org_id or member.user_kind != "human"
            or member.is_shadow or not action_allowed):
        raise HTTPException(404, detail={"code": "not_found"})

    settings = version.settings if version is not None else {}
    devops = owner_native_policy if owner_native_policy is not None else settings.get("devops") or {}
    if (configuration is None or version is None or configuration.tenant_id != actor.org_id
            or configuration.product_id != product_id or configuration.project_id != project_id
            or configuration.service_id != "devops"
            or configuration.relationship_revision != str(relationship_revision)
            or configuration.current_revision != configuration_revision
            or version.revision != configuration_revision
            or owner_native_policy is None and version.state != "ready"
            or configuration.handoff_state != "linked"
            or devops.get("browser_launch_policy") != "scoped_session"
            or devops.get("authentication_mode") != "scoped_iam_federation"
            or "launchNativeBrowser" not in devops.get("permitted_native_actions", [])):
        raise HTTPException(404, detail={"code": "not_found"})

    selected = receipt.selection if receipt is not None and isinstance(receipt.selection, dict) else {}
    if (receipt is None or receipt.tenant_id != actor.org_id
            or owner_native_policy is None and receipt.configuration_revision != configuration_revision
            or receipt.state not in {"ready", "succeeded"}
            or not receipt.native_space_id or not receipt.scope_fingerprint
            or selected.get("tenant_id") != actor.org_id
            or selected.get("product_id") != product_id
            or selected.get("project_id") != project_id
            or selected.get("binding_id") != binding_id):
        raise HTTPException(404, detail={"code": "not_found"})
    try:
        selection = SpaceSelection(**selected)
        if selection.fingerprint() != receipt.scope_fingerprint:
            raise ValueError("binding scope mismatch")
    except (TypeError, ValueError):
        raise HTTPException(404, detail={"code": "not_found"}) from None
    return selection
