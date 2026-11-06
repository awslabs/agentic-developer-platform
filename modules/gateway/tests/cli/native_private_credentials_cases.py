"""Real private process/crypto/volatile-sink tests; no network or credentials."""

import copy
import importlib.util
import os
import shutil
import signal
import sys
import tempfile
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa, x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MODULE = Path(__file__).parents[2] / "cli" / "adp_private_credentials.py"
spec = importlib.util.spec_from_file_location("adp_private_credentials", MODULE)
host = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = host
spec.loader.exec_module(host)


@pytest.fixture(scope="module")
def issuer():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def root():
    path = Path(tempfile.mkdtemp(prefix="adp-native-test-", dir="/dev/shm"))
    yield path
    shutil.rmtree(path)


@pytest.fixture
def selection(tmp_path):
    recipient_python = Path("/usr/bin/python3.12")
    if not recipient_python.is_file():
        pytest.fail("Native recipient tests require distro /usr/bin/python3.12, independent of setup-python loader variables.")
    return {
        "issuer": "https://issuer.example.invalid",
        "audience": "adp-native-private-host",
        "key_id": "test-rsa-key",
        "tenant_id": "tenant-real-human",
        "user_id": "canonical-human",
        "session_id": "canonical-native-session",
        "execution_id": "original-execution",
        "request_id": "original-request",
        "receipt_id": "original-receipt",
        "policy_version": "policy-7",
        "argument_digest": "a" * 64,
        "credential_id": "selected-credential",
        "service": "example",
        "label": "test-fixture",
        "operation": "materialize",
        "argv": [str(recipient_python), "-c", "pass"],
        "cwd": str(tmp_path),
        "use_seconds": 10,
    }


def prepare(selection, root, issuer):
    pem = issuer.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    return host.PrivateHost.prepare(selection, issuer_key_pem=pem, volatile_root=str(root))


def envelope(client, issuer, payload=None, *, mutate=None, challenge=None):
    challenge = challenge or host.b64(os.urandom(32))
    proof = client.prove(challenge)
    assert proof["proof"] == {"version": host.VERSION, "offer": client.offer, "challenge": challenge}
    ed25519.Ed25519PublicKey.from_public_bytes(host.unb64(client.offer["proof_public_key"])).verify(
        host.unb64(proof["signature"]), host.canonical(proof["proof"])
    )
    private = x25519.X25519PrivateKey.generate()
    now = int(time.time())
    body = {
        "version": host.VERSION,
        **{key: client.offer["selection"][key] for key in ("issuer", "audience", "key_id")},
        "offer": copy.deepcopy(client.offer),
        "challenge": challenge,
        "issued_at": now,
        "delivery_expires_at": now + 30,
        "credential_expires_at": now + client.offer["selection"]["use_seconds"],
        "ephemeral_public_key": host.b64(private.public_key().public_bytes_raw()),
        "iv": host.b64(os.urandom(12)),
    }
    if mutate:
        mutate(body)
    aad = host.canonical(body)
    shared = private.exchange(x25519.X25519PublicKey.from_public_bytes(host.unb64(client.offer["encryption_public_key"])))
    import hashlib

    key = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=host.unb64(client.offer["nonce"]), info=host.VERSION.encode() + hashlib.sha256(aad).digest()
    ).derive(shared)
    body["ciphertext"] = host.b64(
        AESGCM(key).encrypt(host.unb64(body["iv"]), host.canonical(payload or {"file_base64": host.b64(b"PRIVATE_CANARY")}), aad)
    )
    return {
        "body": body,
        "signature": host.b64(issuer.sign(host.canonical(body), padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())),
    }


def consume(client, value):
    first = client.start(value)
    return client.wait() if first["type"] == "started" else first


def assert_clean(result, root):
    assert result["cleanup"] == "complete", result
    assert list(root.iterdir()) == []
    assert result["execution_id"] == "original-execution"
    assert result["request_id"] == "original-request"
    assert result["receipt_id"] == "original-receipt"
    assert "PRIVATE_CANARY" not in str(result)


def test_file_consumption_is_unlinked_readonly_private_and_output_is_discarded(selection, root, issuer, capfd):
    selection["argv"][2] = """
import os,stat
p=os.environ['ADP_CREDENTIAL_FILE']
s=os.stat(p)
assert stat.S_IMODE(s.st_mode)==0o600 and s.st_nlink==0
assert open(p,'rb').read()==b'PRIVATE_CANARY'
try: os.write(int(p.rsplit('/',1)[-1]),b'bad')
except OSError: pass
else: raise AssertionError('writable recipient descriptor')
print('PRIVATE_CANARY',flush=True)
os.write(2,b'PRIVATE_CANARY')
"""
    with prepare(selection, root, issuer) as client:
        result = consume(client, envelope(client, issuer))
    assert result["exit_code"] == 0
    assert_clean(result, root)
    captured = capfd.readouterr()
    assert "PRIVATE_CANARY" not in captured.out + captured.err


def test_sts_environment_only_child_and_no_profiles(selection, root, issuer, monkeypatch, tmp_path):
    selection.update(operation="assume_role", use_seconds=900)
    monkeypatch.setenv("AWS_PROFILE", "ambient-profile")
    monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", "ambient-token")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ambient-key")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/untrusted/loader")
    monkeypatch.setenv("LD_PRELOAD", "/untrusted/preload.so")
    monkeypatch.setenv("PYTHONPATH", "/untrusted/python")
    monkeypatch.setenv("PYTHONHOME", "/untrusted/home")
    selection["argv"][2] = """
import os,ctypes,hashlib
assert os.environ['AWS_ACCESS_KEY_ID']=='PRIVATE_CANARY'
assert os.environ['AWS_SECRET_ACCESS_KEY']=='fake-secret'
assert os.environ['AWS_SESSION_TOKEN']=='fake-token'
assert os.environ['AWS_SHARED_CREDENTIALS_FILE']=='/dev/null'
assert 'AWS_PROFILE' not in os.environ
assert 'AWS_WEB_IDENTITY_TOKEN_FILE' not in os.environ
assert 'LD_LIBRARY_PATH' not in os.environ
assert 'LD_PRELOAD' not in os.environ
assert 'PYTHONPATH' not in os.environ
assert 'PYTHONHOME' not in os.environ
"""
    with prepare(selection, root, issuer) as client:
        result = consume(
            client, envelope(client, issuer, {"access_key_id": "PRIVATE_CANARY", "secret_access_key": "fake-secret", "session_token": "fake-token"})
        )
    assert result["exit_code"] == 0
    assert_clean(result, root)
    assert os.environ["AWS_ACCESS_KEY_ID"] == "ambient-key"
    assert not (tmp_path / ".aws").exists()


@pytest.mark.parametrize(
    "field",
    [
        "tenant_id",
        "user_id",
        "session_id",
        "receipt_id",
        "request_id",
        "execution_id",
        "argument_digest",
        "policy_version",
        "credential_id",
        "argv",
        "cwd",
        "operation",
    ],
)
def test_signed_binding_substitution_refused(selection, root, issuer, field):
    def mutate(body):
        body["offer"]["selection"][field] = ["/bin/false"] if field == "argv" else "substitution"

    with prepare(selection, root, issuer) as client:
        result = consume(client, envelope(client, issuer, mutate=mutate))
    assert result["code"] == "delivery_rejected"
    assert_clean(result, root)


@pytest.mark.parametrize("field", ["nonce", "recipient_fingerprint", "encryption_public_key", "proof_public_key", "command_digest", "sink_digest"])
def test_signed_recipient_and_sink_substitution_refused(selection, root, issuer, field):
    with prepare(selection, root, issuer) as client:
        result = consume(client, envelope(client, issuer, mutate=lambda body: body["offer"].update({field: "substitution"})))
    assert result["code"] == "delivery_rejected"
    assert_clean(result, root)


@pytest.mark.parametrize(
    "change",
    [
        {"issuer": "other-issuer"},
        {"audience": "worker"},
        {"key_id": "other-key"},
        {"delivery_expires_at": 1},
        {"credential_expires_at": 1},
        {"issued_at": 9999999999},
        {"delivery_expires_at": 9999999999},
        {"credential_expires_at": 9999999999},
        {"challenge": "replacement"},
        {"version": "other"},
    ],
)
def test_signed_header_changes_refused(selection, root, issuer, change):
    with prepare(selection, root, issuer) as client:
        result = consume(client, envelope(client, issuer, mutate=lambda body: body.update(change)))
    assert result["code"] == "delivery_rejected"
    assert_clean(result, root)


def test_forged_signature_and_one_shot(selection, root, issuer):
    with prepare(selection, root, issuer) as client:
        value = envelope(client, issuer)
        value["signature"] = host.b64(b"not-a-signature")
        result = consume(client, value)
        with pytest.raises(host.HostUnavailableError, match="already_consumed"):
            client.start(value)
    assert_clean(result, root)


def test_recipient_key_never_exits_private_process(selection, root, issuer):
    with prepare(selection, root, issuer) as client:
        assert set(vars(client)) == {"_control", "_watchdog_pid", "offer", "_original_ids", "_phase", "_result"}
        assert not any("private" in key for key in client.offer)
        result = consume(client, envelope(client, issuer))
    assert result["exit_code"] == 0
    assert_clean(result, root)


def test_executable_replacement_after_selection_cannot_change_consumption(selection, root, issuer, tmp_path):
    selected = tmp_path / "selected"
    shutil.copyfile("/usr/bin/true", selected)
    selected.chmod(0o700)
    selection["argv"] = [str(selected)]
    with prepare(selection, root, issuer) as client:
        selected.write_bytes(b"not an executable anymore")
        result = consume(client, envelope(client, issuer))
    assert result["exit_code"] == 0
    assert_clean(result, root)


def test_bound_cwd_descriptor_survives_path_replacement(selection, root, issuer, tmp_path):
    selected = tmp_path / "selected-dir"
    selected.mkdir()
    (selected / "marker").write_text("original")
    selection["cwd"] = str(selected)
    selection["argv"][2] = "assert open('marker').read()=='original'"
    with prepare(selection, root, issuer) as client:
        selected.rename(tmp_path / "renamed")
        selected.mkdir()
        (selected / "marker").write_text("replacement")
        result = consume(client, envelope(client, issuer))
    assert result["exit_code"] == 0
    assert_clean(result, root)


@pytest.mark.parametrize("duration", [1, 899, 901, True])
def test_sts_lifetime_refuses_before_offer(selection, root, issuer, duration):
    selection.update(operation="assume_role", use_seconds=duration)
    with pytest.raises(host.HostUnavailableError):
        prepare(selection, root, issuer)
    assert list(root.iterdir()) == []


def test_nonvolatile_or_symlink_sink_refuses_before_offer(selection, root, issuer, tmp_path):
    disk = tmp_path / "disk"
    disk.mkdir(mode=0o700)
    with pytest.raises(host.HostUnavailableError):
        prepare(selection, disk, issuer)
    link = tmp_path / "link"
    link.symlink_to(root)
    with pytest.raises(host.HostUnavailableError):
        prepare(selection, link, issuer)
    root.chmod(0o755)
    with pytest.raises(host.HostUnavailableError):
        prepare(selection, root, issuer)
    assert list(root.iterdir()) == []


def test_escape_syscalls_denied_but_normal_descendant_works(selection, root, issuer):
    selection["argv"][2] = """
import os,subprocess,errno,ctypes
for action in (os.setsid, lambda:os.setpgid(0,0)):
 try: action()
 except PermissionError: pass
 else: raise AssertionError('escaped group')
libc=ctypes.CDLL(None,use_errno=True)
for syscall,args in ((272,(0x10000000,)),(308,(-1,0)),(56,(0x10000000,0,0,0,0))):
 assert libc.syscall(syscall,*args)==-1
 assert ctypes.get_errno()==errno.EPERM
assert subprocess.run(['/usr/bin/true']).returncode==0
"""
    with prepare(selection, root, issuer) as client:
        result = consume(client, envelope(client, issuer))
    assert result["exit_code"] == 0
    assert_clean(result, root)


def wait_file(path):
    deadline = time.monotonic() + 5
    while not path.exists() or not (contents := path.read_text()):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    return int(contents)


@pytest.mark.parametrize("already_created", [False, True])
def test_wait_file_waits_for_pid_contents(tmp_path, monkeypatch, already_created):
    pidfile = tmp_path / "pid"
    if already_created:
        pidfile.touch()
    sleeps = []

    def publish_pid(delay):
        sleeps.append(delay)
        pidfile.write_text("12345")

    monkeypatch.setattr(time, "sleep", publish_pid)
    assert wait_file(pidfile) == 12345
    assert sleeps == [0.01]


@pytest.mark.parametrize("already_created", [False, True])
def test_wait_file_deadline_includes_empty_pid_files(tmp_path, monkeypatch, already_created):
    pidfile = tmp_path / "pid"
    if already_created:
        pidfile.touch()
    clock = iter([0, 5])
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))
    with pytest.raises(AssertionError):
        wait_file(pidfile)


def assert_dead(pid):
    deadline = time.monotonic() + 5
    while Path(f"/proc/{pid}").exists():
        assert time.monotonic() < deadline, f"process {pid} survived"
        time.sleep(0.01)


def test_cancel_kills_descendants_and_cleans_sink(selection, root, issuer, tmp_path):
    pidfile = tmp_path / "descendant-pid"
    selection["argv"][2] = f"""
import os,time
if os.fork()==0:
 open({str(pidfile)!r},'w').write(str(os.getpid()))
 time.sleep(60)
time.sleep(60)
"""
    with prepare(selection, root, issuer) as client:
        assert client.start(envelope(client, issuer))["type"] == "started"
        child = wait_file(pidfile)
        result = client.cancel()
    assert result["delivery_state"] == "cancelled"
    assert_clean(result, root)
    assert_dead(child)


def test_expiry_kills_child_and_cleans_sink(selection, root, issuer, tmp_path):
    pidfile = tmp_path / "expiry-pid"
    selection["use_seconds"] = 2
    selection["argv"][2] = f"import os,time; open({str(pidfile)!r},'w').write(str(os.getpid())); time.sleep(60)"
    with prepare(selection, root, issuer) as client:
        assert client.start(envelope(client, issuer))["type"] == "started"
        child = wait_file(pidfile)
        result = client.wait()
    # Either watchdog observes the deadline, or private supervisor terminates
    # the group at that same deadline. Both preserve uncertain issuance status.
    assert result["delivery_state"] in {"expired", "unknown"}
    assert_clean(result, root)
    assert_dead(child)


def test_child_launch_failure_cleans_sink(selection, root, issuer, tmp_path):
    selected = tmp_path / "bad-elf"
    selected.write_bytes(b"\x7fELF" + b"bad executable")
    selected.chmod(0o700)
    selection["argv"][0] = str(selected)
    with prepare(selection, root, issuer) as client:
        result = consume(client, envelope(client, issuer))
    assert result["exit_code"] == 126
    assert result["delivery_state"] == "failed"
    assert result["code"] == "child_start_failed"
    assert_clean(result, root)


def test_supervisor_crash_watchdog_kills_child_and_cleans_sink(selection, root, issuer, tmp_path):
    pidfile = tmp_path / "supervisor-pid"
    childfile = tmp_path / "child-pid"
    selection["argv"][2] = (
        f"import os,time; open({str(pidfile)!r},'w').write(str(os.getppid())); open({str(childfile)!r},'w').write(str(os.getpid())); time.sleep(60)"
    )
    with prepare(selection, root, issuer) as client:
        assert client.start(envelope(client, issuer))["type"] == "started"
        supervisor = wait_file(pidfile)
        child = wait_file(childfile)
        os.kill(supervisor, signal.SIGKILL)
        result = client.wait()
    assert result["delivery_state"] == "unknown"
    assert_clean(result, root)
    assert_dead(child)


@pytest.mark.parametrize("failure", ["crypto", "seccomp", "watchdog"])
def test_missing_host_prerequisite_never_returns_recipient_offer(selection, root, issuer, monkeypatch, failure):
    def fail(*_):
        raise OSError("unsafe-error-payload-PRIVATE_CANARY")

    if failure == "crypto":
        monkeypatch.setattr(host, "_crypto", fail)
    elif failure == "seccomp":
        monkeypatch.setattr(host, "_seccomp_group_bound", fail)
    else:
        original = host._prctl

        def prctl(option, argument):
            if option == 36:
                fail()
            return original(option, argument)

        monkeypatch.setattr(host, "_prctl", prctl)
    with pytest.raises(host.HostUnavailableError) as error:
        prepare(selection, root, issuer)
    assert str(error.value) == "native_private_host_unavailable"
    assert list(root.iterdir()) == []


def test_wrong_signing_key_and_ciphertext_tamper_rejected(selection, root, issuer):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with prepare(selection, root, issuer) as client:
        result = consume(client, envelope(client, other))
    assert_clean(result, root)
    assert result["code"] == "delivery_rejected"
    with prepare(selection, root, issuer) as client:
        value = envelope(client, issuer)
        ciphertext = bytearray(host.unb64(value["body"]["ciphertext"]))
        ciphertext[0] ^= 1
        value["body"]["ciphertext"] = host.b64(ciphertext)
        # Correct signature cannot authenticate malformed encrypted material.
        value["signature"] = host.b64(
            issuer.sign(host.canonical(value["body"]), padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())
        )
        result = consume(client, value)
    assert_clean(result, root)
    assert result["code"] == "delivery_rejected"


def test_cross_recipient_envelope_replay_rejected(selection, root, issuer):
    with prepare(selection, root, issuer) as original, prepare(selection, root, issuer) as other:
        value = envelope(original, issuer)
        other.prove(value["body"]["challenge"])
        result = consume(other, value)
        assert result["code"] == "delivery_rejected"
        own_result = consume(original, value)
        assert own_result["exit_code"] == 0
    assert_clean(own_result, root)


@pytest.mark.parametrize("payload", [{"file_base64": ""}, {"file_base64": "!!!!"}, {"file_base64": "YQ", "extra": "PRIVATE_CANARY"}])
def test_malformed_decrypted_payload_cannot_escape(selection, root, issuer, payload, capfd):
    with prepare(selection, root, issuer) as client:
        result = consume(client, envelope(client, issuer, payload))
    assert result["code"] == "delivery_rejected"
    assert_clean(result, root)
    captured = capfd.readouterr()
    assert "PRIVATE_CANARY" not in captured.out + captured.err


def test_native_host_process_crash_watchdog_cleans_independently(selection, root, issuer, tmp_path):
    # Test process adopts orphans to prove complete reaping, including watchdog.
    import ctypes
    import json

    previous = ctypes.c_int()
    assert ctypes.CDLL(None).prctl(37, ctypes.byref(previous), 0, 0, 0) == 0
    host._prctl(36, 1)
    reader, writer = os.pipe()
    childfile = tmp_path / "crash-child"
    selection["argv"][2] = f"import os,time; open({str(childfile)!r},'w').write(str(os.getpid())); time.sleep(60)"
    native = os.fork()
    if native == 0:
        os.close(reader)
        try:
            client = prepare(selection, root, issuer)
            assert client.start(envelope(client, issuer))["type"] == "started"
            child = wait_file(childfile)
            os.write(writer, json.dumps({"watchdog": client._watchdog_pid, "child": child}).encode())
            while True:
                time.sleep(1)
        except BaseException:
            os._exit(2)
    os.close(writer)
    try:
        ready = json.loads(os.read(reader, 4096))
        os.kill(native, signal.SIGKILL)
        os.waitpid(native, 0)
        # Watchdog's own pidfd observes host death even if socket EOF is lost.
        os.waitpid(ready["watchdog"], 0)
        assert_dead(ready["child"])
        assert list(root.iterdir()) == []
    finally:
        os.close(reader)
        host._prctl(36, previous.value)


def test_watchdog_death_fails_closed_without_named_plaintext(selection, root, issuer, tmp_path):
    import ctypes

    previous = ctypes.c_int()
    assert ctypes.CDLL(None).prctl(37, ctypes.byref(previous), 0, 0, 0) == 0
    host._prctl(36, 1)
    pidfile = tmp_path / "watchdog-death-child"
    selection["argv"][2] = f"import os,time; open({str(pidfile)!r},'w').write(str(os.getpid())); time.sleep(60)"
    try:
        with prepare(selection, root, issuer) as client:
            assert client.start(envelope(client, issuer))["type"] == "started"
            child = wait_file(pidfile)
            os.kill(client._watchdog_pid, signal.SIGKILL)
            result = client.wait()
            assert result["delivery_state"] == "unknown"
            assert result["cleanup"] == "unknown"
        # The private supervisor independently kills the same bounded group.
        deadline = time.monotonic() + 5
        while True:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            assert time.monotonic() < deadline
            if pid == 0:
                time.sleep(0.01)
        assert_dead(child)
        assert list(root.rglob("credential")) == []
        assert all(path.is_dir() for path in root.iterdir())
    finally:
        host._prctl(36, previous.value)


def test_full_size_file_consumes_without_named_plaintext(selection, root, issuer):
    selection["argv"][2] = "import os; assert open(os.environ['ADP_CREDENTIAL_FILE'],'rb').read()==b'x'*65536"
    with prepare(selection, root, issuer) as client:
        result = consume(client, envelope(client, issuer, {"file_base64": host.b64(b"x" * 65536)}))
    assert result["exit_code"] == 0
    assert_clean(result, root)


def test_type_coercion_cannot_change_exact_binding(selection, root, issuer):
    with prepare(selection, root, issuer) as client:
        result = consume(client, envelope(client, issuer, mutate=lambda body: body["offer"]["selection"].update(use_seconds=10.0)))
    assert result["code"] == "delivery_rejected"
    assert_clean(result, root)


def test_nonstandard_gcm_nonce_refused(selection, root, issuer):
    with prepare(selection, root, issuer) as client:
        result = consume(client, envelope(client, issuer, mutate=lambda body: body.update(iv=host.b64(os.urandom(16)))))
    assert result["code"] == "delivery_rejected"
    assert_clean(result, root)


def test_second_proof_refused_and_selected_context_unchanged(selection, root, issuer):
    with prepare(selection, root, issuer) as client:
        value = envelope(client, issuer)
        with pytest.raises(host.HostUnavailableError, match="already_consumed"):
            client.prove(host.b64(os.urandom(32)))
        result = consume(client, value)
    assert result["exit_code"] == 0
    assert_clean(result, root)


@pytest.mark.parametrize("field", ["agent_id", "run_id", "task_id"])
def test_no_synthetic_worker_selector_in_native_selection(selection, root, issuer, field):
    selection[field] = "synthetic-worker"
    with pytest.raises(host.HostUnavailableError):
        prepare(selection, root, issuer)
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("pid_write_delay", [0, 0.1])
def test_normal_exit_also_kills_background_descendants(selection, root, issuer, tmp_path, pid_write_delay):
    childfile = tmp_path / "background-pid"
    selection["argv"][2] = f"""
import os,time
if os.fork()==0:
 with open({str(childfile)!r},'w') as pidfile:
  time.sleep({pid_write_delay})
  pidfile.write(str(os.getpid()))
 time.sleep(60)
else:
 while not os.path.exists({str(childfile)!r}) or not open({str(childfile)!r}).read(): time.sleep(0.01)
"""
    with prepare(selection, root, issuer) as client:
        result = consume(client, envelope(client, issuer))
    assert result["exit_code"] == 0
    assert_clean(result, root)
    assert_dead(int(childfile.read_text()))


def test_lost_start_ack_drains_nonterminal_response_and_keeps_unknown_original_receipt(selection, root, issuer, monkeypatch):
    import select

    selection["argv"][2] = "import time; time.sleep(30)"
    with prepare(selection, root, issuer) as client:
        value = envelope(client, issuer)
        original = host._recv
        calls = 0

        def lose_first_response(channel):
            nonlocal calls
            calls += 1
            if calls == 1:
                assert select.select([channel], [], [], 5)[0]
                raise OSError("synthetic lost read before consuming queued started")
            return original(channel)

        monkeypatch.setattr(host, "_recv", lose_first_response)
        result = client.start(value)
        assert result["type"] == "result"
        assert result["delivery_state"] == "unknown"
        assert result["code"] == "delivery_acknowledgement_lost"
        assert_clean(result, root)
        assert client.wait() == result
        with pytest.raises(host.HostUnavailableError, match="already_consumed"):
            client.start(value)


def test_malformed_terminal_response_is_never_exported(selection, root, issuer, monkeypatch):
    with prepare(selection, root, issuer) as client:
        value = envelope(client, issuer)
        assert client.start(value)["type"] == "started"
        original = host._recv
        seen = False

        def corrupt_terminal(channel):
            nonlocal seen
            response = original(channel)
            if response.get("type") == "result" and not seen:
                seen = True
                return {**response, "credential": "PRIVATE_CANARY"}
            return response

        monkeypatch.setattr(host, "_recv", corrupt_terminal)
        result = client.wait()
        assert result["delivery_state"] == "unknown"
        assert "PRIVATE_CANARY" not in str(result)
        assert result["receipt_id"] == "original-receipt"
        assert list(root.iterdir()) == []
