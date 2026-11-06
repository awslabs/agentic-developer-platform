"""Bounded durable WA readback after the existing native owner authorization.

These are current database observations, not a fabricated history of injected
faults. In particular a successful attempt does not prove a lost Start response.
"""

from copy import deepcopy

from fastapi import HTTPException
from l5_services.api.bindings import _digest
from l5_services.models.attempt import WorkerAttempt
from l5_services.models.binding import Binding
from l5_services.models.event import EventReceipt
from l5_services.models.generation_collection import GenerationCollection
from l5_services.models.profile_claim import ProfileClaim
from l5_services.models.work import Work
from sqlalchemy import select


async def read_runtime(session, run, context):
    """Project only the already-authorized run and its exact source Profile."""
    binding = await session.get(Binding, (run.tenant_id, run.binding_id), populate_existing=True)
    if binding is None or binding.scope != run.admission["scope"]:
        raise HTTPException(409, detail={"code": "native_observation_scope_changed"})
    claim_key = _digest((binding.provider_identity["account_id"], binding.provider_identity["region"], binding.native_resource_ref["record_id"]))
    claim = await session.get(ProfileClaim, ("well_architected", claim_key), populate_existing=True)
    owned = claim is not None and all(getattr(claim, field) == context["scope"][field] for field in ("tenant_id", "product_id", "project_id"))
    attempts = (
        await session.scalars(
            select(WorkerAttempt)
            .where(
                WorkerAttempt.tenant_id == run.tenant_id,
                WorkerAttempt.run_id == run.run_id,
            )
            .order_by(WorkerAttempt.attempt_id)
            .limit(21)
            .execution_options(populate_existing=True)
        )
    ).all()
    work = (
        await session.scalars(
            select(Work)
            .where(
                Work.tenant_id == run.tenant_id,
                Work.run_id == run.run_id,
            )
            .order_by(Work.work_id)
            .limit(201)
            .execution_options(populate_existing=True)
        )
    ).all()
    events = (
        await session.scalars(
            select(EventReceipt)
            .where(
                EventReceipt.tenant_id == run.tenant_id,
                EventReceipt.product_id == run.product_id,
                EventReceipt.project_id == run.project_id,
                EventReceipt.service_id == "well_architected",
                EventReceipt.run_id == run.run_id,
            )
            .order_by(EventReceipt.event_id)
            .limit(101)
            .execution_options(populate_existing=True)
        )
    ).all()
    collection = await session.get(GenerationCollection, (run.tenant_id, run.run_id), populate_existing=True)
    if len(attempts) > 20 or len(work) > 200 or len(events) > 100:
        raise HTTPException(409, detail={"code": "runtime_observation_incomplete"})
    return deepcopy(
        {
            "schema_version": 1,
            "source": "durable-runtime",
            "scope": context["scope"],
            "run_id": run.run_id,
            "run_revision": run.revision,
            "profile_claim_matches": owned,
            "attempts": [
                {
                    field: getattr(item, field)
                    for field in ("attempt_id", "operation", "status", "first_work_id", "first_fence", "native_job_ref", "reconcile_checks")
                }
                for item in attempts
            ],
            "work": [{field: getattr(item, field) for field in ("work_id", "kind", "generation", "step", "fence", "attempts")} for item in work],
            "events": [
                {
                    field: getattr(item, field)
                    for field in ("trigger_id", "event_id", "event_kind", "state", "reason", "run_id", "configuration_revision")
                }
                for item in events
            ],
            "collection": None
            if collection is None
            else {
                "native_job_ref": collection.native_job_ref,
                "page_count": collection.page_count,
                "complete": collection.next_token is None and not collection.pending_recommendations,
                "result_id": collection.result_id,
            },
            "limitations": ["fault_history_not_recorded", "provider_start_call_count_not_observed"],
        }
    )
