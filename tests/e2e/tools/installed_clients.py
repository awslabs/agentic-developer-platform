"""Verified, temporary installations for the protected tools evaluation."""

import hashlib
import ctypes
import json
import os
import re
import shutil
import select
import signal
import stat
import sys
import tomllib
from contextlib import contextmanager
from pathlib import Path
from tempfile import mkdtemp
from urllib.request import urlopen
from uuid import UUID

from tests.e2e.tools.process_bounds import run_bounded


SOURCE_DIR = "modules/gateway/cli"
REVISION = re.compile(r"[0-9a-f]{40}\Z")


def source_file(revision, name):
    if not REVISION.fullmatch(revision):
        raise ValueError("a pinned gateway source revision is required")
    result = run_bounded(["git", "show", f"{revision}:{SOURCE_DIR}/{name}"],
                         environ=os.environ, timeout=20)
    if result.returncode:
        raise ValueError("pinned CLI source is unavailable")
    return result.stdout


def digest(content):
    return hashlib.sha256(content).hexdigest()


def run_client(command, environ):
    result = run_bounded(command, environ=environ, timeout=120)
    if result.returncode:
        raise ValueError("installed client command failed")


def _pidfd_call(name, arguments, argtypes):
    # Some portable Python builds omit the stdlib wrappers although the Linux
    # runner's libc/kernel provide pidfds. Keep the same kernel ownership fence.
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, name, None)
    if function is None:
        raise ValueError("isolated proxy cleanup requires Linux pidfd support")
    function.argtypes, function.restype = argtypes, ctypes.c_int
    result = function(*arguments)
    if result < 0:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number))
    return result


def _open_pidfd(pid):
    if hasattr(os, "pidfd_open"):
        return os.pidfd_open(pid)
    return _pidfd_call("pidfd_open", (pid, 0), (ctypes.c_int, ctypes.c_uint))


def _signal_pidfd(descriptor, number):
    if hasattr(signal, "pidfd_send_signal"):
        signal.pidfd_send_signal(descriptor, number)
    else:
        _pidfd_call("pidfd_send_signal", (descriptor, number, None, 0),
                    (ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint))


def stop_owned_proxies(installed, environ, gateway_url, *, records=None, deployment_id="default"):
    """Stop only detached auth proxies published by this private installation.

    The protected harness runs on Linux. A pidfd prevents PID reuse between the
    installed CLI's birth-time/target check and the actual signal. No process
    group or operator-wide proxy discovery is used.
    """
    home = Path(environ["HOME"])
    config = home / ".bedrock-gateway"
    records = [config / "proxy.json", *config.glob("tenants/*/proxy.json")] if records is None else records
    if len(records) > 17:
        raise ValueError("isolated proxy inventory exceeded its bound")
    for record in records:
        if not record.is_relative_to(home):
            raise ValueError("isolated proxy identity is outside the original home")
        if not record.exists() and not record.is_symlink():
            continue
        if any(part.is_symlink() for part in (record, *record.parents) if part != home.parent):
            raise ValueError("isolated proxy identity is not privately owned")
        metadata = record.stat()
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or
                metadata.st_mode & 0o077 or metadata.st_size > 16384):
            raise ValueError("isolated proxy identity is not privately owned")
        value = json.loads(record.read_text())
        pid = value.get("pid")
        if type(pid) is not int or pid <= 1:
            raise ValueError("isolated proxy process identity cannot be safely checked")
        try:
            descriptor = _open_pidfd(pid)
        except ProcessLookupError:
            continue
        try:
            if select.select([descriptor], [], [], 0)[0]:
                continue
            checked = run_bounded(
                [sys.executable, str(installed.parent / "adp_deployments.py"), "proxy-owner",
                 str(record.parent), deployment_id, gateway_url], environ=environ,
                timeout=10, max_output_bytes=128)
            if checked.returncode or checked.stdout.strip() != str(pid).encode():
                raise ValueError("isolated proxy ownership does not match the selected installation")
            _signal_pidfd(descriptor, signal.SIGTERM)
            if not select.select([descriptor], [], [], 5)[0]:
                _signal_pidfd(descriptor, signal.SIGKILL)
                if not select.select([descriptor], [], [], 2)[0]:
                    raise ValueError("isolated proxy did not terminate")
        except ProcessLookupError:
            pass
        finally:
            os.close(descriptor)


def release_home(home):
    try:
        operation_dir = home / ".bedrock-gateway" / "tool-operations"
        if operation_dir.exists():
            metadata = operation_dir.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_mode & 0o077:
                raise ValueError("run-owned operation store cannot be safely inspected")
        if not ((home / "proxy-cleanup-pending.json").exists() or
                (operation_dir.is_dir() and any(operation_dir.iterdir()))):
            shutil.rmtree(home)
    finally:
        (home / ".bedrock-gateway" / "tokens.json").unlink(missing_ok=True)


@contextmanager
def persistent_home(root, recovery_id):
    home = root / recovery_id
    home.mkdir(mode=0o700)
    try:
        yield str(home)
    finally:
        release_home(home)


@contextmanager
def temporary_home():
    home = Path(mkdtemp(prefix="adp-tools-client-"))
    try:
        yield str(home)
    finally:
        release_home(home)


def cleanup_proxies(installed, environ, gateway_url):
    home = Path(environ["HOME"])
    marker = home / "proxy-cleanup-pending.json"
    try:
        stop_owned_proxies(installed, environ, gateway_url)
    except Exception:
        # This marker is only a retention flag, never evidence of termination.
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as output:
            output.write('{"version":1,"status":"unconfirmed"}\n')
        raise
    else:
        marker.unlink(missing_ok=True)


@contextmanager
def installed_clients(gateway_url, revision, *, initial_settings=None, recovery_root=None, recovery_id=None):
    """Use only bytes from the selected release; never touch the operator's home."""
    if not re.fullmatch(r"https?://[a-zA-Z0-9.:-]+(?:/api)?/?", gateway_url):
        raise ValueError("a gateway origin without credentials or query parameters is required")
    api_url = gateway_url.rstrip("/")
    if not api_url.endswith("/api"):
        api_url += "/api"
    expected_installer = source_file(revision, "install.sh")
    with urlopen(f"{api_url}/cli/install.sh", timeout=20) as response:
        served_installer = response.read(1024 * 1024 + 1)
    if digest(served_installer) != digest(expected_installer):
        raise ValueError("served CLI installer differs from pinned gateway source")
    file_list = re.search(rb'^CLI_FILES="([a-zA-Z0-9_. -]+)"$', expected_installer, re.MULTILINE)
    if not file_list:
        raise ValueError("pinned installer has no verifiable CLI file list")
    names = file_list.group(1).decode().split()
    expected_hashes = {name: digest(source_file(revision, name)) for name in names}
    if recovery_root is not None:
        root = Path(recovery_root)
        metadata = root.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise ValueError("recovery root must be an owned private directory")
        if not recovery_id or str(UUID(recovery_id)) != recovery_id:
            raise ValueError("run-owned recovery ID must be a canonical UUID")
        scope = persistent_home(root, recovery_id)
    else:
        if recovery_id is not None:
            raise ValueError("recovery ID requires a private recovery root")
        scope = temporary_home()
    with scope as directory:
        home = Path(directory)
        home.chmod(0o700)
        for name, content in (initial_settings or {}).items():
            if name not in (".claude.json", ".codex/config.toml"):
                raise ValueError("only isolated client settings can be initialized")
            setting = home / name
            setting.parent.mkdir(parents=True, exist_ok=True)
            setting.write_text(content)
        installer = home / "install.sh"
        installer.write_bytes(served_installer)
        install_dir = home / ".adp" / "bin"
        environ = {key: value for key, value in os.environ.items()
                   if not key.startswith(("ADP_", "BG_", "CODEX_", "CLAUDE_", "SIGV4_"))}
        environ.update(HOME=str(home), ADP_HOME=str(home / ".adp"),
                       BG_CONFIG_DIR=str(home / ".bedrock-gateway"),
                       CODEX_HOME=str(home / ".codex"),
                       PATH=f"{install_dir}{os.pathsep}{os.environ.get('PATH', '')}")
        run_client(["sh", str(installer), "--prefix", str(install_dir),
                    "--no-path-edit", "--gateway-url", api_url], environ)
        observed_hashes = {name: digest((install_dir / name).read_bytes()) for name in names}
        if observed_hashes != expected_hashes:
            raise ValueError("installed CLI files differ from pinned gateway source")
        installed = install_dir / "adp"
        manifest = json.loads((install_dir / ".adp-manifest.json").read_text())
        if manifest.get("gateway_url") != api_url:
            raise ValueError("installed CLI targets a different gateway")
        for client in ("claude-code", "codex"):
            run_client([str(installed), "mcp", "configure", "--client", client], environ)
        claude = json.loads((home / ".claude.json").read_text())
        codex = (home / ".codex" / "config.toml").read_text()
        expected = {"command": str(installed), "args": ["--deployment", "default", "mcp"],
                    "env": {"BG_CONFIG_DIR": str(home / ".bedrock-gateway"),
                            "ADP_DEPLOYMENT_ID": "default", "ADP_DEPLOYMENT_URL": api_url}}
        claude_entry = claude.get("mcpServers", {}).get("adp-tools")
        codex_entry = tomllib.loads(codex).get("mcp_servers", {}).get("adp-tools")
        for client, entry in (("Claude Code", claude_entry), ("Codex", codex_entry)):
            if (not isinstance(entry, dict) or entry.get("command") != expected["command"]
                    or entry.get("args") != expected["args"] or entry.get("env") != expected["env"]):
                raise ValueError(f"{client} does not use the pinned installed CLI")
        try:
            yield installed, environ, observed_hashes
        finally:
            cleanup_proxies(installed, environ, api_url)


def configure_model_transport(installed, environ):
    """Use the installed ADP model-auth setup, not ambient native API keys."""
    for client in ("claude", "codex"):
        run_client([str(installed), client, "setup"], environ)
    home = Path(environ["HOME"])
    gateway = json.loads((Path(environ["BG_CONFIG_DIR"]) / "config.json").read_text())["gateway_url"]
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    if (settings.get("env", {}).get("ANTHROPIC_BEDROCK_BASE_URL") != gateway or
            settings.get("env", {}).get("CLAUDE_CODE_USE_BEDROCK") != "1" or
            settings.get("env", {}).get("CLAUDE_CODE_SKIP_BEDROCK_AUTH") != "1" or
            str(installed) not in settings.get("apiKeyHelper", "")):
        raise ValueError("Claude model transport is not bound to the installed ADP CLI")
    codex = (home / ".codex" / "config.toml").read_text()
    if (not re.search(r'^model_provider = "adp-gateway"$', codex, re.MULTILINE) or
            not re.search(r'^base_url = "http://127\.0\.0\.1:[0-9]+/openai/v1"$', codex, re.MULTILINE) or
            not re.search(r'^env_key = "ADP_GATEWAY_DUMMY"$', codex, re.MULTILINE)):
        raise ValueError("Codex model transport is not bound to the installed ADP proxy")


@contextmanager
def resume_installed_clients(gateway_url, revision, *, recovery_root, recovery_id):
    """Reopen the original private operation store without a new install or request ID."""
    root = Path(recovery_root)
    metadata = root.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        raise ValueError("recovery root must be an owned private directory")
    if str(UUID(recovery_id)) != recovery_id:
        raise ValueError("recovery ID must be canonical")
    home = root / recovery_id
    metadata = home.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        raise ValueError("original recovery home is not private")
    store = home / ".bedrock-gateway" / "tool-operations"
    metadata = store.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_mode & 0o077:
        raise ValueError("original operation store is not private")
    api_url = gateway_url.rstrip("/")
    if not api_url.endswith("/api"):
        api_url += "/api"
    install_dir = home / ".adp" / "bin"
    manifest = json.loads((install_dir / ".adp-manifest.json").read_text())
    config = json.loads((home / ".bedrock-gateway" / "config.json").read_text())
    if manifest.get("gateway_url") != api_url or config.get("gateway_url") != api_url:
        raise ValueError("original recovery home targets a different deployment")
    installer = source_file(revision, "install.sh")
    file_list = re.search(rb'^CLI_FILES="([a-zA-Z0-9_. -]+)"$', installer, re.MULTILINE)
    if not file_list:
        raise ValueError("original CLI manifest cannot be verified")
    hashes = {name: digest(source_file(revision, name)) for name in file_list.group(1).decode().split()}
    if any(digest((install_dir / name).read_bytes()) != expected for name, expected in hashes.items()):
        raise ValueError("original recovery CLI bytes differ from the selected release")
    environ = {key: value for key, value in os.environ.items()
               if not key.startswith(("ADP_", "BG_", "CODEX_", "CLAUDE_", "SIGV4_"))}
    environ.update(HOME=str(home), ADP_HOME=str(home / ".adp"),
                   BG_CONFIG_DIR=str(home / ".bedrock-gateway"), CODEX_HOME=str(home / ".codex"),
                   PATH=f"{install_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    try:
        yield install_dir / "adp", environ, hashes
    finally:
        try:
            cleanup_proxies(install_dir / "adp", environ, api_url)
        finally:
            (home / ".bedrock-gateway" / "tokens.json").unlink(missing_ok=True)
