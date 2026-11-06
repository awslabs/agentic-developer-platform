"""Prepare, retain and review genuine human use of an original publication."""

from datetime import UTC, datetime, timedelta
from hashlib import sha256

from l5_services.api.bindings import _intent_hash, view_operation
from l5_services.devops.knowledge_contract import AGENT_TYPES
from l5_services.models.setup import BindingOperation, SetupIntent
from sqlalchemy import select

from src import knowledge_evidence_store as store
from src.auth.l5_owner_delegation import _owner
from src.knowledge_evidence_contract import Observe, Prepare
from src.knowledge_evidence_inspection import metadata
from src.knowledge_evidence_scope import pin, retained_source_access, scope
from src.orchestration.l5_observation_scope import aware, digest, require
from src.orchestration.l5_observations import read as read_human_observation
from src.products.knowledge_evidence_model import KnowledgeUseEvidence


def clock():
    return datetime.now(UTC)


async def native_observation(backend, human, flow, challenge):
    receipt = await read_human_observation(backend.db, flow_id=flow, challenge_id=challenge, human=human)
    observed = receipt.get("observation_receipt") or {}
    require(
        receipt["service_id"] == "devops"
        and receipt["subject"] == "native_ui"
        and receipt["observer_id"] == human.user_id
        and observed.get("actor_id") == human.user_id
        and observed.get("observed", {}).get("observation") == "displayed"
        and observed.get("provenance") == "authenticated-human-observation",
        "knowledge_evidence_native_observation_required",
    )
    return receipt


def match_native(receipt, context):
    selected = receipt["snapshot"]["selection"]
    require(
        all(
            selected.get(key) == value
            for key, value in {
                "tenant_id": context.intent.tenant_id,
                "product_id": context.intent.product_id,
                "project_id": context.intent.project_id,
                "configuration_id": context.intent.configuration_id,
                "configuration_revision": context.intent.request["configuration_revision"],
                "binding_id": context.binding.binding_id,
                "binding_generation": context.binding.generation,
            }.items()
        ),
        "knowledge_evidence_native_scope_changed",
    )


async def prepare(backend, human, product, project, service, operation, body, key, etag, *, revalidate_human):
    # Preserve shared flow -> configuration -> binding -> work lock order.
    native = await native_observation(backend, human, body.flow_id, body.challenge_id)
    now = clock()
    context = await scope(backend, product, project, service, operation, now, lock=True)
    match_native(native, context)
    require(body.source_key in context.publications, "knowledge_evidence_source_unselected", 422)
    evidence_id = store.identity(operation, human.user_id, key)
    request_hash = digest({"request": body.model_dump(mode="json"), "if_match": etag})
    previous = await store.read(backend.db, human.tenant_id, operation, evidence_id, "prepared", required=False)
    if previous:
        require(previous.document["request_hash"] == request_hash, "idempotency_conflict")
        await revalidate_human()
        return store.view(previous)
    require(etag == f'"{context.operation.revision}"', "stale_revision", 412)
    require(context.operation.state == "waiting", "knowledge_evidence_already_completed")
    existing = list(
        await backend.db.scalars(
            select(KnowledgeUseEvidence.id)
            .where(
                KnowledgeUseEvidence.tenant_id == human.tenant_id,
                KnowledgeUseEvidence.operation_id == operation,
                KnowledgeUseEvidence.kind == "prepared",
            )
            .limit(129)
        )
    )
    require(len(existing) < 128, "knowledge_evidence_limit_reached")
    selection = pin(context, body.source_key)
    require(selection["receipt"]["metadata"].get("adp_active") is True, "knowledge_evidence_inactive_requires_non_use")
    expires = min(
        now + timedelta(minutes=30),
        aware(human.expires_at),
        datetime.fromisoformat(native["expires_at"]),
        datetime.fromisoformat(selection["snapshot"]["expires_at"]),
    )
    require(expires > now, "knowledge_evidence_expired")
    artifact = await store.put_artifact(body.model_dump(mode="json"))
    context = await scope(backend, product, project, service, operation, clock(), lock=True)
    require(pin(context, body.source_key) == selection, "knowledge_evidence_publication_changed")
    await revalidate_human()
    now = clock()
    require(expires > now, "knowledge_evidence_expired")
    row = await store.append(
        backend.db,
        human.tenant_id,
        operation,
        evidence_id,
        "prepared",
        human.user_id,
        {
            "request_hash": request_hash,
            "selection": selection,
            "artifact": artifact,
            "expires_at": expires.isoformat(),
            "native_observation": native,
            "source_grant_renewed": False,
            "native_authority_granted": False,
        },
        now,
    )
    await backend.db.commit()
    return store.view(row)


def verify_behavior(prepared, body, scenario, now):
    selection = prepared.document["selection"]
    receipt = selection["receipt"]
    created, expires = aware(prepared.created_at), datetime.fromisoformat(prepared.document["expires_at"])
    require(
        created <= body.before.observed_at <= body.session_started_at <= body.after.observed_at <= now
        and body.session_started_at > datetime.fromisoformat(selection["published_at"])
        and now < expires,
        "knowledge_evidence_session_time_invalid",
    )
    targets = receipt["metadata"].get("agent_types", [receipt["metadata"].get("agent_type")])
    require(
        body.native_agent_type in AGENT_TYPES and body.native_agent_type != "GENERIC" and ("GENERIC" in targets or body.native_agent_type in targets),
        "knowledge_evidence_agent_type_mismatch",
    )
    for readback in (body.before, body.after):
        require(
            (readback.asset_id, readback.version, readback.metadata) == (receipt["asset_id"], receipt["version"], receipt["metadata"])
            and sha256(readback.content.encode()).hexdigest() == receipt["source_hash"],
            "knowledge_evidence_asset_changed",
        )
    require(body.before.content == body.after.content and scenario.source_excerpt in body.before.content, "knowledge_evidence_source_excerpt_missing")
    turns = body.transcript
    require(turns[0].role == "user" and turns[0].text == scenario.prompt and turns[-1].role == "assistant", "knowledge_evidence_transcript_mismatch")
    require(
        all(
            scenario.expected_behavior.casefold() not in turn.text.casefold() and scenario.source_excerpt.casefold() not in turn.text.casefold()
            for turn in turns
            if turn.role == "user"
        ),
        "knowledge_evidence_answer_supplied",
    )
    replies = [turn.text for turn in turns if turn.role == "assistant"]
    require(
        any(body.response_quote in reply for reply in replies)
        and any(body.source_citation_quote in reply for reply in replies)
        and any(
            marker in body.source_citation_quote
            for marker in (receipt["asset_id"], selection["snapshot"]["source_ref"]["record_id"], receipt["metadata"].get("name"))
            if marker
        ),
        "knowledge_evidence_source_use_missing",
    )
    if selection["native_type"] == "skill":
        require(
            body.skill_invocation_quote is not None
            and any(body.skill_invocation_quote in reply for reply in replies)
            and any(marker in body.skill_invocation_quote for marker in (receipt["asset_id"], receipt["metadata"].get("name")) if marker),
            "knowledge_evidence_skill_use_missing",
        )
    else:
        require(body.skill_invocation_quote is None, "knowledge_evidence_skill_use_unselected", 422)
    # These checks bind actual artifacts and a predeclared scenario. Semantic
    # correctness still requires the explicit human review at completion.


async def observe(backend, human, product, project, service, operation, evidence_id, body, *, revalidate_human):
    prepared = await store.read(backend.db, human.tenant_id, operation, evidence_id, "prepared")
    require(prepared.actor_id == human.user_id and prepared.event_hash == body.preparation_hash, "knowledge_evidence_selected_human_required", 403)
    await scope(backend, product, project, service, operation, clock())
    scenario = Prepare.model_validate(await store.artifact(prepared.document["artifact"]))
    native = await native_observation(backend, human, scenario.flow_id, scenario.challenge_id)
    now = clock()
    context = await scope(backend, product, project, service, operation, now, lock=True)
    match_native(native, context)
    source = prepared.document["selection"]["source_key"]
    require(source in context.publications and pin(context, source) == prepared.document["selection"], "knowledge_evidence_publication_changed")
    request = body.model_dump(mode="json")
    previous = await store.read(backend.db, human.tenant_id, operation, evidence_id, "observed", required=False)
    if previous:
        require(previous.document["request_hash"] == digest(request) and previous.actor_id == human.user_id, "knowledge_evidence_already_recorded")
        await revalidate_human()
        return store.view(previous)
    require(context.operation.state == "waiting", "knowledge_evidence_already_completed")
    verify_behavior(prepared, body, scenario, now)
    # One real conversation may cover distinct prepared sources, but its
    # transcript/session identity cannot change or be replayed for the same source.
    used = list(
        await backend.db.scalars(
            select(KnowledgeUseEvidence).where(
                KnowledgeUseEvidence.tenant_id == human.tenant_id,
                KnowledgeUseEvidence.operation_id == operation,
                KnowledgeUseEvidence.kind == "observed",
            )
        )
    )
    session_hash = digest({"space": context.binding.native_resource_ref, "session_id": body.native_session_id})
    conversation_hash = digest(
        {
            "session_id": body.native_session_id,
            "agent_type": body.native_agent_type,
            "started_at": body.session_started_at.isoformat(),
            "transcript": request["transcript"],
        }
    )
    require(
        all(
            row.document.get("session_hash") != session_hash
            or row.document.get("source_key") != source
            and row.document.get("conversation_hash") == conversation_hash
            for row in used
        ),
        "knowledge_evidence_session_reused",
    )
    artifact = await store.put_artifact(request)
    context = await scope(backend, product, project, service, operation, clock(), lock=True)
    require(pin(context, source) == prepared.document["selection"], "knowledge_evidence_publication_changed")
    await revalidate_human()
    now = clock()
    verify_behavior(prepared, body, scenario, now)
    row = await store.append(
        backend.db,
        human.tenant_id,
        operation,
        evidence_id,
        "observed",
        human.user_id,
        {
            "request_hash": digest(request),
            "preparation_hash": prepared.event_hash,
            "artifact": artifact,
            "session_hash": session_hash,
            "conversation_hash": conversation_hash,
            "source_key": source,
            "provenance": "authenticated-human-native-use-observation",
            "native_observation_hash": digest(native["observation_receipt"]),
            "semantic_review_required": True,
        },
        now,
    )
    await backend.db.commit()
    return store.view(row)


async def historical_scope(backend, product, project, service, operation):
    # A current owner can inspect the original history after later operations.
    require(service == "devops", "not_found", 404)
    config, _ = await _owner(backend, product, project, service, active=False)
    tenant = backend.actor.org_id
    intent = await backend.db.get(SetupIntent, (tenant, operation), populate_existing=True)
    current = await backend.db.get(BindingOperation, (tenant, operation), populate_existing=True)
    require(
        intent is not None
        and current is not None
        and (intent.product_id, intent.project_id, intent.configuration_id, intent.service_id) == (product, project, config.configuration_id, service)
        and intent.request.get("action") == "sync_knowledge"
        and intent.intent_hash == _intent_hash(intent),
        "not_found",
        404,
    )
    return intent, current


async def inspect(backend, product, project, service, operation):
    intent, current = await historical_scope(backend, product, project, service, operation)
    tenant = intent.tenant_id
    rows = list(
        await backend.db.scalars(
            select(KnowledgeUseEvidence)
            .where(KnowledgeUseEvidence.tenant_id == tenant, KnowledgeUseEvidence.operation_id == operation)
            .order_by(KnowledgeUseEvidence.created_at, KnowledgeUseEvidence.id)
            .limit(388)
        )
    )
    require(len(rows) <= 387, "knowledge_evidence_limit_reached")
    for row in rows:
        await store.read(backend.db, row.tenant_id, operation, row.evidence_id, row.kind)
    return {
        "operation_id": operation,
        "state": current.state,
        "items": [store.view(row) for row in rows],
        **await metadata(backend, intent, current, rows, clock()),
        **store.FLAGS,
    }


async def read_artifact(backend, product, project, service, operation, evidence_id, kind):
    intent, _ = await historical_scope(backend, product, project, service, operation)
    await retained_source_access(backend, intent, clock())
    require(kind in {"prepared", "observed", "completed"}, "not_found", 404)
    row = await store.read(backend.db, intent.tenant_id, operation, evidence_id, kind)
    reference = row.document["review_artifact" if kind == "completed" else "artifact"]
    return {"evidence_id": evidence_id, "kind": kind, "event_hash": row.event_hash, "artifact": await store.artifact(reference), **store.FLAGS}


async def complete(backend, human, product, project, service, operation, body, key, etag, *, revalidate_human):
    now = clock()
    context = await scope(backend, product, project, service, operation, now, lock=True)
    evidence_id = store.identity(operation, human.user_id, "complete:" + key)
    request_hash = digest({"request": body.model_dump(mode="json"), "if_match": etag})
    prior = await store.read(backend.db, human.tenant_id, operation, evidence_id, "completed", required=False)
    if prior:
        require(prior.document["request_hash"] == request_hash, "idempotency_conflict")
        await revalidate_human()
        return prior.document["response"]
    require(etag == f'"{context.operation.revision}"', "stale_revision", 412)
    require(context.operation.state == "waiting", "knowledge_evidence_already_completed")
    covered, references = set(), []
    for review in body.reviews:
        prepared = await store.read(backend.db, human.tenant_id, operation, review.evidence_id, "prepared")
        observed = await store.read(backend.db, human.tenant_id, operation, review.evidence_id, "observed")
        source = prepared.document["selection"]["source_key"]
        require(
            source in context.publications
            and source not in covered
            and pin(context, source) == prepared.document["selection"]
            and observed.event_hash == review.observation_hash
            and observed.document["preparation_hash"] == prepared.event_hash
            and observed.actor_id == prepared.actor_id
            and observed.document["native_observation_hash"] == digest(prepared.document["native_observation"]["observation_receipt"]),
            "knowledge_evidence_publication_changed",
        )
        scenario = Prepare.model_validate(await store.artifact(prepared.document["artifact"]))
        observation = Observe.model_validate(await store.artifact(observed.document["artifact"]))
        require(scenario.source_key == source, "knowledge_evidence_changed")
        require(
            digest(observation.model_dump(mode="json")) == observed.document["request_hash"] and observation.preparation_hash == prepared.event_hash,
            "knowledge_evidence_changed",
        )
        # Validate historical collection at its retained time. Expiry of the human
        # collection window does not authenticate the reviewer, nor erase a valid fact.
        verify_behavior(prepared, observation, scenario, aware(observed.created_at))
        covered.add(source)
        references.append(
            {
                "evidence_id": review.evidence_id,
                "preparation_hash": prepared.event_hash,
                "observation_hash": observed.event_hash,
                "source_key": source,
            }
        )
    require(covered == set(context.publications), "knowledge_evidence_incomplete")
    review_artifact = await store.put_artifact(body.model_dump(mode="json"))
    # Revalidate current access after the private artifact I/O before changing
    # the operation, retaining the same transaction and lock order.
    now = clock()
    context = await scope(backend, product, project, service, operation, now, lock=True)
    await revalidate_human()
    now = clock()
    require(aware(human.expires_at) > now, "knowledge_evidence_expired")
    qualification = {
        "method": "authenticated-human-observation-and-owner-review",
        "reviewed_by": human.user_id,
        "reviewed_at": now.isoformat(),
        "evidence": references,
        "provider_attested_native_use": False,
        "release_check_completed": False,
        "native_cleanup_performed": False,
        "native_authority_granted": False,
    }
    context.operation.state, context.operation.gaps = "succeeded", []
    context.operation.revision += 1
    context.operation.readback_ref = {**context.operation.readback_ref, "native_use_qualified": True, "knowledge_qualification": qualification}
    response = view_operation(context.operation)
    await store.append(
        backend.db,
        human.tenant_id,
        operation,
        evidence_id,
        "completed",
        human.user_id,
        {"request_hash": request_hash, "review_artifact": review_artifact, "response": response},
        now,
    )
    await backend.db.commit()
    return response
