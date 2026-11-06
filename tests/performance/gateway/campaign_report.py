"""Protected campaign evidence and intentionally conservative public summaries."""

import hashlib
import json
import math
import os
import uuid
from pathlib import Path


MATRIX_CELLS = (
    "opus_ramp", "astra_ramp", "distinct_developers", "distinct_agents",
    "distinct_tenants", "cached_context", "unique_context", "fan_out",
    "burst_recovery", "bounded_soak", "mixed_models", "flow_callback",
    "release_upgrade", "cli_artifact_recovery", "http_pool_2_plus_1",
)


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[math.ceil(len(ordered) * fraction) - 1], 2)


def qualify_cell(name, result, config):
    if not isinstance(result, dict) or not result.get("protected_receipt"):
        return "blocked"
    if result.get("revision") != config["revision"]:
        return "fail"
    limits = result.get("declared_limits")
    health = result.get("health")
    if not isinstance(limits, dict) or not isinstance(health, dict):
        return "fail"
    ceilings = {"attempts": 12000, "active_seconds": 1440, "drain_seconds": 240}
    for key, ceiling in ceilings.items():
        bound = limits.get(key)
        if isinstance(bound, bool) or not isinstance(bound, (int, float)) or not math.isfinite(bound) or not 0 < bound <= ceiling:
            return "fail"
    for key in ("input_tokens", "output_tokens", "max_spend"):
        bound = limits.get(key)
        if isinstance(bound, bool) or not isinstance(bound, (int, float)) or not math.isfinite(bound) or bound <= 0:
            return "fail"
    attempts = result.get("attempts")
    complete = result.get("complete_streams")
    if (not isinstance(attempts, int) or isinstance(attempts, bool) or attempts <= 0
            or not isinstance(complete, int) or isinstance(complete, bool)
            or complete < 0 or complete > attempts
            or attempts > limits["attempts"] or complete / attempts < 0.99):
        return "fail"
    if (health.get("oom") is not False or isinstance(health.get("restarts"), bool)
            or health.get("restarts") != 0
            or health.get("dependencies_ok") is not True):
        return "fail"
    for key in ("active_seconds", "drain_seconds", "input_tokens", "output_tokens", "spend"):
        measured = result.get(key)
        cap = limits["max_spend"] if key == "spend" else limits[key]
        if (isinstance(measured, bool) or not isinstance(measured, (int, float))
                or not math.isfinite(measured) or not 0 <= measured <= cap):
            return "fail"
    latencies = result.get("p95")
    if not isinstance(latencies, dict) or any(
            not isinstance(latencies.get(key), (int, float)) or not math.isfinite(latencies[key])
            or latencies[key] < 0
            or latencies[key] > limit for key, limit in config["slos"].items()):
        return "fail"
    if name in {"opus_ramp", "astra_ramp"}:
        stages = result.get("plateaus", [])
        required = [(10, 60), (60, 120), (100, 120), (150, 120), (200, 300)]
        if (not isinstance(stages, list) or len(stages) != len(required) or any(
                not isinstance(stage, dict) or stage.get("configured_sessions") != sessions
                or stage.get("actual_sessions", 0) < sessions
                or stage.get("actual_dwell_seconds", 0) < dwell
                or stage.get("attempts", 0) <= 0
                or stage.get("complete_streams", 0) / stage["attempts"] < 0.99
                for stage, (sessions, dwell) in zip(stages, required))):
            return "fail"
    if name == "distinct_developers" and result.get("observed_developers", 0) < 2:
        return "fail"
    if name == "distinct_agents" and result.get("observed_agents", 0) < 2:
        return "fail"
    if name == "distinct_tenants" and result.get("observed_tenants", 0) < 2:
        return "fail"
    if name in {"cached_context", "unique_context"} and not result.get("provider_cache_usage"):
        return "fail"
    if name == "fan_out" and not result.get("fan_out_distribution"):
        return "fail"
    if name == "burst_recovery" and result.get("cooldown_complete") is not True:
        return "fail"
    if name == "bounded_soak" and result.get("active_seconds", 0) < 1200:
        return "fail"
    if name == "mixed_models" and len(result.get("model_counts", {})) < 2:
        return "fail"
    if name in {"flow_callback", "release_upgrade", "cli_artifact_recovery", "http_pool_2_plus_1"} and result.get("verified") is not True:
        return "fail"
    return "pass"


def observed_cell(config, evidence, attestation):
    campaign = evidence.get("campaign", {})
    rows = evidence.get("rows", [])
    if not campaign and not rows:
        return "not-run", None
    if not isinstance(campaign, dict) or not isinstance(rows, list) or not all(
            isinstance(row, dict) for row in rows):
        return "fail", None
    stages = campaign.get("stages", [])
    measured = {"attempts": campaign.get("physical_attempts"),
                "complete_streams": sum(row.get("success") is True and row.get("complete") is True
                                        for row in rows),
                "active_seconds": campaign.get("active_seconds"),
                "drain_seconds": campaign.get("drain_seconds"),
                "input_tokens": campaign.get("reserved_input_tokens"),
                "output_tokens": campaign.get("reserved_output_tokens"),
                "spend": campaign.get("reserved_spend"),
                "peak_inflight": campaign.get("peak_inflight"),
                "time_weighted_inflight": campaign.get("time_weighted_inflight")}
    expected_hash = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    if (evidence.get("revision") != config["revision"] or evidence.get("config_hash") != expected_hash
            or campaign.get("status") != "pass" or campaign.get("stop_reason")
            or campaign.get("outstanding") != 0 or evidence.get("success") is not True
            or not isinstance(measured["attempts"], int) or isinstance(measured["attempts"], bool)
            or measured["attempts"] != len(rows) or campaign.get("completed") != len(rows)
            or len(stages) != len(config["stages"])):
        return "fail", measured
    model = next(entry for entry in config["models"] if entry["api"] == config["ramp_api"])
    plateaus = []
    for expected, stage in zip(config["stages"], stages):
        stage_rows = [row for row in rows if row.get("stage") == expected["name"]]
        completed = sum(row.get("success") is True and row.get("complete") is True for row in stage_rows)
        attempts = stage.get("attempts")
        if (stage.get("name") != expected["name"] or stage.get("status") != "pass"
                or stage.get("achieved_sessions") != expected["desired_sessions"]
                or stage.get("actual_dwell_seconds", 0) < expected["min_dwell_seconds"]
                or len({row.get("session_index") for row in stage_rows}) != expected["desired_sessions"]
                or any(isinstance(row.get("session_index"), bool) or
                       not isinstance(row.get("session_index"), int) or
                       not 0 <= row["session_index"] < expected["desired_sessions"]
                       for row in stage_rows)
                or not isinstance(attempts, int) or attempts < expected["desired_sessions"]
                or attempts != len(stage_rows)
                or stage.get("completed") != attempts or stage.get("failures") != attempts - completed
                or completed / attempts < 0.99):
            return "fail", measured
        plateaus.append({"configured_sessions": expected["desired_sessions"],
                         "actual_sessions": stage["achieved_sessions"],
                         "actual_dwell_seconds": stage["actual_dwell_seconds"],
                         "attempts": attempts, "complete_streams": completed})
    if (sum(stage["attempts"] for stage in plateaus) != measured["attempts"]
            or not isinstance(measured["peak_inflight"], int)
            or measured["peak_inflight"] > max(stage["desired_sessions"] for stage in config["stages"])
            or any(row.get("api") != model["api"] or row.get("model") != model["model"]
                   for row in rows)):
        return "fail", measured
    measured["plateaus"] = plateaus
    measured["p95"] = {
        "ttft_ms": percentile([row["ttft_ms"] for row in rows if row.get("success")
                               and isinstance(row.get("ttft_ms"), (int, float))], 0.95),
        "http_ms": percentile([row["duration_ms"] for row in rows if row.get("success")
                               and isinstance(row.get("duration_ms"), (int, float))], 0.95),
        "terminal_ms": percentile([row["terminal_to_eof_ms"] for row in rows if row.get("success")
                                   and isinstance(row.get("terminal_to_eof_ms"), (int, float))], 0.95),
    }
    if config["plan"] == "comparison":
        from campaign_controller import comparison_measured, comparison_metrics
        comparison = comparison_metrics(config, rows, campaign)
        if evidence.get("comparison") != comparison or not comparison_measured(config, comparison):
            return "fail", measured
        measured.update({key: comparison[key] for key in (
            "observed_developers", "observed_agents", "observed_tenants",
            "observed_human_roots", "fan_out_distribution", "provider_cache_samples",
            "provider_cached_input_tokens", "prompt_variants")})
        measured["provider_cache_usage"] = {
            "samples": comparison["provider_cache_samples"],
            "cached_input_tokens": comparison["provider_cached_input_tokens"]}
    for key in ("attempts", "active_seconds", "drain_seconds", "input_tokens", "output_tokens", "spend"):
        value = measured[key]
        ceiling = config["limits"]["max_spend" if key == "spend" else key]
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                not math.isfinite(value) or value < 0 or value > ceiling):
            return "fail", measured
    if any(value is None or not math.isfinite(value) or value > config["slos"][key]
           for key, value in measured["p95"].items()):
        return "fail", measured
    if isinstance(attestation, dict):
        for key in measured:
            if key in attestation and attestation[key] != measured[key]:
                return "fail", measured
    health = attestation.get("health") if isinstance(attestation, dict) else None
    if isinstance(health, dict) and (health.get("oom") is True or
                                     isinstance(health.get("restarts"), int) and health["restarts"] > 0 or
                                     health.get("dependencies_ok") is False):
        return "fail", measured
    if config.get("local_test"):
        return "blocked", measured
    cleanup = evidence.get("cleanup", {})
    if cleanup.get("status") == "hold" or cleanup.get("accounting") == "unknown":
        return "fail", measured
    if cleanup.get("status") != "clean" or cleanup.get("accounting") != "observed":
        return "blocked", measured
    if not isinstance(attestation, dict) or not attestation.get("protected_receipt") or attestation.get("operator_verified") is not True:
        return "blocked", measured
    for field, expected in (("revision", config["revision"]), ("target", config["target"]),
                            ("config_hash", expected_hash)):
        if field not in attestation:
            return "blocked", measured
        if attestation[field] != expected:
            return "fail", measured
    result = {**measured, "revision": config["revision"], "declared_limits": config["limits"],
              "protected_receipt": attestation["protected_receipt"], "health": attestation.get("health")}
    cell = config.get("matrix_cell", config.get("comparison_cell"))
    status = qualify_cell(cell, result, config)
    return ("blocked" if status == "pass" and cell not in MATRIX_CELLS else status), measured


def summarize(config, evidence):
    rows = evidence.get("rows", [])
    campaign = evidence.get("campaign", {})
    stages = campaign.get("stages", [])
    planned = {stage["name"]: stage for stage in config["stages"]}
    stage_summary = []
    for stage in stages:
        expected = planned.get(stage.get("name"))
        if expected is None:
            continue
        reached = (stage.get("achieved_sessions", 0) >= expected["desired_sessions"]
                   and stage.get("actual_dwell_seconds", 0) >= expected["min_dwell_seconds"]
                   and stage.get("status") == "pass")
        stage_summary.append({"api": stage["name"], "configured_sessions": expected["desired_sessions"],
                              "actual_sessions": stage.get("achieved_sessions", 0),
                              "actual_dwell_seconds": stage.get("actual_dwell_seconds", 0),
                              "status": "pass" if reached else stage.get("status", "not-run")})
    for name, expected in planned.items():
        if not any(stage["api"] == name for stage in stage_summary):
            stage_summary.append({"api": name, "configured_sessions": expected["desired_sessions"],
                                  "actual_sessions": 0, "actual_dwell_seconds": 0,
                                  "status": "not-run"})
    completed = sum(row.get("success") is True and row.get("complete") is True for row in rows)
    total = max(len(rows), campaign.get("physical_attempts", 0))
    complete_rate = completed / total if total else None
    ttft = [row["ttft_ms"] for row in rows if row.get("success") and row.get("ttft_ms") is not None]
    http = [row["duration_ms"] for row in rows if row.get("success") and row.get("duration_ms") is not None]
    terminal = [row["terminal_to_eof_ms"] for row in rows
                if row.get("success") and row.get("terminal_to_eof_ms") is not None]
    latency = {"ttft_ms": percentile(ttft, 0.95), "http_ms": percentile(http, 0.95),
               "terminal_ms": percentile(terminal, 0.95)}
    slos_met = all(latency[key] is not None and latency[key] <= config["slos"][key]
                   for key in latency)
    developers = {row["identity_id"] for row in rows
                  if row.get("identity_kind") == "developer" and row.get("identity_id")}
    agents = {row["identity_id"] for row in rows
              if row.get("identity_kind") == "agent" and row.get("identity_id")}
    matrix = {cell: "not-run" for cell in MATRIX_CELLS}
    blocked = {"opus_ramp", "astra_ramp", "release_upgrade", "flow_callback",
               "cli_artifact_recovery", "http_pool_2_plus_1"}
    matrix.update({cell: "blocked" for cell in blocked})
    matrix_results = evidence.get("matrix_results", {})
    if not isinstance(matrix_results, dict):
        matrix_results = {}
    executed = config.get("plan") in {"model_ramp", "comparison"}
    if not executed:
        for cell, result in matrix_results.items():
            if cell in matrix:
                matrix[cell] = qualify_cell(cell, result, config)
    measured_cell = None
    if executed:
        cell = config.get("matrix_cell", config.get("comparison_cell"))
        cell_status, measured_cell = observed_cell(config, evidence, matrix_results.get(cell))
        if cell in matrix:
            matrix[cell] = cell_status
    mandatory_pass = all(stage["status"] == "pass" for stage in stage_summary)
    revision_matches = evidence.get("revision") == config["revision"]
    cleanup = evidence.get("cleanup", {"status": "not-run", "accounting": "not-run"})
    checkpoint_status = ("fail" if rows and (not mandatory_pass or not revision_matches or
                                             completed != total) else
                         "blocked" if not config.get("local_test") and rows else "not-run")
    if not rows or executed:
        checkpoint_status = "not-run"
    status = ("fail" if checkpoint_status == "fail" or campaign.get("stop_reason")
              or "fail" in matrix.values() or executed and cell_status == "fail" else "blocked")
    if cleanup.get("status") == "hold" or cleanup.get("accounting") == "unknown":
        status = "fail"
    sources = ("campaign_controller.py", "campaign_guard.py", "campaign_recovery.py",
               "campaign_report.py", "locustfile.py")
    source_hash = hashlib.sha256(b"".join((Path(__file__).parent / source).read_bytes()
                                         for source in sources)).hexdigest()
    config_hash = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    summary = {
        "schema_version": 1, "source_hash": source_hash, "config_hash": config_hash,
        "tested_revision": evidence.get("revision"), "revision_matches": revision_matches,
        "scenario": config["scenario"], "status": status,
        "attempts": total, "complete_streams": completed, "complete_rate": complete_rate,
        "errors": total - completed,
        "cancellations": sum(bool(row.get("cancelled_by_runner")) for row in rows),
        "unknown_attempts": campaign.get("outstanding", 0),
        "peak_inflight": campaign.get("peak_inflight"),
        "time_weighted_inflight": campaign.get("time_weighted_inflight"),
        "configured_developers": sum(identity["kind"] == "developer" for identity in config["identities"]),
        "configured_agents": sum(identity["kind"] == "agent" for identity in config["identities"]),
        "observed_developers": len(developers), "observed_agents": len(agents),
        "observed_identity_coverage": bool(rows) and all(row.get("identity_id") and
                                                   row.get("identity_kind") in {"developer", "agent"}
                                                   for row in rows),
        "stages": stage_summary, "p95": latency,
        "latency_samples": {"ttft_ms": len(ttft), "http_ms": len(http),
                            "terminal_ms": len(terminal)},
        "predeclared_slos_met": slos_met,
        "matrix": matrix, "measured_cell": measured_cell,
        "measured_cell_status": cell_status if executed else "not-run",
        "stop_reason": campaign.get("stop_reason"),
        "cleanup": cleanup, "acceptance": {"AC-01": checkpoint_status,
                                           "AC-02": "fail" if campaign.get("stop_reason") else "not-run",
                                           "AC-03": "fail" if cleanup.get("status") == "hold" else "blocked",
                                           "AC-04": "blocked"},
    }
    return summary


def sanitized_markdown(summary):
    stages = "\n".join(
        f"- {stage['api']}: {stage['status']}; configured {stage['configured_sessions']}, "
        f"observed {stage['actual_sessions']}, dwell {stage['actual_dwell_seconds']:.2f}s"
        for stage in summary["stages"]
    )
    cells = "\n".join(f"- {cell}: {status}" for cell, status in summary["matrix"].items())
    acceptance = "\n".join(f"- {key}: {value}" for key, value in summary["acceptance"].items())
    rate = ("not-run" if summary["complete_rate"] is None
            else f"{summary['complete_rate']:.1%} ({summary['complete_streams']}/{summary['attempts']})")
    measured = summary.get("measured_cell")
    measured_line = ""
    if measured:
        widths = sorted(len(agents) for agents in measured.get("fan_out_distribution", {}).values())
        measured_line = (
            f"Measured cell: {summary['measured_cell_status']}; "
            f"configured/live sessions {(measured.get('plateaus') or [{}])[0].get('configured_sessions', 'unknown')}/"
            f"{(measured.get('plateaus') or [{}])[0].get('actual_sessions', 'unknown')}; "
            f"successful developer/agent identities {measured.get('observed_developers', 'unknown')}/"
            f"{measured.get('observed_agents', 'unknown')}; tenants "
            f"{measured.get('observed_tenants', 'unknown')}; human roots "
            f"{measured.get('observed_human_roots', 'unknown')}; "
            f"agents per rooted chain {widths}; provider cache samples "
            f"{measured.get('provider_cache_samples', 'unknown')}. "
            "Evidence status is not live acceptance.\n"
        )
    return (
        "# Gateway campaign summary (sanitized)\n\n"
        f"Status: {summary['status']}. No live capacity is certified by this report.\n"
        f"Complete streams: {rate}; errors {summary['errors']}, cancellations "
        f"{summary['cancellations']}, unknown {summary['unknown_attempts']}. "
        f"Observed p95 within predeclared limits: {summary['predeclared_slos_met']} "
        f"(sample counts: {summary['latency_samples']}). Short samples cannot certify reliability.\n"
        f"Configured developer/agent identities: {summary['configured_developers']}/"
        f"{summary['configured_agents']}; observed: {summary['observed_developers']}/"
        f"{summary['observed_agents']}; attribution complete: "
        f"{summary['observed_identity_coverage']}.\n"
        f"Peak simultaneous inference: {summary['peak_inflight']}; "
        f"time-weighted inference: {summary['time_weighted_inflight']}.\n\n"
        f"## Checkpoint stages\n{stages}\n\n{measured_line}"
        f"## Representative matrix\n{cells}\n\n"
        f"## Acceptance\n{acceptance}\n"
    )


def write_private(path, text):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
    with temporary.open("x") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(text)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
