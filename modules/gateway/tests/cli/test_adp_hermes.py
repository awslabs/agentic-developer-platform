"""Hermes transport regression tests with real proxies and local gateway stand-ins."""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from . import test_adp_launch as launcher_suite

# Reuse the installed-CLI and real-proxy harness.
fake_launchd = launcher_suite.fake_launchd
launch = launcher_suite.launch
proxy_port = launcher_suite.proxy_port
stub_tools = launcher_suite.stub_tools


@pytest.fixture
def gateways():
    servers = []
    threads = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 -- BaseHTTPRequestHandler API
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = json.dumps({"gateway": self.server.label}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    for label in ("dev", "integration"):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.label = label
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append(server)
        threads.append(thread)
    yield servers
    for server in servers:
        server.shutdown()
        server.server_close()
    for thread in threads:
        thread.join(timeout=5)


def test_hermes_selected_deployment_receives_request(launch, adp_home, fake_launchd, gateways):
    """Read the written Hermes config and issue a request through real proxies."""
    environment = {"ADP_PROXY_PORT": "", "SHELL": "/bin/zsh"}
    for server in gateways:
        launcher_suite.TestDaemonInstall._seed_named_deployment(launch, adp_home, server.label, server.server_port)
        for command in (["hermes", "setup"], ["daemon", "install"]):
            result = launch(["--deployment", server.label, *command], extra_env=environment)
            assert result.returncode == 0, result.stderr

    # The Hermes stand-in reads its config and expands the environment just as
    # Hermes does. Real proxies share a durable capability, so that capability
    # alone cannot distinguish these two deployments.
    environment["STUB_HERMES_REQUEST"] = "1"
    try:
        result = launch(["--deployment", "dev", "hermes"], extra_env=environment)
        assert result.returncode == 0, result.stderr
        actual = json.loads(result.stdout.strip().splitlines()[-1])["gateway"]
        assert actual == "dev", "Hermes sent the request to a different deployment"
        # Switching deployments must work without rewriting shared config.
        result = launch(["--deployment", "integration", "hermes"], extra_env=environment)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout.strip().splitlines()[-1])["gateway"] == "integration"
    finally:
        for server in gateways:
            launch(["--deployment", server.label, "daemon", "uninstall"], extra_env=environment)


def test_hermes_setup_with_documented_python_prerequisites(launch, adp_home, stub_tools):
    """A Python interpreter without site packages meets the published prerequisites."""
    launcher_suite._seed_valid_session(adp_home)
    python = stub_tools / "python3"
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" -S "$@"\n')
    python.chmod(0o755)
    result = launch(["hermes", "setup"])
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "change",
    [
        {"base_url": "http://127.0.0.1:9192/v1"},
        {"provider": "openrouter"},
        {"key_env": "OTHER_KEY"},
        {"api_key": "stale-key"},
        {"api_mode": "responses"},
    ],
)
def test_stale_transport_fails_before_starting_proxy(launch, adp_home, change):
    launcher_suite._seed_valid_session(adp_home)
    config = adp_home / ".hermes" / "config.yaml"
    data = json.loads(config.read_text())
    data["model"].update(change)
    config.write_text(json.dumps(data))

    result = launch(["hermes"])

    assert result.returncode != 0
    assert "adp hermes setup" in result.stderr
    assert not launch.record.tools
    assert not launcher_suite._port_is_open(launch.port)
    assert "stale-key" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "args",
    [
        ["--provider", "openrouter"],
        ["--provider=custom"],
        ["--profile", "another"],
        ["--profile=another"],
        ["-panother"],
        ["chat", "--ignore-user-config"],
        ["chat", "--vanilla"],
    ],
)
def test_transport_overrides_are_rejected(launch, adp_home, args):
    launcher_suite._seed_valid_session(adp_home)
    result = launch(["hermes", *args])
    assert result.returncode != 0
    assert "ADP manages the Hermes provider" in result.stderr
    assert not launch.record.tools
    assert not launcher_suite._port_is_open(launch.port)


def test_custom_hermes_home_is_used_for_setup_and_launch(launch, adp_home, tmp_path):
    launcher_suite._seed_valid_session(adp_home)
    default_config = adp_home / ".hermes" / "config.yaml"
    original = default_config.read_bytes()
    custom_home = tmp_path / "custom-hermes-home"
    environment = {"HERMES_HOME": str(custom_home)}

    configured = launch(["hermes", "setup"], extra_env=environment)
    assert configured.returncode == 0, configured.stderr
    assert (custom_home / "config.yaml").exists()
    result = launch(["hermes", "--model", "sonnet45"], extra_env=environment)
    assert result.returncode == 0, result.stderr
    assert launch.record.tools == ["hermes"]
    assert default_config.read_bytes() == original


def test_missing_or_invalid_config_is_not_overwritten(launch, adp_home):
    launcher_suite._seed_valid_session(adp_home)
    config = adp_home / ".hermes" / "config.yaml"
    config.unlink()
    result = launch(["hermes"])
    assert result.returncode != 0
    assert "adp hermes setup" in result.stderr
    assert not config.exists()

    config.write_text("[invalid yaml")
    result = launch(["hermes", "setup"])
    assert result.returncode != 0
    assert config.read_text() == "[invalid yaml"
    assert not launch.record.tools
