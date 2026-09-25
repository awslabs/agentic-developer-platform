"""Real investigator frames -> installed host adapter -> real gateway HTTP route.

Only workload authentication and provider execution are controlled; the child,
Python adapter, HTTP body schema and route size/digest input are actual source.
Build the investigator package before running this seam (no skip on absent dist).
"""

import base64
import hashlib
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import time
from types import SimpleNamespace
import uuid

import pytest
import rfc8785
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "modules/agent-factory/agent-worker-image"))
sys.path.insert(0, str(ROOT / "modules/gateway"))
os.environ.setdefault("BG_TOKEN_SECRET_KEY", "seam-fixture-only-not-a-live-secret")
from lib.task_host import TaskHost  # noqa: E402
from lib.task_protocol import validate_child_frame, TaskProtocolError  # noqa: E402
from src.agentauth import task_runtime_routes as routes  # noqa: E402
from src.agentauth import task_model  # noqa: E402


def child_request(count, alphabet):
    package = ROOT / "modules/agent-factory/task-agents/investigator"
    assert (package / "dist/index.js").is_file(), (
        "Build the actual investigator before this seam"
    )
    start = json.loads(
        (
            ROOT / "docs/task-api/contracts/v1/fixtures/valid/process-start-frame.json"
        ).read_text()
    )
    start.pop("$fixture")
    start["artifacts"] = []
    child = subprocess.Popen(
        ["node", str(package / "dist/index.js"), "--embedded"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        env={
            "PATH": os.environ["PATH"],
            "NODE_OPTIONS": "--require=" + str(package / "test/network-deny-hook.cjs"),
        },
    )

    def send(frame):
        child.stdin.write((json.dumps(frame, ensure_ascii=False) + "\n").encode())
        child.stdin.flush()

    try:
        for _ in range(count):
            prefix = b"line1\nline2\n"
            unit = alphabet.encode()
            content = prefix + unit * ((262144 - len(prefix)) // len(unit))
            content += b"x" * (262144 - len(content))
            artifact_id = "art_" + str(uuid.uuid4())
            digest = hashlib.sha256(content).hexdigest()
            start["artifacts"].append(
                {
                    "artifact_id": artifact_id,
                    "content_type": "text/plain",
                    "content_sha256": digest,
                    "byte_length": len(content),
                }
            )
            for offset in range(0, len(content), 32768):
                send(
                    {
                        "protocol_version": 1,
                        "type": "artifact.chunk",
                        "request_id": str(uuid.uuid4()),
                        "task_id": start["task_id"],
                        "artifact_id": artifact_id,
                        "content_type": "text/plain",
                        "content_sha256": digest,
                        "sequence": offset // 32768 + 1,
                        "total_bytes": len(content),
                        "data_base64": base64.b64encode(
                            content[offset : offset + 32768]
                        ).decode(),
                        "last": offset + 32768 >= len(content),
                    }
                )
        send(start)
        deadline = time.monotonic() + 10
        selector = selectors.DefaultSelector()
        selector.register(child.stdout, selectors.EVENT_READ)
        while time.monotonic() < deadline:
            assert selector.select(timeout=max(0, deadline - time.monotonic())), (
                "Child failed to emit a model request"
            )
            line = child.stdout.readline()
            assert line, child.stderr.read().decode()
            assert len(line) <= 65536
            frame = json.loads(line)
            validate_child_frame(frame, start["task_id"])
            if frame["type"] == "model.request":
                return frame
            if frame["type"] == "progress":
                send(
                    {
                        "protocol_version": 1,
                        "type": "report.ack",
                        "request_id": str(uuid.uuid4()),
                        "task_id": start["task_id"],
                        "report_id": frame["report_id"],
                        "sequence": 1,
                    }
                )
        raise AssertionError("No model request before deadline")
    finally:
        child.kill()
        child.communicate(timeout=5)


class Turns:
    def __init__(self):
        self.calls = 0

    def turn(self, body):
        self.calls += 1
        n = body["expected_transcript_version"]
        return {
            "schema_version": "1.0",
            "operation_status": "committed",
            "pending_input_count": 0,
            "turn": {
                "task_id": body["attempt"]["run"]["task_id"],
                "turn_id": body["request_id"],
                "turn_number": n,
                "transcript_version": n + 1,
                "command_ids": [],
            },
            "messages": [],
        }


@pytest.mark.parametrize(
    ("count", "alphabet"), [(0, "a"), (1, "a"), (4, '"'), (4, "€")]
)
def test_actual_child_request_passes_actual_gateway_model_route(
    monkeypatch, count, alphabet
):
    frame = child_request(count, alphabet)
    identity = SimpleNamespace(
        task_id=frame["task_id"],
        invocation_id=str(uuid.uuid4()),
        generation=1,
        runtime_attempt_id=str(uuid.uuid4()),
    )
    attempt = {
        "run": {
            "task_id": identity.task_id,
            "invocation_id": identity.invocation_id,
            "generation": 1,
        },
        "runtime_attempt_id": identity.runtime_attempt_id,
    }
    turns = Turns()
    prepared = TaskHost(client=turns)._model_request(identity, attempt, frame, 4096)
    invocation = {
        k: prepared[k] for k in ("messages", "max_tokens", "system") if k in prepared
    }
    assert (
        prepared["request_digest"]
        == hashlib.sha256(rfc8785.dumps(invocation)).hexdigest()
    )
    for original, wrapped in zip(frame["messages"], prepared["messages"], strict=True):
        assert (
            "".join(block["text"] for block in wrapped["content"])
            == original["content"]
        )
        assert all(
            block["type"] == "text" and 0 < len(block["text"]) <= 32000
            for block in wrapped["content"]
        )
        assert len(wrapped["content"]) <= 16
    if count:
        assert "bytes were omitted" in frame["messages"][0]["content"]
    if count == 1:
        assert len(prepared["messages"][0]["content"]) == 2
    wire = json.dumps(
        prepared, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    assert len(wire) <= 65536
    assert len(json.dumps(invocation, ensure_ascii=False).encode()) <= 65536
    observed = []

    class Provider:
        def __init__(self, *args, **kwargs):
            pass

        async def execute(self, **kwargs):
            observed.append(kwargs)
            return {"operation_status": "confirmed"}

    async def authenticate(request):
        return identity

    monkeypatch.setattr(routes, "authenticate_task_attempt", authenticate)
    monkeypatch.setattr(
        routes, "task_runtime", lambda _: SimpleNamespace(repository=object())
    )
    monkeypatch.setattr(task_model, "TaskModel", Provider)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.require_agent_transport] = lambda: None
    app.dependency_overrides[routes.get_agent_runtime] = lambda: None
    app.dependency_overrides[routes.get_db] = lambda: None
    with TestClient(app) as client:
        response = client.post(
            "/internal/v1/agent/task/model",
            content=wire,
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 200, response.text
        # Original child representation is valid process input but invalid HTTP model input.
        invalid = {**prepared, "messages": frame["messages"]}
        assert (
            client.post("/internal/v1/agent/task/model", json=invalid).status_code
            == 422
        )
    assert len(observed) == 1 and observed[0]["request"] == invocation
    assert turns.calls == 1


def test_adapter_refuses_oversize_before_committing_a_turn():
    turns = Turns()
    with pytest.raises(TaskProtocolError, match="65536"):
        TaskHost(client=turns)._model_request(
            SimpleNamespace(),
            {},
            {
                "turn_id": str(uuid.uuid4()),
                "messages": [{"role": "user", "content": "x" * 65500}],
            },
            4096,
        )
    assert turns.calls == 0


def test_actual_child_final_progress_can_exit_before_durable_report_ack(tmp_path, monkeypatch):
    import importlib.util
    import shutil

    helper_path = ROOT / "modules/agent-factory/agent-worker-image/tests/test_task_host.py"
    spec = importlib.util.spec_from_file_location("host_fixture_helpers", helper_path)
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    assignment, envelope, bootstrap = helpers.assignment_and_bootstrap.__wrapped__()
    events = []
    report = {
        "summary": "The affected service is checkout-api.",
        "findings": [{"statement": "Checkout-api is the affected service.", "evidence_refs": ["affected_service"], "confidence": "high"}],
        "uncertainties": ["No upstream logs were supplied."],
        "recommendations": ["Collect upstream logs."],
        "evidence_refs": [{"ref": "affected_service", "source": "inputs"}],
    }

    class SlowReportClient(helpers.FakeClient):
        def report(self, body):
            if body["data"].get("stage") == "analysis":
                # Real report round trip lets the real child write synthesis,
                # result and close stdin before this durable receipt arrives.
                time.sleep(0.15)
            return super().report(body)

        def model(self, body):
            return {
                "schema_version": "1.0", "task_id": assignment.task_id,
                "turn_id": body["turn_id"], "request_digest": body["request_digest"],
                "automatic_replay_permitted": False, "operation_status": "confirmed",
                "handoff": "confirmed", "content": [{"type": "text", "text": json.dumps(report)}],
                "stop_reason": "end_turn",
            }

    entry = ROOT / "modules/agent-factory/task-agents/investigator/dist/index.js"
    assert entry.is_file(), "Build actual investigator before running seam"
    monkeypatch.setattr("lib.task_host.workload_identity", lambda: {"pod_uid": "fixture", "namespace": "test"})
    # CI setup-python requires LD_LIBRARY_PATH, intentionally absent from the
    # sandbox environment. Use the system interpreter for the same real network
    # wrapper, and resolve setup-node before the host replaces PATH.
    wrapper = ROOT / "modules/agent-factory/agent-worker-image/lib/task_network_exec.py"
    monkeypatch.setattr("lib.task_host._network_wrapped_command", lambda command: ["/usr/bin/python3", str(wrapper), *command])
    node = shutil.which("node")
    assert node
    client = SlowReportClient(bootstrap, events)
    host = TaskHost(client=client, work_root=tmp_path / "work", command_resolver=lambda _: [node, str(entry), "--embedded"])
    assert host.run(assignment, envelope, heartbeat=helpers.FakeHeartbeat(events), acknowledge=lambda: events.append("ack")) == 0, client.finalize_body
    assert client.finalize_body["outcome"] == "completed"
    assert client.finalize_body["result"]["artifact_ids"] == client.finalize_body["committed_result_refs"]
    assert len(client.finalize_body["committed_result_refs"]) == 1
    assert events.index("artifact") < events.index("finalize:completed") < events.index("ack")
    assert sum(event.startswith("report:") for event in events) == 3
