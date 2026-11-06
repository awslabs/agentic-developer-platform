"""Admit a durable source receipt only through current Product work authority."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json

from fastapi import HTTPException
from sqlalchemy import select

from l5_services.admission import submit_and_dispatch
from l5_services.events.schedules import finops_admissions_enabled, previous_closed_month_query
from l5_services.models.configuration import Configuration
from l5_services.models.event import EventReceipt, semantic_key
from l5_services.models.receipt import RunReceipt
from l5_services.models.run import Run
from l5_services.models.preparation import AdmissionPreparation
from l5_services.events.cron import matches


def _verify_finops_schedule(receipt, verified):
    selected = receipt.target or {}
    trigger = verified.get("trigger") or {}
    tick = datetime.fromisoformat(receipt.event_id.removeprefix("schedule:").replace("Z", "+00:00")).astimezone(UTC)
    frozen_fields = ("trigger_id", "kind", "enabled", "capability", "binding_id",
                     "schedule", "period_template", "template_revision", "query_template",
                     "question", "source_refs")
    if (receipt.event_kind != "schedule" or selected.get("kind") != "schedule"
            or selected.get("enabled") is not True
            or any(trigger.get(field) != selected.get(field) for field in frozen_fields)
            or selected.get("binding_generation") != verified.get("binding_generation")
            or selected.get("configuration_revision") != receipt.configuration_revision
            or selected.get("scheduled_at") != receipt.event_id.removeprefix("schedule:")
            or tick != datetime(tick.year, tick.month, 1, tzinfo=UTC)
            or not isinstance(trigger.get("query_template"), dict)
            or receipt.run_input.get("query") != previous_closed_month_query(trigger["query_template"], tick)
            or selected.get("query") != receipt.run_input.get("query")
            or selected.get("question") != receipt.run_input.get("question")
            or (selected.get("source_refs") or []) != receipt.run_input.get("sources", [])
            or verified.get("configuration_enabled") is not True
            or verified.get("binding_state") != "ready"
            or verified.get("delegation_active") is not True
            or verified.get("native_qualified") is not True):
        raise HTTPException(409, detail={"code": "binding_or_delegation_unavailable"})


def _verify_transform_schedule(receipt, verified):
    selected = receipt.target or {}
    trigger = verified.get("trigger") or {}
    pinned = (receipt.run_input or {}).get("sources") or []
    frozen_fields = ("trigger_id", "kind", "enabled", "capability", "binding_id",
                     "schedule", "goal", "source_policy", "branch_selector", "source_refs")
    if (receipt.event_kind != "schedule" or selected.get("kind") != "schedule"
            or selected.get("enabled") is not True
            or any(trigger.get(field) != selected.get(field) for field in frozen_fields)
            or selected.get("configuration_revision") != receipt.configuration_revision
            or receipt.event_id != (f"schedule:{selected.get('scheduled_at')}:revision:"
                                    f"{receipt.configuration_revision}")
            or selected.get("binding_generation") != verified.get("binding_generation")
            or len(pinned) != 1 or selected.get("pinned_source") != pinned[0]
            or selected.get("source_policy", "pinned") not in {"pinned", "branch"}
            or pinned[0] not in (verified.get("source_refs") or [])
            or (selected.get("source_policy", "pinned") == "pinned"
                and selected.get("source_refs") != pinned)
            or (selected.get("source_policy") == "branch"
                and (not selected.get("branch_selector")
                     or any(selected["branch_selector"].get(field) != pinned[0].get(field)
                            for field in ("tenant_id", "project_id", "source_connection_id", "record_id"))))
            or receipt.run_input.get("transform_input", {}).get("goal") != selected.get("goal")
            or verified.get("configuration_enabled") is not True
            or verified.get("binding_state") != "ready"
            or verified.get("delegation_active") is not True):
        raise HTTPException(409, detail={"code": "binding_or_delegation_unavailable"})


def _verify_well_architected_schedule(receipt, verified):
    selected = receipt.target or {}
    trigger = verified.get("trigger") or {}
    sources = (receipt.run_input or {}).get("sources")
    if (receipt.event_kind != "schedule" or selected.get("kind") != "schedule"
            or selected.get("enabled") is not True
            or any(trigger.get(field) != selected.get(field) for field in (
                "trigger_id", "kind", "enabled", "capability", "binding_id", "schedule", "source_refs"))
            or selected.get("configuration_revision") != receipt.configuration_revision
            or receipt.event_id != (f"schedule:{selected.get('scheduled_at')}:revision:"
                                    f"{receipt.configuration_revision}")
            or selected.get("binding_generation") != verified.get("binding_generation")
            or sources != selected.get("source_refs")
            or sources != verified.get("source_refs")
            or verified.get("configuration_enabled") is not True
            or verified.get("binding_state") != "ready"
            or verified.get("delegation_active") is not True
            or verified.get("connection_ref") != verified.get("binding_connection_ref")):
        raise HTTPException(409, detail={"code": "binding_or_delegation_unavailable"})


def _verify_security_schedule(receipt, verified):
    """A scheduled Security assessment keeps the trigger plus UTC instant identity."""
    selected = receipt.target or {}
    trigger = verified.get("trigger") or {}
    sources = (receipt.run_input or {}).get("sources")
    if (receipt.event_kind != "schedule" or selected.get("kind") != "schedule"
            or selected.get("enabled") is not True
            or any(trigger.get(field) != selected.get(field) for field in (
                "trigger_id", "kind", "enabled", "capability", "binding_id", "schedule", "source_refs"))
            or selected.get("capability") not in {"threat_model", "code_review"}
            or selected.get("configuration_revision") != receipt.configuration_revision
            or receipt.event_id != f"schedule:{selected.get('scheduled_at')}"
            or selected.get("binding_generation") != verified.get("binding_generation")
            or sources != selected.get("source_refs")
            or verified.get("causal_origin") != "schedule"
            or verified.get("configuration_enabled") is not True
            or verified.get("binding_state") != "ready"
            or verified.get("delegation_active") is not True
            or verified.get("connection_ref") != verified.get("binding_connection_ref")
            or verified.get("source_registration") is None
            or (selected["capability"] == "threat_model" and not verified.get("native_model_ref"))
            or not isinstance(verified.get("consent_expires_at"), str)):
        raise HTTPException(409, detail={"code": "binding_or_delegation_unavailable"})


def _verify_receipt_scope(receipt, verified):
    scope = verified.get("scope") or {}
    trigger = verified.get("trigger") or {}
    selected = receipt.target or {}
    if (tuple(scope.get(field) for field in ("tenant_id", "product_id", "project_id"))
            != (receipt.tenant_id, receipt.product_id, receipt.project_id)
            or verified.get("service_id") != receipt.service_id or verified.get("actor_type") != "service"
            or verified.get("configuration_id") != receipt.configuration_id
            or verified.get("configuration_revision") != receipt.configuration_revision
            or verified.get("event_id") != receipt.event_id
            or any(trigger.get(field) != selected.get(field) for field in (
                "trigger_id", "kind", "enabled", "capability", "binding_id",
                *(("source_refs",) if receipt.service_id in {"security", "finops", "transform", "well_architected"}
                  else ("resource_refs", "telemetry_refs")),
            ))):
        raise HTTPException(404, detail={"code": "not_found"})


async def _timely_security_preparation(session, receipt, scheduled_at, now):
    """A timely frozen preparation may finish within its original deadline.

    This relaxes only the occurrence-arrival window, never current authorization,
    configuration checks or the immutable assessment deadline.
    """
    if receipt.service_id != "security" or (receipt.run_input or {}).get("capability") != "code_review":
        return False
    rows = (await session.scalars(select(AdmissionPreparation).where(
        AdmissionPreparation.tenant_id == receipt.tenant_id,
        AdmissionPreparation.product_id == receipt.product_id,
        AdmissionPreparation.project_id == receipt.project_id,
        AdmissionPreparation.service_id == "security",
        AdmissionPreparation.operation == "submit",
        AdmissionPreparation.idempotency_key == semantic_key(receipt),
    ).limit(2))).all()
    if len(rows) != 1:
        return False
    preparation = rows[0]
    frozen = preparation.verified
    duration = (frozen.get("limits") or {}).get("deadline_seconds")
    if type(duration) is not int or not 60 <= duration <= 86400:
        return False
    deadline = preparation.deadline_at.replace(tzinfo=preparation.deadline_at.tzinfo or UTC)
    started = deadline - timedelta(seconds=duration)
    digest = sha256(json.dumps({"input": receipt.run_input, "event_id": receipt.event_id,
                               "exclusions": frozen.get("exclusions", [])}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return (scheduled_at <= started <= scheduled_at + timedelta(minutes=5) and deadline > now
            and preparation.request_hash == digest and frozen.get("actor_type") == "service"
            and frozen.get("actor_id") == preparation.actor_id
            and frozen.get("event_id") == receipt.event_id
            and frozen.get("configuration_id") == receipt.configuration_id
            and frozen.get("configuration_revision") == receipt.configuration_revision
            and frozen.get("trigger", {}).get("trigger_id") == receipt.trigger_id)


async def admit_source_receipt(session, sessions, receipt, verify_target, publish, *, now=None,
                               budget_guard=None):
    receipt = await session.get(EventReceipt, (
        receipt.tenant_id, receipt.product_id, receipt.project_id, receipt.service_id,
        receipt.trigger_id, receipt.event_id,
    ), with_for_update=True)
    if receipt is None:
        raise HTTPException(404, detail={"code": "not_found"})
    if receipt.state == "rejected":
        raise HTTPException(409, detail={"code": receipt.reason})
    if receipt.state not in {"pending", "admitted"} or receipt.run_input is None:
        raise HTTPException(409, detail={"code": "event_unavailable"})
    if receipt.event_kind == "schedule":
        scheduled_at = datetime.fromisoformat(
            (receipt.target["scheduled_at"] if receipt.service_id in {"transform", "well_architected"} and receipt.target
             else receipt.event_id.removeprefix("schedule:")).replace("Z", "+00:00"))
        if (now or datetime.now(UTC)) < scheduled_at:
            raise HTTPException(409, detail={"code": "schedule_not_due"})
        if receipt.service_id in {"well_architected", "transform", "security"}:
            try:
                due = matches(receipt.target.get("schedule"), scheduled_at)
            except ValueError:
                due = False
            if not due:
                raise HTTPException(409, detail={"code": "schedule_mismatch"})
            if (receipt.state == "pending" and (now or datetime.now(UTC)) > scheduled_at + timedelta(minutes=5)
                    and not await _timely_security_preparation(session, receipt, scheduled_at, now or datetime.now(UTC))):
                raise HTTPException(409, detail={"code": "missed_tick"})

    key = semantic_key(receipt)
    selected_authority = None
    if receipt.service_id in {"finops", "transform", "well_architected", "security"} and receipt.event_kind == "schedule":
        configuration = await session.get(Configuration, (receipt.tenant_id, receipt.configuration_id))
        if configuration is None or configuration.current_revision != receipt.configuration_revision:
            raise HTTPException(409, detail={"code": "stale_revision"})
        selected_authority = await verify_target(receipt)
        _verify_receipt_scope(receipt, selected_authority[0])
        if receipt.service_id == "finops":
            _verify_finops_schedule(receipt, selected_authority[0])
        elif receipt.service_id == "transform":
            _verify_transform_schedule(receipt, selected_authority[0])
        elif receipt.service_id == "security":
            _verify_security_schedule(receipt, selected_authority[0])
        else:
            _verify_well_architected_schedule(receipt, selected_authority[0])
    stored = await session.scalar(select(RunReceipt).where(
        RunReceipt.tenant_id == receipt.tenant_id, RunReceipt.product_id == receipt.product_id,
        RunReceipt.project_id == receipt.project_id, RunReceipt.service_id == receipt.service_id,
        RunReceipt.operation == "submit", RunReceipt.idempotency_key == key,
    ))
    if stored is not None:
        run = await session.get(Run, (receipt.tenant_id, stored.run_id))
        if (run is None or run.input != receipt.run_input
                or run.admission.get("event_id") != receipt.event_id
                or (selected_authority is not None and any(
                    run.admission.get(field) != selected_authority[0].get(field) for field in (
                        "actor_id", "delegation_id", "delegation_version", "binding_generation",
                        "connection_ref")))):
            raise HTTPException(409, detail={"code": "idempotency_conflict"})
        if receipt.run_id != run.run_id or receipt.state != "admitted":
            receipt.run_id, receipt.state, receipt.reason = run.run_id, "admitted", None
            await session.commit()
        return run

    if receipt.service_id == "finops" and not finops_admissions_enabled():
        raise HTTPException(409, detail={"code": "finops_admissions_disabled"})

    if selected_authority is None:
        configuration = await session.get(Configuration, (receipt.tenant_id, receipt.configuration_id))
        if configuration is None or configuration.current_revision != receipt.configuration_revision:
            raise HTTPException(409, detail={"code": "stale_revision"})
        selected_authority = await verify_target(receipt)
    verified, source_client = selected_authority
    if receipt.service_id not in {"finops", "transform", "well_architected", "security"} or receipt.event_kind != "schedule":
        _verify_receipt_scope(receipt, verified)
    verified = deepcopy(verified)
    if receipt.service_id == "security":
        if (receipt.event_kind == "pull_request" and
                receipt.source_ref not in receipt.run_input["sources"]):
            raise HTTPException(404, detail={"code": "not_found"})
        verified["trigger"]["source_refs"] = receipt.run_input["sources"]
        verified["event_kind"] = receipt.event_kind
        verified["event_source_ref"] = deepcopy(receipt.source_ref)
    if receipt.service_id == "transform" and receipt.event_kind == "schedule":
        verified["trigger"]["source_refs"] = deepcopy(receipt.run_input["sources"])
    budget_kwargs = {"budget_guard": budget_guard} if receipt.service_id in {"devops", "finops", "transform", "well_architected"} else {}
    run = await submit_and_dispatch(session, sessions, source_client, publish, receipt.run_input,
                                    verified, key, now=now, **budget_kwargs)
    receipt.run_id, receipt.state, receipt.reason = run.run_id, "admitted", None
    await session.commit()
    return run
