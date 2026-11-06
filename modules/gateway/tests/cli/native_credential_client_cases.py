"""Real native CLI-to-private-consumer flow with offline issuer and browser decisions."""

import copy
import hashlib
import importlib.util
import io
import json
import os
import select
import signal
import sys
import time
import urllib.error
import uuid
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization

CLI = Path(__file__).parents[2] / "cli"
sys.path.insert(0, str(CLI))
spec = importlib.util.spec_from_file_location("native_cli", CLI / "adp-native-credentials.py")
client_cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client_cli)
spec = importlib.util.spec_from_file_location("native_cases", Path(__file__).with_name("native_private_credentials_cases.py"))
cases = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cases)


@pytest.fixture
def root():
    yield from cases.root.__wrapped__()


@pytest.fixture
def signer():
    return cases.issuer.__wrapped__()


@pytest.fixture
def args(tmp_path):
    return client_cli.parser().parse_args(
        [
            "use",
            "--credential-id",
            str(uuid.uuid4()),
            "--service",
            "test",
            "--label",
            "fixture",
            "--operation",
            "materialize",
            "--use-seconds",
            "10",
            "--cwd",
            str(tmp_path),
            "--request-id",
            str(uuid.uuid4()),
            "--no-browser",
            "--",
            str(Path(sys.executable).resolve()),
            "-c",
            "import os,hashlib; value=open(os.environ['ADP_CREDENTIAL_FILE'],'rb').read(); "
            + "assert hashlib.sha256(value).hexdigest()=="
            + repr(hashlib.sha256(b"PRIVATE_CANARY").hexdigest())
            + "; print(value)",
        ]
    )


class OfflineIssuer:
    def __init__(self, signer, *, decision="approved", fail=None):
        self.signer = signer
        self.calls = []
        self.decision = decision
        self.fail = fail
        self.original = None
        self.offer = None
        self.challenge = cases.host.b64(os.urandom(32))
        self.config = {
            "version": cases.host.VERSION,
            "issuer": "https://native.example.invalid",
            "audience": "adp-native-private-host",
            "key_id": "offline-key",
            "approval_origin": "https://browser.example.invalid",
            "issuer_key_pem": signer.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode(),
        }

    def request(self, method, path, body=None):
        # Record ONLY method/path, never the private body.
        self.calls.append((method, path))
        if self.fail == path.rsplit("/", 1)[-1]:
            raise client_cli.common.CliError("private body PRIVATE_CANARY must not reach output", "unknown_mutation_outcome")
        if path.endswith("/config"):
            return self.config
        if path.endswith("/requests"):
            arguments = {"service": body["service"], "label": body["label"]}
            if body["operation"] == "assume_role":
                arguments["purpose"] = body.get("purpose", "")
            selection = {
                **{key: body[key] for key in ("request_id", "credential_id", "service", "label", "operation", "argv", "cwd", "use_seconds")},
                **{key: self.config[key] for key in ("issuer", "audience", "key_id")},
                "user_id": "canonical-user",
                "tenant_id": "canonical-tenant",
                "session_id": "canonical-jti-session",
                "execution_id": str(uuid.uuid4()),
                "receipt_id": str(uuid.uuid4()),
                "policy_version": "policy-version",
                "argument_digest": cases.host._digest(arguments),
            }
            self.original = {
                **{key: selection[key] for key in client_cli.IDS},
                "selection": selection,
                "status": "created",
                "expires_at": int(time.time()) + 90,
                "snapshot": "",
            }
            return copy.deepcopy(self.original)
        if method == "GET":
            return {**copy.deepcopy(self.original), "status": self.decision, "snapshot": "a" * 64, "unsafe_extra": "PRIVATE_CANARY"}
        if path.endswith("/offer"):
            self.offer = body["offer"]
            assert self.offer["selection"] == self.original["selection"]
            return {
                "receipt_id": self.original["receipt_id"],
                "status": "awaiting_approval",
                "snapshot": "a" * 64,
                "expires_at": self.original["expires_at"],
            }
        if path.endswith("/challenge"):
            return {"receipt_id": self.original["receipt_id"], "challenge": self.challenge, "expires_at": int(time.time()) + 30}
        if path.endswith("/issue"):
            expected_challenge = self.challenge

            class ExistingProof:
                offer = self.offer

                def prove(self, challenge):
                    assert challenge == expected_challenge
                    return body

            payload = None
            if self.original["selection"]["operation"] == "assume_role":
                payload = {"access_key_id": "PRIVATE_CANARY", "secret_access_key": "fake-secret", "session_token": "fake-token"}
            return cases.envelope(ExistingProof(), self.signer, payload, challenge=self.challenge)
        if path.endswith("/ack"):
            assert "PRIVATE_CANARY" not in str(body)
            assert all(body[key] == self.original[key] for key in client_cli.IDS)
            return {"receipt_id": self.original["receipt_id"], "status": "acknowledged"}
        raise AssertionError(path)


@pytest.fixture(autouse=True)
def local_private_sink(root, monkeypatch):
    monkeypatch.setattr(client_cli, "volatile_root", lambda: str(root))


def test_real_file_delivery_and_receipt_have_no_secret(args, signer, root, capfd):
    issuer = OfflineIssuer(signer)
    outcome = client_cli.use_credential(args, issuer)
    assert outcome["delivery_state"] == "completed", outcome
    assert outcome["exit_code"] == 0
    assert outcome["acknowledgement"] == "confirmed"
    assert outcome["cleanup"] == "complete"
    assert list(root.iterdir()) == []
    record = client_cli.common.read_state("native-credential-" + args.request_id)
    assert "PRIVATE_CANARY" not in json.dumps(record)
    assert not {"offer", "proof", "signature", "ciphertext", "argv", "selection", "token"} & record.keys()
    stdout, stderr = capfd.readouterr()
    assert "PRIVATE_CANARY" not in stdout + stderr
    assert "/credentials/native/" in stderr
    assert sum(path.endswith("/issue") for _, path in issuer.calls) == 1
    assert not any("/approvals/" in path for _, path in issuer.calls)


def test_real_sts_delivery_keeps_original_ids_and_no_profile(args, signer, tmp_path):
    args.operation, args.use_seconds = "assume_role", 900
    args.command[-1] = (
        "import os,hashlib; assert hashlib.sha256(os.environ['AWS_ACCESS_KEY_ID'].encode()).hexdigest()=="
        + repr(hashlib.sha256(b"PRIVATE_CANARY").hexdigest())
        + "; assert 'AWS_PROFILE' not in os.environ"
    )
    issuer = OfflineIssuer(signer)
    outcome = client_cli.use_credential(args, issuer)
    assert outcome["delivery_state"] == "completed", outcome
    assert outcome["exit_code"] == 0
    assert all(outcome[key] == issuer.original[key] for key in client_cli.IDS)
    assert not (tmp_path / ".aws").exists()


def test_browser_denial_never_challenges_or_issues(args, signer, root):
    issuer = OfflineIssuer(signer, decision="denied")
    result = client_cli.use_credential(args, issuer)
    assert result["status"] == "denied"
    assert not any(path.endswith(("/issue", "/challenge")) for _, path in issuer.calls)
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("lost", ["requests", "offer", "challenge", "issue", "ack"])
def test_lost_ack_never_reissues_or_loses_original_request(args, signer, root, lost):
    issuer = OfflineIssuer(signer, fail=lost)
    result = client_cli.use_credential(args, issuer)
    assert result["request_id"] == args.request_id
    assert "PRIVATE_CANARY" not in str(result)
    assert sum(path.endswith("/issue") for _, path in issuer.calls) <= 1
    if lost == "issue":
        assert result["delivery_state"] == "unknown"
        assert result["receipt_id"] == issuer.original["receipt_id"]
    if lost == "ack":
        assert result["delivery_state"] == "completed"
        assert result["acknowledgement"] == "unknown"
    assert list(root.iterdir()) == []


def test_existing_request_is_metadata_only_no_new_recipient(args, signer):
    issuer = OfflineIssuer(signer)
    first = client_cli.use_credential(args, issuer)
    before = len(issuer.calls)
    second = client_cli.use_credential(args, issuer)
    assert len(issuer.calls) == before + 1
    assert issuer.calls[-1] == ("GET", client_cli.PREFIX + "/requests/by-request/" + args.request_id)
    assert all(first[key] == second[key] for key in client_cli.IDS)
    assert second["code"] == "existing_request_requires_status_read"


@pytest.mark.parametrize(
    "origin",
    [
        "http://unsafe.invalid",
        "https://unsafe.invalid/path",
        "https://user@unsafe.invalid",
        "https://unsafe.invalid?token=bad",
        "https://unsafe.invalid/#bad",
    ],
)
def test_untrusted_approval_location_refused_before_request(args, signer, origin):
    issuer = OfflineIssuer(signer)
    issuer.config["approval_origin"] = origin
    result = client_cli.use_credential(args, issuer)
    assert result["delivery_state"] == "unavailable"
    assert issuer.calls == [("GET", client_cli.PREFIX + "/config")]


def test_expired_selection_is_not_issued(args, signer):
    issuer = OfflineIssuer(signer, decision="expired")
    outcome = client_cli.use_credential(args, issuer)
    assert outcome["status"] == "expired"
    assert not any(path.endswith("/issue") for _, path in issuer.calls)


def test_pinned_native_transport_never_uses_worker_credentials(monkeypatch):
    monkeypatch.setattr(client_cli.common, "gateway_url", lambda: "https://pinned.invalid/api")
    tokens = []

    def token():
        tokens.append(1)
        return "native-login-token"

    monkeypatch.setattr(client_cli.common, "access_token", token)
    monkeypatch.setenv("ADP_RUN_CREDENTIAL", "worker-must-not-be-used")
    sent = []

    class Opener:
        def open(self, request, timeout):
            sent.append(request)
            return io.BytesIO(b'{"status":"ready"}')

    transport = client_cli.NativeTransport()
    transport.opener = Opener()
    for _ in range(2):
        assert transport.request("GET", client_cli.PREFIX + "/config") == {"status": "ready"}
    assert len(tokens) == 1
    assert all(request.headers["Authorization"] == "Bearer native-login-token" for request in sent)
    assert all(request.full_url.startswith("https://pinned.invalid/api/") for request in sent)


def test_transport_error_body_is_not_read_or_exported(monkeypatch):
    monkeypatch.setattr(client_cli.common, "gateway_url", lambda: "https://pinned.invalid/api")
    monkeypatch.setattr(client_cli.common, "access_token", lambda: "native-token")

    class PoisonBody:
        def read(self, *_):
            raise AssertionError("private error body was read")

        def close(self):
            pass

    class Opener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(request.full_url, 403, "PRIVATE_CANARY", {}, PoisonBody())

    transport = client_cli.NativeTransport()
    transport.opener = Opener()
    with pytest.raises(client_cli.common.CliError) as error:
        transport.request("POST", client_cli.PREFIX + "/requests/x/issue", {})
    assert "PRIVATE_CANARY" not in str(error.value)
    assert error.value.code == "permission_denied"


def test_https_mandatory_before_native_token_read(monkeypatch):
    monkeypatch.setattr(client_cli.common, "gateway_url", lambda: "http://localhost/api")
    monkeypatch.setattr(client_cli.common, "access_token", lambda: pytest.fail("token read before transport validation"))
    with pytest.raises(client_cli.common.CliError, match="HTTPS"):
        client_cli.NativeTransport()


def test_runtime_setup_uses_isolated_hashed_wheels_without_gateway_calls(monkeypatch, tmp_path):
    import subprocess

    calls = []
    runtime_root = tmp_path / "runtime-root"
    runtime_root.mkdir()
    monkeypatch.setattr(client_cli, "runtime_dir", lambda: runtime_root / "runtime")

    def run(command, **options):
        calls.append(command)
        assert options.get("capture_output") is True
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(client_cli.subprocess, "run", run)
    assert client_cli.setup_runtime() == {"runtime": "ready", "cryptography": "50.0.0"}
    installation = next(command for command in calls if "pip" in command)
    assert "--isolated" in installation and "--require-hashes" in installation
    assert "--only-binary=:all:" in installation
    requirements = Path(installation[-1]).read_text()
    assert "cryptography==50.0.0" in requirements and "--hash=sha256:" in requirements
    assert sorted(path.name for path in runtime_root.iterdir()) == ["runtime"]


def test_native_entry_help_routes_without_authentication(tmp_path):
    import subprocess

    config = tmp_path / ".bedrock-gateway"
    config.mkdir(mode=0o700)
    (config / "config.json").write_text(json.dumps({"gateway_url": "https://gateway.example.invalid/api"}))
    result = subprocess.run(
        ["bash", str(CLI / "adp"), "credential", "use", "--help"],
        env={**os.environ, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "--credential-id" in result.stdout and "--operation" in result.stdout


def test_offer_network_delay_cannot_extend_prepared_host_lifetime(args, signer, monkeypatch, root):
    real_monotonic = time.monotonic
    offset = [0]
    monkeypatch.setattr(client_cli.time, "monotonic", lambda: real_monotonic() + offset[0])

    class SlowOffer(OfflineIssuer):
        def request(self, method, path, body=None):
            result = super().request(method, path, body)
            if path.endswith("/offer"):
                offset[0] = 100
            return result

    issuer = SlowOffer(signer)
    outcome = client_cli.use_credential(args, issuer)
    assert outcome["status"] == "expired"
    assert not any(path.endswith(("/challenge", "/issue")) for _, path in issuer.calls)
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("failure", ["offer", "challenge", "issue"])
def test_aborted_private_flow_retains_cleanup_without_upgrading_unknown(args, signer, root, failure):
    issuer = OfflineIssuer(signer, fail=failure)
    result = client_cli.use_credential(args, issuer)
    assert result["cleanup"] == "complete"
    assert all(result[key] == issuer.original[key] for key in client_cli.IDS)
    if failure == "issue":
        assert result["delivery_state"] == "unknown"
        assert result["status"] == "unknown"
    saved = client_cli.common.read_state("native-credential-" + args.request_id)
    assert saved["cleanup"] == "complete"
    assert saved["delivery_state"] == result["delivery_state"]
    assert list(root.iterdir()) == []


def test_browser_denial_keeps_local_cleanup_observation(args, signer):
    issuer = OfflineIssuer(signer, decision="denied")
    result = client_cli.use_credential(args, issuer)
    assert result["status"] == "denied"
    assert result["cleanup"] == "complete"
    saved = client_cli.common.read_state("native-credential-" + args.request_id)
    assert saved["cleanup"] == "complete"
    assert saved["status"] == "denied"


def terminal(original, **changes):
    return {
        "type": "result",
        **{key: original[key] for key in client_cli.IDS},
        "delivery_state": "completed",
        "code": "child_exited",
        "cleanup": "complete",
        "exit_code": 0,
        **changes,
    }


def test_status_exposes_validated_ack_and_preserves_original_local_observations(args, signer):
    issuer = OfflineIssuer(signer)
    outcome = client_cli.use_credential(args, issuer)
    assert outcome["delivery_state"] == "completed"
    # An older metadata-only status response cannot erase observed local cleanup.
    status = client_cli.metadata({**issuer.original, "status": "delivered"}, args.request_id)
    saved = client_cli.save_reference(args.request_id, status)
    assert saved["cleanup"] == "complete" and saved["exit_code"] == 0
    ack = terminal(issuer.original)
    status = client_cli.metadata(
        {**issuer.original, "status": "acknowledged", "acknowledgment": ack, "acknowledgment_source": "client_reported"}, args.request_id
    )
    assert status["acknowledgment"] == ack
    assert status["delivery_state"] == "completed" and status["exit_code"] == 0
    assert status["acknowledgement"] == "confirmed"
    client_cli.save_reference(args.request_id, status)
    saved = client_cli.common.read_state("native-credential-" + args.request_id)
    assert saved["acknowledgment"] == ack


def test_unknown_issuer_status_remains_unknown_even_with_complete_cleanup(args, signer):
    issuer = OfflineIssuer(signer)
    client_cli.use_credential(args, issuer)
    status = client_cli.metadata(
        {**issuer.original, "status": "unknown", "acknowledgment": terminal(issuer.original), "acknowledgment_source": "client_reported"},
        args.request_id,
    )
    assert status["delivery_state"] == "unknown"
    assert status["cleanup"] == "complete"
    assert status["acknowledgement"] == "unknown"
    saved = client_cli.save_reference(args.request_id, status)
    assert saved["delivery_state"] == "unknown"
    assert saved["acknowledgment"]["delivery_state"] == "completed"


@pytest.mark.parametrize(
    "change",
    [
        {"receipt_id": str(uuid.uuid4())},
        {"credential": "PRIVATE_CANARY"},
        {"code": "PRIVATE_CANARY"},
        {"exit_code": True},
        {"exit_code": 999},
        {"cleanup": []},
    ],
)
def test_terminal_status_never_exports_foreign_or_unbounded_ack(args, signer, change):
    issuer = OfflineIssuer(signer)
    request = client_cli.request_body(args, args.request_id)
    original = issuer.request("POST", client_cli.PREFIX + "/requests", request)
    with pytest.raises(ValueError):
        client_cli.metadata(
            {**original, "status": "acknowledged", "acknowledgment": terminal(original, **change), "acknowledgment_source": "client_reported"},
            args.request_id,
        )


@pytest.mark.parametrize("source", [None, "provider_verified", "", []])
def test_acknowledgment_requires_client_reported_qualifier(args, signer, source):
    issuer = OfflineIssuer(signer)
    original = issuer.request("POST", client_cli.PREFIX + "/requests", client_cli.request_body(args, args.request_id))
    value = {**original, "status": "acknowledged", "acknowledgment": terminal(original)}
    if source is not None:
        value["acknowledgment_source"] = source
    with pytest.raises(ValueError):
        client_cli.metadata(value, args.request_id)
    with pytest.raises(ValueError):
        client_cli.save_reference(args.request_id, value)


def test_acknowledgment_source_cannot_appear_without_ack(args, signer):
    issuer = OfflineIssuer(signer)
    original = issuer.request("POST", client_cli.PREFIX + "/requests", client_cli.request_body(args, args.request_id))
    value = {**original, "acknowledgment_source": "client_reported"}
    with pytest.raises(ValueError):
        client_cli.metadata(value, args.request_id)


@pytest.mark.parametrize("first", ["status", "terminal"])
def test_concurrent_status_and_terminal_writers_preserve_cleanup(args, signer, first):
    issuer = OfflineIssuer(signer)
    original = issuer.request("POST", client_cli.PREFIX + "/requests", client_cli.request_body(args, args.request_id))
    client_cli.save_reference(args.request_id, client_cli.metadata(original, args.request_id))
    values = {
        "status": client_cli.metadata({**original, "status": "delivered"}, args.request_id),
        "terminal": {**terminal(original), "status": "acknowledged", "acknowledgement": "confirmed"},
    }
    ready_read, ready_write = os.pipe()
    release_read, release_write = os.pipe()
    done_read, done_write = os.pipe()
    children = []

    def spawn(role, pause):
        pid = os.fork()
        if pid == 0:
            try:
                if pause:
                    read_state = client_cli.common.read_state

                    def paused_read(name):
                        snapshot = read_state(name)
                        os.write(ready_write, b"r")
                        os.read(release_read, 1)
                        return snapshot

                    client_cli.common.read_state = paused_read
                client_cli.save_reference(args.request_id, values[role])
                os.write(done_write, b"d")
                os._exit(0)
            except BaseException:
                os._exit(1)
        children.append(pid)

    try:
        spawn(first, True)
        assert select.select([ready_read], [], [], 3)[0]
        assert os.read(ready_read, 1) == b"r"
        spawn("terminal" if first == "status" else "status", False)
        # The competing writer cannot complete from an obsolete snapshot while
        # the first writer is paused between read and atomic replacement.
        assert not select.select([done_read], [], [], 0.15)[0]
    finally:
        os.write(release_write, b"x")
        statuses = [os.waitpid(pid, 0)[1] for pid in children]
        for fd in (ready_read, ready_write, release_read, release_write, done_read, done_write):
            os.close(fd)
    assert statuses == [0, 0]
    saved = client_cli.common.read_state("native-credential-" + args.request_id)
    assert saved["cleanup"] == "complete" and saved["exit_code"] == 0 and saved["delivery_state"] == "completed"
    assert all(saved[key] == original[key] for key in client_cli.IDS)


def test_killed_metadata_writer_releases_lock_for_original_recovery(args):
    ready_read, ready_write = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            with client_cli.reference_lock(args.request_id):
                os.write(ready_write, b"r")
                signal.pause()
        finally:
            os._exit(1)
    try:
        assert select.select([ready_read], [], [], 3)[0]
        assert os.read(ready_read, 1) == b"r"
    finally:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        os.close(ready_read)
        os.close(ready_write)
    assert client_cli.reserve_reference(args.request_id)
    saved = client_cli.save_reference(args.request_id, {"request_id": args.request_id, "status": "unknown", "cleanup": "unknown"})
    assert saved["request_id"] == args.request_id and saved["delivery_state"] == "unknown"


@pytest.mark.parametrize("unsafe", ["symlink", "mode", "hardlink"])
def test_metadata_lock_refuses_unsafe_file(args, tmp_path, unsafe):
    path = client_cli.common.state_path("native-credential-" + args.request_id).with_suffix(".lock")
    target = tmp_path / "lock-target"
    target.touch(mode=0o600)
    if unsafe == "symlink":
        path.symlink_to(target)
    elif unsafe == "hardlink":
        os.link(target, path)
    else:
        path.touch(mode=0o644)
        path.chmod(0o644)
    with pytest.raises((OSError, ValueError)):
        client_cli.save_reference(args.request_id, {"request_id": args.request_id, "status": "unknown"})
    assert target.read_bytes() == b""
    assert client_cli.common.read_state("native-credential-" + args.request_id) == {}
