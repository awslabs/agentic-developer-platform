"""Current owner consent for exact source publication, without native credentials."""

from datetime import UTC, datetime

from fastapi import HTTPException
from l5_services.api.configuration import view
from l5_services.devops.knowledge_admission import REF_FIELDS, publication_selection
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration, ConfigurationVersion
from sqlalchemy import select

from src.auth.aws_connection_authority import owned_aws_connection, verified_connection_evidence
from src.l5_service_identity import worker_id
from src.products.models import Product, ProductProjectBinding, Project
from src.shared.identity.product_scope import owner_scope, product_member

PURPOSE = "devops_knowledge_publication"
OPERATIONS = ["knowledge:publish", "product:read", "source:pin", "source:read"]


def is_knowledge_scope(grant):
    return isinstance(grant.execution_scope, dict) and grant.execution_scope.get("purpose") == PURPOSE


def knowledge_binding_selection(binding, configuration, version, credential):
    selected = version.settings.get("devops") or {}
    selection = publication_selection(selected.get("knowledge_policy"), configuration.tenant_id, configuration.project_id)
    identity = {
        "account_id": (credential.scopes or {}).get("account_id"),
        "role_arn": (credential.scopes or {}).get("role_arn"),
        "region": selected.get("region"),
    }
    connection = selected.get("hosting_connection_ref")
    if (
        binding is None
        or binding.state != "ready"
        or (binding.product_id, binding.project_id, binding.service_id, binding.configuration_id)
        != (configuration.product_id, configuration.project_id, "devops", configuration.configuration_id)
        or binding.scope != view(configuration, version)["scope"]
        or binding.connection_ref != connection
        or binding.provider_identity != identity
        or not isinstance(binding.native_resource_ref, dict)
        or set(binding.native_resource_ref) != REF_FIELDS
        or any(not isinstance(value, str) or not value for value in binding.native_resource_ref.values())
        or binding.native_resource_ref.get("tenant_id") != configuration.tenant_id
        or binding.native_resource_ref.get("project_id") != configuration.project_id
        or binding.native_resource_ref.get("source_connection_id") != credential.id
        or binding.native_resource_ref.get("record_type") != "agent_space"
        or not binding.native_resource_ref.get("record_id")
        or version.settings.get("visibility") != "product_team"
    ):
        raise ValueError("knowledge_binding_not_ready")
    return {
        **selection,
        "native_space_ref": binding.native_resource_ref,
        "provider_identity": identity,
        "connection_ref": connection,
        "binding_revision": binding.revision,
        "binding_generation": binding.generation,
    }


async def current_knowledge_scope(db, grant, *, admitted=None):
    if not is_knowledge_scope(grant):
        return False
    scope = grant.execution_scope
    now = datetime.now(UTC)
    expiry = grant.expires_at.replace(tzinfo=grant.expires_at.tzinfo or UTC)
    if (
        grant.service_id != worker_id("devops")
        or grant.revoked_at is not None
        or expiry <= now
        or any(
            getattr(grant, field) is not None for field in ("parent_delegation_id", "operation_id", "attempt_id", "lease_fence", "installation_id")
        )
        or scope.get("control_action") != PURPOSE
        or grant.operations != OPERATIONS
        or grant.capabilities != []
        or grant.visibility != "product_team"
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
            "devops",
            grant.created_by,
            scope.get("configuration_revision"),
            "linked",
            str(relation.relationship_revision),
        )
    ):
        return False
    version = await db.get(ConfigurationVersion, (grant.tenant_id, config.configuration_id, config.current_revision), populate_existing=True)
    binding = await db.get(Binding, (grant.tenant_id, scope.get("binding_id")), populate_existing=True)
    if version is None or version.hash != scope.get("configuration_hash"):
        return False
    try:
        credential = await owned_aws_connection(db, grant.credential_id, grant.created_by, grant.tenant_id)
        verified_connection_evidence(credential)
        current = knowledge_binding_selection(binding, config, version, credential)
    except (HTTPException, ValueError, TypeError, KeyError):
        return False
    original = scope.get("knowledge_selection")
    if not isinstance(original, dict):
        return False
    allowed_offsets = {1} if admitted else {0} if admitted is False else {0, 1}
    offset = current["binding_generation"] - original.get("binding_generation", -1)
    if (
        offset not in allowed_offsets
        or current["binding_revision"] != original.get("binding_revision", -1) + offset
        or grant.binding_generation != original.get("binding_generation", -1) + 1
        or {**current, "binding_generation": original["binding_generation"], "binding_revision": original["binding_revision"]} != original
    ):
        return False
    connection = current["connection_ref"]
    return (
        connection.get("record_id") == credential.id
        and connection.get("source_connection_id") == credential.id
        and connection.get("record_revision") == credential.aws_verified_version_id
        and connection.get("tenant_id") == grant.tenant_id
        and connection.get("project_id") == grant.project_id
        and scope.get("provider_account_id") == current["provider_identity"]["account_id"]
        and scope.get("provider_role_arn") == current["provider_identity"]["role_arn"]
        and scope.get("region") == current["provider_identity"]["region"]
        and grant.sources == [{**connection, "record_type": "credential"}, *[item["source_ref"] for item in original["publications"]]]
    )


async def require_knowledge_scope(db, grant, *, admitted=None):
    if not await current_knowledge_scope(db, grant, admitted=admitted):
        raise HTTPException(
            403,
            detail={
                "code": "knowledge_publication_consent_required",
                "message": "Review current owner consent for the exact selected knowledge and native space.",
            },
        )
