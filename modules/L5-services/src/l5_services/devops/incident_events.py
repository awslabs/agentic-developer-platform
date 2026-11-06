"""Authenticate incident sources before freezing a project-scoped event receipt."""

import hmac
import json
import re
from datetime import UTC, datetime
from hashlib import sha256

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.devops.trigger_policy import require_new_work
from l5_services.models.event import EventReceipt, semantic_key
from l5_services.run_input import validate_run_input


async def receive_incident_event(session, *, source_id, payload, signature, load_secret, now=None):
    """Select the authenticated source binding, never scope from event-supplied IDs."""
    if (not isinstance(source_id, str) or not 1 <= len(source_id) <= 255
            or not isinstance(payload, bytes) or len(payload) > 16384 or not payload
            or not isinstance(signature, str)):
        raise HTTPException(422, detail={"code": "validation_error"})
    if re.fullmatch(r"sha256=[0-9a-f]{64}", signature) is None:
        raise HTTPException(401, detail={"code": "invalid_source_signature"})
    candidates = (await session.execute(select(Configuration, ConfigurationVersion).join(
        ConfigurationVersion,
        (Configuration.tenant_id == ConfigurationVersion.tenant_id)
        & (Configuration.configuration_id == ConfigurationVersion.configuration_id)
        & (Configuration.current_revision == ConfigurationVersion.revision),
    ).where(Configuration.service_id == "devops"))).all()
    verified = []
    for configuration, version in candidates:
        for source in (version.settings.get("devops") or {}).get("incident_sources", []):
            if source.get("source_id") != source_id or not source.get("secret_ref"):
                continue
            secret = await load_secret(source["secret_ref"])
            if not isinstance(secret, (str, bytes)) or not secret:
                raise HTTPException(503, detail={"code": "source_secret_unavailable"})
            key = secret.encode() if isinstance(secret, str) else secret
            expected = "sha256=" + hmac.new(key, payload, sha256).hexdigest()
            if hmac.compare_digest(expected, signature):
                verified.append((configuration, version, source))
    if len(verified) != 1:
        raise HTTPException(401, detail={"code": "invalid_source_signature"})
    configuration, version, source = verified[0]
    try:
        event = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(422, detail={"code": "validation_error"}) from None
    if (not isinstance(event, dict) or set(event) != {"event_id", "environment_ref", "question", "severity"}
            or not isinstance(event["event_id"], str) or not 1 <= len(event["event_id"]) <= 255
            or not isinstance(event["severity"], str) or not 1 <= len(event["severity"]) <= 32
            or event["environment_ref"] != source.get("environment_ref")):
        raise HTTPException(422, detail={"code": "validation_error"})
    trigger_id = source.get("trigger_id")
    receipt_key = (configuration.tenant_id, configuration.product_id, configuration.project_id,
                   "devops", trigger_id, event["event_id"])
    digest = sha256(payload).hexdigest()
    original = await session.get(EventReceipt, receipt_key)
    if original is not None:
        if original.source_cursor != digest or original.source_ref != source.get("source_ref"):
            raise HTTPException(409, detail={"code": "idempotency_conflict"})
        if original.state == "admitted":
            return original
    triggers = [item for item in (version.settings.get("devops") or {}).get("trigger_policies", [])
                if item.get("trigger_id") == trigger_id]
    trigger = triggers[0] if len(triggers) == 1 else None
    if (version.state != "ready" or version.settings.get("enabled") is not True
            or trigger is None or trigger.get("enabled") is not True
            or trigger.get("kind") != "native_event" or trigger.get("capability") != "investigation"):
        raise HTTPException(409, detail={"code": "trigger_disabled"})
    resource_refs = trigger.get("resource_refs")
    telemetry_refs = trigger.get("telemetry_refs")
    run_input = {"service_id": "devops", "configuration_revision": version.revision,
                 "binding_id": trigger["binding_id"], "capability": "investigation",
                 "devops_input": {"kind": "investigation", "question": event["question"],
                                  "resource_refs": resource_refs, "telemetry_refs": telemetry_refs}}
    validate_run_input(run_input)
    if (not isinstance(source.get("source_ref"), dict)
            or any(reference.get("tenant_id") != configuration.tenant_id
                   or reference.get("project_id") != configuration.project_id
                   for reference in [source["source_ref"], source["environment_ref"],
                                     *resource_refs, *telemetry_refs])):
        raise HTTPException(404, detail={"code": "not_found"})
    if original is not None:
        if original.source_cursor != digest or original.run_input != run_input:
            raise HTTPException(409, detail={"code": "idempotency_conflict"})
        return original
    require_new_work(version.settings, trigger, event=event, now=now)
    receipt = EventReceipt(
        tenant_id=configuration.tenant_id, product_id=configuration.product_id,
        project_id=configuration.project_id, service_id="devops", trigger_id=trigger_id,
        event_id=event["event_id"], event_kind="native_event", source_ref=source["source_ref"],
        source_cursor=digest, causal_origin={"severity": event["severity"],
                                             "environment_ref": event["environment_ref"]},
        configuration_id=configuration.configuration_id,
        configuration_revision=version.revision, target=trigger, run_input=run_input,
        state="pending", reason=None, received_at=now or datetime.now(UTC),
    )
    receipt.idempotency_key = semantic_key(receipt)
    session.add(receipt)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        original = await session.get(EventReceipt, receipt_key)
        if original is None or original.source_cursor != digest or original.run_input != run_input:
            raise HTTPException(409, detail={"code": "idempotency_conflict"}) from None
        return original
    return receipt
