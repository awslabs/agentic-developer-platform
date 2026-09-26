"""Independent Task CLI behavior tests using real command and protocol code.

Only HTTP is substituted. No test provisions identities, dispatches live work,
or reads the operator's credentials.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import time
import urllib.error
from pathlib import Path

import pytest

CLI = Path(__file__).parents[2] / "cli"
GATEWAY = "https://task-deployment.example.test/api"
TASK = "tsk_00000000-0000-4000-8000-000000000001"
COMMAND = "00000000-0000-4000-8000-000000000002"
SECRET = "private-fixture-secret-never-display"
TOKEN = "private-fixture-token-never-display"


class Response(io.BytesIO):
    headers = {}


def response(value):
    return Response(json.dumps(value).encode())


def http_error(status, body=None):
    return urllib.error.HTTPError(GATEWAY, status, "failure", {}, io.BytesIO(json.dumps(body or {}).encode()))


class Opener:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []
        self.timeouts = []

    def open(self, request, timeout):
        self.requests.append(request)
        self.timeouts.append(timeout)
        result = next(self.responses)
        if callable(result):
            result = result(request)
        if isinstance(result, BaseException):
            raise result
        return result


def event(sequence, kind="progress.updated", **extra):
    payload = {"type": kind, "task_id": TASK, "sequence": sequence, "data": {"message": "authored progress"}, **extra}
    return f"id: {TASK}:{sequence}\nevent: event\ndata: {json.dumps(payload)}\n\n".encode()


@pytest.fixture
def cli(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(CLI))
    for key in list(os.environ):
        if key.startswith("ADP_") or key in {"BG_CONFIG_DIR", "BG_AWS_PROFILE"}:
            monkeypatch.delenv(key, raising=False)
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    spec = importlib.util.spec_from_file_location("task_cli_independent", CLI / "adp-task.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.common, "_deployment", module.common._UNRESOLVED)
    config = home / ".bedrock-gateway"
    config.mkdir(mode=0o700)
    (config / "config.json").write_text(json.dumps({"gateway_url": GATEWAY}))
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    return module


@pytest.fixture
def token_file(tmp_path):
    path = tmp_path / "token.json"
    path.write_text(json.dumps({"gateway_url": GATEWAY, "access_token": TOKEN, "expires_at": time.time() + 3600}))
    path.chmod(0o600)
    return path


@pytest.fixture
def oauth_file(tmp_path):
    path = tmp_path / "oauth.json"
    path.write_text(
        json.dumps(
            {"gateway_url": GATEWAY, "token_url": "https://auth.example.test/oauth2/token", "client_id": "registered-client", "client_secret": SECRET}
        )
    )
    path.chmod(0o600)
    return path


def install_transport(monkeypatch, cli, responses):
    opener = Opener(responses)
    monkeypatch.setattr(cli.urllib.request, "build_opener", lambda *args: opener)
    return opener


def run(cli, capsys, args):
    code = cli.main(args)
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
    assert TOKEN not in output.out + output.err
    return code, [json.loads(line) for line in output.out.splitlines() if line], output


def test_submit_ambiguous_retry_preserves_exact_body_key_and_handles(cli, token_file, tmp_path, monkeypatch, capsys):
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"schema_version": "1.0", "persona": "agent-task-investigator", "instructions": "investigate café"}))
    opener = install_transport(
        monkeypatch, cli, [urllib.error.URLError("response lost"), response({"task_id": TASK, "invocation_id": COMMAND, "status": "accepted"})]
    )
    code, rows, _ = run(cli, capsys, ["submit", str(request), "--key", "durable-key", "--token-file", str(token_file), "--json"])
    assert code == 0
    assert rows[0]["data"]["task_id"] == TASK
    assert rows[0]["data"]["invocation_id"] == COMMAND
    assert len(opener.requests) == 2
    first, retry = opener.requests
    assert first.data == retry.data
    assert first.get_header("Idempotency-key") == retry.get_header("Idempotency-key") == "durable-key"
    assert first.full_url == retry.full_url == GATEWAY + "/v1/tasks"


def test_submit_conflict_is_explicit_not_retried(cli, token_file, tmp_path, monkeypatch, capsys):
    request = tmp_path / "request.json"
    request.write_text("{}")
    opener = install_transport(monkeypatch, cli, [http_error(409, {"secret": SECRET})])
    code, rows, _ = run(cli, capsys, ["submit", str(request), "--key", "reused", "--token-file", str(token_file), "--json"])
    assert code == 5
    assert rows[-1]["error"]["code"] == "task_conflict"
    assert len(opener.requests) == 1


@pytest.mark.parametrize("status", [401, 403, 404])
def test_denied_status_does_not_leak_response_or_task_data(cli, token_file, monkeypatch, capsys, status):
    opener = install_transport(
        monkeypatch, cli, [http_error(status, {"access_token": TOKEN, "secret": SECRET, "task": "other-tenant"}) for _ in range(2)]
    )
    code, rows, output = run(cli, capsys, ["status", TASK, "--token-file", str(token_file), "--json"])
    assert code == (2 if status in (401, 403) else 5)
    assert "other-tenant" not in output.out + output.err
    assert all(row.get("type") != "snapshot" for row in rows)
    assert len(opener.requests) == (2 if status == 401 else 1)


def test_oauth_401_refreshes_once_and_preserves_resource_request(cli, oauth_file, monkeypatch, capsys):
    opener = install_transport(
        monkeypatch,
        cli,
        [
            response({"access_token": "first", "expires_in": 900}),
            http_error(401),
            response({"access_token": "second", "expires_in": 900}),
            response({"task_id": TASK, "status": "completed"}),
        ],
    )
    code, _, _ = run(cli, capsys, ["status", TASK, "--credentials", str(oauth_file), "--json"])
    assert code == 0
    assert len(opener.requests) == 4
    assert opener.requests[1].get_header("Authorization") == "Bearer first"
    assert opener.requests[3].get_header("Authorization") == "Bearer second"
    assert opener.requests[1].full_url == opener.requests[3].full_url
    assert b"scope=adp-tasks%2Fread" in opener.requests[0].data
    assert SECRET.encode() not in opener.requests[0].data


def test_wrong_deployment_credential_env_binding_fails_before_http(cli, oauth_file, monkeypatch, capsys):
    value = json.loads(oauth_file.read_text())
    value["gateway_url"] = "https://another.example.test/api"
    oauth_file.write_text(json.dumps(value))
    monkeypatch.setenv("ADP_TASK_CREDENTIALS_FILE", str(oauth_file))
    opener = install_transport(monkeypatch, cli, [])
    code, rows, _ = run(cli, capsys, ["status", TASK, "--json"])
    assert code == 2
    assert rows[-1]["error"]["code"] == "deployment_mismatch"
    assert not opener.requests


def test_browser_login_is_not_silent_task_authorization(cli, monkeypatch, capsys):
    tokens = cli.common.config_path().parent / "tokens.json"
    tokens.write_text(json.dumps({"access_token": TOKEN, "expires_at": time.time() + 3600}))
    tokens.chmod(0o600)
    opener = install_transport(monkeypatch, cli, [])
    code, _, output = run(cli, capsys, ["status", TASK, "--json"])
    assert code == 2
    assert "Task" in output.out
    assert not opener.requests


@pytest.mark.parametrize("mode", ["world-readable", "symlink", "expired"])
def test_unsafe_or_expired_token_file_never_sends_token(cli, token_file, tmp_path, monkeypatch, capsys, mode):
    if mode == "world-readable":
        token_file.chmod(0o644)
    elif mode == "symlink":
        link = tmp_path / "link.json"
        link.symlink_to(token_file)
        token_file = link
    else:
        token_file.write_text(json.dumps({"gateway_url": GATEWAY, "access_token": TOKEN, "expires_at": 1}))
    opener = install_transport(monkeypatch, cli, [])
    code, _, _ = run(cli, capsys, ["status", TASK, "--token-file", str(token_file), "--json"])
    assert code == 2
    assert not opener.requests


def test_monitor_reconnect_deduplicates_and_persists_only_handled_cursor(cli, token_file, tmp_path, monkeypatch, capsys):
    cursor = tmp_path / "private" / "cursor.json"
    running = {"task_id": TASK, "status": "running"}
    opener = install_transport(
        monkeypatch,
        cli,
        [
            response(running),
            Response(event(1)),
            response(running),
            Response(event(1) + event(2, "task.completed")),
            response({"task_id": TASK, "status": "completed", "result": {"artifact_id": "artifact"}}),
        ],
    )
    code, rows, _ = run(cli, capsys, ["monitor", TASK, "--cursor-file", str(cursor), "--token-file", str(token_file), "--json"])
    assert code == 0
    events = [row["data"] for row in rows if row.get("type") == "event"]
    assert [row["id"] for row in events] == [TASK + ":1", TASK + ":2"]
    streams = [request for request in opener.requests if request.full_url.endswith("/events")]
    assert streams[1].get_header("Last-event-id") == TASK + ":1"
    assert json.loads(cursor.read_text())["cursor"] == TASK + ":2"
    assert cursor.stat().st_mode & 0o077 == 0
    assert rows[-1]["type"] == "resume"


@pytest.mark.parametrize("fault", ["expired", "explicit-gap", "sequence-gap"])
def test_monitor_history_loss_never_claims_continuity(cli, token_file, monkeypatch, capsys, fault):
    stream = http_error(410) if fault == "expired" else Response(event(2, "history.gap") if fault == "explicit-gap" else event(3))
    opener = install_transport(monkeypatch, cli, [response({"status": "running"}), stream])
    code, rows, _ = run(cli, capsys, ["monitor", TASK, "--cursor", TASK + ":1", "--token-file", str(token_file), "--json"])
    assert code == 6
    resume = next(row["data"] for row in rows if row.get("type") == "resume")
    assert resume["cursor"] == TASK + ":1"
    assert not any(request.get_method() == "POST" for request in opener.requests)


def test_monitor_ctrl_c_stops_locally_without_cancel(cli, token_file, monkeypatch, capsys):
    class Interrupted(Response):
        def read1(self, *args):
            raise KeyboardInterrupt

    opener = install_transport(monkeypatch, cli, [response({"status": "running"}), Interrupted()])
    code, rows, _ = run(cli, capsys, ["monitor", TASK, "--token-file", str(token_file), "--json"])
    assert code == 130
    assert rows[-1]["data"]["remote_abort_requested"] is False
    assert all(request.get_method() == "GET" for request in opener.requests)


def test_monitor_output_failure_does_not_advance_saved_cursor(cli, token_file, tmp_path, monkeypatch, capsys):
    cursor = tmp_path / "private" / "cursor.json"
    install_transport(monkeypatch, cli, [response({"status": "running"}), Response(event(1))])
    original = cli.output

    def failed_output(kind, value, as_json):
        if kind == "event":
            raise BrokenPipeError
        return original(kind, value, as_json)

    monkeypatch.setattr(cli, "output", failed_output)
    code, _, _ = run(cli, capsys, ["monitor", TASK, "--cursor-file", str(cursor), "--token-file", str(token_file), "--json"])
    assert code == 3
    assert not cursor.exists()


def test_monitor_event_bound_returns_timeout_and_resume(cli, token_file, monkeypatch, capsys):
    opener = install_transport(monkeypatch, cli, [response({"status": "running"}), Response(event(1) + event(2))])
    code, rows, _ = run(cli, capsys, ["monitor", TASK, "--max-events", "1", "--token-file", str(token_file), "--json"])
    assert code == 4
    assert len([row for row in rows if row.get("type") == "event"]) == 1
    assert rows[-1]["data"]["cursor"] == TASK + ":1"
    assert len(opener.requests) == 2


def test_abort_retry_preserves_command_and_never_claims_confirmed_exit(cli, token_file, monkeypatch, capsys):
    opener = install_transport(monkeypatch, cli, [TimeoutError(), response({"command_id": COMMAND, "status": "accepted"})])
    code, rows, _ = run(
        cli, capsys, ["abort", TASK, "--command-id", COMMAND, "--reason", "stop investigation", "--yes", "--token-file", str(token_file), "--json"]
    )
    assert code == 4
    assert rows[0]["data"]["terminal_cancellation_confirmed"] is False
    assert opener.requests[0].data == opener.requests[1].data
    assert json.loads(opener.requests[0].data)["command_id"] == COMMAND
    assert opener.requests[0].full_url.endswith("/cancel")


@pytest.mark.parametrize("state,expected", [("cancelled", 7), ("completed", 5), ("failed", 5)])
def test_abort_wait_distinguishes_cancel_from_completion_race(cli, token_file, monkeypatch, capsys, state, expected):
    install_transport(
        monkeypatch,
        cli,
        [
            response({"command_id": COMMAND, "status": "accepted"}),
            response({"task_id": TASK, "status": state, "error": {"child_exit_confirmed": True, "recovery_required": False}}),
        ],
    )
    code, _, _ = run(
        cli, capsys, ["abort", TASK, "--command-id", COMMAND, "--reason", "stop", "--yes", "--wait", "--token-file", str(token_file), "--json"]
    )
    assert code == expected


@pytest.fixture
def bounded_http_server():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    stop = threading.Event()
    stream_open = threading.Event()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):  # noqa: N802 — standard-library HTTP handler interface
            requests.append(("GET", self.path))
            if self.path.endswith("/events"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                stream_open.set()
                # A malicious/slow peer never finishes a single SSE line.
                try:
                    while not stop.is_set():
                        self.wfile.write(b"d")
                        self.wfile.flush()
                        stop.wait(0.1)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            else:
                body = json.dumps({"task_id": TASK, "status": "running"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def do_POST(self):  # noqa: N802 — standard-library HTTP handler interface
            requests.append(("POST", self.path))
            self.send_error(500, "No remote mutation is permitted in this fixture")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/api", requests, stream_open
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


def test_real_http_dribbling_frame_cannot_extend_total_monitor_deadline(cli, token_file, bounded_http_server, capsys):
    gateway, requests, _ = bounded_http_server
    cli.common.config_path().write_text(json.dumps({"gateway_url": gateway}))
    cli.common._deployment = cli.common._UNRESOLVED
    token_file.write_text(json.dumps({"gateway_url": gateway, "access_token": TOKEN, "expires_at": time.time() + 3600}))
    started = time.monotonic()
    code, rows, _ = run(cli, capsys, ["monitor", TASK, "--timeout", "1", "--token-file", str(token_file), "--json"])
    elapsed = time.monotonic() - started
    assert code == 4
    assert elapsed < 2.5
    assert any(path.endswith("/events") for _, path in requests)
    assert not any(method == "POST" for method, _ in requests)
    assert not any(row.get("type") == "event" for row in rows)


def test_real_cli_sigint_reports_resume_without_remote_abort(cli, token_file, bounded_http_server):
    import signal
    import subprocess

    gateway, requests, stream_open = bounded_http_server
    cli.common.config_path().write_text(json.dumps({"gateway_url": gateway}))
    cli.common._deployment = cli.common._UNRESOLVED
    token_file.write_text(json.dumps({"gateway_url": gateway, "access_token": TOKEN, "expires_at": time.time() + 3600}))
    process = subprocess.Popen(
        [sys.executable, str(CLI / "adp-task.py"), "monitor", TASK, "--timeout", "20", "--token-file", str(token_file), "--json"],
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert stream_open.wait(5), "CLI never began the real HTTP stream"
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 130, stderr
        rows = [json.loads(line) for line in stdout.splitlines() if line]
        assert rows[-1]["type"] == "resume"
        assert rows[-1]["data"]["remote_abort_requested"] is False
        assert not any(method == "POST" for method, _ in requests)
        assert TOKEN not in stdout + stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_terminal_snapshot_after_disconnect_still_replays_requested_history(cli, token_file, monkeypatch, capsys):
    terminal = {"task_id": TASK, "status": "completed", "latest_event_cursor": TASK + ":4", "result": {"artifact_id": "artifact"}}
    opener = install_transport(monkeypatch, cli, [response(terminal), Response(event(3) + event(4, "task.completed")), response(terminal)])
    code, rows, _ = run(cli, capsys, ["monitor", TASK, "--cursor", TASK + ":2", "--token-file", str(token_file), "--json"])
    assert code == 0
    assert [row["data"]["id"] for row in rows if row.get("type") == "event"] == [TASK + ":3", TASK + ":4"]
    assert rows[-1]["data"]["cursor"] == TASK + ":4"
    stream = next(request for request in opener.requests if request.full_url.endswith("/events"))
    assert stream.get_header("Last-event-id") == TASK + ":2"


def test_submit_response_body_timeout_retries_identical_wire_request(cli, token_file, tmp_path, monkeypatch, capsys):
    class LostBody(Response):
        def read1(self, *args):
            raise TimeoutError("response body lost after server committed")

    request = tmp_path / "request.json"
    request.write_text('{"instructions":"check"}')
    opener = install_transport(monkeypatch, cli, [LostBody(), response({"task_id": TASK, "invocation_id": COMMAND, "status": "accepted"})])
    code, _, _ = run(cli, capsys, ["submit", str(request), "--key", "body-retry-key", "--token-file", str(token_file), "--json"])
    assert code == 0
    assert len(opener.requests) == 2
    assert opener.requests[0].data == opener.requests[1].data
    assert opener.requests[0].get_header("Idempotency-key") == opener.requests[1].get_header("Idempotency-key") == "body-retry-key"


def test_monitor_revocation_after_progress_preserves_resume_without_repost(cli, token_file, monkeypatch, capsys):
    opener = install_transport(monkeypatch, cli, [response({"status": "running"}), Response(event(1)), http_error(403, {"token": TOKEN})])
    code, rows, _ = run(cli, capsys, ["monitor", TASK, "--token-file", str(token_file), "--json"])
    assert code == 2
    assert next(row["data"] for row in rows if row.get("type") == "resume")["cursor"] == TASK + ":1"
    assert all(request.get_method() == "GET" for request in opener.requests)


@pytest.mark.parametrize("wrong_binding", [False, True])
def test_token_file_refresh_revalidates_deployment_binding(cli, token_file, monkeypatch, capsys, wrong_binding):
    def rotate(_request):
        token_file.write_text(
            json.dumps(
                {
                    "gateway_url": "https://wrong.example.test/api" if wrong_binding else GATEWAY,
                    "access_token": "rotated-token",
                    "expires_at": time.time() + 3600,
                }
            )
        )
        return http_error(401)

    opener = install_transport(monkeypatch, cli, [rotate, response({"status": "completed", "task_id": TASK})])
    code, _, output = run(cli, capsys, ["status", TASK, "--token-file", str(token_file), "--json"])
    assert code == (2 if wrong_binding else 0)
    assert len(opener.requests) == (1 if wrong_binding else 2)
    if not wrong_binding:
        assert opener.requests[1].get_header("Authorization") == "Bearer rotated-token"
    assert "rotated-token" not in output.out + output.err


def test_oauth_transport_failure_is_sanitized_and_classified(cli, oauth_file, monkeypatch, capsys):
    install_transport(monkeypatch, cli, [urllib.error.URLError(SECRET)] * 3)
    code, rows, _ = run(cli, capsys, ["status", TASK, "--credentials", str(oauth_file), "--json"])
    assert code == 3
    assert rows[-1]["error"]["code"] in {"transport_failure", "authentication_transport_failure"}


def test_abort_terminal_cancelled_with_unconfirmed_stop_is_not_confirmation(cli, token_file, monkeypatch, capsys):
    fixture = Path(__file__).parents[4] / "docs/task-api/contracts/v1/fixtures/valid/result-cancelled-stop-unconfirmed.json"
    error = json.loads(fixture.read_text())
    error.pop("$fixture")
    install_transport(
        monkeypatch,
        cli,
        [
            response({"command_id": COMMAND, "status": "accepted"}),
            response({"task_id": TASK, "status": "cancelled", "error": error, "recovery_required": True}),
        ],
    )
    code, rows, _ = run(
        cli, capsys, ["abort", TASK, "--command-id", COMMAND, "--reason", "stop", "--yes", "--wait", "--token-file", str(token_file), "--json"]
    )
    assert code == 4
    assert not any(row.get("type") == "abort_confirmed" and row["data"].get("terminal_cancellation_confirmed") for row in rows)


def test_oversized_submit_file_is_rejected_before_auth_or_dispatch(cli, token_file, tmp_path, monkeypatch, capsys):
    request = tmp_path / "oversized.json"
    request.write_bytes(b" " * (1024 * 1024 + 1))
    opener = install_transport(monkeypatch, cli, [])
    code, rows, _ = run(cli, capsys, ["submit", str(request), "--key", "too-large", "--token-file", str(token_file), "--json"])
    assert code == 1
    assert rows[-1]["error"]["code"] == "usage_error"
    assert not opener.requests


@pytest.mark.parametrize("snapshot", [{}, {"status": "complete"}, {"status": None}, {"status": "future_state"}])
def test_unknown_snapshot_cannot_report_success(cli, snapshot):
    with pytest.raises(cli.CliError) as error:
        cli.snapshot_exit(snapshot)
    assert error.value.exit_code == 3


def test_human_login_pins_token_without_reading_service_credentials(cli, monkeypatch):
    from unittest.mock import Mock

    token = Mock(return_value=TOKEN)
    monkeypatch.setattr(cli.common, "access_token", token)
    opener = Opener([response({"ok": True}), response({"ok": True})])
    client = cli.TaskClient({"gateway_url": GATEWAY}, GATEWAY, human_login=True, opener=opener)
    assert client.json("GET", "/v1/tasks/" + TASK) == {"ok": True}
    assert client.json("GET", "/v1/tasks/" + TASK) == {"ok": True}
    token.assert_called_once()
    assert all(request.headers["Authorization"] == "Bearer " + TOKEN for request in opener.requests)
    with pytest.raises(cli.CliError):
        client.authenticate(force=True)
    token.assert_called_once()


def test_human_login_conflicts_with_service_files_before_auth(cli, token_file, monkeypatch):
    from unittest.mock import Mock

    token = Mock()
    monkeypatch.setattr(cli.common, "access_token", token)
    assert cli.main(["status", TASK, "--human-login", "--token-file", str(token_file), "--json"]) == 1
    token.assert_not_called()
