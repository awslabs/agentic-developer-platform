"""Freeze a source event's configured target at first semantic receipt."""

from copy import deepcopy
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.event import EventReceipt, semantic_key
from l5_services.run_input import validate_run_input


async def receive_source_event(session, *, tenant_id, product_id, project_id, trigger_id,
                               event_id, source_ref, event_kind, source_cursor=None,
                               causal_origin=None, now=None):
    capabilities = {"pull_request": ("repository_commit", "code_review"),
                    "document_revision": ("document", "threat_model")}
    if event_kind not in capabilities:
        raise HTTPException(422, detail={"code": "validation_error"})
    if source_cursor is not None and (not isinstance(source_cursor, str) or not 1 <= len(source_cursor) <= 2048):
        raise HTTPException(422, detail={"code": "validation_error"})
    if causal_origin is not None and (
        not isinstance(causal_origin, dict) or set(causal_origin) != {"source", "service_id", "run_id"}
        or causal_origin.get("source") != "context"
        or any(not isinstance(causal_origin.get(field), str) or not causal_origin[field]
               or len(causal_origin[field]) > 255 for field in ("service_id", "run_id"))
    ):
        raise HTTPException(422, detail={"code": "validation_error"})
    record_type, capability = capabilities[event_kind]
    if (not all(isinstance(value, str) and 0 < len(value) <= 255 for value in (
            tenant_id, product_id, project_id, trigger_id, event_id))
            or not isinstance(source_ref, dict)
            or set(source_ref) != {"tenant_id", "project_id", "source_connection_id",
                                   "record_type", "record_id", "record_revision"}
            or source_ref.get("tenant_id") != tenant_id or source_ref.get("project_id") != project_id
            or source_ref.get("record_type") != record_type):
        raise HTTPException(422, detail={"code": "validation_error"})
    validate_run_input({"configuration_revision": 1, "binding_id": "validation", "capability": capability,
                        "sources": [source_ref]})
    key = (tenant_id, product_id, project_id, "security", trigger_id, event_id)
    original = await session.get(EventReceipt, key)
    if original is not None:
        if original.source_ref != source_ref:
            raise HTTPException(409, detail={"code": "idempotency_conflict"})
        return original

    configuration = await session.scalar(select(Configuration).where(
        Configuration.tenant_id == tenant_id, Configuration.product_id == product_id,
        Configuration.project_id == project_id, Configuration.service_id == "security",
    ))
    if configuration is None:
        raise HTTPException(404, detail={"code": "not_found"})
    version = await session.get(ConfigurationVersion, (
        tenant_id, configuration.configuration_id, configuration.current_revision,
    ))
    triggers = [item for item in version.settings.get("triggers", []) if item.get("trigger_id") == trigger_id]
    trigger = triggers[0] if len(triggers) == 1 else None
    reason = None
    target = None
    run_input = None
    if (version.state != "ready" or version.settings.get("enabled") is not True
            or trigger is None or trigger.get("enabled") is not True):
        reason = "trigger_disabled"
    elif causal_origin is not None and causal_origin["service_id"] == "security":
        reason = "self_caused"
    elif trigger.get("kind") != event_kind or trigger.get("capability") != capability:
        reason = "trigger_mismatch"
    else:
        selected = trigger.get("source_refs") or []
        matches = [index for index, source in enumerate(selected) if (
            isinstance(source, dict) and all(source.get(field) == source_ref[field] for field in (
                "tenant_id", "project_id", "source_connection_id", "record_type", "record_id"
            ))
        )]
        if (len(matches) != 1 or trigger.get("binding_id") is None
                or ("source_refs" in version.settings and any(
                    source not in version.settings["source_refs"] for source in selected
                ))):
            reason = "source_scope_conflict"
        else:
            sources = deepcopy(selected)
            sources[matches[0]] = deepcopy(source_ref)
            run_input = {"configuration_revision": version.revision, "binding_id": trigger["binding_id"],
                         "capability": trigger["capability"], "sources": sources}
            validate_run_input(run_input)
            target = deepcopy(trigger)

    receipt = EventReceipt(
        tenant_id=tenant_id, product_id=product_id, project_id=project_id, service_id="security",
        trigger_id=trigger_id, event_id=event_id, event_kind=event_kind, source_ref=deepcopy(source_ref),
        source_cursor=source_cursor, causal_origin=deepcopy(causal_origin),
        configuration_id=configuration.configuration_id, configuration_revision=version.revision,
        target=target, run_input=run_input, state="rejected" if reason else "pending", reason=reason,
        received_at=now or datetime.now(UTC),
    )
    receipt.idempotency_key = semantic_key(receipt)
    session.add(receipt)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        original = await session.get(EventReceipt, key)
        if original is None:
            raise
        if original.source_ref != source_ref:
            raise HTTPException(409, detail={"code": "idempotency_conflict"}) from None
        return original
    return receipt


async def receive_pr_event(session, *, tenant_id, product_id, project_id, trigger_id,
                           event_id, source_ref, now=None):
    return await receive_source_event(session, tenant_id=tenant_id, product_id=product_id,
                                      project_id=project_id, trigger_id=trigger_id, event_id=event_id,
                                      source_ref=source_ref, event_kind="pull_request", now=now)


async def receive_document_event(session, *, tenant_id, product_id, project_id, trigger_id,
                                 event_id, source_ref, source_cursor=None, causal_origin=None, now=None):
    return await receive_source_event(session, tenant_id=tenant_id, product_id=product_id,
                                      project_id=project_id, trigger_id=trigger_id, event_id=event_id,
                                      source_ref=source_ref, event_kind="document_revision",
                                      source_cursor=source_cursor, causal_origin=causal_origin, now=now)
