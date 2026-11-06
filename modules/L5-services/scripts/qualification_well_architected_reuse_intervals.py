"""Collect exact post-cleanup replay/use intervals; no fixture-supplied counts.

Only the selected ADP configuration's no-new-Start claim can be established.
Delayed CloudTrail absence is corroboration, never accountwide native-zero proof.
Publication fault/lost-ack history remains a separate original WA05/AC-02 gap.
"""

import re
from copy import deepcopy

from l5_services.adapters.well_architected.qualification import ProductReader
from qualification_well_architected_periodic import IDENTIFIER, digest, instant


def snapshots(document, case):
    if (not isinstance(document, dict) or document.get("contract") != "wa-reuse-interval/v1"
            or document.get("execution_authorized") is not False or document.get("native_qualified") is not False):
        raise ValueError("reuse interval is not a protected partial observation")
    values = []
    for name in ("before", "after"):
        wrapped = document[name]
        value = wrapped["snapshot"]
        scope, selected = value["scope"], value["selection"]
        result = case["result"]
        if (wrapped.get("snapshot_sha256") != digest(value)
                or wrapped.get("execution_authorized") is not False or wrapped.get("native_qualified") is not False
                or value.get("contract") != "wa-reuse-checkpoint/v1" or value.get("source") != "authenticated-reuse-readback"
                or value.get("execution_authorized") is not False
                or any(scope.get(key) != result["scope"][key] for key in ("tenant_id", "product_id", "project_id"))
                or scope.get("result_scope") != result["scope"] or scope.get("result_id") != result["result_id"]
                or scope.get("result_revision") != case["result_revision"] or scope.get("result_digest") != digest(result)
                or scope.get("run_id") != case["run_id"] or scope.get("binding_id") != case["binding_id"]
                or scope.get("native_job_ref") != case["native_job_ref"]
                or scope.get("source_refs") != case["sources"] or scope.get("provider_identity") != case["provider_identity"]
                or scope.get("run_input_digest") != digest(case["run_input"])
                or selected.get("result_id") != result["result_id"] or selected.get("result_revision") != result["revision"]
                or scope.get("binding_state") not in {"ready", "cleaned"}
                or not scope.get("cleanup", {}).get("receipt_digest" if scope.get("binding_state") == "cleaned" else "creation_receipt_digest")
                or not instant(value["read_started_at"]) <= instant(value["observed_at"])):
            raise ValueError("reuse checkpoint differs from the original result, source or cleanup")
        values.append(value)
    first, last = values
    if (first["scope"] != last["scope"] or first["selection"] != last["selection"]
            or not instant(first["observed_at"]) < instant(last["read_started_at"])
            or (instant(last["observed_at"]) - instant(first["observed_at"])).total_seconds() > 86400):
        raise ValueError("reuse interval crosses identities or its bounded time window")
    for name in ("transport", "runs", "work"):
        identity = {"transport": "observation_id", "runs": "run_id", "work": "work_id"}[name]
        for value in values:
            items = value["runtime"][name]
            if len({item[identity] for item in items}) != len(items):
                raise ValueError("reuse runtime observation repeats an identity")
    before = {item["observation_id"]: item for item in first["runtime"]["transport"]}
    after = {item["observation_id"]: item for item in last["runtime"]["transport"]}
    if any(after.get(key) != value for key, value in before.items()):
        raise ValueError("append-only Start observation history changed")
    return first, last


def evaluate(document, case, clients, context, installed):
    first, last = snapshots(document, case)
    before, after = first["runtime"], last["runtime"]
    scope = first["scope"]
    reasons = []
    coverage = installed.get("observation_interval", {}) if isinstance(installed, dict) else {}
    if (not isinstance(installed, dict) or installed.get("source") != "independent-installed-read"
            or installed.get("source_revision") != context["source_revision"] or len(installed.get("snapshot_sha256", [])) != 3
            or not coverage.get("start") or not coverage.get("end")
            or instant(coverage["start"]) > instant(first["read_started_at"])
            or instant(coverage["end"]) < instant(last["observed_at"])):
        reasons.append("installed_instrumented_interval_not_verified")
    if before["runs"] != after["runs"] or before["work"] != after["work"] or before["transport"] != after["transport"]:
        reasons.append("selected_configuration_has_work_or_transport_change")
    grouped = {}
    for row in before["transport"]:
        grouped.setdefault(row["send_id"], []).append(row)
    for rows in grouped.values():
        if (len(rows) != 2 or [row["phase"] for row in rows] != ["before_send", "after_call"]
                or rows[0]["run_id"] != rows[1]["run_id"] or rows[0]["attempt_id"] != rows[1]["attempt_id"]
                or rows[0]["facts"].get("request_hash") != rows[1]["facts"].get("request_hash")
                or any(row["facts"].get("contract") != "wa-start-transport/v1"
                       or instant(row["observed_at"]) > instant(first["read_started_at"]) for row in rows)
                or not rows[1]["facts"].get("provider_request_id")):
            reasons.append("preexisting_start_is_uncertain")
    audit = document["native_audit"]
    if (audit.get("source") != "independent-cloudtrail-read" or audit.get("pagination_complete") is not True
            or audit.get("start") != first["observed_at"] or audit.get("end") != last["read_started_at"]
            or audit.get("events") != []):
        reasons.append("selected_native_audit_incomplete_or_has_start")
    if before["replays"] or before["usage"] is not None:
        reasons.append("replay_or_usage_predates_checkpoint")
    usage = clients.get("usage") or {}
    recorded = after.get("usage") or {}
    if (usage.get("task_id") != first["selection"].get("task_id") or usage.get("actor_id") != scope["actor_id"]
            or scope.get("run_admission_digest") != digest(usage.get("origin", {}).get("admission"))
            or recorded.get("task_id") != usage.get("task_id") or recorded.get("actor_id") != usage.get("actor_id")
            or recorded.get("result_id") != scope["result_id"]
            or any(recorded.get(key) != usage.get(key) for key in ("retrieval_receipt", "usage_receipt"))
            or not recorded.get("observed_at")
            or not instant(first["observed_at"]) < instant(recorded["observed_at"]) <= instant(last["read_started_at"])):
        reasons.append("exact_later_task_use_not_observed_inside_interval")
    peers = {item["destination"]: item for item in clients.get("publication", {}).get("consumers", [])}
    replayed = after["replays"]
    if len(replayed) != 2 or {item["destination"] for item in replayed} != {"product-context", "engineering-insights"}:
        reasons.append("both_publication_replays_not_observed")
    else:
        for row in replayed:
            peer = peers.get(row["destination"], {})
            original = next((item for item in scope["publications"] if item["destination"] == row["destination"]), {})
            selected_key = "context_retry_key" if row["destination"] == "product-context" else "insights_retry_key"
            response = row["response"]
            if (row["idempotency_key"] != first["selection"].get(selected_key)
                    or not isinstance(row.get("request_hash"), str) or not re.fullmatch(r"[a-f0-9]{64}", row["request_hash"])
                    or row["publication_id"] != peer.get("publication_id") or row["publication_id"] != original.get("publication_id")
                    or response.get("publication_id") != row["publication_id"] or response.get("result_id") != scope["result_id"]
                    or response.get("state") != "published" or response.get("invalidation_watermark") != 0
                    or response.get("contribution_hash") != original.get("contribution_hash")
                    or response.get("receipt_id") != peer.get("receipt_id") or not response.get("receipt_id")
                    or response.get("consumer_revision") != peer.get("consumer_revision")):
                reasons.append("replay_receipt_differs_from_original_publication")
    if recorded.get("publication_id") != peers.get("product-context", {}).get("publication_id"):
        reasons.append("later_task_publication_identity_changed")
    return {"outcome": "incomplete" if reasons else "passed", "blocker_codes": sorted(set(reasons)),
            "adp_start_count": None if reasons else 0, "provider_effect_count": None,
            "claim_scope": "selected_adp_configuration_interval_only", "accountwide_native_zero_proved": False,
            "limitations": ["cloudtrail_delivery_is_delayed", "no_accountwide_absence_claim", "publication_fault_history_not_proved"]}


def collect(selection, case, connection):
    selected = selection.get("qualification_reuse_interval")
    if (not isinstance(selected, dict) or set(selected) != {"before_id", "after_id"}
            or any(not isinstance(value, str) or not IDENTIFIER.fullmatch(value) for value in selected.values())
            or selected["before_id"] == selected["after_id"]):
        raise ValueError("select distinct original reuse checkpoint IDs, never counts or verdicts")
    with connection(selection) as client:
        product = ProductReader(client, selection["scope"])
        response = client.post(product.path + "/qualification-experiments/reuse-intervals/read", json={**selected, "read_native_audit": True})
        response.raise_for_status()
        document = response.json()
        snapshots(document, case)
        if any(document[name]["checkpoint_id"] != selected[name + "_id"] for name in ("before", "after")):
            raise ValueError("reuse reader returned a different original checkpoint")
        return document


def selected_intervals(value, selections):
    if (not isinstance(value, dict) or not 1 <= len(value) <= 2
            or not set(value) <= {case["run_id"] for case in selections}
            or any(not isinstance(item, dict) or set(item) != {"before_id", "after_id"}
                   or any(not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier) for identifier in item.values())
                   or item["before_id"] == item["after_id"] for item in value.values())):
        raise ValueError("select existing before/after checkpoint IDs for original runs; keep the frozen fixture unchanged")
    return value


def observe(selections, cases, prior, selected, driver):
    import qualification_well_architected_clients as clients
    import qualification_well_architected_cleanup as cleanup
    from qualification_well_architected_revocation import stable_evaluations

    selected_intervals(selected, selections)
    if len(selections) != 2 or len(cases) != 2 or len(prior) != 2:
        raise ValueError("reuse observations retain both original release cases")
    evaluated = []
    for selection, case, previous in zip(selections, cases, prior, strict=True):
        if selection["run_id"] not in selected:
            fresh = cleanup.observe_case(selection, case, previous, driver)
            if stable_evaluations([fresh]) != stable_evaluations([previous]):
                raise ValueError("the other case's original readbacks changed during reuse collection")
        else:
            fresh = deepcopy(previous)
            document = collect({**selection, "qualification_reuse_interval": selected[selection["run_id"]]}, case, driver.product_connection)
            client_selection = {key: value for key, value in selection.get("qualification_clients", {}).items() if key != "retained_reuse"}
            client_selection["design_task_id"] = document["before"]["snapshot"]["selection"]["task_id"]
            if document["before"]["snapshot"]["scope"]["binding_state"] == "cleaned":
                client_selection["retained_reuse"] = True
            current = clients.collect({**selection, "qualification_clients": client_selection}, driver.product_connection, read_only=True)
            # Original successful artifact reads and foreign denials precede the
            # cleanup/unlink. Preserve them; refresh only current exact use facts.
            prior_clients = fresh.setdefault("clients", {})
            for key in ("usage", "publication", "clients"):
                prior_clients[key] = current[key]
            fresh["reuse_interval"] = document
        evaluated.append(fresh)
    return evaluated
