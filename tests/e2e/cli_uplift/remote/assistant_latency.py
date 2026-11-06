"""E49: record headless turn timing from an ordinary user's WebSocket."""

import hashlib
import json
import math
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import pstdev
from contextlib import contextmanager
from urllib.parse import urlsplit

import assistant_baseline
import assistant_client
import common


DESIGN_REVISION = "80691ee7431fdec3f186f8f808cf21781163effd"


def money(value, label, *, zero_ok=False):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise common.RemoteError(f"E49 {label} must be a finite USD amount") from None
    common.require(
        result.is_finite() and (result >= 0 if zero_ok else result > 0),
        f"E49 {label} must be a finite USD amount",
    )
    return result


def approved_budget(plan):
    common.require(plan.get("enabled") is True, "E49 requires explicit opt-in")
    limit = money(plan.get("approved_budget_usd"), "approved_budget_usd")
    turn_ceiling = money(plan.get("max_turn_usd"), "max_turn_usd")
    node_hourly = money(plan.get("node_hourly_usd"), "node_hourly_usd", zero_ok=True)
    nodes = plan.get("max_idle_nodes")
    seconds = plan.get("max_measurement_seconds")
    common.require(
        type(nodes) is int and 0 <= nodes <= 3, "E49 requires bounded max_idle_nodes"
    )
    common.require(
        type(seconds) is int and 0 < seconds <= 3600,
        "E49 requires bounded max_measurement_seconds",
    )
    node_reserve = node_hourly * nodes * Decimal(seconds) / 3600
    common.require(
        node_reserve + turn_ceiling <= limit,
        "E49 approved budget cannot cover idle reservation and one model turn",
    )
    return limit, turn_ceiling, node_hourly, node_reserve


def capacity_cost(plan, node_hourly):
    observation = plan.get("capacity_observation")
    if not isinstance(observation, dict):
        return None
    seconds = observation.get("window_seconds")
    idle = observation.get("idle_node_seconds")
    common.require(
        type(seconds) is int
        and 0 < seconds <= plan["max_measurement_seconds"]
        and type(idle) is int
        and 0 <= idle <= seconds * plan["max_idle_nodes"]
        and observation.get("mode") == plan["mode"]
        and isinstance(observation.get("revision"), str)
        and bool(observation["revision"]),
        "E49 capacity observation must name a matching mode, revision and bounded window",
    )
    amount = node_hourly * Decimal(idle) / 3600
    return {
        "source": "operator_supplied_capacity_observation",
        "revision": observation["revision"],
        "window_seconds": seconds,
        "idle_node_seconds": idle,
        "node_idle_usd": str(amount),
        "node_idle_usd_per_measured_hour": str(amount * 3600 / seconds),
    }


def fixture_fingerprint(fixture, revision):
    return hashlib.sha256(
        json.dumps([fixture, revision], sort_keys=True).encode()
    ).hexdigest()


def measure_turn(client, session_id, prompt, request_id, labels, clock=time.monotonic):
    """Return timing evidence, never the prompt or assistant response text."""
    sample = {
        **labels,
        "request_id": request_id,
        "status": "failed",
        "timings": {
            metric: None
            for metric in (
                "durable_ack",
                "scheduled",
                "accepted_to_scheduled",
                "image_start",
                "bootstrap",
                "model_started",
                "model_first_token",
                "first_useful_output",
                "completion",
                "reconnect_recovery",
            )
        },
    }
    sent = clock()
    try:
        client.send(
            {
                "action": "message",
                "session_id": session_id,
                "text": prompt,
                "persona": assistant_baseline.PERSONA,
                "request_id": request_id,
            }
        )
        response = client.receive()
        common.require(
            isinstance(response, dict)
            and response.get("type") == "acknowledged"
            and response.get("durable") is True
            and response.get("request_id") == request_id
            and response.get("session_id") == session_id,
            "E49 lacks a correlated durable acknowledgement",
        )
        sample["timings"]["durable_ack"] = clock() - sent
        for _ in range(128):
            event = client.receive()
            elapsed = clock() - sent
            common.require(isinstance(event, dict), "E49 received an invalid event")
            common.require(
                event.get("request_id", request_id) == request_id
                and event.get("session_id", session_id) == session_id,
                "E49 event crossed request or session identity",
            )
            common.require(
                not event.get("error") and event.get("status") != "failed",
                "E49 assistant turn failed",
            )
            event_type = event.get("type")
            if event_type in {"queued", "image", "starting", "running", "model_token"}:
                name = {
                    "queued": "scheduled",
                    "image": "image_start",
                    "starting": "bootstrap",
                    "running": "model_started",
                    "model_token": "model_first_token",
                }[event_type]
                if sample["timings"][name] is None:
                    sample["timings"][name] = elapsed
                if (
                    event_type == "queued"
                    and sample["timings"]["accepted_to_scheduled"] is None
                ):
                    sample["timings"]["accepted_to_scheduled"] = (
                        elapsed - sample["timings"]["durable_ack"]
                    )
            if (
                event_type == "answer"
                and isinstance(event.get("text"), str)
                and event["text"].strip()
            ):
                if sample["timings"]["first_useful_output"] is None:
                    sample["timings"]["first_useful_output"] = elapsed
            if event_type == "response" and event.get("status") == "completed":
                common.require(
                    isinstance(event.get("content"), str) and event["content"].strip(),
                    "E49 completed without useful output",
                )
                if sample["timings"]["first_useful_output"] is None:
                    sample["timings"]["first_useful_output"] = elapsed
                sample["timings"]["completion"] = elapsed
                usage = event.get("usage")
                if isinstance(usage, dict) and all(
                    type(usage.get(key)) is int and usage[key] >= 0
                    for key in ("input_tokens", "output_tokens")
                ):
                    try:
                        sample["model_usd"] = str(
                            money(
                                usage.get("estimated_usd"), "model usage", zero_ok=True
                            )
                        )
                        sample["usage"] = {
                            "input_count": usage["input_tokens"],
                            "output_count": usage["output_tokens"],
                        }
                    except common.RemoteError:
                        pass
                sample["status"] = "completed"
                break
        else:
            sample["error"] = "event_limit"
    except (
        common.RemoteError,
        assistant_client.ClientError,
        TimeoutError,
        OSError,
    ) as exc:
        timed_out = isinstance(exc, TimeoutError) or (
            isinstance(exc, (assistant_client.ClientError, common.RemoteError))
            and "timed out" in str(exc).lower()
        )
        sample["status"] = "timeout" if timed_out else "failed"
        sample["error"] = type(exc).__name__
    return sample


def percentile(values, fraction):
    ordered = sorted(values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    return ordered[lower] + (ordered[math.ceil(position)] - ordered[lower]) * (
        position - lower
    )


def summarize(samples):
    result = {}
    for kind in ("first", "follow_up"):
        group = [sample for sample in samples if sample["turn_kind"] == kind]
        result[kind] = {
            "attempts": len(group),
            "completed": sum(sample["status"] == "completed" for sample in group),
            "failed": sum(sample["status"] == "failed" for sample in group),
            "timeouts": sum(sample["status"] == "timeout" for sample in group),
        }
        for metric in (
            "durable_ack",
            "accepted_to_scheduled",
            "image_start",
            "bootstrap",
            "model_started",
            "model_first_token",
            "first_useful_output",
            "completion",
            "reconnect_recovery",
        ):
            values = [
                sample["timings"][metric]
                for sample in group
                if isinstance(sample["timings"].get(metric), (int, float))
                and not isinstance(sample["timings"][metric], bool)
            ]
            result[kind][metric] = {
                "observed": len(values),
                "missing": len(group) - len(values),
                "p50": percentile(values, 0.5),
                "p95": percentile(values, 0.95) if len(values) == len(group) else None,
                "stddev": pstdev(values) if values else None,
            }
    return result


MATCHED_FIELDS = (
    "model",
    "model_version",
    "region",
    "client_path",
    "output_size_class",
    "concurrency",
    "fixture_state",
    "transport",
)


def baseline_mismatches(samples, baseline):
    mismatches = []
    for field in MATCHED_FIELDS:
        values = []
        for name, group in (("candidate", samples), ("baseline", baseline)):
            observed = [sample.get(field) for sample in group]
            valid = (
                all(type(value) is int and value > 0 for value in observed)
                if field == "concurrency"
                else all(
                    isinstance(value, str) and bool(value.strip()) for value in observed
                )
            )
            if (
                not observed
                or not valid
                or any(value != observed[0] for value in observed)
            ):
                mismatches.append(f"{name} {field} missing or inconsistent")
                values.append(None)
            else:
                values.append(observed[0])
        if all(value is not None for value in values) and values[0] != values[1]:
            mismatches.append(f"{field} differs")
    for name, group, expected_mode in (
        ("candidate", samples, {"cold", "image-warm", "node-warm", "persistent"}),
        ("baseline", baseline, {"baseline"}),
    ):
        modes = [sample.get("mode") for sample in group]
        if (
            not modes
            or any(
                not isinstance(mode, str) or mode not in expected_mode for mode in modes
            )
            or any(mode != modes[0] for mode in modes)
        ):
            mismatches.append(f"{name} mode missing or inconsistent")
        for kind in ("first", "follow_up"):
            prompts = [
                (sample.get("prompt_id"), sample.get("prompt_sha256"))
                for sample in group
                if sample.get("turn_kind") == kind
            ]
            if (
                not prompts
                or any(
                    not isinstance(prompt_id, str)
                    or not isinstance(digest, str)
                    or not prompt_id
                    or len(digest) != 64
                    for prompt_id, digest in prompts
                )
                or len(set(prompts)) != len(prompts)
            ):
                mismatches.append(f"{name} {kind} prompt set missing or duplicated")
    for kind in ("first", "follow_up"):
        candidate_prompts = {
            (sample.get("prompt_id"), sample.get("prompt_sha256"))
            for sample in samples
            if sample.get("turn_kind") == kind
        }
        baseline_prompts = {
            (sample.get("prompt_id"), sample.get("prompt_sha256"))
            for sample in baseline
            if sample.get("turn_kind") == kind
        }
        if candidate_prompts != baseline_prompts:
            mismatches.append(f"{kind} prompt set differs")
    return mismatches


def verdict(samples, baseline):
    candidate = summarize(samples)
    if not baseline:
        return {
            "status": "BLOCKED",
            "reason": "matched baseline missing",
            "stats": candidate,
        }
    if not isinstance(baseline, list) or any(
        not isinstance(sample, dict) for sample in baseline
    ):
        return {
            "status": "BLOCKED",
            "reason": "matched baseline samples invalid",
            "stats": candidate,
        }
    mismatches = baseline_mismatches(samples, baseline)
    if mismatches:
        return {
            "status": "BLOCKED",
            "reason": "; ".join(mismatches),
            "stats": candidate,
        }
    reference = summarize(baseline)
    if any(reference[kind]["completed"] < 30 for kind in reference):
        return {
            "status": "BLOCKED",
            "reason": "matched baseline has fewer than 30 completed turns",
            "stats": candidate,
        }
    incomplete = any(candidate[kind]["completed"] < 30 for kind in candidate)
    if incomplete and all(
        candidate[kind]["failed"] + candidate[kind]["timeouts"] == 0
        for kind in candidate
    ):
        return {
            "status": "BLOCKED",
            "reason": "candidate has fewer than 30 completed turns",
            "stats": candidate,
        }

    def within(kind, metric, ratio, quantile="p95"):
        value = candidate[kind][metric][quantile]
        matched = reference[kind][metric][quantile]
        return value is not None and matched is not None and value <= ratio * matched

    criteria = {
        "durable_ack": all(
            candidate[kind]["durable_ack"]["p95"] is not None
            and candidate[kind]["durable_ack"]["p95"] <= 3
            for kind in candidate
        ),
        "first_useful_output": within("first", "first_useful_output", 0.8),
        "persistent_follow_up": within("follow_up", "first_useful_output", 0.8),
        "p50_regression": all(
            within(kind, "first_useful_output", 1.1, "p50") for kind in candidate
        ),
        "completed_turns": not incomplete
        and all(
            candidate[kind]["failed"] == candidate[kind]["timeouts"] == 0
            for kind in candidate
        ),
    }
    return {
        "status": "PASS" if all(criteria.values()) else "FAIL",
        "criteria": criteria,
        "stats": candidate,
    }


@contextmanager
def selected_session_mode(config, evidence, session, session_id, mode, on_change=None):
    if mode != "persistent":
        yield
        return
    common.require(
        isinstance(session_id, str)
        and assistant_baseline.SESSION.fullmatch(session_id),
        "E49 mode selection requires an owned session identifier",
    )
    url = config["gateway_url"].rstrip("/") + "/chat/sessions/" + session_id + "/mode"
    prior = assistant_client.http_json(url, session)
    common.require(
        isinstance(prior, dict) and prior.get("mode") in {"ephemeral", "persistent"},
        "E49 could not read the prior session mode",
    )
    if prior["mode"] == mode:
        yield
        return
    setting = "session_mode:" + hashlib.sha256(session_id.encode()).hexdigest()[:12]
    change = {
        "setting": setting,
        "prior": prior["mode"],
        "selected": mode,
        "restored": False,
    }
    evidence["detail"]["mode_changes"].append(change)
    if on_change is not None:
        on_change(change)
    try:
        selected = assistant_client.http_json(
            url, session, method="PUT", body={"mode": mode}
        )
        observed = assistant_client.http_json(url, session)
        common.require(
            isinstance(selected, dict)
            and selected.get("mode") == mode
            and isinstance(observed, dict)
            and observed.get("mode") == mode,
            "E49 could not verify the selected session mode",
        )
        yield
    finally:
        try:
            restored = assistant_client.http_json(
                url, session, method="PUT", body={"mode": prior["mode"]}
            )
            observed = assistant_client.http_json(url, session)
            common.require(
                isinstance(restored, dict)
                and restored.get("mode") == prior["mode"]
                and isinstance(observed, dict)
                and observed.get("mode") == prior["mode"],
                "E49 could not verify restored session mode",
            )
            change["restored"] = True
            if on_change is not None:
                on_change(change)
        except Exception:
            evidence["detail"]["clean"] = False
            evidence["detail"].setdefault("unrestored_settings", []).append(setting)
            raise common.RemoteError("E49 session_mode restoration failed") from None


def execute(config, evidence):
    plan = config.get("assistant_latency") or {}
    prompts = plan.get("prompts")
    common.require(
        isinstance(prompts, list)
        and len(prompts) == 30
        and all(
            isinstance(row, dict)
            and isinstance(row.get("id"), str)
            and row["id"]
            and isinstance(row.get("text"), str)
            and row["text"]
            for row in prompts
        )
        and len({row["id"] for row in prompts}) == len(prompts),
        "E49 requires 30 distinct scripted prompts",
    )
    common.require(
        plan.get("mode")
        in {"baseline", "cold", "image-warm", "node-warm", "persistent"},
        "E49 requires an explicit mode",
    )
    limit, turn_ceiling, node_hourly, node_reserve = approved_budget(plan)
    common.require(
        not config.get("prior_dispatch"), "E49 prior dispatch needs reconciliation"
    )
    common.require(
        all(
            isinstance(plan.get(key), str) and bool(plan[key])
            for key in ("model", "model_version", "output_size_class")
        )
        and plan.get("concurrency") == 1
        and isinstance(plan.get("fixture_revision"), str)
        and bool(plan["fixture_revision"])
        and plan.get("transport") in {"deterministic", "real_model"},
        "E49 requires explicit model, version, output class, single-client concurrency, fixture revision and transport",
    )
    if (
        plan["mode"] != "baseline"
        and plan["transport"] == "real_model"
        and not (
            isinstance(plan.get("baseline_decision_ref"), str)
            and bool(plan["baseline_decision_ref"].strip())
        )
    ):
        evidence.update(
            success=False,
            stage="blocked",
            detail={
                "status": "BLOCKED",
                "reason": "approved baseline decision not recorded",
                "samples": [],
                "clean": True,
            },
        )
        return
    fixture = (config.get("assistant_users") or {}).get("a1")
    common.require(isinstance(fixture, dict), "E49 ordinary-user fixture is missing")
    websocket = config["websocket_url"]
    baseline = plan.get("baseline_samples")
    if plan["mode"] != "baseline":
        if not baseline:
            evidence.update(
                success=False,
                stage="blocked",
                detail={
                    "status": "BLOCKED",
                    "reason": "matched baseline missing",
                    "samples": [],
                    "clean": True,
                },
            )
            return
        if not isinstance(baseline, list) or any(
            not isinstance(row, dict) for row in baseline
        ):
            evidence.update(
                success=False,
                stage="blocked",
                detail={
                    "status": "BLOCKED",
                    "reason": "matched baseline samples invalid",
                    "samples": [],
                    "clean": True,
                },
            )
            return
        if any(
            sum(
                row.get("status") == "completed"
                for row in baseline
                if row.get("turn_kind") == kind
            )
            < 30
            for kind in ("first", "follow_up")
        ):
            evidence.update(
                success=False,
                stage="blocked",
                detail={
                    "status": "BLOCKED",
                    "reason": "matched baseline has fewer than 30 completed turns",
                    "samples": [],
                    "clean": True,
                },
            )
            return
        metadata = {
            "mode": plan["mode"],
            "model": plan["model"],
            "model_version": plan["model_version"],
            "region": config.get("region"),
            "client_path": urlsplit(websocket).path,
            "concurrency": plan["concurrency"],
            "output_size_class": plan["output_size_class"],
            "fixture_state": fixture_fingerprint(fixture, plan["fixture_revision"]),
            "transport": plan["transport"],
        }
        expected = [
            {
                **metadata,
                "turn_kind": kind,
                "prompt_id": prompt["id"],
                "prompt_sha256": hashlib.sha256(prompt["text"].encode()).hexdigest(),
            }
            for kind in ("first", "follow_up")
            for prompt in prompts
        ]
        mismatches = baseline_mismatches(expected, baseline)
        if mismatches:
            evidence.update(
                success=False,
                stage="blocked",
                detail={
                    "status": "BLOCKED",
                    "reason": "; ".join(mismatches),
                    "samples": [],
                    "clean": True,
                },
            )
            return
    common.require(config.get("work_dir"), "E49 needs a durable run directory")
    journal_path = Path(config["work_dir"]) / (
        "assistant-latency-"
        + hashlib.sha256(
            json.dumps([config["evaluation_id"], plan["mode"]]).encode()
        ).hexdigest()
        + ".json"
    )
    if journal_path.exists():
        prior = json.loads(journal_path.read_text())
        common.require(
            prior.get("evaluation_id") == config["evaluation_id"]
            and prior.get("mode") == plan["mode"],
            "E49 recovery journal identity changed",
        )
        pending = prior.get("mode_change") or {}
        detail = {
            "status": "BLOCKED",
            "reason": "prior E49 measurement needs reconciliation; no automatic resubmission",
            "samples": prior.get("samples", []),
            "clean": pending.get("restored") is not False,
        }
        if pending.get("restored") is False:
            detail["unrestored_settings"] = [pending["setting"]]
        evidence.update(success=False, stage="blocked", detail=detail)
        return
    session = assistant_baseline.session_for(config, fixture)
    assistant_client.websocket_url(websocket, session)
    principal = assistant_client.http_json(
        config["gateway_url"].rstrip("/") + "/auth/me", session
    )
    common.require(
        principal.get("account_type") == "human"
        and principal.get("is_admin") is False
        and principal.get("user_id")
        in {fixture["login_user_id"], fixture["canonical_user_id"]}
        and principal.get("org_id") == fixture["tenant_id"],
        "E49 requires the expected ordinary user",
    )
    samples = []
    evidence["detail"] = {
        "samples": samples,
        "raw_samples_artifact": "assistant-latency-samples.jsonl",
        "design_revision": DESIGN_REVISION,
        "baseline_decision_ref": plan.get("baseline_decision_ref"),
        "mode": plan["mode"],
        "clean": True,
        "mode_changes": [],
    }
    journal = {
        "evaluation_id": config["evaluation_id"],
        "mode": plan["mode"],
        "phase": "prepared",
        "samples": [],
    }
    journal_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    assistant_baseline.persist(journal_path, journal)
    spent = Decimal(0)
    halt = None
    started = time.monotonic()

    def save_mode(change):
        journal["mode_change"] = dict(change)
        assistant_baseline.persist(journal_path, journal)

    def within_budget():
        return (
            time.monotonic() - started < plan["max_measurement_seconds"]
            and spent + turn_ceiling + node_reserve <= limit
        )

    with assistant_client.connect(websocket, session) as client:
        for prompt in prompts:
            if not within_budget():
                halt = "approved spend or measurement time cap reached"
                break
            client_request = hashlib.sha256(
                json.dumps(
                    [config["evaluation_id"], plan["mode"], prompt["id"], "session"]
                ).encode()
            ).hexdigest()
            journal["phase"] = "session_create_attempted"
            assistant_baseline.persist(journal_path, journal)
            client.send({"action": "create-session", "request_id": client_request})
            created = client.receive()
            common.require(
                isinstance(created, dict)
                and created.get("request_id") == client_request
                and isinstance(created.get("session_id"), str),
                "E49 session creation was not correlated",
            )
            journal["session_id"] = created["session_id"]
            journal["phase"] = "mode_selection_attempted"
            assistant_baseline.persist(journal_path, journal)
            with selected_session_mode(
                config,
                evidence,
                session,
                created["session_id"],
                plan["mode"],
                on_change=save_mode,
            ):
                for turn_kind in ("first", "follow_up"):
                    if not within_budget():
                        halt = "approved spend or measurement time cap reached"
                        break
                    request_id = hashlib.sha256(
                        json.dumps(
                            [
                                config["evaluation_id"],
                                plan["mode"],
                                turn_kind,
                                prompt["id"],
                            ]
                        ).encode()
                    ).hexdigest()
                    labels = {
                        "mode": plan["mode"],
                        "prompt_id": prompt["id"],
                        "turn_kind": turn_kind,
                        "model": plan.get("model"),
                        "model_version": plan.get("model_version"),
                        "region": config.get("region"),
                        "client_path": urlsplit(websocket).path,
                        "concurrency": plan.get("concurrency"),
                        "fixture_state": fixture_fingerprint(
                            fixture, plan["fixture_revision"]
                        ),
                        "prompt_sha256": hashlib.sha256(
                            prompt["text"].encode()
                        ).hexdigest(),
                        "output_size_class": plan.get("output_size_class"),
                        "transport": plan.get("transport"),
                    }
                    journal["phase"] = "message_attempted"
                    assistant_baseline.persist(journal_path, journal)
                    samples.append(
                        measure_turn(
                            client,
                            created["session_id"],
                            prompt["text"],
                            request_id,
                            labels,
                        )
                    )
                    sample = samples[-1]
                    journal.update(phase="turn_observed", samples=samples)
                    assistant_baseline.persist(journal_path, journal)
                    if sample.get("model_usd") is None:
                        halt = "model usage unavailable after a turn; stopped before another dispatch"
                        break
                    charged = Decimal(sample["model_usd"])
                    spent += charged
                    if charged > turn_ceiling or spent + node_reserve > limit:
                        halt = "reported model charge exceeded approved spend cap"
                        break
                    if sample["status"] != "completed":
                        break
            if halt or samples[-1]["status"] != "completed":
                break
    if not halt and time.monotonic() - started > plan["max_measurement_seconds"]:
        halt = "approved measurement time cap reached"
    result = verdict(samples, plan.get("baseline_samples"))
    capacity = capacity_cost(plan, node_hourly)
    node_cost = Decimal(capacity["node_idle_usd"]) if capacity else None
    if halt and "exceeded" in halt:
        cost_status = "FAIL"
    elif halt or capacity is None:
        cost_status = "BLOCKED"
    elif spent + node_cost > limit:
        cost_status = "FAIL"
    else:
        cost_status = "PASS"
    cost = {
        "status": cost_status,
        "model_spend_usd": (
            str(spent)
            if all(sample.get("model_usd") is not None for sample in samples)
            else None
        ),
        "priced_turns_usd": str(spent),
        "unknown_cost_attempts": sum(
            sample.get("model_usd") is None for sample in samples
        ),
        "model_spend_source": "assistant_event_estimated_usd",
        "reserved_idle_usd": str(node_reserve),
        "approved_budget_usd": str(limit),
        "capacity": capacity,
        "reason": halt
        or ("capacity observation missing" if capacity is None else None),
    }
    latency_status = result["status"]
    result["latency_status"] = latency_status
    result["cost"] = cost
    if "FAIL" in (latency_status, cost_status):
        result["status"] = "FAIL"
    elif "BLOCKED" in (latency_status, cost_status):
        result["status"] = "BLOCKED"
    evidence["detail"].update(result)
    evidence["success"] = result["status"] == "PASS" and evidence["detail"]["clean"]
    journal.update(phase="completed", samples=samples, model_spend_usd=str(spent))
    assistant_baseline.persist(journal_path, journal)
    if result["status"] == "BLOCKED":
        evidence["stage"] = "blocked"
