# tests/cli/test_bg_cognito_auth_login_web.py
"""Tests for `bg-cognito-auth.sh login --web` — browser-approval sign-in.

The flow: POST /auth/cli/start → show the user_code + open the approval URL →
poll POST /auth/cli/token until the browser user approves → persist config +
tokens. No credential is ever displayed or pasted.

These tests drive the real shell script against a mock gateway that scripts
the poll progression (pending → approved), with a sandboxed HOME.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

USER_CODE = "ABCD-2345"
DEVICE_CODE = "device-code-material-for-tests-0123456789abcdef"
TOKENS = {
    "token_type": "Bearer",
    "access_token": "web-access-token",
    "id_token": "web-id-token",
    "refresh_token": "web-refresh-token",
    "expires_in": 3600,
    "client_id": "cli-client-abc123",
    "user_pool_id": "us-east-1_webpool",
    "region": "us-east-1",
}

# What /auth/cli/refresh returns: a rotated set (new access/id AND a new refresh
# token that supersedes the one presented — the CLI must persist it).
REFRESH_TOKENS = {
    "token_type": "Bearer",
    "access_token": "rotated-access-token",
    "id_token": "rotated-id-token",
    "refresh_token": "rotated-refresh-token",
    "expires_in": 3600,
}


class CliLoginHandler(BaseHTTPRequestHandler):
    """Mock gateway /auth/cli endpoints under a /api base path.

    ``poll_statuses`` scripts what each successive /token poll returns
    (e.g. two pendings, then success). ``requests`` records everything.
    """

    protocol_version = "HTTP/1.1"
    poll_statuses: list[int] = []
    requests: list[dict[str, Any]] = []
    start_status: int = 200
    refresh_status: int = 200

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        self.__class__.requests.append({"path": self.path, "body": body.decode("utf-8", "replace")})

        if self.path == "/api/auth/cli/start":
            if self.start_status != 200:
                self._send(self.start_status, {"detail": {"error": "cli_login_not_configured"}})
                return
            self._send(
                200,
                {
                    "user_code": USER_CODE,
                    "device_code": DEVICE_CODE,
                    "verification_path": f"/cli-auth?code={USER_CODE}",
                    "expires_in": 600,
                    "interval": 0,  # poll immediately — keeps tests fast
                },
            )
        elif self.path == "/api/auth/cli/token":
            status = self.poll_statuses.pop(0) if self.poll_statuses else 202
            if status == 200:
                self._send(200, TOKENS)
            elif status == 202:
                self._send(202, {"detail": {"error": "authorization_pending"}})
            elif status == 403:
                self._send(403, {"detail": {"error": "access_denied"}})
            elif status == 410:
                self._send(410, {"detail": {"error": "expired"}})
            else:
                self._send(status, {"detail": {"error": "boom"}})
        elif self.path == "/api/auth/cli/refresh":
            if self.refresh_status == 200:
                self._send(200, REFRESH_TOKENS)
            elif self.refresh_status == 401:
                self._send(401, {"detail": {"error": "refresh_expired"}})
            else:
                self._send(self.refresh_status, {"detail": {"error": "refresh_failed"}})
        else:
            self._send(404, {"detail": "not_found"})

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def mock_cli_gateway():
    """Mock gateway on an ephemeral loopback port; yields its /api base URL."""
    CliLoginHandler.requests = []
    CliLoginHandler.poll_statuses = []
    CliLoginHandler.start_status = 200
    CliLoginHandler.refresh_status = 200
    server = ThreadingHTTPServer(("127.0.0.1", 0), CliLoginHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/api"
    server.shutdown()
    server.server_close()


@pytest.fixture
def fake_browser(tmp_path: Path) -> Path:
    """A fake `open`/`xdg-open` earlier on PATH that records the URL."""
    bin_dir = tmp_path / "fake-browser-bin"
    bin_dir.mkdir()
    for name in ("open", "xdg-open"):
        script = bin_dir / name
        script.write_text(f'#!/usr/bin/env bash\necho "$1" >> "{tmp_path}/browser-opened.log"\n')
        script.chmod(0o755)
    return bin_dir


@pytest.fixture
def run_web(run_bg_cognito_auth, fake_browser: Path, mock_cli_gateway: str):
    """Run `login --web` with the fake browser first on PATH."""
    import os

    def _run(extra: list[str] | None = None, gateway_url: str | None = None):
        return run_bg_cognito_auth(
            ["login", "--web", "--gateway-url", gateway_url or mock_cli_gateway, *(extra or [])],
            extra_env={"PATH": f"{fake_browser}:{os.environ.get('PATH', '')}"},
        )

    return _run


class TestLoginWebHappyPath:
    def test_signs_in_after_browser_approval(self, run_web, mock_cli_gateway: str, cognito_home: Path, tmp_path: Path) -> None:
        CliLoginHandler.poll_statuses = [202, 202, 200]  # two pendings, then approved

        result = run_web()
        assert result.returncode == 0, result.stderr + result.stdout

        # The short code was shown for the human to cross-check.
        assert USER_CODE in result.stdout
        # The browser was opened at the dashboard approval page (no /api).
        opened = (tmp_path / "browser-opened.log").read_text().strip()
        assert opened.endswith(f"/cli-auth?code={USER_CODE}")
        assert "/api/cli-auth" not in opened

        # Config carries the CLI client id from the token response.
        config = json.loads((cognito_home / ".bedrock-gateway" / "config.json").read_text())
        assert config["client_id"] == TOKENS["client_id"]
        assert config["user_pool_id"] == TOKENS["user_pool_id"]
        assert config["gateway_url"] == mock_cli_gateway

        tokens = json.loads((cognito_home / ".bedrock-gateway" / "tokens.json").read_text())
        assert tokens["refresh_token"] == TOKENS["refresh_token"]
        assert tokens["access_token"] == TOKENS["access_token"]

    def test_device_code_never_in_process_args(self, run_web) -> None:
        """The poll body must arrive via stdin, not argv (ps exposure)."""
        CliLoginHandler.poll_statuses = [200]
        result = run_web()
        assert result.returncode == 0
        # The mock recorded the device_code arriving in the POST body.
        polls = [r for r in CliLoginHandler.requests if r["path"] == "/api/auth/cli/token"]
        assert polls and json.loads(polls[0]["body"]) == {"device_code": DEVICE_CODE}
        # And the script never rendered it to the terminal.
        assert DEVICE_CODE not in result.stdout
        assert DEVICE_CODE not in result.stderr

    def test_no_browser_flag_prints_url_instead(self, run_web, tmp_path: Path) -> None:
        CliLoginHandler.poll_statuses = [200]
        result = run_web(extra=["--no-browser"])
        assert result.returncode == 0
        assert f"/cli-auth?code={USER_CODE}" in result.stdout
        assert not (tmp_path / "browser-opened.log").exists()

    def test_relogin_reuses_stored_gateway_url(
        self, run_bg_cognito_auth, run_web, mock_cli_gateway: str, cognito_home: Path, fake_browser: Path
    ) -> None:
        """Second `login --web` needs no --gateway-url."""
        import os

        CliLoginHandler.poll_statuses = [200]
        assert run_web().returncode == 0

        CliLoginHandler.poll_statuses = [200]
        result = run_bg_cognito_auth(
            ["login", "--web"],
            extra_env={"PATH": f"{fake_browser}:{os.environ.get('PATH', '')}"},
        )
        assert result.returncode == 0, result.stderr + result.stdout


class TestLoginWebFailures:
    def test_denied_in_browser(self, run_web) -> None:
        CliLoginHandler.poll_statuses = [202, 403]
        result = run_web()
        assert result.returncode != 0
        assert "denied" in result.stderr.lower() or "denied" in result.stdout.lower()

    def test_expired_request(self, run_web) -> None:
        CliLoginHandler.poll_statuses = [410]
        result = run_web()
        assert result.returncode != 0
        assert "expired" in (result.stderr + result.stdout).lower()

    def test_gateway_without_cli_login_suggests_fallbacks(self, run_web) -> None:
        CliLoginHandler.start_status = 503
        result = run_web()
        assert result.returncode != 0
        combined = result.stderr + result.stdout
        assert "import" in combined  # points at the fallback

    def test_transient_poll_errors_are_retried(self, run_web) -> None:
        CliLoginHandler.poll_statuses = [500, 500, 200]
        result = run_web()
        assert result.returncode == 0, result.stderr + result.stdout

    def test_nothing_persisted_on_failure(self, run_web, cognito_home: Path) -> None:
        CliLoginHandler.poll_statuses = [403]
        run_web()
        assert not (cognito_home / ".bedrock-gateway" / "tokens.json").exists()


class TestExistingCommandsUnchanged:
    def test_help_mentions_web_login(self, run_bg_cognito_auth) -> None:
        result = run_bg_cognito_auth(["help"])
        assert result.returncode == 0
        assert "login --web" in result.stdout

    def test_bare_login_still_requires_gateway_url(self, run_bg_cognito_auth) -> None:
        """`login` without --web must keep its password-flow behaviour."""
        result = run_bg_cognito_auth(["login"])
        assert result.returncode != 0


class TestRefreshViaGateway:
    """`login --web` configures refresh to go through the gateway, because the
    CLI Cognito client rotates and the user holds no AWS creds to hit the admin
    API directly (Issue #4837 follow-up)."""

    def test_web_login_marks_config_for_gateway_refresh(self, run_web, cognito_home: Path) -> None:
        CliLoginHandler.poll_statuses = [200]
        assert run_web().returncode == 0
        config = json.loads((cognito_home / ".bedrock-gateway" / "config.json").read_text())
        assert config["refresh_via"] == "gateway"

    @staticmethod
    def _expire_token_on_disk(cognito_home: Path) -> None:
        """Force the saved access token past its expiry so the next `token`
        call refreshes (the real Codex/Claude Code hot path)."""
        token_file = cognito_home / ".bedrock-gateway" / "tokens.json"
        data = json.loads(token_file.read_text())
        data["expires_at"] = 0  # 1970 — well past the 5-minute refresh buffer
        token_file.write_text(json.dumps(data))

    def test_token_refresh_goes_through_gateway_and_persists_rotated_tokens(self, run_web, run_bg_cognito_auth, cognito_home: Path) -> None:
        CliLoginHandler.poll_statuses = [200]
        assert run_web().returncode == 0
        self._expire_token_on_disk(cognito_home)

        result = run_bg_cognito_auth(["token"])
        assert result.returncode == 0, result.stderr + result.stdout
        # `token` prints the (now rotated) access token for apiKeyHelper/serve.
        assert result.stdout == REFRESH_TOKENS["access_token"]

        # It refreshed against the gateway (not Cognito) with the stored token.
        refreshes = [r for r in CliLoginHandler.requests if r["path"] == "/api/auth/cli/refresh"]
        assert refreshes, "expected a POST to /api/auth/cli/refresh"
        assert json.loads(refreshes[0]["body"]) == {"refresh_token": TOKENS["refresh_token"]}

        # The rotated set replaced the old one on disk.
        tokens = json.loads((cognito_home / ".bedrock-gateway" / "tokens.json").read_text())
        assert tokens["access_token"] == REFRESH_TOKENS["access_token"]
        assert tokens["refresh_token"] == REFRESH_TOKENS["refresh_token"]

    def test_expired_refresh_token_directs_user_to_relogin(self, run_web, run_bg_cognito_auth, cognito_home: Path) -> None:
        CliLoginHandler.poll_statuses = [200]
        assert run_web().returncode == 0
        self._expire_token_on_disk(cognito_home)

        CliLoginHandler.refresh_status = 401
        # `refresh` surfaces the guidance directly (token swallows stderr).
        result = run_bg_cognito_auth(["refresh"])
        assert result.returncode != 0
        assert "login --web" in (result.stderr + result.stdout)

    def test_no_aws_cli_needed_for_gateway_refresh(self, run_web, run_bg_cognito_auth, cognito_home: Path) -> None:
        """The whole point: refresh must not shell out to `aws` (the user has no
        platform creds). Prove it by making `aws` on PATH fail hard and still
        getting a rotated token out of `token`."""
        import os

        CliLoginHandler.poll_statuses = [200]
        assert run_web().returncode == 0
        self._expire_token_on_disk(cognito_home)

        # A poisoned `aws` that errors if invoked — the gateway path must not touch it.
        bin_dir = cognito_home / "poison-bin"
        bin_dir.mkdir()
        aws = bin_dir / "aws"
        aws.write_text("#!/usr/bin/env bash\necho 'aws must not be called' >&2\nexit 99\n")
        aws.chmod(0o755)

        result = run_bg_cognito_auth(
            ["token"],
            extra_env={"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}"},
        )
        assert result.returncode == 0, result.stderr + result.stdout
        assert result.stdout == REFRESH_TOKENS["access_token"]
