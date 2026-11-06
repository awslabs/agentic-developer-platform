"""Human owner abandonment retains uncertain native effects and reservations."""

from datetime import UTC, datetime
from typing import Literal

from fastapi import Depends, Header, HTTPException, Request, Response
from l5_services.api.bindings import _intent_hash, view_operation
from l5_services.devops.knowledge_effects import KnowledgeEffects
from l5_services.devops.knowledge_sync import source_key
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration
from l5_services.models.knowledge_effect import KnowledgeEffect, KnowledgeOwnership
from l5_services.models.setup import BindingOperation, SetupIntent, SetupWork
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from src import knowledge_evidence_store as store
from src.auth.l5_owner_delegation import _owner
from src.orchestration.l5_observation_routes import human_session
from src.orchestration.l5_observation_scope import ObservationError, aware, digest, require


class AbandonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=1000)
    native_effects_may_remain: Literal[True]

    @field_validator("native_effects_may_remain", mode="before")
    @classmethod
    def explicit_acknowledgement(cls, value):
        if value is not True:
            raise ValueError("Explicit acknowledgement is required")
        return value

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value):
        if not value.strip():
            raise ValueError("A reason is required")
        return value


async def locked_operation(backend, product, project, service, operation_id):
    config, _ = await _owner(backend, product, project, service, active=False)
    db, tenant = backend.db, backend.actor.org_id
    intent = await db.get(SetupIntent, (tenant, operation_id), populate_existing=True)
    require(
        service == "devops"
        and intent is not None
        and (intent.product_id, intent.project_id, intent.service_id, intent.configuration_id) == (product, project, service, config.configuration_id)
        and intent.request.get("action") == "sync_knowledge"
        and intent.intent_hash == _intent_hash(intent),
        "not_found",
        404,
    )

    async def locked(model, *conditions):
        return await db.scalar(select(model).where(*conditions).with_for_update().execution_options(populate_existing=True))

    # Match worker, cancellation and evidence completion lock order. Current
    # ownership is checked again after waiting, independently of old grants.
    await locked(Configuration, Configuration.tenant_id == tenant, Configuration.configuration_id == intent.configuration_id)
    await _owner(backend, product, project, service, active=False)
    binding = await locked(Binding, Binding.tenant_id == tenant, Binding.binding_id == intent.request["binding_id"])
    works = list(
        await db.scalars(
            select(SetupWork)
            .where(SetupWork.tenant_id == tenant, SetupWork.operation_id == operation_id)
            .order_by(SetupWork.generation)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    operation = await locked(BindingOperation, BindingOperation.tenant_id == tenant, BindingOperation.operation_id == operation_id)
    require(
        binding is not None
        and (binding.configuration_id, binding.product_id, binding.project_id, binding.service_id)
        == (intent.configuration_id, product, project, service)
        and operation is not None
        and operation.action == "sync_knowledge"
        and operation.binding_id == binding.binding_id
        and len(works) == 1
        and works[0].generation == intent.binding_generation,
        "knowledge_abandon_state_changed",
    )
    return intent, binding, works[0], operation


async def retained_effects(db, intent, binding, work):
    events = list(
        await db.scalars(
            select(KnowledgeEffect)
            .where(KnowledgeEffect.tenant_id == intent.tenant_id, KnowledgeEffect.operation_id == intent.operation_id)
            .order_by(KnowledgeEffect.source_key, KnowledgeEffect.sequence)
        )
    )
    ownership = list(
        await db.scalars(
            select(KnowledgeOwnership)
            .where(KnowledgeOwnership.tenant_id == intent.tenant_id, KnowledgeOwnership.operation_id == intent.operation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    admission = intent.authorization["knowledge_admission"]
    sources = {source_key(item["source_ref"]): item["source_ref"] for item in admission["publications"]}
    snapshots = {source_key(item["source_ref"]): item for item in admission["source_snapshots"]}
    space = intent.authorization["selection"]["native_space_ref"]["record_id"]
    grouped = {}
    transitions = {None: {"planned"}, "planned": {"started"}, "started": {"outcome_unknown", "ready"}, "outcome_unknown": {"ready"}, "ready": set()}
    for item in events:
        chain = grouped.setdefault(item.source_key, [])
        previous = chain[-1] if chain else None
        require(
            item.source_key in sources
            and item.sequence == len(chain) + 1
            and item.previous_hash == (previous.event_hash if previous else None)
            and item.event_hash == digest(KnowledgeEffects._body(item))
            and item.work_id == work.work_id
            and 1 <= item.fence <= work.fence
            and item.phase in transitions.get(previous.phase if previous else None, set()),
            "knowledge_abandon_effect_changed",
        )
        if not chain:
            require(
                item.payload.get("space_id") == space
                and item.payload.get("source_key") == item.source_key
                and item.payload.get("source_ref") == sources[item.source_key]
                and item.payload.get("source_hash") == snapshots[item.source_key]["content"]["sha256"],
                "knowledge_abandon_effect_changed",
            )
        chain.append(item)
    require(
        set(grouped) == {owned.source_key for owned in ownership}
        and len(grouped) == len(ownership)
        and all(
            owned.space_id == space
            and owned.binding_id == binding.binding_id
            and owned.state == grouped[owned.source_key][-1].phase
            and (owned.state != "ready" or owned.receipt == grouped[owned.source_key][-1].payload)
            for owned in ownership
        ),
        "knowledge_abandon_effect_changed",
    )
    require(any(item.phase == "started" for item in events), "knowledge_abandon_not_started")
    return events, ownership


async def abandon(backend, human, product, project, service, operation_id, body, key, etag, *, revalidate_human):
    intent, binding, work, operation = await locked_operation(backend, product, project, service, operation_id)
    now = datetime.now(UTC)
    require(aware(human.expires_at) > now, "session_required", 401)
    evidence_id = store.identity(operation_id, human.user_id, "abandon:" + key)
    request_hash = digest({"request": body.model_dump(mode="json"), "if_match": etag})
    prior = await store.read(backend.db, intent.tenant_id, operation_id, evidence_id, "abandoned", required=False)
    if prior:
        require(prior.document["request_hash"] == request_hash, "idempotency_conflict")
        await revalidate_human()
        return prior.document["response"]
    require(etag == f'"{operation.revision}"', "stale_revision", 412)
    require(work.lease_until is None or aware(work.lease_until) <= now, "knowledge_work_already_running")
    require(operation.state in {"accepted", "running", "waiting", "blocked", "outcome_unknown", "failed"}, "knowledge_abandon_not_available")
    events, ownership = await retained_effects(backend.db, intent, binding, work)
    await _owner(backend, product, project, service, active=False)
    await revalidate_human()
    now = datetime.now(UTC)
    abandonment = {
        "abandoned_at": now.isoformat(),
        "acknowledged_by": human.user_id,
        "native_effects_may_remain": True,
        "native_cleanup_performed": False,
        "native_authority_granted": False,
        "native_call_performed": False,
        "native_ownership_released": False,
        "release_check_completed": False,
        "intent_hash": intent.intent_hash,
        "work_id": work.work_id,
        "work_generation": work.generation,
        "fence": work.fence,
        "effect_hashes": [item.event_hash for item in events],
        "prior_ownership_states": {owned.source_key: owned.state for owned in ownership},
        "evidence_id": evidence_id,
    }
    prior_state, prior_error = operation.state, work.last_error
    work.completed_at = work.completed_at or now
    work.last_error = "knowledge_publication_abandoned"
    operation.state, operation.gaps = "abandoned", ["knowledge_publication_abandoned"]
    operation.revision += 1
    operation.readback_ref = {**(operation.readback_ref or {}), "knowledge_abandonment": abandonment}
    for owned in ownership:
        # The reservation and any prior receipt survive. New publication cannot
        # take over an abandoned Asset as if an uncertain change were cleaned up.
        owned.state = "abandoned"
    response = view_operation(operation)
    await store.append(
        backend.db,
        intent.tenant_id,
        operation_id,
        evidence_id,
        "abandoned",
        human.user_id,
        {
            "request_hash": request_hash,
            "reason": body.reason,
            "original_actor_id": intent.actor_id,
            "prior_state": prior_state,
            "prior_error": prior_error,
            "response": response,
            **abandonment,
        },
        now,
    )
    await backend.db.commit()
    return response


NEXT = {
    "knowledge_work_already_running": "Wait for the current worker lease to end, then read this operation again before acknowledging abandonment.",
    "knowledge_abandon_not_started": "No native effect has started. Use this operation's cancellation action instead.",
    "knowledge_abandon_effect_changed": (
        "Retain this operation and inspect its effect history and native reservations. Resolve the inconsistent record before closing it."
    ),
    "knowledge_abandon_not_available": "This operation is already terminal. Read its retained outcome; do not restart native publication.",
    "stale_revision": "Read the latest operation and review its native effects before acknowledging abandonment with its current ETag.",
}


def add_routes(router, backend_dependency):
    @router.post("/binding-operations/{operation_id}/abandon", operation_id="abandonKnowledgeBindingOperation")
    async def abandon_operation(
        product_id: str,
        project_id: str,
        service_id: str,
        operation_id: str,
        body: AbandonRequest,
        request: Request,
        response: Response,
        idempotency_key: str = Header(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"),
        if_match: str = Header(pattern=r'^"[1-9][0-9]*"$'),
        backend=Depends(backend_dependency),
    ):
        response.headers["Cache-Control"] = "no-store"
        human = await human_session(request, backend.actor, backend.db)

        async def revalidate_human():
            current = await human_session(request, backend.actor, backend.db)
            if current != human:
                raise HTTPException(403, detail={"code": "l5_observation_human_required"})

        try:
            result = await abandon(
                backend,
                human,
                product_id,
                project_id,
                service_id,
                operation_id,
                body,
                idempotency_key,
                if_match,
                revalidate_human=revalidate_human,
            )
        except HTTPException:
            await backend.db.rollback()
            raise
        except ObservationError as error:
            await backend.db.rollback()
            raise HTTPException(
                error.status,
                detail={
                    "code": error.code,
                    "next_action": NEXT.get(error.code, "Read this original operation and resolve the changed ownership or request before retrying."),
                },
            ) from None
        response.headers["ETag"] = f'"{result["revision"]}"'
        return result
