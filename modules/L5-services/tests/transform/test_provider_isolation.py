"""Exercise the kernel boundary without invoking Transform or using provider credentials."""

import ctypes
import base64
import json
import platform
import subprocess
import sys
from pathlib import Path
from hashlib import sha256

import pytest

from l5_services.adapters.transform.provider_runner import clean_environment
from l5_services.adapters.transform import provider_runner


def _test_interpreter_environment(home):
    return {**clean_environment(home), "LD_LIBRARY_PATH": str(Path(sys.base_prefix) / "lib")}


def test_interpreter_bootstrap_uses_only_its_own_library_directory(monkeypatch, tmp_path):
    selected_python = tmp_path / "selected-python"
    monkeypatch.setattr(sys, "base_prefix", str(selected_python))
    for name in ("LD_LIBRARY_PATH", "LD_PRELOAD", "PYTHONPATH", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "untrusted-inherited-value")
    environment = _test_interpreter_environment(tmp_path / "home")
    assert environment["LD_LIBRARY_PATH"] == str(selected_python / "lib")
    assert all(name not in environment for name in ("LD_PRELOAD", "PYTHONPATH", "AWS_SECRET_ACCESS_KEY"))
    assert "LD_LIBRARY_PATH" not in clean_environment(tmp_path / "home")


@pytest.mark.parametrize("interpreter", [
    pytest.param([sys.executable], id="test-interpreter"),
    pytest.param([sys._base_executable, "-S"], id="external-dependencies"),
])
def test_provider_child_cannot_read_private_files_or_execute_shell(tmp_path, interpreter):
    if platform.system() != "Linux" or platform.machine() != "x86_64" or ctypes.CDLL(None).syscall(444, 0, 0, 1) < 3:
        pytest.skip("Landlock ABI 3 on x86_64 is required for this kernel check")
    workspace, home = tmp_path / "source", tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    (home / "tmp").mkdir()
    (workspace / "allowed.txt").write_text("original")
    (workspace / "readonly.txt").write_text("read only")
    private = tmp_path / "private.txt"
    private.write_text("private fixture")
    program = """
import json, os, subprocess, sys
from pathlib import Path
sys.path[:] = json.loads(sys.argv[1])
from l5_services.adapters.transform.provider_runner import restrict_provider
workspace, home, private = map(Path, sys.argv[2:])
allowed, readonly = workspace / 'allowed.txt', workspace / 'readonly.txt'
restrict_provider(os.path.realpath(sys.executable), workspace, home, ['allowed.txt'])
assert open(allowed).read() == 'original'
open(allowed, 'w').write('changed')
for path, mode in [(readonly, 'w'), (private, 'r'), ('/proc/self/environ', 'r')]:
    try:
        open(path, mode)
    except PermissionError:
        pass
    else:
        raise AssertionError('filesystem boundary allowed ' + str(path))
try:
    subprocess.run(['/bin/sh', '-c', 'true'], check=True)
except PermissionError:
    pass
else:
    raise AssertionError('shell execution was allowed')
"""
    subprocess.run([*interpreter, "-c", program, json.dumps(sys.path),
                    str(workspace), str(home), str(private)], check=True,
                   env=_test_interpreter_environment(home), timeout=10)
    assert (workspace / "allowed.txt").read_text() == "changed"


@pytest.mark.parametrize("clarification", [False, True])
def test_provider_commits_do_not_hide_original_patch(monkeypatch, tmp_path, capsys, clarification):
    if (platform.system() != "Linux" or platform.machine() != "x86_64"
            or ctypes.CDLL(None).syscall(444, 0, 0, 1) < 3 or not Path("/usr/local/bin/node").exists()):
        pytest.skip("Node and Landlock are required for the isolated provider fixture")
    workspace, home = tmp_path / "source", tmp_path / "home"
    fake = tmp_path / "provider.js"
    fake.write_text("""
const fs = require('fs');
const cp = require('child_process');
const rl = require('readline').createInterface({input: process.stdin});
const emit = frame => console.log(JSON.stringify(frame));
rl.on('line', line => {
  const request = JSON.parse(line);
  if (request.type === 'start_session') {
    emit({type:'session_started', conversationId:'fixture-native'});
    emit({type:'agent_minutes_update', used:0.1});
    emit({type:'tool_permission_required', toolCallId:'write-a', name:'file_write', parameters:{path:'src/example.py'}});
  } else if (request.type === 'tool_permission_response') {
    if (request.decision !== 'approve') process.exit(2);
    fs.writeFileSync('src/example.py', 'changed\\n');
    cp.execFileSync('/usr/bin/git', ['add', 'src/example.py']);
    cp.execFileSync('/usr/bin/git', ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'Provider change']);
    emit({type:'complete', status:'completed'});
  }
});
""")
    if clarification:
        fake.write_text(fake.read_text().replace("emit({type:'complete', status:'completed'});",
            "emit({type:'text_delta', content:'Which approach should I use?'});"
            "emit({type:'input_required', reason:'agent_turn_complete'});"))
    binary_hash = sha256(fake.read_bytes()).hexdigest()
    original_path = Path

    def mapped_path(value):
        return {"/work/source": workspace, "/work/home": home, "/usr/local/bin/atx": fake}.get(str(value), original_path(value))

    original_popen = subprocess.Popen

    def provider_boundary(argv, *args, **kwargs):
        if argv[:2] == ["/usr/local/bin/node", "/usr/local/bin/atx"]:
            deployed = home / "provider.js"
            deployed.write_bytes(fake.read_bytes())
            argv = [argv[0], str(deployed), *argv[2:]]
        return original_popen(argv, *args, **kwargs)

    monkeypatch.setattr(provider_runner, "Path", mapped_path)
    monkeypatch.setattr(provider_runner, "NDJSON_BINARY_SHA256", binary_hash)
    monkeypatch.setattr(provider_runner.subprocess, "Popen", provider_boundary)
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(key, "offline-fixture")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    contents = b"original\n"
    manifest = {"files": [{"path": "src/example.py", "bytes": len(contents), "sha256": sha256(contents).hexdigest(), "mode": "100644"}]}
    digest = sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    intent = {"executor_name": "fixture", "tenant_id": "tenant-a", "product_id": "product-a", "project_id": "project-a",
              "run_id": "run-a", "attempt_token": "a" * 64, "input_hash": digest, "executor_binding_ref": {},
              "image_digest": "sha256:" + "b" * 64, "binary_sha256": binary_hash, "definition_revision": "fixture",
              "read_paths": ["src"], "write_paths": ["src/example.py", "src/unwritten.py"], "excluded_paths": [".git"],
              "provider_job": {"argv": ["/usr/local/bin/atx", "json"], "binary_sha256": binary_hash,
                  "active_deadline_seconds": 10, "session_request": {"type": "start_session", "params": {"agentMinutesLimit": "1"}}}}
    provider_runner.run_provider({"intent": intent, "manifest": manifest,
                                 "files": {"src/example.py": base64.b64encode(contents).decode()}})
    emitted = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    if clarification:
        assert (workspace / "src/example.py").read_bytes() == b"changed\n"
        assert emitted[-1]["readback"]["state"] == "unknown"
        assert all("patch" not in record and record["readback"]["state"] != "completed" for record in emitted)
        return
    assert emitted[-1]["readback"]["state"] == "completed"
    patch = base64.b64decode(emitted[-1]["patch"])
    assert b"-original\n+changed\n" in patch
    assert b"unwritten.py" not in patch
    assert set(emitted[-1]["candidate_files"]) == {"src/example.py"}
    assert base64.b64decode(emitted[-1]["candidate_files"]["src/example.py"]) == b"changed\n"
