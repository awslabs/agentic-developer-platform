"""Task host process, reporting, isolation, and finalization fixtures."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.task_dispatch import parse_task_envelope
from lib.task_gateway_client import TaskGatewayError
from lib.task_host import TASK_EXIT_FAILED, TASK_EXIT_RETRYABLE, TaskHost, _stop_process

CONTRACTS = Path(__file__).resolve().parents[4] / "docs/task-api/contracts/v1/fixtures/valid"


def fixture(name: str) -> dict:
    value = json.loads((CONTRACTS / name).read_text())
    value.pop("$fixture", None)
    return value


@pytest.fixture
def assignment_and_bootstrap():
    envelope = fixture("envelope-dispatch.json")
    envelope["input_ref"]["artifact_refs"] = []
    assignment = parse_task_envelope(envelope)
    bootstrap = fixture("bootstrap-response.json")
    bootstrap["input"]["artifacts"] = []
    deadline = datetime.now(UTC) + timedelta(minutes=5)
    bootstrap["deadline_at"] = deadline.strftime("%Y-%m-%dT%H:%M:%SZ")
    bootstrap["limits"]["deadline_at"] = bootstrap["deadline_at"]
    return assignment, envelope, bootstrap


class FakeHeartbeat:
    def __init__(self, events):
        self.events = events

    def stop(self):
        self.events.append("heartbeat.stop")


class FakeClient:
    def __init__(
        self,
        bootstrap,
        events,
        *,
        report_failure=False,
        model_response=None,
        cancel=False,
    ):
        self.bootstrap_response = bootstrap
        self.events = events
        self.report_failure = report_failure
        self.model_response = model_response
        self.cancel = cancel
        self.attempt_body = None
        self.finalize_body = None
        self.settlements = []

    def bootstrap(self, body):
        self.events.append("bootstrap")
        return copy.deepcopy(self.bootstrap_response)

    def attempt(self, body):
        self.events.append("attempt")
        self.attempt_body = copy.deepcopy(body)
        return {"schema_version": "1.0", "operation_status": "confirmed", "request_id": body["runtime_attempt_id"]}

    def control(self, body):
        return {
            "schema_version": "1.0",
            "task_id": body["attempt"]["run"]["task_id"],
            "cancel_requested": self.cancel,
            "cancel_command_id": ("f6071829-3a4b-4c5d-9f70-819203142536" if self.cancel else None),
            "pending_input_count": 0,
            "last_receipt_cursor": None,
            "attempt_valid": True,
        }

    def report(self, body):
        self.events.append("report:" + body["data"].get("message", "input"))
        if self.report_failure:
            from lib.task_run_client import TaskRunClientError

            raise TaskRunClientError("report unavailable")
        return {
            "schema_version": "1.0",
            "report_id": body["report_id"],
            "sequence": len([event for event in self.events if event.startswith("report:")]),
            "event_id": f"{body['attempt']['run']['task_id']}:1",
        }

    def turn(self, body):
        number = body["expected_transcript_version"]
        return {
            "schema_version": "1.0",
            "operation_status": "committed",
            "pending_input_count": 0,
            "turn": {
                "task_id": self.bootstrap_response["task_id"],
                "turn_id": body["request_id"],
                "turn_number": number,
                "transcript_version": number + 1,
                "command_ids": [],
            },
            "messages": [],
        }

    def model(self, body):
        self.events.append("model")
        return copy.deepcopy(self.model_response)

    def finalize(self, body):
        self.events.append("finalize:" + body["outcome"])
        self.finalize_body = body
        return {
            "schema_version": "1.0",
            "task_id": body["attempt"]["run"]["task_id"],
            "status": body["outcome"],
            "version": 4,
            "terminal_event_id": f"{body['attempt']['run']['task_id']}:4",
        }

    def settlement(self, body):
        self.events.append("settlement")
        self.settlements.append(body)
        return {
            "schema_version": "1.0",
            "operation_status": "confirmed",
            "stop_only": True,
            "queue_ack_status": "unknown",
        }

    def clear_credential(self):
        self.events.append("credential.clear")


def child_script(path: Path, *, model=False) -> Path:
    script = path / "task-child.py"
    frames = """
import json, os, socket, sys, uuid
start = json.loads(sys.stdin.readline())
assert start['type'] == 'start'
assert not any(name.startswith(('AWS_', 'GH_', 'GITHUB_')) for name in os.environ)
try:
    socket.socket()
except PermissionError:
    pass
else:
    raise RuntimeError('task child unexpectedly has network access')
task_id = start['task_id']
def send(value):
    print(json.dumps(value), flush=True)
def base(kind):
    return {'protocol_version': 1, 'type': kind, 'request_id': str(uuid.uuid4()), 'task_id': task_id}
ready = base('ready'); ready['capabilities'] = ['input', 'cancel']; send(ready)
for index in range(2):
    progress = base('progress'); progress.update({'report_id': str(uuid.uuid4()), 'message': f'inspected evidence {index}', 'stage': 'analysis'}); send(progress)
    assert json.loads(sys.stdin.readline())['type'] == 'report.ack'
MODEL_BLOCK
report = {'summary': 'Bounded fixture completed', 'findings': [{'statement': 'The supplied evidence supports the result.', 'evidence_refs': ['fixture:L1'], 'confidence': 'high'}], 'uncertainties': [], 'recommendations': [], 'evidence_refs': [{'ref': 'fixture:L1', 'source': 'inputs'}]}
result = base('result'); result['report'] = report; send(result)
"""
    model_block = ""
    if model:
        model_block = "model = base('model.request'); model.update({'turn_id': str(uuid.uuid4()), 'messages': [{'role': 'user', 'content': 'analyze'}], 'max_tokens': 32}); send(model); sys.stdin.readline()"
    script.write_text(frames.replace("MODEL_BLOCK", model_block))
    return script


def cancellation_child_script(path: Path, *, ignore_cancel=False) -> Path:
    script = path / "task-cancellation-child.py"
    behavior = """
import json, signal, sys, time, uuid
start = json.loads(sys.stdin.readline())
task_id = start['task_id']
def base(kind):
    return {'protocol_version': 1, 'type': kind, 'request_id': str(uuid.uuid4()), 'task_id': task_id}
ready = base('ready'); ready['capabilities'] = ['cancel']; print(json.dumps(ready), flush=True)
cancel = json.loads(sys.stdin.readline())
BEHAVIOR
"""
    if ignore_cancel:
        action = "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
    else:
        action = "receipt = base('cancelled'); receipt['command_id'] = cancel['command_id']; print(json.dumps(receipt), flush=True)"
    script.write_text(behavior.replace("BEHAVIOR", action))
    return script


def burst_child_script(path: Path, *, progress_count: int, include_model: bool = False) -> Path:
    script = path / "task-burst-child.py"
    model_frame = ""
    model_reads = ""
    if include_model:
        model_frame = "model = base('model.request'); model.update({'turn_id': str(uuid.uuid4()), 'messages': [{'role': 'user', 'content': 'analyze'}]}); send(model)"
        model_reads = "while json.loads(sys.stdin.readline())['type'] != 'model.result': pass"
    script.write_text(
        f"""
import json, sys, uuid
start = json.loads(sys.stdin.readline()); task_id = start['task_id']
def base(kind): return {{'protocol_version': 1, 'type': kind, 'request_id': str(uuid.uuid4()), 'task_id': task_id}}
def send(value): print(json.dumps(value), flush=True)
ready = base('ready'); ready['capabilities'] = ['cancel']; send(ready)
for index in range({progress_count}):
    progress = base('progress'); progress.update({{'report_id': str(uuid.uuid4()), 'message': f'burst evidence {{index}}', 'stage': 'analysis'}}); send(progress)
{model_frame}
{model_reads}
report = {{'summary': 'Burst fixture completed', 'findings': [{{'statement': 'Evidence was processed.', 'evidence_refs': ['fixture:L1']}}], 'uncertainties': [], 'recommendations': [], 'evidence_refs': [{{'ref': 'fixture:L1', 'source': 'inputs'}}]}}
result = base('result'); result['report'] = report; send(result)
"""
    )
    return script


def batched_child_script(path: Path) -> Path:
    script = path / "task-batched-child.py"
    script.write_text(
        """
import json, sys, uuid
start = json.loads(sys.stdin.readline()); task_id = start['task_id']
def base(kind): return {'protocol_version': 1, 'type': kind, 'request_id': str(uuid.uuid4()), 'task_id': task_id}
frames = []
ready = base('ready'); ready['capabilities'] = ['cancel']; frames.append(ready)
for index in range(2):
    progress = base('progress'); progress.update({'report_id': str(uuid.uuid4()), 'message': f'batched evidence {index}', 'stage': 'analysis'}); frames.append(progress)
sys.stdout.write(''.join(json.dumps(frame) + '\\n' for frame in frames)); sys.stdout.flush()
assert json.loads(sys.stdin.readline())['type'] == 'report.ack'
assert json.loads(sys.stdin.readline())['type'] == 'report.ack'
report = {'summary': 'Batched fixture completed', 'findings': [{'statement': 'Evidence was processed.', 'evidence_refs': ['fixture:L1']}], 'uncertainties': [], 'recommendations': [], 'evidence_refs': [{'ref': 'fixture:L1', 'source': 'inputs'}]}
result = base('result'); result['report'] = report; print(json.dumps(result), flush=True)
"""
    )
    return script


def test_progress_and_result_are_durable_before_acknowledgement(
    tmp_path, monkeypatch, assignment_and_bootstrap
):
    assignment, envelope, bootstrap = assignment_and_bootstrap
    events = []
    client = FakeClient(bootstrap, events)
    executable = child_script(tmp_path)
    monkeypatch.setattr(
        "lib.task_host.workload_identity",
        lambda: {"pod_uid": str(__import__("uuid").uuid4()), "namespace": "test"},
    )
    host = TaskHost(
        client=client,
        work_root=tmp_path / "work",
        command_resolver=lambda persona: [sys.executable, str(executable)],
    )

    def acknowledge():
        events.append("ack")

    assert (
        host.run(
            assignment,
            envelope,
            heartbeat=FakeHeartbeat(events),
            acknowledge=acknowledge,
        )
        == 0
    )
    assert events.index("report:inspected evidence 0") < events.index("finalize:completed")
    assert events.index("report:inspected evidence 1") < events.index("finalize:completed")
    assert events.index("finalize:completed") < events.index("heartbeat.stop") < events.index("ack")
    assert client.finalize_body["result"]["process_exit_validated"] is True
    assert client.attempt_body["capabilities"] == ["cancel"]
    assert list((tmp_path / "work").iterdir()) == []


def test_multiple_ndjson_frames_buffered_in_one_pipe_read_are_all_processed(
    tmp_path, monkeypatch, assignment_and_bootstrap
):
    assignment, envelope, bootstrap = assignment_and_bootstrap
    events = []
    client = FakeClient(bootstrap, events)
    executable = batched_child_script(tmp_path)
    monkeypatch.setattr(
        "lib.task_host.workload_identity",
        lambda: {"pod_uid": str(__import__("uuid").uuid4()), "namespace": "test"},
    )
    host = TaskHost(
        client=client,
        work_root=tmp_path / "work",
        command_resolver=lambda persona: [sys.executable, str(executable)],
    )
    assert (
        host.run(
            assignment,
            envelope,
            heartbeat=FakeHeartbeat(events),
            acknowledge=lambda: events.append("ack"),
        )
        == 0
    )
    assert "report:batched evidence 0" in events
    assert "report:batched evidence 1" in events


def test_reporting_failure_terminates_child_leaves_assignment_unacked_and_cleans_workspace(
    tmp_path, monkeypatch, assignment_and_bootstrap
):
    monkeypatch.setattr("lib.task_host._REPORT_BUFFER_MAX_SECONDS", 0.05)
    monkeypatch.setattr("lib.task_host._REPORT_RETRY_SECONDS", 0.01)
    monkeypatch.setattr("lib.task_host._TERM_AFTER_SECONDS", 0.01)
    monkeypatch.setattr("lib.task_host._KILL_AFTER_SECONDS", 0.02)
    assignment, envelope, bootstrap = assignment_and_bootstrap
    events = []
    client = FakeClient(bootstrap, events, report_failure=True)
    executable = child_script(tmp_path)
    monkeypatch.setattr(
        "lib.task_host.workload_identity",
        lambda: {"pod_uid": str(__import__("uuid").uuid4()), "namespace": "test"},
    )
    host = TaskHost(
        client=client,
        work_root=tmp_path / "work",
        command_resolver=lambda persona: [sys.executable, str(executable)],
    )
    assert (
        host.run(
            assignment,
            envelope,
            heartbeat=FakeHeartbeat(events),
            acknowledge=lambda: events.append("ack"),
        )
        == TASK_EXIT_RETRYABLE
    )
    assert "ack" not in events
    assert "settlement" in events
    assert list((tmp_path / "work").iterdir()) == []


def test_transient_reporting_outage_replays_reports_before_completion(
    tmp_path, monkeypatch, assignment_and_bootstrap
):
    assignment, envelope, bootstrap = assignment_and_bootstrap
    events = []
    client = FakeClient(bootstrap, events)
    failures = 2
    successful_report = client.report

    def flaky_report(body):
        nonlocal failures
        if failures:
            failures -= 1
            events.append("report.failed")
            from lib.task_run_client import TaskRunClientError

            raise TaskRunClientError("report unavailable")
        events.append("report.recovered")
        return successful_report(body)

    client.report = flaky_report
    executable = child_script(tmp_path)
    monkeypatch.setattr("lib.task_host._REPORT_RETRY_SECONDS", 0.01)
    monkeypatch.setattr(
        "lib.task_host.workload_identity",
        lambda: {"pod_uid": str(__import__("uuid").uuid4()), "namespace": "test"},
    )
    host = TaskHost(
        client=client,
        work_root=tmp_path / "work",
        command_resolver=lambda persona: [sys.executable, str(executable)],
    )
    assert host.run(
        assignment,
        envelope,
        heartbeat=FakeHeartbeat(events),
        acknowledge=lambda: events.append("ack"),
    ) == 0
    assert events.count("report.failed") == 2
    assert events.index("report.recovered") < events.index("finalize:completed")
    assert events.index("finalize:completed") < events.index("ack")


@pytest.mark.parametrize(
    ("limit_name", "limit"),
    [("_REPORT_BUFFER_MAX_REPORTS", 2), ("_REPORT_BUFFER_MAX_BYTES", 1)],
)
def test_report_buffer_overflow_stops_without_finalizing_or_acknowledging(
    tmp_path, monkeypatch, assignment_and_bootstrap, limit_name, limit
):
    assignment, envelope, bootstrap = assignment_and_bootstrap
    events = []
    client = FakeClient(bootstrap, events, report_failure=True)
    executable = burst_child_script(tmp_path, progress_count=3)
    monkeypatch.setattr(f"lib.task_host.{limit_name}", limit)
    monkeypatch.setattr("lib.task_host._TERM_AFTER_SECONDS", 0.01)
    monkeypatch.setattr("lib.task_host._KILL_AFTER_SECONDS", 0.02)
    monkeypatch.setattr(
        "lib.task_host.workload_identity",
        lambda: {"pod_uid": str(__import__("uuid").uuid4()), "namespace": "test"},
    )
    host = TaskHost(
        client=client,
        work_root=tmp_path / "work",
        command_resolver=lambda persona: [sys.executable, str(executable)],
    )
    assert host.run(
        assignment,
        envelope,
        heartbeat=FakeHeartbeat(events),
        acknowledge=lambda: events.append("ack"),
    ) == TASK_EXIT_RETRYABLE
    assert client.finalize_body is None
    assert "ack" not in events
    assert client.settlements[-1]["stop_evidence"]["child_exit_confirmed"] is True


def test_terminal_result_does_not_consume_nonterminal_report_capacity(
    tmp_path, monkeypatch, assignment_and_bootstrap
):
    assignment, envelope, bootstrap = assignment_and_bootstrap
    events = []
    client = FakeClient(bootstrap, events, report_failure=True)
    executable = burst_child_script(tmp_path, progress_count=2)
    monkeypatch.setattr("lib.task_host._REPORT_BUFFER_MAX_REPORTS", 2)
    monkeypatch.setattr("lib.task_host._REPORT_BUFFER_MAX_SECONDS", 0.3)
    monkeypatch.setattr("lib.task_host._REPORT_RETRY_SECONDS", 0.01)
    monkeypatch.setattr("lib.task_host._TERM_AFTER_SECONDS", 0.01)
    monkeypatch.setattr("lib.task_host._KILL_AFTER_SECONDS", 0.02)
    monkeypatch.setattr(
        "lib.task_host.workload_identity",
        lambda: {"pod_uid": str(__import__("uuid").uuid4()), "namespace": "test"},
    )
    host = TaskHost(
        client=client,
        work_root=tmp_path / "work",
        command_resolver=lambda persona: [sys.executable, str(executable)],
    )
    assert host.run(
        assignment,
        envelope,
        heartbeat=FakeHeartbeat(events),
        acknowledge=lambda: events.append("ack"),
    ) == TASK_EXIT_RETRYABLE
    assert len([event for event in events if event.startswith("report:")]) > 1
    assert client.finalize_body is None
    assert "ack" not in events


def test_model_admission_waits_for_report_storage_recovery(
    tmp_path, monkeypatch, assignment_and_bootstrap
):
    assignment, envelope, bootstrap = assignment_and_bootstrap
    events = []
    client = FakeClient(bootstrap, events)
    failures = 1
    successful_report = client.report

    def flaky_report(body):
        nonlocal failures
        if failures:
            failures -= 1
            events.append("report.failed")
            from lib.task_run_client import TaskRunClientError

            raise TaskRunClientError("report unavailable")
        events.append("report.recovered")
        return successful_report(body)

    client.report = flaky_report
    client.model_response = {
        "schema_version": "1.0",
        "task_id": assignment.task_id,
        "turn_id": "placeholder",
        "operation_status": "confirmed", "handoff": "confirmed",
        "content": [{"type": "text", "text": "Done"}], "stop_reason": "end_turn",
    }

    def model(body):
        client.model_response.update(turn_id=body["turn_id"], request_digest=body["request_digest"], automatic_replay_permitted=False)
        return FakeClient.model(client, body)

    client.model = model
    executable = burst_child_script(tmp_path, progress_count=1, include_model=True)
    monkeypatch.setattr("lib.task_host._REPORT_RETRY_SECONDS", 0.01)
    monkeypatch.setattr("lib.task_host._MIN_PROGRESS_MARKERS", 1)
    monkeypatch.setattr(
        "lib.task_host.workload_identity",
        lambda: {"pod_uid": str(__import__("uuid").uuid4()), "namespace": "test"},
    )
    host = TaskHost(
        client=client,
        work_root=tmp_path / "work",
        command_resolver=lambda persona: [sys.executable, str(executable)],
    )
    assert host.run(
        assignment,
        envelope,
        heartbeat=FakeHeartbeat(events),
        acknowledge=lambda: events.append("ack"),
    ) == 0
    assert events.index("report.recovered") < events.index("model")


def test_stop_process_reports_unknown_after_term_kill_and_wait_timeout(monkeypatch):
    class UnstoppableProcess:
        pid = 1234

        @staticmethod
        def poll():
            return None

        @staticmethod
        def wait(*, timeout):
            raise subprocess.TimeoutExpired("task-child", timeout)

    signals = []
    monkeypatch.setattr("lib.task_host._TERM_AFTER_SECONDS", 0)
    monkeypatch.setattr("lib.task_host._KILL_AFTER_SECONDS", 0)
    monkeypatch.setattr(
        "lib.task_host.os.killpg", lambda pid, sent_signal: signals.append((pid, sent_signal))
    )
    assert _stop_process(UnstoppableProcess()) is None
    assert signals == [(1234, __import__("signal").SIGTERM), (1234, __import__("signal").SIGKILL)]


def test_unconfirmed_child_stop_never_finalizes_or_acknowledges(
    tmp_path, monkeypatch, assignment_and_bootstrap
):
    monkeypatch.setattr("lib.task_host._REPORT_BUFFER_MAX_SECONDS", 0.05)
    monkeypatch.setattr("lib.task_host._REPORT_RETRY_SECONDS", 0.01)
    assignment, envelope, bootstrap = assignment_and_bootstrap
    events = []
    client = FakeClient(bootstrap, events, report_failure=True)
    executable = child_script(tmp_path)
    monkeypatch.setattr("lib.task_host._stop_process", lambda process: None)
    monkeypatch.setattr(
        "lib.task_host.workload_identity",
        lambda: {"pod_uid": str(__import__("uuid").uuid4()), "namespace": "test"},
    )
    host = TaskHost(
        client=client,
        work_root=tmp_path / "work",
        command_resolver=lambda persona: [sys.executable, str(executable)],
    )
    assert host.run(
        assignment,
        envelope,
        heartbeat=FakeHeartbeat(events),
        acknowledge=lambda: events.append("ack"),
    ) == TASK_EXIT_RETRYABLE
    assert client.finalize_body is None
    assert "ack" not in events
    assert client.settlements[-1]["stop_evidence"]["child_exit_confirmed"] is False


def test_unknown_persona_is_terminally_failed_without_starting_a_legacy_runtime(
    tmp_path, monkeypatch, assignment_and_bootstrap
):
    assignment, envelope, bootstrap = assignment_and_bootstrap
    bootstrap["persona"] = assignment.persona
    events = []
    client = FakeClient(bootstrap, events)
    monkeypatch.setattr(
        "lib.task_host.workload_identity",
        lambda: {"pod_uid": str(__import__("uuid").uuid4()), "namespace": "test"},
    )
    host = TaskHost(
        client=client,
        work_root=tmp_path / "work",
        command_resolver=lambda persona: (_ for _ in ()).throw(ValueError("not packaged")),
    )
    assert (
        host.run(
            assignment,
            envelope,
            heartbeat=FakeHeartbeat(events),
            acknowledge=lambda: events.append("ack"),
        )
        == TASK_EXIT_FAILED
    )
    assert "finalize:failed" in events
    assert events[-2:] == ["ack", "credential.clear"]


def test_confirmed_model_receipt_without_content_fails_honestly(
    tmp_path, monkeypatch, assignment_and_bootstrap
):
    assignment, envelope, bootstrap = assignment_and_bootstrap
    events = []
    client = FakeClient(
        bootstrap,
        events,
        model_response={
            "schema_version": "1.0",
            "task_id": assignment.task_id,
            "turn_id": "placeholder",
            "operation_status": "confirmed",
        },
    )

    def model(body):
        client.model_response.update(turn_id=body["turn_id"], request_digest=body["request_digest"], automatic_replay_permitted=False)
        return FakeClient.model(client, body)

    client.model = model
    executable = child_script(tmp_path, model=True)
    monkeypatch.setattr(
        "lib.task_host.workload_identity",
        lambda: {"pod_uid": str(__import__("uuid").uuid4()), "namespace": "test"},
    )
    host = TaskHost(
        client=client,
        work_root=tmp_path / "work",
        command_resolver=lambda persona: [sys.executable, str(executable)],
    )
    assert (
        host.run(
            assignment,
            envelope,
            heartbeat=FakeHeartbeat(events),
            acknowledge=lambda: events.append("ack"),
        )
        == TASK_EXIT_FAILED
    )
    assert client.finalize_body["error"]["code"] == "protocol_violation"
    assert events.index("finalize:failed") < events.index("ack")


def test_stale_bootstrap_identity_never_starts_or_acknowledges(
    tmp_path, monkeypatch, assignment_and_bootstrap
):
    assignment, envelope, bootstrap = assignment_and_bootstrap
    bootstrap["invocation_id"] = "11111111-2222-4333-8444-555555555555"
    events = []
    client = FakeClient(bootstrap, events)
    monkeypatch.setattr(
        "lib.task_host.workload_identity",
        lambda: {"pod_uid": str(__import__("uuid").uuid4()), "namespace": "test"},
    )
    host = TaskHost(
        client=client,
        work_root=tmp_path / "work",
        command_resolver=lambda persona: (_ for _ in ()).throw(
            AssertionError("stale assignment started a child")
        ),
    )
    assert (
        host.run(
            assignment,
            envelope,
            heartbeat=FakeHeartbeat(events),
            acknowledge=lambda: events.append("ack"),
        )
        == TASK_EXIT_RETRYABLE
    )
    assert "attempt" not in events and "ack" not in events


def test_intentional_cancellation_confirms_exit_before_terminal_ack(
    tmp_path, monkeypatch, assignment_and_bootstrap
):
    assignment, envelope, bootstrap = assignment_and_bootstrap
    events = []
    client = FakeClient(bootstrap, events, cancel=True)
    executable = cancellation_child_script(tmp_path)
    monkeypatch.setattr(
        "lib.task_host.workload_identity",
        lambda: {"pod_uid": str(__import__("uuid").uuid4()), "namespace": "test"},
    )
    host = TaskHost(
        client=client,
        work_root=tmp_path / "work",
        command_resolver=lambda persona: [sys.executable, str(executable)],
    )
    assert (
        host.run(
            assignment,
            envelope,
            heartbeat=FakeHeartbeat(events),
            acknowledge=lambda: events.append("ack"),
        )
        == TASK_EXIT_FAILED
    )
    assert client.finalize_body["outcome"] == "cancelled"
    assert client.finalize_body["child_exit"]["confirmed"] is True
    assert events.index("finalize:cancelled") < events.index("ack")


def test_uncooperative_cancel_is_force_killed_with_owned_process_group_only(
    tmp_path, monkeypatch, assignment_and_bootstrap
):
    assignment, envelope, bootstrap = assignment_and_bootstrap
    events = []
    client = FakeClient(bootstrap, events, cancel=True)
    executable = cancellation_child_script(tmp_path, ignore_cancel=True)
    monkeypatch.setattr("lib.task_host._TERM_AFTER_SECONDS", 0.05)
    monkeypatch.setattr("lib.task_host._KILL_AFTER_SECONDS", 0.1)
    monkeypatch.setattr(
        "lib.task_host.workload_identity",
        lambda: {"pod_uid": str(__import__("uuid").uuid4()), "namespace": "test"},
    )
    host = TaskHost(
        client=client,
        work_root=tmp_path / "work",
        command_resolver=lambda persona: [sys.executable, str(executable)],
    )
    assert (
        host.run(
            assignment,
            envelope,
            heartbeat=FakeHeartbeat(events),
            acknowledge=lambda: events.append("ack"),
        )
        == TASK_EXIT_FAILED
    )
    assert client.finalize_body["outcome"] == "cancelled"
    assert client.finalize_body["child_exit"]["confirmed"] is True


def test_ack_failure_records_unknown_settlement_and_returns_retryable(
    tmp_path, monkeypatch, assignment_and_bootstrap
):
    assignment, envelope, bootstrap = assignment_and_bootstrap
    events = []
    client = FakeClient(bootstrap, events)
    executable = child_script(tmp_path)
    monkeypatch.setattr(
        "lib.task_host.workload_identity",
        lambda: {"pod_uid": str(__import__("uuid").uuid4()), "namespace": "test"},
    )
    host = TaskHost(
        client=client,
        work_root=tmp_path / "work",
        command_resolver=lambda persona: [sys.executable, str(executable)],
    )

    def unavailable_ack():
        events.append("ack.attempted")
        raise TaskGatewayError("ack unavailable")

    assert (
        host.run(
            assignment,
            envelope,
            heartbeat=FakeHeartbeat(events),
            acknowledge=unavailable_ack,
        )
        == TASK_EXIT_RETRYABLE
    )
    assert events.index("finalize:completed") < events.index("ack.attempted")
    assert client.settlements[-1]["queue_ack_status"] == "unknown"


def test_ambiguous_finalization_receipt_never_attempts_a_conflicting_failure_write(
    tmp_path, monkeypatch, assignment_and_bootstrap
):
    assignment, envelope, bootstrap = assignment_and_bootstrap
    events = []
    client = FakeClient(bootstrap, events)
    executable = child_script(tmp_path)

    def ambiguous_finalize(body):
        events.append("finalize:" + body["outcome"])
        return {
            "schema_version": "1.0",
            "task_id": assignment.task_id,
            "status": body["outcome"],
            "version": 4,
            "terminal_event_id": None,
        }

    client.finalize = ambiguous_finalize
    monkeypatch.setattr(
        "lib.task_host.workload_identity",
        lambda: {"pod_uid": str(__import__("uuid").uuid4()), "namespace": "test"},
    )
    host = TaskHost(
        client=client,
        work_root=tmp_path / "work",
        command_resolver=lambda persona: [sys.executable, str(executable)],
    )
    assert (
        host.run(
            assignment,
            envelope,
            heartbeat=FakeHeartbeat(events),
            acknowledge=lambda: events.append("ack"),
        )
        == TASK_EXIT_RETRYABLE
    )
    assert events.count("finalize:completed") == 1
    assert "finalize:failed" not in events
    assert "ack" not in events
    assert "settlement" in events


def test_network_wrapper_denies_socket_creation():
    wrapper = Path(__file__).resolve().parent.parent / "lib/task_network_exec.py"
    result = subprocess.run(
        [
            sys.executable,
            str(wrapper),
            sys.executable,
            "-c",
            "import socket;\ntry: socket.socket()\nexcept PermissionError: raise SystemExit(0)\nraise SystemExit(1)",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_confirmed_model_result_reaches_child_protocol(assignment_and_bootstrap):
    assignment, _, bootstrap = assignment_and_bootstrap
    turn_id = str(__import__("uuid").uuid4())
    content = [{"type": "text", "text": "Stored result"}]
    client = FakeClient(
        bootstrap,
        [],
        model_response={
            "schema_version": "1.0",
            "task_id": assignment.task_id,
            "turn_id": turn_id,
            "operation_status": "confirmed",
            "content": content,
            "stop_reason": "end_turn",
        },
    )
    from lib.task_host import _canonical_digest

    client.model_response.update(
        {
            "request_digest": _canonical_digest(
                {"messages": [{"role": "user", "content": "évidence"}], "max_tokens": 32}
            ),
            "automatic_replay_permitted": False,
            "handoff": "confirmed",
        }
    )
    host = TaskHost(client=client)
    response = host._model(
        assignment,
        {},
        {
            "turn_id": turn_id,
            "messages": [{"role": "user", "content": "évidence"}],
        },
        32,
    )
    assert response["type"] == "model.result"
    assert response["operation_status"] == "confirmed"
    assert response["content"] == content
    assert response["stop_reason"] == "end_turn"


@pytest.mark.parametrize("status", ["pending", "unknown", "rejected"])
def test_nonconfirmed_model_result_cannot_leak_content(status, assignment_and_bootstrap):
    from lib.task_host import TaskHostError

    assignment, _, bootstrap = assignment_and_bootstrap
    turn_id = str(__import__("uuid").uuid4())
    client = FakeClient(
        bootstrap,
        [],
        model_response={
            "task_id": assignment.task_id,
            "turn_id": turn_id,
            "operation_status": status,
            "content": [{"type": "text", "text": "unconfirmed"}],
        },
    )
    from lib.task_host import _canonical_digest

    client.model_response.update(
        {
            "schema_version": "1.0",
            "request_digest": _canonical_digest(
                {"messages": [{"role": "user", "content": "test"}], "max_tokens": 32}
            ),
            "automatic_replay_permitted": False,
        }
    )
    with pytest.raises(TaskHostError, match="unconfirmed"):
        TaskHost(client=client)._model(
            assignment,
            {},
            {
                "turn_id": turn_id,
                "messages": [{"role": "user", "content": "test"}],
            },
            32,
        )


def test_host_digest_uses_canonical_json_for_unicode_and_numbers():
    import hashlib
    from lib.task_host import _canonical_digest

    expected = '{"a":0,"b":1e-7,"é":"évidence"}'.encode()
    assert (
        _canonical_digest({"é": "évidence", "b": 0.0000001, "a": -0.0})
        == hashlib.sha256(expected).hexdigest()
    )


def test_host_rejects_oversized_frame_before_writing():
    import io
    from types import SimpleNamespace
    from lib.task_host import _write_frame
    from lib.task_protocol import TaskProtocolError

    process = SimpleNamespace(stdin=io.StringIO())
    with pytest.raises(TaskProtocolError, match="byte limit"):
        _write_frame(process, {"content": "x" * 65536})
    assert process.stdin.getvalue() == ""


@pytest.mark.parametrize(
    "corruption",
    [
        {"request_digest": "0" * 64},
        {"handoff": "unknown"},
        {"automatic_replay_permitted": True},
        {"schema_version": "wrong"},
    ],
)
def test_model_receipt_must_match_request_and_confirmed_handoff(
    corruption, assignment_and_bootstrap
):
    from lib.task_host import TaskHostError, _canonical_digest

    assignment, _, bootstrap = assignment_and_bootstrap
    turn_id = str(__import__("uuid").uuid4())
    request = {"messages": [{"role": "user", "content": "test"}], "max_tokens": 32}
    receipt = {
        "schema_version": "1.0",
        "task_id": assignment.task_id,
        "turn_id": turn_id,
        "operation_status": "confirmed",
        "handoff": "confirmed",
        "automatic_replay_permitted": False,
        "request_digest": _canonical_digest(request),
        "content": [{"type": "text", "text": "stored"}],
        "stop_reason": "end_turn",
        **corruption,
    }
    host = TaskHost(client=FakeClient(bootstrap, [], model_response=receipt))
    with pytest.raises(TaskHostError, match="receipt"):
        host._model(assignment, {}, {"turn_id": turn_id, **request}, 32)


def test_host_commits_model_turn_before_provider_call(assignment_and_bootstrap):
    assignment, _, bootstrap = assignment_and_bootstrap
    client = FakeClient(bootstrap, [])
    observed = []

    def turn(body):
        observed.append(("turn", body["request_id"]))
        return FakeClient.turn(client, body)

    def model(body):
        observed.append(("model", body["turn_id"]))
        return {
            "schema_version": "1.0",
            "task_id": assignment.task_id,
            "turn_id": body["turn_id"],
            "request_digest": body["request_digest"],
            "automatic_replay_permitted": False,
            "operation_status": "confirmed",
            "handoff": "confirmed",
            "content": [{"type": "text", "text": "stored"}],
            "stop_reason": "end_turn",
        }

    client.turn, client.model = turn, model
    frame = {
        "turn_id": str(__import__("uuid").uuid4()),
        "messages": [{"role": "user", "content": "test"}],
    }
    host = TaskHost(client=client)
    host._model(assignment, {}, frame, 32)
    assert observed == [("turn", frame["turn_id"]), ("model", frame["turn_id"])]


def test_host_delivers_only_committed_command_text_and_retains_turn_id(assignment_and_bootstrap):
    import io
    from types import SimpleNamespace
    from lib.task_protocol import TaskProtocolError

    assignment, _, bootstrap = assignment_and_bootstrap
    client = FakeClient(bootstrap, [])
    host = TaskHost(client=client)
    host._turn_number = 1
    command_id = str(__import__("uuid").uuid4())

    def turn(body):
        return {
            "schema_version": "1.0",
            "operation_status": "committed",
            "pending_input_count": 0,
            "turn": {
                "task_id": assignment.task_id,
                "turn_id": body["request_id"],
                "turn_number": 2,
                "transcript_version": 3,
                "command_ids": [command_id],
            },
            "messages": [{"command_id": command_id, "text": "Include the timestamp."}],
        }

    client.turn = turn
    process = SimpleNamespace(stdin=io.StringIO())
    host._deliver_input(
        assignment, {}, process, {"pending_input_count": 1, "cancel_requested": False}
    )
    frame = json.loads(process.stdin.getvalue())
    assert frame["type"] == "turn"
    assert frame["messages"] == [{"command_id": command_id, "text": "Include the timestamp."}]
    assert host._pending_turn_id == frame["turn_id"]
    before = process.stdin.getvalue()
    host._deliver_input(
        assignment, {}, process, {"pending_input_count": 1, "cancel_requested": False}
    )
    assert process.stdin.getvalue() == before
    with pytest.raises(TaskProtocolError, match="skipped"):
        host._model(
            assignment, {}, {"turn_id": str(__import__("uuid").uuid4()), "messages": []}, 32
        )


def test_host_rejects_turn_text_not_in_committed_membership(assignment_and_bootstrap):
    from lib.task_protocol import TaskProtocolError

    assignment, _, bootstrap = assignment_and_bootstrap
    client = FakeClient(bootstrap, [])

    def turn(body):
        result = FakeClient.turn(client, body)
        result["messages"] = [
            {"command_id": str(__import__("uuid").uuid4()), "text": "uncommitted"}
        ]
        return result

    client.turn = turn
    with pytest.raises(TaskProtocolError, match="membership"):
        TaskHost(client=client)._turn(assignment, {}, str(__import__("uuid").uuid4()))


def test_verified_artifact_chunks_roundtrip_at_per_artifact_limit(assignment_and_bootstrap):
    from lib.task_protocol import TaskProtocolError
    import base64
    import hashlib
    import io
    from types import SimpleNamespace

    assignment, _, bootstrap = assignment_and_bootstrap
    content = ("é" * 131072).encode()
    reference = {
        "artifact_id": "art_11111111-1111-4111-8111-111111111111",
        "version": 1,
        "content_type": "text/plain",
        "content_sha256": hashlib.sha256(content).hexdigest(),
    }
    bootstrap["input"]["artifacts"] = [reference]
    client = FakeClient(bootstrap, [])

    def read(body):
        return {
            **body,
            "content_type": reference["content_type"],
            "content_sha256": reference["content_sha256"],
            "byte_length": len(content),
            "content_base64": base64.b64encode(content).decode(),
        }

    client.artifact = read
    host = TaskHost(client=client)
    artifacts = host._input_artifacts(assignment, bootstrap)
    process = SimpleNamespace(stdin=io.StringIO())
    host._send_artifacts(process, assignment, artifacts)
    lines = process.stdin.getvalue().splitlines(keepends=True)
    frames = [json.loads(line) for line in lines]
    assert len(frames) == 8
    assert all(len(line.encode()) <= 65536 for line in lines)
    assert [frame["sequence"] for frame in frames] == list(range(1, 9))
    assert [frame["last"] for frame in frames] == [False] * 7 + [True]
    assert b"".join(base64.b64decode(frame["data_base64"]) for frame in frames) == content
    client.artifact = lambda body: {**read(body), "content_sha256": "0" * 64}
    with pytest.raises(TaskProtocolError, match="mismatch"):
        host._input_artifacts(assignment, bootstrap)

@pytest.mark.parametrize("report_outage", [False, True])
def test_cancel_during_model_precedes_response_after_normal_or_deferred_delivery(
    tmp_path, monkeypatch, assignment_and_bootstrap, report_outage
):
    assignment, envelope, bootstrap = assignment_and_bootstrap
    events = []
    client = FakeClient(bootstrap, events)
    original_report = client.report
    report_ids = []
    fail_once = report_outage

    def report(body):
        nonlocal fail_once
        report_ids.append(body["report_id"])
        if fail_once:
            fail_once = False
            from lib.task_run_client import TaskRunClientError
            raise TaskRunClientError("transient outage")
        return original_report(body)

    def model(body):
        client.cancel = True
        return {"schema_version": "1.0", "task_id": assignment.task_id,
                "turn_id": body["turn_id"], "operation_status": "pending",
                "request_digest": body["request_digest"], "automatic_replay_permitted": False}

    client.report, client.model = report, model
    executable = burst_child_script(tmp_path, progress_count=2, include_model=True)
    source = executable.read_text().replace(
        "while json.loads(sys.stdin.readline())['type'] != 'model.result': pass",
        """seen = []
while True:
    frame = json.loads(sys.stdin.readline()); seen.append(frame['type'])
    if frame['type'] == 'cancel': break
assert 'model.result' not in seen, seen
""",
    )
    executable.write_text(source)
    monkeypatch.setattr("lib.task_host._REPORT_RETRY_SECONDS", 0.01)
    monkeypatch.setattr("lib.task_host.workload_identity", lambda: {
        "pod_uid": str(__import__("uuid").uuid4()), "namespace": "test"})
    host = TaskHost(client=client, work_root=tmp_path / "work",
                    command_resolver=lambda persona: [sys.executable, str(executable)])
    assert host.run(assignment, envelope, heartbeat=FakeHeartbeat(events),
                    acknowledge=lambda: events.append("ack")) == TASK_EXIT_FAILED
    assert client.finalize_body["outcome"] == "cancelled"
    assert client.finalize_body["child_exit"]["exit_code"] == 0
    if report_outage:
        assert report_ids[0] == report_ids[1]


@pytest.mark.parametrize("cancel", [False, True])
def test_delayed_http_model_keeps_control_live_and_polls_exact_request(
    tmp_path, monkeypatch, assignment_and_bootstrap, cancel
):
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    import requests
    from lib.task_run_client import TaskRunClientUnavailable

    assignment, envelope, bootstrap = assignment_and_bootstrap
    events, bodies = [], []
    state = {"started": None, "provider_calls": 0, "control_while_pending": 0}

    class Gateway(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            bodies.append(body)
            if state["started"] is None:
                state["started"] = time.monotonic()
                state["provider_calls"] += 1
                time.sleep(0.35)  # First HTTP response exceeds the worker read timeout.
            confirmed = time.monotonic() - state["started"] >= 0.3
            receipt = {"schema_version": "1.0", "task_id": assignment.task_id, "turn_id": body["turn_id"],
                "request_digest": body["request_digest"], "automatic_replay_permitted": False,
                "operation_status": "confirmed" if confirmed else "pending", "handoff": "confirmed" if confirmed else "prepared"}
            if confirmed:
                receipt.update(content=[{"type": "text", "text": "Actual provider result"}], stop_reason="end_turn")
            encoded = json.dumps(receipt).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = FakeClient(bootstrap, events)
    original_control = client.control

    def control(body):
        if state["started"] is not None:
            state["control_while_pending"] += 1
            client.cancel = cancel
        return original_control(body)

    def model(body):
        try:
            return requests.post(f"http://127.0.0.1:{server.server_port}/model", json=body, timeout=0.06).json()
        except requests.RequestException:
            raise TaskRunClientUnavailable("HTTP outcome unknown") from None

    client.control, client.model = control, model
    executable = child_script(tmp_path, model=True)
    script = executable.read_text().replace("send(model); sys.stdin.readline()", """send(model)
answer = json.loads(sys.stdin.readline())
if answer['type'] == 'cancel':
    stopped = base('cancelled'); stopped['command_id'] = answer['command_id']; send(stopped); sys.exit(0)
assert answer['type'] == 'model.result' and answer['operation_status'] == 'confirmed'
assert answer['content'][0]['text'] == 'Actual provider result'
""")
    executable.write_text(script)
    monkeypatch.setattr("lib.task_host._CONTROL_POLL_SECONDS", 0.02)
    monkeypatch.setattr("lib.task_host._MODEL_POLL_SECONDS", 0.02)
    monkeypatch.setattr("lib.task_host.workload_identity", lambda: {
        "pod_uid": str(__import__("uuid").uuid4()), "namespace": "test"})
    host = TaskHost(client=client, work_root=tmp_path / "work", command_resolver=lambda persona: [sys.executable, str(executable)])
    try:
        result = host.run(assignment, envelope, heartbeat=FakeHeartbeat(events), acknowledge=lambda: events.append("ack"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)
    assert result == (TASK_EXIT_FAILED if cancel else 0)
    assert client.finalize_body["outcome"] == ("cancelled" if cancel else "completed")
    assert state["provider_calls"] == 1
    assert state["control_while_pending"] >= 1
    assert all(body == bodies[0] for body in bodies)
    if not cancel:
        assert len(bodies) >= 2  # Lost first response was followed by durable receipt lookups.
        assert state["control_while_pending"] >= 2
    assert events.index("finalize:" + client.finalize_body["outcome"]) < events.index("ack")


@pytest.mark.parametrize("status", ["pending", "unknown", "rejected"])
def test_unconfirmed_attempt_never_starts_child(tmp_path, monkeypatch, assignment_and_bootstrap, status):
    assignment, envelope, bootstrap = assignment_and_bootstrap
    events = []
    client = FakeClient(bootstrap, events)
    client.attempt = lambda body: {"schema_version": "1.0", "operation_status": status, "request_id": body["runtime_attempt_id"]}
    spawned = []
    host = TaskHost(client=client, work_root=tmp_path / "work", command_resolver=lambda persona: spawned.append(persona))
    monkeypatch.setattr("lib.task_host.workload_identity", lambda: {
        "pod_uid": str(__import__("uuid").uuid4()), "namespace": "test"})
    assert host.run(assignment, envelope, heartbeat=FakeHeartbeat(events), acknowledge=lambda: events.append("ack")) != 0
    assert not spawned and "ack" not in events


def test_pending_model_receipt_wait_is_bounded_and_never_becomes_fake_success(tmp_path, monkeypatch, assignment_and_bootstrap):
    assignment, envelope, bootstrap = assignment_and_bootstrap
    events, bodies = [], []
    client = FakeClient(bootstrap, events)
    def pending(body):
        bodies.append(copy.deepcopy(body))
        return {"schema_version": "1.0", "task_id": assignment.task_id, "turn_id": body["turn_id"],
            "request_digest": body["request_digest"], "automatic_replay_permitted": False,
            "operation_status": "pending", "handoff": "prepared"}
    client.model = pending
    executable = child_script(tmp_path, model=True)
    monkeypatch.setattr("lib.task_host._MODEL_RECEIPT_SECONDS", 0.25)
    monkeypatch.setattr("lib.task_host._MODEL_POLL_SECONDS", 0.01)
    monkeypatch.setattr("lib.task_host.workload_identity", lambda: {
        "pod_uid": str(__import__("uuid").uuid4()), "namespace": "test"})
    host = TaskHost(client=client, work_root=tmp_path / "work", command_resolver=lambda persona: [sys.executable, str(executable)])
    assert host.run(assignment, envelope, heartbeat=FakeHeartbeat(events), acknowledge=lambda: events.append("ack")) == TASK_EXIT_FAILED
    assert client.finalize_body["error"]["code"] == "model_outcome_unknown"
    assert client.finalize_body["error"]["total_usd"] is None
    assert len(bodies) >= 2 and all(body == bodies[0] for body in bodies)
