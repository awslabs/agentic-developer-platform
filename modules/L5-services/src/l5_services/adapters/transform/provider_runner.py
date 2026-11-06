"""Finite provider Pod entry point; no repository command runs in this process."""

import base64
import ctypes
import json
import os
import platform
import selectors
import signal
import subprocess
import time
from hashlib import sha256
from pathlib import Path

from l5_services.adapters.transform.ndjson import TransformConversation, TransformProtocolError, source_path
from l5_services.adapters.transform.provider_job import NDJSON_BINARY_SHA256
from l5_services.adapters.transform.provider_endpoint import TransformProviderEndpoint
from l5_services.adapters.transform.continuation import continuation_request


MAX_PAYLOAD_BYTES = 32 * 1024 * 1024


def restrict_provider(executable, workspace, home, write_paths):
    if platform.machine() != "x86_64":
        raise RuntimeError("Transform binary requires x86_64 isolation")
    library = ctypes.CDLL(None, use_errno=True)
    if library.syscall(444, 0, 0, 1) < 3:
        raise RuntimeError("Transform requires Landlock ABI 3; no unrestricted fallback")

    class Ruleset(ctypes.Structure):
        _fields_ = [("handled_access_fs", ctypes.c_uint64)]

    class PathRule(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]

    handled = (1 << 15) - 1
    ruleset = Ruleset(handled)
    descriptor = library.syscall(444, ctypes.byref(ruleset), ctypes.sizeof(ruleset), 0)
    if descriptor < 0:
        raise RuntimeError("Transform filesystem isolation unavailable")
    try:
        rules = [(Path(path), 4 | 8) for path in (
            "/usr", "/lib", "/lib64", "/opt/transform", "/etc/ssl", "/etc/pki", "/etc/resolv.conf",
            "/etc/hosts", "/etc/nsswitch.conf", "/etc/gai.conf", "/dev/urandom")]
        rules += [(workspace, 4 | 8), (home, handled & ~1), (Path(executable), 1 | 4),
                  (Path("/lib64/ld-linux-x86-64.so.2"), 1 | 4),
                  (Path("/usr/bin/git"), 1 | 4), (workspace / ".git", handled & ~1),
                  (Path("/dev/null"), 2 | 4)]
        rules += [(workspace / path, handled & ~1) for path in write_paths]
        for path, access in rules:
            if not path.exists():
                continue
            if not path.is_dir():
                access &= 1 | 2 | 4 | (1 << 14)
            parent = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = PathRule(access, parent)
                if library.syscall(445, descriptor, 1, ctypes.byref(rule), 0) != 0:
                    raise RuntimeError("Transform filesystem rule rejected")
            finally:
                os.close(parent)
        if library.prctl(38, 1, 0, 0, 0) != 0 or library.syscall(446, descriptor, 0) != 0:
            raise RuntimeError("Transform filesystem isolation not enforced")
    finally:
        os.close(descriptor)


def clean_environment(home):
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(home), "TMPDIR": str(home / "tmp"),
            "LANG": "C.UTF-8", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0"}


def provider_environment(home, credentials):
    environment = {**clean_environment(home), **credentials, "AWS_EC2_METADATA_DISABLED": "true"}
    endpoint_ref = os.environ.pop("L5_TRANSFORM_PROVIDER_ENDPOINT_REF", None)
    if endpoint_ref is not None:
        endpoint = TransformProviderEndpoint.from_installed(endpoint_ref,
            region=credentials["AWS_REGION"], provider_image=os.environ.pop("L5_TRANSFORM_PROVIDER_IMAGE", ""))
        environment["ATXCT_FES_ENDPOINT"] = endpoint.url
    return environment


def emit(intent, readback, **extra):
    record = {key: intent[key] for key in (
        "executor_name", "tenant_id", "product_id", "project_id", "run_id", "attempt_token", "input_hash",
        "executor_binding_ref", "image_digest", "binary_sha256", "definition_revision")}
    record.update(kind="transform-provider-observation-v1", readback=readback, **extra)
    encoded = json.dumps(record, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode()) > MAX_PAYLOAD_BYTES:
        raise ValueError("Transform provider observation exceeds its bound")
    print(encoded, flush=True)


def run_session(intent, conversation, *, interpreter, executable, workspace, home,
                environment, allowed_writes, baseline_commit):
    """Use one allowance/deadline for the initial child and one continuation."""
    job = intent["provider_job"]
    request = job["session_request"]
    deadline = time.monotonic() + job["active_deadline_seconds"]
    observed = None
    while True:
        if time.monotonic() >= deadline:
            raise TransformProtocolError("Transform original provider deadline elapsed")
        process = subprocess.Popen([interpreter, executable, "json"], cwd=workspace, env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True,
            preexec_fn=lambda: restrict_provider(interpreter, workspace, home, allowed_writes))
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)

        def send(frame):
            process.stdin.write((json.dumps(frame) + "\n").encode())
            process.stdin.flush()

        try:
            send(request)
            while time.monotonic() < deadline and not conversation.finished:
                if not selector.select(timeout=min(0.1, max(0, deadline - time.monotonic()))):
                    if process.poll() is not None:
                        break
                    continue
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                for reply in conversation.feed(chunk):
                    send(reply)
                readback = conversation.readback()
                if readback is not None and readback != observed and readback["state"] == "running":
                    emit(intent, readback)
                    observed = readback
            # Never turn a deadline, protocol exception or live-child silence
            # into another provider request. An exited child is required.
            if conversation.finished:
                return
            if time.monotonic() >= deadline:
                raise TransformProtocolError("Transform original provider deadline elapsed")
            process.wait(timeout=min(2, max(0.01, deadline - time.monotonic())))
        finally:
            # Reap/terminate the original process group before any continuation.
            # This is local termination, never a claim of remote cancellation.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)
            selector.close()
            process.stdin.close()
            process.stdout.close()
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise TransformProtocolError("Transform original process group remains active")
        request = continuation_request(request, conversation, home=home, workspace=workspace,
                                       baseline_commit=baseline_commit, region=environment["AWS_REGION"])


def run_provider(payload):
    intent, manifest, encoded_files = payload["intent"], payload["manifest"], payload["files"]
    job = intent["provider_job"]
    executable = "/usr/local/bin/atx"
    interpreter = "/usr/local/bin/node"
    if (job["argv"] != [executable, "json"] or job["binary_sha256"] != NDJSON_BINARY_SHA256
            or sha256(Path(executable).read_bytes()).hexdigest() != NDJSON_BINARY_SHA256
            or sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            != intent["input_hash"]):
        raise ValueError("Transform job executable or source pin changed")
    workspace, home = Path("/work/source"), Path("/work/home")
    workspace.mkdir(mode=0o700)
    home.mkdir(mode=0o700)
    (home / "tmp").mkdir()
    modes = {}
    if set(encoded_files) != {entry["path"] for entry in manifest["files"]}:
        raise ValueError("Transform projected source coverage changed")
    for entry in manifest["files"]:
        path = source_path(entry["path"])
        content = base64.b64decode(encoded_files[path], validate=True)
        if len(content) != entry["bytes"] or sha256(content).hexdigest() != entry["sha256"]:
            raise ValueError("Transform projected source bytes changed")
        selected = workspace / path
        selected.parent.mkdir(parents=True, exist_ok=True)
        selected.write_bytes(content)
        modes[path] = entry.get("mode", "100644")
        if modes[path] not in {"100644", "100755"}:
            raise ValueError("Transform projected file mode rejected")
        selected.chmod(0o755 if modes[path] == "100755" else 0o644)
    clean = clean_environment(home)

    def git(*args):
        return subprocess.check_output(["git", "-c", "core.hooksPath=/dev/null", *args],
                                       cwd=workspace, env=clean, timeout=15, stderr=subprocess.DEVNULL)

    git("init", "-q", "--template=")
    git("add", "--all", "--", ".")
    git("-c", "user.name=Transform", "-c", "user.email=transform@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "-q", "-m", "Sealed source")
    baseline_commit = git("rev-parse", "HEAD").decode().strip()
    allowed_writes = []
    prepared_files = {}
    for value in intent["write_paths"]:
        path = source_path(value)
        selected = workspace / path
        selected.parent.mkdir(parents=True, exist_ok=True)
        if not selected.exists():
            selected.touch(mode=0o644)
            metadata = selected.stat()
            prepared_files[path] = (metadata.st_mtime_ns, metadata.st_ctime_ns)
        allowed_writes.append(path)
    atx_home = home / ".aws/atx"
    atx_home.mkdir(parents=True)
    (atx_home / "trust-settings.yaml").write_text(
        "trustedTools: []\ntrustedShellCommands: []\nalwaysPromptCommands: ['*']\n")
    credentials = {key: os.environ.pop(key) for key in (
        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_REGION")}
    environment = provider_environment(home, credentials)
    minutes = int(job["session_request"]["params"]["agentMinutesLimit"])
    conversation = TransformConversation(intent, workspace=workspace, agent_minutes=minutes)
    run_session(intent, conversation, interpreter=interpreter, executable=executable, workspace=workspace,
                home=home, environment=environment, allowed_writes=allowed_writes, baseline_commit=baseline_commit)
    if conversation.buffer or not conversation.finished:
        raise TransformProtocolError("Transform ended without a complete bounded conversation")
    if conversation.state != "completed":
        emit(intent, conversation.readback(), capability_trace=conversation.capability_trace())
        return
    candidate = {}
    candidate_modes = {}
    for path, identity in prepared_files.items():
        selected = workspace / path
        if not selected.exists() and not selected.is_symlink():
            continue
        metadata = selected.lstat()
        if (not selected.is_symlink() and metadata.st_size == 0
                and (metadata.st_mtime_ns, metadata.st_ctime_ns) == identity):
            selected.unlink()
    candidate_bytes = 0
    for selected in sorted(workspace.rglob("*")):
        path = selected.relative_to(workspace).as_posix()
        if path == ".git" or path.startswith(".git/"):
            continue
        if selected.is_symlink():
            raise ValueError("Transform candidate contains a link")
        if selected.is_dir():
            continue
        if not selected.is_file() or selected.is_symlink():
            raise ValueError("Transform candidate file type changed")
        content = selected.read_bytes()
        candidate_bytes += len(content)
        if candidate_bytes > MAX_PAYLOAD_BYTES or len(candidate) >= 10000:
            raise ValueError("Transform candidate exceeds its bound")
        candidate[path] = base64.b64encode(content).decode()
        candidate_modes[path] = "100755" if selected.stat().st_mode & 0o111 else "100644"
    git("add", "--intent-to-add", "--all", "--", ".")
    patch = git("diff", "--binary", "--no-ext-diff", "--no-textconv", baseline_commit, "--")
    emit(intent, conversation.readback(), patch=base64.b64encode(patch).decode(),
         candidate_files=candidate, candidate_modes=candidate_modes, capability_trace=conversation.capability_trace())


def main():
    raw = b"".join(path.read_bytes() for path in sorted(Path("/input").glob("part-*")))
    if not raw or len(raw) > MAX_PAYLOAD_BYTES:
        raise ValueError("Transform input exceeds its bound")
    run_provider(json.loads(raw))


if __name__ == "__main__":
    main()
