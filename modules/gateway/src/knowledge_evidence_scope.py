"""Current local read/review authority, separate from expired native write grants."""

from datetime import datetime
from types import SimpleNamespace

from l5_services.api.bindings import _intent_hash
from l5_services.devops.knowledge_effects import KnowledgeEffects
from l5_services.devops.knowledge_sync import source_key
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration
from l5_services.models.knowledge_effect import KnowledgeEffect, KnowledgeOwnership
from l5_services.models.setup import BindingOperation, SetupIntent, SetupWork
from sqlalchemy import select

from src.auth.l5_owner_delegation import _owner
from src.orchestration.l5_observation_scope import aware, digest, require
from src.products.models import ProductSourceSnapshot


async def scope(backend, product_id, project_id, service_id, operation_id, now, *, lock=False, retained=True):
    require(service_id == "devops", "not_found", 404)
    config, version = await _owner(backend, product_id, project_id, service_id)
    db, tenant = backend.db, backend.actor.org_id
    intent = await db.get(SetupIntent, (tenant, operation_id), populate_existing=True)
    require(
        intent is not None
        and (intent.product_id, intent.project_id, intent.configuration_id, intent.service_id)
        == (product_id, project_id, config.configuration_id, "devops")
        and intent.request.get("action") == "sync_knowledge"
        and intent.intent_hash == _intent_hash(intent),
        "not_found",
        404,
    )

    async def get(model, *conditions):
        query = select(model).where(*conditions).execution_options(populate_existing=True)
        return await db.scalar(query.with_for_update() if lock else query)

    if lock:
        await get(Configuration, Configuration.tenant_id == tenant, Configuration.configuration_id == intent.configuration_id)
        config, version = await _owner(backend, product_id, project_id, service_id)
    binding = await get(Binding, Binding.tenant_id == tenant, Binding.binding_id == intent.request["binding_id"])
    work = await get(
        SetupWork, SetupWork.tenant_id == tenant, SetupWork.operation_id == operation_id, SetupWork.generation == intent.binding_generation
    )
    operation = await get(BindingOperation, BindingOperation.tenant_id == tenant, BindingOperation.operation_id == operation_id)
    selected = intent.authorization["selection"]
    require(
        binding is not None
        and work is not None
        and operation is not None
        and (
            binding.configuration_id,
            binding.product_id,
            binding.project_id,
            binding.service_id,
            binding.state,
            binding.generation,
            binding.revision,
        )
        == (config.configuration_id, product_id, project_id, "devops", "ready", intent.binding_generation, intent.request["binding_revision"] + 1)
        and binding.native_resource_ref == selected["native_space_ref"]
        and binding.provider_identity == selected["provider_identity"]
        and binding.connection_ref == selected["connection_ref"]
        and binding.scope == intent.scope
        and config.current_revision == intent.request["configuration_revision"]
        and version.hash == selected["configuration_hash"]
        and operation.binding_id == binding.binding_id
        and operation.action == "sync_knowledge"
        and operation.state in {"waiting", "succeeded"}
        and work.completed_at is not None,
        "knowledge_evidence_publication_not_ready",
    )
    admission = intent.authorization["knowledge_admission"]
    if retained:
        await backend.authorize_sources(version, project_id)
        await retained_source_access(backend, intent, now)
    publications = {}
    for selected_publication in admission["publications"]:
        key = source_key(selected_publication["source_ref"])
        events = list(
            await db.scalars(
                select(KnowledgeEffect)
                .where(KnowledgeEffect.tenant_id == tenant, KnowledgeEffect.operation_id == operation_id, KnowledgeEffect.source_key == key)
                .order_by(KnowledgeEffect.sequence)
            )
        )
        previous = None
        for sequence, event in enumerate(events, 1):
            require(
                event.sequence == sequence
                and event.previous_hash == previous
                and event.event_hash == digest(KnowledgeEffects._body(event))
                and event.work_id == work.work_id,
                "knowledge_evidence_effect_changed",
            )
            previous = event.event_hash
        owned = await get(
            KnowledgeOwnership,
            KnowledgeOwnership.tenant_id == tenant,
            KnowledgeOwnership.space_id == binding.native_resource_ref["record_id"],
            KnowledgeOwnership.source_key == key,
        )
        require(
            events
            and events[-1].phase == "ready"
            and owned is not None
            and owned.state == "ready"
            and owned.operation_id == operation_id
            and owned.binding_id == binding.binding_id
            and owned.receipt == events[-1].payload,
            "knowledge_evidence_effect_changed",
        )
        snapshot = next(item for item in admission["source_snapshots"] if item["source_ref"] == selected_publication["source_ref"])
        publications[key] = {
            "publication": selected_publication,
            "snapshot": snapshot,
            "effect": events[-1].event_hash,
            "published_at": aware(events[-1].created_at).isoformat(),
            "receipt": events[-1].payload,
            "native_type": events[0].payload["native_type"],
        }
    return SimpleNamespace(intent=intent, binding=binding, work=work, operation=operation, publications=publications)


def pin(context, source):
    return {
        "operation_id": context.intent.operation_id,
        "intent_hash": context.intent.intent_hash,
        "binding_id": context.binding.binding_id,
        "binding_generation": context.binding.generation,
        "configuration_id": context.intent.configuration_id,
        "configuration_revision": context.intent.request["configuration_revision"],
        "native_space_ref": context.binding.native_resource_ref,
        "source_key": source,
        **context.publications[source],
    }


async def retained_source_access(backend, intent, now):
    # Current human read authority does not reuse or renew the service grant.
    rights = await backend.source_rights(intent.project_id)
    for snapshot in intent.authorization["knowledge_admission"]["source_snapshots"]:
        source = snapshot["source_ref"]
        require((source["source_connection_id"], source["record_id"]) in rights, "knowledge_evidence_source_access_revoked", 403)
        row = await backend.db.get(ProductSourceSnapshot, (intent.tenant_id, snapshot["snapshot_id"]), populate_existing=True)
        require(
            row is not None
            and (row.product_id, row.project_id) == (intent.product_id, intent.project_id)
            and row.delegation_id == intent.authorization["delegation_id"]
            and row.delegation_revision == intent.authorization["delegation_revision"]
            and row.source_ref == snapshot["source_ref"]
            and aware(row.expires_at) == datetime.fromisoformat(snapshot["expires_at"])
            and row.receipt == snapshot
            and aware(row.expires_at) > now
            and aware(row.retain_until) > now,
            "knowledge_evidence_source_expired",
        )
