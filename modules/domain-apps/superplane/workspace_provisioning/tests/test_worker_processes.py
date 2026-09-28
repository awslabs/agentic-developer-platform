"""Provider commands have explicit credentials and die with all child processes."""

from pathlib import Path
import signal
from types import SimpleNamespace

import pytest

from workspace_provisioning.process import WorkerProcesses
from workspace_provisioning.runtime_config import LifecycleRefused


def worker(tmp_path, verify=lambda: None):
    credentials = SimpleNamespace(
        get_frozen_credentials=lambda: SimpleNamespace(
            access_key="fixture-access",
            secret_key="fixture-private",
            token="fixture-session",
        )
    )
    return WorkerProcesses(
        binaries={
            name: "/opt/bin/" + name
            for name in ("python", "terraform", "kubectl", "aws")
        },
        directory=tmp_path,
        session=SimpleNamespace(get_credentials=lambda: credentials),
        region="us-west-2",
        verify=verify,
    )


def test_command_environment_does_not_inherit_host_credentials_or_cli_state(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AWS_PROFILE", "unrelated-profile")
    monkeypatch.setenv("KUBECONFIG", "/unrelated/kubeconfig")
    monkeypatch.setenv("TF_DATA_DIR", "/unrelated/state")
    captured = {}

    def launch(command, **kwargs):
        captured.update(command=command, **kwargs)
        return SimpleNamespace(pid=123, returncode=0, poll=lambda: 0)

    monkeypatch.setattr("workspace_provisioning.process.subprocess.Popen", launch)
    result = worker(tmp_path).run(["terraform", "version"])
    assert result.returncode == 0
    assert captured["start_new_session"] is True
    assert captured["command"][0] == "/opt/bin/terraform"
    assert not {"AWS_PROFILE", "KUBECONFIG", "TF_DATA_DIR"} & captured["env"].keys()
    assert captured["env"]["HOME"] == str(tmp_path)
    assert captured["env"]["AWS_SHARED_CREDENTIALS_FILE"] == "/dev/null"
    assert captured["env"]["AWS_ACCESS_KEY_ID"] == "fixture-access"


def test_authority_loss_kills_children_even_after_parent_has_exited(
    tmp_path, monkeypatch
):
    calls, checks = [], []

    def verify():
        checks.append(True)
        if len(checks) == 2:
            raise LifecycleRefused("authority withdrawn")

    monkeypatch.setattr(
        "workspace_provisioning.process.subprocess.Popen",
        lambda *a, **k: SimpleNamespace(pid=123, returncode=0, poll=lambda: 0),
    )
    monkeypatch.setattr(
        "workspace_provisioning.process.os.killpg",
        lambda pid, sig: calls.append((pid, sig)),
    )
    with pytest.raises(LifecycleRefused, match="withdrawn"):
        worker(tmp_path, verify).run(["python", "/reviewed/prepare.py"])
    assert calls == [(123, signal.SIGTERM), (123, signal.SIGKILL)]


def test_executable_outside_exact_reviewed_paths_never_launches(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "workspace_provisioning.process.subprocess.Popen",
        lambda *a, **k: calls.append(a),
    )
    with pytest.raises(LifecycleRefused, match="reviewed set"):
        worker(tmp_path).run([str(Path("/tmp/terraform")), "apply"])
    assert calls == []
