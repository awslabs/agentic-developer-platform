"""Entries written by ADP's own setup before provenance existed are adopted, not refused.

Found live on 2026-11-03: after a CLI upgrade, ``adp claude`` and ``adp codex``
refused to launch because ``~/.claude.json`` and ``~/.codex/config.toml`` held
``{"command": "adp", "args": ["mcp"]}`` entries written by an older setup with
no record in ``~/.adp/mcp-config/managed.json``.
"""

import importlib.util
import json
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[2] / "cli" / "adp_mcp_config.py"
ENTRY = {"command": "/home/u/.adp/bin/adp", "args": ["mcp", "--deployment", "default"], "env": {"ADP_DEPLOYMENT_NAME": "default"}}


@pytest.fixture
def helper():
    spec = importlib.util.spec_from_file_location("adp_mcp_config", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _state(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    return state


def test_legacy_claude_entry_is_adopted_and_then_managed(helper, tmp_path, capsys):
    config = tmp_path / ".claude.json"
    config.write_text(json.dumps({"mcpServers": {"adp-tools": {"command": "adp", "args": ["mcp"]}, "other": {"command": "npx"}}}))
    state = _state(tmp_path)
    helper.configure("claude-code", config, ENTRY, state, check=True)  # the launcher's --check path
    assert "Adopted" in capsys.readouterr().err
    written = json.loads(config.read_text())
    assert written["mcpServers"]["adp-tools"] == ENTRY
    assert written["mcpServers"]["other"] == {"command": "npx"}, "neighbouring servers are untouched"
    managed = json.loads((state / "managed.json").read_text())
    assert managed["clients"]["claude-code"]["digest"] == helper.digest(ENTRY)
    # Second check is a no-op under management.
    helper.configure("claude-code", config, ENTRY, state, check=True)
    assert json.loads(config.read_text())["mcpServers"]["adp-tools"] == ENTRY


def test_legacy_codex_block_is_adopted(helper, tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('model_provider = "adp"\n\n[mcp_servers.adp-tools]\ncommand = "adp"\nargs = ["mcp"]\n')
    state = _state(tmp_path)
    helper.configure("codex", config, ENTRY, state)
    text = config.read_text()
    assert 'command = "/home/u/.adp/bin/adp"' in text and 'model_provider = "adp"' in text
    assert json.loads((state / "managed.json").read_text())["clients"]["codex"]


@pytest.mark.parametrize(
    "foreign",
    [
        {"command": "npx", "args": ["-y", "something"]},
        {"command": "adp", "args": ["serve"]},
        {"command": "adp", "args": ["mcp"], "env": {"OPENAI_API_KEY": "x"}},
        {"command": "adp", "args": ["mcp"], "cwd": "/tmp"},
    ],
)
def test_entries_that_adp_did_not_write_are_still_refused(helper, tmp_path, foreign):
    config = tmp_path / ".claude.json"
    config.write_text(json.dumps({"mcpServers": {"adp-tools": foreign}}))
    with pytest.raises(helper.ConfigurationError, match="unowned or modified"):
        helper.configure("claude-code", config, ENTRY, _state(tmp_path), check=True)
    assert json.loads(config.read_text())["mcpServers"]["adp-tools"] == foreign


def test_a_managed_entry_edited_by_the_user_is_still_refused(helper, tmp_path):
    config = tmp_path / ".claude.json"
    config.write_text(json.dumps({"mcpServers": {}}))
    state = _state(tmp_path)
    helper.configure("claude-code", config, ENTRY, state)
    data = json.loads(config.read_text())
    data["mcpServers"]["adp-tools"]["args"].append("--extra")
    config.write_text(json.dumps(data))
    with pytest.raises(helper.ConfigurationError, match="changed since ADP managed it|unowned or modified"):
        helper.configure("claude-code", config, ENTRY, state, check=True)
