"""Real worker -> embedded Codex SDK -> fixture gateway lifecycle (no inference)."""
import hashlib
import json
import os
import shutil
import sys
import uuid
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "modules/agent-factory/agent-worker-image"))

from tests import test_task_host  # noqa: E402
from tests.test_task_host import FakeClient, FakeHeartbeat  # noqa: E402
from lib.task_host import TaskHost  # noqa: E402

assignment_and_bootstrap = test_task_host.assignment_and_bootstrap
ENTRY = ROOT / "modules/agent-factory/codex-harness/dist/task-entry.mjs"


def harness(deadline):
    limits = {"maxTurns": 4, "maxContextBytes": 60000, "maxDurationMs": 30000}
    persona = {"schemaVersion": 1, "key": "gpt-fixture", "revision": "1", "displayName": "Fixture",
               "instructions": "Use supplied evidence and report uncertainty.", "skills": [],
               "requiredCapabilities": ["artifacts.publish"], "optionalCapabilities": [],
               "surfaces": ["task-api"], "completionPolicy": "report", "effort": "medium", "limits": limits}
    definition = json.dumps(persona, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(definition.encode()).hexdigest()
    return {"snapshot": {"definition": definition, "digest": digest, "instructions": persona["instructions"], "skillSources": "[]"},
            "policy": {"personaKey": persona["key"], "personaDigest": digest, "compatibilityClass": "codex-sdk",
                       "harnessRevision": "codex-sdk-0.155.1/adp-v1", "canonicalModel": "gpt-5-codex", "allowedEfforts": ["medium"],
                       "capabilityLayers": {key: ["artifacts.publish"] for key in ["tenant", "principal", "run", "surface", "runtime"]},
                       "limits": limits, "deadlineMs": int(datetime.fromisoformat(deadline.replace("Z", "+00:00")).timestamp() * 1000)}}


def report():
    return {"summary": "Supplied task examined; incident logs are absent.",
            "findings": [{"statement": "The task requests incident analysis.", "evidence_refs": ["instructions"]}],
            "uncertainties": ["No log evidence was supplied to establish the cause."], "recommendations": ["Supply incident logs."],
            "evidence_refs": [{"ref": "instructions", "source": "instructions"}]}


class CodexClient(FakeClient):
    def __init__(self, bootstrap, events, mode):
        super().__init__(bootstrap, events)
        self.mode = mode
        self.requests = []
        self.amended = False
        self.command_id = str(uuid.uuid4())

    def control(self, body):
        result = super().control(body)
        if self.mode == "steer" and self.requests and not self.amended:
            result["pending_input_count"] = 1
        return result

    def turn(self, body):
        result = super().turn(body)
        if self.mode == "steer" and self.requests and not self.amended:
            self.amended = True
            result["turn"]["command_ids"] = [self.command_id]
            result["messages"] = [{"command_id": self.command_id, "text": "Also inspect the retry configuration."}]
        return result

    def model(self, body):
        self.events.append("model")
        self.requests.append(body)
        text = json.dumps(report())
        if self.mode == "repair" and len(self.requests) == 1:
            text = "invalid report"
        if self.mode == "invalid":
            text = "invalid report"
        if self.mode == "cancel":
            self.cancel = True
        if self.mode == "unknown":
            return {"schema_version": "1.0", "task_id": self.bootstrap_response["task_id"],
                    "turn_id": body["turn_id"], "request_digest": body["request_digest"],
                    "automatic_replay_permitted": False, "operation_status": "unknown", "handoff": "unknown"}
        return {"schema_version": "1.0", "task_id": self.bootstrap_response["task_id"],
                "turn_id": body["turn_id"], "request_digest": body["request_digest"],
                "automatic_replay_permitted": False, "operation_status": "confirmed", "handoff": "confirmed",
                "content": [], "stop_reason": "completed", "responses_response": {
                    "id": "resp_fixture", "status": "completed", "output": [
                        {"id": "msg_fixture", "type": "message", "role": "assistant", "status": "completed",
                         "phase": "final_answer", "content": [{"type": "output_text", "text": text, "annotations": []}]}],
                    "usage": {"input_tokens": 100, "output_tokens": 50}}}


@pytest.mark.parametrize("mode,expected_calls,outcome", [("success", 1, "completed"), ("repair", 2, "completed"),
                                                         ("invalid", 2, "failed"), ("cancel", 1, "cancelled"),
                                                         ("steer", 2, "completed"), ("unknown", 1, "failed"),
                                                         ("tampered", 0, "failed")])
def test_real_codex_task_lifecycle(tmp_path, monkeypatch, assignment_and_bootstrap, mode, expected_calls, outcome):
    node = os.environ.get("ADP_CODEX_TEST_NODE") or shutil.which("node")
    assert node, "Node >=24 and built Codex/investigator packages are required"
    assignment, envelope, bootstrap = assignment_and_bootstrap
    persona = "agent-task-gpt-fixture"
    assignment = replace(assignment, persona=persona)
    envelope["persona"] = bootstrap["persona"] = persona
    bootstrap["model_binding"].update(model_id="gpt-5-codex", transport="openai_responses")
    bootstrap["harness"] = harness(bootstrap["deadline_at"])
    if mode == "tampered":
        bootstrap["harness"]["snapshot"]["instructions"] = "Unpinned instructions"
    events = []
    client = CodexClient(bootstrap, events, mode)
    monkeypatch.setattr("lib.task_host.workload_identity", lambda: {"pod_uid": str(uuid.uuid4()), "namespace": "test"})
    # Match the worker image layout outside the source tree. Dependency linkage
    # reuses the installed SDK; all application protocol imports must be packaged.
    packaged = tmp_path / "app/codex-harness"
    shutil.copytree(ENTRY.parent, packaged / "dist")
    shutil.copyfile(ENTRY.parent.parent / "package.json", packaged / "package.json")
    (packaged / "node_modules").symlink_to(ENTRY.parent.parent / "node_modules", target_is_directory=True)
    host = TaskHost(client=client, work_root=tmp_path / "work", command_resolver=lambda _: [node, str(packaged / "dist/task-entry.mjs"), "--embedded"])
    result = host.run(assignment, envelope, heartbeat=FakeHeartbeat(events), acknowledge=lambda: events.append("ack"))
    assert client.finalize_body is not None, (result, events)
    assert client.finalize_body["outcome"] == outcome, (result, events, client.finalize_body)
    assert len(client.requests) == expected_calls
    if outcome == "completed":
        assert result == 0
        assert client.finalize_body["result"]["process_exit_validated"] is True
        assert events.index("artifact") < events.index("finalize:completed") < events.index("ack")
    assert not list((tmp_path / "work").iterdir()), "Task workspace survived cleanup"
    if mode == "repair":
        assert "Previous output failed" in json.dumps(client.requests[1])
        assert client.requests[0]["turn_id"] != client.requests[1]["turn_id"]

    if mode == "steer":
        assert "Also inspect the retry configuration." in json.dumps(client.requests[1])
        assert "follow_up_input." + client.command_id in json.dumps(client.requests[1])
