"""Current-owner discovery and metadata for original knowledge operations."""

from fastapi import HTTPException
from l5_services.api.bindings import _intent_hash
from l5_services.devops.knowledge_sync import source_key
from l5_services.models.setup import BindingOperation, SetupIntent
from sqlalchemy import select

from src.auth.l5_owner_delegation import _owner
from src.knowledge_evidence_scope import scope
from src.orchestration.l5_observation_scope import ObservationError, require


async def operations(backend, product, project, service, offset, limit, binding_id):
    require(service == "devops", "not_found", 404)
    config, _ = await _owner(backend, product, project, service, active=False)
    query = (
        select(SetupIntent, BindingOperation)
        .join(BindingOperation, (BindingOperation.tenant_id == SetupIntent.tenant_id) & (BindingOperation.operation_id == SetupIntent.operation_id))
        .where(
            SetupIntent.tenant_id == backend.actor.org_id,
            SetupIntent.product_id == product,
            SetupIntent.project_id == project,
            SetupIntent.service_id == service,
            SetupIntent.configuration_id == config.configuration_id,
            BindingOperation.action == "sync_knowledge",
        )
        .order_by(SetupIntent.binding_generation.desc(), SetupIntent.operation_id)
        .offset(offset)
        .limit(limit + 1)
    )
    if binding_id is not None:
        query = query.where(BindingOperation.binding_id == binding_id)
    rows = list(await backend.db.execute(query))
    items = []
    for intent, operation in rows[:limit]:
        require(intent.request.get("action") == "sync_knowledge" and intent.intent_hash == _intent_hash(intent), "knowledge_evidence_changed")
        items.append(
            {
                "operation_id": operation.operation_id,
                "binding_id": operation.binding_id,
                "action": operation.action,
                "state": operation.state,
                "revision": operation.revision,
                "gaps": operation.gaps,
                "configuration_revision": intent.request["configuration_revision"],
                "binding_generation": intent.binding_generation,
            }
        )
    return {"items": items, "next_offset": offset + limit if len(rows) > limit else None}


async def metadata(backend, intent, current, rows, now):
    context, blocked = None, None
    try:
        context = await scope(backend, intent.product_id, intent.project_id, intent.service_id, intent.operation_id, now)
    except (ObservationError, HTTPException) as error:
        if isinstance(error, HTTPException) and error.status_code in {401, 404}:
            raise
        blocked = error.code if isinstance(error, ObservationError) else (error.detail.get("code") if isinstance(error.detail, dict) else None)
        blocked = blocked or "knowledge_evidence_publication_not_ready"
    admission = intent.authorization["knowledge_admission"]
    publications = []
    for publication, snapshot in zip(admission["publications"], admission["source_snapshots"], strict=True):
        key = source_key(publication["source_ref"])
        verified = context.publications.get(key) if context is not None else None
        receipt = verified["receipt"] if verified else {}
        values = publication["native_metadata"]
        publications.append(
            {
                "source_key": key,
                "source_ref": publication["source_ref"],
                "kind": publication["kind"],
                "scope": publication["scope"],
                "active": publication["active"],
                "native_type": "skill" if publication["kind"] == "skill" else "agents_md",
                "metadata": {key: value for key, value in values.items() if key in {"name", "agent_type", "agent_types", "status", "skill_type"}},
                "asset_id": receipt.get("asset_id"),
                "version": receipt.get("version"),
                "source_expires_at": snapshot["expires_at"],
            }
        )
    observed = {row.document.get("source_key") for row in rows if row.kind == "observed"}
    waiting = context is not None and current.state == "waiting"
    return {
        "revision": current.revision,
        "selection": {
            "tenant_id": intent.tenant_id,
            "product_id": intent.product_id,
            "project_id": intent.project_id,
            "configuration_id": intent.configuration_id,
            "configuration_revision": intent.request["configuration_revision"],
            "binding_id": current.binding_id,
            "binding_generation": intent.binding_generation,
        },
        "publications": publications,
        "eligibility": {
            "can_prepare": waiting and any(row["active"] for row in publications),
            "can_review": waiting and all(row["active"] and row["source_key"] in observed for row in publications),
            "blocked_code": blocked,
        },
    }
