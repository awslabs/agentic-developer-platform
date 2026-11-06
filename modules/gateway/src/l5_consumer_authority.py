"""Current Product/run and canonical Task authority for copied L5 contributions.

This module cannot submit native work. Signed assertions bind a single request;
consumers call back here before each durable effect and before every read release.
"""

import asyncio
from datetime import UTC, datetime

from adp_platform_common.l5_contributions.contract import digest, kind_of, storage_key
from fastapi import HTTPException
from l5_services.models.binding import Binding
from l5_services.models.publication import Publication
from l5_services.models.run import Run

from src.products.models import Product, Project
from src.shared.identity.product_scope import current_l5_parent, service_product_scope
from src.shared.models.vault import CredentialServiceDelegation
from src.shared.schemas.auth import TokenContext


def denied():
    return HTTPException(404, detail={"code": "not_found"})


async def task_authorized(db, actor, run, task_id):
    from src.tasks import authz
    from src.tasks.errors import TaskApiError
    from src.tasks.routes import get_store

    if actor.account_type != "human" or not task_id or task_id == run.run_id:
        return False
    try:
        caller = await authz.resolve_caller(actor, frozenset(), db)
        caller.require(authz.SCOPE_READ)
        task = await asyncio.to_thread(authz.authorize_task, caller, get_store(), task_id)
    except (TaskApiError, HTTPException):
        return False
    admission = run.admission or {}
    source_ids = {run.run_id, run.trigger_id, admission.get("task_id"), run.input.get("task_id")}
    if (
        task.task_id in source_ids
        or task.invocation_id in source_ids
        or (task.external_reference is not None and task.external_reference in source_ids)
        or (task.product_id, task.project_id) != (run.product_id, run.project_id)
        or task.tenant_id != run.tenant_id
    ):
        return False
    try:
        created = datetime.fromisoformat(task.created_at.replace("Z", "+00:00"))
        admitted = datetime.fromisoformat(admission["admitted_at"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return False
    return created.tzinfo is not None and admitted.tzinfo is not None and created > admitted


def snapshot(run, publication):
    return digest(
        {
            "run": {
                field: getattr(run, field)
                for field in (
                    "tenant_id",
                    "product_id",
                    "project_id",
                    "service_id",
                    "run_id",
                    "binding_id",
                    "binding_generation",
                    "configuration_id",
                    "configuration_revision",
                    "input",
                    "admission",
                    "scope",
                )
            },
            "publication": {
                field: getattr(publication, field)
                for field in (
                    "publication_id",
                    "result_id",
                    "result_version",
                    "destination",
                    "schema_version",
                    "payload",
                    "contribution_hash",
                    "state",
                    "revision",
                    "invalidation_watermark",
                    "invalidation",
                    "receipt",
                )
            },
            "retained_result_authorization": getattr(run, "_wa_reuse_authorization", None),
        }
    )


async def current(db, actor, *, tenant_id, contribution_id, destination, action, delegation_id=None, task_id=None):
    from src.l5_integration import GatewayConfigurationBackend
    from src.l5_service_identity import contribution_worker_id, worker_id

    publication = await db.get(Publication, (tenant_id, contribution_id), populate_existing=True)
    if publication is None or publication.destination != destination:
        raise denied()
    run = await db.get(Run, (tenant_id, publication.payload.get("run_id")), populate_existing=True)
    if (
        run is None
        or publication.schema_version != "1.0"
        or kind_of(publication.payload)
        != {
            "security": "security_result",
            "devops": "devops_result",
            "finops": "finops_cost_result",
            "transform": "transform_result",
            "well_architected": "well_architected_result",
        }.get(run.service_id)
        or publication.payload.get("scope") != (run.admission or {}).get("scope")
        or publication.payload.get("contribution_id") != publication.publication_id
        or publication.contribution_hash != digest(publication.payload)
        or publication.result_id != publication.payload.get("result_ref", {}).get("record_id")
        or str(publication.result_version) != publication.payload.get("result_ref", {}).get("record_revision")
    ):
        raise denied()
    run._wa_reuse_authorization = None
    admission = run.admission or {}
    grant = await db.get(CredentialServiceDelegation, admission.get("delegation_id"), populate_existing=True)
    if (
        grant is None
        or grant.tenant_id != tenant_id
        or grant.service_id != worker_id(run.service_id)
        or (grant.product_id, grant.project_id) != (run.product_id, run.project_id)
        or any(publication.payload["scope"].get(key) != getattr(run, key) for key in ("tenant_id", "product_id", "project_id"))
    ):
        raise denied()
    if action == "invalidate":
        # Revocation must not prevent removing an already copied result. Only the
        # exact registered producer may apply the durable, producer-owned tombstone.
        if (
            actor.account_type != "service"
            or actor.auth_source != "iam"
            or contribution_worker_id(actor) != grant.service_id
            or delegation_id != grant.id
            or publication.state != "invalidation_pending"
            or publication.invalidation is None
            or publication.invalidation_watermark <= 0
        ):
            raise denied()
        return run, publication
    # Product profile/read history intentionally includes archived parents. Those
    # readers cannot authorize releasing copied results or recording new reuse.
    # Invalidation above remains available so archive/revocation cannot strand a copy.
    product = await db.get(Product, (tenant_id, run.product_id), populate_existing=True)
    project = await db.get(Project, (tenant_id, run.project_id), populate_existing=True)
    if product is None or project is None or product.archived_at is not None or project.archived_at is not None:
        raise denied()
    if publication.state not in {"pending", "publishing", "retry_wait", "published"} or publication.invalidation_watermark:
        raise denied()
    # Explicit result-only consent is separate from execution delegation. It is
    # available solely to its human owner for existing copies after proven owned
    # cleanup; it cannot admit a publication or authorize a provider operation.
    if run.service_id == "well_architected" and actor.account_type == "human" and action in {"read", "use"}:
        from src.l5_wa_reuse_authorization import current_reuse

        reuse = await current_reuse(GatewayConfigurationBackend(db, actor), run, publication)
        if reuse is not None:
            if action == "use" and not await task_authorized(db, actor, run, task_id):
                raise denied()
            run._wa_reuse_authorization = reuse
            return run, publication
    sources = (
        (run.manifest or {}).get("sources", [])
        if run.service_id == "devops" and run.input.get("capability") == "release_readiness"
        else run.input.get("sources", [])
    )
    if (
        not isinstance(sources, list)
        or any(source not in grant.sources for source in sources)
        or (sources and "source:read" not in grant.operations)
        or run.input.get("capability") not in grant.capabilities
        or ("source_refs" in publication.payload and publication.payload["source_refs"] != sources)
    ):
        raise denied()
    binding = await db.get(Binding, (tenant_id, run.binding_id), populate_existing=True)
    if (
        binding is None
        or binding.state != "ready"
        or binding.generation != run.binding_generation
        or grant.binding_generation != binding.generation
        or grant.revision != admission.get("delegation_version")
        or binding.service_id != run.service_id
        or (binding.product_id, binding.project_id) != (run.product_id, run.project_id)
        or not await current_l5_parent(db, grant)
    ):
        raise denied()
    if actor.account_type == "service":
        producer_id = contribution_worker_id(actor)
        # The actor in assertions remains the actual IAM principal. This local
        # copy only checks the original producer grant, within this read boundary.
        scope_actor = actor.model_copy(update={"agent_registry_id": producer_id})
        if (
            delegation_id != grant.id
            or producer_id != worker_id(run.service_id)
            or action not in {"admit", "read"}
            or await service_product_scope(db, scope_actor, grant.id, run.product_id, run.project_id, "product:read") is None
        ):
            raise denied()
        # Current human visibility constrains the already-authorized delegation;
        # this identity never leaves this scope or authorizes execution.
        reader = actor.model_copy(
            update={"account_type": "human", "user_id": grant.created_by, "org_id": tenant_id, "is_admin": False, "scope": "", "cognito_username": ""}
        )
    elif actor.account_type == "human" and actor.org_id == tenant_id and action in {"read", "use"}:
        if publication.state != "published" or publication.receipt is None:
            raise denied()
        reader = actor
    else:
        raise denied()
    backend = GatewayConfigurationBackend(db, reader)
    visible = await backend.get_run(run.product_id, run.project_id, run.service_id, run.run_id)
    if visible.run_id != run.run_id:
        raise denied()
    # Security/WA/DevOps readers also need a fresh live grant; run readers alone
    # mainly prove source visibility and cannot replace publication authority.
    expiry = grant.expires_at.replace(tzinfo=grant.expires_at.tzinfo or UTC)
    if grant.revoked_at is not None or expiry <= datetime.now(UTC):
        raise denied()
    if action == "use" and not await task_authorized(db, actor, run, task_id):
        raise denied()
    return run, publication


def claims_for(actor, run, publication, *, action, delegation_id=None, usage=None):
    claims = {
        "actor": actor.model_dump(mode="json"),
        "tenant_id": run.tenant_id,
        "scope": publication.payload["scope"],
        "result_ref": publication.payload["result_ref"],
        "contribution_id": publication.publication_id,
        "contribution_hash": publication.contribution_hash,
        "kind": kind_of(publication.payload),
        "run_id": run.run_id,
        "destination": publication.destination,
        "action": action,
        "delegation_id": delegation_id,
        "snapshot": snapshot(run, publication),
        "watermark": publication.invalidation_watermark,
        "storage_key": storage_key(
            publication.payload["scope"], publication.payload["result_ref"], publication.destination, publication.schema_version
        ),
    }
    if usage is not None:
        claims.update(usage=usage, task_id=usage["task_id"], actor_id=actor.user_id, source_task_id=(run.admission or {}).get("task_id"))
    return claims


async def authorize_assertion(claims, sessions):
    actor = TokenContext.model_validate(claims["actor"])
    if actor.expires_at.replace(tzinfo=actor.expires_at.tzinfo or UTC) <= datetime.now(UTC):
        return False
    async with sessions() as db:
        try:
            run, publication = await current(
                db,
                actor,
                tenant_id=claims["tenant_id"],
                contribution_id=claims["contribution_id"],
                destination=claims["destination"],
                action=claims["action"],
                delegation_id=claims.get("delegation_id"),
                task_id=claims.get("task_id"),
            )
        except HTTPException:
            return False
        return snapshot(run, publication) == claims["snapshot"]
