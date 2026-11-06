"""Materialize maintained Product client sessions without calling a provider."""

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

from journey_driver import private_path
from qualification import ROOT


def token_names(value):
    if isinstance(value, dict):
        for name, child in value.items():
            if (name.endswith("token_env") or name == "credential_ref") and isinstance(child, str):
                yield child
            else:
                yield from token_names(child)
    elif isinstance(value, list):
        for child in value:
            yield from token_names(child)


def write_private(path, value):
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as destination:
        json.dump(value, destination, sort_keys=True)


def local_command(arguments, environment):
    result = subprocess.run(arguments, env=environment, capture_output=True, timeout=30, check=False)
    if result.returncode or len(result.stdout) > 65536 or len(result.stderr) > 65536:
        raise ValueError("private CLI preparation failed")
    return result.stdout


def materialize_clients(manifest, manifest_path):
    supplied = manifest.get("private_clients")
    if supplied is None:
        return manifest
    selected = {name: value for name, value in manifest.items() if name != "private_clients"}
    driver = selected["journey_driver"]
    from manual_ui import validate_manual
    manual = validate_manual(selected) is not None
    if (not isinstance(supplied, dict)
            or set(supplied) != {"version", "environment", "cli_config", "cli_tokens"} | (set() if manual else {"browser_state", "recovery_browser_states"})
            or supplied["version"] != 1):
        raise ValueError("private client transport requires the versioned Product session bundle")
    environment = supplied["environment"]
    referenced = set(token_names(selected))
    if (not isinstance(environment, dict) or not environment
            or any(not re.fullmatch(r"DEVOPS_[A-Z0-9_]+", name) or name not in referenced
                   or not isinstance(value, str) or not value or len(value) > 16384
                   or "\n" in value or "\r" in value or "\x00" in value
                   for name, value in environment.items())):
        raise ValueError("private transport may populate only referenced DEVOPS_ Product tokens")
    gateway = driver["gateway_url"].rstrip("/")
    address = urlsplit(gateway)
    config, tokens = supplied["cli_config"], supplied["cli_tokens"]
    browser = supplied.get("browser_state")
    recovery_states = supplied.get("recovery_browser_states", {})
    users = {user["id"] for user in selected.get("recovery", {}).get("users", [])}
    if not isinstance(recovery_states, dict) or set(recovery_states) != (set() if manual else users):
        raise ValueError("private transport needs a separate browser state for each recovery identity")
    if (address.scheme != "https" or not address.hostname or address.path
            or address.username or address.password or address.query or address.fragment
            or not isinstance(config, dict) or config.get("gateway_url", "").rstrip("/") != gateway + "/api"
            or not isinstance(tokens, dict) or not tokens
            or any(not isinstance(state, dict) or set(state) - {"cookies", "origins"}
                   or not isinstance(state.get("cookies"), list) or not isinstance(state.get("origins"), list)
                   or any(not isinstance(origin, dict) or origin.get("origin") != gateway for origin in state["origins"])
                   or any(not isinstance(cookie, dict) or cookie.get("domain", "").lstrip(".") != address.hostname
                          or cookie.get("secure") is not True for cookie in state["cookies"])
                   for state in ([] if manual else [browser, *recovery_states.values()]))):
        raise ValueError("maintained client sessions must belong to the selected HTTPS Gateway")
    if any(os.environ.get(name, value) != value for name, value in environment.items()):
        raise ValueError("private Product token conflicts with the current process")
    parent = private_path(str(Path(manifest_path).parent), directory=True)
    directory = parent / "private-clients"
    identity = hashlib.sha256(json.dumps({"clients": supplied, "driver": driver,
                                        "source": selected["release"]["source_sha"]}, sort_keys=True).encode()).hexdigest()
    expected = {"version": 1, "sha256": identity}
    home = directory / "home"
    executable = home / ".adp/bin/adp"
    browser_path = directory / "browser.json"
    recovery_paths = {user: directory / ("browser-" + hashlib.sha256(user.encode()).hexdigest() + ".json") for user in ([] if manual else users)}
    marker = directory / "prepared.json"
    if directory.exists() or directory.is_symlink():
        private_path(str(directory), directory=True)
        if json.loads(private_path(str(marker)).read_text()) != expected:
            raise ValueError("private client state belongs to another fixture; retain it for recovery")
    else:
        source = ROOT / "modules/gateway/cli"
        installer = source / "install.sh"
        names = re.search(r'^CLI_FILES="([^"]+)"$', installer.read_text(), re.MULTILINE)
        if names is None or any(not (source / name).is_file() or (source / name).is_symlink()
                                for name in names[1].split()):
            raise ValueError("complete reviewed CLI sources are required for offline installation")
        if hashlib.sha256((source / "adp").read_bytes()).hexdigest() != driver["cli_sha256"]:
            raise ValueError("private CLI pin differs from the reviewed source")
        directory.mkdir(mode=0o700)
        home.mkdir(mode=0o700)
        cli_environment = {"HOME": str(home), "ADP_HOME": str(home / ".adp"),
                           "BG_CONFIG_DIR": str(home / ".installer"), "PATH": os.environ.get("PATH", os.defpath)}
        local_command(["sh", str(installer), "--prefix", str(executable.parent), "--no-path-edit",
                       "--gateway-url", gateway], cli_environment)
        cli_environment.pop("BG_CONFIG_DIR")
        resolver = executable.with_name("adp_deployments.py")
        local_command([sys.executable, str(resolver), "add", driver["cli_deployment"], "--url", gateway, "--json"],
                      cli_environment)
        resolved = json.loads(local_command([sys.executable, str(resolver), "resolve", "--deployment",
                                            driver["cli_deployment"], "--format", "json"], cli_environment))
        session_dir = private_path(resolved["config_dir"], directory=True)
        if not session_dir.is_relative_to(home / ".adp/deployments"):
            raise ValueError("CLI session resolved outside its isolated deployment")
        write_private(session_dir / "config.json", config)
        write_private(session_dir / "tokens.json", tokens)
        if not manual:
            write_private(browser_path, browser)
        for user, state in recovery_states.items():
            write_private(recovery_paths[user], state)
        write_private(marker, expected)
    private_path(str(home), directory=True)
    if not manual:
        private_path(str(browser_path))
    for path in recovery_paths.values():
        private_path(str(path))
    if hashlib.sha256(private_path(str(executable), executable=True).read_bytes()).hexdigest() != driver["cli_sha256"]:
        raise ValueError("installed private CLI differs from its reviewed pin")
    os.environ.update(environment)
    if "recovery" in selected and not manual:
        selected = {**selected, "recovery": {**selected["recovery"], "browser": {
            **selected["recovery"]["browser"], "storage_states": {user: str(path) for user, path in recovery_paths.items()}}}}
    return {**selected, "journey_driver": {**driver, "cli_home": str(home), "cli_executable": str(executable),
                                           **({} if manual else {"browser_storage_state": str(browser_path)})}}
