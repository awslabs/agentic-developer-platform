"""Offline fixture for the private, per-boot owner credential."""

import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest

IMAGE = Path(__file__).resolve().parents[1] / "image"
SPEC = importlib.util.spec_from_file_location("publisher", IMAGE / "publish_shell_model.py")
publisher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publisher)
RUN = "11111111-2222-4333-8444-555555555555"
PAYLOAD = {"run_id": RUN, "model_id": "approved-model", "base_url": "http://127.0.0.1:12345", "secret": "a" * 64}


@pytest.fixture
def host(tmp_path):
    root = tmp_path / "adp"
    root.mkdir(mode=0o700)
    (root / "crew-ready").write_text(json.dumps({"run_id": RUN, "lease_generation": 2}))
    (root / "crew-model.json").write_text(json.dumps({"run_id": RUN, "lease_generation": 2, "model_id": "approved-model"}))
    return root, tmp_path / "shell", tmp_path / "owner-home"


def publish(host, payload=PAYLOAD):
    root, directory, home = host
    home.mkdir(exist_ok=True)
    publisher.publish(payload, root=root, directory=directory, owner_uid=os.getuid(),
                      owner_gid=os.getgid(), trusted_uid=os.getuid(), home=home)


def test_publishes_only_private_runtime_and_removes_on_shutdown(host):
    root, directory, home = host
    home.mkdir()
    publish(host)
    secret = directory / "model.env"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    assert secret.stat().st_uid == os.getuid()
    assert secret.read_text() == ("ANTHROPIC_BASE_URL=http://127.0.0.1:12345\nANTHROPIC_API_KEY=" + "a" * 64 +
                                  "\nANTHROPIC_MODEL=approved-model\nADP_CREW_RUN_ID=" + RUN + "\n")
    # The home holds only Claude Code's own state file (#8719); the full key never leaves tmpfs.
    assert [path.name for path in home.iterdir()] == [".claude.json"]
    assert "a" * 64 not in (home / ".claude.json").read_text()
    publisher.remove(directory=directory, owner_uid=os.getuid(), owner_gid=os.getgid())
    assert not secret.exists()
    publisher.remove(directory=directory, owner_uid=os.getuid(), owner_gid=os.getgid())


@pytest.mark.parametrize("change", [
    {"run_id": "00000000-0000-0000-0000-000000000000"}, {"model_id": "other"},
    {"secret": "bad\nOPENAI_API_KEY=other"}, {"base_url": "http://example.com:12345"},
])
def test_refuses_misattribution_or_injection(host, change):
    with pytest.raises(ValueError):
        publish(host, {**PAYLOAD, **change})
    assert not host[1].exists()


def test_refuses_changed_authority_and_symlink_or_unsafe_directory(host):
    root, directory, _ = host
    (root / "crew-ready").write_text(json.dumps({"run_id": RUN, "lease_generation": 3}))
    with pytest.raises(ValueError, match="authority changed"):
        publish(host)
    (root / "crew-ready").write_text(json.dumps({"run_id": RUN, "lease_generation": 2}))
    directory.symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError, match="untrusted owner runtime directory"):
        publish(host)
    directory.unlink()
    publish(host)
    (directory / "model.env").chmod(0o644)
    publish(host)
    assert stat.S_IMODE((directory / "model.env").stat().st_mode) == 0o600
    directory.chmod(0o777)
    with pytest.raises(ValueError, match="untrusted owner runtime directory"):
        publish(host)


def test_image_installs_only_a_restricted_root_helper_and_cleans_up_on_stop():
    base = (IMAGE / "base.pkr.hcl").read_text()
    shell = (IMAGE / "prepare_shell.sh").read_text()
    from test_install_crew import INSTALLER
    assert 'source      = "${path.root}/publish_shell_model.py"' in base
    assert "sudo install -m 0700 -o root -g root /tmp/adp-publish-shell-model.py" in base
    assert "agent ALL=(root) NOPASSWD: /usr/bin/python3 -I /opt/adp/crew/publish_shell_model.py" in shell
    assert "ExecStopPost=+/usr/bin/python3 -I /opt/adp/crew/publish_shell_model.py --remove" in INSTALLER.UNIT


def test_publish_seeds_claude_onboarding_and_approves_this_boots_key(host):
    # #8719: interactive `claude` in the owner shell showed the login wizard; the state file must say
    # onboarding is done and approve the per-boot adapter key (last 20 chars), keeping MCP servers.
    root, directory, home = host
    home.mkdir()
    state = home / ".claude.json"
    state.write_text(json.dumps({"mcpServers": {"adp-tools": {"command": "x"}}, "theme": "light",
                                 "customApiKeyResponses": {"approved": [], "rejected": [PAYLOAD["secret"][-20:]]}}))
    publish(host)
    seeded = json.loads(state.read_text())
    assert seeded["hasCompletedOnboarding"] is True and seeded["theme"] == "light"
    assert seeded["customApiKeyResponses"] == {"approved": [PAYLOAD["secret"][-20:]], "rejected": []}
    assert seeded["mcpServers"] == {"adp-tools": {"command": "x"}}
    assert stat.S_IMODE(state.stat().st_mode) == 0o600
    publish(host)  # idempotent
    assert json.loads(state.read_text()) == seeded


def test_publish_refuses_a_symlinked_claude_state_file(host):
    root, directory, home = host
    home.mkdir()
    (home / "elsewhere.json").write_text("{}")
    (home / ".claude.json").symlink_to(home / "elsewhere.json")
    with pytest.raises(ValueError, match="untrusted Claude state file"):
        publish(host)


def test_publish_adds_claude_code_model_family_defaults_and_refuses_bad_aliases(host):
    """Dev 2026-10-25: Claude Code's /model opus|sonnet|haiku must resolve to gateway model ids the owner is
    entitled to, not Anthropic spellings the gateway refuses; the host publishes the mapping per boot."""
    root, directory, home = host
    home.mkdir()
    aliases = {"opus": "global.anthropic.claude-opus-5-5", "sonnet": "approved-model", "haiku": "global.anthropic.claude-haiku-4-5-20251001-v1:0"}
    publish(host, {**PAYLOAD, "model_aliases": aliases})
    text = (directory / "model.env").read_text()
    assert text.endswith("ANTHROPIC_DEFAULT_OPUS_MODEL=global.anthropic.claude-opus-5-5\nANTHROPIC_DEFAULT_SONNET_MODEL=approved-model\n"
                         "ANTHROPIC_DEFAULT_HAIKU_MODEL=global.anthropic.claude-haiku-4-5-20251001-v1:0\n")
    for bad in ({"opus": "bad id with spaces"}, {"gpt": "x"}, {"opus": 5}, "opus"):
        with pytest.raises(ValueError):
            publish(host, {**PAYLOAD, "model_aliases": bad})
