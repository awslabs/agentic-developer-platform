# tests/cli/test_bg_auth.py
"""
Unit tests for bg-auth.sh credential exchange helper.

These tests use a mock HTTP server to simulate the BedrockGateway API
and verify the script's behavior for various scenarios.
"""

from pathlib import Path

from tests.cli.conftest import MockGatewayServer, run_script


class TestBgAuthHelp:
    """Test help and version options."""

    def test_help_option(self, bg_auth_script: Path) -> None:
        """Test --help option displays usage information."""
        result = run_script(bg_auth_script, ["--help"])
        assert result.returncode == 0
        assert "bg-auth" in result.stderr
        assert "Usage:" in result.stderr
        assert "--gateway-url" in result.stderr
        assert "--profile" in result.stderr

    def test_version_option(self, bg_auth_script: Path) -> None:
        """Test --version option displays version."""
        result = run_script(bg_auth_script, ["--version"])
        assert result.returncode == 0
        assert "bg-auth v" in result.stdout


class TestBgAuthConfiguration:
    """Test configuration validation."""

    def test_missing_gateway_url(self, bg_auth_script: Path, mock_aws_credentials: dict[str, str], mock_aws_cli: Path) -> None:
        """Test error when gateway URL is not configured."""
        import os

        # Run without BG_GATEWAY_URL
        env = mock_aws_credentials.copy()
        env.pop("BG_GATEWAY_URL", None)
        # Put mock aws CLI first in PATH so the dependency check passes
        env["PATH"] = f"{mock_aws_cli}:{os.environ.get('PATH', '')}"

        result = run_script(bg_auth_script, env=env)

        assert result.returncode == 3  # Configuration error
        assert "Gateway URL not configured" in result.stderr

    def test_gateway_url_from_env(self, bg_auth_script: Path, mock_aws_credentials: dict[str, str], mock_aws_cli: Path) -> None:
        """Test gateway URL is read from environment variable."""
        import os

        with MockGatewayServer() as server:
            server.set_response(
                "/auth/exchange",
                200,
                {
                    "token": "bg-test-token-123",
                    "expires_at": "2024-01-01T12:00:00Z",
                    "user_id": "user-123",
                    "org_id": "org-123",
                    "team_id": "team-123",
                    "department_id": "dept-123",
                    "account_type": "human",
                },
            )

            env = mock_aws_credentials.copy()
            env["BG_GATEWAY_URL"] = server.url
            # Put mock aws CLI first in PATH so the dependency check passes
            env["PATH"] = f"{mock_aws_cli}:{os.environ.get('PATH', '')}"

            result = run_script(bg_auth_script, env=env)

            # Script should attempt to connect (may fail on AWS creds, but URL is accepted)
            assert "Gateway URL not configured" not in result.stderr

    def test_gateway_url_from_arg(self, bg_auth_script: Path, mock_aws_credentials: dict[str, str], mock_aws_cli: Path) -> None:
        """Test gateway URL can be provided via --gateway-url argument."""
        import os

        with MockGatewayServer() as server:
            server.set_response(
                "/auth/exchange",
                200,
                {
                    "token": "bg-test-token-123",
                    "expires_at": "2024-01-01T12:00:00Z",
                    "user_id": "user-123",
                    "org_id": "org-123",
                    "team_id": "team-123",
                    "department_id": "dept-123",
                    "account_type": "human",
                },
            )

            env = mock_aws_credentials.copy()
            # Don't set BG_GATEWAY_URL in env
            # Put mock aws CLI first in PATH so the dependency check passes
            env["PATH"] = f"{mock_aws_cli}:{os.environ.get('PATH', '')}"

            result = run_script(bg_auth_script, ["--gateway-url", server.url], env=env)

            # URL should be accepted from argument
            assert "Gateway URL not configured" not in result.stderr


class TestBgAuthSuccessfulExchange:
    """Test successful credential exchange scenarios."""

    def test_successful_token_exchange(self, run_bg_auth, mock_gateway: MockGatewayServer) -> None:
        """Test successful credential exchange returns token."""
        expected_token = "bg-test-token-abc123"

        mock_gateway.set_response(
            "/auth/exchange",
            200,
            {
                "token": expected_token,
                "expires_at": "2024-01-01T12:00:00Z",
                "user_id": "user-123",
                "org_id": "org-123",
                "team_id": "team-123",
                "department_id": "dept-123",
                "account_type": "human",
            },
        )

        result = run_bg_auth(mock_gateway.url)

        # Token should be printed to stdout
        assert expected_token in result.stdout.strip()
        # Exit code should be 0 (success)
        assert result.returncode == 0

    def test_request_contains_credentials(self, run_bg_auth, mock_gateway: MockGatewayServer) -> None:
        """Test that request body contains AWS credentials."""
        mock_gateway.set_response(
            "/auth/exchange",
            200,
            {
                "token": "bg-token",
                "expires_at": "2024-01-01T12:00:00Z",
                "user_id": "user-123",
                "org_id": "org-123",
                "team_id": "team-123",
                "department_id": "dept-123",
                "account_type": "human",
            },
        )

        run_bg_auth(mock_gateway.url)

        # Check that a request was made
        assert len(mock_gateway.requests) > 0

        # Find the auth/exchange request
        exchange_requests = [r for r in mock_gateway.requests if r["path"] == "/auth/exchange"]
        assert len(exchange_requests) == 1

        request = exchange_requests[0]
        assert request["method"] == "POST"
        assert "Content-Type" in request["headers"]
        assert "application/json" in request["headers"]["Content-Type"]

        # Check body contains credential fields
        body = request["body"]
        assert "aws_access_key_id" in body
        assert "aws_secret_access_key" in body
        assert "aws_session_token" in body

    def test_service_account_token(self, run_bg_auth, mock_gateway: MockGatewayServer) -> None:
        """Test token exchange for service accounts."""
        mock_gateway.set_response(
            "/auth/exchange",
            200,
            {
                "token": "bg-service-token-xyz",
                "expires_at": "2024-01-01T13:00:00Z",
                "user_id": "service-agent-001",
                "org_id": "org-123",
                "team_id": "team-123",
                "department_id": "dept-123",
                "account_type": "service",
            },
        )

        result = run_bg_auth(mock_gateway.url)

        assert "bg-service-token-xyz" in result.stdout
        assert result.returncode == 0


class TestBgAuthErrorHandling:
    """Test error handling scenarios."""

    def test_401_invalid_credentials(self, run_bg_auth, mock_gateway: MockGatewayServer) -> None:
        """Test handling of 401 unauthorized response."""
        mock_gateway.set_response(
            "/auth/exchange",
            401,
            {"error": "invalid_credentials", "message": "AWS credentials are invalid"},
        )

        result = run_bg_auth(mock_gateway.url)

        assert result.returncode == 1  # Credential error
        assert "Authentication failed" in result.stderr

    def test_403_unknown_organization(self, run_bg_auth, mock_gateway: MockGatewayServer) -> None:
        """Test handling of 403 unknown organization response."""
        mock_gateway.set_response(
            "/auth/exchange",
            403,
            {
                "error": "unknown_organization",
                "message": "AWS account 123456789012 is not registered with any organization.",
            },
        )

        result = run_bg_auth(mock_gateway.url)

        assert result.returncode == 2  # Auth failed
        assert "not registered" in result.stderr

    def test_403_unregistered_service_account(self, run_bg_auth, mock_gateway: MockGatewayServer) -> None:
        """Test handling of 403 unregistered service account response."""
        mock_gateway.set_response(
            "/auth/exchange",
            403,
            {
                "error": "unregistered_service_account",
                "message": "Agent not registered. Contact your org administrator.",
            },
        )

        result = run_bg_auth(mock_gateway.url)

        assert result.returncode == 2
        assert "not registered" in result.stderr.lower()

    def test_429_rate_limited(self, run_bg_auth, mock_gateway: MockGatewayServer) -> None:
        """Test handling of 429 rate limit response."""
        mock_gateway.set_response(
            "/auth/exchange",
            429,
            {"error": "rate_limited", "retry_after_seconds": 30},
        )

        result = run_bg_auth(mock_gateway.url)

        assert result.returncode == 2
        assert "Rate limited" in result.stderr

    def test_500_server_error(self, run_bg_auth, mock_gateway: MockGatewayServer) -> None:
        """Test handling of 500 server error response."""
        mock_gateway.set_response("/auth/exchange", 500, {"error": "internal_error"})

        result = run_bg_auth(mock_gateway.url)

        assert result.returncode == 4  # Network/server error
        assert "server error" in result.stderr.lower()

    def test_503_service_unavailable(self, run_bg_auth, mock_gateway: MockGatewayServer) -> None:
        """Test handling of 503 service unavailable response."""
        mock_gateway.set_response("/auth/exchange", 503, {"error": "service_unavailable"})

        result = run_bg_auth(mock_gateway.url)

        assert result.returncode == 4
        assert "server error" in result.stderr.lower()

    def test_invalid_json_response(self, run_bg_auth, mock_gateway: MockGatewayServer) -> None:
        """Test handling when response doesn't contain expected token field."""
        mock_gateway.set_response(
            "/auth/exchange",
            200,
            {"unexpected": "response"},  # Missing token field
        )

        result = run_bg_auth(mock_gateway.url)

        assert result.returncode == 2  # Auth failed
        assert "no token in response" in result.stderr.lower() or "Invalid response" in result.stderr


class TestBgAuthDebugMode:
    """Test debug mode functionality."""

    def test_debug_flag_enables_verbose_output(self, run_bg_auth, mock_gateway: MockGatewayServer) -> None:
        """Test --debug flag produces debug output."""
        mock_gateway.set_response(
            "/auth/exchange",
            200,
            {
                "token": "bg-debug-token",
                "expires_at": "2024-01-01T12:00:00Z",
                "user_id": "user-123",
                "org_id": "org-123",
                "team_id": "team-123",
                "department_id": "dept-123",
                "account_type": "human",
            },
        )

        result = run_bg_auth(mock_gateway.url, extra_args=["--debug"])

        # Debug output should contain [DEBUG] markers
        assert "[DEBUG]" in result.stderr

    def test_no_debug_output_by_default(self, run_bg_auth, mock_gateway: MockGatewayServer) -> None:
        """Test that debug output is not shown by default."""
        mock_gateway.set_response(
            "/auth/exchange",
            200,
            {
                "token": "bg-token",
                "expires_at": "2024-01-01T12:00:00Z",
                "user_id": "user-123",
                "org_id": "org-123",
                "team_id": "team-123",
                "department_id": "dept-123",
                "account_type": "human",
            },
        )

        result = run_bg_auth(mock_gateway.url)

        # Should not have debug markers
        assert "[DEBUG]" not in result.stderr


class TestBgAuthNetworkErrors:
    """Test network error handling."""

    def test_connection_refused(self, run_bg_auth) -> None:
        """Test handling when gateway is unreachable."""
        # Use a URL that won't connect
        result = run_bg_auth("http://127.0.0.1:1")

        assert result.returncode == 4  # Network error
        assert "connect" in result.stderr.lower() or "network" in result.stderr.lower()


class TestBgAuthTrailingSlash:
    """Test URL normalization."""

    def test_gateway_url_trailing_slash_removed(self, run_bg_auth, mock_gateway: MockGatewayServer) -> None:
        """Test that trailing slash in URL is handled correctly."""
        mock_gateway.set_response(
            "/auth/exchange",
            200,
            {
                "token": "bg-token",
                "expires_at": "2024-01-01T12:00:00Z",
                "user_id": "user-123",
                "org_id": "org-123",
                "team_id": "team-123",
                "department_id": "dept-123",
                "account_type": "human",
            },
        )

        # Pass URL with trailing slash
        result = run_bg_auth(mock_gateway.url + "/")

        # Should still work
        assert result.returncode == 0
        assert "bg-token" in result.stdout


class TestInstallScript:
    """Test install.sh script."""

    def test_install_help(self, install_script: Path) -> None:
        """Test install.sh --help shows usage."""
        result = run_script(install_script, ["--help"])
        assert result.returncode == 0
        assert "Install Bedrock Gateway CLI tools" in result.stderr or "Install Bedrock Gateway CLI tools" in result.stdout

    def test_install_version(self, install_script: Path) -> None:
        """Test install.sh --version shows version."""
        result = run_script(install_script, ["--version"])
        assert result.returncode == 0
        assert "v1.0.0" in result.stdout


class TestClaudeSettingsExample:
    """Test claude-settings.example.json is valid JSON."""

    def test_settings_is_valid_json(self, cli_dir: Path) -> None:
        """Test that claude-settings.example.json is valid JSON."""
        import json

        settings_file = cli_dir / "claude-settings.example.json"
        assert settings_file.exists()

        # Should parse without error
        with open(settings_file) as f:
            settings = json.load(f)

        # Check key fields exist
        assert "env" in settings
        assert "apiKeyHelper" in settings
        assert "ANTHROPIC_BEDROCK_BASE_URL" in settings["env"]
        assert "CLAUDE_CODE_USE_BEDROCK" in settings["env"]
        assert "CLAUDE_CODE_SKIP_BEDROCK_AUTH" in settings["env"]
        assert "BG_GATEWAY_URL" in settings["env"]
