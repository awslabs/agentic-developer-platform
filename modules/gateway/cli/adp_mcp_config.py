#!/usr/bin/env python3
"""Keep ADP-owned MCP entries separate from user-owned client configuration."""

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
import tomllib
from pathlib import Path

MCP_HEADER = re.compile(r'^\s*\[\s*mcp_servers\s*\.\s*(?:adp-tools|"adp-tools"|\'adp-tools\')(?:\s*\.|\s*\])')
PROVIDER_HEADER = re.compile(r'^\s*\[\s*model_providers\s*\.\s*(?:adp-gateway|"adp-gateway"|\'adp-gateway\')\s*\]')
TABLE_HEADER = re.compile(r'^\s*\[')


class ConfigurationError(Exception):
    pass


def digest(entry):
    return hashlib.sha256(json.dumps(entry, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def read_file(path):
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise ConfigurationError(f"{path} is not a regular file owned by this user; no changes made")
    return path.read_text(encoding="utf-8")


def write_atomic(path, contents):
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(contents)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_config(client, contents):
    if client == "claude-code":
        config = json.loads(contents or "{}")
        if not isinstance(config, dict) or not isinstance(config.get("mcpServers", {}), dict):
            raise ConfigurationError("Claude configuration or mcpServers is not an object; no changes made")
        return config, config.get("mcpServers", {}).get("adp-tools")
    config = tomllib.loads(contents or "")
    servers = config.get("mcp_servers", {})
    if not isinstance(servers, dict):
        raise ConfigurationError("Codex mcp_servers is not a table; no changes made")
    return config, servers.get("adp-tools")


def render_codex(contents, entry, provider_port):
    preserved = []
    removed = False
    in_table = False
    for line in contents.splitlines(keepends=True):
        if TABLE_HEADER.match(line):
            in_table = True
            next_removed = bool(MCP_HEADER.match(line) or (provider_port and PROVIDER_HEADER.match(line)))
            if next_removed and not removed:
                while preserved and not preserved[-1].strip():
                    preserved.pop()
            removed = next_removed
        if removed or (provider_port and not in_table and re.match(r'^\s*model_provider\s*=', line)):
            continue
        preserved.append(line)
    prefix = ''.join(preserved).rstrip('\n')
    if provider_port:
        prefix = prefix.lstrip('\n')
        prefix = 'model_provider = "adp-gateway"\n' + ('\n' + prefix if prefix else '')
        prefix += (f'\n\n[model_providers.adp-gateway]\nname = "adp-gateway"\n'
                   f'base_url = "http://127.0.0.1:{provider_port}/openai/v1"\n'
                   'wire_api = "responses"\nenv_key = "ADP_GATEWAY_DUMMY"')
    if entry is not None:
        prefix += ('\n\n[mcp_servers.adp-tools]\n'
                   f'command = {json.dumps(entry["command"])}\n'
                   f'args = {json.dumps(entry["args"])}\n\n[mcp_servers.adp-tools.env]\n')
        prefix += '\n'.join(f'{key} = {json.dumps(value)}' for key, value in sorted(entry['env'].items())) + '\n'
    elif prefix:
        prefix += '\n'
    tomllib.loads(prefix)
    return prefix


def legacy_adp_entry(existing):
    """An adp-tools entry written by ADP's own setup before provenance records existed (#8806).

    Those entries point at the adp executable with ``mcp`` as the first argument
    and carry no environment or only ADP's own variables. Nothing else writes an
    ``adp-tools`` server with that shape, so it is adopted and brought under
    management rather than refused as foreign; a user-authored or edited entry
    (different command, different first argument, non-ADP environment) is still
    refused.
    """
    if not isinstance(existing, dict):
        return False
    command = existing.get("command")
    args = existing.get("args")
    env = existing.get("env", {})
    if not isinstance(command, str) or Path(command).name != "adp":
        return False
    if not isinstance(args, list) or not args or args[0] != "mcp":
        return False
    if env is None:
        env = {}
    if not isinstance(env, dict) or any(not str(key).startswith("ADP_") for key in env):
        return False
    return set(existing) <= {"command", "args", "env"}


def configure(client, path, entry, state_dir, provider_port=None, remove=False, check=False):
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if state_dir.is_symlink() or state_dir.stat().st_uid != os.getuid() or state_dir.stat().st_mode & 0o077:
        raise ConfigurationError(f"{state_dir} must be a private directory owned by this user")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = state_dir / "config.lock"
    with open(lock_path, "a+", encoding="utf-8") as lock:
        os.fchmod(lock.fileno(), 0o600)
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ConfigurationError("Another ADP MCP configuration update is running; retry shortly") from None
                time.sleep(0.05)
        state_path = state_dir / "managed.json"
        state = json.loads(read_file(state_path) or '{"version":1,"clients":{},"pending":{}}')
        if state.get("version") != 1 or not isinstance(state.get("clients"), dict) or not isinstance(state.get("pending"), dict):
            raise ConfigurationError("Managed MCP provenance is unreadable; restore it from backup before retrying")
        current_text = read_file(path)
        config, existing = read_config(client, current_text)
        current_digest = digest(existing) if existing is not None else None
        recorded = state["clients"].get(client)
        pending = state["pending"].get(client)
        if pending:
            if pending.get("path") != str(path) or current_digest not in (pending.get("previous"), pending.get("digest")):
                raise ConfigurationError(f"{path} changed during an interrupted ADP update; no changes made")
            if current_digest == pending["digest"]:
                if current_digest is None:
                    state["clients"].pop(client, None)
                else:
                    state["clients"][client] = {"path": str(path), "digest": current_digest}
                del state["pending"][client]
                write_atomic(state_path, json.dumps(state, indent=2) + '\n')
                recorded = state["clients"].get(client)
        if remove and not recorded:
            return
        adopting = existing is not None and not recorded and not remove and legacy_adp_entry(existing)
        if existing is not None and not adopting and (not recorded or recorded.get("path") != str(path) or recorded.get("digest") != current_digest):
            raise ConfigurationError(f"{path} has an unowned or modified adp-tools entry; remove it or restore ADP's provenance before retrying")
        if recorded and (recorded.get("path") != str(path) or recorded.get("digest") != current_digest):
            raise ConfigurationError(f"{path} has changed since ADP managed it; no changes made")
        if check and not adopting:
            return
        if adopting:
            print(f"[OK] Adopted ADP's pre-provenance adp-tools entry in {path}; it is now managed.", file=sys.stderr)
        updated = digest(entry) if entry is not None else None
        if client == "claude-code":
            if remove:
                config.get("mcpServers", {}).pop("adp-tools", None)
            else:
                config.setdefault("mcpServers", {})["adp-tools"] = entry
            rendered = json.dumps(config, indent=2) + '\n'
        else:
            rendered = render_codex(current_text or "", entry, provider_port)
        if current_text == rendered and recorded and recorded["digest"] == updated:
            return
        state["pending"][client] = {"path": str(path), "previous": current_digest, "digest": updated}
        write_atomic(state_path, json.dumps(state, indent=2) + '\n')
        if current_text is not None and current_text != rendered:
            backup = state_dir / f"{client}.{time.time_ns()}.backup"
            write_atomic(backup, current_text)
        if current_text != rendered:
            write_atomic(path, rendered)
        if remove:
            state["clients"].pop(client, None)
        else:
            state["clients"][client] = {"path": str(path), "digest": updated}
        del state["pending"][client]
        write_atomic(state_path, json.dumps(state, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--client', choices=('codex', 'claude-code'), required=True)
    parser.add_argument('--entry')
    parser.add_argument('--remove', action='store_true')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--provider-port', type=int)
    arguments = parser.parse_args()
    try:
        entry = json.loads(arguments.entry) if arguments.entry else None
        if not arguments.remove and (not isinstance(entry, dict) or set(entry) != {'command', 'args', 'env'}):
            raise ConfigurationError('Invalid managed MCP entry')
        configure(arguments.client, arguments.config, entry, arguments.state_dir, arguments.provider_port,
                  arguments.remove, arguments.check)
    except (ConfigurationError, ValueError, OSError, tomllib.TOMLDecodeError) as error:
        parser.exit(1, f"ADP MCP configuration: {error}\n")


if __name__ == '__main__':
    main()
