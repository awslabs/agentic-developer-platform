"""Installed native clients using an already exchanged original native session."""

import json
import fcntl
import os
import re
import select
import signal
import stat
import subprocess
import sys
import time
import tomllib
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from modules.tools.platform.scripts.native_test_session import (
    NativeSession,
    private_file,
)
from tests.e2e.tools.installed_clients import (
    _open_pidfd,
    _signal_pidfd,
    digest,
    source_file,
    stop_owned_proxies,
)


@dataclass(frozen=True)
class NativeClientLaunch:
    client: str
    installed: str
    transport_file: str
    model: str
    command: tuple[str, ...]
    configuration_sha256: str | None = None


def configure_native_mcp(installed, environ, transport_file, *, client):
    """Pass the original session explicitly across the client's MCP env filter.

    Native clients may deliberately filter the parent environment before
    launching an MCP subprocess. Use the installed configuration owner to add
    the private reference to its existing isolated entry; never copy credentials
    into client settings or replace an unowned entry.
    """
    home = Path(environ["HOME"])
    path = home / (".claude.json" if client == "claude-code" else ".codex/config.toml")

    def read_entry():
        raw = private_file(path, 65536).decode()
        config = json.loads(raw) if client == "claude-code" else tomllib.loads(raw)
        field = "mcpServers" if client == "claude-code" else "mcp_servers"
        return config[field]["adp-tools"]

    entry = read_entry()
    gateway = json.loads((Path(environ["BG_CONFIG_DIR"]) / "config.json").read_text())[
        "gateway_url"
    ]
    expected = {
        "command": str(installed),
        "args": ["--deployment", "default", "mcp"],
        "env": {
            "BG_CONFIG_DIR": environ["BG_CONFIG_DIR"],
            "ADP_DEPLOYMENT_ID": "default",
            "ADP_DEPLOYMENT_URL": gateway,
        },
    }
    resumed = {
        **expected,
        "env": {
            **expected["env"],
            "ADP_NATIVE_TEST_TRANSPORT_FILE": str(transport_file),
        },
    }
    if entry == resumed:
        return digest(json.dumps(entry, sort_keys=True, separators=(",", ":")).encode())
    if entry != expected:
        raise ValueError("native MCP entry differs from the isolated installed client")
    entry = {
        **entry,
        "env": {**entry["env"], "ADP_NATIVE_TEST_TRANSPORT_FILE": str(transport_file)},
    }
    process = subprocess.run(
        [
            sys.executable,
            str(installed.parent / "adp_mcp_config.py"),
            "--client",
            client,
            "--config",
            str(path),
            "--state-dir",
            str(home / ".adp/mcp-config"),
            "--entry",
            json.dumps(entry, sort_keys=True),
        ],
        env=environ,
        cwd=home,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=10,
        check=False,
    )
    if process.returncode != 0 or read_entry() != entry:
        raise ValueError("original native MCP configuration was not confirmed")
    return digest(json.dumps(entry, sort_keys=True, separators=(",", ":")).encode())


@contextmanager
def native_client_transport(
    installed, environ, session, transport_file, *, client, model
):
    installed, home, gateway = _verify_native_client(
        installed, environ, session, transport_file, client=client, model=model
    )
    directory = home / ("native-model-" + client)
    directory.mkdir(mode=0o700, exist_ok=True)
    metadata = directory.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o077
    ):
        raise ValueError("Original native proxy directory is not private")
    descriptor = os.open(
        directory / "launch.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077
        ):
            raise ValueError("Original native proxy lock is not private")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with _native_client_transport(
            installed,
            environ,
            session,
            transport_file,
            home=home,
            gateway=gateway,
            client=client,
            model=model,
        ) as launch:
            yield launch
    finally:
        os.close(descriptor)


def _verify_native_client(
    installed, environ, session, transport_file, *, client, model
):
    """Own one local proxy; never refresh or substitute the native fixture login."""
    if type(session) is not NativeSession or client not in {"claude-code", "codex"}:
        raise ValueError(
            "original native session and supported installed client required"
        )
    installed = Path(installed)
    value = json.loads(private_file(transport_file, 65536))
    if (
        value.get("reference") != asdict(session.reference)
        or value.get("access_token") != session.token
        or value.get("capability") != session.capability
    ):
        raise ValueError("native transport belongs to a different original session")
    route = "/v1/messages" if client == "claude-code" else "/openai/v1/responses"
    if (
        sum(
            entry["route"] == route and entry["model"] == model
            for entry in value["models"]
        )
        != 1
    ):
        raise ValueError("native model is outside the original client plan")
    home = Path(environ["HOME"])
    if (
        installed != home / ".adp" / "bin" / "adp"
        or not home.is_absolute()
        or not stat.S_ISDIR(home.lstat().st_mode)
        or home.lstat().st_uid != os.getuid()
        or home.lstat().st_mode & 0o077
        or Path(transport_file).resolve().is_relative_to(home.resolve())
    ):
        raise ValueError("verified isolated native client installation required")
    for name in (
        "adp",
        "adp-mcp.py",
        "adp_common.py",
        "adp_native_test.py",
        "adp_native_credential_handoff.py",
        "adp_deployments.py",
        "adp_mcp_config.py",
        "bg-gateway-proxy.py",
    ):
        if digest((installed.parent / name).read_bytes()) != digest(
            source_file(session.reference.source_revision, name)
        ):
            raise ValueError(
                "installed native transport differs from the original approved source"
            )
    gateway = session.reference.gateway_origin + "/api"
    if (
        json.loads((Path(environ["BG_CONFIG_DIR"]) / "config.json").read_text()).get(
            "gateway_url"
        )
        != gateway
    ):
        raise ValueError("native client installation targets another gateway")
    return installed, home, gateway


@contextmanager
def _native_client_transport(
    installed, environ, session, transport_file, *, home, gateway, client, model
):
    directory = home / ("native-model-" + client)
    identity = directory / "proxy.json"
    env = {
        key: value
        for key, value in environ.items()
        if not key.startswith(("ANTHROPIC_", "OPENAI_", "AWS_", "CLAUDE_", "CODEX_"))
        and key != "ADP_GATEWAY_DUMMY"
    }
    env.update(
        ADP_NATIVE_TEST_TRANSPORT_FILE=str(transport_file),
        CODEX_HOME=str(home / ".codex"),
    )
    configuration_sha256 = configure_native_mcp(
        installed, env, transport_file, client=client
    )
    if identity.exists() or identity.is_symlink():
        private_file(identity, 16384)
        stop_owned_proxies(
            installed,
            env,
            gateway,
            records=[identity],
            deployment_id=session.reference.deployment_id,
        )
        identity.unlink(missing_ok=True)
    fd = os.open(
        directory / "proxy.private.log",
        os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
        0o600,
    )
    metadata = os.fstat(fd)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o077
    ):
        os.close(fd)
        raise ValueError("Original native proxy log is not private")
    process, pidfd = None, None
    try:
        with os.fdopen(fd, "ab") as output:
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(installed.parent / "bg-gateway-proxy.py"),
                    "--gateway-url",
                    gateway,
                    "--auth-helper",
                    str(installed.parent / "bg-cognito-auth.sh"),
                    "--port",
                    "0",
                    "--identity-file",
                    str(identity),
                    "--deployment-id",
                    session.reference.deployment_id,
                ],
                env=env,
                cwd=home,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=output,
            )
            pidfd = _open_pidfd(process.pid)
            deadline = time.monotonic() + 5
            while (
                not identity.exists()
                and time.monotonic() < deadline
                and process.poll() is None
            ):
                time.sleep(0.02)
            info = json.loads(private_file(identity, 16384))
            capability, port = info.get("capability"), info.get("port")
            if (
                info.get("pid") != process.pid
                or info.get("gateway_url") != gateway
                or info.get("deployment_id") != session.reference.deployment_id
                or info.get("proxy") != "adp-gateway-proxy"
                or type(port) is not int
                or not 1 <= port <= 65535
                or not isinstance(capability, str)
                or re.fullmatch(r"[A-Za-z0-9_-]{43}", capability) is None
            ):
                raise ValueError("original native model proxy is unconfirmed")
            base = "http://127.0.0.1:" + str(port)
            env["ADP_GATEWAY_DUMMY"] = capability
            if client == "claude-code":
                settings = home / ".claude" / "settings.json"
                if settings.exists():
                    raise ValueError(
                        "native Claude transport requires fresh isolated settings"
                    )
                env.update(
                    ANTHROPIC_BASE_URL=base,
                    ANTHROPIC_AUTH_TOKEN=capability,
                    CLAUDE_CODE_USE_BEDROCK="0",
                    CLAUDE_CODE_USE_VERTEX="0",
                    CLAUDE_CODE_USE_FOUNDRY="0",
                    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
                )
                command = ("claude",)
            else:
                command = (
                    "codex",
                    "-c",
                    'model_provider="adp-native-test"',
                    "-c",
                    'model_providers.adp-native-test.name="ADP original native test"',
                    "-c",
                    'model_providers.adp-native-test.base_url="' + base + '/openai/v1"',
                    "-c",
                    'model_providers.adp-native-test.wire_api="responses"',
                    "-c",
                    'model_providers.adp-native-test.env_key="ADP_GATEWAY_DUMMY"',
                )
            yield (
                NativeClientLaunch(
                    client,
                    str(installed),
                    str(transport_file),
                    model,
                    command,
                    configuration_sha256,
                ),
                env,
            )
    finally:
        if process is not None:
            if pidfd is not None and not select.select([pidfd], [], [], 0)[0]:
                _signal_pidfd(pidfd, signal.SIGTERM)
                if not select.select([pidfd], [], [], 5)[0]:
                    _signal_pidfd(pidfd, signal.SIGKILL)
            process.wait(timeout=5)
        if pidfd is not None:
            os.close(pidfd)
        # Only the exact proxy process created here is eligible for termination.
        # Retain the original operation journal and private recovery evidence.
        identity.unlink(missing_ok=True)
