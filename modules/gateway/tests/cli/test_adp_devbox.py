"""Dev Box SSH never writes grant credentials or pushes a key before authorization."""

import importlib.util
import shlex
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

CLI = Path(__file__).parents[2] / "cli"
sys.path.insert(0, str(CLI))
spec = importlib.util.spec_from_file_location("devbox_cli", CLI / "adp-devbox.py")
devbox = importlib.util.module_from_spec(spec)
spec.loader.exec_module(devbox)
BOX_ID = "00000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def local_capability_check(monkeypatch):
    monkeypatch.setattr(devbox.common, "ensure_can_mutate", lambda operation: None)


def grant(**overrides):
    return {
        "desktop_id": BOX_ID,
        "instance_id": "i-0123456789abcdef0",
        "region": "us-east-1",
        "os_user": "ec2-user",
        "expires_at": (datetime.now(UTC) + timedelta(minutes=14)).isoformat(),
        "credentials": {"access_key_id": "test-access", "secret_access_key": "test-secret", "session_token": "test-token"},
        **overrides,
    }


def test_proxy_exchanges_token_pushes_key_and_starts_one_scoped_session(monkeypatch, tmp_path):
    monkeypatch.setenv("AWS_PROFILE", "ambient-profile")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ambient-access")
    api = Mock(return_value=grant())
    monkeypatch.setattr(devbox.common, "api", api)
    calls = []

    def run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        if arguments[0] == "ssh-keygen":
            private = Path(arguments[-1])
            private.write_text("test-private")
            private.chmod(0o600)
            Path(str(private) + ".pub").write_text("ssh-ed25519 dGVzdA==\n")
            return SimpleNamespace(returncode=0)
        return SimpleNamespace(returncode=0, stdout='{"Success":true}')

    monkeypatch.setattr(devbox.subprocess, "run", run)
    execute = Mock()
    monkeypatch.setattr(devbox.os, "execvpe", execute)
    devbox.proxy(BOX_ID)
    api.assert_called_once_with("POST", f"/cloud-desktops/{BOX_ID}/ssh-access")
    assert calls[0][0][:4] == ["ssh-keygen", "-q", "-t", "ed25519"]
    assert calls[1][0][:3] == ["aws", "ec2-instance-connect", "send-ssh-public-key"]
    assert calls[1][0][calls[1][0].index("--ssh-public-key") + 1] == "ssh-ed25519 dGVzdA=="
    assert calls[1][1]["env"]["AWS_ACCESS_KEY_ID"] == "test-access"
    assert "AWS_PROFILE" not in calls[1][1]["env"]
    assert execute.call_args.args[1][:3] == ["aws", "ssm", "start-session"]
    assert execute.call_args.args[2]["AWS_SESSION_TOKEN"] == "test-token"
    assert not any("test-secret" in str(path) or "test-token" in str(path) for path in tmp_path.rglob("*"))
    calls.clear()
    devbox.proxy(BOX_ID)
    assert len(calls) == 1 and calls[0][0][1] == "ec2-instance-connect"


@pytest.mark.parametrize(
    "overrides",
    [
        {"desktop_id": "00000000-0000-4000-8000-000000000002"},
        {"instance_id": "i-foreign;command"},
        {"os_user": "root"},
        {"expires_at": lambda: (datetime.now(UTC) - timedelta(seconds=1)).isoformat()},
        {"expires_at": lambda: (datetime.now(UTC) + timedelta(minutes=30)).isoformat()},
    ],
)
def test_invalid_grant_does_not_create_or_send_key(monkeypatch, overrides):
    # Collection can precede this test by minutes in the coverage shards.
    overrides = {key: value() if callable(value) else value for key, value in overrides.items()}
    monkeypatch.setattr(devbox.common, "api", lambda *args: grant(**overrides))
    run = Mock()
    monkeypatch.setattr(devbox.subprocess, "run", run)
    with pytest.raises(devbox.common.CliError):
        devbox.prepare(BOX_ID)
    run.assert_not_called()
    assert not (Path.home() / ".ssh").exists()


def test_key_delivery_refusal_does_not_start_session(monkeypatch):
    monkeypatch.setattr(devbox.common, "api", lambda *args: grant())
    execute = Mock()
    monkeypatch.setattr(devbox.os, "execvpe", execute)

    def run(arguments, **kwargs):
        if arguments[0] == "ssh-keygen":
            private = Path(arguments[-1])
            private.write_text("test-private")
            private.chmod(0o600)
            Path(str(private) + ".pub").write_text("ssh-ed25519 dGVzdA==\n")
            return SimpleNamespace(returncode=0)
        return SimpleNamespace(returncode=0, stdout='{"Success":false}')

    monkeypatch.setattr(devbox.subprocess, "run", run)
    with pytest.raises(devbox.common.CliError, match="refused"):
        devbox.proxy(BOX_ID)
    execute.assert_not_called()


def test_non_owner_gateway_denial_never_creates_key(monkeypatch):
    def refuse(*args):
        raise devbox.common.CliError("ADP returned HTTP 404", "http_error", 5)

    monkeypatch.setattr(devbox.common, "api", refuse)
    with pytest.raises(devbox.common.CliError, match="404"):
        devbox.prepare(BOX_ID)
    assert not (Path.home() / ".ssh").exists()


def owner_alias(monkeypatch):
    deployment = SimpleNamespace(id="deployment-one", name="dev", gateway_url="https://gateway.example.test/api", legacy=False)
    monkeypatch.setattr(devbox.common, "deployment", lambda: deployment)
    monkeypatch.setattr(devbox.common, "gateway_url", lambda: deployment.gateway_url)
    monkeypatch.setenv("ADP_TENANT_ID", "tenant-one")
    monkeypatch.setenv("ADP_TENANT_SUB", "alice-sub")
    api = Mock(return_value={"items": [{"desktop_id": BOX_ID, "name": "assistant", "status": "running"}]})
    monkeypatch.setattr(devbox.common, "api", api)
    monkeypatch.setattr(devbox, "ensure_key", Mock())
    return api


def test_ssh_config_preserves_existing_hosts_and_pins_proxy_identity(monkeypatch, capsys):
    api = owner_alias(monkeypatch)
    directory = Path.home() / ".ssh"
    directory.mkdir(mode=0o700)
    config = directory / "config"
    existing = "# user settings\nHost *\n    User someone-else\nHost github.example.test\n    Port 2222\n"
    config.write_text(existing)
    config.chmod(0o600)
    assert devbox.main(["ssh-config", "assistant"]) == 0
    first = config.read_text()
    assert first.endswith(existing[existing.index("Host *") :])
    assert first.index("Host adp-devbox-dev-assistant") < first.index("Host *")
    assert "    User ec2-user" in first
    assert f'    IdentityFile "{devbox.key_path(BOX_ID)}"' in first
    assert "    IdentitiesOnly yes" in first
    assert "    HostKeyAlias adp-devbox-dev-assistant" in first
    assert "test-secret" not in first and "test-token" not in first
    assert config.stat().st_mode & 0o777 == 0o600
    proxy = next(line.strip().removeprefix("ProxyCommand ") for line in first.splitlines() if "ProxyCommand " in line)
    arguments = shlex.split(proxy)
    assert "ADP_DEPLOYMENT_ID=deployment-one" in arguments
    assert "ADP_DEPLOYMENT_URL=https://gateway.example.test/api" in arguments
    assert "ADP_DEVBOX_GATEWAY_URL=https://gateway.example.test/api" in arguments
    assert "ADP_TENANT_ID=tenant-one" in arguments
    assert "ADP_TENANT_SUB=alice-sub" in arguments
    assert arguments[-4:] == [str(CLI / "adp"), "devbox", "proxy", BOX_ID]
    assert devbox.main(["ssh-config", BOX_ID]) == 0
    assert config.read_text() == first
    assert api.call_count == 2
    assert capsys.readouterr().out.count("SSH host configured: adp-devbox-dev-assistant") == 2


def test_ssh_executes_written_alias_without_exposing_credentials(monkeypatch):
    owner_alias(monkeypatch)
    execute = Mock()
    monkeypatch.setattr(devbox.os, "execvp", execute)
    assert devbox.main(["ssh", "assistant"]) == 0
    execute.assert_called_once_with("ssh", ["ssh", "adp-devbox-dev-assistant"])
    assert "ProxyCommand " in (Path.home() / ".ssh" / "config").read_text()


def test_ssh_config_does_not_replace_an_existing_alias_or_follow_a_symlink(monkeypatch, tmp_path):
    owner_alias(monkeypatch)
    directory = Path.home() / ".ssh"
    directory.mkdir(mode=0o700)
    config = directory / "config"
    existing = "Host adp-devbox-dev-assistant\n    User preserved\n"
    config.write_text(existing)
    config.chmod(0o600)
    assert devbox.main(["ssh-config", "assistant"]) == 1
    assert config.read_text() == existing
    config.unlink()
    target = tmp_path / "not-ssh-config"
    target.write_text(existing)
    config.symlink_to(target)
    assert devbox.main(["ssh-config", "assistant"]) == 1
    assert target.read_text() == existing


def test_ssh_config_replaces_this_clis_stale_entry_for_a_released_box_of_the_same_name(monkeypatch, capsys):
    """Release → Provision keeps the box name, so the alias is the same while the box id (and the marker digest)
    changes; the stale ADP-written entry is replaced, the user's own hosts stay (dev 2026-10-26)."""
    owner_alias(monkeypatch)
    directory = Path.home() / ".ssh"
    directory.mkdir(mode=0o700)
    config = directory / "config"
    old_box = "74280768-fccb-481a-9dc4-780da42032c8"
    stale = (
        f"# ADP Dev Box 0123456789abcdef0123 begin\nHost adp-devbox-dev-assistant\n    HostName {old_box}\n"
        f"    User ec2-user\n    ProxyCommand env adp devbox proxy {old_box}\n# ADP Dev Box 0123456789abcdef0123 end\n"
    )
    existing = "# user settings\n" + stale + "\nHost github.example.test\n    Port 2222\n"
    config.write_text(existing)
    config.chmod(0o600)
    assert devbox.main(["ssh-config", "assistant"]) == 0
    written = config.read_text()
    assert written.count("Host adp-devbox-dev-assistant") == 1
    assert old_box not in written and "0123456789abcdef0123" not in written
    assert f"    HostName {BOX_ID}" in written
    assert written.endswith("Host github.example.test\n    Port 2222\n") and written.startswith("# user settings")
    assert devbox.main(["ssh-config", BOX_ID]) == 0 and config.read_text() == written
    assert capsys.readouterr().out.count("SSH host configured: adp-devbox-dev-assistant") == 2


def test_ssh_alias_refuses_gateway_switch_and_missing_owner(monkeypatch):
    api = owner_alias(monkeypatch)
    api.return_value = {"items": []}
    assert devbox.main(["ssh-config", "assistant"]) == 1
    assert not (Path.home() / ".ssh").exists()
    monkeypatch.setenv("ADP_DEVBOX_GATEWAY_URL", "https://other.example.test/api")
    api.reset_mock()
    with pytest.raises(devbox.common.CliError, match="different gateway"):
        devbox.prepare(BOX_ID)
    api.assert_not_called()
