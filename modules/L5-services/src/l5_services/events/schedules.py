"""Freeze scheduled assessment targets at their configured UTC tick."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import os

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.binding import Binding
from l5_services.models.event import EventReceipt, semantic_key
from l5_services.run_input import validate_run_input
from l5_services.events.cron import matches as cron_matches
from l5_services.events.observations import observe


SCHEDULE_CAPABILITIES = {"security": {"threat_model", "code_review"},
                         "finops": {"cost_inquiry"},
                         "transform": {"refactor", "dependency_upgrade"},
                         "well_architected": {"architecture_review"}}


def finops_admissions_enabled():
    return os.environ.get("L5_FINOPS_ADMISSION_ENABLED") == "true"


def wa_configured(version):
    return version.state in {"ready", "needs_input"} and not (set(version.gaps or []) - {
        "provider_unqualified", "catalog_mapping_unresolved", "execution_delegation",
    })


def security_configured(version):
    """Normal Security saves persist needs_input even after owner consent, so a
    stored ready state is never required to freeze a structural receipt. Current
    configuration, binding, native and consent rights are separate admission
    checks resolved by the Security schedule authority.
    """
    return version.state in {"ready", "needs_input"}


def previous_closed_month_query(template, tick):
    report_end = tick.date().replace(day=1)
    report_start = (report_end - timedelta(days=1)).replace(day=1)
    return {**deepcopy(template), "start": report_start.isoformat(), "end": report_end.isoformat()}


def schedule_window_reason(trigger, tick, now):
    try:
        if not cron_matches(trigger.get("schedule"), tick):
            return "schedule_mismatch"
    except ValueError:
        return "schedule_invalid"
    if now > tick + timedelta(minutes=5):
        return "missed_tick"
    return None


async def receive_schedule_tick(session, *, tenant_id, product_id, project_id, trigger_id,
                                scheduled_at, now=None, service_id="security", resolve_branch=None, configuration_revision=None,
                                defer_transform_branch=False):
    if (not all(isinstance(value, str) and 0 < len(value) <= 255 for value in (
            tenant_id, product_id, project_id, trigger_id))
            or service_id not in SCHEDULE_CAPABILITIES
            or not isinstance(scheduled_at, datetime) or scheduled_at.tzinfo is None
            or scheduled_at.utcoffset() is None):
        raise HTTPException(422, detail={"code": "validation_error"})
    tick = scheduled_at.astimezone(UTC)
    now = now or datetime.now(UTC)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise HTTPException(422, detail={"code": "validation_error"})
    instant = tick.isoformat().replace("+00:00", "Z")
    event_id = f"schedule:{instant}"
    configuration = None
    version = None
    if service_id in {"transform", "well_architected"}:
        configuration = await session.scalar(select(Configuration).where(
            Configuration.tenant_id == tenant_id, Configuration.product_id == product_id,
            Configuration.project_id == project_id, Configuration.service_id == service_id,
        ).execution_options(populate_existing=True))
        if configuration is None:
            raise HTTPException(404, detail={"code": "not_found"})
        version = await session.get(ConfigurationVersion, (
            tenant_id, configuration.configuration_id, configuration.current_revision,
        ))
        if version is None:
            raise HTTPException(409, detail={"code": "stale_revision"})
        if configuration_revision is not None and version.revision != configuration_revision:
            raise HTTPException(409, detail={"code": "stale_revision"})
        event_id = f"schedule:{instant}:revision:{version.revision}"
    key = (tenant_id, product_id, project_id, service_id, trigger_id, event_id)
    original = await session.get(EventReceipt, key, populate_existing=True)
    if original is not None:
        observe(session, original, "duplicate", now)
        await session.commit()
        return original

    if configuration is None:
        configuration = await session.scalar(select(Configuration).where(
            Configuration.tenant_id == tenant_id, Configuration.product_id == product_id,
            Configuration.project_id == project_id, Configuration.service_id == service_id,
        ))
        if configuration is None:
            raise HTTPException(404, detail={"code": "not_found"})
        version = await session.get(ConfigurationVersion, (
            tenant_id, configuration.configuration_id, configuration.current_revision,
        ))
        if version is None:
            raise HTTPException(409, detail={"code": "stale_revision"})
    matches = [item for item in version.settings.get("triggers", []) if item.get("trigger_id") == trigger_id]
    trigger = matches[0] if len(matches) == 1 else None
    reason = None
    target = None
    run_input = None
    if service_id == "finops" and not finops_admissions_enabled():
        reason = "finops_admissions_disabled"
    elif ((not wa_configured(version) if service_id == "well_architected"
           else not security_configured(version) if service_id == "security"
           else version.state != "ready") or version.settings.get("enabled") is not True
            or trigger is None or trigger.get("enabled") is not True):
        reason = "trigger_disabled"
    elif trigger.get("kind") != "schedule":
        reason = "trigger_mismatch"
    elif service_id in {"transform", "security"} and (window_reason := schedule_window_reason(trigger, tick, now)):
        reason = window_reason
    elif service_id == "finops":
        template = trigger.get("query_template")
        received_at = now or datetime.now(UTC)
        if (not isinstance(received_at, datetime) or received_at.tzinfo is None
                or received_at.utcoffset() is None):
            raise HTTPException(422, detail={"code": "validation_error"})
        if (tick.day, tick.hour, tick.minute, tick.second, tick.microsecond) != (1, 0, 0, 0, 0):
            reason = "schedule_mismatch"
        elif received_at.astimezone(UTC) > tick + timedelta(days=1):
            reason = "missed_tick"
        elif (trigger.get("schedule") != "0 0 1 * *"
              or trigger.get("period_template") != "previous_closed_month"
              or type(trigger.get("template_revision")) is not int or trigger["template_revision"] < 1
              or not isinstance(template, dict) or set(template) != {
                  "metric", "granularity", "group_by", "currency"}
              or not isinstance(trigger.get("question"), str)
              or not isinstance(trigger.get("binding_id"), str)
              or not trigger["binding_id"]):
            reason = "template_invalid"
        else:
            binding = await session.get(Binding, (tenant_id, trigger["binding_id"]))
            if (binding is None or binding.service_id != service_id or binding.state != "ready"
                    or binding.configuration_id != configuration.configuration_id
                    or (binding.product_id, binding.project_id) != (product_id, project_id)
                    or ("binding_generation" in trigger
                        and trigger["binding_generation"] != binding.generation)):
                reason = "binding_unavailable"
            else:
                query = previous_closed_month_query(template, tick)
                run_input = {"service_id": "finops", "capability": "cost_inquiry",
                             "configuration_revision": version.revision,
                             "binding_id": trigger["binding_id"], "question": trigger["question"],
                             "query": query, "sources": deepcopy(trigger.get("source_refs") or [])}
                try:
                    validate_run_input(run_input, today=tick.date())
                except HTTPException:
                    run_input = None
                    reason = "template_invalid"
                else:
                    target = {**deepcopy(trigger), "binding_generation": binding.generation,
                              "configuration_revision": version.revision, "scheduled_at": instant,
                              "query": deepcopy(query)}
    elif service_id == "well_architected":
        sources = trigger.get("source_refs")
        try:
            due = cron_matches(trigger.get("schedule"), tick)
        except ValueError:
            reason = "schedule_invalid"
            due = False
        if not due:
            reason = reason or "schedule_mismatch"
        elif now > tick + timedelta(minutes=5):
            reason = "missed_tick"
        elif (trigger.get("capability") != "architecture_review"
                or not isinstance(trigger.get("schedule"), str) or not trigger["schedule"]
                or not isinstance(trigger.get("binding_id"), str) or not trigger["binding_id"]
                or not isinstance(sources, list) or not sources
                or sources != version.settings.get("source_refs")):
            reason = "source_scope_conflict"
        else:
            binding = await session.get(Binding, (tenant_id, trigger["binding_id"]))
            if (binding is None or binding.service_id != service_id or binding.state != "ready"
                    or binding.configuration_id != configuration.configuration_id
                    or (binding.product_id, binding.project_id) != (product_id, project_id)
                    or ("binding_generation" in trigger
                        and trigger["binding_generation"] != binding.generation)):
                reason = "binding_unavailable"
            else:
                run_input = {"service_id": service_id, "configuration_revision": version.revision,
                             "binding_id": binding.binding_id, "capability": "architecture_review",
                             "sources": deepcopy(sources)}
                try:
                    validate_run_input(run_input)
                except HTTPException:
                    run_input = None
                    reason = "source_scope_conflict"
                else:
                    target = {**deepcopy(trigger), "binding_generation": binding.generation,
                              "configuration_revision": version.revision, "scheduled_at": instant}
    elif service_id == "transform":
        selected = trigger.get("source_refs")
        goal = trigger.get("goal")
        source_policy = trigger.get("source_policy", "pinned")
        branch_selector = trigger.get("branch_selector")
        if (trigger.get("capability") not in SCHEDULE_CAPABILITIES[service_id]
                or not isinstance(selected, list) or len(selected) != 1
                or selected[0] not in version.settings.get("source_refs", [])
                or not isinstance(selected[0], dict)
                or selected[0].get("record_type") != "repository_commit"
                or not isinstance(source_policy, str) or source_policy not in {"pinned", "branch"}
                or (source_policy == "pinned" and branch_selector is not None)
                or (source_policy == "branch" and (
                    not isinstance(branch_selector, dict)
                    or set(branch_selector) != {"tenant_id", "project_id", "source_connection_id",
                                                 "record_type", "record_id", "record_revision"}
                    or branch_selector["record_type"] != "repository_branch"
                    or not isinstance(branch_selector["record_revision"], str)
                    or not branch_selector["record_revision"].strip()
                    or branch_selector["record_revision"].lower() in {"head", "latest", "null"}
                    or any(branch_selector.get(field) != selected[0][field] for field in (
                        "tenant_id", "project_id", "source_connection_id", "record_id"))))
                or not isinstance(goal, dict) or goal.get("kind") != trigger.get("capability")
                or not isinstance(trigger.get("schedule"), str) or not trigger["schedule"]
                or not isinstance(trigger.get("binding_id"), str) or not trigger["binding_id"]):
            reason = "source_scope_conflict"
        else:
            binding = await session.get(Binding, (tenant_id, trigger["binding_id"]))
            if (binding is None or binding.service_id != service_id or binding.state != "ready"
                    or binding.configuration_id != configuration.configuration_id
                    or (binding.product_id, binding.project_id) != (product_id, project_id)):
                reason = "binding_unavailable"
            else:
                pinned = deepcopy(selected[0])
                if source_policy == "branch":
                    if defer_transform_branch:
                        pinned = None
                        target = {**deepcopy(trigger), "binding_generation": binding.generation,
                                  "configuration_revision": version.revision, "scheduled_at": instant}
                    elif resolve_branch is None:
                        reason = "source_resolution_unavailable"
                    else:
                        pinned = await resolve_branch(
                            tenant_id=tenant_id, product_id=product_id, project_id=project_id,
                            branch_selector=deepcopy(branch_selector), scheduled_at=tick,
                            configuration_revision=version.revision)
                        if (not isinstance(pinned, dict)
                                or set(pinned) != set(selected[0])
                                or any(pinned.get(field) != selected[0][field] for field in (
                                    "tenant_id", "project_id", "source_connection_id", "record_type", "record_id"))):
                            reason = "source_scope_conflict"
                if reason is None and pinned is not None:
                    run_input = {"service_id": service_id, "configuration_revision": version.revision,
                                 "binding_id": binding.binding_id, "capability": trigger["capability"],
                                 "sources": [deepcopy(pinned)], "transform_input": {"goal": deepcopy(goal)}}
                    try:
                        validate_run_input(run_input)
                    except HTTPException:
                        run_input = None
                        reason = "source_scope_conflict"
                    else:
                        target = {**deepcopy(trigger), "binding_generation": binding.generation,
                                  "configuration_revision": version.revision, "scheduled_at": instant,
                                  "pinned_source": deepcopy(pinned)}
    else:
        # Security keeps the plain trigger plus scheduled UTC instant identity;
        # it never copies the revision-suffixed WA/Transform event identity.
        sources = trigger.get("source_refs") or []
        if (trigger.get("capability") not in SCHEDULE_CAPABILITIES[service_id]
                or not isinstance(trigger.get("schedule"), str) or not trigger["schedule"]
                or not isinstance(trigger.get("binding_id"), str) or not trigger["binding_id"]
                or not isinstance(sources, list) or not sources
                or any(source not in version.settings.get("source_refs", []) for source in sources)):
            reason = "source_scope_conflict"
        else:
            binding = await session.get(Binding, (tenant_id, trigger["binding_id"]))
            if (binding is None or binding.service_id != service_id or binding.state != "ready"
                    or binding.configuration_id != configuration.configuration_id
                    or (binding.product_id, binding.project_id) != (product_id, project_id)
                    or ("binding_generation" in trigger
                        and trigger["binding_generation"] != binding.generation)):
                reason = "binding_unavailable"
            else:
                run_input = {"configuration_revision": version.revision, "binding_id": binding.binding_id,
                             "capability": trigger["capability"], "sources": deepcopy(sources)}
                try:
                    validate_run_input(run_input)
                except HTTPException:
                    run_input = None
                    reason = "source_scope_conflict"
                else:
                    target = {**deepcopy(trigger), "binding_generation": binding.generation,
                              "configuration_revision": version.revision, "scheduled_at": instant}

    receipt = EventReceipt(
        tenant_id=tenant_id, product_id=product_id, project_id=project_id, service_id=service_id,
        trigger_id=trigger_id, event_id=event_id, event_kind="schedule", source_ref=None,
        configuration_id=configuration.configuration_id, configuration_revision=version.revision,
        target=target, run_input=run_input, state="rejected" if reason else "pending", reason=reason,
        received_at=now or datetime.now(UTC), retry_at=tick,
    )
    receipt.idempotency_key = semantic_key(receipt)
    session.add(receipt)
    observe(session, receipt, "received", now)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        original = await session.get(EventReceipt, key)
        if original is None:
            raise
        observe(session, original, "duplicate", now)
        await session.commit()
        return original
    return receipt
