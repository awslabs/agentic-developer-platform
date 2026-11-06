"""Materialize transported client sessions beside the protected release fixture."""

import hashlib
import json
import os
import re
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[5]


def write_private(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(value, output, sort_keys=True)
        output.flush()
        os.fsync(output.fileno())


def materialize_clients(manifest, directory):
    if "clients" not in manifest:
        return manifest
    clients = manifest["clients"]
    selected = manifest.get("cli_deployment", "")
    if (not isinstance(clients, dict) or set(clients) != {
            "api_tokens", "cli_registry", "cli_config", "cli_tokens", "browser_storage_state"}
            or not isinstance(selected, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,62}", selected)):
        raise ValueError("FinOps requires the portable client session contract")
    registry = clients["cli_registry"]
    records = registry.get("deployments", {}) if isinstance(registry, dict) else {}
    record = records.get(selected, {})
    gateway = manifest["gateway_url"].rstrip("/") + "/api"
    if (not isinstance(record, dict) or set(records) != {selected}
            or record.get("legacy") or not re.fullmatch(r"d[0-9a-f]{16}", record.get("id", ""))
            or record.get("gateway_url") != gateway
            or not isinstance(clients["cli_config"], dict) or clients["cli_config"].get("gateway_url") != gateway
            or any(not isinstance(clients[key], dict) for key in (
                "api_tokens", "cli_tokens", "browser_storage_state"))):
        raise ValueError("portable CLI session must name only the selected gateway")
    tokens = clients["api_tokens"]
    if (set(tokens) != {"api_token_env", "readback_token_env", "mcp_token_env"}
            or any(not isinstance(value, str) or not value.strip() for value in tokens.values())):
        raise ValueError("portable Product sessions are missing")
    digest = hashlib.sha256(json.dumps({"source_sha": manifest["release_sha"], "clients": clients},
                                     sort_keys=True).encode()).hexdigest()
    target = Path(directory) / "clients"
    if target.exists():
        if (target.is_symlink() or target.stat().st_mode & 0o077
                or (target / "fixture-digest.json").is_symlink()
                or json.loads((target / "fixture-digest.json").read_text()) != digest):
            raise ValueError("portable client directory differs; retain and reconcile existing sessions")
    else:
        target.mkdir(mode=0o700)
        adp_home = target / ".adp"
        store = adp_home / "deployments" / record["id"]
        for path in (adp_home, adp_home / "deployments", store, target / ".bedrock-gateway"):
            path.mkdir(mode=0o700)
        write_private(adp_home / "deployments.json", registry)
        write_private(store / "config.json", clients["cli_config"])
        write_private(store / "tokens.json", clients["cli_tokens"])
        write_private(target / "api-tokens.json", tokens)
        write_private(target / "browser-state.json", clients["browser_storage_state"])
        shutil.copytree(ROOT / "modules/gateway/cli", target / "cli",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (target / "cli/adp").chmod(0o700)
        write_private(target / "fixture-digest.json", digest)
    return {key: value for key, value in manifest.items() if key != "clients"} | {
        "cli_home": str(target), "cli_config_dir": str(target / ".adp/deployments" / record["id"]),
        "cli_executable": str(target / "cli/adp"),
        "cli_sha256": hashlib.sha256((ROOT / "modules/gateway/cli/adp").read_bytes()).hexdigest(),
        "session_file": str(target / "api-tokens.json"),
        "browser_storage_state": str(target / "browser-state.json"),
    }
