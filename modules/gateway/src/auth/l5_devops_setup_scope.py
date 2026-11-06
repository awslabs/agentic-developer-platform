"""Consent to create or read/link one space, without execution or UI authority."""

import re
from datetime import UTC, datetime

from fastapi import HTTPException
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration, ConfigurationVersion
from sqlalchemy import select

from src.auth.aws_connection_authority import owned_aws_connection, verified_connection_evidence
from src.l5_service_identity import worker_id
from src.products.models import Product, ProductProjectBinding, Project
from src.shared.identity.product_scope import owner_scope, product_member

RESOURCE_OPERATIONS = ["binding:verify-resource", "credential:session"]

SETUP_OPERATIONS = ["binding:create", "credential:session"]
LINK_OPERATIONS = ["binding:link", "credential:session"]


def setup_target(settings):
    selected = settings.get("devops") or {}
    if selected.get("space_mode") == "adopt":
        space = selected.get("existing_space_id")
        if not isinstance(space, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", space):
            return None
        return {
            "setup_action": "link",
            "native_resource_ref": {
                **(selected.get("hosting_connection_ref") or {}),
                "record_type": "agent_space",
                "record_id": space,
                "record_revision": space,
            },
        }
    if selected.get("space_mode") == "create" and not selected.get("existing_space_id"):
        return {"setup_action": "create", "native_resource_ref": None}
    return None


def setup_selection_valid(settings):
    selected = settings.get("devops") or {}
    return (
        settings.get("enabled") is False
        and settings.get("capabilities") == ["investigation"]
        and not settings.get("triggers")
        and setup_target(settings) is not None
        and not selected.get("integrations")
        and not selected.get("trigger_policies")
        and not selected.get("incident_sources")
        and not selected.get("prevention_enabled")
        and not (selected.get("prevention_policy") or {}).get("enabled")
        and (
            selected.get("browser_launch_policy") in {None, "disabled"}
            or selected.get("browser_launch_policy") == "scoped_session"
            and selected.get("authentication_mode") == "scoped_iam_federation"
        )
        and not selected.get("permitted_native_actions")
    )


def is_devops_setup_scope(grant):
    return isinstance(grant.execution_scope, dict) and grant.execution_scope.get("purpose") == "devops_setup"


def is_devops_resource_scope(grant):
    return isinstance(grant.execution_scope, dict) and grant.execution_scope.get("purpose") == "devops_resource_verification"


async def current_devops_setup_scope(db, grant, *, action=None, binding_generation=1, resource=False, admitted=None):
    if not (is_devops_resource_scope(grant) if resource else is_devops_setup_scope(grant)):
        return False
    scope = grant.execution_scope
    expected_action = "check" if resource else scope.get("setup_action", "create")
    expected_operations = RESOURCE_OPERATIONS if resource else LINK_OPERATIONS if expected_action == "link" else SETUP_OPERATIONS
    expiry = grant.expires_at.replace(tzinfo=UTC) if grant.expires_at.tzinfo is None else grant.expires_at
    if (
        grant.service_id != worker_id("devops")
        or expected_action not in {"check", "create", "link"}
        or action is not None
        and action != expected_action
        or binding_generation != grant.binding_generation
        or (not resource and grant.binding_generation != 1)
        or grant.revoked_at is not None
        or expiry <= datetime.now(UTC)
        or any(
            getattr(grant, field) is not None for field in ("parent_delegation_id", "operation_id", "attempt_id", "lease_fence", "installation_id")
        )
        or scope.get("control_action") != ("devops_resource_verification" if resource else "devops_setup")
        or (not resource and scope.get("binding_id") is not None)
        or grant.operations != expected_operations
        or grant.capabilities != []
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
        .where(
            Product.id == grant.product_id,
            owner_scope(member, grant.tenant_id) if member else False,
        )
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
    if version is None or version.hash != scope.get("configuration_hash") or not setup_selection_valid(version.settings):
        return False
    target = setup_target(version.settings)
    if not resource and (target["setup_action"] != expected_action or target["native_resource_ref"] != scope.get("native_resource_ref")):
        return False
    try:
        credential = await owned_aws_connection(db, grant.credential_id, grant.created_by, grant.tenant_id)
        verified_connection_evidence(credential)
    except HTTPException:
        return False
    expected = {
        "tenant_id": grant.tenant_id,
        "project_id": grant.project_id,
        "source_connection_id": credential.id,
        "record_type": "connection",
        "record_id": credential.id,
        "record_revision": credential.aws_verified_version_id,
    }
    selected, identity = version.settings["devops"], credential.scopes or {}
    if resource:
        binding = await db.get(Binding, (grant.tenant_id, scope.get("binding_id")), populate_existing=True)
        try:
            from src.auth.l5_devops_resource_scope import current_resource_environment, resource_selection_valid

            resource_selection_valid(
                scope.get("resource_selection"), binding, config, version, credential, generation=grant.binding_generation, admitted=admitted
            )
            from l5_services.devops.setup import original_setup

            if not await current_resource_environment(db, tenant_id=grant.tenant_id, project_id=grant.project_id, settings=version.settings):
                return False
            await original_setup(
                db, binding, configuration=config, settings=version.settings, require_setup_consent=binding.ownership == "customer_linked"
            )
        except (ValueError, TypeError, KeyError):
            return False
    return (
        selected.get("hosting_connection_ref") == expected
        and grant.sources == [{**expected, "record_type": "credential"}]
        and selected.get("region") == scope.get("region")
        and version.settings.get("visibility", "only_me") == grant.visibility
        and identity.get("account_id") == scope.get("provider_account_id")
        and identity.get("role_arn") == scope.get("provider_role_arn")
    )


async def require_devops_setup_scope(db, grant, **target):
    if not await current_devops_setup_scope(db, grant, **target):
        raise HTTPException(
            403,
            detail={
                "code": "setup_consent_required",
                "message": (
                    "Review current owner consent to create or link the exact disabled DevOps space. "
                    "This consent cannot authorize execution or native UI access."
                ),
            },
        )


async def current_devops_resource_scope(db, grant, *, admitted=None):
    return await current_devops_setup_scope(db, grant, action="check", binding_generation=grant.binding_generation, resource=True, admitted=admitted)


async def require_devops_resource_scope(db, grant, *, admitted=None):
    if not await current_devops_resource_scope(db, grant, admitted=admitted):
        raise HTTPException(
            403,
            detail={
                "code": "resource_verification_consent_required",
                "message": "Review current owner consent for this exact space, resource role, Lambda function and log group.",
            },
        )
