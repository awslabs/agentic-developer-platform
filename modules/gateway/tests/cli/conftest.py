# tests/cli/conftest.py
"""
Pytest configuration and fixtures for CLI tests.

Since CLI tools are shell scripts, we use Python subprocess to test them
with mock HTTP responses via a simple mock server.

For tests that require AWS credential validation, we create a mock aws CLI
that returns fake successful responses.
"""

import json
import os
import stat
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def cli_dir() -> Path:
    """Return the path to the cli directory."""
    return Path(__file__).parent.parent.parent / "cli"


@pytest.fixture
def bg_auth_script(cli_dir: Path) -> Path:
    """Return the path to bg-auth.sh script."""
    return cli_dir / "bg-auth.sh"


@pytest.fixture
def install_script(cli_dir: Path) -> Path:
    """Return the path to install.sh script."""
    return cli_dir / "install.sh"


class MockGatewayHandler(BaseHTTPRequestHandler):
    """HTTP request handler for mock gateway responses."""

    # Class-level configuration for mock responses
    mock_responses: dict[str, dict[str, Any]] = {}
    request_log: list[dict[str, Any]] = []

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress default logging."""
        pass

    def do_POST(self) -> None:
        """Handle POST requests."""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")

        # Log the request
        self.__class__.request_log.append(
            {
                "method": "POST",
                "path": self.path,
                "headers": dict(self.headers),
                "body": json.loads(body) if body else None,
            }
        )

        # Get mock response for this path
        response_config = self.__class__.mock_responses.get(self.path, {"status": 404, "body": {"error": "not_found"}})

        status = response_config.get("status", 200)
        body_response = response_config.get("body", {})

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body_response).encode("utf-8"))

    def do_GET(self) -> None:
        """Handle GET requests (for health checks)."""
        self.__class__.request_log.append({"method": "GET", "path": self.path, "headers": dict(self.headers)})

        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "healthy"}).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()


class MockGatewayServer:
    """Context manager for running a mock gateway server."""

    def __init__(self, port: int = 0):
        self.port = port
        self.server: HTTPServer | None = None
        self.thread: threading.Thread | None = None

    def __enter__(self) -> "MockGatewayServer":
        # Reset class-level state
        MockGatewayHandler.mock_responses = {}
        MockGatewayHandler.request_log = []

        # Create server with available port
        self.server = HTTPServer(("127.0.0.1", self.port), MockGatewayHandler)
        self.port = self.server.server_address[1]

        # Start server in background thread
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()

        return self

    def __exit__(self, *args: Any) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def set_response(self, path: str, status: int, body: dict[str, Any]) -> None:
        """Configure mock response for a path."""
        MockGatewayHandler.mock_responses[path] = {"status": status, "body": body}

    @property
    def requests(self) -> list[dict[str, Any]]:
        """Get all logged requests."""
        return MockGatewayHandler.request_log


@pytest.fixture
def mock_gateway():
    """Fixture that provides a mock gateway server."""
    with MockGatewayServer() as server:
        yield server


@pytest.fixture
def mock_aws_cli(tmp_path: Path) -> Path:
    """
    Create a mock AWS CLI that returns success for STS calls.

    This allows testing the credential exchange flow without
    actually calling AWS STS.
    """
    mock_bin_dir = tmp_path / "mock_bin"
    mock_bin_dir.mkdir()

    mock_aws_script = mock_bin_dir / "aws"
    mock_aws_script.write_text("""#!/bin/bash
# Mock AWS CLI for testing

# Handle STS get-caller-identity
if [[ "$1" == "sts" && "$2" == "get-caller-identity" ]]; then
    echo '{"UserId": "AIDAIOSFODNN7EXAMPLE:user@example.com", "Account": "123456789012", "Arn": "arn:aws:sts::123456789012:assumed-role/TestRole/user@example.com"}'
    exit 0
fi

# Handle configure export-credentials
if [[ "$1" == "configure" && "$2" == "export-credentials" ]]; then
    echo "export AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"
    echo "export AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    echo "export AWS_SESSION_TOKEN=FwoGZXIvYXdzEBYaDMOCKEXAMPLE"
    exit 0
fi

# Handle configure get
if [[ "$1" == "configure" && "$2" == "get" ]]; then
    case "$3" in
        aws_access_key_id)
            echo "AKIAIOSFODNN7EXAMPLE"
            ;;
        aws_secret_access_key)
            echo "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
            ;;
        aws_session_token)
            echo "FwoGZXIvYXdzEBYaDMOCKEXAMPLE"
            ;;
    esac
    exit 0
fi

# Default: pass through to real aws if needed
exec /usr/local/bin/aws "$@"
""")

    # Make executable
    mock_aws_script.chmod(mock_aws_script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    return mock_bin_dir


@pytest.fixture
def mock_aws_credentials(tmp_path: Path) -> dict[str, str]:
    """Create mock AWS credentials directory and files."""
    aws_dir = tmp_path / ".aws"
    aws_dir.mkdir()

    # Create credentials file
    credentials_file = aws_dir / "credentials"
    credentials_file.write_text(
        """[default]
aws_access_key_id = AKIAIOSFODNN7EXAMPLE
aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
aws_session_token = FwoGZXIvYXdzEBYaDMOCKEXAMPLE
"""
    )

    # Create config file
    config_file = aws_dir / "config"
    config_file.write_text(
        """[default]
region = us-east-1
output = json
"""
    )

    return {
        "AWS_CONFIG_FILE": str(config_file),
        "AWS_SHARED_CREDENTIALS_FILE": str(credentials_file),
        "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
        "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "AWS_SESSION_TOKEN": "FwoGZXIvYXdzEBYaDMOCKEXAMPLE",
    }


def run_script(
    script_path: Path,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess:
    """Run a shell script and capture output."""
    cmd = ["bash", str(script_path)]
    if args:
        cmd.extend(args)

    # Merge environment
    full_env = os.environ.copy()
    if env:
        full_env.update(env)

    return subprocess.run(cmd, capture_output=True, text=True, env=full_env, timeout=timeout)


@pytest.fixture
def run_bg_auth(bg_auth_script: Path, mock_aws_credentials: dict[str, str], mock_aws_cli: Path):
    """
    Fixture that returns a function to run bg-auth.sh with mock credentials.

    Uses a mock AWS CLI to bypass real STS calls.
    """

    def _run(
        gateway_url: str,
        extra_args: list[str] | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        env = mock_aws_credentials.copy()
        env["BG_GATEWAY_URL"] = gateway_url
        # Put mock aws CLI first in PATH
        env["PATH"] = f"{mock_aws_cli}:{os.environ.get('PATH', '')}"
        if extra_env:
            env.update(extra_env)

        args = extra_args or []
        return run_script(bg_auth_script, args, env)

    return _run
