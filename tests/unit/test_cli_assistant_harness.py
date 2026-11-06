"""Offline assistant harness contracts; Unix socketpairs only, no network or cloud."""

import hashlib
import io
import json
import os
import socket
import struct
import subprocess
import sys
import threading
from copy import deepcopy
from pathlib import Path
from urllib.error import HTTPError

import pytest

from tests.e2e.cli_uplift import (
    assistant_oracles,
    assistant_provider_sources,
    bundle,
    cases,
    cleanup,
    config,
    fixtures,
    live,
    preflight,
    runner,
    stages,
    statestore,
)
from tests.e2e.cli_uplift.remote import (
    assistant_client,
    assistant_isolation_clients,
    assistant_process,
    common,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("Offline assistant tests must not use the network")

    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)


@pytest.fixture
def users():
    return {
        label: {
            "login_user_id": label,
            "canonical_user_id": label + "-canonical",
            "tenant_id": "tenant-a" if label != "b1" else "tenant-b",
            "fixture_name": "synthetic/assistant/" + label,
        }
        for label in ("a1", "a2", "b1")
    }


def test_assistant_selection_and_missing_fixture_never_accept(users):
    assert [case.id for case in cases.suite_cases("assistant")] == [
        f"E{index}" for index in range(43, 52)
    ]
    assert set(cases.suite_cases("assistant")).isdisjoint(cases.suite_cases("nightly"))
    assert stages.JOURNEY_DRIVERS["E43"] == "assistant_stream"
    matrix = cases.new_matrix(("assistant",))
    assert (
        len(
            cases.block_missing_fixtures(
                matrix, {cases.EC2, cases.COGNITO, cases.PLATFORM}
            )
        )
        == 9
    )
    assert all(row["status"] == cases.BLOCKED for row in matrix.values())
    assert cases.is_full(("assistant",)) is False
    assert cases.accept(matrix, ("assistant",), cleanup_ok=True)[0] != cases.PASSED
    fixtures.validate_fixture("assistant_users", users)
    assert cases.ASSISTANT_USERS in config.fixture_classes(
        {"assistant_users": users, "websocket_url": "wss://example.invalid/ws"}
    )
    assert cases.ASSISTANT_USERS not in preflight.evaluate_fixtures(
        {"assistant_users": users}
    )
    assert cases.ASSISTANT_USERS in preflight.evaluate_fixtures(
        {"assistant_users": users, "websocket_url": "wss://example.invalid/ws"}
    )


def latency_samples(
    first, follow_up, ack=1, *, mode="image-warm", fixture_state="synthetic-fixture"
):
    return [
        {
            "turn_kind": kind,
            "status": "completed",
            "mode": mode,
            "model": "synthetic-model",
            "model_version": "synthetic-version",
            "region": "synthetic-region",
            "client_path": "/ws",
            "concurrency": 1,
            "output_size_class": "short",
            "fixture_state": fixture_state,
            "transport": "deterministic",
            "prompt_id": str(index),
            "prompt_sha256": hashlib.sha256(b"synthetic prompt").hexdigest(),
            "timings": {
                "durable_ack": ack,
                "first_useful_output": delay,
                "completion": delay + 1,
            },
        }
        for kind, delay in (("first", first), ("follow_up", follow_up))
        for index in range(30)
    ]


def test_e49_known_percentiles_and_passing_failing_verdicts():
    import tarfile

    from tests.e2e.cli_uplift.remote import dispatcher

    module, _ = dispatcher.load("assistant_latency")
    assert "assistant_latency" in bundle.purposes()
    with tarfile.open(fileobj=io.BytesIO(bundle.archive()), mode="r:gz") as archive:
        assert "remote/assistant_latency.py" in archive.getnames()
    assert module.percentile([1, 2, 3, 4, 5], 0.95) == pytest.approx(4.8)
    baseline = latency_samples(10, 10, mode="baseline")
    result = module.verdict(latency_samples(7, 8), baseline)
    assert result["status"] == "PASS"
    assert result["stats"]["first"]["first_useful_output"] == {
        "observed": 30,
        "missing": 0,
        "p50": 7,
        "p95": 7,
        "stddev": 0,
    }
    assert result["stats"]["follow_up"]["attempts"] == 30
    failed = module.verdict(latency_samples(9, 9, ack=4), baseline)
    assert failed["status"] == "FAIL"
    assert failed["criteria"]["durable_ack"] is False
    assert failed["criteria"]["first_useful_output"] is False


def test_e49_real_dispatcher_measures_headless_synthetic_turns(
    monkeypatch, users, tmp_path, capsys
):
    from tests.e2e.cli_uplift.remote import dispatcher

    module, _ = dispatcher.load("assistant_latency")
    monkeypatch.setattr(module.assistant_baseline, "session_for", lambda *_: object())
    monkeypatch.setattr(
        module.assistant_client, "websocket_url", lambda *_: "wss://example.invalid/ws"
    )
    monkeypatch.setattr(
        module.assistant_client,
        "http_json",
        lambda *_: {
            "account_type": "human",
            "is_admin": False,
            "user_id": users["a1"]["canonical_user_id"],
            "org_id": users["a1"]["tenant_id"],
        },
    )
    state = {"time": 0.0, "include_usage": True, "model_turns": 0}

    class Client:
        def __enter__(self):
            self.queue = []
            return self

        def __exit__(self, *_):
            pass

        def send(self, message):
            if message["action"] == "create-session":
                self.queue = [
                    {
                        "request_id": message["request_id"],
                        "session_id": "synthetic-session",
                    }
                ]
            else:
                state["model_turns"] += 1
                self.queue = [
                    {
                        "type": "acknowledged",
                        "durable": True,
                        "request_id": message["request_id"],
                        "session_id": message["session_id"],
                    },
                    {"type": "progress", "text": "thinking"},
                    {
                        "type": "response",
                        "status": "completed",
                        "content": "useful",
                        "usage": {
                            "input_tokens": 10,
                            "output_tokens": 20,
                            "estimated_usd": "0.01",
                        }
                        if state["include_usage"]
                        else None,
                    },
                ]

        def receive(self):
            event = self.queue.pop(0)
            state["time"] += {
                "acknowledged": 1,
                "progress": 1,
                "response": 5,
            }.get(event.get("type"), 0)
            return event

    monkeypatch.setattr(module.assistant_client, "connect", lambda *_: Client())
    original = module.measure_turn
    monkeypatch.setattr(
        module,
        "measure_turn",
        lambda client, session_id, prompt, request_id, labels: original(
            client, session_id, prompt, request_id, labels, clock=lambda: state["time"]
        ),
    )
    cfg = {
        "assistant_users": users,
        "websocket_url": "wss://example.invalid/ws",
        "gateway_url": "https://example.invalid",
        "evaluation_id": "synthetic-e49",
        "work_dir": str(tmp_path),
        "region": "synthetic-region",
        "assistant_latency": {
            "enabled": True,
            "mode": "image-warm",
            "approved_budget_usd": "2",
            "max_turn_usd": "0.02",
            "node_hourly_usd": "0.10",
            "max_idle_nodes": 1,
            "max_measurement_seconds": 3600,
            "capacity_observation": {
                "mode": "image-warm",
                "revision": "synthetic-revision",
                "window_seconds": 3600,
                "idle_node_seconds": 3600,
            },
            "model": "synthetic-model",
            "model_version": "synthetic-version",
            "fixture_revision": "synthetic-fixture-revision",
            "concurrency": 1,
            "output_size_class": "short",
            "transport": "deterministic",
            "prompts": [
                {"id": str(index), "text": "synthetic prompt"} for index in range(30)
            ],
            "baseline_samples": latency_samples(
                10,
                10,
                mode="baseline",
                fixture_state=hashlib.sha256(
                    json.dumps(
                        [users["a1"], "synthetic-fixture-revision"], sort_keys=True
                    ).encode()
                ).hexdigest(),
            ),
        },
    }
    evidence = {}
    module.execute(cfg, evidence)
    assert evidence["success"] is True
    assert evidence["detail"]["cost"]["model_spend_usd"] == "0.60"
    assert evidence["detail"]["cost"]["status"] == "PASS"
    assert module.common.redact(evidence)["detail"]["samples"][0]["usage"] == {
        "input_count": 10,
        "output_count": 20,
    }
    assert evidence["detail"]["stats"]["first"]["completed"] == 30
    assert evidence["detail"]["stats"]["follow_up"]["completed"] == 30
    assert evidence["detail"]["stats"]["first"]["first_useful_output"]["p95"] == 7
    assert evidence["detail"]["samples"][0]["prompt_id"] == "0"
    assert "synthetic prompt" not in json.dumps(evidence)
    matrix = cases.new_matrix(("assistant",))
    monkeypatch.setattr(stages, "JOURNEY_DRIVERS", {"E49": "assistant_latency"})
    stages.journeys_stage({}, {"journey": lambda *_: lambda *_: evidence})(
        stage_context(matrix)
    )
    assert matrix["E49"]["status"] == cases.PASSED
    assert "unimplemented" not in matrix["E49"]["detail"]
    assert state["model_turns"] == 60

    state["model_turns"] = 0
    repeated = {}
    module.execute(cfg, repeated)
    assert repeated["stage"] == "blocked"
    assert "no automatic resubmission" in repeated["detail"]["reason"]
    assert state["model_turns"] == 0
    assert list(tmp_path.glob("assistant-latency-*.json"))
    assert all(
        path.stat().st_mode & 0o777 == 0o600
        for path in tmp_path.glob("assistant-latency-*.json")
    )
    journal_path = next(tmp_path.glob("assistant-latency-*.json"))
    journal = json.loads(journal_path.read_text())
    journal.update(
        phase="message_attempted",
        mode_change={
            "setting": "session_mode:synthetic",
            "restored": False,
        },
    )
    module.assistant_baseline.persist(journal_path, journal)
    interrupted = {}
    module.execute(cfg, interrupted)
    assert interrupted["stage"] == "blocked"
    assert interrupted["detail"]["clean"] is False
    assert interrupted["detail"]["unrestored_settings"] == ["session_mode:synthetic"]
    assert state["model_turns"] == 0

    state.update(include_usage=False, model_turns=0)
    cfg["evaluation_id"] = "synthetic-e49-unknown"
    missing_usage = {}
    module.execute(cfg, missing_usage)
    assert missing_usage["success"] is False
    assert missing_usage["detail"]["cost"]["status"] == "BLOCKED"
    assert "usage unavailable" in missing_usage["detail"]["cost"]["reason"]
    assert missing_usage["detail"]["cost"]["model_spend_usd"] is None
    assert missing_usage["detail"]["cost"]["unknown_cost_attempts"] == 1
    assert state["model_turns"] == 1

    state.update(include_usage=True, model_turns=0)
    cfg["evaluation_id"] = "synthetic-e49-capped"
    cfg["assistant_latency"].update(approved_budget_usd="0.05", node_hourly_usd="0")
    capped = {}
    module.execute(cfg, capped)
    assert capped["success"] is False
    assert capped["detail"]["cost"]["status"] == "BLOCKED"
    assert "cap reached" in capped["detail"]["cost"]["reason"]
    assert capped["detail"]["cost"]["model_spend_usd"] == "0.04"
    assert state["model_turns"] == 4

    cfg["evaluation_id"] = "synthetic-e49-dispatcher"
    cfg["assistant_latency"].update(approved_budget_usd="2", node_hourly_usd="0.10")
    payload_path = tmp_path / "e49-dispatcher.json"
    payload_path.write_text(json.dumps(cfg))
    monkeypatch.setattr(module.common, "assert_owned_instance", lambda *_: None)
    assert dispatcher.main(["assistant_latency", str(payload_path)]) == 0
    emitted = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert emitted["success"] is True
    assert emitted["detail"]["stats"]["first"]["completed"] == 30
    assert emitted["detail"]["samples"][0]["usage"] == {
        "input_count": 10,
        "output_count": 20,
    }
    assert "synthetic prompt" not in json.dumps(emitted)


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"enabled": False}, "explicit opt-in"),
        ({"approved_budget_usd": "NaN"}, "approved_budget_usd"),
        ({"approved_budget_usd": "-1"}, "approved_budget_usd"),
        ({"max_turn_usd": "2"}, "cannot cover"),
        ({"max_measurement_seconds": 3601}, "max_measurement_seconds"),
        ({"max_idle_nodes": 4}, "max_idle_nodes"),
    ],
)
def test_e49_requires_explicit_bounded_approved_spend(change, reason):
    from tests.e2e.cli_uplift.remote import dispatcher

    module, _ = dispatcher.load("assistant_latency")
    plan = {
        "enabled": True,
        "approved_budget_usd": "1",
        "max_turn_usd": "0.01",
        "node_hourly_usd": "0.10",
        "max_idle_nodes": 1,
        "max_measurement_seconds": 3600,
    }
    plan.update(change)
    with pytest.raises(module.common.RemoteError, match=reason):
        module.approved_budget(plan)


def test_e49_capacity_cost_is_labelled_operator_observation():
    from tests.e2e.cli_uplift.remote import dispatcher

    module, _ = dispatcher.load("assistant_latency")
    plan = {
        "mode": "node-warm",
        "max_idle_nodes": 1,
        "max_measurement_seconds": 3600,
        "capacity_observation": {
            "mode": "node-warm",
            "revision": "synthetic-revision",
            "window_seconds": 3600,
            "idle_node_seconds": 1800,
        },
    }
    observation = module.capacity_cost(plan, module.money("0.10", "node_hourly_usd"))
    assert observation["node_idle_usd"] == "0.05"
    assert observation["node_idle_usd_per_measured_hour"] == "0.05"
    assert observation["source"] == "operator_supplied_capacity_observation"
    plan["capacity_observation"]["mode"] = "cold"
    with pytest.raises(module.common.RemoteError, match="matching mode"):
        module.capacity_cost(plan, module.money("0.10", "node_hourly_usd"))


def test_e49_report_writes_sanitized_revisioned_jsonl(tmp_path):
    from tests.e2e.cli_uplift import report

    matrix = cases.new_matrix(("assistant",))
    for case_id in matrix:
        detail = {"missing_fixtures": ["synthetic"]}
        if case_id == "E49":
            detail = {
                "design_revision": "80691ee7431fdec3f186f8f808cf21781163effd",
                "samples": [
                    {
                        "mode": "baseline",
                        "prompt_id": "synthetic",
                        "access_token": "private-marker",
                        "status": "timeout",
                    },
                    {
                        "mode": "baseline",
                        "prompt_id": "synthetic-2",
                        "status": "completed",
                    },
                ],
            }
        cases.record(matrix, case_id, cases.BLOCKED, detail)
    evaluation_id = "adp-e2e-20260915-143022-a1b2c3"
    document = report.build(
        matrix=matrix,
        suites=("assistant",),
        config={"expected_revision": "a" * 40, "harness_commit": "b" * 40},
        evaluation_id=evaluation_id,
        attempt_id=evaluation_id + "-a1",
        cleanup_ok=True,
        timing={
            "started_at": "2026-09-15T14:30:22Z",
            "ended_at": "2026-09-15T15:10:00Z",
            "duration_seconds": 2378,
        },
        correlation={},
        preflight={"deployed_revision": "c" * 40},
    )
    assert report.validate(document) == []
    report.write(tmp_path, document, matrix, evaluation_id)
    raw_path = tmp_path / "assistant-latency-samples.jsonl"
    rows = [json.loads(line) for line in raw_path.read_text().splitlines()]
    assert len(rows) == 2
    assert rows[0]["status"] == "timeout"
    assert rows[1]["prompt_id"] == "synthetic-2"
    assert rows[0]["evaluation_id"] == evaluation_id
    assert rows[0]["harness_commit"] == "b" * 40
    assert rows[0]["deployed_revision"] == "c" * 40
    assert "private-marker" not in raw_path.read_text()
    assert raw_path.stat().st_mode & 0o777 == 0o600
    workflow = (
        Path(__file__).resolve().parents[2] / ".github/workflows/eval-cli-uplift.yml"
    ).read_text()
    assert (
        "${{ runner.temp }}/cli-uplift-evidence/assistant-latency-samples.jsonl"
        in workflow
    )


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("missing", "baseline missing"),
        ("unapproved", "baseline decision not recorded"),
        ("region", "region differs"),
        ("failed", "fewer than 30 completed"),
    ],
)
def test_e49_refuses_unmatched_baseline_before_model_dispatch(
    monkeypatch, users, mutation, reason
):
    from tests.e2e.cli_uplift.remote import dispatcher

    module, _ = dispatcher.load("assistant_latency")
    monkeypatch.setattr(
        module.assistant_baseline,
        "session_for",
        lambda *_: pytest.fail("Invalid baseline must not open a user session"),
    )
    baseline = latency_samples(
        10,
        10,
        mode="baseline",
        fixture_state=module.fixture_fingerprint(
            users["a1"], "synthetic-fixture-revision"
        ),
    )
    if mutation == "unapproved":
        for sample in baseline:
            sample["transport"] = "real_model"
    if mutation == "region":
        for sample in baseline:
            sample["region"] = "other-region"
    elif mutation == "failed":
        baseline[0]["status"] = "failed"
    plan = {
        "enabled": True,
        "mode": "image-warm",
        "approved_budget_usd": "1",
        "max_turn_usd": "0.01",
        "node_hourly_usd": "0",
        "max_idle_nodes": 0,
        "max_measurement_seconds": 3600,
        "model": "synthetic-model",
        "model_version": "synthetic-version",
        "fixture_revision": "synthetic-fixture-revision",
        "concurrency": 1,
        "output_size_class": "short",
        "transport": "real_model" if mutation == "unapproved" else "deterministic",
        "prompts": [
            {"id": str(index), "text": "synthetic prompt"} for index in range(30)
        ],
        "baseline_samples": None if mutation == "missing" else baseline,
    }
    evidence = {}
    module.execute(
        {
            "assistant_latency": plan,
            "assistant_users": users,
            "websocket_url": "wss://example.invalid/ws",
            "region": "synthetic-region",
        },
        evidence,
    )
    assert evidence["success"] is False
    assert evidence["stage"] == "blocked"
    assert evidence["detail"]["status"] == "BLOCKED"
    assert reason in evidence["detail"]["reason"]
    assert evidence["detail"]["samples"] == []


def test_e49_missing_baseline_is_blocked_in_verdict_and_case(monkeypatch):
    from tests.e2e.cli_uplift.remote import dispatcher

    module, _ = dispatcher.load("assistant_latency")
    candidate = latency_samples(7, 8)
    result = module.verdict(candidate, None)
    assert result["status"] == "BLOCKED"
    assert "baseline missing" in result["reason"]
    matrix = cases.new_matrix(("assistant",))
    monkeypatch.setattr(stages, "JOURNEY_DRIVERS", {"E49": "assistant_latency"})
    stages.journeys_stage(
        {},
        {
            "journey": lambda *_: lambda *_: {
                "success": False,
                "stage": "blocked",
                "detail": result,
            }
        },
    )(stage_context(matrix))
    assert matrix["E49"]["status"] == cases.BLOCKED
    assert "baseline missing" in matrix["E49"]["detail"]["reason"]


@pytest.mark.parametrize(
    "field,other,reason",
    [
        ("model", "other-model", "model differs"),
        ("model_version", "other-version", "model_version differs"),
        ("region", "other-region", "region differs"),
        ("client_path", "/other", "client_path differs"),
        ("concurrency", 2, "concurrency differs"),
        ("output_size_class", "long", "output_size_class differs"),
        ("fixture_state", "other-fixture", "fixture_state differs"),
        ("transport", "real-model", "transport differs"),
        ("prompt_sha256", "0" * 64, "prompt set differs"),
    ],
)
def test_e49_mismatched_baseline_names_condition(field, other, reason):
    from tests.e2e.cli_uplift.remote import dispatcher

    module, _ = dispatcher.load("assistant_latency")
    candidate = latency_samples(7, 8)
    baseline = latency_samples(10, 10, mode="baseline")
    for sample in baseline:
        sample[field] = other
    result = module.verdict(candidate, baseline)
    assert result["status"] == "BLOCKED"
    assert reason in result["reason"]
    assert result["status"] != "PASS"


def test_e49_baseline_missing_metadata_never_compares_as_equal():
    from tests.e2e.cli_uplift.remote import dispatcher

    module, _ = dispatcher.load("assistant_latency")
    candidate = latency_samples(7, 8)
    baseline = latency_samples(10, 10, mode="baseline")
    for sample in baseline + candidate:
        sample.pop("model")
    result = module.verdict(candidate, baseline)
    assert result["status"] == "BLOCKED"
    assert "model missing" in result["reason"]
    baseline = latency_samples(10, 10, mode="baseline")
    baseline[0]["prompt_id"] = baseline[1]["prompt_id"]
    result = module.verdict(candidate, baseline)
    assert result["status"] == "BLOCKED"
    assert "prompt set" in result["reason"]


def test_e49_persistent_session_mode_restores_prior_value(monkeypatch):
    from tests.e2e.cli_uplift.remote import dispatcher

    module, _ = dispatcher.load("assistant_latency")
    config = {"gateway_url": "https://example.invalid"}
    evidence = {"detail": {"clean": True, "mode_changes": []}}
    state = {"mode": "ephemeral", "writes": []}

    def http(url, session, *, method="GET", body=None):
        assert url == "https://example.invalid/chat/sessions/synthetic-session/mode"
        if method == "PUT":
            state["mode"] = body["mode"]
            state["writes"].append(body["mode"])
        return {"mode": state["mode"]}

    monkeypatch.setattr(module.assistant_client, "http_json", http)
    with module.selected_session_mode(
        config, evidence, object(), "synthetic-session", "persistent"
    ):
        assert state["mode"] == "persistent"
        assert evidence["detail"]["mode_changes"][0]["prior"] == "ephemeral"
    assert state["mode"] == "ephemeral"
    assert state["writes"] == ["persistent", "ephemeral"]
    assert evidence["detail"]["mode_changes"][0]["restored"] is True
    assert evidence["detail"]["clean"] is True
    with pytest.raises(ValueError, match="measurement failed"):
        with module.selected_session_mode(
            config, evidence, object(), "synthetic-session", "persistent"
        ):
            raise ValueError("measurement failed")
    assert state["mode"] == "ephemeral"
    with module.selected_session_mode(
        config, evidence, object(), "synthetic-session", "node-warm"
    ):
        pass
    assert state["writes"] == ["persistent", "ephemeral", "persistent", "ephemeral"]


def test_e49_restore_failure_is_non_clean_and_names_setting(monkeypatch):
    from tests.e2e.cli_uplift.remote import dispatcher

    module, _ = dispatcher.load("assistant_latency")
    config = {"gateway_url": "https://example.invalid"}
    evidence = {"detail": {"clean": True, "mode_changes": []}}
    state = {"mode": "ephemeral"}

    def http(url, session, *, method="GET", body=None):
        if method == "PUT":
            if body["mode"] == "ephemeral":
                raise module.assistant_client.ClientError("simulated restore failure")
            state["mode"] = body["mode"]
        return {"mode": state["mode"]}

    monkeypatch.setattr(module.assistant_client, "http_json", http)
    with pytest.raises(
        module.common.RemoteError, match="session_mode restoration failed"
    ):
        with module.selected_session_mode(
            config, evidence, object(), "synthetic-session", "persistent"
        ):
            assert state["mode"] == "persistent"
    assert evidence["detail"]["clean"] is False
    assert len(evidence["detail"]["unrestored_settings"]) == 1
    assert evidence["detail"]["unrestored_settings"][0].startswith("session_mode:")
    assert evidence["detail"]["mode_changes"][0]["restored"] is False


def test_e49_mode_write_is_confined_to_owned_session_path():
    session = assistant_client.UserSession(
        {"access_token": "ordinary", "expires_at": 4000000000}
    )
    calls = []

    def opener(request, timeout):
        calls.append((request.get_method(), request.full_url, json.loads(request.data)))
        assert request.get_header("Authorization") == "Bearer ordinary"
        assert request.get_header("Content-type") == "application/json"
        return Response(b'{"mode":"persistent"}')

    address = "https://example.invalid/chat/sessions/synthetic-session/mode"
    assert assistant_client.http_json(
        address, session, opener=opener, method="PUT", body={"mode": "persistent"}
    ) == {"mode": "persistent"}
    assert calls == [("PUT", address, {"mode": "persistent"})]
    for unsafe in (
        "https://example.invalid/admin/chat/sessions/synthetic-session/mode",
        "https://example.invalid/chat/sessions/../mode",
        "https://example.invalid/chat/sessions/synthetic-session/mode?token=x",
    ):
        with pytest.raises(assistant_client.ClientError):
            assistant_client.http_json(
                unsafe,
                session,
                opener=opener,
                method="PUT",
                body={"mode": "persistent"},
            )
    with pytest.raises(assistant_client.ClientError):
        assistant_client.http_json(
            address, session, opener=opener, method="PUT", body={"mode": "untrusted"}
        )
    assert len(calls) == 1


def test_e49_failures_and_timeouts_remain_in_latency_denominator():
    from tests.e2e.cli_uplift.remote import dispatcher

    module, _ = dispatcher.load("assistant_latency")
    baseline = latency_samples(10, 10, mode="baseline")
    candidate = latency_samples(7, 8)
    candidate[0]["status"] = "failed"
    candidate[0]["timings"].pop("first_useful_output")
    candidate[1]["status"] = "timeout"
    candidate[1]["timings"].clear()
    result = module.verdict(candidate, baseline)
    assert result["status"] == "FAIL"
    assert result["criteria"]["completed_turns"] is False
    assert result["criteria"]["first_useful_output"] is False
    assert result["stats"]["first"]["attempts"] == 30
    assert result["stats"]["first"]["completed"] == 28
    assert result["stats"]["first"]["failed"] == 1
    assert result["stats"]["first"]["timeouts"] == 1
    assert result["stats"]["first"]["first_useful_output"]["observed"] == 28
    assert result["stats"]["first"]["first_useful_output"]["missing"] == 2
    assert result["stats"]["first"]["first_useful_output"]["p95"] is None
    assert result["stats"]["first"]["durable_ack"]["missing"] == 1
    assert module.verdict(candidate, None)["status"] == "BLOCKED"


def test_e49_ack_and_thinking_are_not_useful_output():
    from tests.e2e.cli_uplift.remote import dispatcher

    module, _ = dispatcher.load("assistant_latency")

    class Client:
        def __init__(self, events):
            self.events = iter(events)
            self.sent = []

        def send(self, message):
            self.sent.append(message)

        def receive(self):
            event = next(self.events)
            if isinstance(event, BaseException):
                raise event
            return event

    ack = {
        "type": "acknowledged",
        "durable": True,
        "request_id": "turn",
        "session_id": "session",
    }
    client = Client([ack, {"type": "progress", "content": "thinking"}, TimeoutError()])
    times = iter((0, 1, 2))
    sample = module.measure_turn(
        client,
        "session",
        "private prompt",
        "turn",
        {"turn_kind": "first"},
        clock=lambda: next(times),
    )
    assert sample["status"] == "timeout"
    assert sample["timings"]["durable_ack"] == 1
    assert all(
        value is None
        for key, value in sample["timings"].items()
        if key != "durable_ack"
    )
    assert sample["error"] == "TimeoutError"
    assert "private prompt" not in json.dumps(sample)
    missing_ack = Client([{"type": "progress", "content": "thinking"}])
    sample = module.measure_turn(
        missing_ack,
        "session",
        "private prompt",
        "turn",
        {"turn_kind": "first"},
        clock=lambda: 0,
    )
    assert sample["status"] == "failed"
    assert sample["timings"]["first_useful_output"] is None
    isolated_timeout = Client(
        [
            ack,
            module.assistant_client.ClientError(
                "Assistant isolated response timed out"
            ),
        ]
    )
    timed = module.measure_turn(
        isolated_timeout,
        "session",
        "private prompt",
        "turn",
        {"turn_kind": "first"},
        clock=lambda: 0,
    )
    assert timed["status"] == "timeout"
    assert timed["timings"]["first_useful_output"] is None


def test_e49_component_times_and_cross_session_events_are_not_confused():
    from tests.e2e.cli_uplift.remote import dispatcher

    module, _ = dispatcher.load("assistant_latency")
    ack = {
        "type": "acknowledged",
        "durable": True,
        "request_id": "turn",
        "session_id": "session",
    }

    class Client:
        def __init__(self, events):
            self.events = iter(events)

        def send(self, message):
            assert message["request_id"] == "turn"

        def receive(self):
            return next(self.events)

    events = [
        ack,
        {"type": "queued", "request_id": "turn"},
        {"type": "image", "session_id": "session"},
        {"type": "starting"},
        {"type": "model_token"},
        {"type": "answer", "text": "useful"},
        {"type": "response", "status": "completed", "content": "final"},
    ]
    ticks = iter(range(8))
    sample = module.measure_turn(
        Client(events),
        "session",
        "private prompt",
        "turn",
        {"turn_kind": "first"},
        clock=lambda: next(ticks),
    )
    assert sample["status"] == "completed"
    assert sample["timings"]["accepted_to_scheduled"] == 1
    assert sample["timings"]["image_start"] == 3
    assert sample["timings"]["bootstrap"] == 4
    assert sample["timings"]["model_first_token"] == 5
    assert sample["timings"]["first_useful_output"] == 6
    assert sample["timings"]["completion"] == 7
    assert sample["timings"]["reconnect_recovery"] is None
    assert "private prompt" not in json.dumps(sample)
    crossed = module.measure_turn(
        Client(
            [
                ack,
                {
                    "type": "response",
                    "status": "completed",
                    "content": "another user's answer",
                    "session_id": "other",
                },
            ]
        ),
        "session",
        "private prompt",
        "turn",
        {"turn_kind": "first"},
        clock=lambda: 0,
    )
    assert crossed["status"] == "failed"
    assert crossed["timings"]["first_useful_output"] is None


def test_e49_completion_requires_nonempty_useful_answer():
    from tests.e2e.cli_uplift.remote import dispatcher

    module, _ = dispatcher.load("assistant_latency")

    class Client:
        def send(self, message):
            pass

        def receive(self):
            return self.events.pop(0)

    client = Client()
    client.events = [
        {
            "type": "acknowledged",
            "durable": True,
            "request_id": "turn",
            "session_id": "session",
        },
        {"type": "response", "status": "completed", "content": "   "},
    ]
    sample = module.measure_turn(
        client,
        "session",
        "private prompt",
        "turn",
        {"turn_kind": "follow_up"},
        clock=lambda: 0,
    )
    assert sample["status"] == "failed"
    assert sample["timings"]["first_useful_output"] is None


def test_e46_fixture_accepts_only_a2_run_owned_membership(users):
    users["a2"].update(membership_id="example-owned-member", membership_run_owned=True)
    fixtures.validate_fixture("assistant_users", users)


@pytest.mark.parametrize(
    "change",
    [
        lambda users: users.pop("a2"),
        lambda users: users["a1"].update(
            membership_id="foreign", membership_run_owned=True
        ),
        lambda users: users["a2"].update(membership_id="foreign"),
        lambda users: users["a2"].update(
            membership_id="foreign", membership_run_owned=False
        ),
        lambda users: users["b1"].update(tenant_id="tenant-a"),
        lambda users: users["a2"].update(canonical_user_id="a1-canonical"),
        lambda users: users["b1"].update(fixture_name="synthetic/assistant/a1"),
        lambda users: users["a1"].update(password="not-allowed"),
        lambda users: users["a1"].update(fixture_name="https://bad.example"),
    ],
)
def test_assistant_fixture_refuses_unsafe_identity(users, change):
    change(users)
    with pytest.raises(config.ConfigError):
        fixtures.validate_fixture("assistant_users", users)


@pytest.fixture(
    params=[
        "wss://example.invalid:not-a-port/ws",
        "wss://example.invalid:99999/ws",
        "wss://example.invalid:-1/ws",
        "wss://example.invalid:0/ws",
        "wss://example.invalid:/ws",
        "wss://[broken/ws",
        "wss://[2001:db8::1]suffix/ws",
        "wss://[2001:db8::1]:99999/ws",
        "https://example.invalid/ws",
        "wss:///ws",
        "wss://private-marker:private-marker@example.invalid/ws",
        "wss://@example.invalid/ws",
        "wss://example.invalid/ws?token=private-marker",
        "wss://example.invalid/ws#private-marker",
        "wss://example.invalid/ws?",
        "wss://example.invalid/ws#",
        "wss://exa mple.invalid/ws",
        "wss://example.invalid\n/ws",
        "\x00wss://example.invalid/ws",
        "wss://example.invalid\uff1aprivate-marker/ws",
        443,
        [],
    ]
)
def invalid_assistant_websocket_url(request):
    return request.param


def test_assistant_config_rejects_malformed_websocket_target(
    users, invalid_assistant_websocket_url
):
    from tests.unit.test_cli_uplift import config_fixture

    supplied = config_fixture(
        assistant_users=users, websocket_url=invalid_assistant_websocket_url
    )
    with pytest.raises(config.ConfigError, match="websocket_url") as failure:
        config.validate(supplied)
    assert "private-marker" not in str(failure.value)
    assert "wss://" in str(failure.value)
    assert "port" in str(failure.value)


def test_malformed_websocket_target_cannot_enable_assistant_fixtures(
    users, invalid_assistant_websocket_url
):
    supplied = {
        "assistant_users": users,
        "websocket_url": invalid_assistant_websocket_url,
    }
    assert cases.ASSISTANT_USERS not in config.fixture_classes(supplied)
    assert cases.ASSISTANT_USERS not in preflight.evaluate_fixtures(supplied)


@pytest.mark.parametrize(
    "target",
    [
        "wss://example.invalid/ws",
        "wss://example.invalid:443/ws",
        "wss://example.invalid:1/ws",
        "wss://example.invalid:65535/ws",
        "wss://example.invalid:00443/ws",
        "wss://example.invalid/@assistant",
        "wss://[2001:db8::1]/ws",
        "wss://[2001:db8::1]:8443/ws",
        "WSS://EXAMPLE.invalid/ws",
    ],
)
def test_valid_websocket_targets_enable_assistant_fixtures(users, target):
    from tests.unit.test_cli_uplift import config_fixture

    validated = config.validate(
        config_fixture(assistant_users=users, websocket_url=target)
    )
    assert validated["websocket_url"] == target
    assert cases.ASSISTANT_USERS in config.fixture_classes(validated)
    assert cases.ASSISTANT_USERS in preflight.evaluate_fixtures(validated)


@pytest.mark.parametrize("target", [None, ""])
def test_absent_websocket_target_keeps_assistant_fixtures_blocked(users, target):
    from tests.unit.test_cli_uplift import config_fixture

    validated = config.validate(
        config_fixture(assistant_users=users, websocket_url=target)
    )
    assert cases.ASSISTANT_USERS not in config.fixture_classes(validated)


def events():
    result = []
    for cursor, kind in enumerate(
        ("acknowledged", "queued", "starting", "running", "tool", "answer", "completed")
    ):
        event = {
            "cursor": cursor,
            "request_id": "request-1",
            "session_id": "session-1",
            "type": kind,
        }
        if kind == "acknowledged":
            event["durable"] = True
        if kind == "tool":
            event["tool_id"] = "tool-1"
        if kind == "answer":
            event["text"] = "synthetic answer"
        result.append(event)
    return result


def isolation_rows():
    return [
        {
            "probe": probe,
            "actor": actor,
            "target_owner": target_owner,
            "expected": expected,
            "observed": {
                "source": "sandbox_tool",
                "executed": True,
                "tool_call_id": f"call-{actor}-{probe}",
                "outcome": expected,
                "sandbox_uid": f"sandbox-{actor}",
            },
            "evidence_ref": f"event-{actor}-{probe}",
        }
        for (probe, actor), (
            target_owner,
            expected,
        ) in assistant_oracles.isolation_expectations().items()
    ]


def isolation_config(users):
    return {
        "assistant_users": users,
        "gateway_url": "https://example.invalid",
        "region": "us-east-1",
        "sts_endpoint": "https://sts.example.invalid",
    }


@pytest.fixture
def isolation_dispatch(tmp_path, monkeypatch, users):
    import importlib.util
    import tarfile
    import time

    root = tmp_path / "shipped"
    root.mkdir()
    with tarfile.open(fileobj=io.BytesIO(bundle.archive()), mode="r:gz") as archive:
        archive.extractall(root, filter="data")
    monkeypatch.syspath_prepend(str(root / "remote"))
    for name in ("assistant_isolation", "assistant_isolation_evidence"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    spec = importlib.util.spec_from_file_location(
        "e45_shipped_dispatcher", root / "remote/dispatcher.py"
    )
    dispatcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dispatcher)
    module, _ = dispatcher.load("assistant_isolation")
    assert Path(module.__file__).is_relative_to(root)
    assert Path(module.oracle.__file__).is_relative_to(root)
    assert "assistant_isolation" in bundle.purposes()
    clock = int(time.time())
    cfg = {
        **isolation_config(users),
        "evaluation_id": "e45-offline",
        "work_dir": str(tmp_path / "journal"),
        "websocket_url": "wss://example.invalid/ws",
    }
    payload = tmp_path / "e45-payload.json"
    payload.write_text(json.dumps(cfg))
    monkeypatch.setattr(module.common, "assert_owned_instance", lambda *_args: None)
    monkeypatch.setattr(
        module.assistant_baseline.common,
        "fixture_secret",
        lambda config, *_args: {
            "access_token": config["credential_secret"].split("/")[-1],
            "id_token": "synthetic-id",
            "expires_at": clock + 3600,
        },
    )
    bindings, results = {}, {}
    for index, actor in enumerate(("a1", "a2", "b1")):
        binding = {
            "nonce": str(index + 1) * 64,
            "manifest_digest": module.oracle.MANIFEST_DIGEST,
            "manifest_version": "e45-v1",
            "evaluation_id": cfg["evaluation_id"],
            "target": "qualification-test",
            "actor": actor,
            "tenant_id": users[actor]["tenant_id"],
            "user_id": users[actor]["canonical_user_id"],
            "team_id": "",
            "session_id": "session-" + actor,
            "run_id": "run-" + actor,
            "sandbox_uid": "pod-" + actor,
            "lease_generation": 1,
            "attempt": 1,
            "credential_epoch": 1,
            "image_digest": "sha256:" + "f" * 64,
            "issued_at": clock,
            "expires_at": clock + 120,
            "limits": module.oracle.LIMITS,
        }
        bindings[actor] = binding
        expected = assistant_oracles.isolation_expectations()
        receipt = {
            "source": "trusted_runtime",
            "binding": deepcopy(binding),
            "recorded_at": clock,
            "status": "completed",
            "failure": None,
            "fixture_digest": "e" * 64,
            "observations": [
                {
                    "probe": name,
                    "executed": True,
                    "outcome": expected[(name, actor)][1],
                    "exposure_digests": [],
                }
                for name in module.oracle.MANIFEST["probes"][actor]
            ],
        }
        receipt["result_digest"] = module.oracle.digest(receipt)
        results[actor] = {
            "state": "finished",
            "binding": deepcopy(binding),
            "receipt": receipt,
            "sandbox_removed": True,
        }
    state = {
        "sent": [],
        "ended": [],
        "cancelled": [],
        "cleaned": [],
        "disabled": False,
        "cleanup_failure": False,
        "admission_failure": False,
        "result_missing": False,
    }

    def http(url, session, method="GET", body=None):
        actor = session.token()
        fixture = users[actor]
        if url.endswith("/auth/me"):
            return {
                "account_type": "human",
                "is_admin": False,
                "user_id": fixture["canonical_user_id"],
                "org_id": fixture["tenant_id"],
            }
        if url.endswith("/chat/capabilities"):
            return {
                "user_id": fixture["canonical_user_id"],
                "tenant_id": fixture["tenant_id"],
                "enabled": True,
                "history_configured": True,
            }
        if "/chat/qualification?" in url:
            if state["disabled"]:
                raise module.assistant_client.ClientError(
                    "Assistant HTTP request returned 503 (chat_qualification_disabled)"
                )
            return {
                "protocol": "e45-v1",
                "manifest_digest": module.oracle.MANIFEST_DIGEST,
                "actor": actor,
                "evaluation_id": cfg["evaluation_id"],
                "target": "qualification-test",
                "image_digest": bindings[actor]["image_digest"],
            }
        if url.endswith("/qualification"):
            if state["admission_failure"]:
                raise module.assistant_client.ClientError(
                    "Assistant HTTP request returned 422 (chat_qualification_fixture_unavailable)"
                )
            return deepcopy(bindings[actor])
        if url.endswith("/result"):
            if state["result_missing"]:
                raise module.assistant_client.ClientError(
                    "Assistant HTTP request returned 503 (chat_qualification_request_unavailable)"
                )
            return deepcopy(results[actor])
        if url.endswith("/cancel"):
            state["cancelled"].append(actor)
            return {"state": "finished"}
        if url.endswith("/end"):
            state["ended"].append(actor)
            return {}
        if url.endswith("/cleanup"):
            state["cleaned"].append(actor)
            return {
                "cleanup_complete": not state["cleanup_failure"],
                "nonce": bindings[actor]["nonce"],
            }
        assert url.endswith("/chat/sessions/session-" + actor), url
        return {
            "user_id": fixture["canonical_user_id"],
            "tenant_id": fixture["tenant_id"],
            "session_id": "session-" + actor,
        }

    class Socket:
        def __init__(self, actor):
            self.actor = actor
            self.last = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def send(self, message):
            self.last = message
            state["sent"].append((self.actor, message))

        def receive(self):
            if self.last["action"] == "create-session":
                return {
                    "request_id": self.last["request_id"],
                    "session_id": "session-" + self.actor,
                }
            return {"task_id": "run-" + self.actor}

    monkeypatch.setattr(module.assistant_client, "http_json", http)
    monkeypatch.setattr(
        module.assistant_isolation_clients.assistant_client, "http_json", http
    )
    monkeypatch.setattr(
        module.assistant_client,
        "connect",
        lambda _url, session: Socket(session.token()),
    )
    monkeypatch.setattr(module.time, "time", lambda: clock)
    return dispatcher, module, cfg, payload, state, bindings, results


@pytest.mark.parametrize(
    "mutation",
    [
        "valid",
        "disabled",
        "missing",
        "unexecuted",
        "model",
        "exposure",
        "cross_success",
        "residue",
        "owner_write",
        "stale",
        "expired",
        "replayed",
        "generation",
        "fixture",
        "digest",
        "cleanup",
        "missing_request",
    ],
)
def test_e45_shipped_dispatcher_and_stage_reject_false_passes(
    isolation_dispatch, capsys, mutation
):
    dispatcher, module, cfg, payload, state, bindings, results = isolation_dispatch
    receipt = results["a2"]["receipt"]
    target = next(
        row for row in receipt["observations"] if row["probe"] == "message_read"
    )
    if mutation == "disabled":
        state["disabled"] = True
    elif mutation == "missing":
        receipt["observations"].remove(target)
    elif mutation == "unexecuted":
        target["executed"] = False
    elif mutation == "model":
        receipt["source"] = "model"
    elif mutation == "exposure":
        target = next(
            row for row in receipt["observations"] if row["probe"] == "environment"
        )
        target["exposure_digests"] = ["d" * 64]
    elif mutation == "cross_success":
        target["outcome"] = "allowed"
    elif mutation == "residue":
        next(row for row in receipt["observations"] if row["probe"] == "residue_file")[
            "outcome"
        ] = "present"
    elif mutation == "owner_write":
        receipt = results["a1"]["receipt"]
        next(row for row in receipt["observations"] if row["probe"] == "memory_write")[
            "outcome"
        ] = "denied"
    elif mutation == "stale":
        receipt["recorded_at"] -= 3600
    elif mutation == "expired":
        receipt["recorded_at"] = receipt["binding"]["expires_at"]
    elif mutation == "replayed":
        receipt["binding"]["nonce"] = bindings["a1"]["nonce"]
    elif mutation == "generation":
        receipt["binding"]["lease_generation"] += 1
    elif mutation == "fixture":
        receipt["fixture_digest"] = "c" * 64
    elif mutation == "cleanup":
        state["cleanup_failure"] = True
    elif mutation == "missing_request":
        state.update(admission_failure=True, result_missing=True)
    receipt["result_digest"] = module.oracle.digest(
        {key: value for key, value in receipt.items() if key != "result_digest"}
    )
    if mutation == "digest":
        receipt["result_digest"] = "0" * 64
    code = dispatcher.main(["assistant_isolation", str(payload)])
    result = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert code == (0 if mutation == "valid" else 1), result
    matrix = cases.new_matrix(("assistant",))
    context = {
        **stage_context(matrix),
        "evaluation_id": cfg["evaluation_id"],
        "manifest": cleanup.Manifest(
            payload.parent / "manifest.json", cfg["evaluation_id"]
        ),
    }
    stages.journeys_stage(cfg, {"journey": lambda _purpose: lambda *_args: result})(
        context
    )
    expected = (
        cases.PASSED
        if mutation == "valid"
        else cases.BLOCKED
        if mutation in {"disabled", "missing_request"}
        else cases.FAILED
    )
    assert matrix["E45"]["status"] == expected, matrix["E45"]
    assert "unimplemented" not in matrix["E45"]["detail"]
    if mutation == "valid":
        assert len(result["detail"]["probes"]) == 94
        assert matrix["E45"]["detail"]["trusted_receipts_verified"] is True
        assert len(state["sent"]) == 6
    elif mutation not in {"disabled", "missing_request", "cleanup"}:
        assert result.get("error"), result
    if mutation == "cleanup":
        assert result["detail"]["cleanup_errors"]
    if mutation == "missing_request":
        assert state["ended"] == ["a1"]
    elif mutation != "disabled":
        actors = (
            ["a1"]
            if mutation == "owner_write"
            else ["a1", "a2"]
            if mutation
            in {
                "missing",
                "unexecuted",
                "model",
                "exposure",
                "cross_success",
                "residue",
            }
            else ["a1", "a2", "b1"]
        )
        assert state["cleaned"] == actors
        assert len(result["removed"]) == (0 if mutation == "cleanup" else len(actors))


def test_e45_recovery_cleans_without_reentering_expired_admission(
    isolation_dispatch, capsys
):
    dispatcher, _module, _cfg, payload, state, _bindings, _results = isolation_dispatch
    assert dispatcher.main(["assistant_isolation", str(payload)]) == 0
    capsys.readouterr()
    state["disabled"] = True
    assert dispatcher.main(["assistant_isolation", str(payload)]) == 1
    result = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert "cannot be replayed" in result["error"]
    assert len(state["sent"]) == 6
    assert state["cleaned"] == ["a1", "a2", "b1"] * 2


def test_e45_lost_journal_refuses_new_dispatch(isolation_dispatch, capsys):
    dispatcher, _module, cfg, payload, state, _bindings, _results = isolation_dispatch
    payload.write_text(json.dumps({**cfg, "prior_dispatch": True}))
    assert dispatcher.main(["assistant_isolation", str(payload)]) == 1
    result = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert "lacks a recovery journal" in result["error"]
    assert state["sent"] == []


@pytest.mark.parametrize(
    "qualification",
    [None, {"source": "model", "text": "Access denied"}, {"probes": isolation_rows()}],
)
def test_e45_stage_refuses_success_without_trusted_receipts(users, qualification):
    matrix = cases.new_matrix(("assistant",))
    result = {
        "success": True,
        "qualification": qualification,
        "detail": {"trusted_receipts_verified": True},
    }
    stages.journeys_stage(
        {"assistant_users": users}, {"journey": lambda _purpose: lambda *_args: result}
    )(stage_context(matrix))
    assert matrix["E45"]["status"] == cases.FAILED
    assert matrix["E45"]["detail"]["oracle_error"]
    assert "unimplemented" not in matrix["E45"]["detail"]


def test_isolation_fixture_sessions_reject_missing_and_wrong_tenant(users, monkeypatch):
    config = isolation_config(users)
    monkeypatch.setattr(
        assistant_isolation_clients.baseline().common,
        "fixture_secret",
        lambda config, _env, _key: {
            "access_token": config["credential_secret"],
            "id_token": config["credential_secret"],
            "expires_at": 9999999999,
        },
    )
    sessions = assistant_isolation_clients.fixture_sessions(config)
    assert [
        sessions[actor].token() for actor in assistant_isolation_clients.ACTORS
    ] == [users[actor]["fixture_name"] for actor in assistant_isolation_clients.ACTORS]
    missing = deepcopy(users)
    del missing["b1"]
    with pytest.raises(common.RemoteError, match="B1 ordinary-user fixture"):
        assistant_isolation_clients.fixture_sessions(isolation_config(missing))
    same_tenant = deepcopy(users)
    same_tenant["b1"]["tenant_id"] = users["a1"]["tenant_id"]
    with pytest.raises(common.RemoteError, match="distinct users in two tenants"):
        assistant_isolation_clients.fixture_sessions(isolation_config(same_tenant))


def test_isolation_ordinary_clients_never_inherit_controller_credentials(
    users, monkeypatch, tmp_path
):
    config = isolation_config(users)
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "GH_TOKEN",
        "ADP_WORKLOAD_TOKEN_FILE",
    ):
        monkeypatch.setenv(name, "synthetic-controller-secret")
    environment = assistant_process.python_env(tmp_path)
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "GH_TOKEN",
        "ADP_WORKLOAD_TOKEN_FILE",
    ):
        assert name not in environment
    sessions = {
        actor: assistant_client.UserSession(
            {"access_token": f"ordinary-{actor}", "expires_at": 9999999999}
        )
        for actor in assistant_isolation_clients.ACTORS
    }
    observed = []

    def http(url, session):
        actor = session.token().removeprefix("ordinary-")
        observed.append((url, actor))
        fixture = users[actor]
        if url.endswith("/auth/me"):
            return {
                "account_type": "human",
                "is_admin": False,
                "user_id": fixture["login_user_id"],
                "org_id": fixture["tenant_id"],
            }
        return {
            "user_id": fixture["canonical_user_id"],
            "tenant_id": fixture["tenant_id"],
            "enabled": True,
            "history_configured": True,
        }

    monkeypatch.setattr(assistant_isolation_clients.assistant_client, "http_json", http)
    assistant_isolation_clients.verify_identities(config, sessions)
    assert len(observed) == 6
    assert {actor for _url, actor in observed} == set(
        assistant_isolation_clients.ACTORS
    )

    def elevated(url, session):
        if url.endswith("/auth/me"):
            return {
                "account_type": "human",
                "is_admin": True,
                "user_id": users["a1"]["login_user_id"],
                "org_id": users["a1"]["tenant_id"],
            }
        raise AssertionError("Elevated identity must be refused before chat")

    monkeypatch.setattr(
        assistant_isolation_clients.assistant_client, "http_json", elevated
    )
    with pytest.raises(
        common.RemoteError, match="A1 requires its own ordinary non-admin identity"
    ):
        assistant_isolation_clients.verify_identities(config, sessions)


def test_isolation_expired_user_token_has_no_fallback(users, monkeypatch):
    sessions = {
        actor: assistant_client.UserSession(
            {
                "access_token": f"ordinary-{actor}",
                "expires_at": 0 if actor == "a1" else 9999999999,
            }
        )
        for actor in assistant_isolation_clients.ACTORS
    }
    http_json = assistant_client.http_json

    def http(url, session):
        return http_json(
            url,
            session,
            opener=lambda *_args, **_kwargs: pytest.fail(
                "No request with an expired token"
            ),
        )

    monkeypatch.setattr(assistant_isolation_clients.assistant_client, "http_json", http)
    with pytest.raises(
        assistant_client.ClientError, match="Ordinary-user session expired"
    ):
        assistant_isolation_clients.verify_identities(isolation_config(users), sessions)


def test_isolation_oracle_accepts_full_executed_matrix():
    rows = isolation_rows()
    assert (
        assistant_oracles.isolation(rows, canary="synthetic-canary")["probe_count"]
        == len(rows)
        == 94
    )


@pytest.mark.parametrize(
    "probe",
    [
        "session_write",
        "message_write",
        "summary_write",
        "artifact_write",
        "memory_write",
    ],
)
@pytest.mark.parametrize("owner_outcome", ["missing", "denied"])
def test_isolation_oracle_requires_successful_owner_write(probe, owner_outcome):
    rows = [
        row for row in isolation_rows() if (row["probe"], row["actor"]) != (probe, "a1")
    ]
    assert {
        row["actor"]: row["observed"]["outcome"]
        for row in rows
        if row["probe"] == probe and row["actor"] in {"a2", "b1"}
    } == {"a2": "denied", "b1": "denied"}
    error = f"Missing isolation probe.*{probe}.*a1"
    if owner_outcome == "denied":
        rows.append(
            {
                "probe": probe,
                "actor": "a1",
                "target_owner": "a1",
                "expected": "allowed",
                "observed": {
                    "source": "sandbox_tool",
                    "executed": True,
                    "tool_call_id": f"owner-control-{probe}",
                    "outcome": "denied",
                },
                "evidence_ref": f"owner-control-event-{probe}",
            }
        )
        error = f"{probe}/a1 expected allowed, observed denied"
    with pytest.raises(assistant_oracles.EvidenceError, match=error):
        assistant_oracles.isolation(rows, canary="synthetic-canary")


@pytest.mark.parametrize(
    "probe,actor,outcome",
    [
        ("aws_provider", "a1", "exposed"),
        ("artifact_read", "b1", "allowed"),
        ("residue_file", "a2", "present"),
        ("session_read", "a1", "denied"),
    ],
)
def test_isolation_oracle_rejects_named_leaks_and_broken_owner_paths(
    probe, actor, outcome
):
    rows = isolation_rows()
    row = next(row for row in rows if (row["probe"], row["actor"]) == (probe, actor))
    row["observed"]["outcome"] = outcome
    with pytest.raises(assistant_oracles.EvidenceError, match=f"{probe}/{actor}"):
        assistant_oracles.isolation(rows, canary="synthetic-canary")


def test_isolation_oracle_rejects_missing_or_unexecuted_probe():
    rows = isolation_rows()
    rows.pop(0)
    with pytest.raises(
        assistant_oracles.EvidenceError, match="Missing isolation probe"
    ):
        assistant_oracles.isolation(rows, canary="synthetic-canary")
    rows = isolation_rows()
    row = next(row for row in rows if row["probe"] == "s3_read")
    row["observed"] = {
        "source": "model_refusal",
        "executed": False,
        "outcome": "denied",
    }
    with pytest.raises(
        assistant_oracles.EvidenceError, match="s3_read/a1.*no correlated"
    ):
        assistant_oracles.isolation(rows, canary="synthetic-canary")


def test_isolation_oracle_rejects_wrong_residue_sandbox_and_reused_identity():
    rows = isolation_rows()
    next(row for row in rows if (row["probe"], row["actor"]) == ("residue_file", "a2"))[
        "observed"
    ]["sandbox_uid"] = "sandbox-a1"
    with pytest.raises(assistant_oracles.EvidenceError, match="wrong sandbox"):
        assistant_oracles.isolation(rows, canary="synthetic-canary")
    rows = isolation_rows()
    next(
        row
        for row in rows
        if (row["probe"], row["actor"]) == ("sandbox_identity", "a2")
    )["observed"]["sandbox_uid"] = "sandbox-a1"
    with pytest.raises(assistant_oracles.EvidenceError, match="identities were reused"):
        assistant_oracles.isolation(rows, canary="synthetic-canary")


def test_stream_checks_order_replay_and_secret():
    assert (
        assistant_oracles.stream(
            events(), request_id="request-1", canary="synthetic-secret"
        )["last_cursor"]
        == 6
    )
    for altered in (events()[:-1], events() + [events()[0]], list(reversed(events()))):
        with pytest.raises(assistant_oracles.EvidenceError):
            assistant_oracles.stream(
                altered, request_id="request-1", canary="synthetic-secret"
            )
    repeated = events()
    extra_tool = {**repeated[4], "cursor": 5, "tool_id": "tool-2"}
    repeated.insert(5, extra_tool)
    for index, event in enumerate(repeated):
        event["cursor"] = index
    assert (
        assistant_oracles.stream(
            repeated, request_id="request-1", canary="synthetic-secret"
        )["event_count"]
        == 8
    )
    repeated[5]["tool_id"] = "tool-1"
    with pytest.raises(assistant_oracles.EvidenceError, match="duplicated"):
        assistant_oracles.stream(
            repeated, request_id="request-1", canary="synthetic-secret"
        )
    leaked = events()
    leaked[5]["text"] = "synthetic-secret"
    with pytest.raises(assistant_oracles.EvidenceError, match="leaked"):
        assistant_oracles.stream(
            leaked, request_id="request-1", canary="synthetic-secret"
        )
    foreign = events()
    foreign[4]["session_id"] = "foreign"
    with pytest.raises(assistant_oracles.EvidenceError):
        assistant_oracles.stream(
            foreign, request_id="request-1", canary="synthetic-secret"
        )


def test_empty_source_window_passes_only_with_independent_empty_fixture():
    page = {
        "status": "empty",
        "cursor": None,
        "next_cursor": None,
        "records": [],
        "citations": [],
    }
    assert (
        assistant_oracles.sources(
            [page],
            expected_ids=[],
            allowed_ids=[],
            expected_timestamps={},
            window=source_fixture()["window"],
            canary="synthetic-secret",
        )["source_count"]
        == 0
    )
    with pytest.raises(assistant_oracles.EvidenceError, match="coverage"):
        assistant_oracles.sources(
            [page],
            expected_ids=["activity-1"],
            allowed_ids=["activity-1"],
            expected_timestamps={"activity-1": "2026-01-01T10:00:00+00:00"},
            window=source_fixture()["window"],
            canary="synthetic-secret",
        )


def pages():
    return [
        {
            "status": "complete",
            "cursor": None,
            "next_cursor": "page-2",
            "records": [{"id": "activity-1", "timestamp": "2026-01-01T10:00:00+00:00"}],
            "citations": ["activity-1"],
        },
        {
            "status": "complete",
            "cursor": "page-2",
            "next_cursor": None,
            "records": [{"id": "activity-2", "timestamp": "2026-01-01T11:00:00+00:00"}],
            "citations": ["activity-2"],
        },
    ]


def source_fixture():
    return {
        "expected_timestamps": {
            "activity-1": "2026-01-01T10:00:00+00:00",
            "activity-2": "2026-01-01T11:00:00+00:00",
        },
        "window": {
            "start": "2026-01-01T10:00:00+00:00",
            "end": "2026-01-01T12:00:00+00:00",
            "timezone": "UTC",
        },
    }


def provider_fixture():
    return {
        "from": "2026-01-01T10:00:00+00:00",
        "to": "2026-01-01T12:00:00+00:00",
        "timezone": "UTC",
        "gitlab_base_url": "https://gitlab.example.invalid",
        "denied_repositories": ["org/private"],
        "coverage": {
            "github": [["available", "queried"]],
            "gitlab": [["partial", "repository_not_authorized"]],
        },
        "events": {
            "github:org/repo:commit:sha": {
                "timestamp": "2026-01-01T10:10:00+00:00",
                "provider": "github",
                "repository": "org/repo",
                "kind": "commit",
                "attribution": "human",
                "actor_id": "17",
                "source_url": "https://github.com/org/repo/commit/sha",
            },
            "github:org/repo:review:3": {
                "timestamp": "2026-01-01T10:20:00+00:00",
                "provider": "github",
                "repository": "org/repo",
                "kind": "review",
                "attribution": "bot",
                "actor_id": "99",
                "source_url": "https://github.com/org/repo/pull/3#pullrequestreview-3",
            },
            "gitlab:org/repo:comment:42": {
                "timestamp": "2026-01-01T11:00:00+00:00",
                "provider": "gitlab",
                "repository": "org/repo",
                "kind": "comment",
                "attribution": "human",
                "actor_id": "42",
                "source_url": "https://gitlab.example.invalid/org/repo/-/issues/4",
            },
        },
    }


def provider_response():
    fixture = provider_fixture()
    return {
        "status": "partial",
        "from": fixture["from"],
        "to": fixture["to"],
        "timezone": fixture["timezone"],
        "last_key": None,
        "coverage": [
            {"source": provider, "status": status, "reason": reason}
            for provider, reasons in fixture["coverage"].items()
            for status, reason in reasons
        ],
        "external_events": [
            {
                "source_id": source_id,
                "occurred_at": event["timestamp"],
                "provider": event["provider"],
                "repository": event["repository"],
                "event_kind": event["kind"],
                "attribution": event["attribution"],
                "actor_id": event["actor_id"],
                "source_url": event["source_url"],
                "human_work": event["attribution"] == "human",
            }
            for source_id, event in fixture["events"].items()
        ],
    }


def test_e44_provider_adapter_requires_independent_human_bot_and_denied_repository_evidence():
    observed = assistant_provider_sources.verify(
        provider_response(), provider_fixture(), canary="synthetic-secret"
    )
    assert {row["provider"] for row in observed["records"]} == {"github", "gitlab"}
    assert [row["attribution"] for row in observed["records"]] == [
        "human",
        "bot",
        "human",
    ]
    assert len(observed["records"]) == 3


@pytest.mark.parametrize(
    "alter",
    [
        lambda response: response["external_events"].append(
            deepcopy(response["external_events"][0])
        ),
        lambda response: response["external_events"].pop(),
        lambda response: response["external_events"][0].update(
            repository="org/private"
        ),
        lambda response: response["external_events"][0].update(event_kind="assignment"),
        lambda response: response["external_events"][1].update(human_work=True),
        lambda response: response["external_events"][0].update(actor_id="99"),
        lambda response: response["external_events"][0].update(
            source_url="https://evil.invalid/leak"
        ),
        lambda response: response["external_events"][2].update(
            source_url="https://evil.invalid/org/repo/-/issues/4"
        ),
        lambda response: response["external_events"][0].update(
            occurred_at="2026-01-01T09:59:59+00:00"
        ),
        lambda response: response["coverage"][1].update(status="available"),
        lambda response: response["coverage"].pop(),
        lambda response: response.update(last_key="more"),
        lambda response: response["external_events"][0].update(
            source_url="https://github.com/synthetic-secret"
        ),
    ],
)
def test_e44_provider_adapter_rejects_forged_or_incomplete_evidence(alter):
    response = provider_response()
    alter(response)
    with pytest.raises(assistant_oracles.EvidenceError):
        assistant_provider_sources.verify(
            response, provider_fixture(), canary="synthetic-secret"
        )


def test_e44_provider_adapter_rejects_missing_fixture_and_model_answer():
    with pytest.raises(assistant_oracles.EvidenceError):
        assistant_provider_sources.verify(
            provider_response(), None, canary="synthetic-secret"
        )
    with pytest.raises(assistant_oracles.EvidenceError):
        assistant_provider_sources.verify(
            {"text": "I saw three commits"},
            provider_fixture(),
            canary="synthetic-secret",
        )


def test_e44_provider_adapter_never_waives_unavailable_provider():
    response, fixture = provider_response(), provider_fixture()
    fixture["coverage"]["gitlab"] = [["unavailable", "provider_failure"]]
    response["coverage"][1].update(status="unavailable", reason="provider_failure")
    with pytest.raises(
        assistant_oracles.EvidenceError, match="unavailable or incomplete"
    ):
        assistant_provider_sources.verify(response, fixture, canary="synthetic-secret")


def test_e44_driver_redacts_provider_only_canary_before_reporting(capsys):
    response = provider_response()
    response["external_events"][0]["source_url"] += "?synthetic-secret"
    assert (
        common.emit(
            {
                "success": True,
                "provider_response": response,
                "canary": "synthetic-secret",
            }
        )
        == 1
    )
    delivered = json.loads(capsys.readouterr().out)
    assert delivered["success"] is False and delivered["canary_check"] == "failed"
    assert "synthetic-secret" not in json.dumps(delivered)


def test_e44_source_stage_requires_provider_response_and_controller_fixture():
    evidence = {
        "success": True,
        "pages": pages(),
        "expected_ids": ["activity-1", "activity-2"],
        "allowed_ids": ["activity-1", "activity-2"],
        "canary": "synthetic-secret",
        **source_fixture(),
    }
    for fixture, response in ((provider_fixture(), None), (None, provider_response())):
        matrix = cases.new_matrix(("assistant",))
        observed = {**evidence, "provider_response": response}
        stages.journeys_stage(
            {"assistant_provider_activity": fixture},
            {
                "journey": lambda purpose: (lambda _instance, _context: observed)
                if purpose == "assistant_sources"
                else None
            },
        )(stage_context(matrix))
        assert matrix["E44"]["status"] == cases.FAILED
        assert matrix["E44"]["detail"]["oracle_error"]


def test_sources_accept_authorized_pagination_and_empty():
    assert (
        assistant_oracles.sources(
            pages(),
            expected_ids={"activity-1", "activity-2"},
            allowed_ids={"activity-1", "activity-2"},
            canary="synthetic-secret",
            **source_fixture(),
        )["source_count"]
        == 2
    )
    assert (
        assistant_oracles.sources(
            [{"status": "empty", "cursor": None, "next_cursor": None, "records": []}],
            expected_ids=set(),
            allowed_ids=set(),
            canary="synthetic-secret",
            expected_timestamps={},
            window=source_fixture()["window"],
        )["source_count"]
        == 0
    )


@pytest.mark.parametrize(
    "alter",
    [
        lambda value: value[0]["citations"].append("incorrect"),
        lambda value: value[1]["records"][0].update(id="foreign"),
        lambda value: value[1].update(status="partial"),
        lambda value: value[1].update(next_cursor="page-3"),
        lambda value: value[0]["records"][0].update(timestamp="2026-01-01T10:00:00"),
        lambda value: value[0]["records"][0].update(id="activity-2"),
        lambda value: value[0]["records"][0].update(detail="synthetic-secret"),
    ],
)
def test_sources_refuse_wrong_or_incomplete_evidence(alter):
    value = pages()
    alter(value)
    with pytest.raises(assistant_oracles.EvidenceError):
        assistant_oracles.sources(
            value,
            expected_ids={"activity-1", "activity-2"},
            allowed_ids={"activity-1", "activity-2"},
            canary="synthetic-secret",
            **source_fixture(),
        )


@pytest.mark.parametrize(
    "alter",
    [
        pytest.param(
            lambda value: (
                value[0].update(next_cursor=None),
                value[1].update(cursor=None),
            ),
            id="pages-after-end",
        ),
        pytest.param(
            lambda value: value.insert(
                1,
                {
                    "status": "complete",
                    "cursor": "page-2",
                    "next_cursor": "page-2",
                    "records": [],
                },
            ),
            id="reused-cursor",
        ),
        pytest.param(
            lambda value: (
                value[0].update(next_cursor=""),
                value[1].update(cursor=""),
            ),
            id="blank-cursor",
        ),
        pytest.param(
            lambda value: (
                value[0].update(next_cursor=True),
                value[1].update(cursor=True),
            ),
            id="non-string-cursor",
        ),
        pytest.param(
            lambda value: value[0].update(status="empty"),
            id="empty-page-with-records",
        ),
        pytest.param(
            lambda value: value[0].update(citations={"activity-1": "not-a-list"}),
            id="non-list-citations",
        ),
        pytest.param(
            lambda value: value[0].update(records=None),
            id="non-list-records",
        ),
    ],
)
def test_source_pagination_defects_fail_oracle_and_case(alter):
    source_pages = pages()
    alter(source_pages)
    evidence = {
        "success": True,
        "pages": source_pages,
        "expected_ids": ["activity-1", "activity-2"],
        "allowed_ids": ["activity-1", "activity-2"],
        "canary": "synthetic-secret",
        **source_fixture(),
    }
    with pytest.raises(assistant_oracles.EvidenceError):
        assistant_oracles.sources(
            source_pages,
            **{
                key: value
                for key, value in evidence.items()
                if key not in {"success", "pages"}
            },
        )
    matrix = cases.new_matrix(("assistant",))
    context = {
        "document": {"instance_id": "i-synthetic"},
        "matrix": matrix,
        "transcript": [],
        "correlation": {},
        "manifest": None,
        "fault": "none",
        "record": lambda case_id, status, detail: cases.record(
            matrix, case_id, status, detail
        ),
    }
    stages.journeys_stage(
        {},
        {
            "journey": lambda purpose: (
                (lambda instance, ctx: evidence)
                if purpose == "assistant_sources"
                else None
            )
        },
    )(context)
    assert matrix["E44"]["status"] == cases.FAILED
    assert matrix["E44"]["detail"]["oracle_error"]
    assert cases.accept(matrix, ("assistant",), cleanup_ok=True)[0] != cases.PASSED


def test_sources_accept_empty_intermediate_page_with_fresh_cursor():
    source_pages = pages()
    source_pages.insert(
        1,
        {
            "status": "complete",
            "cursor": "page-2",
            "next_cursor": "page-3",
            "records": [],
            "citations": [],
        },
    )
    source_pages[2]["cursor"] = "page-3"
    observed = assistant_oracles.sources(
        source_pages,
        expected_ids={"activity-1", "activity-2"},
        allowed_ids={"activity-1", "activity-2"},
        canary="synthetic-secret",
        **source_fixture(),
    )
    assert observed["source_count"] == observed["citation_count"] == 2
    assert observed["page_count"] == 3


class Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def test_http_adapter_authenticates_without_identity_headers():
    session = assistant_client.UserSession(
        {"access_token": "ordinary", "id_token": "identity", "expires_at": 200},
        clock=lambda: 100,
    )

    def read(request, timeout):
        assert timeout == 15
        assert dict(request.header_items()) == {
            "Authorization": "Bearer ordinary",
            "Accept": "application/json",
        }
        return Response(b'{"results": []}')

    assert assistant_client.http_json(
        "https://example.invalid/assistant/history", session, opener=read
    ) == {"results": []}
    assert (
        assistant_client.websocket_url("wss://example.invalid/assistant", session)
        == "wss://example.invalid/assistant?token=identity"
    )
    with pytest.raises(assistant_client.ClientError):
        assistant_client.http_json("http://example.invalid", session, opener=read)


def test_expired_user_and_denied_http_request():
    session = assistant_client.UserSession(
        {"access_token": "old", "id_token": "old", "expires_at": 100}, clock=lambda: 101
    )
    with pytest.raises(assistant_client.ClientError, match="expired"):
        assistant_client.http_json(
            "https://example.invalid/assistant",
            session,
            opener=lambda *_args, **_kwargs: None,
        )
    session.refresh = lambda previous: {
        "access_token": "new",
        "id_token": "new",
        "expires_at": 200,
    }
    assert session.token() == "new"
    session.refresh = lambda previous: {"access_token": "stale", "expires_at": 120}
    session.tokens["expires_at"] = 100
    with pytest.raises(assistant_client.ClientError, match="refresh"):
        session.token()
    session.tokens["expires_at"] = 200

    def denied(request, timeout):
        raise HTTPError(request.full_url, 403, "denied", {}, None)

    with pytest.raises(assistant_client.ClientError, match="403"):
        assistant_client.http_json(
            "https://example.invalid/assistant", session, opener=denied
        )


class Socket:
    def settimeout(self, timeout):
        assert 0 < timeout <= 15

    def __init__(self, data=b""):
        self.data = data
        self.sent = b""
        self.closed = False

    def recv(self, length):
        result, self.data = self.data[:length], self.data[length:]
        return result

    def sendall(self, payload):
        self.sent += payload

    def close(self):
        self.closed = True


@pytest.mark.parametrize(
    "status_line",
    ["HTTP/1.1 101 Switching Protocols", "HTTP/1.1 101 Custom reason"],
)
@pytest.mark.parametrize(
    "upgrade_headers",
    [
        "Upgrade: websocket\r\nConnection: Upgrade\r\n",
        "uPgRaDe: WebSocket\r\ncOnNeCtIoN: keep-alive, Upgrade\r\n",
        "Upgrade: websocket\r\nConnection: keep-alive\r\nConnection: Upgrade\r\n",
    ],
)
def test_websocket_frames_and_upgrade(monkeypatch, status_line, upgrade_headers):
    import base64
    import hashlib

    monkeypatch.setattr(assistant_client.os, "urandom", lambda size: b"a" * size)
    key = base64.b64encode(b"a" * 16).decode()
    accept = base64.b64encode(
        hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
    ).decode()
    event = json.dumps({"type": "acknowledged"}).encode()
    sock = Socket(
        (
            status_line
            + "\r\n"
            + upgrade_headers
            + "Sec-WebSocket-Accept: "
            + accept
            + "\r\n\r\n"
        ).encode()
        + bytes([0x81, len(event)])
        + event
    )

    class TLS:
        def wrap_socket(self, connection, server_hostname):
            assert connection is sock and server_hostname == "example.invalid"
            return connection

    session = assistant_client.UserSession(
        {"id_token": "identity", "expires_at": 200}, clock=lambda: 100
    )
    ws = assistant_client.connect(
        "wss://example.invalid/assistant",
        session,
        dial=lambda *_args, **_kwargs: sock,
        tls=TLS,
    )
    assert b"?token=identity" in sock.sent
    ws.send({"action": "message", "text": "hello"})
    assert b"Authorization:" not in sock.sent and b"X-User:" not in sock.sent
    assert ws.receive() == {"type": "acknowledged"}
    ws.close()
    assert sock.closed


@pytest.mark.parametrize(
    "upgrade_headers",
    [
        "",
        "Upgrade: websocket\r\n",
        "Connection: Upgrade\r\n",
        "Upgrade: h2c\r\nConnection: Upgrade\r\n",
        "Upgrade: websocket\r\nConnection: keep-alive\r\n",
        "Upgrade: websocket\r\nConnection: not-upgrade\r\n",
        (
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Extensions: permessage-deflate\r\n"
        ),
        (
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Protocol: unexpected\r\n"
        ),
        (
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Accept: wrong\r\n"
        ),
        "Upgrade: websocket\r\nConnection: Upgrade\r\nmalformed header\r\n",
        "Upgrade: websocket\r\nConnection: Upgrade\r\nInvalid: \xff\r\n",
    ],
)
def test_websocket_rejects_invalid_upgrade(monkeypatch, upgrade_headers):
    import base64
    import hashlib

    monkeypatch.setattr(assistant_client.os, "urandom", lambda size: b"a" * size)
    key = base64.b64encode(b"a" * 16).decode()
    accept = base64.b64encode(
        hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
    ).decode()
    sock = Socket(
        (
            "HTTP/1.1 101 Switching Protocols\r\n"
            + upgrade_headers
            + "Sec-WebSocket-Accept: "
            + accept
            + "\r\n\r\n"
        ).encode()
    )

    class TLS:
        def wrap_socket(self, connection, server_hostname):
            return connection

    session = assistant_client.UserSession(
        {"id_token": "ordinary", "expires_at": 200}, clock=lambda: 100
    )
    with pytest.raises(assistant_client.ClientError, match="upgrade"):
        assistant_client.connect(
            "wss://example.invalid/ws",
            session,
            dial=lambda *_args, **_kwargs: sock,
            tls=TLS,
        )
    assert sock.closed


def test_ordinary_client_environment_has_no_controller_identity():
    inherited = {
        "PATH": "/usr/bin",
        "AWS_ACCESS_KEY_ID": "controller",
        "AWS_SESSION_TOKEN": "controller",
        "AWS_PROFILE": "admin",
        "ANTHROPIC_API_KEY": "provider",
        "ADP_INTERNAL_TOKEN": "internal",
    }
    assert assistant_client.ordinary_env("/tmp/ordinary", inherited) == {
        "PATH": "/usr/bin:/bin",
        "HOME": "/tmp/ordinary",
        "LANG": "C.UTF-8",
        "AWS_EC2_METADATA_DISABLED": "true",
    }


def test_isolated_assistant_process_never_receives_controller_credentials(monkeypatch):
    for key in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GITLAB_TOKEN",
        "ADP_INTERNAL_TOKEN",
        "LD_PRELOAD",
        "PYTHONPATH",
    ):
        monkeypatch.setenv(key, "controller-private")

    class InspectedEnvironment(Exception):
        pass

    def inspect(_argv, **kwargs):
        child = kwargs["env"]
        assert child["AWS_EC2_METADATA_DISABLED"] == "true"
        assert child["HOME"] != os.environ["HOME"]
        assert "controller-private" not in json.dumps(child)
        assert not (
            {
                "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY",
                "AWS_SESSION_TOKEN",
                "AWS_PROFILE",
                "ANTHROPIC_API_KEY",
                "OPENAI_API_KEY",
                "GITLAB_TOKEN",
                "ADP_INTERNAL_TOKEN",
                "LD_PRELOAD",
                "PYTHONPATH",
            }
            & child.keys()
        )
        raise InspectedEnvironment

    monkeypatch.setattr(assistant_process.subprocess, "Popen", inspect)
    local, peer = socket.socketpair()
    try:
        with pytest.raises(InspectedEnvironment):
            assistant_process.ProcessClient(local)
    finally:
        local.close()
        peer.close()


def test_ordinary_client_cannot_call_fault_route_or_follow_redirect():
    session = assistant_client.UserSession(
        {"access_token": "ordinary", "expires_at": 200}, clock=lambda: 100
    )
    with pytest.raises(assistant_client.ClientError, match="privileged"):
        assistant_client.http_json("https://example.invalid/internal/fault", session)

    def redirect(request, timeout):
        raise HTTPError(
            request.full_url,
            302,
            "moved",
            {"Location": "https://foreign.invalid"},
            None,
        )

    with pytest.raises(assistant_client.ClientError, match="302"):
        assistant_client.http_json(
            "https://example.invalid/history", session, opener=redirect
        )


@pytest.mark.parametrize("invalid", (False, True))
def test_assistant_stage_grades_injected_evidence_without_faking_qualification(invalid):
    matrix = cases.new_matrix(("assistant",))
    context = {
        "document": {"instance_id": "i-synthetic"},
        "matrix": matrix,
        "transcript": [],
        "correlation": {},
        "manifest": None,
        "fault": "none",
        "record": lambda case_id, status, detail: cases.record(
            matrix, case_id, status, detail
        ),
    }
    stream_events = events()
    source_pages = pages()
    if invalid:
        stream_events[3]["cursor"] = 0
        source_pages[0]["citations"] = ["wrong"]

    def resolve(purpose):
        if purpose == "assistant_stream":
            return lambda instance, ctx: {
                "success": True,
                "events": stream_events,
                "request_id": "request-1",
                "canary": "synthetic-secret",
            }
        if purpose == "assistant_sources":
            return lambda instance, ctx: {
                "success": True,
                "pages": source_pages,
                "provider_response": provider_response(),
                **source_fixture(),
                "expected_ids": {"activity-1", "activity-2"},
                "allowed_ids": {"activity-1", "activity-2"},
                "canary": "synthetic-secret",
            }
        return None

    stages.journeys_stage(
        {"assistant_provider_activity": provider_fixture()}, {"journey": resolve}
    )(context)
    for identifier in ("E43", "E44"):
        assert matrix[identifier]["status"] == (
            cases.FAILED if invalid else cases.PASSED
        )
        if invalid:
            assert matrix[identifier]["detail"]["oracle_error"]
    for identifier in ("E45", "E46", "E48", "E49"):
        assert matrix[identifier]["status"] == cases.FAILED
        assert matrix[identifier]["detail"]["unimplemented"] is True
    assert matrix["E47"]["status"] == cases.NOT_RUN
    assert cases.accept(matrix, ("assistant",), cleanup_ok=True)[0] != cases.PASSED


@pytest.mark.parametrize("case_id", ["E43", "E44"])
@pytest.mark.parametrize("initial_success", [False, True])
@pytest.mark.parametrize(
    "leak_location",
    [
        None,
        "credential",
        "nested",
        "text",
        "key",
        "jwt",
        "metadata",
        "detail",
        "transcript",
        "correlation",
    ],
)
def test_assistant_emit_rejects_raw_canary_leaks_before_grading(
    tmp_path, capsys, case_id, initial_success, leak_location
):
    canary = (
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.signature"
        if leak_location == "jwt"
        else "synthetic-secret"
    )
    observation_leaked = leak_location in {"credential", "nested", "text", "key", "jwt"}
    expected_success = initial_success and not observation_leaked
    evidence = {"success": initial_success, "canary": canary}
    if case_id == "E43":
        evidence.update(events=events(), request_id="request-1")
        observed = evidence["events"][5]
        sensitive_key = "secret"
    else:
        evidence.update(
            pages=pages(),
            expected_ids=["activity-1", "activity-2"],
            allowed_ids=["activity-1", "activity-2"],
            **source_fixture(),
        )
        observed = evidence["pages"][0]["records"][0]
        sensitive_key = "password"
    observed[sensitive_key] = "ordinary-credential"
    if leak_location == "credential":
        observed[sensitive_key] = canary
    elif leak_location == "nested":
        observed[sensitive_key] = {"records": [{"text": canary}]}
    elif leak_location in {"text", "jwt"}:
        observed["text"] = canary
    elif leak_location == "key":
        observed[canary] = "leaked key"
    if observation_leaked:
        evidence.update(
            detail={"note": canary, "other_check": True},
            transcript=[canary],
            correlation={canary: [canary]},
        )
    elif leak_location is not None:
        evidence[leak_location] = (
            [canary, {"nested": canary}]
            if leak_location == "transcript"
            else {"note": canary, canary: [canary]}
        )
    evidence["resources"] = [["adp_connection", "owned-resource"]]
    if case_id == "E44":
        evidence["provider_response"] = provider_response()
    original = json.dumps(evidence, sort_keys=True)

    exit_code = common.emit(evidence)
    emitted = capsys.readouterr().out
    delivered = json.loads(emitted)
    assert exit_code == int(not expected_success)
    assert canary not in emitted
    assert "canary" not in delivered
    assert delivered["canary_check"] == ("failed" if observation_leaked else "passed")
    assert delivered["success"] is expected_success
    assert "ordinary-credential" not in emitted
    assert delivered["resources"] == evidence["resources"]
    assert json.dumps(evidence, sort_keys=True) == original
    if observation_leaked:
        assert delivered["detail"]["other_check"] is True
        assert "Synthetic secret leaked" in delivered["detail"]["oracle_error"]

    matrix = cases.new_matrix(("assistant",))
    for identifier in matrix:
        cases.record(matrix, identifier, cases.PASSED, {})
    cases.record(matrix, case_id, cases.NOT_RUN, {})
    context = {
        "document": {"instance_id": "i-synthetic"},
        "matrix": matrix,
        "transcript": [],
        "correlation": {},
        "manifest": cleanup.Manifest(tmp_path / "inventory.json", "synthetic"),
        "fault": "none",
        "record": lambda identifier, status, detail: cases.record(
            matrix, identifier, status, detail
        ),
    }
    stages.journeys_stage(
        {"assistant_provider_activity": provider_fixture()},
        {"journey": lambda _purpose: lambda *_args: delivered},
    )(context)
    assert matrix[case_id]["status"] == (
        cases.PASSED if expected_success else cases.FAILED
    )
    assert cases.accept(matrix, ("assistant",), cleanup_ok=True)[0] == (
        cases.PASSED if expected_success else cases.FAILED
    )
    if observation_leaked:
        assert "Synthetic secret leaked" in matrix[case_id]["detail"]["oracle_error"]


@pytest.mark.parametrize("canary", [None, "", 123, "<redacted>"])
def test_assistant_emit_cannot_attest_without_a_valid_canary(capsys, canary):
    evidence = {
        "success": True,
        "canary": canary,
        "canary_check": "passed",
        "events": events(),
    }
    assert common.emit(evidence) == 1
    delivered = json.loads(capsys.readouterr().out)
    assert "canary" not in delivered
    assert delivered["success"] is False
    assert delivered["canary_check"] == "missing"
    assert "canary" in delivered["detail"]["oracle_error"]


@pytest.mark.parametrize("check", [None, "failed", "missing", True, "unknown"])
def test_assistant_oracles_require_pre_redaction_check_without_canary(check):
    with pytest.raises(assistant_oracles.EvidenceError, match="canary"):
        assistant_oracles.stream(
            events(), request_id="request-1", canary=None, canary_check=check
        )
    with pytest.raises(assistant_oracles.EvidenceError, match="canary"):
        assistant_oracles.sources(
            pages(),
            expected_ids=["activity-1", "activity-2"],
            allowed_ids=["activity-1", "activity-2"],
            canary=None,
            canary_check=check,
            **source_fixture(),
        )


@pytest.mark.parametrize("case_id", ["E43", "E44"])
def test_assistant_redacted_observations_still_require_structural_checks(
    capsys, case_id
):
    evidence = {"success": True, "canary": "synthetic-secret"}
    if case_id == "E43":
        evidence.update(events=events(), request_id="request-1")
        evidence["events"][3]["cursor"] = 0
    else:
        evidence.update(
            pages=pages(),
            expected_ids=["activity-1", "activity-2"],
            allowed_ids=["activity-1", "activity-2"],
            **source_fixture(),
        )
        evidence["pages"][0]["citations"] = ["wrong"]
    assert common.emit(evidence) == 0
    delivered = json.loads(capsys.readouterr().out)
    assert delivered.pop("canary_check") == "passed"
    delivered.pop("success")
    with pytest.raises(assistant_oracles.EvidenceError):
        if case_id == "E43":
            assistant_oracles.stream(**delivered, canary=None, canary_check="passed")
        else:
            assistant_oracles.sources(**delivered, canary=None, canary_check="passed")


def test_interrupted_assistant_matrix_remains_incomplete_after_resume():
    matrix = cases.new_matrix(("assistant",))
    cases.record(matrix, "E43", cases.PASSED, {"last_cursor": 6})
    resumed = json.loads(json.dumps(matrix))
    assert resumed["E43"]["detail"]["last_cursor"] == 6
    assert resumed["E44"]["status"] == cases.NOT_RUN
    assert cases.accept(resumed, ("assistant",), cleanup_ok=True)[0] == cases.FAILED
    for identifier in (f"E{number}" for number in range(44, 50)):
        cases.record(resumed, identifier, cases.PASSED)
    assert cases.accept(resumed, ("assistant",), cleanup_ok=False)[0] == cases.FAILED
    assert cases.is_full(("assistant",)) is False


def test_websocket_rejects_denied_upgrade_without_credential_fallback():
    sock = Socket(b"HTTP/1.1 403 Forbidden\r\n\r\n")

    class TLS:
        def wrap_socket(self, connection, server_hostname):
            return connection

    session = assistant_client.UserSession(
        {"id_token": "ordinary", "expires_at": 200}, clock=lambda: 100
    )
    with pytest.raises(assistant_client.ClientError, match="authentication"):
        assistant_client.connect(
            "wss://example.invalid/ws",
            session,
            dial=lambda *_args, **_kwargs: sock,
            tls=TLS,
        )
    assert sock.closed


def stage_context(matrix):
    return {
        "document": {"instance_id": "i-synthetic"},
        "matrix": matrix,
        "transcript": [],
        "correlation": {},
        "manifest": None,
        "fault": "none",
        "record": lambda identifier, status, detail: cases.record(
            matrix, identifier, status, detail
        ),
    }


@pytest.mark.parametrize("identifier", [None, "", "  ", 7, False, "wrong"])
def test_stream_requires_correlation_at_oracle_and_stage(identifier):
    observed = events()
    if identifier != "wrong":
        for event in observed:
            event.pop("request_id")
    with pytest.raises(assistant_oracles.EvidenceError):
        assistant_oracles.stream(
            observed, request_id=identifier, canary="synthetic-secret"
        )
    matrix = cases.new_matrix(("assistant",))
    evidence = {"success": True, "events": observed, "canary": "synthetic-secret"}
    if identifier is not None:
        evidence["request_id"] = identifier
    stages.journeys_stage({}, {"journey": lambda purpose: lambda *_args: evidence})(
        stage_context(matrix)
    )
    assert matrix["E43"]["status"] == cases.FAILED
    assert cases.accept(matrix, ("assistant",), cleanup_ok=True)[0] == cases.FAILED


@pytest.mark.parametrize(
    "stamp", ["1999-01-01T10:00:00Z", "2026-01-01T09:59:59Z", "2026-01-01T12:00:00Z"]
)
def test_sources_reject_wrong_timestamp_or_window(stamp):
    observed = pages()
    observed[0]["records"][0]["timestamp"] = stamp
    for fixture in (
        source_fixture(),
        {
            **source_fixture(),
            "expected_timestamps": {
                **source_fixture()["expected_timestamps"],
                "activity-1": stamp,
            },
        },
    ):
        with pytest.raises(assistant_oracles.EvidenceError):
            assistant_oracles.sources(
                observed,
                expected_ids={"activity-1", "activity-2"},
                allowed_ids={"activity-1", "activity-2"},
                canary="synthetic-secret",
                **fixture,
            )


def test_sources_accept_equivalent_instants_and_half_open_window():
    observed = pages()
    observed[0]["records"][0]["timestamp"] = "2026-01-01T05:00:00-05:00"
    fixture = source_fixture()
    fixture["window"].update(
        timezone="America/New_York",
        start="2026-01-01T05:00:00-05:00",
        end="2026-01-01T06:00:01-05:00",
    )
    assert (
        assistant_oracles.sources(
            observed,
            expected_ids={"activity-1", "activity-2"},
            allowed_ids={"activity-1", "activity-2"},
            canary="synthetic-secret",
            **fixture,
        )["source_count"]
        == 2
    )


def frame(opcode, payload=b"", final=True):
    header = bytes([(0x80 if final else 0) | opcode])
    return (
        header
        + (
            bytes([len(payload)])
            if len(payload) < 126
            else b"\x7e" + struct.pack("!H", len(payload))
        )
        + payload
    )


def test_websocket_interleaved_controls_and_continuations():
    sock = Socket(
        frame(9, b"before")
        + frame(1, b'{"text":"', False)
        + frame(10, b"pong")
        + frame(9, b"middle")
        + frame(0, "hello ☃".encode()[:7], False)
        + frame(0, "hello ☃".encode()[7:] + b'"}')
    )
    assert assistant_client.WebSocket(sock).receive() == {"text": "hello ☃"}
    assert sock.sent[0] == 0x8A
    assert sock.sent[1] & 0x80


@pytest.mark.parametrize(
    "wire",
    [
        frame(0, b"{}"),
        frame(1, b"{", False) + frame(1, b"}"),
        frame(9, b"ping", False),
        frame(9, b"x" * 126),
        frame(2, b"{}"),
        b"\xc1\x02{}",
        b"\x81\x82{}",
        frame(8, b"x"),
        frame(8, b"\x03\xed"),
        frame(1, b"x" * 40000, False) + frame(0, b"x" * 30000),
        frame(1, b"\xff"),
        frame(1, b"invalid"),
        frame(1, b"{", False),
    ],
)
def test_websocket_rejects_malformed_or_unbounded_frames(wire):
    with pytest.raises(assistant_client.ClientError):
        assistant_client.WebSocket(Socket(wire)).receive()


def test_websocket_close_is_not_an_application_event():
    sock = Socket(frame(8, struct.pack("!H", 1000)))
    with pytest.raises(assistant_client.ClientError, match="closed before"):
        assistant_client.WebSocket(sock).receive()
    assert sock.closed and sock.sent[0] == 0x88


def test_websocket_controls_do_not_extend_deadline(monkeypatch):
    clock = iter([0, 1, 2, 16])
    monkeypatch.setattr(assistant_client.time, "monotonic", lambda: next(clock))
    with pytest.raises(assistant_client.ClientError, match="timed out"):
        assistant_client.WebSocket(Socket(frame(9, b"a") + frame(1, b"{}"))).receive()


def test_assistant_bindings_reach_shipped_dispatcher(
    tmp_path, users, monkeypatch, capsys
):
    from tests.e2e.cli_uplift.remote import dispatcher
    from tests.unit.test_cli_uplift import write_config

    cfg = config.load(
        write_config(
            tmp_path, assistant_users=users, websocket_url="wss://example.invalid/ws"
        )
    )
    cfg["assistant_latency"] = {"enabled": True}
    payload = live._journey_payload(
        cfg,
        {
            "document": {
                "instance_id": "i-synthetic",
                "session": {"access_token": "controller-secret"},
            },
            "evaluation_id": "synthetic-run",
            "preflight": {},
            "fault": "assistant_worker_loss",
            "assistant_fault_target": {"session_id": "synthetic-session"},
        },
    )
    path = tmp_path / "payload.json"
    path.write_text(json.dumps(payload))
    received = []

    class Driver:
        @staticmethod
        def execute(config, evidence):
            received.append(config)
            evidence["success"] = True

    import common

    monkeypatch.setattr(common, "assert_owned_instance", lambda cfg: None)
    monkeypatch.setattr(dispatcher, "load", lambda purpose: (Driver, {}))
    assert dispatcher.main(["assistant_stream", str(path)]) == 0
    assert received[0]["assistant_users"] == users
    assert received[0]["assistant_latency"] == {"enabled": True}
    assert received[0]["fault"] == "assistant_worker_loss"
    assert received[0]["fault_target"] == {"session_id": "synthetic-session"}
    assert received[0]["websocket_url"] == "wss://example.invalid/ws"
    assert "controller-secret" not in path.read_text()
    assert "remote/assistant_process.py" in dict(bundle.sources())


def test_actual_subprocess_cannot_read_controller_or_metadata(tmp_path, monkeypatch):
    private = tmp_path / "controller-session.json"
    private.write_text("synthetic-controller-secret")
    private.chmod(0o644)
    inherited = os.open(private, os.O_RDONLY)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "controller")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "controller")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "provider")
    script = """
import io, json, os, socket, sys
from tests.e2e.cli_uplift.remote import assistant_client, assistant_process
direct = socket.socket()
assistant_process.restrict()
for name in ("AWS_ACCESS_KEY_ID", "AWS_SESSION_TOKEN", "ANTHROPIC_API_KEY"):
    assert name not in os.environ
for path in (sys.argv[1], "/proc/self/environ", "/proc/" + sys.argv[2] + "/environ"):
    try:
        open(path).read()
    except PermissionError:
        pass
    else:
        raise AssertionError("controller files accessible")
try:
    direct.connect(("169.254.169.254", 80))
except PermissionError:
    pass
else:
    raise AssertionError("metadata accessible")
try:
    socket.socket()
except PermissionError:
    pass
else:
    raise AssertionError("new socket permitted")
try:
    os.read(int(sys.argv[3]), 1)
except OSError:
    pass
else:
    raise AssertionError("controller descriptor inherited")
class Response(io.BytesIO):
    status = 200
def opener(request, timeout):
    assert request.get_header("Authorization") == "Bearer ordinary"
    assert request.get_header("X-User") is None
    return Response(b'{"authorized":true}')
session = assistant_client.UserSession({"access_token":"ordinary", "expires_at":4000000000})
assert assistant_client.http_json("https://example.invalid/history", session, opener=opener) == {"authorized":True}
try:
    assistant_client.http_json("https://example.invalid/internal/fault", session, opener=opener)
except assistant_client.ClientError:
    pass
else:
    raise AssertionError("fault permitted")
print("isolated and authenticated")
"""
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(private),
                str(os.getpid()),
                str(inherited),
            ],
            env=assistant_process.python_env(tmp_path),
            capture_output=True,
            text=True,
            timeout=15,
            close_fds=True,
            check=False,
        )
    finally:
        os.close(inherited)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "isolated and authenticated"


def test_production_process_requires_confinement_before_tokens():
    parent, child = socket.socketpair()
    try:
        with assistant_process.ProcessClient(child) as client:
            assert client.process.pid != os.getpid()
            with pytest.raises(
                assistant_client.ClientError,
                match="Invalid assistant isolated operation",
            ):
                client.call("fault")
    finally:
        parent.close()
        child.close()


def test_isolation_failure_has_no_unrestricted_fallback(monkeypatch):
    original_popen = subprocess.Popen
    bootstrap = (
        f"import sys; sys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r}); "
        "from tests.e2e.cli_uplift.remote import assistant_process; "
        "assistant_process.restrict = lambda: sys.exit(17); assistant_process.worker()"
    )
    processes = []

    def launch(argv, **kwargs):
        process = original_popen([argv[0], "-I", "-c", bootstrap, argv[-1]], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(assistant_process.subprocess, "Popen", launch)
    parent, child = socket.socketpair()
    try:
        with pytest.raises(
            assistant_client.ClientError, match="libseccomp is required"
        ):
            assistant_process.ProcessClient(child)
    finally:
        parent.close()
        child.close()
    assert len(processes) == 1
    assert processes[0].poll() is not None


def test_default_adapters_use_isolated_transport(monkeypatch):
    calls = []

    class Isolated:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def call(self, action, **kwargs):
            calls.append(action)
            assert kwargs == {}
            return {"authorized": True}

    def start(url, session, mode):
        calls.append((url, mode))
        return Isolated()

    monkeypatch.setattr(assistant_process, "start", start)
    session = assistant_client.UserSession(
        {"access_token": "ordinary", "expires_at": 4000000000}
    )
    assert assistant_client.http_json("https://example.invalid/history", session) == {
        "authorized": True
    }
    assert isinstance(
        assistant_client.connect("wss://example.invalid/ws", session), Isolated
    )
    assert calls == [
        ("https://example.invalid/history", "http"),
        "http",
        ("wss://example.invalid/ws", "websocket"),
    ]


@pytest.mark.parametrize(
    "address",
    ["169.254.169.254", "fd00:ec2::254", "127.0.0.1", "::ffff:169.254.169.254"],
)
def test_parent_cannot_delegate_metadata_or_local_socket(monkeypatch, address):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", (address, 443))
        ],
    )
    session = assistant_client.UserSession(
        {"access_token": "ordinary", "expires_at": 4000000000}
    )
    with pytest.raises(
        assistant_client.ClientError, match="metadata or a local service"
    ):
        assistant_client.http_json("https://example.invalid/history", session)


@pytest.mark.parametrize(
    "method,body,expected_status,path",
    [
        ("PUT", {"mode": "persistent"}, 200, "/chat/sessions/synthetic-session/mode"),
        ("PUT", {"mode": "ephemeral"}, 200, "/chat/sessions/synthetic-session/mode"),
        ("POST", None, 202, "/chat/sessions/synthetic-session/end"),
    ],
)
def test_http_session_writes_reach_isolated_transport(
    monkeypatch, method, body, expected_status, path
):
    calls = []
    session = assistant_client.UserSession(
        {"access_token": "ordinary", "expires_at": 4000000000}
    )

    class Isolated:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def call(self, action, **kwargs):
            calls.append((action, kwargs))
            return {"accepted": True}

    def start(url, user, mode):
        assert url == "https://example.invalid" + path
        assert user is session
        assert mode == "http"
        return Isolated()

    monkeypatch.setattr(assistant_process, "start", start)
    assert assistant_client.http_json(
        "https://example.invalid" + path,
        session,
        method=method,
        body=body,
        expected_status=expected_status,
    ) == {"accepted": True}
    assert calls == [
        (
            "http",
            {"method": method, "document": body, "expected_status": expected_status},
        )
    ]


def test_canary_redaction_preserves_json_types():
    assert assistant_oracles.redact_canary({"text": ["n", None, True, 10]}, "n") == {
        "text": ["<redacted>", None, True, 10]
    }


@pytest.mark.parametrize(
    "mode,denied,invalid_upgrade,method,body,expected_status",
    [
        ("http", False, False, "GET", None, 200),
        ("http", False, False, "PUT", {"mode": "persistent"}, 200),
        ("http", False, False, "PUT", {"mode": "ephemeral"}, 200),
        ("http", False, False, "POST", None, 202),
        ("http", True, False, "GET", None, 200),
        ("websocket", False, False, "GET", None, 200),
        ("websocket", True, False, "GET", None, 200),
        ("websocket", False, True, "GET", None, 200),
    ],
)
def test_isolated_production_worker_authenticates_over_tls_socketpair(
    tmp_path, monkeypatch, mode, denied, invalid_upgrade, method, body, expected_status
):
    import base64
    import hashlib
    import ssl

    certificate, key = tmp_path / "certificate.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
            "-days",
            "1",
            "-subj",
            "/CN=example.invalid",
            "-addext",
            "subjectAltName=DNS:example.invalid",
        ],
        check=True,
        capture_output=True,
    )
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(certificate, key)
    parent, child = socket.socketpair()
    parent.settimeout(5)
    child.settimeout(5)
    requests = []
    errors = []
    target_path = {
        "GET": "/history",
        "PUT": "/chat/sessions/synthetic-session/mode",
        "POST": "/chat/sessions/synthetic-session/end",
    }[method]

    def serve():
        try:
            with server_context.wrap_socket(parent, server_side=True) as connection:
                request = bytearray()
                while not request.endswith(b"\r\n\r\n"):
                    chunk = connection.recv(1)
                    if not chunk:
                        raise AssertionError("client disconnected")
                    request.extend(chunk)
                if body is not None:
                    content_length = next(
                        int(line.split(":", 1)[1])
                        for line in request.decode().split("\r\n")
                        if line.startswith("Content-Length:")
                    )
                    while content_length:
                        chunk = connection.recv(content_length)
                        if not chunk:
                            raise AssertionError("client disconnected during body")
                        request.extend(chunk)
                        content_length -= len(chunk)
                requests.append(bytes(request))
                if denied:
                    connection.sendall(
                        b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n"
                    )
                elif mode == "http":
                    connection.sendall(
                        f"HTTP/1.1 {expected_status} OK\r\n".encode()
                        + b'Content-Length: 19\r\n\r\n{"authorized":true}'
                    )
                else:
                    header = next(
                        line
                        for line in request.decode().split("\r\n")
                        if line.startswith("Sec-WebSocket-Key:")
                    )
                    accept = base64.b64encode(
                        hashlib.sha1(
                            (
                                header.split(":", 1)[1].strip()
                                + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
                            ).encode()
                        ).digest()
                    )
                    connection.sendall(
                        b"HTTP/1.1 101 Switching Protocols\r\n"
                        + (
                            b""
                            if invalid_upgrade
                            else b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                        )
                        + b"Sec-WebSocket-Accept: "
                        + accept
                        + b"\r\n\r\n"
                        + frame(1, b'{"authorized":', False)
                        + frame(9, b"ping")
                        + frame(0, b"true}")
                    )
                    if not invalid_upgrade:
                        assert connection.recv(128)[0] == 0x8A
        except (OSError, AssertionError) as exc:
            errors.append(exc)

    original_popen = subprocess.Popen
    bootstrap = f"import sys; sys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r}); from tests.e2e.cli_uplift.remote import assistant_process; original = assistant_process.ssl.create_default_context; assistant_process.ssl.create_default_context = lambda: original(cafile={str(certificate)!r}); assistant_process.worker()"

    def launch(argv, **kwargs):
        return original_popen([argv[0], "-I", "-c", bootstrap, argv[-1]], **kwargs)

    monkeypatch.setattr(assistant_process.subprocess, "Popen", launch)
    server = threading.Thread(target=serve, daemon=True)
    server.start()
    try:
        with assistant_process.ProcessClient(child) as client:
            initialize = {
                "url": ("https" if mode == "http" else "wss")
                + "://example.invalid"
                + target_path,
                "mode": mode,
                "tokens": {
                    "access_token": "ordinary-access",
                    "id_token": "ordinary-id",
                    "expires_at": 4000000000,
                },
            }
            if (denied or invalid_upgrade) and mode == "websocket":
                with pytest.raises(
                    assistant_client.ClientError, match="authentication"
                ):
                    client.call("initialize", **initialize)
            else:
                client.call("initialize", **initialize)
                if denied:
                    with pytest.raises(assistant_client.ClientError, match="403"):
                        client.call("http")
                else:
                    arguments = (
                        {
                            "method": method,
                            "document": body,
                            "expected_status": expected_status,
                        }
                        if mode == "http"
                        else {}
                    )
                    assert client.call(
                        "http" if mode == "http" else "receive", **arguments
                    ) == {"authorized": True}
    finally:
        child.close()
        server.join(6)
        parent.close()
    assert not server.is_alive() and not errors, errors
    assert len(requests) == 1
    assert b"X-User" not in requests[0]
    if mode == "http":
        assert requests[0].startswith(f"{method} {target_path} HTTP/1.1\r\n".encode())
        document = requests[0].split(b"\r\n\r\n", 1)[1]
        assert (json.loads(document) if document else None) == body
    assert (
        b"Authorization: Bearer ordinary-access"
        if mode == "http"
        else b"?token=ordinary-id"
    ) in requests[0]


@pytest.mark.parametrize("case_id", ["E43", "E44"])
@pytest.mark.parametrize("outcome", ["passed", "driver_failed", "oracle_failed"])
def test_assistant_redacts_evidence_before_state_and_report(
    tmp_path, users, capsys, case_id, outcome
):
    from tests.unit.test_cli_uplift import NOW, FakeS3, write_config

    cfg_path = write_config(
        tmp_path,
        assistant_users=users,
        websocket_url="wss://example.invalid/ws",
        assistant_provider_activity=provider_fixture(),
    )
    store = statestore.S3StateStore(FakeS3(), "synthetic-state", clock=lambda: NOW)
    canary = "synthetic-canary"
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.signature"
    evidence = {
        "success": outcome != "driver_failed",
        "events": events(),
        "request_id": "request-1",
        "pages": pages(),
        "provider_response": provider_response(),
        "expected_ids": ["activity-1", "activity-2"],
        "allowed_ids": ["activity-1", "activity-2"],
        **source_fixture(),
        "canary": canary,
        "detail": {"note": canary, "access_token": "ordinary-secret"},
        "transcript": [f"observation {canary} {jwt}"],
        "correlation": {
            "request_id": "request-1",
            "nested": {canary: [canary, jwt], "access_token": "ordinary-secret"},
        },
    }
    if outcome == "oracle_failed":
        evidence["events"][5]["text"] = canary
        evidence["pages"][0]["records"][0]["text"] = canary

    def resolve(purpose):
        if purpose == stages.JOURNEY_DRIVERS[case_id]:
            return lambda _instance, _ctx: evidence
        return None

    stage_map = {name: lambda _ctx: None for name in runner.STAGES}
    stage_map.update(
        ec2=lambda ctx: ctx["document"].update(instance_id="i-synthetic"),
        journeys=stages.journeys_stage(
            {"assistant_provider_activity": provider_fixture()}, {"journey": resolve}
        ),
        cleanup=lambda _ctx: True,
    )
    state_dir = tmp_path / "run"
    assert (
        runner.main(
            [
                "--mode",
                "start",
                "--config",
                cfg_path,
                "--suite",
                "assistant",
                "--state-dir",
                str(state_dir),
            ],
            stages=stage_map,
            clock=lambda: NOW,
            store=store,
        )
        == 1
    )
    local = runner.State(state_dir).read()
    saved, _inventory = store.load(local["evaluation_id"])
    artifact = json.loads((state_dir / "out" / "report.json").read_text())
    for document in (local, saved, artifact):
        serialized = json.dumps(document)
        assert all(
            value not in serialized for value in (canary, jwt, "ordinary-secret")
        )
        assert document["transcript"] == [
            "observation <redacted> [REDACTED]",
        ]
        assert document["correlation"] == {
            "request_id": "request-1",
            "nested": {"<redacted>": ["<redacted>", "[REDACTED]"]},
        }
    for document in (local, saved):
        entry = document["matrix"][case_id]
        assert entry["status"] == (
            cases.PASSED if outcome == "passed" else cases.FAILED
        )
        if outcome == "oracle_failed":
            assert "Synthetic secret leaked" in entry["detail"]["oracle_error"]
        elif outcome == "passed":
            assert entry["detail"]["assistant_observations"]
    assert artifact["status"] == cases.FAILED
    assert artifact["full_acceptance"] is False
    assert canary in evidence["transcript"][0]
    assert evidence["correlation"]["nested"][canary] == [canary, jwt]
    capsys.readouterr()


def test_assistant_interrupt_restore_resume_and_repeated_cleanup(
    tmp_path, users, capsys
):
    from tests.unit.test_cli_uplift import NOW, FakeS3, write_config

    cfg_path = write_config(
        tmp_path,
        assistant_users=users,
        websocket_url="wss://example.invalid/ws",
        assistant_provider_activity=provider_fixture(),
    )
    store = statestore.S3StateStore(FakeS3(), "synthetic-state", clock=lambda: NOW)
    interrupted = True
    calls = []
    deletions = []

    def prepare(ctx):
        instance = ctx["evaluation_id"] + "-instance"
        ctx["document"]["instance_id"] = instance
        ctx["manifest"].record("ec2_instance", instance)
        ctx["manifest"].record("secret", users["a1"]["fixture_name"], reused=True)

    def resolve(purpose):
        if purpose not in {"assistant_stream", "assistant_sources"}:
            return None

        def run(_instance, ctx):
            calls.append(purpose)
            if purpose == "assistant_stream":
                return {
                    "success": True,
                    "events": events(),
                    "request_id": "request-1",
                    "canary": "synthetic-secret",
                    "detail": {
                        "note": "synthetic-secret",
                        "access_token": "ordinary-secret",
                    },
                    "transcript": ["request-1 synthetic-secret"],
                    "correlation": {
                        "note": "synthetic-secret",
                        "access_token": "ordinary-secret",
                    },
                }
            if interrupted:
                raise KeyboardInterrupt("synthetic interruption")
            return {
                "success": True,
                "pages": pages(),
                "provider_response": provider_response(),
                "expected_ids": ["activity-1", "activity-2"],
                "allowed_ids": ["activity-1", "activity-2"],
                "canary": "synthetic-secret",
                **source_fixture(),
            }

        return run

    def remove(identifier):
        deletions.append(identifier)
        if len(deletions) == 1:
            raise RuntimeError("synthetic-secret")

    stage_map = {name: lambda _ctx: None for name in runner.STAGES}
    stage_map.update(
        ec2=prepare,
        journeys=stages.journeys_stage(
            {"assistant_provider_activity": provider_fixture()}, {"journey": resolve}
        ),
        cleanup=lambda ctx: cleanup.sweep(ctx["manifest"], {"ec2_instance": remove})[0],
    )

    def invoke(mode, directory, extra=()):
        return runner.main(
            [
                "--mode",
                mode,
                "--config",
                cfg_path,
                "--suite",
                "assistant",
                "--state-dir",
                str(directory),
                *extra,
            ],
            stages=stage_map,
            clock=lambda: NOW,
            store=store,
        )

    first_dir = tmp_path / "first"
    with pytest.raises(KeyboardInterrupt):
        invoke("start", first_dir)
    local = runner.State(first_dir).read()
    evaluation_id = local["evaluation_id"]
    saved, inventory = store.load(evaluation_id)
    assert saved["matrix"]["E43"]["status"] == cases.PASSED
    assert saved["matrix"]["E44"]["status"] == cases.NOT_RUN
    observations = saved["matrix"]["E43"]["detail"]["assistant_observations"]
    assert observations["request_id"] == "request-1"
    assert observations["events"][4] == {
        "cursor": 4,
        "type": "tool",
        "tool_id": "tool-1",
    }
    assert not deletions
    assert {entry["id"] for entry in inventory["resources"]} == {
        evaluation_id + "-instance",
        users["a1"]["fixture_name"],
    }
    assert "synthetic-secret" not in json.dumps(saved)
    assert "ordinary-secret" not in json.dumps(saved)

    interrupted = False
    resumed_dir = tmp_path / "restored"
    assert (
        invoke("resume", resumed_dir, ("--restore", "--evaluation-id", evaluation_id))
        == 1
    )
    resumed = runner.State(resumed_dir).read()
    assert resumed["attempt"] == 2
    assert calls.count("assistant_stream") == 1
    assert resumed["transcript"] == ["request-1 <redacted>"]
    assert resumed["correlation"] == {"note": "<redacted>"}
    assert resumed["matrix"]["E43"]["detail"]["assistant_observations"] == observations
    assert resumed["matrix"]["E44"]["status"] == cases.PASSED
    assert resumed["cleanup_ok"] is False
    assert resumed["cleanup_outstanding"] == [
        "ec2_instance:" + evaluation_id + "-instance"
    ]
    artifact = (resumed_dir / "out" / "report.json").read_text()
    assert "request-1" in artifact and "activity-1" in artifact
    assert "2026-01-01T10:00:00+00:00" in artifact
    assert "synthetic-secret" not in artifact and "ordinary-secret" not in artifact
    assert json.loads(artifact)["full_acceptance"] is False
    assert invoke("cleanup", resumed_dir) == 0
    assert runner.State(resumed_dir).read()["cleanup_outstanding"] == []
    assert invoke("cleanup", resumed_dir) == 0
    assert deletions == [evaluation_id + "-instance"] * 2
    _saved, inventory = store.load(evaluation_id)
    assert [entry["status"] for entry in inventory["resources"]] == [
        cleanup.DELETED,
        cleanup.REUSED,
    ]


def test_isolated_python_uses_its_own_runtime_not_inherited_loader_paths(
    tmp_path, monkeypatch
):
    import sysconfig

    monkeypatch.setenv("LD_LIBRARY_PATH", "/untrusted/runtime")
    monkeypatch.setenv("LD_PRELOAD", "/untrusted/injection.so")
    monkeypatch.setenv("PYTHONPATH", "/untrusted/modules")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "controller-secret")
    env = assistant_process.python_env(tmp_path)
    assert not {"LD_PRELOAD", "PYTHONPATH", "AWS_SESSION_TOKEN"} & env.keys()
    if sysconfig.get_config_var("Py_ENABLE_SHARED"):
        assert (
            Path(env["LD_LIBRARY_PATH"]) / sysconfig.get_config_var("LDLIBRARY")
        ).is_file()
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import ctypes, ssl; print('native imports work')",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "native imports work"


@pytest.fixture
def baseline_dispatch(tmp_path, monkeypatch, users, capsys):
    from tests.e2e.cli_uplift.remote import dispatcher

    module, _ = dispatcher.load("assistant_baseline")
    tokens = {
        "access_token": "ordinary-access",
        "id_token": "ordinary-id",
        "expires_at": 4000000000,
    }
    identity = {"user_id": users["a1"]["canonical_user_id"], "tenant_id": "tenant-a"}
    state = {
        "sends": [],
        "closed": 0,
        "identity": identity,
        "admin": False,
        "answer": "ready",
        "fail": None,
        "created": None,
        "chunks": None,
        "lifecycle": ["RUN_STARTED", "RUN_FINISHED"],
    }
    cfg = {
        "evaluation_id": "baseline-test",
        "region": "us-east-1",
        "sts_endpoint": "https://sts.example.invalid",
        "work_dir": str(tmp_path),
        "gateway_url": "https://example.invalid",
        "websocket_url": "wss://example.invalid/ws",
        "assistant_users": users,
    }
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps(cfg))
    monkeypatch.setattr(module.common, "assert_owned_instance", lambda config: None)

    def secret(config, env, key):
        assert config["credential_secret"] == users["a1"]["fixture_name"]
        assert key == "ordinary_session"
        return tokens

    monkeypatch.setattr(module.common, "fixture_secret", secret)

    def http(url, session):
        assert session.token() == "ordinary-access"
        if state["fail"] == "auth":
            raise module.assistant_client.ClientError(
                "Assistant HTTP request returned 401"
            )
        if url.endswith("/auth/me"):
            return {
                "user_id": "a1",
                "org_id": "tenant-a",
                "account_type": "human",
                "is_admin": state["admin"],
            }
        if url.endswith("/chat/capabilities"):
            return {**state["identity"], "enabled": True, "history_configured": True}
        assert url.endswith("/chat/sessions/server-session")
        if state["fail"] == "ownership":
            raise module.assistant_client.ClientError(
                "Assistant HTTP request returned 403"
            )
        messages = []
        if len(state["sends"]) == 2:
            messages = [
                {"role": "user", "content": module.PROMPT},
                {"role": "assistant", "content": state["answer"], "task_id": "task-1"},
            ]
        return {**identity, "session_id": "server-session", "messages": messages}

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            state["closed"] += 1

        def send(self, value):
            state["sends"].append(value)
            if state["fail"] == "interrupted" and value["action"] == "message":
                raise KeyboardInterrupt("lost connection after sending")

        def receive(self):
            if len(state["sends"]) == 1:
                if state["created"] is not None:
                    return state["created"]
                return {
                    "request_id": state["sends"][0]["request_id"],
                    "session_id": "server-session",
                }
            if state["lifecycle"]:
                assert state["sends"][-1]["persona"] == "intent-refinement"
                return {
                    "type": "ag_ui",
                    "task_id": "task-1",
                    "event": {
                        "event_type": state["lifecycle"].pop(0),
                        "threadId": "server-session",
                        "runId": "task-1",
                    },
                }
            if state["chunks"] is not None:
                return state["chunks"].pop(0)
            return {
                "type": "response",
                "task_id": "task-1",
                "status": "completed",
                "content": state["answer"],
            }

    monkeypatch.setattr(module.assistant_client, "http_json", http)
    monkeypatch.setattr(
        module.assistant_client, "connect", lambda url, session: Client()
    )

    def run():
        code = dispatcher.main(["assistant_baseline", str(payload)])
        return code, json.loads(capsys.readouterr().out.splitlines()[-1])

    return run, state, module, tokens


def test_registered_baseline_dispatch_and_resume(baseline_dispatch, tmp_path):
    run, state, module, _ = baseline_dispatch
    code, evidence = run()
    assert code == 0 and evidence["success"] is True, evidence
    detail = evidence["detail"]
    assert detail["history_verified"] is True and detail["phase"] == "completed"
    assert state["closed"] == 1
    assert [value["action"] for value in state["sends"]] == [
        "create-session",
        "message",
    ]
    assert "ordinary-access" not in json.dumps(evidence)
    assert "ordinary-id" not in json.dumps(evidence)
    assert (
        state["answer"]
        not in next(tmp_path.glob("assistant-baseline-*.json")).read_text()
    )
    code, resumed = run()
    assert code == 0 and resumed["detail"]["resumed"] is True
    assert len(state["sends"]) == 2
    assert "assistant_baseline" in bundle.purposes()
    matrix = cases.new_matrix(("assistant",))
    stages.journeys_stage(
        {},
        {
            "journey": lambda purpose: (lambda *_args: evidence)
            if purpose == "assistant_baseline"
            else None
        },
    )(stage_context(matrix))
    assert matrix["E50"]["status"] == cases.PASSED
    assert matrix["E43"]["status"] == cases.FAILED
    assert cases.accept(matrix, ("assistant",), cleanup_ok=True)[0] != cases.PASSED


@pytest.mark.parametrize(
    "purpose,mutation",
    [
        ("assistant_stream", "valid"),
        ("assistant_stream", "reordered"),
        ("assistant_stream", "duplicated"),
        ("assistant_stream", "missing"),
        ("assistant_stream", "repeated_cursor"),
        ("assistant_stream", "leaked_canary"),
        ("assistant_stream", "answer_canary"),
        ("assistant_stream", "paginated_replay"),
        ("assistant_stream", "unsupported_envelope"),
        ("assistant_stream", "missing_ack"),
        ("assistant_stream", "missing_history"),
        ("assistant_sources", "valid"),
        ("assistant_sources", "paginated"),
        ("assistant_sources", "wrong_citation"),
        ("assistant_sources", "unauthorized"),
        ("assistant_sources", "missing_source"),
        ("assistant_sources", "incomplete"),
        ("assistant_sources", "denied_provider"),
        ("assistant_sources", "missing_provider"),
        ("assistant_sources", "wrong_timestamp"),
        ("assistant_sources", "wrong_answer"),
        ("assistant_sources", "missing_capability"),
        ("assistant_sources", "legacy_ag_ui"),
        ("assistant_sources", "leaked_canary"),
        ("assistant_sources", "answer_canary"),
    ],
)
def test_assistant_purposes_ship_and_dispatch(
    tmp_path, monkeypatch, users, capsys, purpose, mutation
):
    import tarfile

    from tests.e2e.cli_uplift.remote import dispatcher

    module, _ = dispatcher.load(purpose)
    assert purpose in bundle.purposes()
    with tarfile.open(fileobj=io.BytesIO(bundle.archive()), mode="r:gz") as archive:
        assert "remote/" + purpose + ".py" in archive.getnames()
    monkeypatch.setattr(module.common, "assert_owned_instance", lambda config: None)
    monkeypatch.setattr(
        module.common,
        "fixture_secret",
        lambda *_args: {
            "access_token": "ordinary-access",
            "id_token": "ordinary-id",
            "expires_at": 4000000000,
        },
    )
    cfg = {
        "evaluation_id": "assistant-dispatch",
        "region": "us-east-1",
        "sts_endpoint": "https://sts.example.invalid",
        "work_dir": str(tmp_path),
        "gateway_url": "https://example.invalid",
        "websocket_url": "wss://example.invalid/ws",
        "assistant_users": users,
        "assistant_provider_activity": provider_fixture(),
    }
    payload = tmp_path / "assistant.json"
    payload.write_text(json.dumps(cfg))
    state = {"sent": [], "replayed": False}
    fixture = cfg["assistant_provider_activity"]
    identifiers = list(fixture["events"])
    source_pages = [
        {
            "status": "complete",
            "cursor": None,
            "next_cursor": None,
            "records": [
                {
                    "id": identifier,
                    "timestamp": fixture["events"][identifier]["timestamp"],
                }
                for identifier in identifiers
            ],
            "citations": identifiers.copy(),
        }
    ]
    response = provider_response()
    if purpose == "assistant_sources":
        if mutation == "paginated":
            second = source_pages[0]["records"].pop()
            source_pages[0]["citations"].pop()
            source_pages[0]["next_cursor"] = "page-2"
            source_pages.append(
                {
                    "status": "complete",
                    "cursor": "page-2",
                    "next_cursor": None,
                    "records": [second],
                    "citations": [second["id"]],
                }
            )
        elif mutation == "wrong_citation":
            source_pages[0]["citations"][0] = "unknown-citation"
        elif mutation == "unauthorized":
            source_pages[0]["records"].append(
                {
                    "id": "private-source",
                    "timestamp": fixture["from"],
                }
            )
        elif mutation == "missing_source":
            source_pages[0]["records"].pop()
            source_pages[0]["citations"].pop()
        elif mutation == "incomplete":
            source_pages[0]["status"] = "partial"
        elif mutation == "denied_provider":
            response["external_events"][0]["repository"] = "org/private"
        elif mutation == "missing_provider":
            response["external_events"].pop()
        elif mutation == "wrong_timestamp":
            response["external_events"][0]["occurred_at"] = fixture["to"]
        elif mutation in {"leaked_canary", "answer_canary"}:
            monkeypatch.setattr(
                module.secrets, "token_urlsafe", lambda *_args: "marker"
            )
            if mutation == "leaked_canary":
                source_pages[0]["records"][0]["text"] = "synthetic-assistant-marker"
    source_stream = [
        *[("source_page", page) for page in source_pages],
        ("gateway_work", response),
        ("completed", None),
    ]
    if mutation == "missing_capability":
        source_stream = [("completed", None)]

    def http(url, session):
        assert session.token() == "ordinary-access"
        if url.endswith("/auth/me"):
            return {
                "account_type": "human",
                "is_admin": False,
                "org_id": "tenant-a",
                "user_id": users["a1"]["canonical_user_id"],
            }
        if "/events?cursor=" in url:
            assert state["closed"] is True
            state["replayed"] = True
            state["page_count"] = state.get("page_count", 0) + 1
            if mutation == "paginated_replay":
                trace = (
                    state["trace"][1:4] if url.endswith("%3A0") else state["trace"][4:]
                )
            else:
                assert url.endswith("%3A0")
                trace = state["trace"][1:]
            records = [
                {
                    "sequence": event["cursor"],
                    "cursor": "a" * 32 + ":" + str(event["cursor"]),
                    "kind": "ag_ui",
                    "payload": event,
                }
                for event in trace
            ]
            if mutation == "unsupported_envelope":
                records[0]["payload"] = {"event": {"event_type": "RUN_STARTED"}}
            return {
                "status": "ok",
                "has_more": mutation == "paginated_replay" and url.endswith("%3A0"),
                "events": records,
                "cursor": "a" * 32 + ":" + str(trace[-1]["cursor"]),
            }
        assert url.endswith("/chat/sessions/server-session")
        history = []
        if len(state["sent"]) == 2:
            history = [
                {"role": "user", "content": state["sent"][1]["text"]},
                {
                    "role": "assistant",
                    "content": "synthetic-assistant-marker"
                    if mutation == "answer_canary"
                    else "synthetic answer"
                    if purpose == "assistant_stream"
                    else "unrelated answer"
                    if mutation == "wrong_answer"
                    else " ".join(
                        event["source_url"] for event in fixture["events"].values()
                    ),
                },
            ]
            if mutation == "missing_history":
                history.pop(0)
        return {
            "user_id": users["a1"]["canonical_user_id"],
            "tenant_id": "tenant-a",
            "session_id": "server-session",
            "messages": history,
        }

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            state["closed"] = True

        def send(self, value):
            if purpose == "assistant_sources" and value["action"] == "message":
                assert all(
                    fixture[key] in value["text"] for key in ("from", "to", "timezone")
                )
            state["sent"].append(value)

        def receive(self):
            if len(state["sent"]) == 1:
                return {
                    "request_id": state["sent"][0]["request_id"],
                    "session_id": "server-session",
                }
            request_id = state["sent"][1]["request_id"]
            if purpose == "assistant_stream":
                if mutation == "missing_ack":
                    return {
                        "type": "notification",
                        "status": "notification",
                        "task_id": "task-1",
                    }
                return state["trace"][0]
            if mutation == "legacy_ag_ui":
                return {
                    "type": "ag_ui",
                    "task_id": "task-1",
                    "event": {"event_type": "TOOL_CALL_END"},
                }
            index = state.setdefault("index", 0)
            state["index"] += 1
            kind, value = source_stream[index]
            return {
                "type": kind,
                "request_id": request_id,
                "session_id": "server-session",
                "page": value if kind == "source_page" else None,
                "response": value if kind == "gateway_work" else None,
            }

    monkeypatch.setattr(module.assistant_client, "http_json", http)
    monkeypatch.setattr(module.assistant_client, "connect", lambda *_args: Client())
    if purpose == "assistant_stream":
        trace = events()
        for event in trace:
            event.update(
                request_id=module.hashlib.sha256(
                    json.dumps(
                        [cfg["evaluation_id"], users["a1"]["canonical_user_id"], "E43"]
                    ).encode()
                ).hexdigest(),
                session_id="server-session",
            )
        if mutation == "reordered":
            trace[2], trace[3] = trace[3], trace[2]
        elif mutation == "duplicated":
            trace.insert(5, {**trace[4]})
        elif mutation == "missing":
            trace.pop(4)
        elif mutation == "repeated_cursor":
            trace[2]["cursor"] = trace[1]["cursor"]
        elif mutation in {"leaked_canary", "answer_canary"}:
            monkeypatch.setattr(
                module.secrets, "token_urlsafe", lambda *_args: "marker"
            )
            if mutation == "leaked_canary":
                trace[5]["text"] = "synthetic-assistant-marker"
        if mutation in {"reordered", "duplicated", "missing"}:
            for cursor, event in enumerate(trace):
                event["cursor"] = cursor
        trace[0]["cursor"] = "a" * 32 + ":0"
        monkeypatch.setitem(state, "trace", trace)
    code = dispatcher.main([purpose, str(payload)])
    result = json.loads(capsys.readouterr().out.splitlines()[-1])
    failed_in_driver = {
        "leaked_canary",
        "answer_canary",
        "repeated_cursor",
        "unsupported_envelope",
        "missing_ack",
        "missing_history",
        "wrong_answer",
        "missing_capability",
        "legacy_ag_ui",
    }
    assert code == int(mutation in failed_in_driver), result
    assert result["success"] is (mutation not in failed_in_driver)
    assert len(state["sent"]) == 2
    assert "ordinary-access" not in json.dumps(result)
    if purpose == "assistant_stream":
        assert state["replayed"] is (mutation != "missing_ack")
        if mutation == "paginated_replay":
            assert state["page_count"] == 2
    elif mutation not in {
        "answer_canary",
        "wrong_answer",
        "missing_capability",
        "legacy_ag_ui",
    }:
        assert result["expected_ids"] == identifiers
    matrix = cases.new_matrix(("assistant",))
    dispatched = []
    monkeypatch.setattr(live, "_journey_payload", lambda *_args: cfg)
    monkeypatch.setattr(
        live,
        "_run_worker",
        lambda *_args: lambda instance, requested, payload, **kwargs: (
            dispatched.append((instance, requested, kwargs)),
            result,
        )[1],
    )
    context = stage_context(matrix)
    context["manifest"] = object()
    stages.journeys_stage(
        {"assistant_provider_activity": fixture},
        {"journey": live._journey(None, cfg, lambda *_args: None)},
    )(context)
    case_id = "E43" if purpose == "assistant_stream" else "E44"
    if mutation in {"valid", "paginated", "paginated_replay"}:
        assert matrix[case_id]["status"] == cases.PASSED, matrix[case_id]
    else:
        assert matrix[case_id]["status"] == cases.FAILED, matrix[case_id]
        if mutation in {
            "missing_ack",
            "missing_history",
            "wrong_answer",
            "missing_capability",
            "legacy_ag_ui",
        }:
            reason = {
                "missing_ack": "durable acknowledgement",
                "missing_history": "owned history",
                "wrong_answer": "does not cite",
                "missing_capability": "lacks observable structured source pages",
                "legacy_ag_ui": "lacks observable structured source pages",
            }[mutation]
            assert reason in matrix[case_id]["detail"]["error"]
        else:
            assert matrix[case_id]["detail"]["oracle_error"]
    assert ("i-synthetic", purpose, {"manifest": context["manifest"]}) in dispatched
    with pytest.raises(bundle.BundleError):
        bundle.require_purpose("assistant_not_registered")


@pytest.mark.parametrize("purpose", ["assistant_stream", "assistant_sources"])
@pytest.mark.parametrize("denial", ["expired", "other_user"])
def test_assistant_drivers_refuse_invalid_ordinary_identity(
    tmp_path, monkeypatch, users, capsys, purpose, denial
):
    from tests.e2e.cli_uplift.remote import dispatcher

    module, _ = dispatcher.load(purpose)
    monkeypatch.setattr(module.common, "assert_owned_instance", lambda *_args: None)
    monkeypatch.setattr(
        module.common,
        "fixture_secret",
        lambda *_args: {
            "access_token": "ordinary-access",
            "id_token": "ordinary-id",
            "expires_at": 1 if denial == "expired" else 4000000000,
        },
    )
    calls = []

    def http(url, session):
        calls.append(url)
        assert denial == "other_user"
        assert session.token() == "ordinary-access"
        return {
            "account_type": "human",
            "is_admin": False,
            "org_id": "tenant-a",
            "user_id": "another-user-in-the-same-tenant",
        }

    monkeypatch.setattr(module.assistant_client, "http_json", http)
    monkeypatch.setattr(
        module.assistant_client,
        "connect",
        lambda *_args: pytest.fail("Denied user must not open an assistant WebSocket"),
    )
    payload = tmp_path / "assistant.json"
    payload.write_text(
        json.dumps(
            {
                "evaluation_id": "ordinary-denial",
                "region": "us-east-1",
                "sts_endpoint": "https://sts.example.invalid",
                "work_dir": str(tmp_path),
                "gateway_url": "https://example.invalid",
                "websocket_url": "wss://example.invalid/ws",
                "assistant_users": users,
                "assistant_provider_activity": provider_fixture(),
            }
        )
    )
    assert dispatcher.main([purpose, str(payload)]) == 1
    result = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert result["success"] is False
    assert result["error_type"] == "RemoteError"
    assert ("expired" if denial == "expired" else "expected ordinary user") in result[
        "error"
    ]
    assert calls == ([] if denial == "expired" else ["https://example.invalid/auth/me"])
    assert "ordinary-access" not in json.dumps(result)


@pytest.mark.parametrize("provider_available", [True, False])
@pytest.mark.parametrize("worker_complete", [True, False])
def test_provider_and_worker_stage_requirements_remain_independent(
    baseline_dispatch, provider_available, worker_complete
):
    run, _state, _module, _tokens = baseline_dispatch
    code, baseline_evidence = run()
    assert code == 0
    if not worker_complete:
        baseline_evidence["detail"].pop("worker_completion_verified")

    fixture = provider_fixture()
    response = provider_response()
    if not provider_available:
        fixture["coverage"]["gitlab"] = [["unavailable", "provider_failure"]]
        response["coverage"][1].update(status="unavailable", reason="provider_failure")
        response["external_events"] = [
            event
            for event in response["external_events"]
            if event["provider"] != "gitlab"
        ]
    source_evidence = {
        "success": True,
        "pages": pages(),
        "expected_ids": ["activity-1", "activity-2"],
        "allowed_ids": ["activity-1", "activity-2"],
        "canary": "synthetic-secret",
        "provider_response": response,
        **source_fixture(),
    }
    drivers = {
        "assistant_sources": lambda *_args: source_evidence,
        "assistant_baseline": lambda *_args: baseline_evidence,
    }
    matrix = cases.new_matrix(("assistant",))
    stages.journeys_stage(
        {"assistant_provider_activity": fixture},
        {"journey": drivers.get},
    )(stage_context(matrix))

    assert matrix["E44"]["status"] == (
        cases.PASSED if provider_available else cases.FAILED
    )
    assert matrix["E50"]["status"] == (
        cases.PASSED if worker_complete else cases.FAILED
    )
    if not provider_available:
        assert "unavailable" in matrix["E44"]["detail"]["oracle_error"]
    if not worker_complete:
        assert "incomplete" in matrix["E50"]["detail"]["oracle_error"]


@pytest.mark.parametrize(
    "failure", ["auth", "ownership", "admin", "identity", "expired", "correlation"]
)
def test_registered_baseline_refuses_invalid_identity_and_auth(
    baseline_dispatch, failure
):
    run, state, _module, tokens = baseline_dispatch
    if failure == "admin":
        state["admin"] = True
    elif failure == "identity":
        state["identity"]["user_id"] = "other-user"
    elif failure == "expired":
        tokens["expires_at"] = 1
    elif failure == "correlation":
        state["created"] = {"request_id": "wrong", "session_id": "server-session"}
    else:
        state["fail"] = failure
    code, evidence = run()
    assert code == 1 and evidence["success"] is False
    assert not any(value["action"] == "message" for value in state["sends"])


def test_registered_baseline_never_replays_interrupted_turn(
    baseline_dispatch, tmp_path
):
    run, state, _module, _tokens = baseline_dispatch
    state["fail"] = "interrupted"
    with pytest.raises(KeyboardInterrupt):
        run()
    assert state["closed"] == 1
    journal = json.loads(next(tmp_path.glob("assistant-baseline-*.json")).read_text())
    assert journal["phase"] == "submit_attempted"
    state["fail"] = None
    code, evidence = run()
    assert code == 1 and "not automatic resubmission" in evidence["error"]
    assert len(state["sends"]) == 2


def test_baseline_consumes_actual_response_router_chunks(
    baseline_dispatch, monkeypatch
):
    import importlib.util

    run, state, _module, _tokens = baseline_dispatch
    path = (
        Path(__file__).resolve().parents[2]
        / "modules/agent-factory/gateway/lambdas/response/routers/websocket.py"
    )
    spec = importlib.util.spec_from_file_location("baseline_response_router", path)
    router_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(router_module)
    router = router_module.WebSocketRouter("wss://example.invalid/ws")
    frames = []
    monkeypatch.setattr(
        router,
        "_send_frame",
        lambda payload, *_args: frames.append(json.loads(payload)) or True,
    )
    state["lifecycle"] = []
    for kind in ["RUN_STARTED", "RUN_FINISHED"]:
        assert router.route(
            "",
            {
                "connection_id": "owned",
                "response_type": "ag_ui",
                "ag_ui_payload": {
                    "event_type": kind,
                    "threadId": "server-session",
                    "runId": "task-1",
                },
            },
            "task-1",
        )
    state["answer"] = "ready " * 5000
    assert router.route(
        state["answer"], {"connection_id": "owned", "status": "completed"}, "task-1"
    )
    assert len(frames) > 3 and frames[2]["chunk_index"] == 1
    state["chunks"] = frames
    code, evidence = run()
    assert code == 0 and evidence["detail"]["response_chunks"] > 1


@pytest.mark.parametrize("mutation", ["task", "order", "failed", "missing"])
def test_baseline_rejects_invalid_wire_evidence(baseline_dispatch, mutation):
    run, state, _module, _tokens = baseline_dispatch
    frames = [
        {
            "type": "response",
            "status": "completed",
            "task_id": "task-1",
            "content": "rea",
            "chunk_index": 1,
            "chunk_total": 2,
        },
        {
            "type": "response",
            "status": "completed",
            "task_id": "task-1",
            "content": "dy",
            "chunk_index": 2,
            "chunk_total": 2,
        },
    ]
    if mutation == "task":
        frames[1]["task_id"] = "other-task"
    elif mutation == "order":
        frames.reverse()
    elif mutation == "failed":
        frames[0]["status"] = "failed"
    else:
        frames[0].pop("task_id")
    state["chunks"] = frames
    code, evidence = run()
    assert code == 1 and evidence["success"] is False


def test_baseline_lost_journal_refuses_prior_dispatch(baseline_dispatch, tmp_path):
    run, state, _module, _tokens = baseline_dispatch
    assert run()[0] == 0
    for journal in tmp_path.glob("assistant-baseline-*.json"):
        journal.unlink()
    payload = tmp_path / "payload.json"
    cfg = json.loads(payload.read_text())
    cfg["prior_dispatch"] = True
    payload.write_text(json.dumps(cfg))
    code, evidence = run()
    assert code != 0
    assert "prior dispatch has no local journal" in json.dumps(evidence)
    assert len(state["sends"]) == 2


@pytest.mark.parametrize(
    "purpose",
    [
        "assistant_baseline",
        "assistant_stream",
        "assistant_sources",
        "assistant_isolation",
    ],
)
@pytest.mark.parametrize("sink_failure", [False, True])
def test_assistant_durable_intent_before_dispatch(tmp_path, sink_failure, purpose):
    from tests.e2e.cli_uplift import cleanup, live

    calls = []
    durable = []

    def push(document, *, critical):
        assert critical
        if sink_failure:
            raise RuntimeError("storage unavailable")
        durable.append(json.loads(json.dumps(document)))

    manifest = cleanup.Manifest(tmp_path / "manifest.json", "baseline", on_change=push)

    class Ssm:
        def json_result(self, instance, commands, **kwargs):
            intent = durable[-1]["diagnostic_intents"][purpose + ":baseline"]
            if purpose == "assistant_sources":
                assert intent["provider_activity_sha256"]
                assert "provider-fixture-private" not in json.dumps(intent)
            calls.append(commands)
            return None, {}

    worker = live._run_worker(Ssm(), {}, lambda *a: None)
    payload = {
        "evaluation_id": "baseline",
        "gateway_url": "https://example.invalid",
        "assistant_provider_activity": {"fixture": "provider-fixture-private"},
    }
    if sink_failure:
        with pytest.raises(RuntimeError, match="storage unavailable"):
            worker("i-owned", purpose, payload, manifest=manifest)
        assert not calls
    else:
        worker("i-owned", purpose, payload, manifest=manifest)
        worker("i-replacement", purpose, payload, manifest=manifest)
        assert '"prior_dispatch": false' in "\n".join(calls[0])
        assert '"prior_dispatch": true' in "\n".join(calls[1])


@pytest.mark.parametrize(
    "purpose", ["assistant_stream", "assistant_sources", "assistant_isolation"]
)
def test_assistant_journey_passes_manifest_to_remote_worker(monkeypatch, purpose):
    calls = []
    manifest = object()
    monkeypatch.setattr(
        live, "_journey_payload", lambda *_args: {"evaluation_id": "test"}
    )
    monkeypatch.setattr(
        live,
        "_run_worker",
        lambda *_args: lambda instance, name, payload, **kwargs: calls.append(
            (instance, name, payload, kwargs)
        ),
    )
    driver = live._journey(None, {}, lambda *_args: None)(purpose)
    assert driver is not None
    driver("i-synthetic", {"manifest": manifest})
    assert calls == [
        ("i-synthetic", purpose, {"evaluation_id": "test"}, {"manifest": manifest})
    ]


def test_isolated_python_handles_relocated_installation(tmp_path, monkeypatch):
    library_dir = tmp_path / "relocated" / "lib"
    library_dir.mkdir(parents=True)
    (library_dir / "libpython-test.so").touch()
    monkeypatch.setattr(assistant_process.sys, "base_prefix", str(library_dir.parent))
    settings = {
        "Py_ENABLE_SHARED": 1,
        "LIBDIR": "/missing/build-machine/lib",
        "LDLIBRARY": "libpython-test.so",
    }
    monkeypatch.setattr(assistant_process.sysconfig, "get_config_var", settings.get)
    assert assistant_process.python_env(tmp_path)["LD_LIBRARY_PATH"] == str(library_dir)


@pytest.mark.parametrize(
    "events", [[], ["RUN_STARTED"], ["RUN_FINISHED"], ["RUN_STARTED", "RUN_ERROR"]]
)
def test_baseline_refuses_classifier_only_or_incomplete_worker_reply(
    baseline_dispatch, events
):
    run, state, _module, _tokens = baseline_dispatch
    state["lifecycle"] = events
    code, evidence = run()
    assert code == 1 and evidence["success"] is False


@pytest.mark.parametrize(
    "field,value", [("runId", "other-task"), ("threadId", "other-session")]
)
def test_baseline_rejects_worker_lifecycle_from_another_turn(
    baseline_dispatch, field, value
):
    run, state, _module, _tokens = baseline_dispatch
    state["lifecycle"] = []
    event = {
        "event_type": "RUN_STARTED",
        "threadId": "server-session",
        "runId": "task-1",
        field: value,
    }
    state["chunks"] = [{"type": "ag_ui", "task_id": "task-1", "event": event}]
    code, evidence = run()
    assert code == 1 and evidence["success"] is False


@pytest.mark.parametrize(
    ("duplicate_session", "duplicate_committed", "ttl_only"),
    [
        (False, False, False),
        (True, False, False),
        (False, True, False),
        (False, False, True),
    ],
)
def test_e46_dispatch_runs_persistent_sessions_and_cleanup(
    monkeypatch,
    tmp_path,
    capsys,
    users,
    duplicate_session,
    duplicate_committed,
    ttl_only,
):
    import re
    import time

    from tests.e2e.cli_uplift.remote import dispatcher

    module, _ = dispatcher.load("assistant_sessions")
    monkeypatch.setattr(module.common, "assert_owned_instance", lambda _config: None)
    monkeypatch.setattr(
        module.assistant_baseline,
        "session_for",
        lambda _config, fixture: fixture["login_user_id"],
    )
    users["a2"].update(membership_run_owned=True, membership_id="owned-member")
    monkeypatch.setattr(module, "controller_ready", lambda _config, _fixture: True)
    sessions = {}
    by_slot = {}
    requests = []
    elapsed = [0.0]
    original_monotonic = time.monotonic
    monkeypatch.setattr(
        module.time, "monotonic", lambda: original_monotonic() + elapsed[0]
    )

    def advance(seconds):
        elapsed[0] += seconds
        if seconds == 300:
            sessions[by_slot["a2", 1]]["health"] = "ended"
            sessions[by_slot["a2", 1]]["removed"] = True

    monkeypatch.setattr(module.time, "sleep", advance)

    class Socket:
        def __init__(self, user):
            self.user = user
            self.sent = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def send(self, document):
            self.sent = document
            if document["action"] == "create-session":
                requests.append(self.user)
                slot = 1 + sum(label == self.user for label in requests[:-1])
                sid = (
                    "chat-duplicate"
                    if duplicate_session
                    else "chat-" + document["request_id"]
                )
                by_slot[self.user, slot] = sid
                sessions.setdefault(
                    sid,
                    {
                        "owner": self.user,
                        "mode": "ephemeral",
                        "health": "idle",
                        "messages": [],
                        "removed": False,
                        "revoked": False,
                    },
                )
            else:
                assert document["action"] == "message"
                assert document["persona"] == module.PERSONA
                item = sessions[document["session_id"]]
                if item["owner"] != self.user or item["revoked"] or item["removed"]:
                    return
                assert item["mode"] == "persistent"

        def receive(self):
            if self.sent["action"] == "message":
                return {"type": "session_invalid"}
            return {
                "request_id": self.sent["request_id"],
                "session_id": by_slot[
                    self.user, sum(label == self.user for label in requests)
                ],
            }

    monkeypatch.setattr(
        module.assistant_client, "connect", lambda _url, user: Socket(user)
    )

    def collect(client, sid):
        text = client.sent["text"]
        item = sessions[sid]
        if "marker" not in item:
            item["marker"] = re.search(r"[a-f0-9]{12}", text).group()
        task = sid + "-task-" + str(len(item["messages"]) // 2 + 1)
        item["messages"].extend(
            [
                {"role": "user", "content": text},
                {"role": "assistant", "content": item["marker"], "task_id": task},
            ]
        )
        if duplicate_committed:
            item["messages"].append(dict(item["messages"][-1]))
        return task, item["marker"], 1

    monkeypatch.setattr(module.assistant_baseline, "collect", collect)

    def http(url, user, *, method="GET", body=None, expected_status=200):
        if url.endswith("/auth/me"):
            return {
                "user_id": user,
                "org_id": users[user]["tenant_id"],
                "account_type": "human",
                "is_admin": False,
            }
        sid = url.split("/chat/sessions/", 1)[1].split("/", 1)[0]
        item = sessions[sid]
        if item["owner"] != user or item["revoked"]:
            raise module.assistant_client.ClientError(
                "Assistant HTTP request returned 404"
            )
        if url.endswith("/mode"):
            if method == "PUT":
                item["mode"] = body["mode"]
                item["health"] = "active" if body["mode"] == "persistent" else "idle"
                item["removed"] = body["mode"] == "ephemeral"
            return {
                "mode": item["mode"],
                "health": item["health"],
                **({"idle_timeout_seconds": 300} if item["health"] == "active" else {}),
            }
        if url.endswith("/end"):
            assert method == "POST" and body is None
            item["health"] = "ended"
            item["removed"] = True
            return {"health": "ended"}
        fixture = users[user]
        return {
            "session_id": sid,
            "user_id": fixture["canonical_user_id"],
            "tenant_id": fixture["tenant_id"],
            "messages": item["messages"],
            "truncated": False,
        }

    monkeypatch.setattr(module.assistant_client, "http_json", http)

    def observe(config, fixture, sid):
        assert fixture == users[sessions[sid]["owner"]]
        assert sessions[sid]["removed"] is True
        return {
            "evaluation_id": config["evaluation_id"],
            "session_id": sid,
            "observed_absent": True,
            "observation_source": "ttl_expiry" if ttl_only else "worker_teardown",
        }

    monkeypatch.setattr(module, "controller_observe", observe)

    def revoke(config, fixture, sid):
        item = sessions[sid]
        item["revoked"] = True
        item["removed"] = True
        return {
            "evaluation_id": config["evaluation_id"],
            "session_id": sid,
            "membership_id": fixture["membership_id"],
            "revoked": True,
            "observed_absent": True,
            "observation_source": "worker_teardown",
            "cleanup_seconds": 1,
        }

    monkeypatch.setattr(module, "controller_revoke", revoke)
    payload = tmp_path / "e46.json"
    payload.write_text(
        json.dumps(
            {
                "evaluation_id": "synthetic-run",
                "work_dir": str(tmp_path),
                "gateway_url": "https://example.invalid",
                "websocket_url": "wss://example.invalid/ws",
                "assistant_users": users,
            }
        )
    )

    def run():
        code = dispatcher.main(["assistant_sessions", str(payload)])
        return code, json.loads(capsys.readouterr().out.splitlines()[-1])

    code, evidence = run()
    if duplicate_session:
        assert code == 1 and evidence["success"] is False
        assert requests == ["a1", "a2"]
        code, resumed = run()
        assert code == 1 and resumed["success"] is False
        assert requests == ["a1", "a2"]
    elif duplicate_committed or ttl_only:
        assert code == 1 and evidence["success"] is False
        assert (
            "missing or duplicated in committed history"
            if duplicate_committed
            else "not TTL-only cleanup"
        ) in evidence["error"]
        assert requests == ["a1", "a2", "b1"] * 2
    else:
        assert code == 0 and evidence["success"] is True, evidence
        assert evidence["detail"]["lifecycle_verified"] is True
        assert evidence["detail"]["sessions_created"] == 6
        assert evidence["detail"]["worker_turns"] == 12
        assert len(evidence["detail"]["cleanup_seconds"]) == 6
        assert requests == ["a1", "a2", "b1"] * 2
        assert all(item["removed"] for item in sessions.values())
        code, resumed = run()
        assert code == 0 and resumed["detail"]["resumed"] is True
        assert len(requests) == 6
        assert "assistant_sessions" in bundle.purposes()


@pytest.mark.parametrize(
    "owned_membership,controller_ready", [(False, True), (True, False)]
)
def test_e46_requires_owned_revocation_authority_before_creating_sessions(
    monkeypatch, tmp_path, capsys, users, owned_membership, controller_ready
):
    from tests.e2e.cli_uplift.remote import dispatcher

    module, _ = dispatcher.load("assistant_sessions")
    monkeypatch.setattr(module.common, "assert_owned_instance", lambda _config: None)
    users["a2"].update(
        membership_run_owned=owned_membership, membership_id="owned-member"
    )
    monkeypatch.setattr(
        module, "controller_ready", lambda _config, _fixture: controller_ready
    )
    creations = []
    monkeypatch.setattr(
        module.assistant_client, "connect", lambda *_args: creations.append(1)
    )
    payload = tmp_path / "e46-unready.json"
    payload.write_text(
        json.dumps(
            {
                "evaluation_id": "synthetic-unready",
                "work_dir": str(tmp_path),
                "gateway_url": "https://example.invalid",
                "websocket_url": "wss://example.invalid/ws",
                "assistant_users": users,
            }
        )
    )

    assert dispatcher.main(["assistant_sessions", str(payload)]) == 1
    evidence = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert evidence["success"] is False
    expected = (
        "no sessions created" if owned_membership else "requires a run-owned membership"
    )
    assert expected in evidence["error"]
    assert not creations
    assert not list(tmp_path.glob("assistant-sessions-*.json"))


@pytest.mark.parametrize(
    ("fault", "receipt", "observed", "interrupted", "defect"),
    [
        ("none", False, False, False, None),
        ("assistant_disconnect", False, True, False, None),
        ("assistant_duplicate_delivery", True, True, False, None),
        ("assistant_worker_loss", False, False, False, None),
        ("assistant_worker_loss", True, True, False, None),
        ("assistant_worker_loss", True, True, True, None),
        ("assistant_worker_loss", True, True, True, "retryable"),
        ("assistant_queue_unavailable", True, True, False, None),
        ("assistant_supervisor_restart", True, True, False, None),
        ("assistant_duplicate_delivery", True, False, False, "duplicate_terminal"),
        ("assistant_disconnect", False, False, False, "replay_gap"),
        ("assistant_disconnect", False, False, False, "trailing_gap"),
        ("assistant_worker_loss", True, False, False, "stale_lease"),
        ("assistant_worker_loss", True, False, False, "uncertain_completed"),
        ("assistant_disconnect", False, False, False, "duplicate_input"),
    ],
)
def test_e47_dispatch_requires_opt_in_and_controller_receipt(
    monkeypatch, tmp_path, capsys, users, fault, receipt, observed, interrupted, defect
):
    import hashlib
    from contextlib import nullcontext
    from tests.e2e.cli_uplift.remote import dispatcher

    module, _ = dispatcher.load("assistant_faults")
    monkeypatch.setattr(module.common, "assert_owned_instance", lambda _config: None)
    monkeypatch.setattr(
        module.assistant_baseline,
        "session_for",
        lambda _config, fixture: fixture["login_user_id"],
    )
    calls = []
    connections = []
    monkeypatch.setattr(
        module.assistant_client,
        "connect",
        lambda url, session: (connections.append((url, session)), nullcontext())[1],
    )
    session_id = "chat-owned"
    cursor = "a" * 32 + ":0"
    replay = {
        "status": "ok",
        "events": [
            {
                "sequence": 1,
                "event_id": "event-owned",
                "cursor": "a" * 32 + ":1",
                "kind": "terminal",
                "payload": {
                    "session_id": session_id,
                    "task_id": "task-owned",
                    "delivery_id": "delivery-owned",
                    "terminal_delivery": True,
                    "automatic_replay_permitted": False,
                    "accounting_status": (
                        "not_used"
                        if defect == "retryable"
                        else "unresolved"
                        if interrupted
                        else "settled"
                    ),
                    "status": "interrupted" if interrupted else "completed",
                    **({"retryable": defect == "retryable"} if interrupted else {}),
                },
            }
        ],
        "cursor": "a" * 32 + ":1",
        "latest_sequence": 1,
        "has_more": False,
    }

    def http(url, session):
        calls.append(url)
        assert session == "a1" and "/chat/sessions/chat-owned" in url
        if "/events?cursor=" in url:
            assert url.endswith("?cursor=" + cursor)
            return replay
        if url.endswith("/mode"):
            return {"mode": "persistent", "health": "active"}
        return {
            "session_id": session_id,
            "user_id": users["a1"]["canonical_user_id"],
            "tenant_id": users["a1"]["tenant_id"],
            "truncated": False,
            "messages": [{"role": "user", "content": "Run the selected fault"}]
            * (2 if defect == "duplicate_input" else 1),
        }

    monkeypatch.setattr(module.assistant_client, "http_json", http)
    if defect == "duplicate_terminal":
        replay["events"].append(
            {
                **replay["events"][0],
                "sequence": 2,
                "cursor": "a" * 32 + ":2",
                "event_id": "duplicate-terminal",
            }
        )
        replay["cursor"] = "a" * 32 + ":2"
        replay["latest_sequence"] = 2
    elif defect == "replay_gap":
        replay["events"][0]["sequence"] = 2
        replay["events"][0]["cursor"] = "a" * 32 + ":2"
        replay["cursor"] = "a" * 32 + ":2"
        replay["latest_sequence"] = 2
    elif defect == "trailing_gap":
        replay["latest_sequence"] = 2
    elif defect == "uncertain_completed":
        replay["events"][0]["payload"]["accounting_status"] = "unresolved"
    payload = tmp_path / "e47.json"
    payload.write_text(
        json.dumps(
            {
                "fault": fault,
                "evaluation_id": "synthetic-run",
                "gateway_url": "https://example.invalid",
                "websocket_url": "wss://example.invalid/ws",
                "assistant_users": users,
                "fault_target": {
                    "evaluation_id": "synthetic-run",
                    "user_label": "a1",
                    "session_id": session_id,
                    "worker_id": "worker-owned",
                    "cursor": cursor,
                    "turn_digest": hashlib.sha256(
                        b"Run the selected fault"
                    ).hexdigest(),
                    "task_id": "task-owned",
                },
                **(
                    {
                        "fault_receipt": {
                            "evaluation_id": "synthetic-run",
                            "fault": fault,
                            "session_id": session_id,
                            "worker_id": "worker-owned",
                            "injected": True,
                            "delivery_kind": "result",
                            "delivery_attempts": 2,
                            "delivery_id": "delivery-owned",
                            "lost_before_commit": True,
                            "old_lease_write_denied": True,
                            "old_generation": 1,
                            "replacement_generation": 2,
                            "unavailable_seconds": 5,
                            "consumer_resumed": True,
                            "restarted": True,
                        }
                    }
                    if receipt
                    else {}
                ),
            }
        )
    )
    if defect == "stale_lease":
        document = json.loads(payload.read_text())
        document["fault_receipt"]["old_lease_write_denied"] = False
        payload.write_text(json.dumps(document))
    code = dispatcher.main(["assistant_faults", str(payload)])
    evidence = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert code == (0 if observed else 1) and evidence["success"] is observed
    if observed:
        assert evidence["stage"] == "replay_observed"
        assert evidence["detail"]["outcome"] == (
            "interrupted" if interrupted else "completed"
        )
        if interrupted:
            assert evidence["detail"]["accounting_status"] == (
                "not_used" if defect == "retryable" else "unresolved"
            )
            assert evidence["detail"]["automatic_replay_permitted"] is False
        assert evidence["detail"]["events_replayed"] == 1
        assert evidence["detail"]["fault_proof"]
        assert len(calls) == 4
        assert len(connections) == (2 if fault == "assistant_disconnect" else 0)
        matrix = {"E47": cases.new_matrix(("assistant",))["E47"]}
        context = stage_context(matrix)
        context["fault"] = fault
        stages.journeys_stage(
            {},
            {
                "journey": lambda purpose: (lambda *_args: evidence)
                if purpose == "assistant_faults"
                else None
            },
        )(context)
        assert matrix["E47"]["status"] == cases.PASSED
    elif defect:
        violation = {
            "duplicate_terminal": "missing or duplicate committed events",
            "replay_gap": "missing, repeated, or reordered event",
            "trailing_gap": "latest sequence has a gap",
            "stale_lease": "fenced or visibly interrupted pre-commit outcome",
            "uncertain_completed": "non-replayed interrupted outcome",
            "duplicate_input": "accepted turn is absent",
        }
        assert violation[defect] in evidence["error"]
        assert len(calls) == (
            2
            if defect == "duplicate_input"
            else 3
            if defect in {"replay_gap", "trailing_gap"}
            else 4
        )
    else:
        assert evidence["stage"] == "start" and calls == []
    assert "assistant_faults" in bundle.purposes()


@pytest.mark.parametrize(
    ("fault", "invalid"),
    [
        ("assistant_duplicate_delivery", {"delivery_attempts": 1}),
        ("assistant_duplicate_delivery", {"delivery_id": "other-result"}),
        ("assistant_worker_loss", {"lost_before_commit": False}),
        ("assistant_worker_loss", {"old_lease_write_denied": False}),
        ("assistant_queue_unavailable", {"unavailable_seconds": 31}),
        ("assistant_queue_unavailable", {"consumer_resumed": False}),
        ("assistant_supervisor_restart", {"old_lease_write_denied": False}),
        ("assistant_supervisor_restart", {"replacement_generation": 1}),
    ],
)
def test_e47_fault_specific_proof_refuses_unbounded_or_unfenced_actions(fault, invalid):
    from tests.e2e.cli_uplift.remote import dispatcher

    module, _ = dispatcher.load("assistant_faults")
    receipt = {
        "delivery_kind": "notification",
        "delivery_attempts": 2,
        "delivery_id": "delivery-owned",
        "lost_before_commit": True,
        "old_lease_write_denied": True,
        "old_generation": 1,
        "replacement_generation": 2,
        "unavailable_seconds": 5,
        "consumer_resumed": True,
        "restarted": True,
    }
    receipt.update(invalid)
    with pytest.raises(module.common.RemoteError, match="E47"):
        module.fault_proof(fault, receipt, {"delivery_id": "delivery-owned"}, [])


def test_e47_duplicate_notification_must_be_the_replayed_event():
    from tests.e2e.cli_uplift.remote import dispatcher

    module, _ = dispatcher.load("assistant_faults")
    receipt = {
        "delivery_kind": "notification",
        "delivery_attempts": 2,
        "delivery_id": "notification-owned",
    }
    outcome = {"delivery_id": "terminal-owned"}
    events = [{"kind": "ag_ui", "event_id": "notification-owned"}]
    assert module.fault_proof(
        "assistant_duplicate_delivery", receipt, outcome, events
    ) == {
        "delivery_kind": "notification",
        "delivery_attempts": 2,
    }
    with pytest.raises(module.common.RemoteError, match="duplicate delivery"):
        module.fault_proof("assistant_duplicate_delivery", receipt, outcome, [])


@pytest.mark.parametrize("late_terminal", [True, False])
def test_e47_waits_only_two_minutes_for_visible_fault_outcome(
    monkeypatch, late_terminal
):
    from tests.e2e.cli_uplift.remote import dispatcher

    module, _ = dispatcher.load("assistant_faults")
    elapsed = [0]
    calls = []
    monkeypatch.setattr(module.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(
        module.time,
        "sleep",
        lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
    )
    cursor = "a" * 32 + ":0"

    def http(_url, _session):
        calls.append(elapsed[0])
        visible = late_terminal and elapsed[0] == 2
        return {
            "status": "ok",
            "has_more": False,
            "cursor": "a" * 32 + (":1" if visible else ":0"),
            "latest_sequence": 1 if visible else 0,
            "events": (
                [
                    {
                        "sequence": 1,
                        "cursor": "a" * 32 + ":1",
                        "event_id": "event-owned",
                        "kind": "terminal",
                        "payload": {"task_id": "task-owned"},
                    }
                ]
                if visible
                else []
            ),
        }

    monkeypatch.setattr(module.assistant_client, "http_json", http)
    if late_terminal:
        assert (
            len(
                module.replay(
                    "https://example.invalid", "a1", "chat-owned", cursor, "task-owned"
                )["events"]
            )
            == 1
        )
        assert calls == [0, 1, 2]
    else:
        with pytest.raises(module.common.RemoteError, match="120 seconds"):
            module.replay(
                "https://example.invalid", "a1", "chat-owned", cursor, "task-owned"
            )
        assert elapsed[0] == 120


def test_e47_worker_loss_without_replacement_requires_visible_interruption():
    from tests.e2e.cli_uplift.remote import dispatcher

    module, _ = dispatcher.load("assistant_faults")
    receipt = {
        "lost_before_commit": True,
        "old_lease_write_denied": True,
        "old_generation": 1,
    }
    assert module.fault_proof(
        "assistant_worker_loss", receipt, {"status": "interrupted"}, []
    ) == {
        "old_lease_write_denied": True,
        "replacement_generation": None,
    }
    with pytest.raises(module.common.RemoteError, match="pre-commit"):
        module.fault_proof(
            "assistant_worker_loss", receipt, {"status": "completed"}, []
        )


@pytest.fixture
def assistant_controller_case(tmp_path, users):
    from tests.e2e.cli_uplift import assistant_fault_control

    evaluation_id = "synthetic-run"
    snapshots = []
    manifest = cleanup.Manifest(
        tmp_path / "manifest.json",
        evaluation_id,
        on_change=lambda document, **_kwargs: snapshots.append(deepcopy(document)),
    )
    target = {
        "evaluation_id": evaluation_id,
        "session_id": "chat-owned",
        "worker_id": "worker-owned",
        "user_label": "a1",
        "consumer_id": "consumer-owned",
    }
    inspected = {
        "controller_identity": "harness_controller",
        "evaluation_id": evaluation_id,
        "session_id": "chat-owned",
        "worker_id": "worker-owned",
        "owner_user_id": users["a1"]["canonical_user_id"],
        "session_run_owned": True,
        "worker_run_owned": True,
        "consumer_id": "consumer-owned",
        "consumer_run_owned": True,
        "supervisor_run_owned": True,
    }
    mutations = []

    def mutate(**action):
        mutations.append(action)
        recorded = snapshots[-1]
        assert (
            recorded["diagnostic_intents"][
                "assistant_fault_" + action["fault"] + ":" + evaluation_id
            ]["worker_id"]
            == "worker-owned"
        )
        assert {entry["kind"] for entry in recorded["resources"]} == {
            "assistant_session",
            "assistant_worker",
        }
        assert "token" not in action and "session" not in action
        return {
            **action,
            "controller_identity": "harness_controller",
            "injected": True,
            "delivery_kind": "result",
            "delivery_attempts": 2,
            "delivery_id": "delivery-owned",
            "lost_before_commit": True,
            "old_lease_write_denied": True,
            "old_generation": 1,
            "replacement_generation": 2,
            "unavailable_seconds": 5,
            "consumer_resumed": True,
            "restarted": True,
        }

    controller = assistant_fault_control.AssistantFaultController(
        inspect=lambda **_target: inspected,
        mutate=mutate,
    )
    return manifest, target, users["a1"], inspected, mutations, snapshots, controller


@pytest.mark.parametrize(
    "fault",
    [
        "assistant_duplicate_delivery",
        "assistant_worker_loss",
        "assistant_queue_unavailable",
        "assistant_supervisor_restart",
        "assistant_disconnect",
    ],
)
def test_ac03_owned_controller_injects_only_after_durable_manifest(
    monkeypatch, assistant_controller_case, fault
):
    manifest, target, fixture, _inspected, mutations, snapshots, controller = (
        assistant_controller_case
    )
    controller.register(manifest, "synthetic-run", fault, target, fixture)
    assert len(manifest.outstanding()) == 2
    payload = {
        "evaluation_id": "synthetic-run",
        "fault": fault,
        "fault_target": target,
        "assistant_users": {"a1": fixture},
    }
    monkeypatch.setattr(live, "_journey_payload", lambda _cfg, _ctx: dict(payload))
    forwarded = []
    monkeypatch.setattr(
        live,
        "_run_worker",
        lambda *_args: lambda _instance,
        _purpose,
        document,
        **_kwargs: forwarded.append(document) or {"success": True},
    )
    drive = live._journey(None, {}, lambda *_args: None, fault_controller=controller)(
        "assistant_faults"
    )
    assert drive("i-owned", {"manifest": manifest}) == {"success": True}
    assert len(forwarded) == 1
    if fault == "assistant_disconnect":
        assert not mutations and "fault_receipt" not in forwarded[0]
    else:
        assert len(mutations) == 1
        assert (
            forwarded[0]["fault_receipt"]["controller_identity"] == "harness_controller"
        )
        assert snapshots[-1]["diagnostic_intents"]
    assert all(entry["status"] == cleanup.PENDING for entry in manifest.outstanding())
    ok, results = cleanup.sweep(manifest, {}, prefix="synthetic-run")
    assert not ok and {item["kind"] for item in results} == {
        "assistant_session",
        "assistant_worker",
    }


@pytest.mark.parametrize(
    "unowned", ["absent", "foreign", "reused", "shared_queue", "shared_supervisor"]
)
def test_ac03_unowned_target_refused_before_controller_mutation(
    assistant_controller_case, unowned
):
    from tests.e2e.cli_uplift import ports

    manifest, target, fixture, inspected, mutations, _snapshots, controller = (
        assistant_controller_case
    )
    fault = (
        "assistant_queue_unavailable"
        if unowned == "shared_queue"
        else (
            "assistant_supervisor_restart"
            if unowned == "shared_supervisor"
            else "assistant_worker_loss"
        )
    )
    if unowned != "absent":
        controller.register(manifest, "synthetic-run", fault, target, fixture)
    if unowned == "foreign":
        inspected["owner_user_id"] = "another-user"
    elif unowned == "reused":
        manifest.mark("assistant_worker", "worker-owned", cleanup.REUSED)
    elif unowned == "shared_queue":
        inspected["consumer_run_owned"] = False
    elif unowned == "shared_supervisor":
        inspected["supervisor_run_owned"] = False
    with pytest.raises(ports.PortError, match="run-owned|unowned|missing"):
        controller.authorize(manifest, "synthetic-run", fault, target, fixture)
    assert not mutations


def test_ac03_ordinary_user_cannot_request_controller_fault(assistant_controller_case):
    from tests.e2e.cli_uplift import assistant_fault_control, ports

    manifest, target, fixture, _inspected, mutations, _snapshots, controller = (
        assistant_controller_case
    )
    controller.register(
        manifest, "synthetic-run", "assistant_worker_loss", target, fixture
    )
    user = assistant_client.UserSession(
        {
            "access_token": "synthetic",
            "id_token": "synthetic",
            "expires_at": 4102444800,
        }
    )
    with pytest.raises(assistant_client.ClientError, match="privileged routes"):
        assistant_client.http_json(
            "https://example.invalid/internal/fault",
            user,
            opener=lambda _request: pytest.fail("user fault reached controller"),
        )
    with pytest.raises(ports.PortError, match="mutation capability"):
        assistant_fault_control.AssistantFaultController(
            inspect=controller.inspect
        ).authorize(manifest, "synthetic-run", "assistant_worker_loss", target, fixture)
    assert not mutations


def test_ac03_controller_never_replays_uncertain_fault_action(
    assistant_controller_case,
):
    from tests.e2e.cli_uplift import ports

    manifest, target, fixture, _inspected, mutations, _snapshots, controller = (
        assistant_controller_case
    )
    controller.register(
        manifest, "synthetic-run", "assistant_worker_loss", target, fixture
    )
    controller.authorize(
        manifest, "synthetic-run", "assistant_worker_loss", target, fixture
    )
    with pytest.raises(ports.PortError, match="already attempted"):
        controller.authorize(
            manifest, "synthetic-run", "assistant_worker_loss", target, fixture
        )
    assert len(mutations) == 1


@pytest.mark.parametrize("reason", ["inspection", "target"])
def test_ac03_missing_capability_or_prepared_target_is_blocked(reason):
    from tests.e2e.cli_uplift import ports

    matrix = {"E47": cases.new_matrix(("assistant",))["E47"]}
    ctx = stage_context(matrix)
    ctx["fault"] = "assistant_worker_loss"
    error = (
        "Assistant fault controller lacks a verified run-owned inspection or mutation capability"
        if reason == "inspection"
        else "E47 requires a separately prepared run-owned session target"
    )

    def missing(_instance, _ctx):
        raise ports.PortError(error)

    stages.journeys_stage(
        {},
        {"journey": lambda purpose: missing if purpose == "assistant_faults" else None},
    )(ctx)
    assert matrix["E47"]["status"] == cases.BLOCKED
    assert error in matrix["E47"]["detail"]["detail"]


def test_ac04_never_observed_reservation_vetoes_pass_through_dispatch_and_report(
    monkeypatch, assistant_controller_case
):
    from tests.e2e.cli_uplift import assistant_fault_control, report

    manifest, _target, _fixture, _inspected, _mutations, _snapshots, _controller = (
        assistant_controller_case
    )
    observed = []

    def inspect_reservations(**scope):
        observed.append(scope)
        return {
            **scope,
            "controller_identity": "harness_controller",
            "creation_run_owned": True,
            "reservation_persisted": True,
            "pod_observed": False,
            "observation_source": "reservation_and_pod_readback",
            "reservation_id": "reservation-owned",
        }

    controller = assistant_fault_control.AssistantFaultController(
        inspect_reservations=inspect_reservations
    )
    monkeypatch.setattr(
        live, "_journey_payload", lambda _cfg, _ctx: {"evaluation_id": "synthetic-run"}
    )
    monkeypatch.setattr(
        live,
        "_run_worker",
        lambda *_args: lambda *_args: {
            "success": True,
            "detail": {"lifecycle_verified": True},
        },
    )
    evidence = live._journey(
        None, {}, lambda *_args: None, fault_controller=controller
    )("assistant_sessions")("i-owned", {"manifest": manifest})
    assert observed == [{"evaluation_id": "synthetic-run"}]
    matrix = {"E46": cases.new_matrix(("case:E46",))["E46"]}
    context = stage_context(matrix)
    stages.journeys_stage(
        {},
        {
            "journey": lambda purpose: (lambda *_args: evidence)
            if purpose == "assistant_sessions"
            else None
        },
    )(context)
    assert matrix["E46"]["status"] == cases.FAILED
    limitation = matrix["E46"]["detail"]["reservation_limitation"]
    assert limitation["status"] == "known_limitation"
    assert limitation["pod_observed"] is False
    document = report.build(
        matrix=matrix,
        suites=("case:E46",),
        config={},
        evaluation_id="synthetic-run",
        attempt_id="synthetic-attempt",
        cleanup_ok=True,
        timing={},
        correlation={},
    )
    assert document["status"] != cases.PASSED
    assert document["full_acceptance"] is False
    assert document["cases"][0]["detail"]["reservation_limitation"] == limitation


def test_qual03_separate_limitation_row_is_schema_checked_and_never_a_pass(tmp_path):
    from tests.e2e.cli_uplift import report

    matrix = {"E46": cases.new_matrix(("case:E46",))["E46"]}
    limitation = {
        "criterion": "QUAL03",
        "status": "known_limitation",
        "reservation_recorded": True,
        "pod_observed": False,
        "observation_source": "reservation_and_pod_readback",
        "reservation_id": "reservation-owned",
    }
    cases.record(matrix, "E46", cases.FAILED, {"reservation_limitation": limitation})
    evaluation_id = "adp-e2e-20260915-143022-a1b2c3"

    def build():
        return report.build(
            matrix=matrix,
            suites=("case:E46",),
            config={"expected_revision": "a" * 40},
            evaluation_id=evaluation_id,
            attempt_id=evaluation_id + "-a1",
            cleanup_ok=True,
            timing={
                "started_at": "2026-09-15T14:30:22Z",
                "ended_at": "2026-09-15T14:30:23Z",
                "duration_seconds": 1,
            },
            correlation={},
        )

    document = build()
    assert document["status"] == cases.FAILED
    assert document["full_acceptance"] is False
    assert document["counts"][cases.FAILED] == 1
    assert len(document["cases"]) == 1
    assert document["known_limitations"] == [
        {
            "id": "QUAL03",
            "source_case": "E46",
            "status": "known_limitation",
            "detail": limitation,
        }
    ]
    assert report.validate(document) == []
    paths = report.write(tmp_path, document, matrix, evaluation_id)
    assert (
        json.loads(Path(paths["report"]).read_text())["known_limitations"]
        == (document["known_limitations"])
    )
    summary = report.summary(matrix, ("case:E46",), evaluation_id, cleanup_ok=True)
    assert "| `QUAL03` | #7174 | assistant | 🚧 known_limitation |" in summary
    assert "1 passed, 1 failed" not in summary

    invalid = deepcopy(document)
    invalid["known_limitations"][0]["detail"].pop("pod_observed")
    assert any("pod_observed" in error for error in report.validate(invalid))
    invalid = deepcopy(document)
    invalid["known_limitations"][0]["status"] = "passed"
    assert any("known_limitations" in error for error in report.validate(invalid))

    matrix["E46"]["status"] = cases.PASSED
    assert build()["status"] == cases.FAILED
    assert any("QUAL03" in reason for reason in build()["reasons"])
    matrix["E46"]["detail"] = {}
    assert build()["known_limitations"] == []


@pytest.mark.parametrize("unverified", ["worker", "foreign", "pod_seen"])
def test_ac04_reservation_limitation_requires_controller_and_unobserved_pod(
    monkeypatch, assistant_controller_case, unverified
):
    from tests.e2e.cli_uplift import assistant_fault_control, ports

    manifest = assistant_controller_case[0]
    monkeypatch.setattr(
        live, "_journey_payload", lambda _cfg, _ctx: {"evaluation_id": "synthetic-run"}
    )
    monkeypatch.setattr(
        live,
        "_run_worker",
        lambda *_args: lambda *_args: {
            "success": True,
            "detail": {
                "lifecycle_verified": True,
                "reservation_limitation": {"status": "known_limitation"},
            },
            "reservation_limitation": {"status": "known_limitation"},
        },
    )
    proof = {
        "controller_identity": "harness_controller",
        "evaluation_id": "synthetic-run",
        "creation_run_owned": True,
        "reservation_persisted": True,
        "pod_observed": False,
        "observation_source": "reservation_and_pod_readback",
        "reservation_id": "reservation-owned",
    }
    if unverified == "foreign":
        proof["evaluation_id"] = "another-run"
    if unverified == "pod_seen":
        proof["pod_observed"] = True
    controller = assistant_fault_control.AssistantFaultController(
        inspect_reservations=None if unverified == "worker" else lambda **_scope: proof
    )
    drive = live._journey(None, {}, lambda *_args: None, fault_controller=controller)(
        "assistant_sessions"
    )
    if unverified == "worker":
        result = drive("i-owned", {"manifest": manifest})
        assert "reservation_limitation" not in result
        assert "reservation_limitation" not in result["detail"]
    else:
        with pytest.raises(ports.PortError, match="did not prove"):
            drive("i-owned", {"manifest": manifest})


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "wrong_account",
        "missing_usage",
        "cleanup_failure",
        "expired_token",
        "instance_loss",
        "assistant_unlisted",
    ],
)
def test_ac05_default_legacy_and_unlisted_faults_do_not_inject_e47(fault):
    from tests.e2e.cli_uplift import runner

    if fault != "assistant_unlisted":
        assert (
            runner.parse_args(["--config", "synthetic.json", "--fault", fault]).fault
            == fault
        )
    else:
        with pytest.raises(SystemExit):
            runner.parse_args(["--config", "synthetic.json", "--fault", fault])

    matrix = cases.new_matrix(("assistant",))
    matrix = {case_id: matrix[case_id] for case_id in ("E46", "E47")}
    context = stage_context(matrix)
    context["fault"] = fault
    driven = []

    def resolve(purpose):
        driven.append(purpose)
        if purpose == "assistant_sessions":
            return lambda *_args: {
                "success": True,
                "detail": {"lifecycle_verified": True},
            }
        raise AssertionError("A non-assistant fault must not invoke E47")

    stages.journeys_stage({}, {"journey": resolve})(context)
    assert driven == ["assistant_sessions"]
    assert matrix["E46"]["status"] == cases.PASSED
    assert matrix["E47"]["status"] == cases.NOT_RUN
    assert "explicit assistant fault" in matrix["E47"]["detail"]["detail"]


def test_ac05_parser_defaults_to_no_injected_fault():
    from tests.e2e.cli_uplift import runner

    assert runner.parse_args(["--config", "synthetic.json"]).fault == "none"


def test_ac06_report_indexes_fault_and_cleanup_without_granting_live_acceptance(
    tmp_path,
):
    from tests.e2e.cli_uplift import report

    matrix = cases.new_matrix(("case:E46", "case:E47"))
    for case_id in ("E01", "C01"):
        cases.record(matrix, case_id, cases.PASSED)
    cleanup_seconds = {
        f"{user}:{slot}": 2.5 for user in ("a1", "a2", "b1") for slot in (1, 2)
    }
    cases.record(
        matrix,
        "E46",
        cases.PASSED,
        {
            "lifecycle_verified": True,
            "cleanup_seconds": cleanup_seconds,
        },
    )
    cases.record(
        matrix,
        "E47",
        cases.PASSED,
        {
            "fault": "assistant_worker_loss",
            "outcome": "interrupted",
            "accounting_status": "unresolved",
            "fault_proof": {
                "old_lease_write_denied": True,
                "access_token": "synthetic-secret",
            },
        },
    )
    evaluation_id = "adp-e2e-20260915-143022-a1b2c3"
    expected = "a" * 40

    def build(*, preflight=None, cleanup_ok=True):
        return report.build(
            matrix=matrix,
            suites=("case:E46", "case:E47"),
            config={"expected_revision": expected},
            evaluation_id=evaluation_id,
            attempt_id=evaluation_id + "-a1",
            cleanup_ok=cleanup_ok,
            timing={
                "started_at": "2026-09-15T14:30:22Z",
                "ended_at": "2026-09-15T14:30:23Z",
                "duration_seconds": 1,
            },
            correlation={},
            preflight=preflight,
        )

    document = build(preflight={"deployed_revision": expected})
    assert report.validate(document) == []
    assert document["status"] == cases.PASSED
    assert len(document["assistant_qualification"]) == 2
    lifecycle, fault = document["assistant_qualification"]
    assert lifecycle == {
        "source_case": "E46",
        "qualification_status": "pending_operator_review",
        "case_status": cases.PASSED,
        "expected_revision": expected,
        "deployed_revision": expected,
        "cleanup_status": "complete",
        "cleanup_seconds": cleanup_seconds,
        "fault": None,
        "outcome": None,
        "accounting_status": None,
        "fault_proof": None,
    }
    assert fault["source_case"] == "E47"
    assert fault["qualification_status"] == "pending_operator_review"
    assert fault["fault"] == "assistant_worker_loss"
    assert fault["outcome"] == "interrupted"
    assert fault["accounting_status"] == "unresolved"
    assert fault["fault_proof"] == {"old_lease_write_denied": True}
    paths = report.write(tmp_path, document, matrix, evaluation_id)
    published = json.loads(Path(paths["report"]).read_text())
    assert published["assistant_qualification"] == document["assistant_qualification"]
    assert "synthetic-secret" not in json.dumps(published)

    missing_revision = build(preflight={})
    assert report.validate(missing_revision) == []
    assert all(
        row["deployed_revision"] is None
        for row in missing_revision["assistant_qualification"]
    )
    assert all(
        row["qualification_status"] == "pending_operator_review"
        for row in missing_revision["assistant_qualification"]
    )
    incomplete = build(preflight={"deployed_revision": expected}, cleanup_ok=False)
    assert incomplete["status"] != cases.PASSED
    assert all(
        row["cleanup_status"] == "failed"
        for row in incomplete["assistant_qualification"]
    )
    matrix["E47"]["status"] = cases.FAILED
    failed_fault = build(preflight={"deployed_revision": expected})
    assert failed_fault["assistant_qualification"][1]["fault_proof"] is None
    invalid = deepcopy(document)
    invalid["assistant_qualification"][0]["qualification_status"] = "passed"
    assert any("qualification_status" in error for error in report.validate(invalid))
