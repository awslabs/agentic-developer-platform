"""Owner-reviewed browser access, isolated from all agent execution grants."""

from datetime import UTC, datetime

from fastapi import HTTPException
from l5_services.adapters.devops.federation import _role_account
from l5_services.api.configuration import view
from l5_services.models.binding import Binding
from sqlalchemy import select

from src.auth.aws_connection_authority import owned_aws_connection, verified_connection_evidence
from src.products.models import Product
from src.shared.identity.product_scope import owner_scope, product_member
from src.shared.models.vault import CredentialServiceDelegation

PURPOSE = "devops_native_ui"
OPERATIONS = ["credential:session"]
CAPABILITIES = ["native_ui"]


def selection(binding, configuration, version, credential, policy):
    settings = version.settings or {}
    selected = settings.get("devops") or {}
    connection = selected.get("hosting_connection_ref") or {}
    identity = binding.provider_identity or {}
    native = binding.native_resource_ref or {}
    account = (credential.scopes or {}).get("account_id")
    if (
        not isinstance(policy, dict)
        or set(policy) != {"launcher_role_arn", "operator_role_arn"}
        or any(not isinstance(policy[field], str) or not _role_account(policy[field], account) for field in policy)
        or len({policy["launcher_role_arn"], policy["operator_role_arn"], (credential.scopes or {}).get("role_arn")}) != 3
        or configuration.service_id != "devops"
        or configuration.handoff_state != "linked"
        or binding.state != "ready"
        or binding.gaps
        or (binding.tenant_id, binding.product_id, binding.project_id, binding.configuration_id, binding.service_id)
        != (configuration.tenant_id, configuration.product_id, configuration.project_id, configuration.configuration_id, "devops")
        or binding.scope != view(configuration, version)["scope"]
        or binding.connection_ref != connection
        or native.get("record_type") != "agent_space"
        or not native.get("record_id")
        or native.get("source_connection_id") != credential.id
        or any(
            identity.get(field) != expected
            for field, expected in (
                ("account_id", account),
                ("role_arn", (credential.scopes or {}).get("role_arn")),
                ("region", selected.get("region")),
            )
        )
        or connection
        != {
            "tenant_id": configuration.tenant_id,
            "project_id": configuration.project_id,
            "source_connection_id": credential.id,
            "record_type": "connection",
            "record_id": credential.id,
            "record_revision": credential.aws_verified_version_id,
        }
    ):
        raise HTTPException(
            409,
            detail={
                "code": "native_ui_scope_invalid",
                "message": "Select a ready DevOps binding and distinct launcher and operator roles in its verified hosting account.",
            },
        )
    return {
        **policy,
        "native_space_ref": native,
        "binding_revision": binding.revision,
        "authentication_mode": "scoped_iam_federation",
        "browser_launch_policy": "scoped_session",
        "permitted_native_actions": ["launchNativeBrowser"],
    }


async def preview(backend, configuration, version, binding_id, policy):
    from src.auth.l5_owner_delegation import _digest

    owner = await product_member(backend.db, user_id=configuration.owner_id, org_id=configuration.tenant_id)
    if owner is None or not await backend.db.scalar(
        select(Product.id).where(
            Product.id == configuration.product_id,
            owner_scope(owner, configuration.tenant_id),
            Product.owner_user_id == configuration.owner_id,
        )
    ):
        raise HTTPException(404, detail={"code": "not_found"})
    connection = (version.settings.get("devops") or {}).get("hosting_connection_ref") or {}
    credential = await owned_aws_connection(backend.db, connection.get("record_id"), configuration.owner_id, configuration.tenant_id)
    verified_connection_evidence(credential)
    binding = await backend.db.get(Binding, (configuration.tenant_id, binding_id), populate_existing=True)
    if binding is None:
        raise HTTPException(404, detail={"code": "not_found"})
    selected = selection(binding, configuration, version, credential, policy)
    from l5_services.devops.setup import original_setup

    try:
        setup = await original_setup(backend.db, binding, configuration=configuration, settings=version.settings, require_setup_consent=False)
    except (ValueError, TypeError, KeyError):
        raise HTTPException(
            409,
            detail={
                "code": "setup_provenance_unavailable",
                "message": "Read the original successful create or exact-space link operation for this binding before granting native UI access.",
            },
        ) from None
    selected["setup_operation_id"] = setup.operation_id
    scope = {
        "purpose": PURPOSE,
        "control_action": PURPOSE,
        "tenant_id": configuration.tenant_id,
        "product_id": configuration.product_id,
        "project_id": configuration.project_id,
        "service_id": "l5-devops-worker",
        "credential_id": credential.id,
        "created_by": configuration.owner_id,
        "provider_account_id": binding.provider_identity["account_id"],
        "provider_role_arn": binding.provider_identity["role_arn"],
        "region": binding.provider_identity["region"],
        "configuration_id": configuration.configuration_id,
        "configuration_revision": version.revision,
        "configuration_hash": version.hash,
        "binding_id": binding.binding_id,
        "binding_generation": binding.generation,
        "operations": OPERATIONS,
        "capabilities": CAPABILITIES,
        "sources": [{**connection, "record_type": "credential"}],
        "visibility": version.settings.get("visibility", "only_me"),
        "native_ui_policy": selected,
    }
    return {"scope": scope, "scope_digest": _digest(scope), "maximum_lifetime_seconds": 86400}


async def current_grant(backend, configuration, version, binding):
    grants = (
        await backend.db.scalars(
            select(CredentialServiceDelegation)
            .where(
                CredentialServiceDelegation.tenant_id == configuration.tenant_id,
                CredentialServiceDelegation.product_id == configuration.product_id,
                CredentialServiceDelegation.project_id == configuration.project_id,
                CredentialServiceDelegation.service_id == "l5-devops-worker",
                CredentialServiceDelegation.created_by == configuration.owner_id,
                CredentialServiceDelegation.parent_delegation_id.is_(None),
                CredentialServiceDelegation.execution_scope["purpose"].as_string() == PURPOSE,
                CredentialServiceDelegation.revoked_at.is_(None),
                CredentialServiceDelegation.expires_at > datetime.now(UTC),
            )
            .execution_options(populate_existing=True)
        )
    ).all()
    if not grants:
        return None
    if len(grants) != 1:
        raise HTTPException(403, detail={"code": "native_ui_consent_changed"})
    grant = grants[0]
    policy = (grant.execution_scope or {}).get("native_ui_policy") or {}
    proposed = await preview(
        backend, configuration, version, binding.binding_id, {field: policy.get(field) for field in ("launcher_role_arn", "operator_role_arn")}
    )
    if grant.execution_scope != proposed["scope"] or any(
        getattr(grant, field) != proposed["scope"][field]
        for field in ("credential_id", "binding_generation", "operations", "capabilities", "sources", "visibility")
    ):
        raise HTTPException(403, detail={"code": "native_ui_consent_changed"})
    return grant
