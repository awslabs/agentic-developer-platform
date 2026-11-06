"""Separate owner consent for exact, receipt-backed resource cleanup only.

This scope is deliberately unusable by Product reads, source brokers and normal
execution. Disabling execution or removing content sources does not authorize
content access and does not prevent a fresh, explicit cleanup consent.
"""

from datetime import UTC, datetime

from fastapi import HTTPException
from l5_services.api.bindings import _intent_hash
from l5_services.api.configuration import view
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.setup import SetupIntent
from sqlalchemy import select

from src.auth.aws_connection_authority import owned_aws_connection, verified_connection_evidence
from src.l5_service_identity import service_for_worker
from src.products.models import Product, ProductProjectBinding, Project
from src.shared.identity.product_scope import owner_scope, product_member

SUPPORTED_SERVICES = frozenset({"security", "well_architected"})
CLEANUP_OPERATIONS = ["binding:cleanup", "credential:session"]


def is_cleanup_scope(grant):
    return isinstance(grant.execution_scope, dict) and grant.execution_scope.get("purpose") == "owned_cleanup"


def denied():
    return HTTPException(
        403,
        detail={
            "code": "cleanup_consent_required",
            "message": ("Review and explicitly authorize cleanup of this exact ADP-created binding with the current owner and verified connection."),
        },
    )


async def owned_cleanup_proof(db, binding, configuration, version, *, allow_cleaned=False):
    """Re-read durable create provenance; never infer ownership from a label."""
    receipt = binding.creation_receipt if binding is not None else None
    if (
        binding is None
        or configuration.service_id not in SUPPORTED_SERVICES
        or binding.ownership != "adp_created"
        or not isinstance(receipt, dict)
        or not isinstance(binding.native_resource_ref, dict)
        or not isinstance(binding.provider_identity, dict)
        or (binding.tenant_id, binding.product_id, binding.project_id, binding.service_id, binding.configuration_id)
        != (configuration.tenant_id, configuration.product_id, configuration.project_id, configuration.service_id, configuration.configuration_id)
        or binding.scope != view(configuration, version)["scope"]
        or (binding.state == "cleaned" and not allow_cleaned)
        or receipt.get("native_resource_ref") != binding.native_resource_ref
        or receipt.get("provider_identity") != binding.provider_identity
    ):
        raise denied()
    original = await db.get(SetupIntent, (binding.tenant_id, receipt.get("operation_id")), populate_existing=True)
    if (
        original is None
        or original.request.get("action") != "create"
        or (original.product_id, original.project_id, original.service_id, original.configuration_id, original.scope)
        != (binding.product_id, binding.project_id, binding.service_id, binding.configuration_id, binding.scope)
        or original.intent_hash != _intent_hash(original)
        or receipt.get("intent_hash") != original.intent_hash
    ):
        raise denied()
    proof = {"native_resource_ref": binding.native_resource_ref, "provider_identity": binding.provider_identity, "creation_receipt": receipt}
    if configuration.service_id == "well_architected":
        original_version = await db.get(
            ConfigurationVersion,
            (binding.tenant_id, binding.configuration_id, original.request.get("configuration_revision")),
            populate_existing=True,
        )
        if original_version is None:
            raise denied()
        selection = original_version.settings.get("well_architected") or {}
        role = (selection.get("execution_role_ref") or {}).get("record_id")
        pillars = selection.get("pillars")
        if role != binding.provider_identity.get("execution_role_arn") or not isinstance(pillars, list) or not pillars:
            raise denied()
        proof["profile_selection"] = {"execution_role_arn": role, "pillars": pillars, "creation_configuration_hash": original_version.hash}
    return proof


async def current_cleanup_scope(db, grant, *, binding_id=None, binding_generation=None, admitted=False):
    if not is_cleanup_scope(grant):
        return False
    scope = grant.execution_scope
    service = service_for_worker(grant.service_id)
    expiry = grant.expires_at.replace(tzinfo=UTC) if grant.expires_at.tzinfo is None else grant.expires_at
    if (
        service not in SUPPORTED_SERVICES
        or grant.revoked_at is not None
        or expiry <= datetime.now(UTC)
        or grant.parent_delegation_id is not None
        or grant.operation_id is not None
        or grant.attempt_id is not None
        or grant.lease_fence is not None
        or grant.installation_id is not None
        or any(
            scope.get(field) != getattr(grant, field)
            for field in (
                "tenant_id",
                "product_id",
                "project_id",
                "service_id",
                "credential_id",
                "created_by",
                "binding_generation",
                "visibility",
                "operations",
                "capabilities",
                "sources",
            )
        )
        or grant.operations != CLEANUP_OPERATIONS
        or grant.capabilities != []
        or scope.get("control_action") != "cleanup"
        or not scope.get("binding_id")
        or (binding_id is not None and binding_id != scope["binding_id"])
        or (binding_generation is not None and binding_generation != scope["binding_generation"])
    ):
        return False
    config = await db.get(Configuration, (grant.tenant_id, scope.get("configuration_id")), populate_existing=True)
    member = await product_member(db, user_id=grant.created_by, org_id=grant.tenant_id)
    product = await db.scalar(
        select(Product)
        .where(Product.id == grant.product_id, owner_scope(member, grant.tenant_id) if member else False)
        .execution_options(populate_existing=True)
    )
    project = await db.get(Project, (grant.tenant_id, grant.project_id), populate_existing=True)
    relation = await db.scalar(
        select(ProductProjectBinding)
        .where(
            ProductProjectBinding.tenant_id == grant.tenant_id,
            ProductProjectBinding.product_id == grant.product_id,
            ProductProjectBinding.project_id == grant.project_id,
            ProductProjectBinding.valid_to.is_(None),
        )
        .execution_options(populate_existing=True)
    )
    if (
        config is None
        or member is None
        or product is None
        or project is None
        or relation is None
        or product.archived_at is not None
        or project.archived_at is not None
        or (
            config.product_id,
            config.project_id,
            config.service_id,
            config.owner_id,
            config.current_revision,
            config.handoff_state,
            config.relationship_revision,
        )
        != (
            grant.product_id,
            grant.project_id,
            service,
            grant.created_by,
            scope.get("configuration_revision"),
            "linked",
            str(relation.relationship_revision),
        )
    ):
        return False
    version = await db.get(ConfigurationVersion, (grant.tenant_id, config.configuration_id, config.current_revision), populate_existing=True)
    binding = await db.get(Binding, (grant.tenant_id, scope["binding_id"]), populate_existing=True)
    if version is None or version.hash != scope.get("configuration_hash") or binding is None:
        return False
    connection = version.settings.get("connection_ref")
    try:
        credential = await owned_aws_connection(db, grant.credential_id, grant.created_by, grant.tenant_id)
        verified_connection_evidence(credential)
        proof = await owned_cleanup_proof(db, binding, config, version)
    except HTTPException:
        return False
    identity = credential.scopes or {}
    expected = {
        "tenant_id": grant.tenant_id,
        "project_id": grant.project_id,
        "source_connection_id": credential.id,
        "record_type": "connection",
        "record_id": credential.id,
        "record_revision": credential.aws_verified_version_id,
    }
    return (
        connection == expected
        and binding.connection_ref == connection
        and grant.sources == [{**connection, "record_type": "credential"}]
        and version.settings.get("region") == scope.get("region")
        and version.settings.get("visibility", "only_me") == grant.visibility
        and identity.get("account_id") == scope.get("provider_account_id")
        and identity.get("role_arn") == scope.get("provider_role_arn")
        and all(
            proof["provider_identity"].get(key) == value
            for key, value in (
                ("account_id", scope.get("provider_account_id")),
                ("role_arn", scope.get("provider_role_arn")),
                ("region", scope.get("region")),
            )
        )
        and proof == scope.get("owned_resource")
        and binding.generation + (0 if admitted else 1) == grant.binding_generation
        and (admitted or binding.state != "cleanup_pending")
    )


async def require_cleanup_scope(db, grant, **target):
    if not await current_cleanup_scope(db, grant, **target):
        raise denied()
