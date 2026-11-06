"""Finite, explicitly configured gateway checkpoint; no ambient credentials."""

import argparse
import hashlib
import json
import math
import os
import stat
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from campaign_guard import CampaignGuard, NoRedirects, running_permits
from campaign_recovery import RecoveryError


class ConfigError(ValueError):
    pass


def positive_number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ConfigError(f"{name} must be a positive number")
    return value


def private_path(value, name, directory=False):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ConfigError(f"{name} must be an absolute private path")
    path = Path(value)
    try:
        mode = path.stat().st_mode
        if path.is_dir() != directory or mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ConfigError(f"{name} must exist with private permissions")
    except OSError as error:
        raise ConfigError(f"{name} must exist with private permissions") from error
    return path


def verify_capacity_receipt(config, receipt_path, ceiling):
    try:
        receipt = json.loads(private_path(receipt_path, "qualified_capacity_receipt").read_text())
    except (OSError, ValueError, TypeError) as error:
        raise ConfigError("invalid private qualified capacity receipt") from error
    health = receipt.get("health") if isinstance(receipt, dict) else None
    sessions = receipt.get("safe_inflight_sessions") if isinstance(receipt, dict) else None
    if (not isinstance(receipt, dict) or receipt.get("revision") != config["revision"]
            or receipt.get("target") != config["target"]
            or receipt.get("approved_account") != config["approved_account"]
            or receipt.get("approved_role") != config["approved_role"]
            or receipt.get("status") != "pass"
            or not isinstance(receipt.get("protected_receipt"), str)
            or not receipt["protected_receipt"].strip()
            or isinstance(sessions, bool) or not isinstance(sessions, int)
            or not ceiling <= sessions <= 200
            or not isinstance(health, dict) or health.get("oom") is not False
            or isinstance(health.get("restarts"), bool) or health.get("restarts") != 0
            or health.get("dependencies_ok") is not True):
        raise ConfigError("qualified capacity receipt does not match the safe target and ceiling")


def load_config(path, for_report=False):
    private_path(str(path), "config")
    try:
        config = json.loads(Path(path).read_text())
    except (OSError, ValueError) as error:
        raise ConfigError("config must contain valid JSON") from error
    if not isinstance(config, dict) or config.get("schema_version") != 1:
        raise ConfigError("unsupported campaign schema_version")
    config.pop("_native_recovery", None)
    for field in ("scenario", "revision", "approved_account", "approved_role", "recovery_owner"):
        if not isinstance(config.get(field), str) or not config[field].strip():
            raise ConfigError(f"missing {field}")
    if not isinstance(config.get("seed"), int) or isinstance(config["seed"], bool):
        raise ConfigError("seed must be an integer")
    target = config.get("target")
    parsed = urllib.parse.urlsplit(target) if isinstance(target, str) else None
    if not parsed or parsed.scheme not in {"https", "http"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigError("target must be an explicit URL without credentials")
    if config.get("local_test") and parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ConfigError("local_test is restricted to loopback targets")
    if parsed.scheme == "http" and (not config.get("local_test") or parsed.hostname not in {"127.0.0.1", "localhost"}):
        raise ConfigError("non-local targets require HTTPS")
    if not config.get("local_test"):
        from campaign_recovery import load_recovery_config
        for field in ("campaign", "campaign_owner", "journal_dir", "recovery_config"):
            if not isinstance(config.get(field), str) or not config[field].strip():
                raise ConfigError(f"missing {field}")
        private_path(config["journal_dir"], "journal_dir", directory=True)
        private_path(config["recovery_config"], "recovery_config")
        recovery = load_recovery_config(config["recovery_config"])
        expected = {"campaign": config["campaign"], "owner": config["campaign_owner"],
                    "journal_dir": config["journal_dir"], "target": config["target"],
                    "revision": config["revision"], "approved_account": config["approved_account"],
                    "approved_role": config["approved_role"]}
        if any(recovery.get(field) != value for field, value in expected.items()):
            raise ConfigError("recovery configuration does not match campaign bindings")
        if recovery.get("backend") == "adp-native":
            config["_native_recovery"] = recovery
    private_path(config.get("evidence_dir"), "evidence_dir", directory=True)
    if not isinstance(config.get("identities"), list) or not config["identities"]:
        raise ConfigError("identities must bind owned test callers")
    seen = set()
    for identity in config["identities"]:
        if not isinstance(identity, dict):
            raise ConfigError("identity must be an object")
        for field in ("id", "kind", "tenant", "root_human", "ownership_receipt"):
            if not isinstance(identity.get(field), str) or not identity[field].strip():
                raise ConfigError(f"identity missing {field}")
        if identity["kind"] not in {"developer", "agent"} or identity["id"] in seen:
            raise ConfigError("identities require unique IDs and developer/agent kind")
        seen.add(identity["id"])
        if not config.get("local_test"):
            if recovery.get("backend") == "adp-native":
                from campaign_native import bound_fixture
                from campaign_adp import private_file
                fixture = bound_fixture(recovery, identity)
                if not for_report:
                    private_file(fixture["run_credential_file"])
                    private_file(fixture["workload_token_file"])
                    if Path(identity["token_file"]).exists():
                        raise ConfigError("native credential cache must be absent before setup")
                continue
            if identity["kind"] != "developer":
                raise ConfigError("ADP Cognito-user fixtures cannot impersonate workload agents")
            fixtures = recovery["fixtures"]
            owned = next((item for item in fixtures if item["kind"] == "identity"
                          and item["binding"] == identity["id"]), None)
            credential = next((item for item in fixtures if item["kind"] == "secret"
                               and item.get("identity") == identity["id"]
                               and item["binding"] == identity.get("token_file")), None)
            quota = next((item for item in fixtures if item["kind"] == "quota"
                         and item["binding"] == identity["id"]), None)
            if (not owned or not credential or not quota
                    or any(owned[field] != identity[field] for field in ("tenant", "root_human", "ownership_receipt"))):
                raise ConfigError("caller does not match the owned fixture inventory")
            if not for_report and Path(identity["token_file"]).exists():
                raise ConfigError("campaign token cache must be absent before setup")
        else:
            private_path(identity.get("token_file"), "token_file")
    if config.get("_native_recovery"):
        if seen != {fixture["binding"] for fixture in recovery["fixtures"]}:
            raise ConfigError("native campaign must explicitly exercise every inventoried workload")
        if config.get("plan", "checkpoint") != "comparison" and len(seen) != 1:
            raise ConfigError("native checkpoints and ramps require exactly one workload")
        if config.get("comparison_cell") == "fan_out":
            roots = [fixture for fixture in recovery["fixtures"] if fixture.get("parent") is None]
            if len(roots) != 1 or len(recovery["fixtures"]) < 2:
                raise ConfigError("native fan-out requires protected descendants of one inventoried root")
    models = config.get("models")
    if not isinstance(models, list) or {entry.get("api") for entry in models if isinstance(entry, dict)} != {"chat", "responses"} or len(models) != 2:
        raise ConfigError("checkpoint requires one chat and one responses model binding")
    for entry in models:
        for field in ("model", "sdk_version"):
            if not isinstance(entry.get(field), str) or not entry[field].strip():
                raise ConfigError(f"model binding missing {field}")
        positive_number(entry.get("max_output_tokens"), "max_output_tokens")
        positive_number(entry.get("max_input_tokens"), "max_input_tokens")
        positive_number(entry.get("price_per_input_token"), "price_per_input_token")
        positive_number(entry.get("price_per_output_token"), "price_per_output_token")
    limits = config.get("limits")
    if not isinstance(limits, dict):
        raise ConfigError("limits are required")
    ramp = config.get("plan", "checkpoint") == "model_ramp"
    comparison = config.get("plan") == "comparison"
    if config.get("plan", "checkpoint") not in {"checkpoint", "model_ramp", "comparison"}:
        raise ConfigError("unknown campaign plan")
    if (ramp or comparison) and config.get("ramp_api") not in {"chat", "responses"}:
        raise ConfigError("session campaign requires an explicit ramp_api")
    for field, ceiling in (("attempts", 12000 if ramp else 1200 if comparison else 40),
                           ("active_seconds", 1440 if ramp else 600 if comparison else 180),
                           ("drain_seconds", 240 if ramp or comparison else 120)):
        value = positive_number(limits.get(field), field)
        if value > ceiling:
            raise ConfigError(f"campaign {field} exceeds finite ceiling")
    for field in ("input_tokens", "output_tokens", "max_spend", "setup_seconds", "cleanup_seconds"):
        positive_number(limits.get(field), field)
    if not ramp and not comparison and limits["attempts"] < len(models):
        raise ConfigError("attempt limit cannot cover both APIs")
    slos = config.get("slos")
    if not isinstance(slos, dict):
        raise ConfigError("predeclared SLOs are required")
    for field in ("ttft_ms", "http_ms", "terminal_ms"):
        positive_number(slos.get(field), field)
    quote = sum(
        entry["max_input_tokens"] * entry["price_per_input_token"]
        + entry["max_output_tokens"] * entry["price_per_output_token"]
        for entry in models
    )
    if (sum(entry["max_input_tokens"] for entry in models) > limits["input_tokens"]
            or sum(entry["max_output_tokens"] for entry in models) > limits["output_tokens"]
            or quote > limits["max_spend"]):
        raise ConfigError("campaign reservations exceed declared token or spend caps")
    stages = config.get("stages")
    if ramp:
        if config.get("matrix_cell") not in {"opus_ramp", "astra_ramp"}:
            raise ConfigError("model ramp requires an explicit matrix_cell")
        expected = [(10, 60), (60, 120), (100, 120), (150, 120), (200, 300)]
        if (not isinstance(stages, list) or len(stages) != 5 or any(
                not isinstance(stage, dict) or stage.get("name") != f"ramp-{sessions}"
                or stage.get("desired_sessions") != sessions or stage.get("min_dwell_seconds") != dwell
                or stage.get("required") is not True or stage.get("ramp") is not True
                for stage, (sessions, dwell) in zip(stages, expected))):
            raise ConfigError("model ramp requires all five mandatory plateaus and exact dwell")
        positive_number(config.get("think_time_seconds"), "think_time_seconds")
        if (sum(stage["desired_sessions"] / positive_number(stage.get("spawn_rate"), "spawn_rate")
                for stage in stages) > 120
                or sum(dwell for _, dwell in expected) + 120 > limits["active_seconds"]):
            raise ConfigError("model ramp cannot fit transition and dwell within active cap")
    elif comparison:
        cell = config.get("comparison_cell")
        allowed = {"shared_identity", "developer_only", "distinct_developers",
                   "distinct_tenants", "distinct_agents", "fan_out", "cached_context", "unique_context"}
        if not isinstance(cell, str) or cell not in allowed:
            raise ConfigError("comparison_cell must select a supported comparison")
        positive_number(config.get("think_time_seconds"), "think_time_seconds")
        ceiling = config.get("safe_inflight_sessions")
        if isinstance(ceiling, bool) or not isinstance(ceiling, int) or not 0 < ceiling <= 200:
            raise ConfigError("safe_inflight_sessions must be a declared positive integer at most 200")
        receipt = config.get("qualified_capacity_receipt")
        if not isinstance(receipt, str) or not receipt.strip():
            raise ConfigError("qualified_capacity_receipt is required")
        if not config.get("local_test"):
            verify_capacity_receipt(config, receipt, ceiling)
        if cell in {"cached_context", "unique_context"}:
            context = private_path(config.get("context_file"), "context_file").read_text().strip()
            if (len(context.split()) < 1024 or
                    next(model for model in models if model["api"] == config["ramp_api"])["max_input_tokens"]
                    < len(context.encode()) + 128):
                raise ConfigError("cache context requires at least 1024 words and reserved input headroom")
            if limits["attempts"] < 2:
                raise ConfigError("cache comparison requires at least two physical attempts")
        if not isinstance(stages, list) or len(stages) != 1 or not isinstance(stages[0], dict):
            raise ConfigError("comparison requires exactly one stage")
        stage = stages[0]
        sessions = stage.get("desired_sessions")
        if (stage.get("name") != cell or stage.get("ramp") is not True
                or stage.get("required") is not True or isinstance(sessions, bool)
                or not isinstance(sessions, int) or not len(config["identities"]) <= sessions <= ceiling):
            raise ConfigError("comparison stage must exercise every owned identity within the safe ceiling")
        dwell = positive_number(stage.get("min_dwell_seconds"), "min_dwell_seconds")
        rate = positive_number(stage.get("spawn_rate"), "spawn_rate")
        if dwell + sessions / rate >= limits["active_seconds"] or sessions / rate > 120:
            raise ConfigError("comparison schedule cannot fit declared active and transition bounds")
        identities = config["identities"]
        developers = all(identity["kind"] == "developer" for identity in identities)
        agents = all(identity["kind"] == "agent" for identity in identities)
        tenants = {identity["tenant"] for identity in identities}
        roots = {identity["root_human"] for identity in identities}
        if ((cell in {"shared_identity", "cached_context", "unique_context"}
             and (not developers or len(identities) != 1))
                or (cell == "developer_only" and not developers)
                or (cell == "distinct_developers" and (not developers or len(identities) < 2
                                                       or len(tenants) != 1 or len(roots) < 2))
                or (cell == "distinct_tenants" and (not developers or len(tenants) < 2
                                                    or len(roots) < 2))
                or (cell in {"distinct_agents", "fan_out"} and (not agents or len(identities) < 2))
                or (cell == "fan_out" and len(roots) != 1)):
            raise ConfigError("comparison requires owned identities with the declared relationship")
    else:
        if not isinstance(stages, list) or len(stages) != 2:
            raise ConfigError("checkpoint requires both mandatory API stages")
        if [stage.get("name") for stage in stages if isinstance(stage, dict)] != [entry["api"] for entry in models]:
            raise ConfigError("checkpoint stages must match both API bindings in order")
        for stage in stages:
            if (stage.get("required") is not True or stage.get("desired_sessions") != 1
                    or stage.get("min_dwell_seconds") != 0):
                raise ConfigError("checkpoint stages must require one session and zero dwell")
            positive_number(stage.get("spawn_rate"), "spawn_rate")
    return config


def stream_request(config, guard, identity, entry, token, started, prompt="Count from 1 to 3.", dispatch=None):
    api = entry["api"]
    payload = {"model": entry["model"], "stream": True}
    if api == "chat":
        payload.update(max_tokens=entry["max_output_tokens"], messages=[{"role": "user", "content": prompt}])
        route = "/v1/chat/completions"
    else:
        payload.update(max_output_tokens=entry["max_output_tokens"], input=prompt)
        route = "/openai/v1/responses"
    request = urllib.request.Request(
        config["target"].rstrip("/") + route,
        data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    row = {"api": api, "model": entry["model"], "success": False, "complete": False,
           "identity_id": identity["id"], "identity_kind": identity["kind"]}
    if config.get("plan") == "comparison":
        row.update(tenant=identity["tenant"], root_human=identity["root_human"],
                   prompt_digest=hashlib.sha256(prompt.encode()).hexdigest())
    began = time.monotonic()
    content = False
    stream_error = False
    terminal_at = None
    try:
        if config.get("_native_recovery"):
            from campaign_native import model_request
            native = config["_native_recovery"]
            request = model_request(native, identity, route, payload, json.loads(token),
                                    min(config["limits"]["drain_seconds"],
                                    max(0.01, config["limits"]["active_seconds"] - (time.monotonic() - started))))
            if guard.stop_reason or time.monotonic() - started >= config["limits"]["active_seconds"]:
                raise RecoveryError("campaign stopped before native dispatch")
            if dispatch is None or not dispatch():
                raise RecoveryError("native dispatch permit expired or was refused")
        with urllib.request.build_opener(NoRedirects, urllib.request.ProxyHandler({})).open(
            request, timeout=min(1 if config.get("plan") not in {"model_ramp", "comparison"} else config["slos"]["http_ms"] / 1000,
                        config["limits"]["drain_seconds"],
                        max(0.01, config["limits"]["active_seconds"] - (time.monotonic() - started)))
        ) as response:
            row["status"] = response.status
            row["request_id"] = response.headers.get("X-Request-ID")
            for raw in response:
                if guard.stop_reason or time.monotonic() - started >= config["limits"]["active_seconds"]:
                    row["error"] = "aborted" if guard.stop_reason else "active_deadline"
                    break
                line = raw.decode("utf-8").strip()
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    if api == "chat":
                        row["complete"] = True
                        terminal_at = time.monotonic()
                    continue
                item = json.loads(data)
                if not isinstance(item, dict):
                    raise ValueError("invalid stream event")
                response_item = item.get("response", {})
                if not isinstance(response_item, dict):
                    raise ValueError("invalid stream response")
                usage = item.get("usage") or response_item.get("usage")
                if isinstance(usage, dict):
                    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details")
                    if isinstance(details, dict):
                        cached = details.get("cached_tokens")
                        if isinstance(cached, int) and not isinstance(cached, bool) and cached >= 0:
                            row["cached_input_tokens"] = cached
                had_content = content
                if api == "chat":
                    choices = item.get("choices", [])
                    if not isinstance(choices, list) or any(
                            not isinstance(choice, dict) or not isinstance(choice.get("delta", {}), dict)
                            for choice in choices):
                        raise ValueError("invalid chat stream choices")
                    content |= any(choice.get("delta", {}).get("content") for choice in choices)
                else:
                    content |= item.get("type") == "response.output_text.delta" and bool(item.get("delta"))
                    if item.get("type") == "response.completed":
                        row["complete"] = response_item.get("status") == "completed"
                        if row["complete"]:
                            terminal_at = time.monotonic()
                    stream_error |= item.get("type") in {"error", "response.failed", "response.incomplete"}
                stream_error |= bool(item.get("error"))
                if content and not had_content:
                    row["ttft_ms"] = (time.monotonic() - began) * 1000
            if terminal_at is not None:
                row["terminal_to_eof_ms"] = (time.monotonic() - terminal_at) * 1000
            row["success"] = bool(content and row["complete"] and not stream_error and "error" not in row)
    except (urllib.error.URLError, OSError, ValueError, RecoveryError) as error:
        row["error"] = type(error).__name__
    row["duration_ms"] = (time.monotonic() - began) * 1000
    return row


def setup_fixtures(config):
    from campaign_recovery import RecoveryJournal, fixture_backend, load_recovery_config

    if config.get("local_test") and not config.get("recovery_config"):
        return None
    recovery = load_recovery_config(config["recovery_config"])
    journal = RecoveryJournal(config["journal_dir"], config["campaign"], config["campaign_owner"])
    journal.bind_recovery(recovery, initialize=True)
    backend = fixture_backend(recovery)
    deadline = time.time() + config["limits"]["setup_seconds"]
    with journal._locked() as record:
        journal._require_owner(record)
        if record.get("controller_started") or record["intents"] or record["attempts"]:
            raise RecoveryError("campaign already started; use independent cleanup, not a rerun")
        record["controller_started"] = True
        journal._save(record)
    try:
        if hasattr(backend, "setup_native"):
            backend.deadline = deadline
            backend.setup_native(journal)
            journal.heartbeat()
            return journal
        order = {"identity": 0, "quota": 1, "secret": 2}
        for fixture in sorted(recovery["fixtures"], key=lambda item: order[item["kind"]]):
            journal.heartbeat()
            backend.deadline = min(deadline, time.time() + journal.lease_seconds / 2)
            journal.mutate(backend, fixture["kind"], fixture["binding"], fixture["desired"],
                           fixture["ownership_receipt"])
        journal.heartbeat()
    except (OSError, ValueError, RecoveryError):
        backend.deadline = time.time() + recovery["cleanup_seconds"]
        journal.recover(backend, recovery["cleanup_seconds"], force=True)
        raise RecoveryError("fixture setup failed; inspect the protected cleanup journal") from None
    return journal


def checkpoint(config, guard=None):
    """Send one physical request per API and keep all evidence private."""
    identity = config["identities"][0]
    journal = setup_fixtures(config)
    token = private_path(identity["token_file"], "token_file").read_text().strip()
    if not token:
        raise ConfigError("token_file is empty")
    evidence = private_path(config["evidence_dir"], "evidence_dir", directory=True)
    guard = guard if guard is not None else CampaignGuard(config["limits"], config["stages"])
    started = time.monotonic()
    rows = []
    with running_permits(guard, journal) as worker:
        for entry in config["models"]:
            api = entry["api"]
            if not guard.start_stage(api, achieved_sessions=1):
                break
            price = (entry["max_input_tokens"] * entry["price_per_input_token"]
                     + entry["max_output_tokens"] * entry["price_per_output_token"])
            attempt_id = worker.permit(entry["max_input_tokens"], entry["max_output_tokens"], price)
            if not attempt_id:
                if worker.lost:
                    guard.signal("lease_lost")
                guard.end_stage()
                break
            if not config.get("_native_recovery") and not worker.dispatch(attempt_id):
                if not worker.finish(attempt_id, {"success": False, "complete": False}):
                    guard.signal("lease_lost")
                guard.end_stage()
                break
            native_dispatch = {"dispatch": lambda: worker.dispatch(attempt_id)} if config.get("_native_recovery") else {}
            row = stream_request(config, guard, identity, entry, token, started, **native_dispatch)
            rows.append(row)
            if not worker.finish(attempt_id, row):
                guard.signal("lease_lost")
            if not guard.end_stage():
                break
    return finish_campaign(config, guard, journal, rows, evidence)


def finish_campaign(config, guard, journal, rows, evidence, comparison=None, outcome=None):
    outcome = outcome if outcome is not None else guard.report()
    cleanup = {"status": "not-run", "accounting": "not-run"}
    if journal is not None:
        from campaign_recovery import fixture_backend, load_recovery_config
        try:
            recovery = load_recovery_config(config["recovery_config"])
            journal.bind_recovery(recovery)
            backend = fixture_backend(recovery)
            backend.deadline = time.time() + recovery["cleanup_seconds"]
            cleanup = journal.recover(backend, recovery["cleanup_seconds"], force=True)
        except (OSError, ValueError, RecoveryError):
            cleanup = {"status": "hold", "accounting": "unknown"}
    output = evidence / "checkpoint-results.json"
    with output.open("x") as result:
        os.fchmod(result.fileno(), 0o600)
        json.dump({"revision": config["revision"], "scenario": config["scenario"],
                   "config_hash": hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest(),
                   "rows": rows, "stage_reached": sum(stage["status"] == "pass" for stage in outcome["stages"]),
                   "success": (outcome["status"] == "pass" and cleanup["status"] in {"clean", "not-run"}
                               and cleanup["accounting"] in {"observed", "not-run"}),
                   "campaign": outcome, "cleanup": cleanup,
                   **({"comparison": comparison} if comparison is not None else {})}, result)
    if cleanup["status"] == "hold" or cleanup["accounting"] == "unknown":
        return 5
    return 0 if outcome["status"] == "pass" else (4 if outcome["stop_reason"] in {
        "active_deadline", "attempt_budget", "input_budget", "output_budget", "spend_budget", "incomplete_stage", "transition_deadline"
    } else 3)


def comparison_metrics(config, rows, campaign=None):
    successful = [row for row in rows if row.get("success") and row.get("complete")]
    roots = {}
    for row in successful:
        if row["identity_kind"] == "agent":
            agents = roots.setdefault(row["root_human"], {})
            agents[row["identity_id"]] = agents.get(row["identity_id"], 0) + 1
    cached = [row["cached_input_tokens"] for row in successful if "cached_input_tokens" in row]
    return {"cell": config["comparison_cell"], "observed_identity_ids": sorted({
        row["identity_id"] for row in successful}),
        "observed_developers": len({row["identity_id"] for row in successful
                                    if row["identity_kind"] == "developer"}),
        "observed_agents": len({row["identity_id"] for row in successful
                                if row["identity_kind"] == "agent"}),
        "observed_tenants": len({row["tenant"] for row in successful}),
        "observed_human_roots": len({row["root_human"] for row in successful}),
        "fan_out_distribution": roots,
        "configured_sessions": config["stages"][0]["desired_sessions"],
        "live_sessions": (campaign["stages"][0].get("achieved_sessions", 0) if campaign else 0),
        "peak_inflight": campaign["peak_inflight"] if campaign else 0,
        "time_weighted_inflight": campaign["time_weighted_inflight"] if campaign else 0,
        "physical_attempts": campaign["physical_attempts"] if campaign else len(rows),
        "prompt_variants": len({row["prompt_digest"] for row in successful}),
        "provider_cache_samples": len(cached), "provider_cached_input_tokens": sum(cached),
        "complete_streams": len(successful)}


def comparison_measured(config, metrics):
    cell = config["comparison_cell"]
    if len(metrics["observed_identity_ids"]) != len(config["identities"]):
        return False
    if cell in {"cached_context", "unique_context"}:
        if metrics["provider_cache_samples"] < 2:
            return False
        if cell == "cached_context":
            return metrics["prompt_variants"] == 1 and metrics["provider_cached_input_tokens"] > 0
        return metrics["prompt_variants"] >= 2
    if cell == "fan_out":
        return metrics["observed_agents"] >= 2 and any(
            len(agents) >= 2 for agents in metrics["fan_out_distribution"].values())
    return True


def model_ramp(config, guard=None):
    """Maintain bounded live sessions for a model ramp or one comparison cell."""
    comparison = config.get("plan") == "comparison"
    entry = next(model for model in config["models"] if model["api"] == config["ramp_api"])
    identities = config["identities"] if comparison else config["identities"][:1]
    journal = setup_fixtures(config)
    tokens = [private_path(identity["token_file"], "token_file").read_text().strip()
              for identity in identities]
    if not all(tokens):
        raise ConfigError("token_file is empty")
    context = (private_path(config["context_file"], "context_file").read_text().strip()
               if comparison and config["comparison_cell"] in {"cached_context", "unique_context"} else None)
    evidence = private_path(config["evidence_dir"], "evidence_dir", directory=True)
    guard = guard if guard is not None else CampaignGuard(config["limits"], config["stages"])
    started = time.monotonic()
    rows = []
    transition_seconds = 0
    drain_seconds = 0
    price = (entry["max_input_tokens"] * entry["price_per_input_token"]
             + entry["max_output_tokens"] * entry["price_per_output_token"])
    with running_permits(guard, journal) as worker:
        for stage in config["stages"]:
            if not guard.start_stage(stage["name"], 0):
                break
            launch_started = time.monotonic()
            stop = threading.Event()
            workers = []

            def session(started_event, identity, token, session_index):
                sequence = 0
                started_event.set()
                while not stop.is_set() and guard.can_dispatch():
                    attempt_id = worker.permit(entry["max_input_tokens"], entry["max_output_tokens"], price)
                    if not attempt_id:
                        if worker.lost:
                            guard.signal("lease_lost")
                        break
                    if not config.get("_native_recovery") and not worker.dispatch(attempt_id):
                        if not worker.finish(attempt_id, {"success": False, "complete": False}):
                            guard.signal("lease_lost")
                        break
                    sequence += 1
                    prompt = "Count from 1 to 3."
                    if comparison and config["comparison_cell"] == "unique_context":
                        prompt = f"Context {config['seed']}-{session_index}-{sequence}. " + prompt
                    if context is not None:
                        prompt += " " + context
                    native_dispatch = {"dispatch": lambda: worker.dispatch(attempt_id)} if config.get("_native_recovery") else {}
                    row = stream_request(config, guard, identity, entry, token, started, prompt, **native_dispatch)
                    row["stage"] = stage["name"]
                    row["session_index"] = session_index
                    rows.append(row)
                    if not worker.finish(attempt_id, row):
                        guard.signal("lease_lost")
                        break
                    stop.wait(config["think_time_seconds"])

            try:
                for index in range(stage["desired_sessions"]):
                    if guard.stop_reason or transition_seconds + time.monotonic() - launch_started > 120:
                        break
                    if time.monotonic() - started >= config["limits"]["active_seconds"]:
                        guard.can_dispatch()
                        break
                    ready = threading.Event()
                    identity_index = index % len(identities)
                    thread = threading.Thread(target=session,
                                              args=(ready, identities[identity_index], tokens[identity_index], index),
                                              daemon=True)
                    thread.start()
                    workers.append(thread)
                    if not ready.wait(timeout=min(1, config["limits"]["drain_seconds"])):
                        guard.signal("lease_lost")
                        break
                    guard.reach_stage(len(workers))
                    if index + 1 < stage["desired_sessions"]:
                        stop.wait(1 / stage["spawn_rate"])
                transition_seconds += time.monotonic() - launch_started
                if transition_seconds > 120 and not guard.stop_reason:
                    guard.signal("transition_deadline")
                reached_at = guard.current["reached_at"]
                while reached_at is not None and not guard.stop_reason and not stop.is_set():
                    if any(not thread.is_alive() for thread in workers):
                        guard.signal("lease_lost")
                        break
                    remaining = min(reached_at + stage["min_dwell_seconds"] - time.monotonic(),
                                    started + config["limits"]["active_seconds"] - time.monotonic())
                    if remaining <= 0:
                        guard.can_dispatch()
                        break
                    stop.wait(min(remaining, 0.05))
            except KeyboardInterrupt:
                guard.signal("operator_abort")
            finally:
                stop.set()
                drain_started = time.monotonic()
                drain_until = drain_started + max(0, config["limits"]["drain_seconds"] - drain_seconds)
                for thread in workers:
                    thread.join(timeout=max(0, drain_until - time.monotonic()))
                drain_seconds += time.monotonic() - drain_started
                if any(thread.is_alive() for thread in workers):
                    guard.signal("lease_lost")
            metrics = comparison_metrics(config, rows) if comparison else None
            if comparison and not guard.stop_reason and not comparison_measured(config, metrics):
                guard.signal("telemetry_missing")
            if not guard.end_stage():
                break
    outcome = guard.report()
    outcome["drain_seconds"] = drain_seconds
    return finish_campaign(config, guard, journal, rows, evidence,
                           comparison_metrics(config, rows, outcome) if comparison else None,
                           outcome)


def report_campaign(config):
    from campaign_report import sanitized_markdown, summarize, write_private

    evidence_dir = private_path(config["evidence_dir"], "evidence_dir", directory=True)
    checkpoint_file = evidence_dir / "checkpoint-results.json"
    matrix_file = evidence_dir / "matrix-evidence.json"
    evidence = {}
    for source, field in ((checkpoint_file, None), (matrix_file, "matrix_results")):
        if source.exists():
            private_path(str(source), source.name)
            try:
                content = json.loads(source.read_text())
            except (OSError, ValueError) as error:
                raise ConfigError("invalid private campaign evidence") from error
            if not isinstance(content, dict):
                raise ConfigError("private campaign evidence must be an object")
            if field is None:
                evidence.update(content)
            else:
                evidence[field] = content
    try:
        summary = summarize(config, evidence)
    except (TypeError, KeyError, ZeroDivisionError, IndexError) as error:
        raise ConfigError("invalid private campaign evidence") from error
    write_private(evidence_dir / "campaign-report.json", json.dumps(summary, indent=2) + "\n")
    write_private(evidence_dir / "sanitized-report.md", sanitized_markdown(summary))
    print(f"Campaign report: {summary['status']}; output is in the private evidence directory")
    if summary["cleanup"].get("status") == "hold" or summary["cleanup"].get("accounting") == "unknown":
        return 5
    return 3 if summary["status"] == "fail" else 4


def main(argv=None):
    parser = argparse.ArgumentParser(description="Finite gateway campaign checkpoint")
    parser.add_argument("--config", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--validate-only", action="store_true")
    mode.add_argument("--run", action="store_true")
    mode.add_argument("--resume-cleanup", action="store_true")
    mode.add_argument("--report", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.resume_cleanup:
            from campaign_recovery import resume_cleanup
            try:
                return resume_cleanup(args.config)
            except RecoveryError as error:
                raise ConfigError(str(error)) from error
        config = load_config(args.config, for_report=args.report)
        if args.validate_only:
            return 0
        if args.report:
            return report_campaign(config)
        if not args.run:
            raise ConfigError("unsupported campaign action")
        if config.get("plan") in {"model_ramp", "comparison"}:
            return model_ramp(config)
        return checkpoint(config)
    except (ConfigError, RecoveryError, OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
