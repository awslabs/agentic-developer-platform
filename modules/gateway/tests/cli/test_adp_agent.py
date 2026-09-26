"""Human Activity contract: real serializers, lost acknowledgements and SSE."""

import importlib.util
import io
import json
import sys
from email.message import Message
from pathlib import Path
from unittest.mock import Mock

import pytest

CLI = Path(__file__).parents[2] / "cli"
sys.path.insert(0, str(CLI))
spec = importlib.util.spec_from_file_location("activity_cli", CLI / "adp-agent.py")
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)
RUN = "owned-run"
ID = "67eb5564-4dce-4fa0-9320-72cf728ca140"


def args(action, *extra):
    return agent.parser().parse_args([action, "--run", RUN, *extra])


def state(**changes):
    from src.activity.control_schemas import ControlStateResponse

    value = dict(
        run_id=RUN, available=True, generation=1, state="running", capabilities={"pause": True, "resume": True, "abort": True, "steer": True}
    )
    value.update(changes)
    return ControlStateResponse(**value).model_dump(mode="json")


@pytest.mark.parametrize("action", sorted(agent.ACTIONS))
def test_request_ack_matches_server_schema(action):
    from src.activity.control_schemas import ControlCommandRequest, ControlCommandResponse, ControlSteerRequest

    client = Mock()
    client.get.return_value = state()
    client.post.return_value = ControlCommandResponse(
        run_id=RUN, action=action, command_id=ID, state="running", command_status="delivered"
    ).model_dump(mode="json")
    result = agent.execute(args(action, "--command-id", ID, "--instruction" if action == "steer" else "--reason", "bounded test", "--yes"), client)
    schema = ControlSteerRequest if action == "steer" else ControlCommandRequest
    schema.model_validate(client.post.call_args.args[1])
    assert result["status"] == "pending"
    assert client.post.call_args.args[0] == f"/activity/invocations/{RUN}/agent/{action}"


@pytest.mark.parametrize("change", [{"available": False}, {"capabilities": {}}, {"state": "terminal", "available": False}])
def test_unavailable_never_posts(change):
    client = Mock()
    client.get.return_value = state(**change)
    assert agent.execute(args("pause", "--command-id", ID, "--reason", "test", "--yes"), client)["status"] == "unavailable"
    client.post.assert_not_called()


def test_dry_run_generation_check_read_only():
    client = Mock()
    client.get.return_value = state()
    options = args("pause", "--command-id", ID, "--reason", "test", "--dry-run")
    assert agent.execute(options, client)["status"] == "dry_run"
    options.expected_generation = 2
    with pytest.raises(agent.common.CliError, match="generation changed"):
        agent.execute(options, client)
    client.post.assert_not_called()


@pytest.mark.parametrize("generation", [1, 2])
def test_lost_ack_reconciles_without_reposting(generation):
    client = Mock()
    client.get.side_effect = [state(), state(generation=generation, commands=[dict(command_id=ID, action="pause", status="applied")])]
    client.post.side_effect = agent.common.CliError("lost ack", "unknown_mutation_outcome", 4)
    result = agent.execute(args("pause", "--command-id", ID, "--reason", "test", "--yes"), client)
    assert result["detail"]["command_status"] == "unknown"
    assert result["status"] == "pending"
    if generation == 1:
        assert result["detail"]["observed_acknowledgement"]["status"] == "applied"
        assert result["detail"]["payload_verified"] is False
    else:
        assert "observed_acknowledgement" not in result["detail"]
    assert client.post.call_count == 1


@pytest.mark.parametrize("status", [401, 403, 404, 409, 410, 501])
def test_refusal_not_retried(status):
    client = Mock()
    client.get.return_value = state()
    client.post.side_effect = agent.common.CliError("refused", "http_error", 5, status_code=status)
    with pytest.raises(agent.common.CliError) as exc:
        agent.execute(args("pause", "--command-id", ID, "--reason", "test", "--yes"), client)
    assert exc.value.status_code == status
    assert client.post.call_count == 1


def test_empty_filtered_page_follows_cursor():
    from src.activity.schemas import InvocationListResponse

    client = Mock()
    client.get.side_effect = [
        InvocationListResponse(items=[], count=0, last_key="next").model_dump(),
        InvocationListResponse(items=[], count=0).model_dump(),
    ]
    result = agent.execute(agent.parser().parse_args(["list", "--max-pages", "2"]), client)
    assert result["detail"] == {"items": [], "last_key": None, "complete": True}
    assert "last_key=next" in client.get.call_args.args[0]


@pytest.mark.parametrize("payload", [{}, {"items": "wrong"}, {"items": [], "last_key": 1}])
def test_bad_pagination_refused(payload):
    client = Mock()
    client.get.return_value = payload
    with pytest.raises(agent.common.CliError):
        agent.execute(agent.parser().parse_args(["list"]), client)


class Response(io.BytesIO):
    def __init__(self, data, media="text/event-stream"):
        super().__init__(data)
        self.headers = Message()
        self.headers["Content-Type"] = media


def event(sequence=1, generation=1, kind="explanation"):
    from src.activity.explanation_stream import frame

    value = dict(
        version=1,
        invocation_id=RUN,
        generation=generation,
        sequence=sequence,
        timestamp="2026-09-25T00:00:00+00:00",
        kind=kind,
        payload={"text": "bounded update"} if kind == "explanation" else {},
    )
    return frame(kind, value, f"{RUN}:{generation}:{sequence}")


def test_server_frames_reconnect_without_duplicates(capsys):
    client = Mock()
    client.raw.side_effect = [Response(event()), Response(event() + event(2, kind="terminal"))]
    result = agent.execute(args("logs", "--follow", "--timeout", "3"), client)
    values = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [v["detail"]["data"]["sequence"] for v in values] == [1, 2]
    assert client.raw.call_args.kwargs["cursor"] == f"{RUN}:1:1"
    assert result["detail"]["stream_closed"] is True


def test_stream_generation_change_refused():
    client = Mock()
    client.raw.return_value = Response(event() + event(2, generation=2))
    with pytest.raises(agent.common.CliError, match="generation changed"):
        agent.execute(args("logs", "--follow"), client)


def test_markdown_transcript():
    client = Mock()
    client.raw.return_value = Response(b"# Retained transcript", "text/markdown")
    assert agent.execute(args("logs"), client)["detail"]["text"] == "# Retained transcript"
    assert client.raw.call_args.args[0] == f"/me/agent-invocations/{RUN}/transcript"


def test_transcript_404_not_empty_success():
    client = Mock()
    client.raw.side_effect = agent.common.CliError("not available", status_code=404)
    assert agent.execute(args("logs"), client)["status"] == "unavailable"


def test_wait_completed_only():
    client = Mock()
    client.get.return_value = {"invocation_id": RUN, "status": "aborted"}
    assert agent.execute(args("wait"), client)["status"] == "failed"
    client.get.return_value["status"] = "complete"
    assert agent.execute(args("wait"), client)["status"] == "ok"


def test_actual_common_transport_sends_only_human_credentials(monkeypatch):
    from src.activity.control_schemas import ControlCommandResponse

    monkeypatch.setattr(agent.common, "gateway_url", lambda: "https://gateway.example/api")
    monkeypatch.setattr(agent.common, "access_token", lambda: "human-session")
    client = agent.Client()
    opener = Mock()
    wire = ControlCommandResponse(run_id=RUN, action="pause", state="running", command_id=ID, command_status="pending").model_dump_json().encode()
    response = Response(wire, "application/json")
    response.status = 202
    opener.open.return_value = response
    client.api.opener = opener
    result = client.post(f"/activity/invocations/{RUN}/agent/pause", {"command_id": ID, "reason": "test"})
    request = opener.open.call_args.args[0]
    assert request.full_url == f"https://gateway.example/api/activity/invocations/{RUN}/agent/pause"
    assert request.get_header("Authorization") == "Bearer human-session"
    assert json.loads(request.data) == {"command_id": ID, "reason": "test"}
    assert result["command_status"] == "pending"
    assert not any("tenant" in key.lower() or "user" in key.lower() for key in request.headers)


def test_missing_confirmation_or_invalid_uuid_never_reads_or_posts():
    client = Mock()
    for command_id, confirmation in [(ID, []), ("invalid", ["--yes"])]:
        with pytest.raises(agent.common.CliError):
            agent.execute(args("pause", "--command-id", command_id, "--reason", "test", *confirmation), client)
    client.get.assert_not_called()
    client.post.assert_not_called()


def test_unknown_wait_status_never_succeeds(monkeypatch):
    client = Mock()
    client.get.return_value = {"invocation_id": RUN, "status": "future_status"}
    clock = iter([0, 0, 2])
    monkeypatch.setattr(agent.time, "monotonic", lambda: next(clock))
    assert agent.execute(args("wait", "--timeout", "1"), client)["status"] == "pending"


def test_wait_terminal_vocabulary_agrees_with_server():
    from src.activity.liveness import OBSERVED_TERMINAL_STATUSES

    client = Mock()
    for status in OBSERVED_TERMINAL_STATUSES:
        client.get.return_value = {"invocation_id": RUN, "status": status}
        assert agent.execute(args("wait"), client)["status"] == ("ok" if status == "complete" else "failed")


def test_mismatched_ack_is_unknown_not_success():
    client = Mock()
    client.get.return_value = state()
    client.post.return_value = {"run_id": "foreign-run", "action": "pause", "command_id": ID, "command_status": "applied"}
    result = agent.execute(args("pause", "--command-id", ID, "--reason", "test", "--yes"), client)
    assert result["status"] == "pending"
    assert result["detail"]["command_status"] == "unknown"
    assert result["detail"]["run_id"] == RUN


def test_preexisting_applied_id_does_not_confirm_changed_payload_after_lost_conflict(capsys):
    client = Mock()
    prior = state(commands=[dict(command_id=ID, action="pause", status="applied", reason="old payload")])
    client.get.side_effect = [prior, prior]
    # The server may have rejected changed text with 409, but that reply was lost.
    client.post.side_effect = agent.common.CliError("lost reply", "unknown_mutation_outcome", 4)
    result = agent.execute(args("pause", "--command-id", ID, "--reason", "changed payload", "--yes"), client)
    assert agent.common.emit(result, True) == 4
    assert result["detail"]["command_status"] == "unknown"
    assert result["detail"]["observed_acknowledgement"]["status"] == "applied"
    assert "informational" in result["detail"]["reconciliation_note"]
    assert client.post.call_count == 1


@pytest.mark.parametrize("status,expected_exit", [(401, 2), (403, 3), (404, 5), (409, 5)])
def test_raw_http_error_matches_common_auth_exit_classification(monkeypatch, status, expected_exit):
    import urllib.error

    monkeypatch.setattr(agent.common, "gateway_url", lambda: "https://gateway.example/api")
    monkeypatch.setattr(agent.common, "access_token", lambda: "human-session")
    client = agent.Client()
    client.api.opener = Mock()
    client.api.opener.open.side_effect = urllib.error.HTTPError("https://gateway.example/api", status, "refused", {}, None)
    with pytest.raises(agent.common.CliError) as exc:
        client.raw(f"/activity/invocations/{RUN}/agent/events", timeout=1)
    assert exc.value.status_code == status
    assert exc.value.exit_code == expected_exit


def test_future_cursor_reset_admits_retained_history_and_terminal(capsys):
    from src.activity.explanation_stream import frame

    client = Mock()
    client.raw.return_value = Response(
        frame("reset", {"reason": "History unavailable; showing retained updates."}) + event() + event(2, kind="terminal")
    )
    result = agent.execute(args("logs", "--follow", "--last-event-id", f"{RUN}:1:999"), client)
    values = [json.loads(line)["detail"] for line in capsys.readouterr().out.splitlines()]
    assert [value["event"] for value in values] == ["reset", "explanation", "terminal"]
    assert values[0]["last_event_id"] is None
    assert result["detail"]["last_event_id"] == f"{RUN}:1:2"
    assert result["detail"]["stream_closed"] is True
    assert client.raw.call_count == 1


def test_reset_preserves_worker_generation_guard():
    from src.activity.explanation_stream import frame

    client = Mock()
    client.raw.return_value = Response(frame("reset", {"reason": "History unavailable"}) + event(generation=2))
    with pytest.raises(agent.common.CliError, match="generation changed"):
        agent.execute(args("logs", "--follow", "--last-event-id", f"{RUN}:1:999"), client)


def test_upstream_unavailable_detaches_with_last_cursor(capsys):
    from src.activity.explanation_stream import frame

    client = Mock()
    client.raw.return_value = Response(event() + frame("unavailable", {"reason": "Live feed unavailable; reconnect to recheck access."}))
    result = agent.execute(args("logs", "--follow"), client)
    assert result["status"] == "unavailable"
    assert result["detail"]["last_event_id"] == f"{RUN}:1:1"
    assert result["detail"]["detached"] is True
    assert agent.common.emit(result, True) == 4


@pytest.mark.parametrize("argv", [["state", "--json"], ["state", "--run", RUN, "--unknown", "--json"]])
def test_parse_errors_emit_usage_json_without_reading_auth(monkeypatch, capsys, argv):
    client = Mock(side_effect=AssertionError("must not read authentication on parse error"))
    monkeypatch.setattr(agent, "Client", client)
    assert agent.main(argv) == 1
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "usage_error"
    assert "Traceback" not in output.err
    client.assert_not_called()


@pytest.mark.parametrize("failure", [KeyboardInterrupt(), agent.common.CliError("Stream unavailable", "gateway_unavailable", 4)])
def test_follow_keeps_ndjson_on_interrupt_or_error_without_json_flag(monkeypatch, capsys, failure):
    client = Mock()
    monkeypatch.setattr(agent, "Client", lambda: client)
    monkeypatch.setattr(agent, "execute", Mock(side_effect=failure))
    assert agent.main(["logs", "--run", RUN, "--follow"]) == 4
    result = json.loads(capsys.readouterr().out)
    assert result["status"] in {"pending", "failed"}
    client.post.assert_not_called()


def test_truncated_post_ack_reconciles_once_and_stays_unknown(monkeypatch):
    import http.client

    monkeypatch.setattr(agent.common, "gateway_url", lambda: "https://gateway.example/api")
    monkeypatch.setattr(agent.common, "access_token", lambda: "human-session")
    client = agent.Client()

    class BrokenResponse(Response):
        def read(self, *args):
            raise http.client.IncompleteRead(b"partial-ack", 100)

    responses = [
        Response(json.dumps(state()).encode(), "application/json"),
        BrokenResponse(b""),
        Response(json.dumps(state()).encode(), "application/json"),
    ]
    for response in responses:
        response.status = 200
    client.api.opener.open = Mock(side_effect=responses)
    result = agent.execute(args("pause", "--command-id", ID, "--reason", "bounded", "--yes"), client)
    assert result["status"] == "pending"
    assert result["detail"]["command_status"] == "unknown"
    assert [call.args[0].get_method() for call in client.api.opener.open.call_args_list] == ["GET", "POST", "GET"]


def test_truncated_sse_detaches_with_last_cursor(capsys):
    import http.client

    class BrokenStream(Response):
        def read1(self, size):
            if self.tell():
                raise http.client.IncompleteRead(b"partial-event", 100)
            return super().read1(size)

    client = Mock()
    client.raw.return_value = BrokenStream(event())
    result = agent.execute(args("logs", "--follow"), client)
    assert result["status"] == "pending"
    assert result["detail"] == {"detached": True, "last_event_id": f"{RUN}:1:1"}
    client.post.assert_not_called()


def test_gateway_finished_is_detach_not_execution_success(capsys):
    from src.activity.explanation_stream import frame

    client = Mock()
    client.raw.return_value = Response(event() + frame("finished", {}))
    result = agent.execute(args("logs", "--follow"), client)
    assert result["status"] == "pending"
    assert result["detail"] == {"event": "finished", "detached": True, "last_event_id": f"{RUN}:1:1"}
    assert client.raw.call_count == 1
    client.post.assert_not_called()


@pytest.fixture
def coding_trigger(tmp_path, monkeypatch):
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({"repository": "owner/repo", "issue": 5}))
    instructions = tmp_path / "instructions.txt"
    instructions.write_text("Update the attached CLI file")
    monkeypatch.setattr(agent.common, "state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(agent.common, "authenticated_scope", lambda **kw: "human:owner")
    options = agent.parser().parse_args(
        [
            "trigger",
            "--repo",
            "owner/repo",
            "--issue",
            "5",
            "--persona",
            "agent-task-codex-developer",
            "--snapshot-file",
            str(snapshot),
            "--instructions-file",
            str(instructions),
            "--request-id",
            "coding-test",
        ]
    )
    task = Mock(gateway="https://gateway.example", token="pinned")
    helper = Mock()
    helper.bounded_read.side_effect = lambda response, *a: response.read()
    task.open.return_value = io.BytesIO(
        json.dumps(
            {
                "artifact_id": "art_67eb5564-4dce-4fa0-9320-72cf728ca140",
                "content_type": "application/json",
                "content_sha256": agent.hashlib.sha256(snapshot.read_bytes()).hexdigest(),
            }
        ).encode()
    )
    task.submit.return_value = {"task_id": "tsk_67eb5564-4dce-4fa0-9320-72cf728ca140", "status": "accepted"}
    return options, helper, task


def test_coding_preview_never_uploads_or_submits(coding_trigger):
    options, helper, task = coding_trigger
    assert agent.task_trigger(options, helper, task)["status"] == "dry_run"
    task.open.assert_not_called()
    task.submit.assert_not_called()


def test_coding_replay_retains_artifact_and_task_idempotency(coding_trigger):
    options, helper, task = coding_trigger
    options.yes = True
    first = agent.task_trigger(options, helper, task)
    second = agent.task_trigger(options, helper, task)
    assert first == second
    assert task.open.call_count == 1
    assert task.submit.call_args_list[0] == task.submit.call_args_list[1]
    body, key = task.submit.call_args.args
    assert key == "coding-test"
    assert body["inputs"]["repository_snapshot_artifact"] == body["artifact_ids"][0]
    assert first["status"] == "pending"


def test_coding_lost_upload_does_not_dispatch_or_reupload(coding_trigger):
    options, helper, task = coding_trigger
    options.yes = True
    task.open.side_effect = agent.common.CliError("response lost", "pending", 4)
    with pytest.raises(agent.common.CliError):
        agent.task_trigger(options, helper, task)
    result = agent.task_trigger(options, helper, task)
    assert result["status"] == "pending"
    assert task.open.call_count == 1
    task.submit.assert_not_called()


def test_coding_reused_request_with_new_inputs_refused(coding_trigger):
    options, helper, task = coding_trigger
    options.yes = True
    agent.task_trigger(options, helper, task)
    Path(options.instructions_file).write_text("Different task")
    with pytest.raises(agent.common.CliError, match="different task inputs"):
        agent.task_trigger(options, helper, task)
    assert task.submit.call_count == 1


@pytest.mark.parametrize("action", ["pause", "resume", "status", "wait", "abort", "steer"])
def test_task_handles_use_canonical_task_helper(monkeypatch, action):
    from types import SimpleNamespace

    task_id = "tsk_67eb5564-4dce-4fa0-9320-72cf728ca140"
    task = Mock()
    task.snapshot.return_value = {"task_id": task_id, "status": "completed"}
    task.command.return_value = {"task_id": task_id, "command_id": ID}
    helper = SimpleNamespace(
        TaskClient=Mock(return_value=task),
        token_expiry=lambda token: 9999999999,
        TERMINAL={"completed", "failed", "cancelled"},
        snapshot_exit=lambda value: 0,
    )
    monkeypatch.setattr(agent.common, "load_provider", lambda name: helper)
    monkeypatch.setattr(agent.common, "gateway_url", lambda: "https://gateway.example")
    flags = ["--command-id", ID, "--instruction" if action == "steer" else "--reason", "bounded", "--yes"] if action in agent.ACTIONS else []
    options = agent.parser().parse_args([action, "--run", task_id, *flags])
    client = Mock(token="pinned-human-token")
    result = agent.execute(options, client)
    assert task.token == "pinned-human-token"
    client.get.assert_not_called()
    client.post.assert_not_called()
    if action in {"pause", "resume"}:
        assert result["status"] == "unavailable"
        task.command.assert_not_called()
    elif action in {"abort", "steer"}:
        assert result["status"] == "pending"
        assert task.command.call_args.args[1] == ("cancel" if action == "abort" else "messages")
    else:
        assert result["status"] == "ok"


def test_task_listing_uses_owner_projection_and_follows_empty_authorized_page():
    client = Mock()
    client.get.side_effect = [dict(items=[], last_key="next"), dict(items=[dict(task_id="tsk-owned")], last_key=None)]
    result = agent.execute(agent.parser().parse_args(["list", "--tasks", "--max-pages", "2"]), client)
    assert result["detail"]["items"] == [dict(task_id="tsk-owned")]
    assert client.get.call_args_list[0].args[0] == "/me/agent-invocations/tasks?page_size=20"
    assert client.get.call_args_list[1].args[0].endswith("last_key=next")
    client.post.assert_not_called()


@pytest.mark.parametrize("extra", [["--admin"], ["--page-size", "21"]])
def test_task_listing_rejects_admin_and_unbounded_page_before_io(extra):
    client = Mock()
    with pytest.raises(agent.common.CliError):
        agent.execute(agent.parser().parse_args(["list", "--tasks", *extra]), client)
    client.get.assert_not_called()


@pytest.mark.parametrize("persona", ["agent-task-claude-developer", "agent-task-codex-developer"])
@pytest.mark.parametrize("mode", ["--dry-run", "--yes"])
def test_coding_steer_is_unavailable_before_any_command(monkeypatch, persona, mode):
    from types import SimpleNamespace

    task_id = "tsk_67eb5564-4dce-4fa0-9320-72cf728ca140"
    task = Mock()
    task.snapshot.return_value = {"task_id": task_id, "status": "running", "persona": persona}
    helper = SimpleNamespace(TaskClient=Mock(return_value=task), token_expiry=lambda _: 9999999999)
    monkeypatch.setattr(agent.common, "load_provider", lambda name: helper)
    monkeypatch.setattr(agent.common, "gateway_url", lambda: "https://gateway.example")
    options = agent.parser().parse_args(["steer", "--run", task_id, "--command-id", ID, "--instruction", "marker", mode])
    result = agent.execute(options, Mock(token="pinned-human-token"))
    assert result["status"] == "unavailable"
    task.command.assert_not_called()
