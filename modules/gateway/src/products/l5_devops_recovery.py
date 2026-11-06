"""Bounded, owner-authorized SQL readback of existing DevOps recovery identity."""

import json
from copy import deepcopy
from datetime import UTC, datetime

from fastapi import HTTPException
from l5_services.models.attempt import WorkerAttempt
from l5_services.models.evidence import AssessmentResult
from l5_services.models.publication import Publication
from l5_services.models.run import view_run
from l5_services.models.work import Work
from sqlalchemy import select

from src.auth.l5_owner_delegation import _owner
from src.shared.models.vault import CredentialServiceDelegation

# The canonical max_polls is 2,880. Reconciliation and polling retain separate
# successor rows, so reserve both full phases plus ordinary lifecycle work.
ROW_LIMITS = {"attempts": 20, "work": 8192, "publications": 20}
BYTE_LIMIT = 8 * 1024 * 1024


def denied():
    return HTTPException(404, detail={"code": "not_found"})


def instant(value):
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC).isoformat() if value is not None else None


def require_actor(actor):
    if (
        actor.account_type != "human"
        or actor.auth_source != "jwt"
        or actor.token_use != "access"
        or actor.expires_at is None
        or actor.expires_at.tzinfo is None
        or actor.expires_at.astimezone(UTC) <= datetime.now(UTC)
    ):
        raise denied()


async def selection(backend, product_id, project_id, run_id):
    """Use existing Product, source, connection and binding authority; no provider session."""
    actor = backend.actor
    require_actor(actor)
    backend.db.expire_all()
    # Exclude the platform-admin visibility override: retained proof still
    # requires current membership in this Product's owning team.
    await _owner(backend, product_id, project_id, "devops")
    _, authority = await backend.binding_authority(product_id, project_id, "devops")
    run = await backend.get_run(product_id, project_id, "devops", run_id)
    binding = await (await backend.binding_store(product_id, project_id, "devops")).binding(run.binding_id)
    grant = await backend.db.get(CredentialServiceDelegation, authority["authorization"]["delegation_id"], populate_existing=True)
    if (
        (run.tenant_id, run.product_id, run.project_id, run.service_id, run.run_id) != (actor.org_id, product_id, project_id, "devops", run_id)
        or run.origin != "adp_submitted"
        or not isinstance(run.admission, dict)
        or not isinstance(run.input, dict)
        or not isinstance(run.admission.get("scope"), dict)
        or run.admission["scope"].get("owner_id") != actor.user_id
        or run.admission["scope"] != authority["scope"]
        or run.scope is not None
        and run.scope != authority["scope"]
        or run.admission.get("connection_ref") != authority["connection_ref"]
        or run.admission.get("provider_identity") != binding.provider_identity
        or run.admission.get("native_space_ref") != binding.native_resource_ref
        or run.configuration_id != authority["configuration_id"]
        or run.configuration_revision != authority["configuration_revision"]
        or (binding.tenant_id, binding.product_id, binding.project_id, binding.service_id) != (actor.org_id, product_id, project_id, "devops")
        or binding.configuration_id != run.configuration_id
        or binding.generation != run.binding_generation
        or binding.state != "ready"
        or binding.gaps
        or binding.scope != authority["scope"]
        or binding.connection_ref != authority["connection_ref"]
        or grant is None
        or grant.revoked_at is not None
        or grant.expires_at.replace(tzinfo=grant.expires_at.tzinfo or UTC) <= datetime.now(UTC)
        or grant.binding_generation != binding.generation
        or run.input.get("capability") not in (grant.capabilities or [])
    ):
        raise denied()
    return deepcopy(
        {
            "actor": (actor.org_id, actor.user_id, actor.account_type, actor.auth_source, actor.token_use, instant(actor.expires_at)),
            "authority": {**authority, "authorization": {key: value for key, value in authority["authorization"].items() if key != "authorized_at"}},
            "run": view_run(run),
            "binding": (
                binding.binding_id,
                binding.revision,
                binding.generation,
                binding.state,
                binding.scope,
                binding.connection_ref,
                binding.provider_identity,
                binding.native_resource_ref,
            ),
            "grant": (grant.revision, grant.binding_generation, grant.capabilities, grant.revoked_at, instant(grant.expires_at)),
        }
    )


async def rows(backend, tenant_id, run_id):
    attempts = (
        await backend.db.scalars(
            select(WorkerAttempt)
            .where(
                WorkerAttempt.tenant_id == tenant_id,
                WorkerAttempt.run_id == run_id,
            )
            .order_by(WorkerAttempt.attempt_id)
            .limit(ROW_LIMITS["attempts"] + 1)
        )
    ).all()
    work = (
        await backend.db.scalars(
            select(Work)
            .where(
                Work.tenant_id == tenant_id,
                Work.run_id == run_id,
            )
            .order_by(Work.work_id)
            .limit(ROW_LIMITS["work"] + 1)
        )
    ).all()
    publications = (
        await backend.db.scalars(
            select(Publication)
            .join(
                AssessmentResult,
                (Publication.tenant_id == AssessmentResult.tenant_id) & (Publication.result_id == AssessmentResult.result_id),
            )
            .where(AssessmentResult.tenant_id == tenant_id, AssessmentResult.run_id == run_id)
            .order_by(Publication.publication_id)
            .limit(ROW_LIMITS["publications"] + 1)
        )
    ).all()
    if any(
        len(items) > ROW_LIMITS[kind]
        for kind, items in (
            ("attempts", attempts),
            ("work", work),
            ("publications", publications),
        )
    ):
        raise HTTPException(
            409,
            detail={
                "code": "recovery_readback_incomplete",
                "message": "Retained history exceeds this export's row limits. Preserve the original records; "
                "a reviewed larger or paginated export is required.",
                "row_limits": ROW_LIMITS,
            },
        )
    return deepcopy(
        {
            "attempts": [
                {
                    "attempt_id": item.attempt_id,
                    "operation": item.operation,
                    "status": item.status,
                    "first_work_id": item.first_work_id,
                    "first_fence": item.first_fence,
                    "idempotency_token": item.idempotency_token,
                    "input_hash": item.input_hash,
                    "native_job_ref": item.native_job_ref,
                    "reconcile_checks": item.reconcile_checks,
                    "started_at": instant(item.started_at),
                }
                for item in attempts
            ],
            "work": [
                {
                    "work_id": item.work_id,
                    "kind": item.kind,
                    "generation": item.generation,
                    "step": item.step,
                    "fence": item.fence,
                    "attempts": item.attempts,
                    "due_at": instant(item.due_at),
                    "completed_at": instant(item.completed_at),
                    "last_enqueued_at": instant(item.last_enqueued_at),
                    "blocked_at": instant(item.blocked_at),
                    "lease_until": instant(item.lease_until),
                    "dispatch_until": instant(item.dispatch_until),
                    "redeliver_after": instant(item.redeliver_after),
                }
                for item in work
            ],
            "publications": [
                {
                    "publication_id": item.publication_id,
                    "result_id": item.result_id,
                    "result_version": item.result_version,
                    "destination": item.destination,
                    "revision": item.revision,
                    "state": item.state,
                    "schema_version": item.schema_version,
                    "attempts": item.attempts,
                    "receipt": item.receipt,
                    "invalidation_attempts": item.invalidation_attempts,
                    "invalidation_watermark": item.invalidation_watermark,
                    "applied_watermark": item.applied_watermark,
                    "invalidation_receipt": item.invalidation_receipt,
                }
                for item in publications
            ],
        }
    )


async def read_recovery_state(backend, product_id, project_id, run_id):
    selected = await selection(backend, product_id, project_id, run_id)
    tenant_id = backend.actor.org_id
    retained = await rows(backend, tenant_id, run_id)
    backend.db.expire_all()
    observed = await rows(backend, tenant_id, run_id)
    current = await selection(backend, product_id, project_id, run_id)
    if current != selected:
        raise denied()
    if observed != retained:
        raise HTTPException(409, detail={"code": "recovery_readback_changed"})
    require_actor(backend.actor)
    response = {"run": selected["run"], **retained}
    if len(json.dumps(response, sort_keys=True, separators=(",", ":")).encode()) > BYTE_LIMIT:
        raise HTTPException(
            409,
            detail={
                "code": "recovery_readback_incomplete",
                "message": "Retained history exceeds this export's byte limit. Preserve the original records; "
                "a reviewed paginated export is required.",
                "byte_limit": BYTE_LIMIT,
            },
        )
    return response
