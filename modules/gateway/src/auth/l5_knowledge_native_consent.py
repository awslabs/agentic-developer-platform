"""Separate explicit owner authority for one retained native knowledge operation."""

from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from fastapi import Depends, Header, HTTPException, Response
from l5_services.api.bindings import _intent_hash
from l5_services.devops.knowledge_admission import digest
from l5_services.models.configuration import Configuration
from l5_services.models.setup import BindingOperation, SetupIntent
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import select

from src.auth.l5_devops_knowledge_scope import current_knowledge_scope
from src.products.source_routes import service_visible_bindings, source_ref_authorized
from src.shared.models.audit import AuditLog
from src.shared.models.vault import CredentialServiceDelegation

NATIVE_PURPOSE = "devops_knowledge_native_publication"
NATIVE_OPERATIONS = ["knowledge:asset-write"]
NATIVE_ACTIONS = ["CreateAsset", "UpdateAsset", "GetAsset", "GetAssetContent", "ListAssets"]


def native_scope(intent, source_grant):
    selected = intent.authorization["selection"]
    admission = intent.authorization["knowledge_admission"]
    return {
        "purpose": NATIVE_PURPOSE,
        "tenant_id": intent.tenant_id,
        "product_id": intent.product_id,
        "project_id": intent.project_id,
        "service_id": source_grant.service_id,
        "created_by": intent.actor_id,
        "operation_id": intent.operation_id,
        "intent_hash": intent.intent_hash,
        "source_delegation_id": source_grant.id,
        "source_delegation_revision": source_grant.revision,
        "configuration_id": intent.configuration_id,
        "configuration_revision": intent.request["configuration_revision"],
        "binding_id": intent.request["binding_id"],
        "binding_generation": intent.binding_generation,
        "native_space_ref": selected["native_space_ref"],
        "provider_identity": selected["provider_identity"],
        "connection_ref": selected["connection_ref"],
        "admission_hash": digest(admission),
        "snapshot_ids": [item["snapshot_id"] for item in admission["source_snapshots"]],
        "operations": NATIVE_OPERATIONS,
        "native_actions": NATIVE_ACTIONS,
        "removals": [],
    }


async def original_authority(db, intent):
    denied = HTTPException(403, detail={"code": "knowledge_source_consent_required"})
    if (
        intent is None
        or intent.service_id != "devops"
        or intent.request.get("action") != "sync_knowledge"
        or intent.intent_hash != _intent_hash(intent)
    ):
        raise denied
    grant = await db.get(CredentialServiceDelegation, intent.authorization.get("delegation_id"), populate_existing=True)
    if (
        grant is None
        or grant.revision != intent.authorization.get("delegation_revision")
        or not await current_knowledge_scope(db, grant, admitted=True)
    ):
        raise denied
    visible = await service_visible_bindings(db, grant)
    if any(
        not any(source_ref_authorized(grant, binding, item["source_ref"]) for binding in visible)
        for item in intent.authorization["knowledge_admission"]["source_snapshots"]
    ):
        raise HTTPException(403, detail={"code": "knowledge_source_access_revoked"})
    return grant


async def current_native_grant(db, grant, intent):
    if grant is None:
        raise HTTPException(403, detail={"code": "knowledge_native_consent_required"})
    await db.refresh(grant)
    source_grant = await original_authority(db, intent)
    expected = native_scope(intent, source_grant)
    credential_ref = {**expected["connection_ref"], "record_type": "credential"}
    if (
        grant is None
        or grant.execution_scope != expected
        or grant.parent_delegation_id != source_grant.id
        or grant.operation_id != intent.operation_id
        or grant.attempt_id is not None
        or grant.lease_fence is not None
        or grant.installation_id is not None
        or grant.revoked_at is not None
        or grant.expires_at.replace(tzinfo=grant.expires_at.tzinfo or UTC) <= datetime.now(UTC)
        or (
            grant.tenant_id,
            grant.product_id,
            grant.project_id,
            grant.created_by,
            grant.service_id,
            grant.credential_id,
            grant.binding_generation,
            grant.visibility,
        )
        != (
            intent.tenant_id,
            intent.product_id,
            intent.project_id,
            intent.actor_id,
            source_grant.service_id,
            source_grant.credential_id,
            intent.binding_generation,
            "product_team",
        )
        or grant.operations != NATIVE_OPERATIONS
        or grant.sources != [credential_ref]
        or grant.capabilities != []
    ):
        raise HTTPException(403, detail={"code": "knowledge_native_consent_required"})
    return source_grant


class NativeConsentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    expires_at: AwareDatetime


async def preview(backend, product_id, project_id, service_id, operation_id):
    from src.auth.l5_owner_delegation import _owner

    await _owner(backend, product_id, project_id, service_id)
    intent = await backend.db.get(SetupIntent, (backend.actor.org_id, operation_id), populate_existing=True)
    if (
        service_id != "devops"
        or intent is None
        or (intent.product_id, intent.project_id, intent.actor_id) != (product_id, project_id, backend.actor.user_id)
    ):
        raise HTTPException(404, detail={"code": "not_found"})
    source = await original_authority(backend.db, intent)
    operation = await backend.db.get(BindingOperation, (intent.tenant_id, operation_id), populate_existing=True)
    if operation is None or operation.state not in {"blocked", "accepted", "running", "waiting", "outcome_unknown"}:
        raise HTTPException(409, detail={"code": "knowledge_operation_not_active"})
    scope = native_scope(intent, source)
    return (
        intent,
        source,
        operation,
        {
            "scope": scope,
            "scope_digest": digest(scope),
            "expires_before": source.expires_at.replace(tzinfo=source.expires_at.tzinfo or UTC).isoformat(),
            "message": "Authorize the listed asset actions for this original operation. Saving consent does not dispatch work.",
        },
    )


def add_routes(router, backend_dependency):
    path = "/binding-operations/{operation_id}/knowledge-native-consent"

    @router.get(path)
    async def read_scope(
        product_id: str, project_id: str, service_id: str, operation_id: str, response: Response, backend=Depends(backend_dependency)
    ):
        _, _, operation, result = await preview(backend, product_id, project_id, service_id, operation_id)
        response.headers.update({"Cache-Control": "no-store", "ETag": f'"{operation.revision}"'})
        return result

    @router.post(path)
    async def create(
        product_id: str,
        project_id: str,
        service_id: str,
        operation_id: str,
        body: NativeConsentRequest,
        response: Response,
        idempotency_key: str = Header(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"),
        if_match: str = Header(),
        backend=Depends(backend_dependency),
    ):
        intent, source, operation, result = await preview(backend, product_id, project_id, service_id, operation_id)
        await backend.db.scalar(
            select(Configuration)
            .where(Configuration.tenant_id == intent.tenant_id, Configuration.configuration_id == intent.configuration_id)
            .with_for_update()
        )
        intent, source, operation, result = await preview(backend, product_id, project_id, service_id, operation_id)
        if if_match != f'"{operation.revision}"' or body.scope_digest != result["scope_digest"]:
            raise HTTPException(412, detail={"code": "knowledge_native_consent_changed"})
        now = datetime.now(UTC)
        if not now + timedelta(seconds=60) < body.expires_at <= datetime.fromisoformat(result["expires_before"]):
            raise HTTPException(422, detail={"code": "knowledge_native_consent_expiry_invalid"})
        grant_id = str(uuid5(NAMESPACE_URL, digest([intent.tenant_id, operation_id, intent.actor_id, idempotency_key])))
        request_hash = digest({"scope": result["scope"], "expires_at": body.expires_at.isoformat()})
        existing = await backend.db.get(CredentialServiceDelegation, grant_id, populate_existing=True)
        if existing is not None:
            audit = await backend.db.scalar(
                select(AuditLog).where(
                    AuditLog.org_id == intent.tenant_id,
                    AuditLog.event_type == "l5_knowledge_native_authorized",
                    AuditLog.details["delegation_id"].as_string() == grant_id,
                )
            )
            if audit is None or audit.details.get("request_hash") != request_hash:
                raise HTTPException(409, detail={"code": "idempotency_conflict"})
            await current_native_grant(backend.db, existing, intent)
        else:
            active = await backend.db.scalar(
                select(CredentialServiceDelegation.id).where(
                    CredentialServiceDelegation.parent_delegation_id == source.id,
                    CredentialServiceDelegation.operation_id == operation_id,
                    CredentialServiceDelegation.revoked_at.is_(None),
                    CredentialServiceDelegation.expires_at > now,
                )
            )
            if active:
                raise HTTPException(409, detail={"code": "knowledge_native_consent_already_active"})
            existing = CredentialServiceDelegation(
                id=grant_id,
                tenant_id=intent.tenant_id,
                product_id=product_id,
                project_id=project_id,
                created_by=intent.actor_id,
                service_id=source.service_id,
                credential_id=source.credential_id,
                parent_delegation_id=source.id,
                operation_id=operation_id,
                binding_generation=intent.binding_generation,
                visibility="product_team",
                operations=NATIVE_OPERATIONS,
                sources=[{**result["scope"]["connection_ref"], "record_type": "credential"}],
                capabilities=[],
                revision=1,
                expires_at=body.expires_at,
                execution_scope=result["scope"],
            )
            backend.db.add(existing)
            backend.db.add(
                AuditLog(
                    org_id=intent.tenant_id,
                    actor_id=intent.actor_id,
                    event_type="l5_knowledge_native_authorized",
                    details={
                        "delegation_id": grant_id,
                        "operation_id": operation_id,
                        "request_hash": request_hash,
                        "scope": result["scope"],
                        "expires_at": body.expires_at.isoformat(),
                    },
                )
            )
            await backend.db.commit()
        response.headers["Cache-Control"] = "no-store"
        return {
            "delegation_id": existing.id,
            "revision": existing.revision,
            "operation_id": operation_id,
            "expires_at": existing.expires_at.replace(tzinfo=existing.expires_at.tzinfo or UTC).isoformat(),
            "scope": result["scope"],
            "work_dispatched": False,
        }

    @router.delete(path + "/{delegation_id}")
    async def revoke(
        product_id: str,
        project_id: str,
        service_id: str,
        operation_id: str,
        delegation_id: str,
        response: Response,
        if_match: str = Header(),
        backend=Depends(backend_dependency),
    ):
        from src.auth.l5_owner_delegation import _owner

        await _owner(backend, product_id, project_id, service_id, active=False)
        grant = await backend.db.get(CredentialServiceDelegation, delegation_id, with_for_update=True, populate_existing=True)
        if (
            service_id != "devops"
            or grant is None
            or (grant.execution_scope or {}).get("purpose") != NATIVE_PURPOSE
            or (grant.tenant_id, grant.product_id, grant.project_id, grant.created_by, grant.operation_id)
            != (backend.actor.org_id, product_id, project_id, backend.actor.user_id, operation_id)
        ):
            raise HTTPException(404, detail={"code": "not_found"})
        if if_match != f'"{grant.revision}"':
            raise HTTPException(412, detail={"code": "stale_revision"})
        if grant.revoked_at is None:
            grant.revoked_at, grant.revision = datetime.now(UTC), grant.revision + 1
            backend.db.add(
                AuditLog(
                    org_id=grant.tenant_id,
                    actor_id=grant.created_by,
                    event_type="l5_knowledge_native_revoked",
                    details={"delegation_id": grant.id, "operation_id": operation_id},
                )
            )
            await backend.db.commit()
        response.headers["Cache-Control"] = "no-store"
        return {"delegation_id": grant.id, "revision": grant.revision, "revoked": True}
