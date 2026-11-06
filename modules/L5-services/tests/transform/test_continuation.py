"""Bounded original-session continuation with local synthetic children only."""

from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

from l5_services.adapters.transform.continuation import continuation_request
from l5_services.adapters.transform.ndjson import TransformConversation, TransformProtocolError
from l5_services.adapters.transform import provider_runner
from l5_services.adapters.transform.provider_trace import verify_provider_trace


def frame(**value):
    return (json.dumps(value) + "\n").encode()


@pytest.fixture
def selection(tmp_path):
    workspace, home = tmp_path / "source", tmp_path / "home"
    workspace.mkdir()
    path = home / ".aws/atx/custom/original-native/metadata.json"
    path.parent.mkdir(parents=True)
    original = {"type": "start_session", "mode": "exec", "params": {
        "trustAllTools": False, "transformationName": "selected-definition",
        "transformationVersion": "pinned", "agentMinutesLimit": "1"}}
    metadata = {"conversationId": "original-native", "latestCheckpointId": "original-native#checkpoint-a",
        "initialCommitId": "a" * 40, "codeRepositoryPath": str(workspace), "workingDirectory": str(workspace),
        "region": "us-east-1", "expectedResponse": "chatMessage", "expectedToolUseIds": [],
        "interactiveMode": True, "trustAllTools": False, "accumulatedAgentMinutes": 0.3, "agentMinutesLimit": 1}
    path.write_text(json.dumps(metadata))
    intent = {"attempt_token": "b" * 64, "input_hash": "c" * 64,
        "read_paths": ["src"], "write_paths": ["src/app.py"], "excluded_paths": [".git"]}
    conversation = TransformConversation(intent, workspace=workspace, agent_minutes=1)
    conversation.feed(frame(type="session_started", conversationId="original-native"))
    conversation.feed(frame(type="agent_minutes_update", used=0.2))
    conversation.feed(frame(type="checkpoint", checkpointId=metadata["latestCheckpointId"]))
    return original, metadata, path, conversation, {"home": home, "workspace": workspace,
        "baseline_commit": "a" * 40, "region": "us-east-1"}


def test_continue_uses_original_vendor_metadata_without_renewing_allowance(selection):
    original, metadata, path, conversation, args = selection
    before = path.read_bytes()
    request = continuation_request(original, conversation, **args)
    assert request["conversation_id"] == "original-native"
    assert request["params"]["resumeCheckpointId"] == metadata["latestCheckpointId"]
    assert request["params"]["agentMinutesLimit"] == "1"
    assert request["params"]["configuration"] == {}
    assert request["params"]["trustAllTools"] is False
    assert request["params"]["enableResumeFromAnyCheckpoint"] is False
    assert path.read_bytes() == before and "conversation_id" not in original
    assert conversation.minutes_used == 0.3
    conversation.feed(frame(type="resume_status", conversationId="original-native", checkpointId=metadata["latestCheckpointId"]))
    conversation.feed(frame(type="agent_minutes_update", used=0.4))
    conversation.feed(frame(type="complete", status="completed"))
    assert conversation.readback()["native_id"] == "original-native"
    assert len(conversation.capability_trace()["continuations"]) == 1
    observed = verify_provider_trace(conversation.capability_trace(), native_id="original-native", agent_minutes=1)
    assert observed["resume_observed"] is True
    assert observed["missing_tool_controls"]
    assert observed["remote_cancel"] == "unsupported"


def test_vendor_short_checkpoint_before_initial_session_is_retained_and_normalized(selection):
    original, metadata, path, conversation, args = selection
    conversation = TransformConversation(conversation.intent, workspace=args["workspace"], agent_minutes=1)
    conversation.feed(frame(type="checkpoint", checkpointId="checkpoint-a"))
    conversation.feed(frame(type="session_started", conversationId="original-native"))
    conversation.feed(frame(type="agent_minutes_update", used=0.2))
    metadata["latestCheckpointId"] = "checkpoint-a"
    path.write_text(json.dumps(metadata))
    request = continuation_request(original, conversation, **args)
    assert request["params"]["resumeCheckpointId"] == "original-native#checkpoint-a"
    conversation.feed(frame(type="resume_status", conversationId="original-native", checkpointId="original-native#checkpoint-a"))
    conversation.feed(frame(type="agent_minutes_update", used=0.4))
    conversation.feed(frame(type="complete", status="completed"))
    observed = verify_provider_trace(conversation.capability_trace(), native_id="original-native", agent_minutes=1)
    assert observed["resume_observed"]


@pytest.mark.parametrize("checkpoint", ["foreign#checkpoint-a", "original-native#", "original-native#a#b"])
@pytest.mark.parametrize("before_start", [False, True])
def test_checkpoint_must_belong_to_original_native_session(selection, checkpoint, before_start):
    _, _, _, original, args = selection
    conversation = TransformConversation(original.intent, workspace=args["workspace"], agent_minutes=1)
    if before_start:
        conversation.feed(frame(type="checkpoint", checkpointId=checkpoint))
        denied = frame(type="session_started", conversationId="original-native")
    else:
        conversation.feed(frame(type="session_started", conversationId="original-native"))
        denied = frame(type="checkpoint", checkpointId=checkpoint)
    with pytest.raises(TransformProtocolError):
        conversation.feed(denied)
    assert not conversation.continuations


def test_retained_foreign_checkpoint_cannot_request_continuation(selection):
    original, metadata, path, conversation, args = selection
    conversation.checkpoint_id = metadata["latestCheckpointId"] = "foreign#checkpoint-a"
    path.write_text(json.dumps(metadata))
    with pytest.raises(TransformProtocolError):
        continuation_request(original, conversation, **args)
    assert not conversation.continuations


def test_repeated_start_frame_cannot_replace_continuation(selection):
    _, _, _, conversation, _ = selection
    with pytest.raises(TransformProtocolError):
        conversation.feed(frame(type="session_started", conversationId="original-native"))


@pytest.mark.parametrize("field,value", [
    ("conversationId", "different"), ("latestCheckpointId", "different"), ("initialCommitId", "d" * 40),
    ("codeRepositoryPath", "/different"), ("workingDirectory", "/different"), ("region", "us-west-2"),
    ("conversationEndDate", "ended"), ("expectedResponse", "unknown"), ("expectedToolUseIds", ["x"] * 129),
    ("interactiveMode", False), ("trustAllTools", True), ("enableResumeFromAnyCheckpoint", True),
    ("campaignName", "different"), ("repoName", "different"), ("lsp", {"enabled": True}),
    ("accumulatedAgentMinutes", 0.1), ("accumulatedAgentMinutes", 1), ("accumulatedAgentMinutes", True),
    ("accumulatedAgentMinutes", float("nan")), ("agentMinutesLimit", 2), ("agentMinutesLimit", True),
])
def test_changed_metadata_never_starts_another_session(selection, field, value):
    original, metadata, path, conversation, args = selection
    metadata[field] = value
    path.write_text(json.dumps(metadata))
    with pytest.raises(TransformProtocolError):
        continuation_request(original, conversation, **args)
    assert not conversation.continuations


@pytest.mark.parametrize("fault", ["absent", "linked", "oversize", "list", "no_checkpoint", "partial_frame", "already_continued", "finished"])
def test_missing_or_ambiguous_original_history_cannot_resume(selection, fault):
    original, _, path, conversation, args = selection
    if fault == "absent": path.unlink()
    elif fault == "linked":
        target = path.with_name("other.json")
        path.rename(target)
        path.symlink_to(target)
    elif fault == "oversize": path.write_bytes(b" " * 262145)
    elif fault == "list": path.write_text("[]")
    elif fault == "no_checkpoint": conversation.checkpoint_id = None
    elif fault == "partial_frame": conversation.buffer = b'{"type"'
    elif fault == "already_continued": continuation_request(original, conversation, **args)
    else: conversation.finished = True
    with pytest.raises(TransformProtocolError):
        continuation_request(original, conversation, **args)


@pytest.mark.parametrize("bad", [
    {"type": "session_started", "conversationId": "original-native"},
    {"type": "resume_status", "conversationId": "foreign", "checkpointId": "original-native#checkpoint-a"},
    {"type": "resume_status", "conversationId": "original-native", "checkpointId": "other"},
    {"type": "agent_minutes_update", "used": 0.4},
    {"type": "complete", "status": "completed"},
])
def test_continuation_requires_exact_acknowledged_native_and_checkpoint(selection, bad):
    original, _, _, conversation, args = selection
    continuation_request(original, conversation, **args)
    with pytest.raises(TransformProtocolError): conversation.feed(frame(**bad))


@pytest.mark.parametrize("bad", [
    {"type": "agent_minutes_update", "used": 0.1},
    {"type": "complete", "status": "completed"},
])
def test_resumed_completion_requires_fresh_monotonic_usage(selection, bad):
    original, metadata, _, conversation, args = selection
    continuation_request(original, conversation, **args)
    conversation.feed(frame(type="resume_status", conversationId="original-native", checkpointId=metadata["latestCheckpointId"]))
    with pytest.raises(TransformProtocolError): conversation.feed(frame(**bad))


def test_capability_trace_keeps_policy_inputs_without_provider_contents(selection):
    _, _, _, conversation, _ = selection
    conversation.feed(frame(type="tool_permission_required", toolCallId="a", name="file_write",
        parameters={"path": "src/app.py", "content": "private-provider-file-content"}))
    conversation.feed(frame(type="tool_permission_required", toolCallId="b", name="shell",
        parameters={"command": "private-provider-command"}))
    trace = conversation.capability_trace()
    assert trace["frames"][-2]["decision"] == "approve"
    assert trace["frames"][-1]["decision"] == "deny"
    assert "private-provider" not in json.dumps(trace)


@pytest.mark.parametrize("name,parameters,control", [
    ("file_write", {"path": "src/app.py", "executable": True}, "denied_other_tool"),
    ("editor", {"path": "src/app.py", "command": "execute"}, "denied_other_tool"),
    ("file_read", {}, "denied_other_tool"),
    ("file_write", {"path": "src/outside.py"}, "denied_source_write"),
    ("file_read", {"path": "/proc/self/environ"}, "denied_source_read"),
])
def test_tool_control_classification_distinguishes_scope_from_other_denials(selection, name, parameters, control):
    _, _, _, conversation, _ = selection
    conversation.feed(frame(type="tool_permission_required", toolCallId="a", name=name, parameters=parameters))
    assert conversation.capability_trace()["frames"][-1]["control"] == control


@pytest.mark.parametrize("fault", ["different_native", "reset_usage", "no_resume_ack", "different_checkpoint", "foreign_checkpoint", "second_start", "changed_limit", "extra_frame", "copied_continuation"])
def test_retained_trace_refuses_fabricated_or_incomplete_session_history(selection, fault):
    original, metadata, _, conversation, args = selection
    continuation_request(original, conversation, **args)
    conversation.feed(frame(type="resume_status", conversationId="original-native", checkpointId=metadata["latestCheckpointId"]))
    conversation.feed(frame(type="agent_minutes_update", used=0.4))
    conversation.feed(frame(type="complete", status="completed"))
    value = conversation.capability_trace()
    if fault == "different_native": value["frames"][0]["native_id"] = "different"
    elif fault == "reset_usage": value["frames"][-2]["used"] = 0.1
    elif fault == "no_resume_ack": value["frames"].pop(-3)
    elif fault == "different_checkpoint": value["frames"][-3]["checkpoint_id"] = "different"
    elif fault == "foreign_checkpoint":
        for record in [*value["frames"], *value["continuations"]]:
            if "checkpoint_id" in record:
                record["checkpoint_id"] = "foreign#checkpoint-a"
    elif fault == "second_start": value["frames"].insert(1, deepcopy(value["frames"][0]))
    elif fault == "changed_limit": value["agent_minutes_limit"] = 2
    elif fault == "extra_frame": value["frames"].append({"type": "agent_minutes_update", "used": 0.5})
    else: value["continuations"][0]["metadata_sha256"] = "d" * 64
    with pytest.raises(ValueError): verify_provider_trace(value, native_id="original-native", agent_minutes=1)


@pytest.mark.parametrize("fault", [None, "second_exit", "no_metadata", "explicit_failure", "wrong_native", "deadline"])
def test_real_exited_child_continues_once_on_same_session_without_a_second_start(selection, monkeypatch, fault):
    original, metadata, path, _, args = selection
    home, workspace = args["home"], args["workspace"]
    script = home / "synthetic-provider.py"
    script.write_text('''
import json, os, sys, time
from pathlib import Path
request = json.loads(sys.stdin.readline())
home = Path(os.environ['HOME'])
with (home / 'requests.jsonl').open('a') as stream: stream.write(json.dumps(request) + '\\n')
def emit(**frame): print(json.dumps(frame), flush=True)
if 'conversation_id' not in request:
    emit(type='session_started', conversationId='original-native')
    emit(type='agent_minutes_update', used=0.2)
    emit(type='checkpoint', checkpointId='original-native#checkpoint-a')
    if os.environ['FAULT'] == 'explicit_failure': emit(type='error', message='provider refused')
    if os.environ['FAULT'] == 'deadline': time.sleep(0.6)
    sys.exit(17)
assert request['conversation_id'] == 'original-native'
assert request['params']['trustAllTools'] is False
assert request['params']['agentMinutesLimit'] == '1'
emit(type='resume_status', conversationId='foreign' if os.environ['FAULT'] == 'wrong_native' else 'original-native', checkpointId='original-native#checkpoint-a')
emit(type='agent_minutes_update', used=0.4)
if os.environ['FAULT'] == 'second_exit': sys.exit(18)
if os.environ['FAULT'] == 'deadline': time.sleep(0.7)
emit(type='complete', status='completed')
''')
    if fault == "no_metadata": path.unlink()
    intent = {"attempt_token": "b" * 64, "input_hash": "c" * 64,
        "read_paths": ["src"], "write_paths": ["src/app.py"], "excluded_paths": [".git"],
        "provider_job": {"session_request": deepcopy(original), "active_deadline_seconds": 1 if fault == "deadline" else 4}}
    conversation = TransformConversation(intent, workspace=workspace, agent_minutes=1)
    monkeypatch.setattr(provider_runner, "restrict_provider", lambda *args: None)
    monkeypatch.setattr(provider_runner, "emit", lambda *args: None)
    kwargs = dict(interpreter=sys.executable, executable=str(script), workspace=workspace, home=home,
        environment={"HOME": str(home), "AWS_REGION": "us-east-1", "FAULT": fault or ""},
        allowed_writes=["src/app.py"], baseline_commit=args["baseline_commit"])
    if fault in {"second_exit", "no_metadata", "wrong_native", "deadline"}:
        with pytest.raises(TransformProtocolError): provider_runner.run_session(intent, conversation, **kwargs)
    else:
        provider_runner.run_session(intent, conversation, **kwargs)
        assert conversation.state == ("failed" if fault == "explicit_failure" else "completed")
    requests = [json.loads(line) for line in (home / "requests.jsonl").read_text().splitlines()]
    assert len([request for request in requests if "conversation_id" not in request]) == 1
    assert len(requests) == (1 if fault in {"no_metadata", "explicit_failure"} else 2)
