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
from lib.task_host import TASK_EXIT_FAILED, TASK_EXIT_RETRYABLE, TaskHost

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
        self.finalize_body = None
        self.settlements = []

    def bootstrap(self, body):
        self.events.append("bootstrap")
        return copy.deepcopy(self.bootstrap_response)

    def attempt(self, body):
        self.events.append("attempt")
        return {"operation_status": "confirmed"}

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
        client.model_response["turn_id"] = body["turn_id"]
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
