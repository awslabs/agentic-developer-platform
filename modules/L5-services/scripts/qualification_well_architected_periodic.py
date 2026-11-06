"""Collect actual periodic intervals and verify original WA06/AC-02 facts.

Selectors identify retained checkpoints, never supply counts or verdicts. A zero
claim is limited to the selected ADP occurrence under the installed instrumented
release. CloudTrail absence is never presented as accountwide native-zero proof.
"""

from datetime import datetime, timedelta
from hashlib import sha256
import re

import rfc8785

from l5_services.adapters.well_architected.qualification import ProductReader
from l5_services.events.cron import next_tick

CASES = {"enabled", "disabled", "duplicate", "revoked", "budget"}
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,36}\Z")


def digest(value):
    return sha256(rfc8785.dumps(value)).hexdigest()


def instant(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("periodic evidence requires aware timestamps")
    return result


def snapshots(document, case):
    if (not isinstance(document, dict) or document.get("contract") != "wa-periodic-interval/v1"
            or document.get("execution_authorized") is not False or document.get("native_qualified") is not False):
        raise ValueError("periodic interval is not a protected observation")
    values = []
    for field in ("before", "after"):
        value = document[field]
        snapshot = value["snapshot"]
        scope = snapshot["scope"]
        if (value["snapshot_sha256"] != digest(snapshot)
                or snapshot["contract"] != "wa-periodic-checkpoint/v1"
                or snapshot["source"] != "authenticated-periodic-readback"
                or snapshot["execution_authorized"] is not False
                or any(scope.get(key) != case["scope"][key] for key in ("tenant_id", "product_id", "project_id"))
                or scope["binding_id"] != case["binding_id"]
                or scope["provider_identity"] != case["provider_identity"]
                or snapshot["settings"]["source_refs"] != case["sources"]):
            raise ValueError("periodic checkpoint differs from selected owner/source/native identity")
        values.append(snapshot)
    first, last = values
    if (first["scope"] != last["scope"] or first["settings"] != last["settings"] or first["selection"] != last["selection"]
            or not instant(first["observed_at"]) < instant(last["read_started_at"])
            or (instant(last["observed_at"]) - instant(first["observed_at"])).total_seconds() > 86400):
        raise ValueError("periodic interval crosses revisions or is not bounded")
    for field, identity in (("event_history", "observation_id"), ("transport", "observation_id")):
        original = {item[identity]: item for item in first["runtime"][field]}
        current = {item[identity]: item for item in last["runtime"][field]}
        if len(current) != len(last["runtime"][field]) or any(current.get(key) != value for key, value in original.items()):
            raise ValueError("append-only periodic history changed")
    for snapshot in values:
        native = snapshot["native"]
        if (native.get("source") != "independent-native-read" or native.get("contexts") != []
                or native.get("aggregation_configuration") != []
                or native.get("profile_arn") != snapshot["scope"]["native_resource_ref"]["record_id"]):
            raise ValueError("periodic Profile readback is not source-only")
    return first, last


def evaluate_interval(kind, document, case, *, instrumented_release):
    first, last = snapshots(document, case)
    before, after = first["runtime"], last["runtime"]
    trigger_id = first["selection"]["trigger_id"]
    trigger = next(item for item in first["settings"]["triggers"] if item["trigger_id"] == trigger_id)
    original_history = {item["observation_id"] for item in before["event_history"]}
    history = [item for item in after["event_history"] if item["observation_id"] not in original_history]
    original_sends = {item["observation_id"] for item in before["transport"]}
    transport = [item for item in after["transport"] if item["observation_id"] not in original_sends]
    start, end = instant(first["observed_at"]), instant(last["read_started_at"])
    reasons = []
    if not instrumented_release:
        reasons.append("installed_instrumented_release_not_verified")
    if any(instant(item["observed_at"]) < start or instant(item["observed_at"]) > end for item in history + transport):
        reasons.append("observation_crosses_checkpoint_boundary")
    if before["runs"] or before["work"] or before["transport"] or before["events"]:
        reasons.append("fresh_interval_baseline_required")
    audit = document["native_audit"]
    if (audit.get("source") != "independent-cloudtrail-read" or audit.get("pagination_complete") is not True
            or audit.get("start") != first["observed_at"] or audit.get("end") != last["read_started_at"]):
        reasons.append(audit.get("code", "independent_native_audit_unavailable"))
    events = after["events"]
    if any(event["configuration_revision"] != first["scope"]["configuration_revision"] for event in events):
        raise ValueError("periodic event belongs to another configuration revision")
    send_before = [item for item in transport if item["phase"] == "before_send"]
    send_after = [item for item in transport if item["phase"] == "after_call"]
    native_count = None
    if kind in {"enabled", "duplicate"}:
        if (len(events) != 1 or events[0]["state"] != "admitted" or not events[0]["run_id"]
                or len(after["runs"]) != 1 or after["runs"][0]["run_id"] != events[0]["run_id"]
                or not any(item["kind"] == "received" for item in history)
                or not any(item["state"] == "admitted" for item in history)
                or len(send_before) != 1 or len(send_after) != 1
                or len(transport) != 2 or send_before[0]["send_id"] != send_after[0]["send_id"]):
            reasons.append("single_admitted_start_not_observed")
        else:
            sent, returned = send_before[0], send_after[0]
            facts, proof = returned["facts"], audit.get("events", [])
            job = after["runs"][0].get("native_job_ref") or {}
            if (sent["run_id"] != events[0]["run_id"] or returned["run_id"] != sent["run_id"]
                    or facts.get("contract") != "wa-start-transport/v1" or sent["facts"].get("contract") != facts["contract"]
                    or facts.get("request_hash") != sent["facts"].get("request_hash")
                    or facts.get("http_status") not in {200, 201, 202}
                    or facts.get("generation_id") != job.get("generation_id") or not facts.get("generation_id")
                    or facts.get("response_profile_arn") != first["scope"]["native_resource_ref"]["record_id"]
                    or facts.get("response_name") != facts.get("name")
                    or len(proof) != 1 or proof[0].get("request_id") != facts.get("provider_request_id")
                    or not facts.get("provider_request_id") or proof[0].get("error_code") is not None
                    or proof[0].get("name") != facts.get("name")
                    or not any(item["id"] == job.get("generation_id") for item in last["native"]["generations"])
                    or not any(item["id"] == job.get("generation_id") and item["name"] == facts.get("name")
                               and item["profile_arn"] == facts.get("response_profile_arn")
                               for item in last["native"].get("generation_details", []))
                    or any(item["id"] == job.get("generation_id") for item in first["native"]["generations"])):
                reasons.append("independent_start_correlation_incomplete")
            else:
                native_count = 1
        if kind == "duplicate" and not any(item["kind"] == "duplicate" for item in history):
            reasons.append("duplicate_arrival_not_observed")
    else:
        if after["runs"] or after["work"] or transport or any(event["run_id"] for event in events):
            reasons.append("denied_occurrence_has_work_or_transport")
        if audit.get("events"):
            reasons.append("selected_native_audit_has_start")
        if kind == "disabled":
            cursor = after["cursor"]["triggers"].get(trigger_id, {})
            if (first["settings"].get("enabled") is True and trigger.get("enabled") is True
                    or cursor.get("reason") != "trigger_disabled" or cursor.get("next_due_at") is not None
                    or next_tick(trigger["schedule"], start + timedelta(microseconds=1)) > end or events or history
                    or instant(before["cursor"]["initialized_at"]) > start):
                reasons.append("disabled_due_interval_not_observed")
        else:
            expected = {"delegation_denied", "delegation_scope_changed"} if kind == "revoked" else {"budget_exhausted"}
            if (len(events) != 1 or events[0]["state"] != "rejected" or events[0]["reason"] not in expected
                    or not any(item["kind"] == "received" for item in history)
                    or not any(item["state"] == "rejected" and item["reason"] in expected for item in history)):
                reasons.append("selected_admission_denial_not_observed")
        if not reasons:
            native_count = 0  # No Run/outbox or mandatory pre-send record in this ADP occurrence.
    return {"case": kind, "outcome": "incomplete" if reasons else "passed", "blocker_codes": sorted(set(reasons)),
            "adp_start_count": native_count, "transport_before_send_count": len(send_before),
            "independent_audit_observed_count": len(audit["events"]) if "events" in audit else None,
            "claim_scope": "selected_adp_occurrence_only", "accountwide_native_zero_proved": False,
            "limitations": ["cloudtrail_delivery_is_delayed", "no_accountwide_absence_claim"]}


def collect(case, connection):
    selectors = case.get("qualification_periodic")
    if not isinstance(selectors, list) or len(selectors) != 5 or {item.get("case") for item in selectors} != CASES:
        raise ValueError("select all five original periodic cases using retained checkpoint IDs")
    collected = []
    with connection(case) as client:
        reader = ProductReader(client, case["scope"])
        for selector in selectors:
            if (set(selector) != {"case", "before_id", "after_id"}
                    or any(not isinstance(selector[key], str) or not IDENTIFIER.fullmatch(selector[key]) for key in ("before_id", "after_id"))):
                raise ValueError("periodic selectors cannot contain fixture-authored facts or counts")
            response = client.post(reader.path + "/qualification-experiments/periodic-intervals/read",
                                   json={"before_id": selector["before_id"], "after_id": selector["after_id"], "read_native_audit": True})
            response.raise_for_status()
            document = response.json()
            snapshots(document, case)
            if any(document[field]["checkpoint_id"] != selector[field + "_id"] for field in ("before", "after")):
                raise ValueError("periodic readback returned another selected checkpoint")
            collected.append({"case": selector["case"], "document": document})
    return {"source": "authenticated-periodic-readback", "selection": selectors, "intervals": collected}


def evaluate(collected, case, context, installed):
    if (not isinstance(collected, dict) or collected.get("source") != "authenticated-periodic-readback"
            or {item.get("case") for item in collected.get("intervals", [])} != CASES or len(collected["intervals"]) != 5):
        raise ValueError("complete collected periodic evidence required")
    instrumented = (isinstance(installed, dict) and installed.get("source") == "independent-installed-read"
                    and installed.get("source_revision") == context["source_revision"]
                    and len(installed.get("snapshot_sha256", [])) == 3)
    coverage = installed.get("observation_interval", {}) if isinstance(installed, dict) else {}
    rows = []
    for item in collected["intervals"]:
        first, last = snapshots(item["document"], case)
        covered = (instrumented and bool(coverage.get("start")) and bool(coverage.get("end"))
                   and instant(coverage["start"]) <= instant(first["read_started_at"])
                   and instant(last["observed_at"]) <= instant(coverage["end"]))
        rows.append(evaluate_interval(item["case"], item["document"], case, instrumented_release=covered))
    return {"outcome": "passed" if all(item["outcome"] == "passed" for item in rows) else "incomplete", "cases": rows}
