"""Read explicitly selected installed observation receipts without executing work."""

from datetime import UTC, datetime, timedelta
from hashlib import sha256
import re
from urllib.parse import quote

import rfc8785

from l5_services.adapters.well_architected.qualification import ProductReader
from qualification_well_architected_reuse import human, timestamp


LIMITATIONS = (
    "fault_history_not_recorded", "provider_send_count_not_observed",
    "native_state_not_read", "source_object_presence_not_read",
    "historical_transitions_not_proved",
)


def digest(value):
    return sha256(rfc8785.dumps(value)).hexdigest()


def selections(case):
    selected = case.get("qualification_experiments")
    if not isinstance(selected, list) or not 1 <= len(selected) <= 10:
        raise ValueError("select one to ten existing qualification observation receipts")
    ids = set()
    for item in selected:
        if (not isinstance(item, dict) or set(item) != {"experiment_id", "revision", "selection_digest"}
                or not isinstance(item["experiment_id"], str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,36}", item["experiment_id"])
                or type(item["revision"]) is not int or item["revision"] < 1
                or not isinstance(item["selection_digest"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", item["selection_digest"])
                or item["experiment_id"] in ids):
            raise ValueError("qualification observation receipts need unique exact identities, revisions and digests")
        ids.add(item["experiment_id"])
    return selected


def receipt(value, selected, case, actor, run):
    choice = value.get("selection") or {}
    scope, targets = choice.get("scope") or {}, choice.get("targets") or {}
    now = datetime.now(UTC)
    if (any(value.get(key) != selected[key] for key in ("experiment_id", "revision", "selection_digest"))
            or value.get("mode") != "observations" or choice.get("mode") != "observations"
            or value.get("execution_authorized") is not False or value.get("active") is not True
            or value.get("revoked_at") is not None or timestamp(value.get("expires_at", "")) <= now
            or timestamp(value.get("created_at", "")) > now
            or digest(choice) != selected["selection_digest"]
            or any(scope.get(key) != case["scope"][key] for key in ("tenant_id", "product_id", "project_id"))
            or scope.get("created_by") != actor["user_id"]
            or scope.get("binding_id") != case["binding_id"]
            or scope.get("configuration_revision") != case["configuration_revision"]
            or scope.get("provider_identity") != case["provider_identity"]
            or not isinstance(scope.get("sources"), list) or scope["sources"][1:] != case["sources"]
            or targets.get("binding_id") != case["binding_id"] or targets.get("run_id") != case["run_id"]
            or choice.get("run_input_digest") != digest(run.get("input"))
            or choice.get("run_admission_digest") != digest(run.get("admission"))):
        raise ValueError("qualification receipt is expired, changed or outside the selected owner/run/source scope")
    events = targets.get("events")
    if (not isinstance(events, list) or len(events) > 10
            or any(not isinstance(event, dict) or set(event) != {"trigger_id", "event_id"}
                   or any(not isinstance(value, str) or not 1 <= len(value) <= 255 for value in event.values()) for event in events)
            or len({(event["trigger_id"], event["event_id"]) for event in events}) != len(events)):
        raise ValueError("qualification receipt has an invalid event selection")
    return value


def observation(value, selected, retained, case, run, started):
    current = value.get("run") or {}
    events, holds = value.get("events"), value.get("holds")
    expected_events = retained["selection"]["targets"]["events"]
    if (value.get("schema_version") != 1 or value.get("source") != "durable-runtime"
            or value.get("execution_authorized") is not False
            or value.get("experiment_id") != selected["experiment_id"]
            or value.get("selection_digest") != selected["selection_digest"]
            or not started - timedelta(seconds=30) <= timestamp(value.get("observed_at", "")) <= datetime.now(UTC) + timedelta(seconds=30)
            or current.get("run_id") != case["run_id"] or current.get("revision") != case["run_revision"]
            or any(current.get(key) != run.get(key) for key in ("state", "provider_state", "native_job_ref"))
            or timestamp(current.get("deadline_at", "")) != timestamp(run.get("deadline_at", ""))
            or not isinstance(events, list) or len(events) != len(expected_events)
            or not isinstance(holds, list) or len(holds) > 200
            or not set(LIMITATIONS) <= set(value.get("limitations", []))):
        raise ValueError("qualification observation does not cover the exact current selected receipt and run")
    if any(not isinstance(event, dict) for event in events) or [
            {key: event.get(key) for key in ("trigger_id", "event_id")} for event in events] != expected_events:
        raise ValueError("qualification observation returned unselected event receipts")
    for event in events:
        if (event.get("event_kind") != "schedule" or event.get("configuration_revision") != case["configuration_revision"]
                or not isinstance(event.get("state"), str) or not event["state"]
                or type(event.get("recovery_attempts")) is not int or event["recovery_attempts"] < 0):
            raise ValueError("qualification event receipt is incompatible with the selected configuration")
        timestamp(event.get("received_at", ""))
    artifacts = set()
    for hold in holds:
        if not isinstance(hold, dict) or not isinstance(hold.get("artifact"), dict) or not hold["artifact"]:
            raise ValueError("qualification source hold is not a retained artifact reference")
        identity = digest(hold["artifact"])
        if identity in artifacts:
            raise ValueError("qualification observation repeated an artifact hold")
        artifacts.add(identity)
        for key in ("required_until", "policy_expires_at"):
            timestamp(hold.get(key, ""))
        for key in ("revoked_at", "released_at"):
            if hold.get(key) is not None:
                timestamp(hold[key])
    return value


def collect(case, connection):
    """GET retained selections and their snapshots; never create an experiment.

    Current holds and admission decisions are evidence of those rows only. Their
    readback does not establish source-object presence, fault history, duplicate
    delivery, provider call counts, or a transition that was never observed.
    """
    selected = selections(case)
    started = datetime.now(UTC)
    with connection(case) as client:
        actor = human(client)
        if actor["org_id"] != case["scope"]["tenant_id"]:
            raise ValueError("qualification reader belongs to another tenant")
        product = ProductReader(client, case["scope"])
        run_path = "runs/" + quote(case["run_id"], safe="")
        run = product.get(run_path)
        if (run.get("run_id") != case["run_id"] or run.get("revision") != case["run_revision"]
                or any(run.get("scope", {}).get(key) != case["scope"][key] for key in ("tenant_id", "product_id", "project_id"))):
            raise ValueError("qualification run identity or revision changed")
        collected = []
        for item in selected:
            path = "qualification-experiments/" + quote(item["experiment_id"], safe="")
            retained = receipt(product.get(path), item, case, actor, run)
            observed = observation(product.get(path + "/observations"), item, retained, case, run, started)
            if receipt(product.get(path), item, case, actor, run) != retained:
                raise ValueError("qualification selection changed during collection")
            collected.append({"receipt": retained, "observation": observed,
                              "receipt_sha256": digest(retained), "observation_sha256": digest(observed)})
        if human(client) != actor or product.get(run_path) != run:
            raise ValueError("qualification owner or run changed during collection")
    return {"source": "authenticated-product-api", "scope": case["scope"], "run_id": case["run_id"],
            "run_revision": case["run_revision"], "reader": actor, "items": collected,
            "limitations": list(LIMITATIONS), "execution_authorized": False}
