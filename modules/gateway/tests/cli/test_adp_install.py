# tests/cli/test_adp_install.py
"""Tests for `install.sh` — the one-line install path (Issue #4852, Phase 1).

The install line in the docs is:

    curl -fsSL https://<gw>/api/cli/install.sh | sh -s -- --gateway-url https://<gw>

Three properties of that line are what these tests defend:

1. **It runs under POSIX sh, not bash.** Piping into `sh` ignores the shebang, and
   on Debian-family systems /bin/sh is dash. A bashism reaches every user at once
   and the failure is a syntax error on line N, which nobody can act on. Tested by
   parsing under dash and by running the whole install with `sh`.
2. **It never guesses the gateway origin.** A wrong origin silently installs a CLI
   pointed at somebody else's deployment. With no URL available the script must
   refuse and print the copy-paste re-run line.
3. **It requires no AWS credentials.** Ordinary gateway users hold none, and the
   previous installer hard-required the `aws` CLI. Tested by poisoning `aws` on
   PATH so any invocation fails the test.

Everything runs against a sandboxed HOME and a --prefix under tmp_path, so no
test can write to a real ~/.adp, ~/.bedrock-gateway or shell rc.
"""

import json
import os
import shutil
import subprocess
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from .conftest import write_adp_session

GATEWAY_URL = "https://gw.example.com/api"

INSTALLED_FILES = ["adp", "bg-cognito-auth.sh", "bg-gateway-proxy.py", "adp_common.py", "adp-admin.py", "adp-bedrock.py", "adp-github-admin.py"]


@pytest.fixture
def install_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    return home


@pytest.fixture
def prefix(tmp_path: Path) -> Path:
    return tmp_path / "prefix" / "bin"


@pytest.fixture
def poisoned_aws(tmp_path: Path) -> Path:
    """A PATH dir whose `aws` fails loudly.

    Proves the installer's independence from the AWS CLI by construction: if any
    code path shells out to `aws`, the marker shows up and the assertion fails.
    """
    bin_dir = tmp_path / "poison"
    bin_dir.mkdir()
    aws = bin_dir / "aws"
    aws.write_text("#!/bin/sh\necho 'POISONED-AWS-WAS-CALLED' >&2\nexit 127\n")
    aws.chmod(0o755)
    return bin_dir


@pytest.fixture
def run_install(install_script: Path, install_home: Path, poisoned_aws: Path):
    """Run install.sh under `sh` with a sandboxed HOME and `aws` poisoned.

    `sh` (not bash) because that is what the documented `curl | sh` line uses.
    """

    def _run(args: list[str], extra_env: dict[str, str] | None = None, shell: str = "sh") -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["HOME"] = str(install_home)
        env["PATH"] = f"{poisoned_aws}:{env.get('PATH', '')}"
        # Keep the installer off the real shell rc unless a test asks otherwise.
        env["SHELL"] = "/bin/sh"
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [shell, str(install_script), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

    return _run


class TestPosixCompliance:
    """The install line pipes into `sh`; a bashism breaks every new user."""

    @pytest.mark.parametrize("shell", ["sh", "dash", "bash"])
    def test_parses_under_each_shell(self, install_script: Path, shell: str) -> None:
        if shutil.which(shell) is None:
            pytest.skip(f"{shell} not available")

        result = subprocess.run([shell, "-n", str(install_script)], capture_output=True, text=True, timeout=30)

        assert result.returncode == 0, result.stderr

    def test_declares_a_posix_shebang(self, install_script: Path) -> None:
        """`sh install.sh` ignores the shebang, but `./install.sh` honours it."""
        assert install_script.read_text().startswith("#!/bin/sh\n")


class TestUsage:
    """Replaces test_bg_auth.py::TestInstallScript, which asserted the old
    installer's banner and v1.0.0 — this script now installs `adp`, not the
    deprecated bg-auth.sh."""

    def test_help_describes_the_adp_cli(self, run_install) -> None:
        result = run_install(["--help"])

        assert result.returncode == 0
        combined = result.stdout + result.stderr
        assert "adp" in combined
        assert "--gateway-url" in combined
        assert "--prefix" in combined
        assert "--uninstall" in combined

    def test_version_is_reported(self, run_install) -> None:
        result = run_install(["--version"])

        assert result.returncode == 0
        assert "install.sh v" in result.stdout

    def test_help_needs_no_gateway_url(self, run_install) -> None:
        """--help must work before you know your gateway URL — it is where you
        find out that you need one."""
        assert run_install(["--help"]).returncode == 0

    def test_unknown_option_fails_with_usage(self, run_install) -> None:
        result = run_install(["--not-an-option"])

        assert result.returncode != 0
        assert "--gateway-url" in result.stdout + result.stderr


class TestGatewayUrlIsNeverGuessed:
    def test_refuses_without_a_url_and_prints_the_rerun_line(self, run_install, prefix: Path) -> None:
        result = run_install(["--prefix", str(prefix)])

        assert result.returncode != 0
        # The actionable part: a copy-pasteable command, not just a complaint.
        assert "--gateway-url" in result.stderr
        assert "install.sh" in result.stderr
        assert not prefix.exists(), "must not half-install before failing"

    def test_accepts_the_url_from_the_environment(self, run_install, prefix: Path, install_home: Path) -> None:
        """ADP_GATEWAY_URL is the fallback for automation that cannot pass flags."""
        result = run_install(["--prefix", str(prefix)], extra_env={"ADP_GATEWAY_URL": GATEWAY_URL})

        assert result.returncode == 0, result.stderr
        config = json.loads((install_home / ".bedrock-gateway" / "config.json").read_text())
        assert config["gateway_url"] == GATEWAY_URL

    def test_reuses_a_previously_stored_url(self, run_install, prefix: Path, install_home: Path) -> None:
        """A re-run (or `adp update`) must not need the flag again."""
        config_dir = install_home / ".bedrock-gateway"
        config_dir.mkdir()
        (config_dir / "config.json").write_text(json.dumps({"gateway_url": GATEWAY_URL}))

        result = run_install(["--prefix", str(prefix)])

        assert result.returncode == 0, result.stderr
        assert (prefix / "adp").is_file()

    def test_strips_a_trailing_slash(self, run_install, prefix: Path, install_home: Path) -> None:
        """Otherwise every derived URL doubles the slash (…/api//cli/install.sh)."""
        result = run_install(["--prefix", str(prefix), "--gateway-url", GATEWAY_URL + "/"])

        assert result.returncode == 0, result.stderr
        config = json.loads((install_home / ".bedrock-gateway" / "config.json").read_text())
        assert config["gateway_url"] == GATEWAY_URL


class TestInstall:
    def test_installs_the_three_files_side_by_side(self, run_install, prefix: Path) -> None:
        """`adp` resolves the core helper and proxy as siblings, so all three
        must land in one directory."""
        result = run_install(["--prefix", str(prefix), "--gateway-url", GATEWAY_URL])

        assert result.returncode == 0, result.stderr
        for name in INSTALLED_FILES:
            assert (prefix / name).is_file(), f"{name} was not installed"

    def test_entrypoints_are_executable(self, run_install, prefix: Path) -> None:
        assert run_install(["--prefix", str(prefix), "--gateway-url", GATEWAY_URL]).returncode == 0

        assert os.access(prefix / "adp", os.X_OK)
        assert os.access(prefix / "bg-cognito-auth.sh", os.X_OK)

    def test_installed_files_match_the_repo_copies(self, run_install, prefix: Path, cli_dir: Path) -> None:
        """Run from a checkout, the installer copies locally rather than fetching
        — which is also what makes it testable without a live gateway."""
        assert run_install(["--prefix", str(prefix), "--gateway-url", GATEWAY_URL]).returncode == 0

        for name in INSTALLED_FILES:
            assert (prefix / name).read_bytes() == (cli_dir / name).read_bytes()

    def test_succeeds_with_the_aws_cli_poisoned(self, run_install, prefix: Path) -> None:
        """The install path must not touch AWS: gateway users have no AWS
        credentials, which is the whole point of gateway-routed refresh."""
        result = run_install(["--prefix", str(prefix), "--gateway-url", GATEWAY_URL])

        assert result.returncode == 0, result.stderr
        assert "POISONED-AWS-WAS-CALLED" not in result.stderr
        assert "POISONED-AWS-WAS-CALLED" not in result.stdout

    def test_config_dir_and_file_are_private(self, run_install, prefix: Path, install_home: Path) -> None:
        """The same dir holds tokens.json, so the modes matter."""
        assert run_install(["--prefix", str(prefix), "--gateway-url", GATEWAY_URL]).returncode == 0

        config_dir = install_home / ".bedrock-gateway"
        assert (config_dir.stat().st_mode & 0o777) == 0o700
        assert ((config_dir / "config.json").stat().st_mode & 0o777) == 0o600

    def test_preserves_an_existing_sessions_config_keys(self, run_install, prefix: Path, install_home: Path) -> None:
        """Updating must not log the user out: client_id and refresh_via belong
        to a live session and are merged, not overwritten."""
        config_dir = install_home / ".bedrock-gateway"
        config_dir.mkdir()
        (config_dir / "config.json").write_text(
            json.dumps({"gateway_url": "https://old.example.com/api", "client_id": "abc123", "refresh_via": "gateway"})
        )

        assert run_install(["--prefix", str(prefix), "--gateway-url", GATEWAY_URL]).returncode == 0

        config = json.loads((config_dir / "config.json").read_text())
        assert config["client_id"] == "abc123"
        assert config["refresh_via"] == "gateway"
        assert config["gateway_url"] == GATEWAY_URL

    def test_never_writes_to_the_legacy_bin_dir(self, run_install, prefix: Path, install_home: Path) -> None:
        """~/bin may hold a hand-installed bg-cognito-auth.sh that must keep
        working; the new CLI lives in its own prefix."""
        legacy = install_home / "bin"
        legacy.mkdir()
        (legacy / "bg-cognito-auth.sh").write_text("#!/usr/bin/env bash\necho hand-installed\n")

        assert run_install(["--prefix", str(prefix), "--gateway-url", GATEWAY_URL]).returncode == 0

        assert (legacy / "bg-cognito-auth.sh").read_text() == "#!/usr/bin/env bash\necho hand-installed\n"
        assert sorted(p.name for p in legacy.iterdir()) == ["bg-cognito-auth.sh"]

    def test_is_idempotent(self, run_install, prefix: Path) -> None:
        """The /setup page's remedy for anything odd is "run the install line
        again", so a second run must be safe."""
        assert run_install(["--prefix", str(prefix), "--gateway-url", GATEWAY_URL]).returncode == 0
        assert run_install(["--prefix", str(prefix), "--gateway-url", GATEWAY_URL]).returncode == 0

        for name in INSTALLED_FILES:
            assert (prefix / name).is_file()

    def test_prints_the_next_commands_to_run(self, run_install, prefix: Path) -> None:
        result = run_install(["--prefix", str(prefix), "--gateway-url", GATEWAY_URL])

        combined = result.stdout + result.stderr
        assert f'"{prefix}/adp" login' in combined
        assert f'"{prefix}/adp" admin setup' in combined
        assert "adp status" in combined


class TestKeepPrevious:
    """`adp update` sets ADP_KEEP_PREVIOUS=1 so `--rollback` has something to
    restore — the mitigation for "update pulled a broken CLI"."""

    def test_keeps_prev_copies_when_asked(self, run_install, prefix: Path) -> None:
        assert run_install(["--prefix", str(prefix), "--gateway-url", GATEWAY_URL]).returncode == 0
        (prefix / "adp").write_text("#!/usr/bin/env bash\necho old-version\n")

        assert run_install(["--prefix", str(prefix), "--gateway-url", GATEWAY_URL], extra_env={"ADP_KEEP_PREVIOUS": "1"}).returncode == 0

        assert "old-version" in (prefix / "adp.prev").read_text()
        assert "old-version" not in (prefix / "adp").read_text()

    def test_no_prev_copies_on_a_normal_install(self, run_install, prefix: Path) -> None:
        assert run_install(["--prefix", str(prefix), "--gateway-url", GATEWAY_URL]).returncode == 0
        assert run_install(["--prefix", str(prefix), "--gateway-url", GATEWAY_URL]).returncode == 0

        assert not (prefix / "adp.prev").exists()


class TestPathSetup:
    def test_adds_the_prefix_to_the_shell_rc(self, run_install, prefix: Path, install_home: Path) -> None:
        (install_home / ".zshrc").write_text("# my zshrc\n")

        result = run_install(["--prefix", str(prefix), "--gateway-url", GATEWAY_URL], extra_env={"SHELL": "/bin/zsh"})

        assert result.returncode == 0, result.stderr
        rc = (install_home / ".zshrc").read_text()
        assert "# my zshrc" in rc, "must append, not overwrite"
        assert str(prefix) in rc

    def test_does_not_add_the_path_line_twice(self, run_install, prefix: Path, install_home: Path) -> None:
        rc_file = install_home / ".zshrc"
        rc_file.write_text("# my zshrc\n")
        env = {"SHELL": "/bin/zsh"}

        assert run_install(["--prefix", str(prefix), "--gateway-url", GATEWAY_URL], extra_env=env).returncode == 0
        assert run_install(["--prefix", str(prefix), "--gateway-url", GATEWAY_URL], extra_env=env).returncode == 0

        assert rc_file.read_text().count(str(prefix)) == 1

    def test_no_path_edit_leaves_the_rc_alone_but_prints_the_line(self, run_install, prefix: Path, install_home: Path) -> None:
        rc_file = install_home / ".zshrc"
        rc_file.write_text("# my zshrc\n")

        result = run_install(
            ["--prefix", str(prefix), "--gateway-url", GATEWAY_URL, "--no-path-edit"],
            extra_env={"SHELL": "/bin/zsh"},
        )

        assert result.returncode == 0, result.stderr
        assert rc_file.read_text() == "# my zshrc\n"
        assert str(prefix) in result.stderr


class TestUninstall:
    def test_removes_the_installed_files(self, run_install, prefix: Path) -> None:
        assert run_install(["--prefix", str(prefix), "--gateway-url", GATEWAY_URL]).returncode == 0

        result = run_install(["--prefix", str(prefix), "--uninstall"])

        assert result.returncode == 0, result.stderr
        for name in INSTALLED_FILES:
            assert not (prefix / name).exists()

    def test_keeps_the_session_so_a_reinstall_stays_signed_in(self, run_install, prefix: Path, install_home: Path) -> None:
        """Deleting a live session on an uninstall would be a surprise, and
        forces a re-login for what is often just a move to a new prefix."""
        assert run_install(["--prefix", str(prefix), "--gateway-url", GATEWAY_URL]).returncode == 0
        tokens = install_home / ".bedrock-gateway" / "tokens.json"
        tokens.write_text(json.dumps({"refresh_token": "still-here"}))

        assert run_install(["--prefix", str(prefix), "--uninstall"]).returncode == 0

        assert json.loads(tokens.read_text())["refresh_token"] == "still-here"
        assert (install_home / ".bedrock-gateway" / "config.json").is_file()

    def test_uninstall_needs_no_gateway_url(self, run_install, prefix: Path, install_home: Path) -> None:
        """You must be able to remove the CLI even if the config is gone."""
        assert run_install(["--prefix", str(prefix), "--gateway-url", GATEWAY_URL]).returncode == 0
        (install_home / ".bedrock-gateway" / "config.json").unlink()

        assert run_install(["--prefix", str(prefix), "--uninstall"]).returncode == 0

    def test_uninstall_on_a_clean_machine_is_not_an_error(self, run_install, prefix: Path) -> None:
        result = run_install(["--prefix", str(prefix), "--uninstall"])

        assert result.returncode == 0


class TestInstalledCliWorks:
    """End-to-end: the thing install.sh produced must actually run. Catches a
    broken install (wrong mode, missing sibling, mangled copy) that per-file
    assertions would miss."""

    def test_installed_adp_reports_its_version(self, run_install, prefix: Path, install_home: Path) -> None:
        assert run_install(["--prefix", str(prefix), "--gateway-url", GATEWAY_URL]).returncode == 0

        env = os.environ.copy()
        env["HOME"] = str(install_home)
        result = subprocess.run([str(prefix / "adp"), "version"], capture_output=True, text=True, env=env, timeout=30)

        assert result.returncode == 0, result.stderr
        assert result.stdout.startswith("adp ")

    def test_installed_adp_can_wire_up_a_tool(self, run_install, prefix: Path, install_home: Path) -> None:
        """Proves the sibling layout install.sh creates is the one `adp` expects:
        `codex setup` refuses if the proxy is not next to the wrapper."""
        assert run_install(["--prefix", str(prefix), "--gateway-url", GATEWAY_URL]).returncode == 0
        write_adp_session(install_home)

        env = os.environ.copy()
        env["HOME"] = str(install_home)
        result = subprocess.run([str(prefix / "adp"), "codex", "setup"], capture_output=True, text=True, env=env, timeout=30)

        assert result.returncode == 0, result.stderr
        assert 'model_provider = "adp-gateway"' in (install_home / ".codex" / "config.toml").read_text()


class TestGatewayUrlNormalization:
    """Every route the CLI uses lives under /api (/api/cli/* to download,
    /api/auth/cli/* for login + refresh), and bg-cognito-auth.sh appends
    /auth/... to the stored gateway_url directly. But the /setup page and this
    file's own examples show a BARE `--gateway-url https://<gw>`. The installer
    must reconcile the two: normalize to the /api form so both the download here
    and the URL persisted for `adp login` are correct. Without this a bare URL
    404/403s (or the SPA fallback returns index.html with a 200 and HTML gets
    installed as `adp`), and `adp login` afterwards hits the wrong path too."""

    def test_bare_url_is_normalized_to_the_api_path(self, run_install, prefix: Path, install_home: Path) -> None:
        result = run_install(["--prefix", str(prefix), "--gateway-url", "https://gw.example.com"])

        assert result.returncode == 0, result.stderr
        config = json.loads((install_home / ".bedrock-gateway" / "config.json").read_text())
        assert config["gateway_url"] == "https://gw.example.com/api"

    def test_bare_url_with_trailing_slash_is_normalized(self, run_install, prefix: Path, install_home: Path) -> None:
        result = run_install(["--prefix", str(prefix), "--gateway-url", "https://gw.example.com/"])

        assert result.returncode == 0, result.stderr
        config = json.loads((install_home / ".bedrock-gateway" / "config.json").read_text())
        assert config["gateway_url"] == "https://gw.example.com/api"

    def test_url_already_carrying_api_is_left_unchanged(self, run_install, prefix: Path, install_home: Path) -> None:
        """No double /api/api when the user (or the stored config) already has it."""
        result = run_install(["--prefix", str(prefix), "--gateway-url", "https://gw.example.com/api/"])

        assert result.returncode == 0, result.stderr
        config = json.loads((install_home / ".bedrock-gateway" / "config.json").read_text())
        assert config["gateway_url"] == "https://gw.example.com/api"


# ---------------------------------------------------------------------------
# Download path (curl | sh from outside a checkout): all-or-nothing install.
# ---------------------------------------------------------------------------


class _CliDownloadHandler(BaseHTTPRequestHandler):
    """Serves the /api/cli/* download route so the curl path can be exercised.

    ``mode`` scripts what the gateway returns:
      "scripts"  — a valid shebang script for every file (happy path)
      "html"     — a 200 SPA-fallback index.html for everything (the trap)
      "adp_only" — a valid `adp`, but 403 for the rest (mid-way failure)
    """

    mode = "scripts"
    seen_paths: list[str] = []

    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:
        self.__class__.seen_paths.append(self.path)
        name = self.path.rsplit("/", 1)[-1]
        if self.mode == "html":
            self._send(200, b"<!doctype html><html><body>SPA fallback</body></html>\n")
        elif self.mode == "adp_only":
            if name == "adp":
                self._send(200, b"#!/usr/bin/env bash\necho fake-adp\n")
            else:
                self._send(403, b"Forbidden\n")
        else:
            self._send(200, f"#!/usr/bin/env bash\necho fake {name}\n".encode())

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def _cli_download_server(mode: str):
    _CliDownloadHandler.mode = mode
    _CliDownloadHandler.seen_paths = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CliDownloadHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _dir_contents(prefix: Path) -> list[str]:
    """Every entry in the install dir — installed files AND leftover temps."""
    if not prefix.exists():
        return []
    return sorted(p.name for p in prefix.iterdir())


class TestDownloadPathIsAllOrNothing:
    """Run from outside a checkout (the real `curl | sh` case), install.sh
    fetches each file from the gateway. A failure part-way through must leave
    nothing behind — never a usable `adp` without the core script it needs."""

    @pytest.fixture
    def isolated_install_script(self, install_script: Path, tmp_path: Path) -> Path:
        """A copy of install.sh with NO sibling CLI files, so script_dir() finds
        nothing on disk and the gateway-download branch is taken."""
        d = tmp_path / "isolated"
        d.mkdir()
        dst = d / "install.sh"
        dst.write_bytes(install_script.read_bytes())
        dst.chmod(0o755)
        return dst

    @pytest.fixture
    def run_download_install(self, isolated_install_script: Path, install_home: Path, poisoned_aws: Path):
        def _run(args: list[str]) -> subprocess.CompletedProcess:
            env = os.environ.copy()
            env["HOME"] = str(install_home)
            env["PATH"] = f"{poisoned_aws}:{env.get('PATH', '')}"
            env["SHELL"] = "/bin/sh"
            return subprocess.run(
                ["sh", str(isolated_install_script), *args],
                capture_output=True,
                text=True,
                env=env,
                timeout=60,
            )

        return _run

    def test_downloads_all_three_from_the_api_cli_route(self, run_download_install, prefix: Path) -> None:
        with _cli_download_server("scripts") as url:
            result = run_download_install(["--prefix", str(prefix), "--gateway-url", url])

        assert result.returncode == 0, result.stderr
        for name in INSTALLED_FILES:
            assert (prefix / name).is_file(), f"{name} was not installed"
        # A bare gateway URL was normalized to hit /api/cli/... (not /cli/...).
        assert "/api/cli/adp" in _CliDownloadHandler.seen_paths
        assert "/cli/adp" not in _CliDownloadHandler.seen_paths

    def test_html_fallback_body_is_rejected_and_nothing_is_installed(self, run_download_install, prefix: Path) -> None:
        """A misrouted request returning the SPA's index.html with a 200 must not
        be installed as `adp` — that is the exact partial/broken install trap."""
        with _cli_download_server("html") as url:
            result = run_download_install(["--prefix", str(prefix), "--gateway-url", url])

        assert result.returncode != 0
        assert "not a script" in (result.stderr + result.stdout).lower()
        assert _dir_contents(prefix) == [], "must leave no files (or temps) behind"

    def test_a_later_download_failure_rolls_back_the_earlier_files(self, run_download_install, prefix: Path) -> None:
        """`adp` downloads fine but the core script 403s: because we stage all
        three before committing any, neither ends up installed."""
        with _cli_download_server("adp_only") as url:
            result = run_download_install(["--prefix", str(prefix), "--gateway-url", url])

        assert result.returncode != 0
        assert _dir_contents(prefix) == [], "a mid-way failure must not leave a partial install"
