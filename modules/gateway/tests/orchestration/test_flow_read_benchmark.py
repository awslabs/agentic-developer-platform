"""Accounting self-tests independent of the backend's pending read optimization."""

import asyncio
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import httpx
import pytest
import yaml

from scripts.benchmark_flow_reads import API_SAMPLES, ApiAttempt, ApiExpectation, collect_api, evaluate_api, evaluate_browser, sample_api

EXPECTED = ApiExpectation(("fixture-flow-a",), "fixture-flow-a", ("story-a", "story-b"), (("story-a", "story-b"),))


def attempts(*, readers=1, latency=0.1):
    return [
        ApiAttempt(operation, (sample - 1) % readers, sample, latency, "ok", 200, True, 1)
        for operation in ("list", "graph")
        for sample in range(1, API_SAMPLES + 1)
    ]


def test_nearest_rank_and_five_reader_totals():
    records = attempts(readers=5)
    for record_number in range(94, 100):
        records[record_number] = ApiAttempt("list", record_number % 5, record_number + 1, 2.5, "ok", 200, True, 1)
    result = evaluate_api(records, readers=5)
    assert result["operations"]["list"]["gate_p95_seconds"] == 2.5
    assert not result["passed"]
    assert len(result["attempts"]) == 200
    assert {reader: sum(attempt.reader == reader for attempt in records) for reader in range(5)} == {reader: 40 for reader in range(5)}
    assert evaluate_api(attempts(readers=5), readers=5)["passed"]


def test_missing_aborted_and_failed_samples_never_disappear():
    records = attempts()
    missing = evaluate_api(records[:-1], readers=1)
    assert not missing["passed"] and not missing["operations"]["graph"]["complete"]
    records[0] = ApiAttempt("list", 0, 1, 15.0, "timeout", None, False, None)
    timed_out = evaluate_api(records, readers=1)
    assert timed_out["operations"]["list"]["success_rate"] == 0.99
    assert len(timed_out["attempts"]) == 200
    assert timed_out["operations"]["list"]["successful_only_p95_seconds"] == 0.1
    assert json.dumps(timed_out, allow_nan=False)
    records[1] = ApiAttempt("list", 0, 2, 15.0, "timeout", None, False, None)
    assert not evaluate_api(records, readers=1)["operations"]["list"]["passed"]
    records[1] = ApiAttempt("list", 0, 2, None, "aborted", None, False, None)
    assert not evaluate_api(records, readers=1)["operations"]["list"]["passed"]


def test_content_violations_fail_even_when_success_target_is_met():
    records = attempts()
    records[0] = ApiAttempt("list", 0, 1, 0.1, "content_violation", 200, False, 1)
    report = evaluate_api(records, readers=1)
    assert report["operations"]["list"]["success_rate"] == 0.99
    assert report["operations"]["list"]["violations"] == 1
    assert not report["passed"]


@pytest.mark.asyncio
async def test_http_samples_classify_content_auth_and_timeout():
    def response(request):
        if request.url.path.endswith("/flows"):
            return httpx.Response(200, json={"flows": [{"id": "foreign-tenant-flow"}]})
        if request.headers.get("test-case") == "timeout":
            raise httpx.ReadTimeout("controlled timeout")
        return httpx.Response(403)

    async with httpx.AsyncClient(transport=httpx.MockTransport(response), base_url="https://example.invalid") as client:
        content = await sample_api(client, EXPECTED, operation="list", reader=0, sample=1)
        assert content.classification == "content_violation" and not content.success
        denied = await sample_api(client, EXPECTED, operation="graph", reader=0, sample=1)
        assert denied.classification == "auth_violation" and denied.status == 403
        client.headers["test-case"] = "timeout"
        timeout = await sample_api(client, EXPECTED, operation="graph", reader=0, sample=2)
        assert timeout.classification == "timeout" and timeout.elapsed_seconds is not None


@pytest.mark.asyncio
async def test_collect_api_records_exact_totals_without_payloads():
    def response(request):
        if request.url.path.endswith("/flows"):
            assert request.url.params["sort"] == "created"
            assert request.url.params["q"] == "fixture-flow"
            return httpx.Response(200, json={"flows": [{"id": "fixture-flow-a"}]})
        assert request.url.params["include_activity"] == "false"
        return httpx.Response(
            200,
            json={
                "flow_id": "fixture-flow-a",
                "activity_included": False,
                "nodes": [{"id": "story-a"}, {"id": "story-b"}],
                "edges": [{"from_node_id": "story-a", "to_node_id": "story-b"}],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(response), base_url="https://example.invalid") as client:
        report = await collect_api(client, EXPECTED, readers=5)
    assert report["passed"] and len(report["attempts"]) == 200
    assert all(attempt["classification"] == "ok" for attempt in report["attempts"])
    assert "fixture-flow-a" not in json.dumps(report)


def test_invalid_reader_mode_is_refused():
    with pytest.raises(ValueError, match="reader mode"):
        evaluate_api([], readers=6)


def browser_attempts(readers=1):
    return [
        dict(
            operation=operation,
            reader=(sample - 1) % readers,
            sample=sample,
            classification="ok",
            meaningful_seconds=0.2,
            core_http_seconds=0.1,
            enrichment_seconds=None,
            status=200,
            api_attempts=2,
            warm_polls=0,
        )
        for operation in ("list", "graph")
        for sample in range(1, 21)
    ]


def test_browser_accounting_uses_nineteenth_sample_and_requires_all_twenty():
    records = browser_attempts(readers=5)
    records[18]["meaningful_seconds"] = 2.99
    records[19]["meaningful_seconds"] = 3.01
    report = evaluate_browser(records, readers=5)
    assert report["operations"]["list"]["gate_p95_seconds"] == 2.99
    assert report["passed"]
    records[18]["classification"] = "timeout"
    assert not evaluate_browser(records, readers=5)["operations"]["list"]["passed"]
    assert not evaluate_browser(records[:-1], readers=5)["operations"]["graph"]["complete"]


@pytest.mark.parametrize("invalid", [None, -1, float("nan"), float("inf"), True, "0.5"])
def test_browser_accounting_rejects_invalid_measurements(invalid):
    records = browser_attempts()
    records[0]["meaningful_seconds"] = invalid
    report = evaluate_browser(records, readers=1)
    assert not report["operations"]["list"]["complete"]
    assert not report["passed"]


@pytest.mark.asyncio
async def test_command_wires_api_browser_and_private_evidence(tmp_path, monkeypatch):
    gateway = Path(__file__).resolve().parents[2]
    scripts = gateway / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("flow_read_command", scripts / "benchmark-flow-reads.py")
    command = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(command)
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps({"access_token": "private-token-sentinel", "id_token": "private-id-sentinel", "expiry_ms": 9999999999999}))
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps(
            {
                "frontend_url": "https://example.invalid",
                "api_base_url": "https://example.invalid/api",
                "auth_file": str(auth_file),
                "list_flow_ids": ["fixture-flow"],
                "graph_flow_id": "fixture-flow",
                "graph_title": "Fixture flow",
                "graph_node_ids": ["story-1"],
                "graph_node_refs": ["story-1"],
                "graph_edges": [],
            }
        )
    )

    def mock_response(request):
        if request.url.path.endswith("/flows"):
            return httpx.Response(200, json={"flows": [{"id": "fixture-flow"}]})
        assert request.url.params["include_activity"] == "false"
        return httpx.Response(200, json={"flow_id": "fixture-flow", "activity_included": False, "nodes": [{"id": "story-1"}], "edges": []})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(command.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(mock_response), **kwargs))
    monkeypatch.setattr(command.subprocess, "check_output", lambda *args, **kwargs: "0" * 40)

    async def mock_browser(environment, budget):
        assert environment["FLOW_READ_REMAINING_ATTEMPTS"] == "2600"
        assert "private-token-sentinel" not in str(budget.output)
        for readers in (1, 5):
            (Path(environment["FLOW_READ_CAPTURE_DIR"]) / f"browser-{readers}.json").write_text(
                json.dumps({"readers": readers, "planned_per_operation": 20, "attempts": browser_attempts(readers)})
            )
        budget.browser_log.write_text("1\n" * 160)
        return 0

    monkeypatch.setattr(command, "run_browser", mock_browser)
    result_dir = tmp_path / "evidence"
    assert await command.run_benchmark(config_file, result_dir)
    evidence = (result_dir / "evidence.json").read_text()
    assert "private-token-sentinel" not in evidence and "example.invalid" not in evidence
    parsed = json.loads(evidence)
    assert parsed["complete"] and len(parsed["modes"]) == 4
    assert all(mode["passed"] for mode in parsed["modes"])
    assert sum(attempt["api_attempts"] for mode in parsed["modes"] if mode["phase"] == "browser" for attempt in mode["attempts"]) == 160
    assert parsed["accounting"]["api_attempts"] == 400
    assert parsed["accounting"]["browser_attempts"] == 160


def test_ownership_budget_enforces_attempts_time_and_failure_streak(tmp_path, monkeypatch):
    import importlib.util

    command_path = Path(__file__).resolve().parents[2] / "scripts/benchmark-flow-reads.py"
    monkeypatch.syspath_prepend(str(command_path.parent))
    spec = importlib.util.spec_from_file_location("flow_read_budget", command_path)
    command = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(command)
    now = [0.0]
    monkeypatch.setattr(command, "MAX_ATTEMPTS", 3)
    budget = command.RunBudget(tmp_path, clock=lambda: now[0])
    budget.reserve()
    budget.record(ApiAttempt("list", 0, 1, 0.1, "ok", 200, True, 1))
    budget.browser_log.write_text("1\n1\n")
    with pytest.raises(RuntimeError, match="budget"):
        budget.reserve()
    assert json.loads((tmp_path / "ownership.json").read_text())["state"] == "incomplete"
    assert json.loads((tmp_path / "api-attempts.jsonl").read_text())["sample"] == 1
    budget.browser_log.write_text("")
    now[0] = 1_201
    with pytest.raises(RuntimeError, match="budget"):
        budget.reserve()
    now[0] = 2
    for sample in range(5):
        budget.record(ApiAttempt("list", 0, sample, 0.1, "timeout", None, False, None))
    with pytest.raises(RuntimeError, match="budget"):
        budget.reserve()
    manifest = json.loads((tmp_path / "ownership.json").read_text())
    assert manifest["consecutive_failures"] == 5 and manifest["attempt_limit"] == 3


@pytest.mark.asyncio
async def test_interrupted_api_run_retains_partial_attempts_and_incomplete_ownership(tmp_path, monkeypatch):
    import importlib.util

    scripts = Path(__file__).resolve().parents[2] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("flow_read_interrupted", scripts / "benchmark-flow-reads.py")
    command = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(command)
    monkeypatch.setattr(
        command,
        "load_configuration",
        lambda path: (
            {"api_base_url": "https://example.invalid", "frontend_url": "https://example.invalid"},
            {"access_token": "private-token-sentinel"},
            EXPECTED,
            "test-revision",
        ),
    )
    monkeypatch.setattr(command.subprocess, "check_output", lambda *args, **kwargs: "0" * 40)

    async def interrupted(client, expected, *, readers, reserve, record):
        reserve()
        record(ApiAttempt("list", 0, 1, 0.1, "ok", 200, True, 1))
        raise KeyboardInterrupt

    monkeypatch.setattr(command, "collect_api", interrupted)
    output = tmp_path / "run"
    assert not await command.run_benchmark(tmp_path / "config.json", output)
    evidence = json.loads((output / "evidence.json").read_text())
    ownership = json.loads((output / "ownership.json").read_text())
    assert evidence["incomplete_reason"] == "KeyboardInterrupt" and not evidence["complete"]
    assert ownership["state"] == "incomplete" and ownership["api_attempts"] == 1
    assert len((output / "api-attempts.jsonl").read_text().splitlines()) == 1
    assert "private-token-sentinel" not in (output / "evidence.json").read_text()


def test_cleanup_restores_owned_workspace_without_touching_inputs_or_protected_results(tmp_path, monkeypatch):
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("flow_read_cleanup", scripts / "benchmark-flow-reads.py")
    command = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(command)
    output = tmp_path / "run"
    output.mkdir()
    budget = command.RunBudget(output)
    (budget.workspace / "browser-artifacts").mkdir()
    (budget.workspace / "browser-artifacts" / "trace.zip").write_text("owned")
    (output / "evidence.json").write_text("protected")
    (tmp_path / "auth.json").write_text("private-input")

    first = command.cleanup_owned(budget.manifest)
    second = command.cleanup_owned(budget.manifest)
    assert first["complete"] and first["removed_workspace"]
    assert second["complete"] and not second["removed_workspace"]
    assert (output / "evidence.json").read_text() == "protected"
    assert (tmp_path / "auth.json").read_text() == "private-input"
    assert len((output / "cleanup-receipts.jsonl").read_text().splitlines()) == 2
    monkeypatch.setattr(sys, "argv", ["benchmark-flow-reads.py", "--cleanup", "--manifest", str(budget.manifest)])
    assert command.main() == 0


def test_cleanup_refuses_unowned_workspace_and_reports_failure(tmp_path, monkeypatch):
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("flow_read_cleanup_failure", scripts / "benchmark-flow-reads.py")
    command = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(command)
    output = tmp_path / "run"
    output.mkdir()
    budget = command.RunBudget(output)
    external = tmp_path / "foreign"
    external.mkdir()
    (external / "private").write_text("preserve")
    shutil.rmtree(budget.workspace)
    budget.workspace.symlink_to(external, target_is_directory=True)

    receipt = command.cleanup_owned(budget.manifest)
    assert not receipt["complete"] and receipt["workspace_remains"]
    assert "workspace_cleanup_failed" in receipt["failures"]
    assert (external / "private").read_text() == "preserve"
    assert not command.cleanup_owned(budget.manifest)["complete"]
    assert json.loads(budget.manifest.read_text())["state"] == "incomplete"

    monkeypatch.setattr(
        command, "load_configuration", lambda path: ({"api_base_url": "https://example.invalid"}, {"access_token": "private"}, EXPECTED, "revision")
    )
    retry = tmp_path / "retry"
    assert not asyncio.run(command.run_benchmark(tmp_path / "config.json", retry, budget.manifest))
    assert not retry.exists() and (external / "private").read_text() == "preserve"


def test_cleanup_stops_only_identified_owned_clients(tmp_path, monkeypatch):
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("flow_read_client_cleanup", scripts / "benchmark-flow-reads.py")
    command = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(command)
    output = tmp_path / "run"
    output.mkdir()
    budget = command.RunBudget(output)
    budget.client_pid = 1234
    budget.client_start = "start-time"
    budget.save()
    alive = [True]
    calls = []
    monkeypatch.setattr(command, "process_start", lambda pid: "start-time" if alive[0] else None)
    monkeypatch.setattr(command, "active_owned_group", lambda pid, start, owner=None: alive[0])
    monkeypatch.setattr(command, "marked_clients", lambda owner, start: [])

    def terminate(pid, signal):
        calls.append((pid, signal))
        alive[0] = False

    monkeypatch.setattr(command.os, "killpg", terminate)
    receipt = command.cleanup_owned(budget.manifest)
    assert receipt["complete"] and receipt["client_stopped"]
    assert calls == [(1234, command.signal.SIGTERM)]
    assert command.cleanup_owned(budget.manifest)["complete"]
    assert len(calls) == 1


def test_owned_fixture_remnants_remain_incomplete_and_visible(tmp_path, monkeypatch):
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("flow_read_fixture_cleanup", scripts / "benchmark-flow-reads.py")
    command = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(command)
    output = tmp_path / "run"
    output.mkdir()
    budget = command.RunBudget(output)
    manifest = json.loads(budget.manifest.read_text())
    manifest["owned_fixtures"] = ["synthetic-transaction-not-rolled-back"]
    budget.manifest.write_text(json.dumps(manifest))

    receipt = command.cleanup_owned(budget.manifest)
    assert receipt["owned_fixture_remnants"] == ["synthetic-transaction-not-rolled-back"]
    assert not receipt["complete"]
    assert json.loads(budget.manifest.read_text())["state"] == "incomplete"


@pytest.mark.asyncio
async def test_rerun_recovers_same_manifest_and_keeps_previous_result(tmp_path, monkeypatch):
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("flow_read_rerun", scripts / "benchmark-flow-reads.py")
    command = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(command)
    first_output = tmp_path / "aborted"
    first_output.mkdir()
    previous = command.RunBudget(first_output)
    previous.reserve()
    previous.record(ApiAttempt("list", 0, 1, 0.1, "timeout", None, False, None))
    (first_output / "evidence.json").write_text("preserved interrupted evidence")
    (previous.workspace / "browser-artifacts").mkdir()
    auth_file = tmp_path / "auth.json"
    auth_file.write_text("private input")
    monkeypatch.setattr(
        command,
        "load_configuration",
        lambda path: (
            {"api_base_url": "https://example.invalid", "frontend_url": "https://example.invalid"},
            {"access_token": "private-token-sentinel"},
            EXPECTED,
            "test-revision",
        ),
    )
    monkeypatch.setattr(command.subprocess, "check_output", lambda *args, **kwargs: "0" * 40)

    async def collect(client, expected, *, readers, reserve, record):
        for sample in range(1, 101):
            for operation in ("list", "graph"):
                reserve()
                record(ApiAttempt(operation, (sample - 1) % readers, sample, 0.1, "ok", 200, True, 1))
        return evaluate_api(
            [
                ApiAttempt(operation, (sample - 1) % readers, sample, 0.1, "ok", 200, True, 1)
                for operation in ("list", "graph")
                for sample in range(1, 101)
            ],
            readers=readers,
        )

    async def browser(environment, budget):
        assert environment["FLOW_READ_ARTIFACT_DIR"] == str((budget.workspace / "browser-artifacts").resolve())
        for readers in (1, 5):
            (budget.output / f"browser-{readers}.json").write_text(json.dumps({"attempts": browser_attempts(readers)}))
        budget.browser_log.write_text("1\n" * 160)
        return 0

    monkeypatch.setattr(command, "collect_api", collect)
    monkeypatch.setattr(command, "run_browser", browser)
    next_output = tmp_path / "retry"
    assert await command.run_benchmark(tmp_path / "config.json", next_output, previous.manifest)
    assert (first_output / "evidence.json").read_text() == "preserved interrupted evidence"
    assert not previous.workspace.exists() and not (next_output / "workspace").exists()
    assert auth_file.read_text() == "private input"
    assert json.loads(previous.manifest.read_text())["owned_output"] == str(next_output.resolve())
    assert json.loads(previous.manifest.read_text())["state"] == "complete"
    assert json.loads((first_output / "cleanup.json").read_text())["complete"]
    assert json.loads((next_output / "cleanup.json").read_text())["complete"]


def test_ci_browser_job_is_explicitly_opt_in():
    workflow = Path(__file__).resolve().parents[4] / ".github/workflows/gateway-ci.yml"
    definition = yaml.safe_load(workflow.read_text())
    browser_job = definition["jobs"]["flow-read-browser"]
    assert "workflow_dispatch" in browser_job["if"] and "inputs.flow_read_browser" in browser_job["if"]
    assert any("flow-read.config.ts" in step.get("run", "") for step in browser_job["steps"])


@pytest.mark.asyncio
async def test_http_sample_enforces_whole_request_deadline(monkeypatch):
    import asyncio

    from scripts import benchmark_flow_reads

    monkeypatch.setattr(benchmark_flow_reads, "REQUEST_TIMEOUT_SECONDS", 0.01)

    async def slow_response(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json={"flows": [{"id": "fixture-flow-a"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(slow_response), base_url="https://example.invalid") as client:
        attempt = await sample_api(client, EXPECTED, operation="list", reader=0, sample=1)
    assert attempt.classification == "timeout" and not attempt.success
    assert attempt.elapsed_seconds < 0.5


def test_scn_01_manifest_selects_real_postgres_fixture_and_keeps_query_gate_distinct():
    location = Path(__file__).with_name("flow_read_command_manifest.json")
    case = json.loads(location.read_text())["scenarios"]["SCN-01"]
    commands = case["commands"]
    from tests.orchestration.flow_read_fixture import DECISION_COUNT, FLOW_COUNT, HISTORY_COUNT, HOPS, NODES_PER_FLOW

    assert case["acceptance_ids"] == ["AC-01"]
    assert case["catalog_scenario"] == "MOD-018-F001-S001"
    assert case["working_directory"] == "modules/gateway"
    assert "PostgreSQL 16" in case["prerequisites"]["database"]
    assert "never a skipped or SQLite success" in case["prerequisites"]["missing_database_or_dependency"]
    assert (
        case["fixture"]["flows"],
        case["fixture"]["nodes_per_flow"],
        case["fixture"]["decisions"],
        case["fixture"]["histories"],
        case["fixture"]["amendment_hops"],
    ) == (FLOW_COUNT, NODES_PER_FLOW, DECISION_COUNT, HISTORY_COUNT, list(HOPS))
    assert "::test_history_scale_regression" in commands["fixture_and_semantics"]
    assert "::test_bounded_history_queries" in commands["bounded_query_growth"]
    assert "::test_injected_per_hop_query_regression_fails" in commands["injected_regression"]
    assert all("${FLOW_READ_EVIDENCE_DIR:?" in command and "--junitxml=" in command for command in commands.values())
    assert all("-o junit_family=legacy" in command for command in commands.values())
    assert all(
        property_name in case["evidence"]["format"]
        for property_name in (
            "fixture_manifest",
            "fixture_source_sha256",
            "source_revision",
        )
    )


@pytest.mark.asyncio
async def test_scn_02_abort_rerun_receipts(tmp_path, monkeypatch, record_property):
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("flow_read_scenario_recovery", scripts / "benchmark-flow-reads.py")
    command = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(command)
    private_config = tmp_path / "private-config.json"
    private_config.write_text("private input remains intact")
    monkeypatch.setattr(
        command,
        "load_configuration",
        lambda path: (
            {"api_base_url": "https://example.invalid", "frontend_url": "https://example.invalid"},
            {"access_token": "private-token-sentinel"},
            EXPECTED,
            "fixture-config-revision",
        ),
    )
    monkeypatch.setattr(command.subprocess, "check_output", lambda *args, **kwargs: "0" * 40)

    async def interrupted(client, expected, *, readers, reserve, record):
        reserve()
        record(ApiAttempt("list", 0, 1, 0.1, "ok", 200, True, 1))
        raise KeyboardInterrupt

    monkeypatch.setattr(command, "collect_api", interrupted)
    first_output = tmp_path / "run-before"
    assert not await command.run_benchmark(private_config, first_output)
    manifest = first_output / "ownership.json"
    first = json.loads((first_output / "evidence.json").read_text())
    assert first["incomplete_reason"] == "KeyboardInterrupt" and first["accounting"]["api_attempts"] == 1
    assert first["cleanup"]["complete"] and not first["cleanup"]["workspace_remains"]
    assert json.loads(manifest.read_text())["state"] == "incomplete"
    assert len((first_output / "api-attempts.jsonl").read_text().splitlines()) == 1
    before_receipt = (first_output / "evidence.json").read_text()

    def response(request):
        if request.url.path.endswith("/flows"):
            return httpx.Response(200, json={"flows": [{"id": "fixture-flow-a"}]})
        assert request.url.params["include_activity"] == "false"
        return httpx.Response(
            200,
            json={
                "activity_included": False,
                "flow_id": "fixture-flow-a",
                "nodes": [{"id": "story-a"}, {"id": "story-b"}],
                "edges": [{"from_node_id": "story-a", "to_node_id": "story-b"}],
            },
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(command.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(response), **kwargs))
    monkeypatch.setattr(command, "collect_api", collect_api)

    async def browser(environment, budget):
        assert environment["FLOW_READ_REMAINING_ATTEMPTS"] == "2600"
        for readers in (1, 5):
            (budget.output / f"browser-{readers}.json").write_text(json.dumps({"attempts": browser_attempts(readers)}))
        budget.browser_log.write_text("1\n" * 160)
        return 0

    monkeypatch.setattr(command, "run_browser", browser)
    second_output = tmp_path / "run-after"
    assert await command.run_benchmark(private_config, second_output, manifest)
    second = json.loads((second_output / "evidence.json").read_text())
    assert second["complete"] and second["accounting"]["api_attempts"] == 400
    assert second["accounting"]["browser_attempts"] == 160
    assert second["cleanup"]["complete"] and not second["cleanup"]["owned_fixture_remnants"]
    assert json.loads(manifest.read_text())["owned_output"] == str(second_output.resolve())
    assert json.loads(manifest.read_text())["state"] == "complete"
    assert len((first_output / "cleanup-receipts.jsonl").read_text().splitlines()) == 2
    assert command.cleanup_owned(manifest)["complete"]
    assert (first_output / "evidence.json").read_text() == before_receipt
    assert private_config.read_text() == "private input remains intact"
    assert "private-token-sentinel" not in json.dumps(second)
    record_property(
        "cleanup_receipts",
        json.dumps(
            {
                "aborted_incomplete": not first["complete"],
                "aborted_attempts": first["accounting"]["api_attempts"],
                "rerun_complete": second["complete"],
                "rerun_attempts": second["accounting"]["api_attempts"] + second["accounting"]["browser_attempts"],
                "idempotent_cleanup": True,
                "old_evidence_preserved": True,
            }
        ),
    )


def test_scn_02_manifest_pins_recovery_commands_and_protected_evidence():
    manifest = json.loads(Path(__file__).with_name("flow_read_command_manifest.json").read_text())
    scenario = manifest["scenarios"]["SCN-02"]
    commands = scenario["commands"]
    assert scenario["working_directory"] == "modules/gateway"
    assert {"AC-02", "AC-03"} == set(scenario["acceptance_ids"])
    assert "::test_scn_02_abort_rerun_receipts" in commands["self_test"]
    assert "--junitxml=" in commands["self_test"] and "junit_family=legacy" in commands["self_test"]
    assert all("${FLOW_READ_EVIDENCE_DIR" in commands[name] for name in ("before", "cleanup", "rerun"))
    assert "--cleanup --manifest" in commands["cleanup"]
    assert "--manifest" in commands["rerun"] and "run-after" in commands["rerun"]
    assert "run-before/ownership.json" in commands["rerun"]
    assert "owned_fixture_remnants" in scenario["evidence"]["cleanup"]
    assert "source_revision" in scenario["evidence"]["measurement"]
    frontend = scenario["frontend_verification"]
    assert "FlowsList.test.tsx" in frontend["unit"]
    assert "flow-read.config.ts" in frontend["controlled_browser"]
    assert "flow-read-benchmark.config.ts" in frontend["sampler"]


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["", "real flow & agents"])
async def test_real_workspace_selection_is_explicit_and_still_checks_content(query):
    from dataclasses import replace

    expected = replace(EXPECTED, list_query=query)

    def handler(request):
        assert request.url.params["q"] == query
        assert request.url.params["limit"] == "25"
        return httpx.Response(200, json={"flows": [{"id": "foreign-flow"}]})

    async with httpx.AsyncClient(base_url="https://example.invalid", transport=httpx.MockTransport(handler)) as client:
        result = await sample_api(client, expected, operation="list", reader=0, sample=1)
    assert result.classification == "content_violation"
    assert not result.success
