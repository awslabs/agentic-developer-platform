"""Real coordinator/Unix IPC/MCP/private-host processes with an offline issuer."""

import base64
import copy
import importlib.util
import json
import os
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

CLI = Path(__file__).parents[2] / "cli"
sys.path.insert(0, str(CLI))


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


host = module("native_coordinator", CLI / "adp-native-credential-host.py")
mcp = module("native_test_mcp", CLI / "adp-mcp.py")
cases = module("native_client_cases", Path(__file__).with_name("native_credential_client_cases.py"))
handoff = host.handoff
common = host.common
DEFAULT_SOCKET_PATH = handoff.socket_path
TEST_SUBJECT = "test-" + str(uuid.uuid4())


def token(**changes):
    claims = {
        "iss": "https://cognito.example.invalid/pool",
        "sub": TEST_SUBJECT,
        "client_id": "native-client",
        "jti": "native-session",
        "token_use": "access",
        "exp": int(time.time()) + 3600,
        **changes,
    }
    return "header." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".signature"


class SharedIssuer(cases.OfflineIssuer):
    """File state stands in for a separate issuer process, never native routing."""

    def __init__(self, signer, directory):
        super().__init__(signer)
        self.directory = directory
        self.base = "https://gateway.example.invalid/api"
        self.token = token()

    def request(self, method, path, body=None):
        options = json.loads((self.directory / "options.json").read_text())
        if options.get("auth_denied"):
            raise common.CliError("CANARY_PRIVATE_AUTH_BODY", "permission_denied", status_code=403)
        self.config.update(options.get("config", {}))
        with (self.directory / "calls.jsonl").open("a") as output:
            output.write(json.dumps({"method": method, "path": path}) + "\n")
        if path.endswith("/config"):
            return copy.deepcopy(self.config)
        if path.endswith("/requests"):
            request_id = body["request_id"]
        elif "/by-request/" in path:
            request_id = path.rsplit("/", 1)[-1]
        else:
            receipt = path.split("/requests/", 1)[1].split("/", 1)[0]
            lookup = self.directory / ("receipt-" + receipt)
            if not lookup.exists():
                raise common.CliError("not found", "native_delivery_unavailable", status_code=404)
            request_id = lookup.read_text()
        state = self.directory / (request_id + ".json")
        if state.exists():
            saved = json.loads(state.read_text())
            self.original, self.offer, self.challenge = saved["original"], saved["offer"], saved["challenge"]
        elif not path.endswith("/requests"):
            raise common.CliError("not found", "native_delivery_unavailable", status_code=404)
        self.decision = options.get("decision", "approved")
        self.fail = options.get("fail")
        if method == "GET":
            result = {**self.original, "status": saved.get("status", self.decision)}
            if "ack" in saved:
                result.update(acknowledgment=saved["ack"], acknowledgment_source="client_reported")
            return result
        result = super().request(method, path, body)
        saved = {"original": self.original, "offer": self.offer, "challenge": self.challenge}
        if path.endswith("/requests"):
            saved["status"] = "created"
            (self.directory / ("receipt-" + self.original["receipt_id"])).write_text(request_id)
        if path.endswith("/issue"):
            saved["status"] = "delivered"
        elif path.endswith("/ack"):
            saved.update(status="acknowledged", ack=body)
        common.write_json(state, saved)
        return result


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    for key in (
        "ADP_CREW_TOOLS_URL",
        "ADP_CREW_TOOLS_TOKEN",
        "ADP_CREW_RUN_ID",
        "SIGV4_PROXY_TARGET",
        "SIGV4_PROXY_PORT",
        "ADP_RUN_CREDENTIAL",
        "ADP_RUN_CREDENTIAL_FILE",
    ):
        monkeypatch.delenv(key, raising=False)
    sink_generator = cases.root.__wrapped__()
    sink = next(sink_generator)
    issuer = tmp_path / "issuer"
    issuer.mkdir(mode=0o700)
    (issuer / "options.json").write_text("{}")
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    cwd = tmp_path / "cwd"
    cwd.mkdir(mode=0o700)
    signer = cases.signer.__wrapped__()
    monkeypatch.setattr(common, "deployment", lambda: SimpleNamespace(id="default", legacy=True))
    monkeypatch.setattr(common, "state_dir", lambda: state)
    monkeypatch.setattr(common, "gateway_url", lambda: "https://gateway.example.invalid/api")
    monkeypatch.setattr(common, "access_token", token)
    monkeypatch.setattr(handoff, "socket_path", lambda binding: sink / "host.sock")
    monkeypatch.setattr(host.client, "volatile_root", lambda: str(sink))
    monkeypatch.setattr(mcp, "operation_directory", lambda: str(state))
    monkeypatch.setattr(mcp, "gateway_request", lambda *args, **kwargs: pytest.fail("private call reached generic facade"))
    monkeypatch.setattr(mcp, "gateway_context", lambda: pytest.fail("private call used generic authority"))
    binding = handoff.current_binding()
    result = SimpleNamespace(
        directory=issuer, state=state, cwd=cwd, sink=sink, signer=signer, binding=binding, transport=lambda: SharedIssuer(signer, issuer), children=[]
    )
    yield result
    for pid in result.children:
        try:
            os.kill(pid, signal.SIGTERM)
            os.waitpid(pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass
    try:
        next(sink_generator)
    except StopIteration:
        pass


def profile(fixture, *, operation="materialize", label="fixture", command=None, name=None):
    command = command or [
        str(Path(sys.executable).resolve()),
        "-c",
        "import os,hashlib; value=open(os.environ['ADP_CREDENTIAL_FILE'],'rb').read(); "
        "assert hashlib.sha256(value).hexdigest()==" + repr(__import__("hashlib").sha256(b"PRIVATE_CANARY").hexdigest()),
    ]
    args = SimpleNamespace(
        profile=name or operation + "-" + label,
        credential_id=str(uuid.uuid4()),
        service="test",
        label=label,
        operation=operation,
        cwd=str(fixture.cwd),
        use_seconds=900 if operation == "assume_role" else 10,
        command=command,
        purpose=None,
    )
    registered = host.register(args, fixture.transport())
    return host.load_profiles([args.profile], fixture.binding)[0], registered


def start(fixture, profiles):
    pid = os.fork()
    if pid == 0:
        try:
            signal.signal(signal.SIGTERM, signal.default_int_handler)
            host.Coordinator(profiles, fixture.binding, transport_factory=fixture.transport, no_browser=True).serve()
        except BaseException:
            pass
        finally:
            os._exit(0)
    fixture.children.append(pid)
    until = time.monotonic() + 5
    while time.monotonic() < until:
        try:
            response = handoff.rpc(fixture.binding, "connect")
            return pid, response["instance"]
        except handoff.UnavailableError:
            time.sleep(0.02)
    pytest.fail("coordinator did not start")


def invoke(name, arguments=None):
    return mcp.handle({"method": "tools/call", "params": {"name": name, "arguments": arguments or {}}})


def payload(result):
    text = json.dumps(result)
    assert "PRIVATE_CANARY" not in text and "CANARY_PRIVATE" not in text
    assert not {"ciphertext", "proof", "offer", "selection", "argv", "token", "signature"} & set(json.loads(result["content"][0]["text"]))
    return json.loads(result["content"][0]["text"])


def poll(request_id, predicate):
    until = time.monotonic() + 8
    last = None
    while time.monotonic() < until:
        last = payload(invoke("adp___operation_status", {"operation_ref": request_id}))
        if predicate(last):
            return last
        time.sleep(0.02)
    pytest.fail("operation did not reach expected metadata state: " + str(last))


def options(fixture, **value):
    common.write_json(fixture.directory / "options.json", value)


def calls(fixture, suffix):
    return [row for row in map(json.loads, (fixture.directory / "calls.jsonl").read_text().splitlines()) if row["path"].endswith(suffix)]


@pytest.mark.parametrize("operation", ["materialize", "assume_role"])
def test_real_mcp_ipc_issuer_host_child_and_metadata_recovery(fixture, operation):
    command = None
    if operation == "assume_role":
        command = [
            str(Path(sys.executable).resolve()),
            "-c",
            "import os,hashlib; assert hashlib.sha256(os.environ['AWS_ACCESS_KEY_ID'].encode()).hexdigest()=="
            + repr(__import__("hashlib").sha256(b"PRIVATE_CANARY").hexdigest())
            + "; assert 'AWS_PROFILE' not in os.environ",
        ]
    selected, _ = profile(fixture, operation=operation, command=command)
    start(fixture, [selected])
    first = payload(invoke("credentials___" + operation, {"service": "test"}))
    assert first["status"] == "pending", first
    request_id = first["operation_ref"]
    final = poll(request_id, lambda value: value.get("status") == "acknowledged")
    assert final["delivery_state"] == "completed" and final["exit_code"] == 0 and final["cleanup"] == "complete"
    assert final["acknowledgment_source"] == "client_reported"
    assert final["request_id"] == request_id
    assert len(calls(fixture, "/issue")) == 1
    retry = payload(invoke("adp___operation_retry", {"operation_ref": request_id}))
    assert all(retry[key] == final[key] for key in handoff.IDS)
    assert len(calls(fixture, "/issue")) == 1
    artifact = payload(invoke("adp___artifact_read", {"operation_ref": request_id}))
    assert artifact["code"] == "native_private_artifact_unavailable"


@pytest.mark.parametrize("decision", ["denied", "expired"])
def test_browser_url_denial_and_cleanup(fixture, decision):
    selected, _ = profile(fixture)
    options(fixture, decision="awaiting_approval")
    start(fixture, [selected])
    request_id = payload(invoke("credentials___materialize", {"service": "test", "label": "fixture"}))["request_id"]
    pending = poll(request_id, lambda result: "approval_url" in result)
    assert pending["approval_url"] == "https://browser.example.invalid/credentials/native/" + pending["receipt_id"]
    options(fixture, decision=decision)
    final = poll(request_id, lambda value: value.get("cleanup") == "complete")
    assert final["status"] == decision and not calls(fixture, "/issue")


def test_cancellation_retains_original_ids_and_cleanup(fixture):
    selected, _ = profile(fixture)
    options(fixture, decision="awaiting_approval")
    start(fixture, [selected])
    first = payload(invoke("credentials___materialize", {"service": "test"}))
    request_id = first["request_id"]
    pending = poll(request_id, lambda value: "approval_url" in value)
    closed = payload(invoke("adp___execution_close", {"operation_ref": request_id}))
    assert closed["code"] == "native_cancel_requested"
    final = poll(request_id, lambda value: value.get("cleanup") == "complete")
    assert all(final[key] == pending[key] for key in handoff.IDS)
    assert not calls(fixture, "/issue")


@pytest.mark.parametrize("failure", ["issue", "ack"])
def test_unknown_issuance_and_lost_ack_never_resubmit(fixture, failure):
    selected, _ = profile(fixture)
    options(fixture, fail=failure)
    start(fixture, [selected])
    first = payload(invoke("credentials___materialize", {"service": "test"}))
    final = poll(first["request_id"], lambda value: "cleanup" in value and (failure == "issue" or "acknowledgement" in value))
    assert final["cleanup"] == "complete"
    if failure == "issue":
        assert final["delivery_state"] == "unknown"
    else:
        assert final["delivery_state"] == "completed" and final["acknowledgement"] == "unknown"
    before = len(calls(fixture, "/issue"))
    payload(invoke("adp___operation_retry", {"operation_ref": first["request_id"]}))
    assert len(calls(fixture, "/issue")) == before == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("argv", ["/bin/sh"]),
        ("sink", "/tmp/leak"),
        ("credential_id", "foreign"),
        ("recipient", "foreign"),
        ("request_id", str(uuid.uuid4())),
        ("user_id", "foreign"),
        ("tenant_id", "foreign"),
        ("run_id", "foreign"),
        ("workload_id", "foreign"),
    ],
)
def test_model_cannot_select_recipient_or_authority(fixture, field, value):
    with pytest.raises(mcp.ToolError):
        invoke("credentials___materialize", {"service": "test", field: value})
    assert not calls(fixture, "/requests") if (fixture.directory / "calls.jsonl").exists() else True


@pytest.mark.parametrize("key", ["ADP_CREW_TOOLS_URL", "SIGV4_PROXY_TARGET", "ADP_RUN_CREDENTIAL_FILE", "ADP_RUN_CREDENTIAL"])
def test_worker_and_crew_contexts_never_access_native_host(fixture, monkeypatch, key):
    selected, _ = profile(fixture)
    start(fixture, [selected])
    monkeypatch.setenv(key, "untrusted")
    result = payload(invoke("credentials___materialize", {"service": "test"}))
    assert result["status"] == "unknown" and result["code"] == "native_private_host_required"
    assert not calls(fixture, "/requests")


@pytest.mark.parametrize("transport_file", ["", "/fixture/native-transport.json", "invalid-native-transport"])
@pytest.mark.parametrize("tool", sorted(handoff.PRIVATE_TOOLS))
def test_native_test_transport_never_uses_private_host_authority(fixture, monkeypatch, transport_file, tool):
    monkeypatch.setenv("ADP_NATIVE_TEST_TRANSPORT_FILE", transport_file)
    monkeypatch.setattr(common, "gateway_url", lambda: pytest.fail("native transport fell back to laptop gateway"))
    monkeypatch.setattr(common, "access_token", lambda: pytest.fail("native transport fell back to laptop login"))
    monkeypatch.setattr(handoff, "rpc", lambda *args, **kwargs: pytest.fail("native transport reached private host IPC"))
    # Discovery is supplied by the selected transport; private names must never
    # be added from another producer, even if a facade inventory includes them.
    monkeypatch.setattr(mcp, "gateway_request", lambda *_: {"tools": [{"name": name, "inputSchema": {}} for name in handoff.PRIVATE_TOOLS]})
    rows = mcp.handle({"method": "tools/list"})["tools"]
    assert not handoff.PRIVATE_TOOLS & {row["name"] for row in rows}
    monkeypatch.setattr(mcp, "gateway_request", lambda *args, **kwargs: pytest.fail("private operation used generic transport"))
    first = payload(invoke(tool, {"service": "test"}))
    assert first["status"] == "unknown" and first["code"] == "native_private_host_required"
    for action in ("adp___operation_retry", "adp___operation_status", "adp___execution_close"):
        result = payload(invoke(action, {"operation_ref": first["operation_ref"]}))
        assert result["status"] == "unknown" and result["code"] == "native_private_host_required"
    assert not (fixture.directory / "calls.jsonl").exists()


def test_discovery_filters_facade_and_only_advertises_ready_native_profiles(fixture, monkeypatch):
    monkeypatch.setattr(mcp, "gateway_request", lambda *_: {"tools": [{"name": name, "inputSchema": {}} for name in handoff.PRIVATE_TOOLS]})
    assert not handoff.PRIVATE_TOOLS & {row["name"] for row in mcp.handle({"method": "tools/list"})["tools"]}
    selected, _ = profile(fixture)
    start(fixture, [selected])
    rows = mcp.handle({"method": "tools/list"})["tools"]
    assert handoff.PRIVATE_TOOLS & {row["name"] for row in rows} == {"credentials___materialize"}
    descriptor = next(row for row in rows if row["name"] == "credentials___materialize")
    assert set(descriptor["inputSchema"]["properties"]) == {"service", "label"}
    options(fixture, auth_denied=True)
    assert not handoff.PRIVATE_TOOLS & {row["name"] for row in mcp.handle({"method": "tools/list"})["tools"]}


def test_executable_and_cwd_drift_refuse_before_intent(fixture):
    executable = fixture.cwd / "python"
    shutil.copy2(Path(sys.executable).resolve(), executable)
    selected, _ = profile(fixture, command=[str(executable), "-c", "pass"])
    start(fixture, [selected])
    executable.write_bytes(b"\x7fELFchanged")
    result = payload(invoke("credentials___materialize", {"service": "test"}))
    assert result["code"] == "native_profile_unavailable"
    assert not calls(fixture, "/requests")


def test_public_key_change_hides_readiness_but_preserves_metadata_recovery(fixture):
    selected, _ = profile(fixture)
    start(fixture, [selected])
    request_id = payload(invoke("credentials___materialize", {"service": "test"}))["request_id"]
    final = poll(request_id, lambda value: value.get("status") == "acknowledged")
    options(fixture, config={"key_id": "rotated-key"})
    with pytest.raises(handoff.UnavailableError, match="native_profile_unavailable"):
        handoff.ready()
    recovered = payload(invoke("adp___operation_retry", {"operation_ref": request_id}))
    assert all(recovered[key] == final[key] for key in handoff.IDS)
    assert recovered["cleanup"] == "complete" and len(calls(fixture, "/issue")) == 1


def test_request_replay_is_status_only_and_conflicting_payload_refuses(fixture):
    selected, _ = profile(fixture)
    _, instance = start(fixture, [selected])
    request_id = str(uuid.uuid4())
    handoff.rpc(fixture.binding, "submit", instance=instance, request_id=request_id, tool="credentials___materialize", arguments={"service": "test"})
    until = time.monotonic() + 5
    while time.monotonic() < until:
        result = handoff.rpc(
            fixture.binding, "submit", instance=instance, request_id=request_id, tool="credentials___materialize", arguments={"service": "test"}
        )["result"]
        if result.get("status") == "acknowledged":
            break
        time.sleep(0.02)
    assert result["status"] == "acknowledged"
    assert len(calls(fixture, "/requests")) == len(calls(fixture, "/issue")) == 1
    with pytest.raises(handoff.UnavailableError, match="native_request_conflict"):
        handoff.rpc(
            fixture.binding,
            "submit",
            instance=instance,
            request_id=request_id,
            tool="credentials___materialize",
            arguments={"service": "test", "label": "other"},
        )


def test_stale_instance_and_deployment_substitution_refuse(fixture):
    selected, _ = profile(fixture)
    _, instance = start(fixture, [selected])
    for binding, supplied in (("a" * 64, instance), (fixture.binding, str(uuid.uuid4()))):
        with pytest.raises(handoff.UnavailableError):
            handoff.rpc(
                binding, "submit", instance=supplied, request_id=str(uuid.uuid4()), tool="credentials___materialize", arguments={"service": "test"}
            )
    assert not calls(fixture, "/requests")


@pytest.mark.parametrize("change", [{"exp": 1}, {"sub": "other"}, {"token_use": "id"}, {"jti": ""}])
def test_changed_or_expired_native_login_cannot_recover(fixture, monkeypatch, change):
    selected, _ = profile(fixture)
    start(fixture, [selected])
    request_id = payload(invoke("credentials___materialize", {"service": "test"}))["request_id"]
    poll(request_id, lambda value: value.get("status") == "acknowledged")
    monkeypatch.setattr(common, "access_token", lambda: token(**change))
    result = payload(invoke("adp___operation_retry", {"operation_ref": request_id}))
    assert result["status"] == "unknown" and "receipt_id" not in result
    assert len(calls(fixture, "/issue")) == 1


def test_coordinator_crash_cancels_worker_and_stale_socket_restart_cannot_reissue(fixture):
    selected, _ = profile(fixture)
    options(fixture, decision="awaiting_approval")
    pid, _ = start(fixture, [selected])
    request_id = payload(invoke("credentials___materialize", {"service": "test"}))["request_id"]
    pending = poll(request_id, lambda value: "approval_url" in value)
    os.kill(pid, signal.SIGKILL)
    os.waitpid(pid, 0)
    fixture.children.remove(pid)
    until = time.monotonic() + 5
    while time.monotonic() < until:
        saved = common.read_state("native-credential-" + request_id)
        if saved.get("cleanup") == "complete":
            break
        time.sleep(0.02)
    assert saved["cleanup"] == "complete"
    start(fixture, [selected])
    result = payload(invoke("adp___operation_retry", {"operation_ref": request_id}))
    assert all(result[key] == pending[key] for key in handoff.IDS)
    assert not calls(fixture, "/issue") and len(calls(fixture, "/requests")) == 1


def test_socket_permission_symlink_and_cross_uid_refuse(fixture, monkeypatch):
    selected, _ = profile(fixture)
    start(fixture, [selected])
    path = fixture.sink / "host.sock"
    path.chmod(0o666)
    with pytest.raises(handoff.UnavailableError):
        handoff.ready()
    path.chmod(0o600)

    class OtherPeer:
        def getsockopt(self, *_):
            return struct.pack("3i", 123, os.getuid() + 1, os.getgid())

    with pytest.raises(handoff.UnavailableError):
        handoff.check_peer(OtherPeer())
    link = fixture.sink / "link.sock"
    link.symlink_to(path)
    monkeypatch.setattr(handoff, "socket_path", lambda _: link)
    with pytest.raises(handoff.UnavailableError):
        handoff.ready()
    link.unlink()


@pytest.mark.parametrize("wire", [b'{"action":"ready","action":"submit"}', b'{"value":NaN}', b"x" * (handoff.MAX_PACKET + 1)])
def test_duplicate_nonfinite_and_oversized_ipc_are_refused(wire):
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        left.send(wire)
        with pytest.raises(ValueError):
            handoff.receive(right)
    finally:
        left.close()
        right.close()


@pytest.mark.parametrize(
    "extra",
    [{"ciphertext": "PRIVATE_CANARY"}, {"argv": ["PRIVATE_CANARY"]}, {"token": "PRIVATE_CANARY"}, {"exit_code": True}, {"code": "PRIVATE_CANARY"}],
)
def test_unbounded_or_private_response_fields_never_reach_mcp(extra):
    request_id = str(uuid.uuid4())
    with pytest.raises(ValueError):
        handoff.project_result({"request_id": request_id, "status": "unknown", **extra}, request_id)


def test_installed_stdio_mcp_uses_real_local_handoff_and_recovers(fixture, monkeypatch, tmp_path):
    # Copy the actual installed sources; only the native-login helper and issuer
    # are offline fixtures. The fresh MCP process receives no monkeypatches.
    monkeypatch.setattr(handoff, "socket_path", DEFAULT_SOCKET_PATH)
    installed = tmp_path / "installed"
    installed.mkdir()
    for name in ("adp-mcp.py", "adp_common.py", "adp_deployments.py", "adp_native_test.py", "adp_native_credential_handoff.py"):
        shutil.copy2(CLI / name, installed / name)
    (installed / "bg-cognito-auth.sh").write_text("#!/bin/sh\nprintf '%s\\n' '" + token() + "'\n")
    home = tmp_path / "native-user-home"
    config = home / ".bedrock-gateway"
    config.mkdir(parents=True, mode=0o700)
    (config / "config.json").write_text(json.dumps({"gateway_url": "https://gateway.example.invalid/api"}))
    selected, _ = profile(fixture)
    pid, _ = start(fixture, [selected])

    def stdio(name, arguments):
        packet = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        result = subprocess.run(
            [sys.executable, str(installed / "adp-mcp.py")],
            input=json.dumps(packet) + "\n",
            text=True,
            capture_output=True,
            timeout=15,
            env={**os.environ, "HOME": str(home)},
        )
        assert result.returncode == 0, result.stderr
        assert "PRIVATE_CANARY" not in result.stdout + result.stderr
        message = json.loads(result.stdout)
        assert "error" not in message, message
        return payload(message["result"])

    try:
        first = stdio("credentials___materialize", {"service": "test"})
        assert first["status"] == "pending", first
        until = time.monotonic() + 8
        while time.monotonic() < until:
            final = stdio("adp___operation_status", {"operation_ref": first["operation_ref"]})
            if final.get("status") == "acknowledged":
                break
            time.sleep(0.02)
        assert final["delivery_state"] == "completed" and final["exit_code"] == 0 and final["cleanup"] == "complete"
        retry = stdio("adp___operation_retry", {"operation_ref": first["operation_ref"]})
        assert all(retry[key] == final[key] for key in handoff.IDS) and len(calls(fixture, "/issue")) == 1
    finally:
        os.kill(pid, signal.SIGTERM)
        os.waitpid(pid, 0)
        fixture.children.remove(pid)
        directory = DEFAULT_SOCKET_PATH(fixture.binding).parent
        (directory / "host.lock").unlink()
        directory.rmdir()


def test_dropped_ipc_submit_response_is_never_submission_permission(fixture):
    selected, _ = profile(fixture)
    _, instance = start(fixture, [selected])
    request_id = str(uuid.uuid4())
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as connection:
        connection.connect(str(handoff.socket_path(fixture.binding)))
        handoff.send(
            connection,
            {
                "version": handoff.VERSION,
                "binding": fixture.binding,
                "action": "submit",
                "instance": instance,
                "request_id": request_id,
                "tool": "credentials___materialize",
                "arguments": {"service": "test"},
            },
        )
        # Close without receiving the response. The coordinator must keep the
        # burned original lease, and recovery must only read that operation.
    until = time.monotonic() + 8
    while time.monotonic() < until:
        final = handoff.rpc(fixture.binding, "status", instance=instance, request_id=request_id)["result"]
        if final.get("status") == "acknowledged":
            break
        time.sleep(0.02)
    assert final["delivery_state"] == "completed" and final["cleanup"] == "complete"
    handoff.rpc(fixture.binding, "submit", instance=instance, request_id=request_id, tool="credentials___materialize", arguments={"service": "test"})
    assert len(calls(fixture, "/requests")) == len(calls(fixture, "/issue")) == 1


def test_concurrent_pending_jobs_are_bounded_and_individually_cancelled(fixture):
    selected, _ = profile(fixture)
    options(fixture, decision="awaiting_approval")
    start(fixture, [selected])
    requests = [payload(invoke("credentials___materialize", {"service": "test"}))["request_id"] for _ in range(host.MAX_JOBS)]
    for request_id in requests:
        poll(request_id, lambda value: "approval_url" in value)
    overflow = payload(invoke("credentials___materialize", {"service": "test"}))
    assert overflow["code"] == "native_host_busy"
    for request_id in requests:
        payload(invoke("adp___execution_close", {"operation_ref": request_id}))
        poll(request_id, lambda value: value.get("cleanup") == "complete")
    assert len(calls(fixture, "/requests")) == host.MAX_JOBS and not calls(fixture, "/issue")


def test_actual_sealed_command_mismatch_refuses_before_offer(fixture):
    selected, _ = profile(fixture)
    selected["command_digest"] = "a" * 64
    request_id = str(uuid.uuid4())
    result = host.run_job(selected, request_id, {"service": "test"}, fixture.transport(), no_browser=True)
    assert result["delivery_state"] == "unavailable"
    assert not calls(fixture, "/offer") and not calls(fixture, "/issue")
    assert not list(fixture.sink.iterdir())


def test_cwd_inode_replacement_refuses_even_when_path_unchanged(fixture):
    selected, _ = profile(fixture)
    start(fixture, [selected])
    fixture.cwd.rename(fixture.cwd.with_name("old-cwd"))
    fixture.cwd.mkdir(mode=0o700)
    result = payload(invoke("credentials___materialize", {"service": "test"}))
    assert result["code"] == "native_profile_unavailable" and not calls(fixture, "/requests")


def test_same_human_renewed_session_can_read_but_not_reissue(fixture, monkeypatch):
    selected, _ = profile(fixture)
    start(fixture, [selected])
    request_id = payload(invoke("credentials___materialize", {"service": "test"}))["request_id"]
    final = poll(request_id, lambda value: value.get("status") == "acknowledged")
    monkeypatch.setattr(common, "access_token", lambda: token(jti="renewed-session"))
    recovered = payload(invoke("adp___operation_retry", {"operation_ref": request_id}))
    assert all(recovered[key] == final[key] for key in handoff.IDS) and len(calls(fixture, "/issue")) == 1
    options(fixture, auth_denied=True)
    denied = payload(invoke("adp___operation_status", {"operation_ref": request_id}))
    assert denied["status"] == "unknown" and "receipt_id" not in denied and "cleanup" not in denied


def test_coordinator_lease_and_profile_file_cannot_be_overwritten(fixture):
    selected, _ = profile(fixture)
    with pytest.raises(FileExistsError):
        profile(fixture)
    start(fixture, [selected])
    second = host.Coordinator([selected], fixture.binding, transport_factory=fixture.transport)
    try:
        with pytest.raises(BlockingIOError):
            second.bind()
    finally:
        second.close()
    assert handoff.ready()["result"]["state"] == "ready"


@pytest.mark.parametrize("action", ["host-register", "host-serve"])
def test_installed_native_dispatch_help_does_not_require_login(tmp_path, monkeypatch, action):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    loader_path = str(runtime / "lib") + os.pathsep + os.environ.get("LD_LIBRARY_PATH", "")
    python = runtime / "python3"
    python.write_text(f'#!/bin/sh\ntest "$LD_LIBRARY_PATH" = {shlex.quote(loader_path)} || exit 91\nexec {shlex.quote(sys.executable)} "$@"\n')
    python.chmod(0o700)
    monkeypatch.setenv("PATH", str(runtime) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("LD_LIBRARY_PATH", loader_path)
    config = tmp_path / ".bedrock-gateway"
    config.mkdir(mode=0o700)
    (config / "config.json").write_text(json.dumps({"gateway_url": "https://gateway.example.invalid/api"}))
    result = subprocess.run(
        ["bash", str(CLI / "adp"), "credential", action, "--help"],
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(tmp_path)},
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "--profile" in result.stdout


@pytest.mark.parametrize("changes", [{"exit_code": 2}, {"cleanup": "unknown"}, {"acknowledgement": "unknown"}, {"delivery_state": "unknown"}])
def test_failed_child_or_unconfirmed_delivery_is_not_mcp_success(changes):
    request_id = str(uuid.uuid4())
    result = mcp.private_result(
        {"request_id": request_id},
        {
            "request_id": request_id,
            "status": "acknowledged",
            "delivery_state": "completed",
            "exit_code": 0,
            "cleanup": "complete",
            "acknowledgement": "confirmed",
            **changes,
        },
    )
    assert result["isError"] is True


def test_profileless_restart_recovers_original_metadata_without_issuing(fixture):
    selected, _ = profile(fixture)
    pid, _ = start(fixture, [selected])
    request_id = payload(invoke("credentials___materialize", {"service": "test"}))["request_id"]
    final = poll(request_id, lambda value: value.get("status") == "acknowledged")
    os.kill(pid, signal.SIGTERM)
    os.waitpid(pid, 0)
    fixture.children.remove(pid)
    start(fixture, [])
    with pytest.raises(handoff.UnavailableError, match="native_profile_unavailable"):
        handoff.ready()
    recovered = payload(invoke("adp___operation_retry", {"operation_ref": request_id}))
    assert all(recovered[key] == final[key] for key in handoff.IDS) and recovered["cleanup"] == "complete"
    assert len(calls(fixture, "/issue")) == 1


def test_learned_original_ids_cannot_be_replaced_by_handoff(fixture, monkeypatch):
    request_id = str(uuid.uuid4())
    original = {
        "request_id": request_id,
        "execution_id": str(uuid.uuid4()),
        "receipt_id": str(uuid.uuid4()),
        "tool": "credentials___materialize",
        "arguments": {"service": "test"},
        "binding": {"mode": "native-private", "binding": fixture.binding},
    }
    mcp.store_operation(original, create=True)

    def rpc(_binding, action, **_):
        if action == "connect":
            return {"instance": str(uuid.uuid4())}
        return {"result": {"request_id": request_id, "execution_id": str(uuid.uuid4()), "receipt_id": str(uuid.uuid4()), "status": "acknowledged"}}

    monkeypatch.setattr(handoff, "rpc", rpc)
    result = payload(invoke("adp___operation_status", {"operation_ref": request_id}))
    assert result["code"] == "native_request_conflict" and "receipt_id" not in result
    saved = mcp.load_operation(request_id)
    assert all(saved[key] == original[key] for key in handoff.IDS)
