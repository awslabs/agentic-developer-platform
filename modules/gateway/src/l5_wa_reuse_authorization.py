"""Owner consent for exact retained WA copies after terminal owned cleanup.

This authority has no worker principal, credential, setup, publication admission,
or provider operation. Every consumer release still checks current source rights.
"""

from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from adp_platform_common.l5_contributions.contract import digest
from fastapi import APIRouter, Depends, Header, HTTPException, Response
from l5_services.api.bindings import _intent_hash
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.evidence import AssessmentResult
from l5_services.models.publication import Publication
from l5_services.models.result import ResultDetail
from l5_services.models.run import Run
from l5_services.models.setup import BindingOperation, SetupIntent
from l5_services.models.source_retention import SourceRetention
from l5_services.models.wa_reuse_authorization import WellArchitectedReuseAuthorization as Authorization
from l5_services.publication.contributions import validate_receipt
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from src.products.models import Product, Project
from src.shared.identity.product_scope import owner_scope, product_member
from src.shared.models.audit import AuditLog


def error(status=404, code="not_found", message="Retained result not found."):
    return HTTPException(status, detail={"code": code, "message": message})


def aware(value):
    return value.replace(tzinfo=value.tzinfo or UTC)


async def owner(backend, product_id, project_id, result_id, *, active=True):
    actor, db = backend.actor, backend.db
    if actor.account_type != "human" or actor.auth_source != "jwt" or actor.token_use != "access":
        raise error()
    member = await product_member(db, user_id=actor.user_id, org_id=actor.org_id)
    product = await db.scalar(
        select(Product)
        .where(Product.id == product_id, owner_scope(member, actor.org_id) if member else False)
        .execution_options(populate_existing=True)
    )
    project = await db.get(Project, (actor.org_id, project_id), populate_existing=True)
    result = await db.get(AssessmentResult, (actor.org_id, result_id), populate_existing=True)
    run = await db.get(Run, (actor.org_id, result.run_id), populate_existing=True) if result else None
    config = await db.get(Configuration, (actor.org_id, run.configuration_id), populate_existing=True) if run else None
    if (
        product is None
        or project is None
        or run is None
        or config is None
        or run.service_id != "well_architected"
        or (run.product_id, run.project_id) != (product_id, project_id)
        or config.owner_id != actor.user_id
        or run.scope.get("owner_id") != actor.user_id
        or config.service_id != "well_architected"
        or (config.product_id, config.project_id) != (product_id, project_id)
        or (active and (product.archived_at is not None or project.archived_at is not None))
    ):
        raise error()
    return run, result


async def cleanup_proof(db, run, binding):
    receipt = binding.creation_receipt
    creation = await db.get(SetupIntent, (run.tenant_id, receipt.get("operation_id")), populate_existing=True) if isinstance(receipt, dict) else None
    candidates = (
        await db.scalars(
            select(SetupIntent)
            .where(
                SetupIntent.tenant_id == run.tenant_id,
                SetupIntent.product_id == run.product_id,
                SetupIntent.project_id == run.project_id,
                SetupIntent.service_id == "well_architected",
                SetupIntent.configuration_id == run.configuration_id,
                SetupIntent.binding_generation == binding.generation,
            )
            .limit(2)
            .execution_options(populate_existing=True)
        )
    ).all()
    cleanup = candidates[0] if len(candidates) == 1 else None
    operation = await db.get(BindingOperation, (run.tenant_id, cleanup.operation_id), populate_existing=True) if cleanup else None
    native = binding.native_resource_ref or {}
    cleanup_revision = (cleanup.request or {}).get("configuration_revision") if cleanup else None
    cleanup_version = (
        await db.get(ConfigurationVersion, (run.tenant_id, run.configuration_id, cleanup_revision), populate_existing=True)
        if type(cleanup_revision) is int and cleanup_revision > 0
        else None
    )
    if (
        creation is None
        or cleanup is None
        or operation is None
        or cleanup_version is None
        or binding.ownership != "adp_created"
        or binding.state != "cleaned"
        or binding.generation <= run.binding_generation
        or (binding.product_id, binding.project_id, binding.service_id, binding.configuration_id)
        != (run.product_id, run.project_id, "well_architected", run.configuration_id)
        or binding.scope != run.admission.get("scope")
        or native.get("record_type") != "agent_profile"
        or native.get("record_id") != (run.native_job_ref or {}).get("profile_arn")
        or any(
            native.get(field) != value
            for field, value in {
                "tenant_id": run.tenant_id,
                "project_id": run.project_id,
                "source_connection_id": binding.connection_ref.get("source_connection_id"),
            }.items()
        )
        or (creation.tenant_id, creation.product_id, creation.project_id, creation.service_id, creation.actor_id)
        != (run.tenant_id, run.product_id, run.project_id, run.service_id, run.scope.get("owner_id"))
        or cleanup.actor_id != run.scope.get("owner_id")
        or creation.request.get("action") != "create"
        or creation.configuration_id != run.configuration_id
        or creation.scope != binding.scope
        or creation.intent_hash != _intent_hash(creation)
        or receipt.get("intent_hash") != creation.intent_hash
        or receipt.get("native_resource_ref") != native
        or receipt.get("provider_identity") != binding.provider_identity
        or cleanup.intent_hash != _intent_hash(cleanup)
        or cleanup.scope != binding.scope
        or cleanup.owned_resource_receipt != receipt
        or cleanup.request
        != {"action": "cleanup", "configuration_revision": cleanup_revision, "binding_id": binding.binding_id, "owned_cleanup_confirmed": True}
        or operation.gaps
        or (operation.binding_id, operation.action, operation.state) != (binding.binding_id, "cleanup", "succeeded")
        or operation.cleanup_receipt
        != {
            "schema_version": 1,
            "source": "installed-worker",
            "absent": True,
            "operation_id": cleanup.operation_id,
            "intent_hash": cleanup.intent_hash,
            "creation_receipt": receipt,
            "native_resource_ref": native,
            "provider_identity": binding.provider_identity,
        }
    ):
        raise error(409, "owned_cleanup_unverified", "Complete and verify terminal owned cleanup before authorizing retained-result reuse.")
    return {
        "operation_id": cleanup.operation_id,
        "generation": binding.generation,
        "receipt_digest": digest(operation.cleanup_receipt),
        "intent_digest": cleanup.intent_hash,
        "creation_operation_id": creation.operation_id,
        "creation_intent_digest": creation.intent_hash,
    }


async def ready_observation_proof(db, run, binding):
    """Read-only provenance for a still-owned Profile; never retained-copy consent."""
    receipt = binding.creation_receipt
    creation = await db.get(SetupIntent, (run.tenant_id, receipt.get("operation_id")), populate_existing=True) if isinstance(receipt, dict) else None
    native = binding.native_resource_ref or {}
    if (
        creation is None
        or binding.state != "ready"
        or binding.ownership != "adp_created"
        or binding.generation != run.binding_generation
        or binding.gaps
        or (binding.product_id, binding.project_id, binding.service_id, binding.configuration_id)
        != (run.product_id, run.project_id, "well_architected", run.configuration_id)
        or binding.scope != run.admission.get("scope")
        or creation.scope != binding.scope
        or (creation.product_id, creation.project_id, creation.service_id, creation.configuration_id, creation.actor_id)
        != (run.product_id, run.project_id, "well_architected", run.configuration_id, run.scope.get("owner_id"))
        or creation.request.get("action") != "create"
        or creation.intent_hash != _intent_hash(creation)
        or receipt.get("intent_hash") != creation.intent_hash
        or receipt.get("native_resource_ref") != native
        or receipt.get("provider_identity") != binding.provider_identity
        or native.get("record_type") != "agent_profile"
        or native.get("record_id") != (run.native_job_ref or {}).get("profile_arn")
        or any(
            native.get(key) != value
            for key, value in {
                "tenant_id": run.tenant_id,
                "project_id": run.project_id,
                "source_connection_id": binding.connection_ref.get("source_connection_id"),
            }.items()
        )
    ):
        raise error(409, "owned_profile_unverified", "The original ready owned Profile provenance could not be verified.")
    return {
        "state": "ready",
        "creation_operation_id": creation.operation_id,
        "creation_intent_digest": creation.intent_hash,
        "creation_receipt_digest": digest(receipt),
    }


async def selection(backend, product_id, project_id, result_id, *, allow_ready_observation=False):
    run, result = await owner(backend, product_id, project_id, result_id)
    # Uses real Product membership, relationship and current pinned-source rights.
    visible = await backend.get_run(product_id, project_id, "well_architected", run.run_id)
    detail = await backend.db.get(ResultDetail, (run.tenant_id, result_id), populate_existing=True)
    binding = await backend.db.get(Binding, (run.tenant_id, run.binding_id), populate_existing=True)
    sources = run.input.get("sources")
    if (
        visible.run_id != run.run_id
        or detail is None
        or binding is None
        or run.state != "succeeded"
        or run.provider_state != "completed"
        or not run.provider_readback_ref
        or not isinstance(sources, list)
        or not sources
        or any(
            not isinstance(source, dict)
            or source.get("tenant_id") != run.tenant_id
            or source.get("project_id") != run.project_id
            or any(
                not isinstance(source.get(key), str) or not source[key]
                for key in ("source_connection_id", "record_type", "record_id", "record_revision")
            )
            for source in sources
        )
        or detail.payload.get("kind") != "well_architected_result"
        or detail.payload.get("run_id") != run.run_id
        or detail.payload.get("revision") != result.version
        or detail.payload.get("result_id") != result_id
        or detail.payload.get("scope") != run.admission.get("scope")
        or detail.payload.get("native_job_ref") != run.native_job_ref
        or detail.payload.get("review_mode") != "source_only"
        or not (run.manifest or {}).get("archive")
        or detail.payload.get("manifest_ref") != run.manifest["archive"]
        or await backend.db.scalar(
            select(SourceRetention.artifact_id)
            .where(SourceRetention.tenant_id == run.tenant_id, SourceRetention.run_id == run.run_id, SourceRetention.revoked_at.is_not(None))
            .limit(1)
        )
    ):
        raise error()
    proof = (
        await ready_observation_proof(backend.db, run, binding)
        if allow_ready_observation and binding.state == "ready"
        else await cleanup_proof(backend.db, run, binding)
    )
    publications = (
        await backend.db.scalars(
            select(Publication)
            .where(
                Publication.tenant_id == run.tenant_id,
                Publication.result_id == result_id,
                Publication.result_version == result.version,
                Publication.destination.in_(("product-context", "engineering-insights")),
            )
            .order_by(Publication.destination)
            .execution_options(populate_existing=True)
        )
    ).all()
    if len(publications) != 2 or {p.destination for p in publications} != {"product-context", "engineering-insights"}:
        raise error(409, "publication_incomplete", "Both consumers must acknowledge the exact result before reuse consent.")
    for publication in publications:
        if (
            publication.state != "published"
            or publication.schema_version != "1.0"
            or publication.invalidation_watermark != 0
            or publication.applied_watermark != 0
            or publication.invalidation is not None
            or publication.payload.get("kind") != "well_architected_result"
            or publication.payload.get("schema_version") != "1.0"
            or publication.payload.get("contribution_id") != publication.publication_id
            or publication.payload.get("source_refs") != sources
            or publication.payload.get("result") != detail.payload
            or publication.payload.get("evidence_refs") != [run.manifest["archive"]]
            or publication.payload.get("invalidation_watermark") != 0
            or publication.payload.get("scope") != run.admission.get("scope")
            or publication.payload.get("run_id") != run.run_id
            or publication.payload.get("result_ref")
            != {
                "tenant_id": run.tenant_id,
                "project_id": project_id,
                "source_connection_id": "l5-well-architected",
                "record_type": "well_architected_result",
                "record_id": result_id,
                "record_revision": str(result.version),
            }
            or publication.contribution_hash != digest(publication.payload)
        ):
            raise error()
        try:
            validate_receipt(publication, publication.receipt)
        except (ValueError, KeyError, TypeError):
            raise error() from None
    scope = {
        "tenant_id": run.tenant_id,
        "product_id": product_id,
        "project_id": project_id,
        "actor_id": backend.actor.user_id,
        "actions": ["read", "use"],
        "run_id": run.run_id,
        "result_id": result_id,
        "result_revision": result.version,
        "result_digest": digest(detail.payload),
        "native_result_digest": digest(result.payload),
        "native_job_ref": run.native_job_ref,
        "provider_readback_ref": run.provider_readback_ref,
        "source_refs": sources,
        "result_scope": run.scope,
        "cleanup": proof,
        "publications": [
            {
                "publication_id": p.publication_id,
                "destination": p.destination,
                "revision": p.revision,
                "contribution_hash": p.contribution_hash,
                "receipt_digest": digest(p.receipt),
            }
            for p in publications
        ],
    }
    return run, scope


def query(backend, result_id):
    return (
        select(Authorization)
        .where(Authorization.tenant_id == backend.actor.org_id, Authorization.actor_id == backend.actor.user_id, Authorization.result_id == result_id)
        .execution_options(populate_existing=True)
    )


async def current_reuse(backend, run, publication):
    if backend.actor.account_type != "human" or run.service_id != "well_architected":
        return None
    try:
        _, scope = await selection(backend, run.product_id, run.project_id, publication.result_id)
    except HTTPException:
        return None
    grants = (
        await backend.db.scalars(
            query(backend, publication.result_id).where(Authorization.revoked_at.is_(None), Authorization.expires_at > datetime.now(UTC)).limit(2)
        )
    ).all()
    if (
        len(grants) != 1
        or grants[0].scope != scope
        or grants[0].scope_digest != digest(scope)
        or not any(
            p["publication_id"] == publication.publication_id
            and p["destination"] == publication.destination
            and p["revision"] == publication.revision
            and p["contribution_hash"] == publication.contribution_hash
            and p["receipt_digest"] == digest(publication.receipt)
            for p in scope["publications"]
        )
    ):
        return None
    grant = grants[0]
    # Signed consumer assertions include this identity. Replacement consent cannot
    # make an assertion signed under an expired/revoked authorization valid again.
    return {"authorization_id": grant.authorization_id, "revision": grant.revision, "scope_digest": grant.scope_digest}


class Consent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    expires_at: AwareDatetime


def view(grant):
    return {
        "authorization_id": grant.authorization_id,
        "revision": grant.revision,
        "scope_digest": grant.scope_digest,
        "expires_at": aware(grant.expires_at).isoformat(),
        "revoked_at": aware(grant.revoked_at).isoformat() if grant.revoked_at else None,
    }


def create_router(backend_dependency):
    router = APIRouter(prefix="/v1/products/{product_id}/projects/{project_id}/services/{service_id}/results/{result_id}", tags=["l5-reuse"])

    def selected_backend(service_id: Literal["well_architected"], backend=Depends(backend_dependency)):
        return backend

    @router.get("/reuse-scope")
    async def preview(product_id: str, project_id: str, result_id: str, response: Response, backend=Depends(selected_backend)):
        _, scope = await selection(backend, product_id, project_id, result_id)
        hashed = digest(scope)
        response.headers.update({"Cache-Control": "no-store", "ETag": f'"{hashed}"'})
        return {"scope": scope, "scope_digest": hashed, "maximum_lifetime_seconds": 86400}

    @router.get("/reuse-authorizations")
    async def list_grants(product_id: str, project_id: str, result_id: str, response: Response, backend=Depends(selected_backend)):
        await owner(backend, product_id, project_id, result_id, active=False)
        response.headers["Cache-Control"] = "no-store"
        return {
            "items": [
                view(row) for row in (await backend.db.scalars(query(backend, result_id).order_by(Authorization.expires_at.desc()).limit(100))).all()
            ]
        }

    @router.post("/reuse-authorizations")
    async def authorize(
        product_id: str,
        project_id: str,
        result_id: str,
        body: Consent,
        response: Response,
        if_match: str | None = Header(None),
        idempotency_key: str | None = Header(None, min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"),
        backend=Depends(selected_backend),
    ):
        if if_match is None or idempotency_key is None:
            raise error(428, "precondition_required", "Review reuse-scope and supply its ETag and a fresh Idempotency-Key.")
        await owner(backend, product_id, project_id, result_id)
        await backend.db.scalar(
            select(AssessmentResult)
            .where(AssessmentResult.tenant_id == backend.actor.org_id, AssessmentResult.result_id == result_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        _, scope = await selection(backend, product_id, project_id, result_id)
        hashed, now = digest(scope), datetime.now(UTC)
        if body.scope_digest != hashed or if_match != f'"{hashed}"':
            raise error(412, "reuse_scope_changed", "Read reuse-scope again before authorizing the current result.")
        if not now + timedelta(minutes=1) < body.expires_at <= now + timedelta(days=1):
            raise error(422, "expiry_invalid", "Select an explicit expiry between one minute and 24 hours.")
        identifier = str(uuid5(NAMESPACE_URL, digest([backend.actor.org_id, result_id, backend.actor.user_id, idempotency_key])))
        request_digest = digest({"scope": scope, "expires_at": body.expires_at.astimezone(UTC).isoformat()})
        grant = await backend.db.get(Authorization, (backend.actor.org_id, identifier), populate_existing=True)
        if grant is not None:
            if grant.request_digest != request_digest:
                raise error(409, "idempotency_conflict", "This key records different consent; use a fresh key.")
            if grant.revoked_at is not None or aware(grant.expires_at) <= now:
                raise error(409, "authorization_inactive", "Revoked or expired consent cannot be restored. Review and grant fresh consent.")
        else:
            if await backend.db.scalar(query(backend, result_id).where(Authorization.revoked_at.is_(None), Authorization.expires_at > now).limit(1)):
                raise error(409, "authorization_active", "Read or revoke the current authorization before replacing it.")
            grant = Authorization(
                tenant_id=backend.actor.org_id,
                authorization_id=identifier,
                result_id=result_id,
                actor_id=backend.actor.user_id,
                scope=scope,
                scope_digest=hashed,
                request_digest=request_digest,
                revision=1,
                expires_at=body.expires_at,
            )
            backend.db.add(grant)
            backend.db.add(
                AuditLog(
                    org_id=backend.actor.org_id,
                    actor_id=backend.actor.user_id,
                    event_type="wa_result_reuse_authorized",
                    details={"authorization_id": identifier, "scope_digest": hashed, "result_id": result_id},
                )
            )
            try:
                await backend.db.commit()
            except IntegrityError:
                await backend.db.rollback()
                raise error(409, "authorization_conflict", "Consent changed concurrently. Read authorizations before retrying.") from None
        response.headers.update({"Cache-Control": "no-store", "ETag": f'"{grant.revision}"'})
        return view(grant)

    @router.delete("/reuse-authorizations/{authorization_id}")
    async def revoke(
        product_id: str,
        project_id: str,
        result_id: str,
        authorization_id: str,
        response: Response,
        if_match: str | None = Header(None),
        backend=Depends(selected_backend),
    ):
        await owner(backend, product_id, project_id, result_id, active=False)
        grant = await backend.db.scalar(query(backend, result_id).where(Authorization.authorization_id == authorization_id))
        if grant is None:
            raise error()
        if if_match is None:
            raise error(428, "precondition_required", "Supply the authorization revision in If-Match.")
        if if_match != f'"{grant.revision}"':
            raise error(412, "stale_revision", "Read reuse-authorizations again before revoking.")
        if grant.revoked_at is None:
            changed = await backend.db.execute(
                update(Authorization)
                .where(
                    Authorization.tenant_id == grant.tenant_id,
                    Authorization.authorization_id == authorization_id,
                    Authorization.revision == grant.revision,
                    Authorization.revoked_at.is_(None),
                )
                .values(revoked_at=datetime.now(UTC), revision=grant.revision + 1)
                .execution_options(synchronize_session=False)
            )
            if changed.rowcount != 1:
                raise error(412, "stale_revision", "Authorization changed; read its current revision.")
            backend.db.add(
                AuditLog(
                    org_id=backend.actor.org_id,
                    actor_id=backend.actor.user_id,
                    event_type="wa_result_reuse_revoked",
                    details={"authorization_id": authorization_id},
                )
            )
            await backend.db.commit()
            await backend.db.refresh(grant)
        response.headers.update({"Cache-Control": "no-store", "ETag": f'"{grant.revision}"'})
        return view(grant)

    return router
