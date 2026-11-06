"""Independent evaluation of a frozen DevLake operating campaign."""

import base64
import binascii
import argparse
import hashlib
import json
import os
import math
import sys
from datetime import datetime
from pathlib import Path


def _instant(value):
    if not isinstance(value, str):
        raise ValueError("timestamps must be ISO-8601 strings")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("invalid timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError("timestamp must have a timezone")
    return parsed


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
        raise ValueError("measurement must be finite and nonnegative")
    return value


CONTINUATION_STEPS = frozenset(("worker_restart", "workflow_loss", "queued_ci", "token_refresh",
                                "checkpoint_recovery", "mediated_repair", "automatic_successor"))


def unattended(receipts, batches, frozen_digest, limits_digest, campaign_id=None, revision=None):
    """Check independently supplied continuation receipts against durable campaign pins."""
    if not receipts:
        return {"status": "not_run", "missing": sorted(CONTINUATION_STEPS)}
    if set(receipts) - CONTINUATION_STEPS:
        raise ValueError("unknown unattended continuation step")
    common = None
    required = {
        "worker_restart": ("lost_worker_id", "resumed_worker_id"),
        "workflow_loss": ("lost_workflow_id", "resumed_workflow_id"),
        "queued_ci": ("ci_run_id", "queued_at", "started_at"),
        "token_refresh": ("old_session_id", "new_session_id"),
        "checkpoint_recovery": ("readback_ref",),
        "mediated_repair": ("failure_ref", "diagnosis_ref", "allowed_edit_ref", "repair_pr_ref",
                            "ci_run_id", "ci_pass_ref", "verified_merge_ref", "component_release_ref",
                            "independent_verification_ref"),
        "automatic_successor": ("successor_operation_id", "trigger", "verification_ref"),
    }
    for step, receipt in receipts.items():
        if (not isinstance(receipt, dict) or
                any(not isinstance(receipt.get(key), str) or not receipt[key]
                    for key in ("attempt_id", "observer", "evidence_ref")) or
                not isinstance(receipt.get("data"), dict)):
            raise ValueError("unattended continuation needs an independent attempt and evidence")
        data = receipt["data"]
        binding = tuple(data.get(key) for key in ("campaign_id", "revision", "correlation_id", "operation_id"))
        if (any(not isinstance(value, str) or not value for value in binding) or
                (campaign_id is not None and binding[0] != campaign_id) or
                (revision is not None and binding[1] != revision) or
                data.get("frozen_sha256") != frozen_digest or data.get("limits_sha256") != limits_digest or
                (common is not None and binding != common)):
            raise ValueError("unattended continuation pins or correlation changed")
        common = binding
        if not isinstance(data.get("workflow_id"), str) or not data["workflow_id"]:
            raise ValueError("unattended continuation workflow is missing")
        _instant(data.get("observed_at"))
        if step in ("worker_restart", "workflow_loss", "checkpoint_recovery"):
            batch_id = data.get("checkpoint_batch_id")
            if not isinstance(batch_id, str) or batch_id not in batches or batches[batch_id] != data.get("checkpoint_batch_sha256"):
                raise ValueError("unattended continuation checkpoint is missing or incompatible")
        if any(not isinstance(data.get(key), str) or not data[key] for key in required[step]):
            raise ValueError("unattended continuation step has missing observations")
        if step in ("worker_restart", "workflow_loss", "token_refresh"):
            old, new = (("lost_worker_id", "resumed_worker_id") if step == "worker_restart" else
                        ("lost_workflow_id", "resumed_workflow_id") if step == "workflow_loss" else
                        ("old_session_id", "new_session_id"))
            if data[old] == data[new]:
                raise ValueError("unattended continuation has no actual replacement")
        if step == "workflow_loss" and data["workflow_id"] != data["resumed_workflow_id"]:
            raise ValueError("unattended continuation did not reattach to resumed workflow")
        if step == "queued_ci" and not _instant(data["queued_at"]) < _instant(data["started_at"]):
            raise ValueError("queued CI has no observed wait")
    missing = sorted(CONTINUATION_STEPS - set(receipts))
    if missing:
        return {"status": "incomplete", "missing": missing}
    repair = receipts["mediated_repair"]["data"]
    successor = receipts["automatic_successor"]["data"]
    if (repair["ci_run_id"] != receipts["queued_ci"]["data"]["ci_run_id"] or
            successor["trigger"] != "automatic" or
            successor.get("source_operation_id") != common[3] or
            successor["successor_operation_id"] == common[3] or
            successor["verification_ref"] != repair["independent_verification_ref"] or
            not _instant(repair["observed_at"]) < _instant(successor["observed_at"]) or
            any(receipts[step]["data"]["workflow_id"] != receipts["workflow_loss"]["data"]["resumed_workflow_id"]
                for step in ("queued_ci", "token_refresh", "checkpoint_recovery", "mediated_repair",
                             "automatic_successor"))):
        raise ValueError("unattended repair, CI, verification or successor is disconnected")
    return {"status": "pass", "missing": []}


def freshness(frozen_updates, read_observations, limits):
    """Measure native event to first independently observed authorized read."""
    expected_count = limits["controlled_updates"]
    if len(frozen_updates) != expected_count:
        raise ValueError("frozen update count differs from the accepted sample")
    updates = {}
    for update in frozen_updates:
        identifier = update["update_id"]
        if not identifier or identifier in updates:
            raise ValueError("duplicate or empty frozen update ID")
        updates[identifier] = update
    if len({update["provider"] for update in frozen_updates}) < 2:
        raise ValueError("sample requires two providers")

    reads = {}
    for observation in read_observations:
        identifier = observation["update_id"]
        if identifier not in updates or identifier in reads:
            raise ValueError("unknown or duplicated authorized read")
        reads[identifier] = observation

    missing = sorted(updates.keys() - reads.keys())
    unavailable = []
    lags = {}
    for identifier, observation in reads.items():
        update = updates[identifier]
        if (observation.get("revision") != update["revision"] or
                observation.get("authorized") is not True or not observation.get("read_at") or
                not update.get("source_at")):
            unavailable.append(identifier)
            continue
        lag = (_instant(observation["read_at"]) - _instant(update["source_at"])).total_seconds() / 60
        if lag < 0:
            unavailable.append(identifier)
        else:
            lags[identifier] = lag

    times = [_instant(update["source_at"]) for update in frozen_updates if update.get("source_at")]
    span_hours = ((max(times) - min(times)).total_seconds() / 3600
                  if len(times) == expected_count else None)
    p95 = (sorted(lags.values())[math.ceil(0.95 * expected_count) - 1]
           if len(lags) == expected_count else None)
    status = ("incomplete" if missing or span_hours is None or span_hours < limits["minimum_observation_hours"]
              else "unavailable" if unavailable
              else "pass" if p95 <= limits["freshness_p95_minutes"] else "fail")
    return {"status": status, "expected": expected_count, "matched": len(lags),
            "missing": missing, "unavailable": sorted(unavailable), "lag_minutes": lags,
            "span_hours": span_hours, "p95_minutes": p95}


def operating_bounds(observations, approved_limits):
    """Report queries and observed costs; never turn unapproved bounds into passes."""
    queries = observations.get("query_seconds")
    jobs = observations.get("jobs")
    storage = observations.get("storage_gib_per_cell")
    cost = observations.get("cost_usd")
    if (not approved_limits.get("approved") or not queries or not jobs or not storage or
            not isinstance(cost, dict) or cost.get("estimate") is None or
            cost.get("billed") is None or not cost.get("basis")):
        return {"status": "unavailable", "query_seconds": None, "cost_usd": cost}
    ordered = sorted(_number(item) for item in queries)
    limits = approved_limits["limits"]
    exceeded = (any(_number(job["vcpu"]) > limits["worker_vcpu_per_job"] or
                    _number(job["memory_mib"]) > limits["worker_memory_mib_per_job"] for job in jobs) or
                any(_number(item) > limits["incremental_storage_gib_per_cell"] for item in storage) or
                _number(cost["estimate"]) > limits["total_incremental_budget_usd"] or
                _number(cost["billed"]) > limits["total_incremental_budget_usd"])
    return {"status": "fail" if exceeded else "pass",
            "query_seconds": {"count": len(ordered), "p95": ordered[math.ceil(0.95 * len(ordered)) - 1],
                              "max": ordered[-1]}, "cost_usd": cost}


def _evidence_check(entry, required, predicate):
    if not isinstance(entry, dict) or any(entry.get(field) is None for field in required):
        return "unavailable"
    return "pass" if predicate(entry) else "fail"


def recovery(observations, required_db_clients):
    """Assess separate source, DB, lifecycle and policy observations, never a claimed result."""
    if not required_db_clients or len(required_db_clients) != len(set(required_db_clients)):
        raise ValueError("pin every deployed DB client before evaluating renewal")
    b1 = observations.get("b1")
    b1_fields = ("native", "sealed", "exported", "served", "b2_native", "manifest_acknowledged",
                 "pinned_mapper", "stale_generation_denied", "duplicate_count")
    checks = {"sealed_b1": _evidence_check(
        b1, b1_fields,
        lambda data: (bool(data["native"]) and data["native"] == data["sealed"] ==
                      data["exported"] == data["served"] and data["b2_native"] != data["native"] and
                      data["manifest_acknowledged"] is True and data["pinned_mapper"] is True and
                      data["stale_generation_denied"] is True and data["duplicate_count"] == 0))}
    outage = observations.get("source_outage")
    checks["source_outage"] = _evidence_check(
        outage, ("duration_minutes", "checkpoint_restored", "all_updates_reconciled", "duplicate_count"),
        lambda data: (_number(data["duration_minutes"]) > 15 and data["checkpoint_restored"] is True and
                      data["all_updates_reconciled"] is True and data["duplicate_count"] == 0))
    revoked = observations.get("revocation")
    checks["revocation"] = _evidence_check(
        revoked, ("before_epoch", "after_epoch", "barrier_observed", "late_pages_discarded",
                  "read_denied", "writes_after", "provider_calls_after"),
        lambda data: (data["before_epoch"] != data["after_epoch"] and data["barrier_observed"] is True and
                      data["late_pages_discarded"] is True and data["read_denied"] is True and
                      data["writes_after"] == 0 and data["provider_calls_after"] == 0))
    clients = observations.get("db_clients")
    if not isinstance(clients, dict) or set(clients) != set(required_db_clients):
        checks["iam_renewal"] = "unavailable"
    else:
        results = [_evidence_check(
            clients[name], ("old_token_age_minutes", "new_token_age_minutes", "fresh_sts",
                            "verified_tls", "password_fallback"),
            lambda data: (_number(data["old_token_age_minutes"]) > 15 and
                          _number(data["new_token_age_minutes"]) < 15 and data["fresh_sts"] is True and
                          data["verified_tls"] is True and data["password_fallback"] is False))
            for name in required_db_clients]
        checks["iam_renewal"] = ("fail" if "fail" in results else
                                  "unavailable" if "unavailable" in results else "pass")
    modes = observations.get("modes")
    mode_fields = ("off", "connector_only", "full", "invalid_rejected", "disabled_during_work")
    checks["optional_modes"] = _evidence_check(
        modes, mode_fields,
        lambda data: (data["off"] == {"provider_calls": 0, "new_optional_resources": 0} and
                      data["connector_only"] == {"profile_available": True, "insights_resources": 0,
                                                 "db_clients": 0} and
                      data["full"] == {"grants_checked": True, "collection_available": True} and
                      data["invalid_rejected"] is True and data["disabled_during_work"] is True))
    profile = observations.get("profile")
    checks["profile_and_holds"] = _evidence_check(
        profile, ("expected_edited", "after_reenable", "holds_before", "holds_after",
                  "shared_before", "shared_after"),
        lambda data: (bool(data["expected_edited"]) and data["expected_edited"] == data["after_reenable"] and
                      data["holds_before"] == data["holds_after"] and
                      data["shared_before"] == data["shared_after"]))
    finalization = observations.get("finalization")
    if not isinstance(finalization, dict) or set(finalization) != {"failed", "aborted", "expired"}:
        checks["finalization"] = "unavailable"
    else:
        results = [_evidence_check(
            finalization[outcome], ("original_outcome", "attempted_while_off", "owned_absent",
                                   "foreign_and_holds_retained", "interrupted_resumed",
                                   "authority_loss_escalated", "failed_or_not_run_preserved"),
            lambda data: (data["original_outcome"] == outcome and data["attempted_while_off"] is True and
                          data["owned_absent"] is True and data["foreign_and_holds_retained"] is True and
                          data["interrupted_resumed"] is True and data["authority_loss_escalated"] is True and
                          data["failed_or_not_run_preserved"] is True))
            for outcome in ("failed", "aborted", "expired")]
        checks["finalization"] = ("fail" if "fail" in results else
                                   "unavailable" if "unavailable" in results else "pass")
    status = ("fail" if "fail" in checks.values() else "unavailable" if "unavailable" in checks.values()
              else "pass")
    return {"scope": "offline-evidence-evaluation", "status": status, "checks": checks}


def compatibility(observations, pinned):
    """Assess a second pinned upstream build, pre-provider gates and prior-image rollback."""
    pins = ("baseline_upstream", "candidate_upstream", "baseline_image", "candidate_image",
            "schema_manifest_sha256", "requirements_sha256", "connector_contract_sha256")
    if not isinstance(pinned, dict) or any(not pinned.get(name) for name in pins):
        raise ValueError("baseline and candidate revisions must be frozen before upgrade checks")
    if pinned["baseline_upstream"] == pinned["candidate_upstream"] or pinned["baseline_image"] == pinned["candidate_image"]:
        raise ValueError("the candidate must be a different pinned upstream and image")
    observed = observations.get("pins")
    checks = {"revision_pins": _evidence_check(
        observed, pins, lambda data: all(data[name] == pinned[name] for name in pins))}
    patches = observations.get("patches")
    patch_summary = None
    if not isinstance(patches, list) or not patches:
        checks["patch_comparison"] = "unavailable"
    else:
        complete = all(isinstance(patch, dict) and patch.get("patch_id") and
                       patch.get("status") in {"reused", "rebased", "conflicted"} and
                       patch.get("effort_minutes") is not None and patch.get("resolved") is not None
                       for patch in patches)
        if not complete:
            checks["patch_comparison"] = "unavailable"
        else:
            identifiers = [patch["patch_id"] for patch in patches]
            patch_summary = {"reused": sum(patch["status"] == "reused" for patch in patches),
                             "rebased": sum(patch["status"] == "rebased" for patch in patches),
                             "conflicted": sum(patch["status"] == "conflicted" for patch in patches),
                             "effort_minutes": sum(_number(patch["effort_minutes"]) for patch in patches)}
            checks["patch_comparison"] = ("pass" if len(set(identifiers)) == len(identifiers) and
                                          all(patch["resolved"] is True for patch in patches) else "fail")
    negatives = observations.get("negative_admission")
    for name in ("missing_schema", "incompatible_schema", "missing_guard", "incompatible_connector"):
        attempt = negatives.get(name) if isinstance(negatives, dict) else None
        checks[name] = _evidence_check(
            attempt, ("result", "provider_calls", "writes", "fallback_used"),
            lambda data: (data["result"] == "migration_required" and data["provider_calls"] == 0 and
                          data["writes"] == 0 and data["fallback_used"] is False))
    migrated = observations.get("migrated")
    checks["migrated_dml"] = _evidence_check(
        migrated, ("migration_role_applied", "receipt_sha256", "first_scope_dml",
                   "new_scope_dml", "worker_ddl_used"),
        lambda data: (data["migration_role_applied"] is True and
                      data["receipt_sha256"] == pinned["schema_manifest_sha256"] and
                      data["first_scope_dml"] is True and data["new_scope_dml"] is True and
                      data["worker_ddl_used"] is False))
    ddl = observations.get("worker_ddl")
    checks["worker_ddl"] = _evidence_check(
        ddl, ("create", "alter", "drop", "schema_unchanged"),
        lambda data: (all(data[action] == "denied" for action in ("create", "alter", "drop")) and
                      data["schema_unchanged"] is True))
    rollback = observations.get("rollback")
    checks["rollback"] = _evidence_check(
        rollback, ("restored_image", "profile_before", "profile_after", "evidence_before",
                   "evidence_after", "shared_before", "shared_after", "read_compatible", "evidence_ref"),
        lambda data: (data["restored_image"] == pinned["baseline_image"] and
                      bool(data["evidence_ref"]) and
                      bool(data["profile_before"]) and data["profile_before"] == data["profile_after"] and
                      bool(data["evidence_before"]) and data["evidence_before"] == data["evidence_after"] and
                      bool(data["shared_before"]) and data["shared_before"] == data["shared_after"] and
                      data["read_compatible"] is True))
    status = ("fail" if "fail" in checks.values() else "unavailable" if "unavailable" in checks.values()
              else "pass")
    return {"scope": "offline-evidence-evaluation", "status": status, "checks": checks,
            "patch_comparison": patch_summary}


def _same_bytes(before, after):
    try:
        original = base64.b64decode(before, validate=True)
        current = base64.b64decode(after, validate=True)
    except (TypeError, ValueError, binascii.Error):
        return False
    return original == current


def isolation(observations, pinned):
    """Compare independently observed routes, policy barriers and exact shared bytes."""
    if not isinstance(pinned, dict) or not isinstance(pinned.get("scopes"), list):
        raise ValueError("frozen isolation scopes are required")
    scopes = pinned["scopes"]
    if (len(scopes) < 4 or not pinned.get("collision_id") or not pinned.get("descriptor_sha256") or
            any(not all(scope.get(key) for key in ("scope_key", "tenant_id", "project_id", "realm_id",
                                                      "cell_id", "source_connection_id", "visibility"))
                for scope in scopes) or
            len({scope["scope_key"] for scope in scopes}) != len(scopes) or
            len({scope["cell_id"] for scope in scopes}) != len(scopes) or
            len({scope["tenant_id"] for scope in scopes}) < 2 or
            not any(len({scope["project_id"] for scope in scopes if scope["tenant_id"] == tenant}) >= 2
                    for tenant in {scope["tenant_id"] for scope in scopes}) or
            not any(first["tenant_id"] == second["tenant_id"] and
                    first["project_id"] == second["project_id"] and
                    {first["visibility"], second["visibility"]} == {"shared", "private"}
                    for first in scopes for second in scopes)):
        raise ValueError("freeze two tenants, two same-tenant Projects and a shared/private cell pair")
    routes = observations.get("routes")
    if not isinstance(routes, list) or len(routes) != len(scopes):
        route_result = "unavailable"
    else:
        by_key = {route.get("scope_key"): route for route in routes if isinstance(route, dict)}
        route_result = "fail" if len(by_key) != len(scopes) or set(by_key) != {s["scope_key"] for s in scopes} else "pass"
        for scope in scopes:
            route = by_key.get(scope["scope_key"])
            if not route:
                continue
            identity = route.get("identity")
            if (not isinstance(identity, list) or len(identity) != 6 or
                    identity[:3] != [scope["tenant_id"], scope["project_id"], scope["source_connection_id"]] or
                    identity[4] != pinned["collision_id"] or not identity[3] or not identity[5] or
                    route.get("cell_id") != scope["cell_id"] or route.get("realm_id") != scope["realm_id"] or
                    route.get("visibility") != scope["visibility"] or
                    route.get("scope_checked_before_raw_write") is not True or
                    route.get("policy_checked_before_response") is not True):
                route_result = "fail"
    checks = {"cell_and_identity_routes": route_result}
    checks["concurrent_collection"] = _evidence_check(
        observations.get("concurrency"), ("overlap_observed", "max_active_jobs"),
        lambda data: data["overlap_observed"] is True and data["max_active_jobs"] == 2)
    negative = observations.get("negative")
    for name in ("forged_route", "foreign_tenant", "same_tenant_project", "private_influence", "stale_policy"):
        attempt = negative.get(name) if isinstance(negative, dict) else None
        checks[name] = _evidence_check(
            attempt, ("result", "raw_writes", "released_bytes", "shared_mutations"),
            lambda data: (data["result"] == "denied" and data["raw_writes"] == 0 and
                          data["released_bytes"] == 0 and data["shared_mutations"] == 0))
    graph = observations.get("policy_graph")
    checks["policy_graph"] = _evidence_check(
        graph, ("descriptor_sha256", "current_epoch", "closure_verified", "private_in_shared",
                "response_rechecked"),
        lambda data: (data["descriptor_sha256"] == pinned["descriptor_sha256"] and
                      bool(data["current_epoch"]) and data["closure_verified"] is True and
                      data["private_in_shared"] == 0 and data["response_rechecked"] is True))
    revocation = observations.get("revocation")
    checks["revocation_barrier"] = _evidence_check(
        revocation, ("before_epoch", "after_epoch", "barrier_observed", "read_denied",
                     "released_bytes", "raw_writes_after", "pre_release_rechecked"),
        lambda data: (data["before_epoch"] != data["after_epoch"] and
                      data["after_epoch"] == graph.get("current_epoch") and
                      data["barrier_observed"] is True and data["read_denied"] is True and
                      data["released_bytes"] == 0 and data["raw_writes_after"] == 0 and
                      data["pre_release_rechecked"] is True)) if isinstance(graph, dict) else "unavailable"
    storage = observations.get("storage_denials")
    for name in ("db", "s3", "kms"):
        attempt = storage.get(name) if isinstance(storage, dict) else None
        checks[name + "_denial"] = _evidence_check(
            attempt, ("workload_role", "result", "foreign_bytes", "foreign_writes"),
            lambda data: (bool(data["workload_role"]) and data["result"] == "denied" and
                          data["foreign_bytes"] == 0 and data["foreign_writes"] == 0))
    shared = observations.get("shared_publication")
    shared_fields = ("before_bytes_base64", "after_bytes_base64", "before_status", "after_status",
                     "before_pointer", "after_pointer", "before_coverage", "after_coverage",
                     "before_freshness", "after_freshness", "private_rejected")
    checks["shared_publication"] = _evidence_check(
        shared, shared_fields,
        lambda data: (_same_bytes(data["before_bytes_base64"], data["after_bytes_base64"]) and
                      data["before_status"] == data["after_status"] and
                      data["before_pointer"] == data["after_pointer"] and
                      data["before_coverage"] == data["after_coverage"] and
                      data["before_freshness"] == data["after_freshness"] and
                      data["private_rejected"] is True))
    status = ("fail" if "fail" in checks.values() else "unavailable" if "unavailable" in checks.values()
              else "pass")
    return {"scope": "offline-evidence-evaluation", "status": status, "checks": checks}


def _load_document(path, label):
    try:
        document = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is missing or invalid JSON") from error
    if not isinstance(document, dict):
        raise ValueError(f"{label} must be a JSON object")
    return document


COMPONENTS = ("gateway", "connector", "collector", "schema", "mapper", "configuration")


def _pilot_pins(manifest):
    pins = {field: manifest.get(field) for field in ("source_revision", "definition_revision",
                                                      "run_id", "run_attempt")}
    components = manifest.get("component_digests")
    if any(not isinstance(pins[field], str) or not pins[field].isdecimal() or len(pins[field]) > 20 or int(pins[field]) == 0
           for field in ("run_id", "run_attempt")):
        raise ValueError("numeric workflow run and attempt pins are required")
    if (any(not isinstance(value, str) or not value for value in pins.values()) or
            not isinstance(components, dict) or set(components) != set(COMPONENTS) or
            any(not isinstance(value, str) or len(value) != 64 or
                any(character not in "0123456789abcdef" for character in value)
                for value in components.values())):
        raise ValueError("source, workflow, run and component pins are required")
    return {**pins, "component_digests": components}


def required_pilot_criteria(plan):
    rows = plan.get("criteria")
    if not isinstance(rows, list) or not rows or any(
            not isinstance(row, dict) or not isinstance(row.get("criterion_id"), str)
            or not row["criterion_id"] or not isinstance(row.get("scenario_id"), str)
            or not row["scenario_id"] or not isinstance(row.get("story"), str)
            for row in rows):
        raise ValueError("acceptance plan has no valid criterion inventory")
    required = {row["criterion_id"]: row for row in rows if row["story"] != "S08"}
    if len(required) != sum(row["story"] != "S08" for row in rows):
        raise ValueError("acceptance plan repeats a required criterion")
    return required


def pilot_document(manifest, plan, read_receipt=None):
    """Evaluate exact external readbacks; this offline result is never live attestation."""
    required = required_pilot_criteria(plan)
    criteria = manifest.get("criteria")
    if not isinstance(criteria, dict):
        raise ValueError(f"pilot evidence missing {len(required)} required criteria")
    missing = sorted(set(required) - set(criteria))
    if missing:
        raise ValueError(f"pilot evidence missing {len(missing)} required criteria (first: {missing[0]})")
    pins = _pilot_pins(manifest)
    if read_receipt is None:
        raise ValueError("independent pilot observations are required before acceptance")
    results = {}
    for criterion_id, row in required.items():
        reference = criteria[criterion_id]
        if (not isinstance(reference, dict) or not isinstance(reference.get("ref"), str) or
                not reference["ref"] or not isinstance(reference.get("sha256"), str)):
            raise ValueError(f"criterion {criterion_id} has no pinned readback")
        try:
            raw = read_receipt(reference["ref"])
        except (KeyError, OSError) as error:
            raise ValueError(f"criterion {criterion_id} independent readback is missing") from error
        if (not isinstance(raw, bytes) or
                hashlib.sha256(raw).hexdigest() != reference["sha256"]):
            raise ValueError(f"criterion {criterion_id} readback digest differs")
        try:
            observation = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ValueError(f"criterion {criterion_id} readback is invalid") from error
        if (not isinstance(observation, dict) or observation.get("criterion_id") != criterion_id or
                observation.get("scenario_id") != row["scenario_id"] or
                any(observation.get(field) != value for field, value in pins.items()) or
                not isinstance(observation.get("observer"), str) or not observation["observer"] or
                not isinstance(observation.get("evidence_ref"), str) or not observation["evidence_ref"] or
                observation.get("outcome") not in ("pass", "fail", "skipped", "not-run")):
            raise ValueError(f"criterion {criterion_id} readback provenance or outcome differs")
        results[criterion_id] = {"outcome": observation["outcome"], "scenario_id": row["scenario_id"],
                                 "evidence_ref": observation["evidence_ref"]}
    counts = {outcome: sum(value["outcome"] == outcome for value in results.values())
              for outcome in ("pass", "fail", "skipped", "not-run")}
    summary = {"scope": "offline-evidence-evaluation", "phase": "pilot",
               "status": "pass" if counts["pass"] == len(required) else "fail",
               "independent_verification": "offline-only", "criteria": results,
               "required_passed": counts["pass"], "required_failed": counts["fail"],
               "required_skipped": counts["skipped"], "required_not_run": counts["not-run"],
               **pins}
    from jsonschema import Draft202012Validator
    schema = json.loads(Path(__file__).with_name("campaign.schema.json").read_text())
    if not Draft202012Validator({"$defs": schema["$defs"], "$ref": "#/$defs/pilot_summary"}).is_valid(summary):
        raise ValueError("pilot summary does not match campaign schema")
    return summary


def native_pilot_artifact(summary, selected, artifact_name, plan):
    """Check the envelope independently required by the native workflow reader."""
    expected_name = f"devlake-pilot-{selected['run_id']}-{selected['run_attempt']}"
    if artifact_name != expected_name:
        raise ValueError("pilot artifact name does not match selected run and attempt")
    for field in ("source_revision", "definition_revision", "run_id", "run_attempt"):
        if not isinstance(selected.get(field), str) or summary.get(field) != selected[field]:
            raise ValueError(f"pilot artifact {field} differs from selected workflow")
    if summary.get("gateway_revision") != selected["source_revision"]:
        raise ValueError("pilot artifact gateway revision differs from selected source")
    from provider_evidence import selection_digest
    if (summary.get("provider_evidence_verified") is not True or
            summary.get("provider_revisions_digest") != selection_digest(
                selected.get("provider_selection"), selected["source_revision"])):
        raise ValueError("pilot artifact lacks verified current provider evidence")
    required = set(required_pilot_criteria(plan))
    results = summary.get("criteria")
    if (not isinstance(results, dict) or set(results) != required or
            summary.get("status") != "pass" or summary.get("scope") != "live-evidence-evaluation" or
            any(not isinstance(result, dict) or result.get("outcome") != "pass"
                for result in results.values()) or
            summary.get("phase") != "pilot" or
            summary.get("independent_verification") != "confirmed" or
            any(summary.get(field) != 0 for field in ("required_failed", "required_skipped",
                                                     "required_not_run", "human_interventions")) or
            summary.get("required_passed") != len(required) or
            summary.get("unresolved_operations") != [] or
            summary.get("authority_snapshot_valid") is not True or
            summary.get("requirements_snapshot_matches") is not True or
            summary.get("live_profile_contract_verified") is not True or
            not isinstance(summary.get("unattended"), dict) or
            summary["unattended"].get("resume_qualified") is not True or
            summary["unattended"].get("repair_qualified") is not True):
        raise ValueError("pilot artifact has missing, failed or unverified native predicates")
    from jsonschema import Draft202012Validator
    schema = json.loads(Path(__file__).with_name("campaign.schema.json").read_text())
    if not Draft202012Validator({"$defs": schema["$defs"], "$ref": "#/$defs/pilot_artifact"}).is_valid(summary):
        raise ValueError("pilot artifact does not match campaign schema")
    return True


def live_pilot_document(manifest, plan, read_receipt, selected, artifact_name):
    """Publish only from individually approved source workflows and a separate native readback."""
    offline = pilot_document(manifest, plan, read_receipt)
    if offline["status"] != "pass":
        raise ValueError("pilot criteria have failed, skipped or not-run observations")
    reference = manifest.get("native_receipt")
    if not isinstance(reference, dict) or not isinstance(reference.get("ref"), str):
        raise ValueError("independent native readback is missing")
    raw = read_receipt(reference["ref"])
    if (not isinstance(raw, bytes) or not isinstance(reference.get("sha256"), str) or
            hashlib.sha256(raw).hexdigest() != reference["sha256"]):
        raise ValueError("independent native readback digest differs")
    try:
        proof = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("independent native readback is invalid") from error
    if (not isinstance(proof, dict) or
            any(proof.get(field) != value for field, value in _pilot_pins(manifest).items()) or
            not isinstance(proof.get("observer"), str) or not proof["observer"] or
            not isinstance(proof.get("evidence_ref"), str) or not proof["evidence_ref"]):
        raise ValueError("independent native readback has changed pins or no observation")
    from provider_evidence import from_measurement, verify
    provider_receipt = manifest.get("provider_receipt")
    if (not isinstance(provider_receipt, dict) or not isinstance(provider_receipt.get("ref"), str) or
            not isinstance(provider_receipt.get("sha256"), str)):
        raise ValueError("independent provider readback is missing")
    provider_raw = read_receipt(provider_receipt["ref"])
    if (not isinstance(provider_raw, bytes) or
            hashlib.sha256(provider_raw).hexdigest() != provider_receipt["sha256"]):
        raise ValueError("independent provider readback digest differs")
    measured = from_measurement(provider_raw, provider_receipt["ref"], selected.get("provider_selection"),
                                manifest["source_revision"], manifest["run_id"], manifest["run_attempt"])
    if proof.get("provider_evidence") != measured:
        raise ValueError("native provider proof differs from independent readback")
    provider_digest = verify(measured, selected.get("provider_selection"),
                             manifest["source_revision"], manifest["run_id"], manifest["run_attempt"])
    summary = {**offline, "scope": "live-evidence-evaluation", "gateway_revision": manifest["source_revision"],
               "provider_evidence_verified": True, "provider_revisions_digest": provider_digest,
               "independent_verification": "confirmed",
               "criteria": {criterion_id: {"outcome": result["outcome"]}
                            for criterion_id, result in offline["criteria"].items()},
               **{field: proof.get(field) for field in ("unresolved_operations", "authority_snapshot_valid",
                   "requirements_snapshot_matches", "human_interventions", "live_profile_contract_verified",
                   "unattended")}}
    native_pilot_artifact(summary, selected, artifact_name, plan)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description="Verify independent DevLake campaign evidence")
    parser.add_argument("--phase", required=True, choices=("pilot", "closure"))
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        manifest = _load_document(args.manifest, "manifest")
        plan = _load_document(args.plan, "acceptance plan")
        if args.phase != "pilot":
            raise ValueError("closure evidence verification is not available")
        if not isinstance(manifest.get("criteria"), dict):
            pilot_document(manifest, plan)
        required = set(required_pilot_criteria(plan))
        missing = sorted(required - set(manifest["criteria"]))
        if missing:
            raise ValueError(f"pilot evidence missing {len(missing)} required criteria (first: {missing[0]})")
        if os.environ.get("GITHUB_ACTIONS") != "true":
            raise ValueError("protected GitHub Actions context and independent sources are required")
        selected = {field: os.environ.get(environment) for field, environment in (
            ("source_revision", "GITHUB_SHA"), ("definition_revision", "ADP_DEVLAKE_WORKFLOW_SHA"),
            ("run_id", "GITHUB_RUN_ID"), ("run_attempt", "GITHUB_RUN_ATTEMPT"))}
        try:
            selected["provider_selection"] = json.loads(os.environ.get("ADP_DEVLAKE_PROVIDER_SELECTION", ""))
        except json.JSONDecodeError as error:
            raise ValueError("protected provider selection is missing") from error
        if any(manifest.get(field) != selected[field] or not selected[field] for field in
               ("source_revision", "definition_revision", "run_id", "run_attempt")):
            raise ValueError("launch manifest differs from selected workflow revision or run")
        try:
            bindings = json.loads(os.environ.get("ADP_DEVLAKE_SOURCE_BINDINGS", ""))
        except json.JSONDecodeError as error:
            raise ValueError("approved independent source bindings are missing") from error
        from source_reader import approved_reader
        reader = approved_reader(manifest, plan, bindings)
        artifact_name = f"devlake-pilot-{selected['run_id']}-{selected['run_attempt']}"
        summary = live_pilot_document(manifest, plan, reader, selected, artifact_name)
        if args.output is not None:
            with args.output.open("x") as handle:
                json.dump(summary, handle, sort_keys=True)
        print(f"PASS pilot: {summary['required_passed']} required criteria; 0 failed/skipped/not-run")
    except OSError:
        print(f"FAIL {args.phase}: private input, source readback or output is unavailable", file=sys.stderr)
        return 1
    except ValueError as error:
        print(f"FAIL {args.phase}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
