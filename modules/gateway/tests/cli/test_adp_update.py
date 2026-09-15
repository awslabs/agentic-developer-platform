# tests/cli/test_adp_update.py
"""Tests for `adp update` against a gateway that really serves the files.

Self-update is the reason this CLI can ship at all: it replaces "re-download the
scripts by hand", so the docs can say `adp update` instead of restating the curl
line. It is also the one verb that fetches and then overwrites its own
executables, so the failure modes are worth pinning:

- it must pull from the gateway that installed it, resolved from the stored
  config — not from a hardcoded or guessed origin;
- a fetch failure must leave the working CLI in place rather than a half-written
  one, because the recovery tool IS the thing being replaced;
- it must leave *.prev behind so `--rollback` works after a bad update.

The other `adp update` cases (rollback, no-gateway-known) need no server and live
in test_adp_setup.py. This module exists for the ones that do.
"""

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

CLI_FILES = ["adp", "install.sh", "bg-cognito-auth.sh", "bg-gateway-proxy.py", "adp_common.py", "adp-admin.py"]


class _CliServer:
    """A stand-in for GET /api/cli/{script_name}.

    Serves real file bytes from a directory, so `adp update` exercises the same
    download route contract the gateway implements (src/cli_download/routes.py).
    Individual paths can be made to fail, to test the abort path.
    """

    def __init__(self, source_dir: Path):
        self.source_dir = source_dir
        self.fail_paths: set[str] = set()
        self.requested: list[str] = []
        self._server: ThreadingHTTPServer | None = None

    def __enter__(self) -> "_CliServer":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def do_GET(self) -> None:
                outer.requested.append(self.path)
                name = self.path.rsplit("/", 1)[-1]
                source = outer.source_dir / name
                if self.path in outer.fail_paths or not source.is_file():
                    self.send_response(404)
                    self.end_headers()
                    return
                body = source.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/x-shellscript")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *args: object) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}/api"


@pytest.fixture
def upstream(tmp_path: Path, cli_dir: Path):
    """A gateway serving a *newer* copy of the CLI than the one installed.

    `adp` is marked so a test can tell the fetched copy from the installed one;
    the rest are the real files, so what lands is genuinely runnable.
    """
    source = tmp_path / "upstream"
    source.mkdir()
    for name in CLI_FILES:
        source.joinpath(name).write_bytes((cli_dir / name).read_bytes())

    marked = source / "adp"
    marked.write_text(marked.read_text().replace('ADP_VERSION="1.0.0"', 'ADP_VERSION="9.9.9-from-gateway"'))

    with _CliServer(source) as server:
        yield server


@pytest.fixture
def installed(tmp_path: Path, cli_dir: Path, upstream) -> tuple[Path, Path]:
    """An install whose config points at the mock gateway. Returns (bin, home)."""
    bin_dir = tmp_path / "installed-bin"
    bin_dir.mkdir()
    for name in ("adp", "bg-cognito-auth.sh", "bg-gateway-proxy.py", "adp_common.py", "adp-admin.py"):
        target = bin_dir / name
        target.write_bytes((cli_dir / name).read_bytes())
        target.chmod(0o755)

    home = tmp_path / "update-home"
    (home / ".bedrock-gateway").mkdir(parents=True)
    (home / ".bedrock-gateway" / "config.json").write_text(json.dumps({"gateway_url": upstream.url, "client_id": "abc123"}))

    return bin_dir, home


def _run_adp(bin_dir: Path, home: Path, args: list[str]) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["SHELL"] = "/bin/sh"
    return subprocess.run(["bash", str(bin_dir / "adp"), *args], capture_output=True, text=True, env=env, timeout=60)


class TestUpdate:
    def test_pulls_the_new_cli_from_the_stored_gateway(self, installed) -> None:
        bin_dir, home = installed

        result = _run_adp(bin_dir, home, ["update"])

        assert result.returncode == 0, result.stderr
        assert "9.9.9-from-gateway" in (bin_dir / "adp").read_text()

    def test_fetches_the_installer_and_all_three_files(self, installed, upstream) -> None:
        """install.sh does the work; `adp update` just re-runs it against the
        same prefix, so the whole set refreshes together."""
        bin_dir, home = installed

        assert _run_adp(bin_dir, home, ["update"]).returncode == 0

        for name in ("install.sh", "adp", "bg-cognito-auth.sh", "bg-gateway-proxy.py", "adp_common.py", "adp-admin.py"):
            assert f"/api/cli/{name}" in upstream.requested, f"{name} was not fetched"

    def test_updates_in_place_without_moving_the_prefix(self, installed) -> None:
        """The updated CLI must stay where PATH already points."""
        bin_dir, home = installed

        assert _run_adp(bin_dir, home, ["update"]).returncode == 0

        for name in ("adp", "bg-cognito-auth.sh", "bg-gateway-proxy.py", "adp_common.py", "adp-admin.py"):
            assert (bin_dir / name).is_file()
        assert not (home / ".adp").exists(), "must not silently install to the default prefix"

    def test_the_updated_cli_runs(self, installed) -> None:
        bin_dir, home = installed
        assert _run_adp(bin_dir, home, ["update"]).returncode == 0

        result = _run_adp(bin_dir, home, ["version"])

        assert result.returncode == 0, result.stderr
        assert "9.9.9-from-gateway" in result.stdout

    def test_leaves_a_rollback_target(self, installed) -> None:
        """Without this, a bad update is unrecoverable without the curl line."""
        bin_dir, home = installed

        assert _run_adp(bin_dir, home, ["update"]).returncode == 0

        assert (bin_dir / "adp.prev").is_file()
        assert 'ADP_VERSION="1.0.0"' in (bin_dir / "adp.prev").read_text()

    def test_update_then_rollback_restores_the_old_version(self, installed) -> None:
        bin_dir, home = installed
        assert _run_adp(bin_dir, home, ["update"]).returncode == 0

        result = _run_adp(bin_dir, home, ["update", "--rollback"])

        assert result.returncode == 0, result.stderr
        assert 'ADP_VERSION="1.0.0"' in (bin_dir / "adp").read_text()
        assert _run_adp(bin_dir, home, ["version"]).returncode == 0, "rolled-back CLI must run"

    def test_keeps_the_session_config(self, installed, upstream) -> None:
        """Updating must not log the user out."""
        bin_dir, home = installed

        assert _run_adp(bin_dir, home, ["update"]).returncode == 0

        config = json.loads((home / ".bedrock-gateway" / "config.json").read_text())
        assert config["client_id"] == "abc123"
        assert config["gateway_url"] == upstream.url


class TestUpdateFailure:
    def test_a_missing_installer_leaves_the_working_cli_intact(self, installed, upstream) -> None:
        """The recovery tool is the thing being replaced, so a failed fetch must
        abort before touching anything."""
        bin_dir, home = installed
        upstream.fail_paths.add("/api/cli/install.sh")

        result = _run_adp(bin_dir, home, ["update"])

        assert result.returncode != 0
        assert 'ADP_VERSION="1.0.0"' in (bin_dir / "adp").read_text()
        assert _run_adp(bin_dir, home, ["version"]).returncode == 0

    def test_a_missing_payload_file_is_reported_not_silently_skipped(self, installed, upstream) -> None:
        """A partial update that reports success is worse than a failed one: the
        user would not know to roll back."""
        bin_dir, home = installed
        upstream.fail_paths.add("/api/cli/bg-cognito-auth.sh")

        result = _run_adp(bin_dir, home, ["update"])

        assert result.returncode != 0
        assert "bg-cognito-auth.sh" in result.stderr

    def test_an_unreachable_gateway_fails_cleanly(self, installed, tmp_path: Path) -> None:
        bin_dir, home = installed
        # Port 1 on loopback: nothing listens, connection refused immediately.
        (home / ".bedrock-gateway" / "config.json").write_text(json.dumps({"gateway_url": "http://127.0.0.1:1/api"}))

        result = _run_adp(bin_dir, home, ["update"])

        assert result.returncode != 0
        assert 'ADP_VERSION="1.0.0"' in (bin_dir / "adp").read_text()
