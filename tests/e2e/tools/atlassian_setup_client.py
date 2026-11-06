"""Observe automatic MCP setup using the served, source-pinned CLI."""

import json
import re
import tomllib
from pathlib import Path

from tests.e2e.tools.atlassian_setup_live import private_file
from tests.e2e.tools.installed_clients import digest, installed_clients


def observe_client_configuration(config, gateway_revision):
    if not isinstance(gateway_revision, str) or not re.fullmatch(
        r"[0-9a-f]{40}", gateway_revision
    ):
        raise ValueError("The deployed gateway source is required for client setup")
    gateway = config["gateway_url"].rstrip("/") + "/api"
    with installed_clients(config["gateway_url"], gateway_revision) as (
        installed,
        environ,
        hashes,
    ):
        home = Path(environ["HOME"])
        expected = {
            "command": str(home / ".adp/bin/adp"),
            "args": ["--deployment", "default", "mcp"],
            "env": {
                "BG_CONFIG_DIR": str(home / ".bedrock-gateway"),
                "ADP_DEPLOYMENT_ID": "default",
                "ADP_DEPLOYMENT_URL": gateway,
            },
        }
        if Path(installed) != home / ".adp/bin/adp":
            raise ValueError("Client setup did not use its isolated installed CLI")
        for client, relative, section, parser in (
            ("claude-code", ".claude.json", "mcpServers", json.loads),
            ("codex", ".codex/config.toml", "mcp_servers", tomllib.loads),
        ):
            settings = parser(private_file(home / relative))
            entries = settings.get(section, {}) if type(settings) is dict else None
            if entries != {"adp-tools": expected}:
                raise ValueError(f"{client} does not have one automatic MCP entry")
        safe = {
            "run_id": config["run_id"],
            "owner_id": config["owner_id"],
            "fixture_kind": config["fixture_kind"],
            "gateway_url": config["gateway_url"],
            "gateway_revision": gateway_revision,
            "installed_files": hashes,
            "clients": ["claude-code", "codex"],
            "entry": "adp-tools",
            "args": expected["args"],
        }
    if home.exists() or home.is_symlink():
        raise ValueError("The isolated client setup was not cleaned up")
    return {
        "action": "mcp_configuration",
        "actor": "machine",
        "status": "observed",
        "source": "client",
        "evidence_sha256": digest(json.dumps(safe, sort_keys=True).encode()),
        "run_id": config["run_id"],
        "owner_id": config["owner_id"],
    }
