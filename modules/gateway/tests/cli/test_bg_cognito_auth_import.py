# tests/cli/test_bg_cognito_auth_import.py
"""
Unit tests for the `bg-cognito-auth.sh import` subcommand (Issue #4145).

`import` seeds the CLI token store from a refresh token the browser login
already holds, so a GitHub-provisioned user (random Cognito password they never
see) can authenticate Claude Code / Codex without a password.

The tests drive the real shell script against:
- a mock `aws` CLI (conftest `mock_aws_cli`) that answers
  `cognito-idp initiate-auth`, with the outcome selected by MOCK_COGNITO_RESULT,
- a mock gateway HTTP server serving `/.well-known/cognito-config`,
- a sandboxed HOME so nothing touches the developer's real ~/.bedrock-gateway.
"""

import json
import stat
import subprocess
from pathlib import Path

import pytest

from tests.cli.conftest import MockGatewayServer

REFRESH_TOKEN = "eyJraWQiOiJtb2NrLXJlZnJlc2gtdG9rZW4tdmFsdWUtNDE0NSJ9.secret-refresh-material"

DISCOVERY_PATH = "/.well-known/cognito-config"
DISCOVERY_BODY = {
    "user_pool_id": "us-east-1_disco123",
    "client_id": "discoveredclientid0123456789",
    "identity_pool_id": "",
    "region": "us-west-2",
}


def _config(home: Path) -> dict:
    return json.loads((home / ".bedrock-gateway" / "config.json").read_text())


def _tokens(home: Path) -> dict:
    return json.loads((home / ".bedrock-gateway" / "tokens.json").read_text())


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def gateway_with_discovery():
    """Mock gateway serving the Cognito discovery document."""
    with MockGatewayServer() as server:
        server.set_response(DISCOVERY_PATH, 200, DISCOVERY_BODY)
        yield server


class TestImportHappyPath:
    """Successful seeding writes a usable config + token store."""

    def test_import_with_flag_writes_config_and_tokens(self, run_bg_cognito_auth, cognito_home: Path, gateway_with_discovery) -> None:
        result = run_bg_cognito_auth(
            [
                "import",
                "--gateway-url",
                gateway_with_discovery.url,
                "--refresh-token",
                REFRESH_TOKEN,
            ]
        )

        assert result.returncode == 0, result.stderr

        tokens = _tokens(cognito_home)
        assert set(tokens) == {"id_token", "access_token", "refresh_token", "expires_at"}
        assert tokens["access_token"] == "mock.access.token"
        assert tokens["id_token"] == "mock.id.token"
        # REFRESH_TOKEN_AUTH returns no new refresh token — the supplied one persists.
        assert tokens["refresh_token"] == REFRESH_TOKEN
        # save_tokens computes an absolute expiry from ExpiresIn (3600).
        assert isinstance(tokens["expires_at"], int)

        config = _config(cognito_home)
        assert config["client_id"] == DISCOVERY_BODY["client_id"]
        assert config["region"] == DISCOVERY_BODY["region"]
        assert config["user_pool_id"] == DISCOVERY_BODY["user_pool_id"]
        assert config["gateway_url"] == gateway_with_discovery.url

    def test_import_reads_refresh_token_from_stdin(self, run_bg_cognito_auth, cognito_home: Path, gateway_with_discovery) -> None:
        """The recommended path: no token in argv, pasted/piped on stdin."""
        result = run_bg_cognito_auth(
            ["import", "--gateway-url", gateway_with_discovery.url],
            stdin_data=f"{REFRESH_TOKEN}\n",
        )

        assert result.returncode == 0, result.stderr
        assert _tokens(cognito_home)["refresh_token"] == REFRESH_TOKEN

    def test_file_modes_are_restrictive(self, run_bg_cognito_auth, cognito_home: Path, gateway_with_discovery) -> None:
        """Config dir 0700, both credential files 0600."""
        result = run_bg_cognito_auth(
            ["import", "--gateway-url", gateway_with_discovery.url],
            stdin_data=REFRESH_TOKEN,
        )
        assert result.returncode == 0, result.stderr

        config_dir = cognito_home / ".bedrock-gateway"
        assert _mode(config_dir) == 0o700
        assert _mode(config_dir / "config.json") == 0o600
        assert _mode(config_dir / "tokens.json") == 0o600

    def test_import_writes_no_aws_credentials(self, run_bg_cognito_auth, cognito_home: Path, gateway_with_discovery) -> None:
        """No Identity Pool exchange: ~/.aws/credentials must not be written."""
        result = run_bg_cognito_auth(
            ["import", "--gateway-url", gateway_with_discovery.url],
            stdin_data=REFRESH_TOKEN,
        )
        assert result.returncode == 0, result.stderr
        assert not (cognito_home / ".aws" / "credentials").exists()
        assert not (cognito_home / ".aws" / "config").exists()

    def test_identity_pool_id_not_required_and_stored_empty(self, run_bg_cognito_auth, cognito_home: Path, gateway_with_discovery) -> None:
        """`import` never prompts for an Identity Pool ID and persists ""."""
        result = run_bg_cognito_auth(
            ["import", "--gateway-url", gateway_with_discovery.url],
            stdin_data=REFRESH_TOKEN,
        )
        assert result.returncode == 0, result.stderr
        assert _config(cognito_home)["identity_pool_id"] == ""
        assert "Identity Pool" not in result.stdout

    def test_refresh_token_never_appears_in_output(self, run_bg_cognito_auth, gateway_with_discovery) -> None:
        """The long-lived credential must not be echoed on either stream."""
        result = run_bg_cognito_auth(
            ["import", "--gateway-url", gateway_with_discovery.url, "--refresh-token", REFRESH_TOKEN],
        )
        assert result.returncode == 0, result.stderr
        assert REFRESH_TOKEN not in result.stdout
        assert REFRESH_TOKEN not in result.stderr

    def test_success_message_goes_to_stderr(self, run_bg_cognito_auth, gateway_with_discovery) -> None:
        """stdout stays clean so it can be piped; guidance goes to stderr."""
        result = run_bg_cognito_auth(
            ["import", "--gateway-url", gateway_with_discovery.url],
            stdin_data=REFRESH_TOKEN,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""
        assert "imported" in result.stderr

    def test_validation_uses_refresh_token_auth_flow(self, run_bg_cognito_auth, gateway_with_discovery, tmp_path: Path) -> None:
        """One REFRESH_TOKEN_AUTH initiate-auth call with the discovered client/region."""
        log = tmp_path / "aws-calls.log"
        result = run_bg_cognito_auth(
            ["import", "--gateway-url", gateway_with_discovery.url],
            extra_env={"MOCK_AWS_LOG": str(log)},
            stdin_data=REFRESH_TOKEN,
        )
        assert result.returncode == 0, result.stderr

        calls = log.read_text().strip().splitlines()
        assert len(calls) == 1
        assert "--auth-flow REFRESH_TOKEN_AUTH" in calls[0]
        assert f"--client-id {DISCOVERY_BODY['client_id']}" in calls[0]
        assert f"--region {DISCOVERY_BODY['region']}" in calls[0]


class TestImportArgumentHandling:
    """Argument validation and discovery overrides."""

    def test_gateway_url_required(self, run_bg_cognito_auth, cognito_home: Path) -> None:
        result = run_bg_cognito_auth(["import", "--refresh-token", REFRESH_TOKEN])
        assert result.returncode != 0
        assert "Gateway URL is required" in result.stderr
        assert not (cognito_home / ".bedrock-gateway" / "config.json").exists()

    def test_empty_refresh_token_fails(self, run_bg_cognito_auth, cognito_home: Path, gateway_with_discovery) -> None:
        result = run_bg_cognito_auth(["import", "--gateway-url", gateway_with_discovery.url], stdin_data="")
        assert result.returncode != 0
        assert "No refresh token supplied" in result.stderr
        assert not (cognito_home / ".bedrock-gateway" / "tokens.json").exists()

    def test_discovery_unreachable_without_client_id_fails(self, run_bg_cognito_auth, cognito_home: Path) -> None:
        """No discovery endpoint and no --client-id → actionable failure, no writes."""
        with MockGatewayServer() as server:  # serves 404 for the discovery path
            result = run_bg_cognito_auth(
                ["import", "--gateway-url", server.url],
                stdin_data=REFRESH_TOKEN,
            )

        assert result.returncode != 0
        assert "Could not determine Cognito client_id" in result.stderr
        assert not (cognito_home / ".bedrock-gateway" / "config.json").exists()
        assert not (cognito_home / ".bedrock-gateway" / "tokens.json").exists()

    def test_explicit_overrides_bypass_discovery(self, run_bg_cognito_auth, cognito_home: Path) -> None:
        """--client-id + --region succeed even with discovery unreachable."""
        with MockGatewayServer() as server:
            result = run_bg_cognito_auth(
                [
                    "import",
                    "--gateway-url",
                    server.url,
                    "--client-id",
                    "explicitclientid123456",
                    "--region",
                    "eu-west-1",
                ],
                stdin_data=REFRESH_TOKEN,
            )

        assert result.returncode == 0, result.stderr
        config = _config(cognito_home)
        assert config["client_id"] == "explicitclientid123456"
        assert config["region"] == "eu-west-1"

    def test_explicit_client_id_wins_over_discovery(self, run_bg_cognito_auth, cognito_home: Path, gateway_with_discovery) -> None:
        result = run_bg_cognito_auth(
            [
                "import",
                "--gateway-url",
                gateway_with_discovery.url,
                "--client-id",
                "overrideclientid987654",
            ],
            stdin_data=REFRESH_TOKEN,
        )
        assert result.returncode == 0, result.stderr
        config = _config(cognito_home)
        assert config["client_id"] == "overrideclientid987654"
        # region still comes from discovery
        assert config["region"] == DISCOVERY_BODY["region"]

    def test_unknown_option_fails(self, run_bg_cognito_auth) -> None:
        result = run_bg_cognito_auth(["import", "--gateway-url", "https://gw.example.com", "--identity-pool-id", "x"])
        assert result.returncode != 0
        assert "Unknown option" in result.stderr


class TestImportValidatesBeforePersisting:
    """A failed import must not write, and must not clobber a working session."""

    def test_expired_refresh_token_writes_nothing(self, run_bg_cognito_auth, cognito_home: Path, gateway_with_discovery) -> None:
        result = run_bg_cognito_auth(
            ["import", "--gateway-url", gateway_with_discovery.url],
            extra_env={"MOCK_COGNITO_RESULT": "notauthorized"},
            stdin_data=REFRESH_TOKEN,
        )

        assert result.returncode != 0
        assert "invalid or expired" in result.stderr
        assert not (cognito_home / ".bedrock-gateway" / "config.json").exists()
        assert not (cognito_home / ".bedrock-gateway" / "tokens.json").exists()

    def test_other_cognito_error_is_distinguished(self, run_bg_cognito_auth, cognito_home: Path, gateway_with_discovery) -> None:
        result = run_bg_cognito_auth(
            ["import", "--gateway-url", gateway_with_discovery.url],
            extra_env={"MOCK_COGNITO_RESULT": "other"},
            stdin_data=REFRESH_TOKEN,
        )

        assert result.returncode != 0
        assert "Could not validate the refresh token" in result.stderr
        assert "invalid or expired" not in result.stderr
        assert not (cognito_home / ".bedrock-gateway" / "tokens.json").exists()

    def test_response_without_tokens_writes_nothing(self, run_bg_cognito_auth, cognito_home: Path, gateway_with_discovery) -> None:
        """Cognito 200 but no AuthenticationResult → no partial state."""
        result = run_bg_cognito_auth(
            ["import", "--gateway-url", gateway_with_discovery.url],
            extra_env={"MOCK_COGNITO_RESULT": "no_tokens"},
            stdin_data=REFRESH_TOKEN,
        )

        assert result.returncode != 0
        assert "Nothing was written" in result.stderr
        assert not (cognito_home / ".bedrock-gateway" / "tokens.json").exists()

    def test_failed_import_leaves_existing_session_byte_identical(self, run_bg_cognito_auth, cognito_home: Path, gateway_with_discovery) -> None:
        """Clobber-safety: a bad import cannot lock the user out of a working CLI."""
        config_dir = cognito_home / ".bedrock-gateway"
        config_dir.mkdir(mode=0o700)
        existing_config = config_dir / "config.json"
        existing_tokens = config_dir / "tokens.json"
        existing_config.write_text('{"gateway_url": "https://old.example.com", "client_id": "oldclient", "region": "us-east-1"}')
        existing_tokens.write_text('{"access_token": "old.access.token", "expires_at": 99999999999}')

        before_config = existing_config.read_bytes()
        before_tokens = existing_tokens.read_bytes()

        result = run_bg_cognito_auth(
            ["import", "--gateway-url", gateway_with_discovery.url],
            extra_env={"MOCK_COGNITO_RESULT": "notauthorized"},
            stdin_data=REFRESH_TOKEN,
        )

        assert result.returncode != 0
        assert existing_config.read_bytes() == before_config
        assert existing_tokens.read_bytes() == before_tokens

    def test_failure_output_never_leaks_the_token(self, run_bg_cognito_auth, gateway_with_discovery) -> None:
        result = run_bg_cognito_auth(
            ["import", "--gateway-url", gateway_with_discovery.url],
            extra_env={"MOCK_COGNITO_RESULT": "notauthorized"},
            stdin_data=REFRESH_TOKEN,
        )
        assert result.returncode != 0
        assert REFRESH_TOKEN not in result.stdout
        assert REFRESH_TOKEN not in result.stderr


class TestTokenAfterImport:
    """The existing apiKeyHelper path works unchanged after seeding."""

    def test_token_prints_access_token_without_trailing_newline(self, run_bg_cognito_auth, gateway_with_discovery) -> None:
        seed = run_bg_cognito_auth(
            ["import", "--gateway-url", gateway_with_discovery.url],
            stdin_data=REFRESH_TOKEN,
        )
        assert seed.returncode == 0, seed.stderr

        result = run_bg_cognito_auth(["token"])
        assert result.returncode == 0, result.stderr
        assert result.stdout == "mock.access.token"

    def test_token_refreshes_inside_expiry_buffer(self, run_bg_cognito_auth, cognito_home: Path, gateway_with_discovery, tmp_path: Path) -> None:
        """expires_at inside the 300s buffer triggers a refresh before printing."""
        seed = run_bg_cognito_auth(
            ["import", "--gateway-url", gateway_with_discovery.url],
            stdin_data=REFRESH_TOKEN,
        )
        assert seed.returncode == 0, seed.stderr

        token_file = cognito_home / ".bedrock-gateway" / "tokens.json"
        tokens = json.loads(token_file.read_text())
        tokens["access_token"] = "stale.access.token"
        tokens["expires_at"] = 1  # long past
        token_file.write_text(json.dumps(tokens))

        log = tmp_path / "refresh-calls.log"
        result = run_bg_cognito_auth(["token"], extra_env={"MOCK_AWS_LOG": str(log)})

        assert result.returncode == 0, result.stderr
        assert result.stdout == "mock.access.token"
        assert "--auth-flow REFRESH_TOKEN_AUTH" in log.read_text()


class TestExistingCommandsUnchanged:
    """Regression: `import` is purely additive."""

    def test_help_lists_import(self, bg_cognito_auth_script: Path, run_bg_cognito_auth) -> None:
        result = run_bg_cognito_auth(["help"])
        assert result.returncode == 0
        assert "import" in result.stdout
        assert "Import Options" in result.stdout
        # existing commands still documented
        for command in ("login", "refresh", "logout", "status", "token"):
            assert command in result.stdout

    def test_script_syntax_is_valid(self, bg_cognito_auth_script: Path) -> None:
        result = subprocess.run(["bash", "-n", str(bg_cognito_auth_script)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr

    def test_unknown_command_still_fails(self, run_bg_cognito_auth) -> None:
        result = run_bg_cognito_auth(["nonsense"])
        assert result.returncode != 0
        # Pre-existing behaviour: the print_* helpers write to stdout.
        assert "Unknown command" in result.stdout

    def test_status_without_config_reports_not_configured(self, run_bg_cognito_auth) -> None:
        result = run_bg_cognito_auth(["status"])
        assert result.returncode == 0
        assert "Not configured" in result.stdout

    def test_token_without_config_fails(self, run_bg_cognito_auth) -> None:
        result = run_bg_cognito_auth(["token"])
        assert result.returncode != 0
        assert "Not configured" in result.stderr

    def test_logout_after_import_removes_tokens(self, run_bg_cognito_auth, cognito_home: Path, gateway_with_discovery) -> None:
        seed = run_bg_cognito_auth(
            ["import", "--gateway-url", gateway_with_discovery.url],
            stdin_data=REFRESH_TOKEN,
        )
        assert seed.returncode == 0, seed.stderr

        result = run_bg_cognito_auth(["logout"])
        assert result.returncode == 0, result.stderr
        assert not (cognito_home / ".bedrock-gateway" / "tokens.json").exists()

    def test_refresh_after_import_reuses_seeded_config(self, run_bg_cognito_auth, cognito_home: Path, gateway_with_discovery) -> None:
        """`refresh` reads the seeded config and refreshes the token store.

        `refresh` then continues into the Identity Pool exchange, which `import`
        deliberately does not configure — so the command as a whole fails. What
        this asserts is that the seeded config is usable by the refresh path.
        """
        seed = run_bg_cognito_auth(
            ["import", "--gateway-url", gateway_with_discovery.url],
            stdin_data=REFRESH_TOKEN,
        )
        assert seed.returncode == 0, seed.stderr

        result = run_bg_cognito_auth(["refresh"])
        assert "Refreshing tokens" in result.stdout
        assert "Tokens refreshed successfully" in result.stdout
        assert _tokens(cognito_home)["access_token"] == "mock.access.token"
        assert result.returncode != 0  # Identity Pool exchange (unchanged path) not configured
