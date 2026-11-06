"""Fail-closed readiness for selected DevOps provider integrations."""

from l5_services.devops.release_prerequisites import INDEXING_UNAVAILABLE


def qualify_source_readiness(selection: dict, evidence: dict, native: dict,
                             *, enabled_native_actions=()) -> dict:
    kind = selection.get("kind")
    if (kind not in {"github", "cloudwatch"} or evidence.get("kind") != kind
            or evidence.get("connection_ref") != selection.get("connection_ref")):
        raise ValueError("unsupported or mismatched DevOps integration")

    reference_field = "repository_refs" if kind == "github" else "resource_refs"
    refs = selection.get(reference_field) or []
    if kind == "github":
        refs = [*refs, *(selection.get("workflow_refs") or [])]
    entries = ([*(evidence.get("repositories") or []), *(evidence.get("workflows") or [])]
               if kind == "github" else evidence.get("resources") or [])
    checked_revisions = {entry["source_ref"]["record_id"]: entry["source_ref"]["record_revision"]
                         for entry in entries if isinstance(entry, dict) and isinstance(entry.get("source_ref"), dict)
                         and entry["source_ref"].get("record_id") and entry["source_ref"].get("record_revision")}
    registration = native.get("registration") or {}
    association = native.get("association") or {}
    indexing = native.get("indexing") or {}
    automation = native.get("automation") or {}
    if not all(isinstance(value, dict) for value in (registration, association, indexing, automation)):
        raise ValueError("malformed native readiness observation")

    stages = {
        "registration": "ready" if registration.get("status") == "active" and registration.get("id")
        and registration.get("connection_ref") == selection.get("connection_ref") else "verifying",
        "association": "verifying",
        "indexing": "not_required" if kind == "cloudwatch" else "verifying",
        "content": "verifying",
        "automation": "verifying",
    }
    if stages["registration"] == "ready" and (association.get("status") == "active"
            and association.get("id") and association.get("registration_id") == registration["id"]
            and association.get("source_refs") == refs):
        stages["association"] = "ready"
    if (selection.get(reference_field) and len(refs) == len(entries) and len(checked_revisions) == len(refs)
            and all(entry.get("source_ref") in refs for entry in entries)
            and all(entry.get("content_readable") is True for entry in evidence.get("repositories") or [])
            and all(entry.get("deployment_readable") is True for entry in evidence.get("workflows") or [])
            and all(entry.get("events_readable") is True for entry in evidence.get("resources") or [])):
        stages["content"] = "ready"
    if kind == "github" and (indexing.get("status") == "indexed"
                             and set(indexing.get("revisions") or []) >= {
                                 entry.get("content_revision") for entry in evidence.get("repositories") or []
                             } and all(entry.get("content_revision") for entry in evidence.get("repositories") or [])):
        stages["indexing"] = "ready"
    elif kind == "github" and indexing == INDEXING_UNAVAILABLE:
        stages["indexing"] = "unavailable"
    native_actions = ("automatic_review", "automatic_testing")
    if all(automation.get(action) in {"ENABLED", "DISABLED"} for action in native_actions):
        stages["automation"] = ("blocked" if any(automation[action] == "ENABLED"
                                and action not in enabled_native_actions for action in native_actions) else "ready")

    mode = native.get("connection_mode")
    event_observed = native.get("event_support") is True
    if kind == "github" and mode == "pat":
        event = {"status": "unsupported", "reason": "pat_events_unavailable"}
    elif (kind == "github" and mode == "app" or kind == "cloudwatch" and mode == "aws") and event_observed:
        event = {"status": "supported"}
    elif mode in {"app", "aws"} and native.get("event_support") is False:
        event = {"status": "unsupported", "reason": "provider_events_unavailable"}
    else:
        event = {"status": "verifying", "reason": "event_support_unverified"}
    if selection.get("event_support") is True and event["status"] != "supported":
        event["requested_mode_unavailable"] = True

    if stages["automation"] == "blocked":
        status, reason = "blocked", "unrequested_native_automation_enabled"
    elif any(stages[stage] != "ready" for stage in ("registration", "association", "content", "automation")):
        status, reason = "verifying", "provider_readback_incomplete"
    elif stages["indexing"] == "verifying":
        status, reason = "verifying", "source_indexing_unverified"
    else:
        status, reason = "ready", None
    return {"version": 1, "kind": kind, "status": status, "reason": reason,
            "stages": stages, "events": event, "checked_revisions": checked_revisions,
            **({"indexing": dict(INDEXING_UNAVAILABLE)} if stages["indexing"] == "unavailable" else {}),
            "registration_id": registration.get("id"), "association_id": association.get("id")}
