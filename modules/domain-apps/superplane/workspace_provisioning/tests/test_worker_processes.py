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
        captured["config"] = Path(kwargs["env"]["AWS_CONFIG_FILE"]).read_text()
        return SimpleNamespace(pid=123, returncode=0, poll=lambda: 0)

    monkeypatch.setattr("workspace_provisioning.process.subprocess.Popen", launch)
    result = worker(tmp_path).run(["terraform", "version"])
    assert result.returncode == 0
    assert captured["start_new_session"] is True
    assert captured["command"][0] == "/opt/bin/terraform"
    assert not {"AWS_PROFILE", "KUBECONFIG", "TF_DATA_DIR"} & captured["env"].keys()
    assert captured["env"]["HOME"] == str(tmp_path)
    assert captured["env"]["PATH"] == "/opt/bin:/bin"
    assert captured["env"]["AWS_SHARED_CREDENTIALS_FILE"] == "/dev/null"
    assert (
        not {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"}
        & captured["env"].keys()
    )
    assert captured["env"]["SUPERPLANE_CREDENTIAL_BRIDGE_URL"].startswith(
        "http://127.0.0.1:"
    )
    assert len(captured["env"]["SUPERPLANE_CREDENTIAL_BRIDGE_TOKEN"]) >= 48
    assert "credential_process = " in captured["config"]
    assert (
        captured["env"]["SUPERPLANE_CREDENTIAL_BRIDGE_TOKEN"] not in captured["config"]
    )
    assert "fixture-private" not in captured["config"]
    assert not Path(captured["env"]["AWS_CONFIG_FILE"]).exists()
    assert "AWS_CONTAINER_CREDENTIALS_FULL_URI" not in captured["env"]


@pytest.mark.parametrize("exit_code", [0, 7])
def test_real_child_scratch_uses_private_volume_and_is_removed(
    tmp_path, monkeypatch, exit_code
):
    import json
    import sys

    monkeypatch.setenv("TMPDIR", "/unrelated-host-scratch")
    root = tmp_path / "superplane-worker-65531" / ("a" * 64)
    root.mkdir(parents=True)
    process = worker(root)
    process.binaries = {"python": sys.executable}
    script = """import json,os,socket,stat,sys,tempfile
from pathlib import Path
scratch=Path(tempfile.gettempdir())
assert scratch == Path(os.environ['TMPDIR'])
assert scratch.resolve().parent == Path(os.environ['HOME'])
assert stat.S_IMODE(scratch.stat().st_mode) == 0o700
assert stat.S_IMODE(scratch.parent.stat().st_mode) == 0o700
(scratch/'provider-download').write_bytes(b'x' * (1 << 20))
with socket.socket(socket.AF_UNIX) as listener:
    listener.bind(str(scratch/'plugin1234567890'))
print(json.dumps(str(scratch)))
sys.exit(int(sys.argv[1]))
"""
    result = process.run(["python", "-c", script, str(exit_code)])
    assert result.returncode == exit_code, result.stderr
    assert not Path(json.loads(result.stdout)).exists()
    if exit_code:
        retained = list(root.glob("command-failure-*"))
        assert len(retained) == 1
        assert retained[0].joinpath("stdout").read_text() == result.stdout
        assert retained[0].joinpath("stderr").read_text() == result.stderr
        assert list(root.iterdir()) == retained
    else:
        assert list(root.iterdir()) == []


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
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("reason", ["authority withdrawn", "runtime deadline expired"])
def test_bootstrap_get_retry_reenters_current_worker_authority(
    tmp_path, monkeypatch, reason
):
    from superplane_bootstrap.adapters import IamNodeRoleFacts, KubectlClusterAccess

    checks, launches = [], []

    def verify():
        checks.append(True)
        # The first command checked before and after its timeout. Authority is
        # gone when the next attempt asks to start another process.
        if len(checks) == 3:
            raise LifecycleRefused(reason)

    def launch(command, **kwargs):
        launches.append(command)
        kwargs["stderr"].write(
            b"Unable to connect to the server: context deadline exceeded "
            b"(Client.Timeout exceeded while awaiting headers)\n"
        )
        return SimpleNamespace(pid=123, returncode=1, poll=lambda: 1)

    monkeypatch.setattr("workspace_provisioning.process.subprocess.Popen", launch)
    process = worker(tmp_path, verify)
    access = KubectlClusterAccess(
        runner=process,
        kubeconfig=tmp_path / "kubeconfig",
        controller_namespace="workspace",
        controller_service_account="superplane-controller",
        controller_image="registry.example/superplane-controller:v1",
        node_role=IamNodeRoleFacts(
            runner=process, node_role_arn="arn:aws:iam::000000000000:role/test"
        ),
        imds_probe_image="registry.example/python@sha256:" + "0" * 64,
    )
    with pytest.raises(LifecycleRefused, match=reason):
        access._kube("get", "rolebinding", "controller", "-n", "workspace")
    assert len(checks) == 3
    assert len(launches) == 1
    assert len(list(tmp_path.glob("command-failure-*"))) == 1


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


def test_bridge_refuses_missing_token_revoked_authority_and_closes(tmp_path):
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
        SimpleNamespace(get_credentials=lambda: credentials), verify, directory=tmp_path
    ) as env:
        url = env["SUPERPLANE_CREDENTIAL_BRIDGE_URL"]
        with pytest.raises(HTTPError) as error:
            urlopen(url, timeout=2)
        assert error.value.code == 403
        request = Request(
            url, headers={"Authorization": env["SUPERPLANE_CREDENTIAL_BRIDGE_TOKEN"]}
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


def test_real_sdk_tolerates_authority_checks_longer_than_metadata_timeout(tmp_path):
    import boto3
    import sys
    import time
    from datetime import UTC, datetime, timedelta

    credentials = SimpleNamespace(
        _expiry_time=datetime.now(UTC) + timedelta(minutes=30),
        get_frozen_credentials=lambda: SimpleNamespace(
            access_key="fixture-key", secret_key="fixture-secret", token="fixture-token"
        ),
    )
    calls = []

    def verify():
        calls.append(True)
        time.sleep(2.1)

    process = WorkerProcesses(
        binaries={"python": sys.executable},
        directory=tmp_path,
        session=SimpleNamespace(get_credentials=lambda: credentials),
        region="us-east-1",
        verify=verify,
    )
    sdk_packages = str(Path(boto3.__file__).resolve().parent.parent)
    script = (
        f"import sys; sys.path.insert(0, {sdk_packages!r})\n"
        + """import boto3
credentials = boto3.Session().get_credentials()
assert credentials.method == 'custom-process'
assert credentials.get_frozen_credentials().access_key == 'fixture-key'
assert credentials.get_frozen_credentials().token == 'fixture-token'
print('slow-authority-verified')
"""
    )
    result = process.run(["python", "-c", script], timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "slow-authority-verified"
    assert len(calls) >= 4
    assert list(tmp_path.iterdir()) == []


def test_failed_command_keeps_private_output_without_exposing_it(tmp_path):
    import stat
    import sys

    process = worker(tmp_path)
    process.binaries = {"python": sys.executable}
    for number in range(2):
        with pytest.raises(LifecycleRefused) as failure:
            process.checked(
                [
                    "python",
                    "-c",
                    f'import sys; print("private-output-{number}"); print("private-error", file=sys.stderr); sys.exit(7)',
                ]
            )
        assert "private-output" not in str(failure.value)
        assert "private-error" not in str(failure.value)
    directories = list(tmp_path.glob("command-failure-*"))
    assert len(directories) == 2
    assert {p.joinpath("stdout").read_text() for p in directories} == {
        "private-output-0\n",
        "private-output-1\n",
    }
    for directory in directories:
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert directory.joinpath("stderr").read_text() == "private-error\n"
        assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in directory.iterdir())
    assert set(tmp_path.iterdir()) == set(directories)
