# tests/cli/conftest.py
"""
Pytest configuration and fixtures for CLI tests.

Since CLI tools are shell scripts, we use Python subprocess to test them
with mock HTTP responses via a simple mock server.

For tests that require AWS credential validation, we create a mock aws CLI
that returns fake successful responses.
"""

import base64
import json
import os
import stat
import subprocess
import threading
import time
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


@pytest.fixture
def bg_cognito_auth_script(cli_dir: Path) -> Path:
    """Return the path to bg-cognito-auth.sh script (Issue #4145)."""
    return cli_dir / "bg-cognito-auth.sh"


ADP_GATEWAY_URL = "https://gw.example.com/api"


def write_adp_config(home: Path, **extra: str) -> None:
    """Seed ~/.bedrock-gateway/config.json the way install.sh/login would."""
    config_dir = home / ".bedrock-gateway"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.json").write_text(json.dumps({"gateway_url": ADP_GATEWAY_URL, **extra}))


def write_adp_session(home: Path, username: str = "github_alice", ttl: int = 3600) -> None:
    """Seed a valid-looking token store (config + tokens), as `adp login` would.

    The access token is a real (unsigned) JWT so `status` can decode the username
    claim out of it — that decode is one of the things under test.
    """
    write_adp_config(home)

    def b64(obj: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    access_token = f"{b64({'alg': 'none'})}.{b64({'username': username, 'sub': 'sub-123'})}.sig"
    (home / ".bedrock-gateway" / "tokens.json").write_text(
        json.dumps(
            {
                "id_token": "id-token",
                "access_token": access_token,
                "refresh_token": "refresh-token",
                "expires_at": int(time.time()) + ttl,
            }
        )
    )


@pytest.fixture
def adp_script(cli_dir: Path) -> Path:
    """Return the path to the `adp` wrapper (Issue #4852)."""
    return cli_dir / "adp"


@pytest.fixture
def adp_home(tmp_path: Path) -> Path:
    """Sandboxed HOME for `adp` runs (Issue #4852).

    Every `adp` test needs this: the wrapper writes ~/.codex/config.toml and
    ~/.claude/settings.json, and a test that leaked into the real home dir would
    rewrite the developer's own tool configuration.
    """
    home = tmp_path / "home"
    home.mkdir()
    return home


@pytest.fixture
def adp_bin(cli_dir: Path, tmp_path: Path) -> Path:
    """An installed prefix holding `adp` + the two files it wraps.

    Copies rather than symlinks: `adp` resolves its core helper and the proxy as
    siblings of its own real path, so a symlink farm would not exercise the
    layout install.sh actually produces.
    """
    bin_dir = tmp_path / "adp-bin"
    bin_dir.mkdir()
    for name in ("adp", "bg-cognito-auth.sh", "bg-gateway-proxy.py", "adp_common.py", "adp-admin.py", "adp-bedrock.py", "adp-aws.py"):
        target = bin_dir / name
        target.write_bytes((cli_dir / name).read_bytes())
        target.chmod(0o755)
    return bin_dir


@pytest.fixture
def run_adp(adp_bin: Path, adp_home: Path):
    """Run the installed `adp` with a sandboxed HOME.

    PATH deliberately does NOT contain the install dir: `adp` must resolve its
    core helper as a sibling of itself, not via a PATH lookup that could find an
    unrelated copy.
    """

    def _run(args: list[str], extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["HOME"] = str(adp_home)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["bash", str(adp_bin / "adp"), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

    return _run


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
            return

        # Issue #4145: serve configured GET responses (e.g. the
        # /.well-known/cognito-config discovery document the CLI helper fetches).
        response_config = self.__class__.mock_responses.get(self.path)
        if response_config is None:
            self.send_response(404)
            self.end_headers()
            return

        self.send_response(response_config.get("status", 200))
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(response_config.get("body", {})).encode("utf-8"))


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

# Handle both public import and admin token refresh (REFRESH_TOKEN_AUTH).
# Refresh prefers admin-initiate-auth when a pool id is configured; letting
# that command fall through invokes real AWS with the fixture credentials.
# Behaviour is driven by env vars so tests can force each failure mode:
#   MOCK_COGNITO_RESULT=ok|notauthorized|other|no_tokens  (default: ok)
#   MOCK_AWS_LOG=<path>  appends the full argv for assertions
if [[ "$1" == "cognito-idp" && ( "$2" == "initiate-auth" || "$2" == "admin-initiate-auth" ) ]]; then
    if [[ -n "${MOCK_AWS_LOG:-}" ]]; then
        echo "$*" >> "${MOCK_AWS_LOG}"
    fi
    case "${MOCK_COGNITO_RESULT:-ok}" in
        notauthorized)
            echo "An error occurred (NotAuthorizedException) when calling the InitiateAuth operation: Invalid Refresh Token" >&2
            exit 254
            ;;
        other)
            echo "An error occurred (ResourceNotFoundException) when calling the InitiateAuth operation: User pool client does not exist" >&2
            exit 254
            ;;
        no_tokens)
            echo '{"ChallengeParameters": {}}'
            exit 0
            ;;
        *)
            echo '{"AuthenticationResult": {"IdToken": "mock.id.token", "AccessToken": "mock.access.token", "ExpiresIn": 3600, "TokenType": "Bearer"}}'
            exit 0
            ;;
    esac
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
    stdin_data: str | None = None,
) -> subprocess.CompletedProcess:
    """Run a shell script and capture output.

    ``stdin_data`` (Issue #4145) feeds the process stdin — needed to test the
    CLI helper's stdin refresh-token path.
    """
    cmd = ["bash", str(script_path)]
    if args:
        cmd.extend(args)

    # Merge environment
    full_env = os.environ.copy()
    if env:
        full_env.update(env)

    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=full_env,
        timeout=timeout,
        input=stdin_data if stdin_data is not None else "",
    )


@pytest.fixture
def cognito_home(tmp_path: Path) -> Path:
    """Sandboxed HOME for bg-cognito-auth.sh runs (Issue #4145)."""
    home = tmp_path / "home"
    home.mkdir()
    return home


@pytest.fixture
def run_bg_cognito_auth(bg_cognito_auth_script: Path, mock_aws_cli: Path, cognito_home: Path):
    """Run bg-cognito-auth.sh with a sandboxed HOME and the mock aws CLI.

    Issue #4145. HOME is redirected into tmp_path so the helper's
    ``~/.bedrock-gateway`` and ``~/.aws`` writes never touch the real home dir.
    """

    def _run(
        args: list[str] | None = None,
        extra_env: dict[str, str] | None = None,
        stdin_data: str | None = None,
    ) -> subprocess.CompletedProcess:
        env = {
            "HOME": str(cognito_home),
            "PATH": f"{mock_aws_cli}:{os.environ.get('PATH', '')}",
        }
        if extra_env:
            env.update(extra_env)
        return run_script(bg_cognito_auth_script, args, env, stdin_data=stdin_data)

    return _run


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
