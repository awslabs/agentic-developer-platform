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
    assert (
        not {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"}
        & captured["env"].keys()
    )
    assert captured["env"]["AWS_CONTAINER_CREDENTIALS_FULL_URI"].startswith(
        "http://127.0.0.1:"
    )
    assert len(captured["env"]["AWS_CONTAINER_AUTHORIZATION_TOKEN"]) >= 48


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


def test_real_child_sdk_renews_through_memory_bridge(tmp_path):
    import boto3
    import sys
    from datetime import UTC, datetime, timedelta

    credentials = SimpleNamespace(_expiry_time=None)
    calls = []

    def frozen():
        calls.append(True)
        credentials._expiry_time = datetime.now(UTC) + timedelta(seconds=2)
        return SimpleNamespace(
            access_key="key-" + str(len(calls)), secret_key="secret", token="token"
        )

    credentials.get_frozen_credentials = frozen
    process = WorkerProcesses(
        binaries={"python": sys.executable},
        directory=tmp_path,
        session=SimpleNamespace(get_credentials=lambda: credentials),
        region="us-east-1",
        verify=lambda: None,
    )
    # CI may install boto3 in the runner's user site. The child deliberately gets
    # a private HOME, so select the real SDK's package directory explicitly for
    # this test rather than weakening the production environment isolation.
    sdk_packages = str(Path(boto3.__file__).resolve().parent.parent)
    script = (
        f"import sys; sys.path.insert(0, {sdk_packages!r})\n"
        + """import boto3, time, os
assert not {'AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY','AWS_SESSION_TOKEN'} & os.environ.keys()
credentials = boto3.Session().get_credentials()
first = credentials.get_frozen_credentials().access_key
time.sleep(2.1)
second = credentials.get_frozen_credentials().access_key
assert first != second
print('refreshed')
"""
    )
    result = process.run(["python", "-c", script], timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "refreshed" and len(calls) >= 2
    assert list(tmp_path.iterdir()) == []


def test_bridge_refuses_missing_token_revoked_authority_and_closes():
    from datetime import UTC, datetime, timedelta
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError, URLError
    from workspace_provisioning.credential_bridge import credential_environment

    revoked = []
    credentials = SimpleNamespace(
        _expiry_time=datetime.now(UTC) + timedelta(minutes=10),
        get_frozen_credentials=lambda: SimpleNamespace(
            access_key="key", secret_key="secret", token="token"
        ),
    )

    def verify():
        if revoked:
            raise LifecycleRefused("revoked")

    with credential_environment(
        SimpleNamespace(get_credentials=lambda: credentials), verify
    ) as env:
        url = env["AWS_CONTAINER_CREDENTIALS_FULL_URI"]
        with pytest.raises(HTTPError) as error:
            urlopen(url, timeout=2)
        assert error.value.code == 403
        request = Request(
            url, headers={"Authorization": env["AWS_CONTAINER_AUTHORIZATION_TOKEN"]}
        )
        with urlopen(request, timeout=2) as response:
            assert (
                response.status == 200
                and response.headers["Cache-Control"] == "no-store"
            )
        revoked.append(True)
        with pytest.raises(HTTPError) as error:
            urlopen(request, timeout=2)
        assert error.value.code == 403
    with pytest.raises(URLError):
        urlopen(request, timeout=2)
