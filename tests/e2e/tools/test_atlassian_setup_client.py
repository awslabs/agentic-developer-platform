"""Client setup evidence comes from installed settings, never readiness state."""

import json
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.e2e.tools.atlassian_setup_client import observe_client_configuration


REVISION = "a" * 40


@pytest.fixture
def setup_installation(tmp_path, monkeypatch):
    config = {
        "gateway_url": "https://gateway.example.test",
        "run_id": "run-one",
        "owner_id": "owner-one",
        "fixture_kind": "fresh",
    }
    calls = []
    controls = {"change": lambda home: None, "cleanup": True}

    @contextmanager
    def installed(gateway, revision):
        calls.append((gateway, revision))
        home = Path(tempfile.mkdtemp(dir=tmp_path))
        (home / ".codex").mkdir(mode=0o700)
        binary = home / ".adp/bin/adp"
        entry = {
            "command": str(binary),
            "args": ["--deployment", "default", "mcp"],
            "env": {
                "BG_CONFIG_DIR": str(home / ".bedrock-gateway"),
                "ADP_DEPLOYMENT_ID": "default",
                "ADP_DEPLOYMENT_URL": gateway + "/api",
            },
        }
        (home / ".claude.json").write_text(
            json.dumps({"mcpServers": {"adp-tools": entry}})
        )
        (home / ".codex/config.toml").write_text(
            "[mcp_servers.adp-tools]\ncommand = "
            + json.dumps(str(binary))
            + '\nargs = ["--deployment", "default", "mcp"]\n'
            + "[mcp_servers.adp-tools.env]\n"
            + "\n".join(
                key + " = " + json.dumps(value) for key, value in entry["env"].items()
            )
        )
        for path in (home / ".claude.json", home / ".codex/config.toml"):
            path.chmod(0o600)
        controls["change"](home)
        try:
            yield binary, {"HOME": str(home)}, {"adp": "b" * 64}
        finally:
            if controls["cleanup"]:
                shutil.rmtree(home)

    monkeypatch.setattr(
        "tests.e2e.tools.atlassian_setup_client.installed_clients", installed
    )
    return config, calls, controls


def test_actual_settings_are_read_and_restart_digest_does_not_include_temporary_paths(
    setup_installation,
):
    config, calls, _ = setup_installation
    first = observe_client_configuration(config, REVISION)
    assert first == observe_client_configuration(config, REVISION)
    assert calls == [(config["gateway_url"], REVISION)] * 2
    assert first["action"] == "mcp_configuration"
    assert first["source"] == "client" and first["actor"] == "machine"
    assert first["owner_id"] == config["owner_id"]
    assert len(first["evidence_sha256"]) == 64
    assert "/tmp/" not in json.dumps(first)


@pytest.mark.parametrize("revision", [None, "main", "a" * 39, "a" * 41])
def test_requires_deployed_source_before_install(setup_installation, revision):
    config, calls, _ = setup_installation
    with pytest.raises(ValueError, match="deployed gateway source"):
        observe_client_configuration(config, revision)
    assert calls == []


@pytest.mark.parametrize(
    "change",
    [
        "wrong-command",
        "foreign-url",
        "extra-entry",
        "missing-codex",
        "non-object",
        "public-config",
        "symlink",
    ],
)
def test_missing_changed_or_unowned_configuration_is_not_evidence(
    setup_installation, change
):
    config, _, controls = setup_installation

    def alter(home):
        path = home / ".claude.json"
        settings = json.loads(path.read_text())
        entry = settings["mcpServers"]["adp-tools"]
        if change == "wrong-command":
            entry["command"] = "unrelated-adp"
        elif change == "foreign-url":
            entry["env"]["ADP_DEPLOYMENT_URL"] = "https://foreign.example.test/api"
        elif change == "extra-entry":
            settings["mcpServers"]["duplicate"] = entry
        elif change == "missing-codex":
            (home / ".codex/config.toml").unlink()
        elif change == "non-object":
            settings = []
        elif change == "public-config":
            path.chmod(0o644)
        elif change == "symlink":
            path.rename(home / "actual.json")
            path.symlink_to(home / "actual.json")
            return
        path.write_text(json.dumps(settings))

    controls["change"] = alter
    with pytest.raises((ValueError, OSError)):
        observe_client_configuration(config, REVISION)


def test_incomplete_local_cleanup_does_not_report_success(setup_installation):
    config, _, controls = setup_installation
    controls["cleanup"] = False
    with pytest.raises(ValueError, match="not cleaned up"):
        observe_client_configuration(config, REVISION)
