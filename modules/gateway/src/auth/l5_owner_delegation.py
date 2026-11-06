"""Explicit, bounded Product-owner consent for the existing L5 grant verifier.

This route only records consent. It never enables configuration, dispatches work,
creates provider resources, or returns credentials. Ordinary setup/run admission
and the broker still recheck current authority for every operation.
"""

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

import rfc8785
from fastapi import APIRouter, Depends, Header, HTTPException, Response
from l5_services.api.configuration import view
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from src.auth.aws_connection_authority import owned_aws_connection, verified_connection_evidence
from src.auth.l5_cleanup_scope import CLEANUP_OPERATIONS, SUPPORTED_SERVICES, owned_cleanup_proof
from src.auth.l5_delegation import required_l5_work_operations
from src.auth.l5_devops_knowledge_scope import OPERATIONS as KNOWLEDGE_OPERATIONS
from src.auth.l5_devops_knowledge_scope import knowledge_binding_selection
from src.auth.l5_devops_setup_scope import LINK_OPERATIONS, RESOURCE_OPERATIONS, SETUP_OPERATIONS, setup_selection_valid, setup_target
from src.auth.l5_transform_branch_scope import BRANCHES, branch_selections
from src.l5_service_identity import WORKER_IDS, worker_id
from src.products.models import Product, Project
from src.shared.identity.product_scope import owner_scope, product_member
from src.shared.models.audit import AuditLog
from src.shared.models.vault import CredentialServiceDelegation


def _error(status, code, message):
    return HTTPException(status, detail={"code": code, "message": message})


def _aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _digest(value):
    return sha256(rfc8785.dumps(value)).hexdigest()


class GrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    expires_at: AwareDatetime
    binding_id: str | None = Field(default=None, min_length=1, max_length=36)
    for_control: bool = False
    control_action: Literal["cleanup", "devops_setup", "devops_resource_verification", "devops_knowledge_publication", "devops_native_ui"] | None = (
        None
    )

    resource_selection: dict | None = None
    native_ui_policy: dict | None = None


class ScopeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    binding_id: str
    for_control: Literal[True] = True
    control_action: Literal["devops_resource_verification", "devops_native_ui"]
    resource_selection: dict | None = None
    native_ui_policy: dict | None = None


async def _owner(backend, product_id, project_id, service_id, *, active=True):
    actor, db = backend.actor, backend.db
    if service_id not in WORKER_IDS or actor.account_type != "human" or actor.auth_source != "jwt" or actor.token_use != "access":
        raise _error(404, "not_found", "Product service not found.")
    # Deliberately exclude the platform-admin Product visibility override. A
    # service grant requires an actual, current member of this Product's team.
    member = await product_member(db, user_id=actor.user_id, org_id=actor.org_id)
    product = await db.scalar(
        select(Product)
        .where(
            Product.id == product_id,
            owner_scope(member, actor.org_id) if member else False,
        )
        .execution_options(populate_existing=True)
    )
    project = await db.get(Project, (actor.org_id, project_id), populate_existing=True)
    configuration, version = await backend.store.current(product_id, project_id, service_id)
    await db.refresh(configuration)
    await db.refresh(version)
    relationship = await backend.relationship(product_id, project_id)
    if relationship is not None:
        await db.refresh(relationship)
    if (
        member is None
        or product is None
        or project is None
        or configuration.owner_id != member.id
        or relationship is None
        or relationship.valid_to is not None
        or str(relationship.relationship_revision) != configuration.relationship_revision
        or (active and (product.archived_at is not None or project.archived_at is not None))
    ):
        raise _error(404, "not_found", "Product service not found.")
    return configuration, version


async def delegation_scope(
    backend,
    product_id,
    project_id,
    service_id,
    binding_id=None,
    for_control=False,
    control_action=None,
    resource_selection=None,
    native_ui_policy=None,
):
    configuration, version = await _owner(backend, product_id, project_id, service_id)
    if control_action == "devops_native_ui":
        from src.auth.l5_devops_native_scope import preview

        if service_id != "devops" or not for_control or not binding_id or resource_selection is not None:
            raise _error(422, "native_ui_scope_invalid", "Select the exact DevOps binding and native UI policy.")
        return await preview(backend, configuration, version, binding_id, native_ui_policy)
    if native_ui_policy is not None:
        raise _error(422, "native_ui_scope_invalid", "Native UI policy requires separate native UI consent.")
    cleanup = control_action == "cleanup"
    setup = control_action == "devops_setup"
    resource = control_action == "devops_resource_verification"
    knowledge = control_action == "devops_knowledge_publication"
    if knowledge and (service_id != "devops" or not for_control or not binding_id):
        raise _error(422, "knowledge_scope_invalid", "Select the existing DevOps space and explicit knowledge publication policy.")
    if resource and (service_id != "devops" or not for_control or not binding_id or not setup_selection_valid(version.settings)):
        raise _error(422, "resource_scope_invalid", "Select a disabled DevOps draft and its existing space.")
    if resource_selection is not None and not resource:
        raise _error(422, "resource_scope_invalid", "Resource selection requires resource verification consent.")
    if setup and (service_id != "devops" or not for_control or binding_id is not None or not setup_selection_valid(version.settings)):
        raise _error(422, "setup_scope_invalid", "Select a disabled DevOps investigation draft and the exact space to create or link.")
    if (
        control_action is not None
        and not (setup or resource or knowledge)
        and (not cleanup or not for_control or not binding_id or service_id not in SUPPORTED_SERVICES)
    ):
        raise _error(422, "cleanup_scope_invalid", "Select an ADP-created binding with an installed cleanup driver and its next control generation.")
    # Reuse the same source and connection authorization as configuration/setup.
    if not cleanup:
        await backend.authorize_settings(version.settings, project_id, service_id)
    settings = version.settings
    selected = settings.get(service_id, {}) if service_id in {"devops", "transform"} else settings
    connection = selected.get("hosting_connection_ref" if service_id == "devops" else "connection_ref")
    if not isinstance(connection, dict):
        raise _error(409, "connection_not_selected", "Select and verify an AWS connection in this service configuration.")
    credential = await owned_aws_connection(backend.db, connection.get("record_id"), backend.actor.user_id, backend.actor.org_id)
    verified_connection_evidence(credential)
    expected = {
        "tenant_id": backend.actor.org_id,
        "project_id": project_id,
        "source_connection_id": credential.id,
        "record_type": "connection",
        "record_id": credential.id,
        "record_revision": credential.aws_verified_version_id,
    }
    if connection != expected:
        raise _error(409, "connection_changed", "Refresh the selected connection and review the delegation scope again.")
    if not isinstance(selected.get("region"), str) or not selected["region"]:
        raise _error(409, "region_not_selected", "Select the provider region in this service configuration before authorizing.")
    capabilities = [] if cleanup or setup or resource or knowledge else sorted(set(settings.get("capabilities") or []))
    if not capabilities and not (cleanup or setup or resource or knowledge):
        raise _error(409, "capability_not_selected", "Select at least one service capability before granting execution authority.")
    generation = 1
    if binding_id is not None:
        binding = await backend.db.get(Binding, (backend.actor.org_id, binding_id), populate_existing=True)
        if (
            binding is None
            or binding.product_id != product_id
            or binding.project_id != project_id
            or binding.service_id != service_id
            or binding.configuration_id != configuration.configuration_id
            or binding.scope != view(configuration, version)["scope"]
            or binding.connection_ref != connection
            or binding.state in ({"cleaned", "cleanup_pending"} if cleanup else {"cleaned", "cleanup_pending", "unlinked"})
        ):
            raise _error(404, "not_found", "Current Product service binding not found.")
        generation = binding.generation + int(for_control)
        if cleanup:
            proof = await owned_cleanup_proof(backend.db, binding, configuration, version)
            if any(
                proof["provider_identity"].get(field) != value
                for field, value in (
                    ("account_id", (credential.scopes or {}).get("account_id")),
                    ("role_arn", (credential.scopes or {}).get("role_arn")),
                    ("region", selected["region"]),
                )
            ):
                raise _error(403, "cleanup_scope_invalid", "The binding belongs to a different provider identity.")
    elif for_control and not setup:
        raise _error(422, "binding_required", "Select the existing binding before authorizing its next control generation.")
    if resource:
        from src.auth.l5_devops_resource_scope import current_resource_environment, resource_selection_valid

        if not await current_resource_environment(backend.db, tenant_id=backend.actor.org_id, project_id=project_id, settings=version.settings):
            raise _error(422, "resource_environment_unavailable", "Select the current verified Product environment for this connection.")
        try:
            resource_selection = resource_selection_valid(resource_selection, binding, configuration, version, credential, generation=generation)
        except (ValueError, TypeError, KeyError):
            raise _error(
                422, "resource_scope_invalid", "Select the exact Lambda and log ARNs with a distinct resource role on this connection."
            ) from None
        from l5_services.devops.setup import original_setup

        try:
            await original_setup(
                backend.db,
                binding,
                configuration=configuration,
                settings=version.settings,
                require_setup_consent=binding.ownership == "customer_linked",
            )
        except (ValueError, TypeError, KeyError):
            raise _error(
                409,
                "setup_provenance_unavailable",
                "Read the binding's original successful create or exact-space link operation before verifying resources.",
            ) from None
    knowledge_selection = None
    if knowledge:
        try:
            knowledge_selection = knowledge_binding_selection(binding, configuration, version, credential)
        except (ValueError, TypeError, KeyError) as error:
            raise _error(422, "knowledge_scope_invalid", str(error)) from None
    sources = (
        [item["source_ref"] for item in knowledge_selection["publications"]]
        if knowledge
        else []
        if cleanup or setup or resource
        else settings.get("source_refs") or []
    )
    operations = (
        KNOWLEDGE_OPERATIONS
        if knowledge
        else RESOURCE_OPERATIONS
        if resource
        else (LINK_OPERATIONS if setup_target(settings)["setup_action"] == "link" else SETUP_OPERATIONS)
        if setup
        else CLEANUP_OPERATIONS
        if cleanup
        else sorted({operation for capability in capabilities for operation in required_l5_work_operations(service_id, sources, capability)})
    )
    scope = {
        "tenant_id": backend.actor.org_id,
        "product_id": product_id,
        "project_id": project_id,
        "service_id": worker_id(service_id),
        "credential_id": credential.id,
        "provider_account_id": (credential.scopes or {}).get("account_id"),
        "provider_role_arn": (credential.scopes or {}).get("role_arn"),
        "region": selected["region"],
        "configuration_id": configuration.configuration_id,
        "configuration_revision": version.revision,
        "configuration_hash": version.hash,
        "binding_id": binding_id,
        "binding_generation": generation,
        "created_by": backend.actor.user_id,
        "operations": operations,
        "sources": [{**connection, "record_type": "credential"}, *sources],
        "capabilities": capabilities,
        "visibility": settings.get("visibility", "only_me"),
        **(
            {"purpose": "devops_resource_verification", "control_action": "devops_resource_verification", "resource_selection": resource_selection}
            if resource
            else {}
        ),
        **({"purpose": "devops_setup", "control_action": "devops_setup"} if setup else {}),
        **(setup_target(settings) if setup and setup_target(settings)["setup_action"] == "link" else {}),
        **(
            {"purpose": "devops_knowledge_publication", "control_action": "devops_knowledge_publication", "knowledge_selection": knowledge_selection}
            if knowledge
            else {}
        ),
        **({"purpose": "owned_cleanup", "control_action": "cleanup", "owned_resource": proof} if cleanup else {}),
    }
    if service_id == "transform" and not for_control:
        branches = branch_selections(settings, backend.actor.org_id, project_id)
        if branches:
            scope[BRANCHES] = branches
    return {"scope": scope, "scope_digest": _digest(scope), "maximum_lifetime_seconds": 86400}


def _query(backend, product_id, project_id, service_id):
    return (
        select(CredentialServiceDelegation)
        .where(
            CredentialServiceDelegation.tenant_id == backend.actor.org_id,
            CredentialServiceDelegation.product_id == product_id,
            CredentialServiceDelegation.project_id == project_id,
            CredentialServiceDelegation.service_id == worker_id(service_id),
            CredentialServiceDelegation.created_by == backend.actor.user_id,
            CredentialServiceDelegation.parent_delegation_id.is_(None),
        )
        .execution_options(populate_existing=True)
    )


def _view(grant):
    return {
        "delegation_id": grant.id,
        "revision": grant.revision,
        "tenant_id": grant.tenant_id,
        "product_id": grant.product_id,
        "project_id": grant.project_id,
        "service_id": grant.service_id,
        "credential_id": grant.credential_id,
        "operations": grant.operations,
        "sources": grant.sources,
        "capabilities": grant.capabilities,
        "visibility": grant.visibility,
        "binding_generation": grant.binding_generation,
        "execution_scope": grant.execution_scope,
        "expires_at": _aware(grant.expires_at).isoformat(),
        "revoked_at": _aware(grant.revoked_at).isoformat() if grant.revoked_at else None,
        "active": grant.revoked_at is None and _aware(grant.expires_at) > datetime.now(UTC),
    }


async def authorize(backend, product_id, project_id, service_id, body, key, etag):
    preview = await delegation_scope(
        backend,
        product_id,
        project_id,
        service_id,
        body.binding_id,
        body.for_control,
        body.control_action,
        body.resource_selection,
        body.native_ui_policy,
    )
    scope, now = preview["scope"], datetime.now(UTC)
    if etag != f'"{scope["configuration_hash"]}"' or body.scope_digest != preview["scope_digest"]:
        raise _error(412, "delegation_scope_changed", "Read delegation-scope again and review the current configuration before authorizing.")
    minimum_seconds = 60
    if not now + timedelta(seconds=minimum_seconds) < body.expires_at <= now + timedelta(days=1):
        raise _error(422, "delegation_expiry_invalid", "Choose an explicit expiry more than 1 minute and no more than 24 hours from now.")
    # Serialize all grants for this Product service against configuration edits.
    # No external/provider I/O occurs while holding this lock.
    configuration = await backend.db.scalar(
        select(Configuration)
        .where(
            Configuration.tenant_id == scope["tenant_id"],
            Configuration.configuration_id == scope["configuration_id"],
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if configuration is None or configuration.current_revision != scope["configuration_revision"]:
        raise _error(412, "delegation_scope_changed", "The configuration changed. Read delegation-scope again.")
    _, version = await _owner(backend, product_id, project_id, service_id)
    if version.settings.get("enabled") is not True and body.control_action not in {
        "cleanup",
        "devops_setup",
        "devops_resource_verification",
        "devops_knowledge_publication",
        "devops_native_ui",
    }:
        raise _error(
            409,
            "configuration_disabled",
            "Complete the service prerequisites and enable its configuration, then review delegation-scope again before authorizing execution.",
        )
    credential = await owned_aws_connection(backend.db, scope["credential_id"], backend.actor.user_id, backend.actor.org_id, lock=True)
    verified_connection_evidence(credential)
    if credential.aws_verified_version_id != scope["sources"][0]["record_revision"]:
        raise _error(412, "delegation_scope_changed", "The connection changed. Read delegation-scope again.")
    if body.control_action in {"cleanup", "devops_setup", "devops_resource_verification", "devops_knowledge_publication", "devops_native_ui"}:
        current = await delegation_scope(
            backend,
            product_id,
            project_id,
            service_id,
            body.binding_id,
            body.for_control,
            body.control_action,
            body.resource_selection,
            body.native_ui_policy,
        )
        if current["scope"] != scope:
            raise _error(412, "delegation_scope_changed", "The selected control scope changed. Review its consent again.")
    identity = _digest([scope["tenant_id"], product_id, project_id, service_id, backend.actor.user_id, key])
    grant_id = str(uuid5(NAMESPACE_URL, "adp:l5-owner-delegation:" + identity))
    request_hash = _digest({"scope": scope, "expires_at": body.expires_at.astimezone(UTC).isoformat()})
    existing = await backend.db.get(CredentialServiceDelegation, grant_id, populate_existing=True)
    if existing is not None:
        receipt = await backend.db.scalar(
            select(AuditLog).where(
                AuditLog.org_id == scope["tenant_id"],
                AuditLog.event_type == "l5_service_authorized",
                AuditLog.details["delegation_id"].as_string() == grant_id,
            )
        )
        if receipt is None or receipt.details.get("request_hash") != request_hash:
            raise _error(
                409, "idempotency_conflict", "This authorization key already records a different scope or expiry. Use a new key for new consent."
            )
        if existing.revoked_at is not None or _aware(existing.expires_at) <= now:
            raise _error(409, "delegation_inactive", "This grant is revoked or expired. Review the scope and authorize with a new key.")
        return _view(existing)
    active_query = _query(backend, product_id, project_id, service_id).where(
        CredentialServiceDelegation.revoked_at.is_(None),
        CredentialServiceDelegation.expires_at > now,
        CredentialServiceDelegation.operation_id.is_(None),
    )
    # Human UI authority can coexist with agent work; it can never satisfy its scope.
    native_purpose = CredentialServiceDelegation.execution_scope["purpose"].as_string()
    active_query = active_query.where(
        native_purpose == "devops_native_ui"
        if body.control_action == "devops_native_ui"
        else (native_purpose.is_(None) | (native_purpose != "devops_native_ui"))
    )
    active = await backend.db.scalar(active_query.limit(1))
    if active is not None:
        raise _error(
            409,
            "delegation_already_active",
            "An active grant already exists. Read delegations to reuse it, or revoke it before granting a different scope or expiry.",
        )
    grant = CredentialServiceDelegation(
        id=grant_id,
        execution_scope=scope,
        **{
            field: scope[field]
            for field in (
                "tenant_id",
                "product_id",
                "project_id",
                "service_id",
                "credential_id",
                "created_by",
                "operations",
                "sources",
                "capabilities",
                "visibility",
                "binding_generation",
            )
        },
        expires_at=body.expires_at,
        revision=1,
    )
    backend.db.add(grant)
    backend.db.add(
        AuditLog(
            org_id=scope["tenant_id"],
            actor_id=backend.actor.user_id,
            event_type="l5_service_authorized",
            details={
                "delegation_id": grant_id,
                "request_hash": request_hash,
                "scope": scope,
                "expires_at": body.expires_at.astimezone(UTC).isoformat(),
            },
        )
    )
    try:
        await backend.db.commit()
    except IntegrityError:
        await backend.db.rollback()
        raise _error(
            409, "authorization_conflict", "Authorization changed concurrently. Read delegations before retrying with the same key."
        ) from None
    return _view(grant)


def create_owner_delegation_router(backend_dependency):
    router = APIRouter(prefix="/v1/products/{product_id}/projects/{project_id}/services/{service_id}", tags=["l5-delegation"])

    @router.get("/delegation-scope")
    async def preview(
        product_id: str,
        project_id: str,
        service_id: str,
        response: Response,
        binding_id: str | None = None,
        for_control: bool = False,
        control_action: Literal["cleanup", "devops_setup", "devops_resource_verification", "devops_knowledge_publication", "devops_native_ui"]
        | None = None,
        backend=Depends(backend_dependency),
    ):
        result = await delegation_scope(backend, product_id, project_id, service_id, binding_id, for_control, control_action)
        response.headers.update({"Cache-Control": "no-store", "ETag": f'"{result["scope"]["configuration_hash"]}"'})
        return result

    @router.post("/delegation-scope")
    async def resource_preview(
        product_id: str, project_id: str, service_id: str, body: ScopeRequest, response: Response, backend=Depends(backend_dependency)
    ):
        result = await delegation_scope(
            backend,
            product_id,
            project_id,
            service_id,
            body.binding_id,
            body.for_control,
            body.control_action,
            body.resource_selection,
            body.native_ui_policy,
        )
        response.headers.update({"Cache-Control": "no-store", "ETag": f'"{result["scope"]["configuration_hash"]}"'})
        return result

    @router.get("/delegations")
    async def list_grants(product_id: str, project_id: str, service_id: str, response: Response, backend=Depends(backend_dependency)):
        await _owner(backend, product_id, project_id, service_id, active=False)
        response.headers["Cache-Control"] = "no-store"
        grants = (
            await backend.db.scalars(
                _query(backend, product_id, project_id, service_id)
                .where(CredentialServiceDelegation.operation_id.is_(None))
                .order_by(
                    CredentialServiceDelegation.expires_at.desc(),
                    CredentialServiceDelegation.id,
                )
                .limit(100)
            )
        ).all()
        return {"items": [_view(grant) for grant in grants]}

    @router.post("/delegations")
    async def grant(
        product_id: str,
        project_id: str,
        service_id: str,
        body: GrantRequest,
        response: Response,
        idempotency_key: str | None = Header(default=None, min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"),
        if_match: str | None = Header(default=None),
        backend=Depends(backend_dependency),
    ):
        if not idempotency_key or if_match is None:
            raise _error(428, "precondition_required", "Supply the configuration ETag and a new Idempotency-Key after reviewing delegation-scope.")
        result = await authorize(backend, product_id, project_id, service_id, body, idempotency_key, if_match)
        response.headers.update({"Cache-Control": "no-store", "ETag": f'"{result["revision"]}"'})
        return result

    @router.delete("/delegations/{delegation_id}")
    async def revoke(
        product_id: str,
        project_id: str,
        service_id: str,
        delegation_id: str,
        response: Response,
        if_match: str | None = Header(default=None),
        backend=Depends(backend_dependency),
    ):
        # Revocation deliberately remains possible after source/connection expiry,
        # configuration disablement, or archive. It only removes authority.
        await _owner(backend, product_id, project_id, service_id, active=False)
        grant = await backend.db.scalar(
            _query(backend, product_id, project_id, service_id).where(
                CredentialServiceDelegation.id == delegation_id,
            )
        )
        if grant is None:
            raise _error(404, "not_found", "Delegation not found.")
        if if_match is None:
            raise _error(428, "precondition_required", "Supply the delegation revision in If-Match.")
        if if_match != f'"{grant.revision}"':
            raise _error(412, "stale_revision", "Read delegations again before revoking the current revision.")
        if grant.revoked_at is None:
            changed = await backend.db.execute(
                update(CredentialServiceDelegation)
                .where(
                    CredentialServiceDelegation.id == grant.id,
                    CredentialServiceDelegation.revision == grant.revision,
                    CredentialServiceDelegation.revoked_at.is_(None),
                )
                .values(revoked_at=datetime.now(UTC), revision=grant.revision + 1)
                .execution_options(synchronize_session=False)
            )
            if changed.rowcount != 1:
                raise _error(412, "stale_revision", "Delegation changed. Read delegations again.")
            backend.db.add(
                AuditLog(
                    org_id=backend.actor.org_id, actor_id=backend.actor.user_id, event_type="l5_service_revoked", details={"delegation_id": grant.id}
                )
            )
            await backend.db.commit()
            await backend.db.refresh(grant)
        result = _view(grant)
        response.headers.update({"Cache-Control": "no-store", "ETag": f'"{result["revision"]}"'})
        return result

    from src.auth.l5_knowledge_native_consent import add_routes

    add_routes(router, backend_dependency)
    from src.l5_knowledge_cancel import add_routes as add_cancel_routes

    add_cancel_routes(router, backend_dependency)
    from src.l5_knowledge_abandon import add_routes as add_abandon_routes

    add_abandon_routes(router, backend_dependency)
    from src.knowledge_evidence_routes import add_routes as add_evidence_routes

    add_evidence_routes(router, backend_dependency)
    return router
