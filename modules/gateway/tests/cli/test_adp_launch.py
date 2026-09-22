# tests/cli/test_adp_launch.py
"""Tests for the `adp codex` / `adp claude` launchers and `adp daemon` (Issue #4863).

Launching Codex used to be two terminals: `adp serve` in one, then
`ADP_GATEWAY_DUMMY=unused codex` in another. `adp codex` collapses that into one
command — preflight the session, start the proxy if it is not already up, set the
placeholder env var, exec Codex — so the behaviours worth testing are exactly the
failure modes that collapsing introduces:

- **it must not spawn a second proxy** when one is already listening (port clash),
- **it must not exec Codex before the proxy is ready** (the first request would 502),
- **two concurrent launches must start exactly one proxy** (the mkdir-lock),
- **a dead session must not launch the tool at all** — print the `adp login` hint
  and exit non-zero, rather than dropping the user into a tool that 401s,
- **`adp claude` must stay a thin preflight**: no proxy, no dummy env var, because
  Claude Code's own apiKeyHelper already handles tokens.

Like the `test_bg_cognito_auth_*` suites these drive the real shell script against
a sandboxed HOME. The tools are stubbed by poisoning PATH with scripts that record
their argv and environment, which is what lets a test prove a tool was *never*
exec'd — the assertion an "it launched anyway" regression needs.

The proxy runs on a per-test free port (`ADP_PROXY_PORT`) so a developer's own
proxy on 9191 neither breaks these tests nor is disturbed by them.
"""

import http.client
import json
import os
import plistlib
import re
import signal
import socket
import subprocess
import time
from pathlib import Path

import pytest

from .conftest import write_adp_session

# A gateway URL on a closed loopback port. The launcher's preflight never needs
# the network for a VALID session (the cached token is returned as-is), and for an
# EXPIRED one this makes the refresh attempt fail with an immediate connection
# refused instead of a multi-second timeout against a real host.
CLOSED_GATEWAY_PORT = 1
DEAD_GATEWAY_URL = f"http://127.0.0.1:{CLOSED_GATEWAY_PORT}/api"

LOGIN_HINT = "Run: adp login"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _port_is_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _listening_lines(log: Path) -> list[str]:
    """The proxy's one-per-start banner — how a second start is detected."""
    if not log.exists():
        return []
    return [line for line in log.read_text(errors="replace").splitlines() if "listening on" in line]


@pytest.fixture
def proxy_port() -> int:
    """A free port for this test's proxy, so 9191 is never touched."""
    return _free_port()


@pytest.fixture
def stub_tools(tmp_path: Path) -> Path:
    """A PATH dir holding fake `codex` and `claude` that record argv + env.

    Recording rather than mocking is deliberate: the launcher `exec`s the tool, so
    the only trustworthy evidence of what it did (and of whether it ran at all) is
    what the tool itself observed.
    """
    bin_dir = tmp_path / "stub-tools"
    bin_dir.mkdir()
    for tool in ("codex", "claude"):
        script = bin_dir / tool
        script.write_text(
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n" "{tool}" >> "${{STUB_INVOCATION_LOG}}"\n'
            'printf "argv=%s\\n" "$*" >> "${STUB_INVOCATION_LOG}"\n'
            'printf "dummy=%s\\n" "${ADP_GATEWAY_DUMMY-<unset>}" >> "${STUB_INVOCATION_LOG}"\n'
        )
        script.chmod(0o755)
    return bin_dir


class Invocations:
    """What the stub tools recorded."""

    def __init__(self, log: Path) -> None:
        self._log = log

    @property
    def _lines(self) -> list[str]:
        if not self._log.exists():
            return []
        return self._log.read_text().splitlines()

    @property
    def tools(self) -> list[str]:
        return [line for line in self._lines if line in ("codex", "claude")]

    @property
    def argv(self) -> list[str]:
        return [line.removeprefix("argv=") for line in self._lines if line.startswith("argv=")]

    @property
    def dummy_env(self) -> list[str]:
        return [line.removeprefix("dummy=") for line in self._lines if line.startswith("dummy=")]

    def launched(self, tool: str) -> bool:
        return tool in self.tools


@pytest.fixture
def launch(adp_bin: Path, adp_home: Path, stub_tools: Path, proxy_port: int, tmp_path: Path):
    """Run `adp <args>` with stubbed tools, a sandboxed HOME and a private port.

    Yields a callable plus the invocation record. Any proxy the launcher started is
    terminated on teardown — a leaked background proxy would hold its port and
    silently change the next test's meaning.
    """
    invocation_log = tmp_path / "invocations.log"
    record = Invocations(invocation_log)

    def _run(args: list[str], extra_env: dict[str, str] | None = None, timeout: int = 45, shell: str = "bash") -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env.pop("ADP_GATEWAY_DUMMY", None)
        env.update(
            {
                "HOME": str(adp_home),
                "PATH": f"{stub_tools}:{os.environ.get('PATH', '')}",
                "STUB_INVOCATION_LOG": str(invocation_log),
                "ADP_PROXY_PORT": str(proxy_port),
            }
        )
        if extra_env:
            env.update(extra_env)
        result = subprocess.run(
            [shell, str(adp_bin / "adp"), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )
        return subprocess.CompletedProcess(
            result.args,
            result.returncode,
            _strip_ansi(result.stdout),
            _strip_ansi(result.stderr),
        )

    _run.record = record  # type: ignore[attr-defined]
    _run.port = proxy_port  # type: ignore[attr-defined]
    _run.proxy_log = adp_home / ".adp" / "logs" / "proxy.log"  # type: ignore[attr-defined]
    _run.pidfile = adp_home / ".bedrock-gateway" / "proxy.pid"  # type: ignore[attr-defined]

    yield _run

    pidfile = adp_home / ".bedrock-gateway" / "proxy.pid"
    if pidfile.exists():
        try:
            pid = int(pidfile.read_text().strip())
        except (ValueError, OSError):
            pid = 0
        if pid:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.kill(pid, sig)
                except OSError:
                    break
                time.sleep(0.3)


def _seed_valid_session(home: Path) -> None:
    """A signed-in user whose access token is still good."""
    write_adp_session(home, ttl=3600)
    config = home / ".bedrock-gateway" / "config.json"
    config.write_text(json.dumps({"gateway_url": DEAD_GATEWAY_URL, "refresh_via": "gateway"}))


def _seed_dead_session(home: Path) -> None:
    """A session whose access token is expired AND whose refresh cannot succeed.

    This is the real "expired" case: an expired access token alone is NOT — it
    refreshes on next use, and a launcher that refused on it would be a bug.
    """
    write_adp_session(home, ttl=-3600)
    config = home / ".bedrock-gateway" / "config.json"
    config.write_text(json.dumps({"gateway_url": DEAD_GATEWAY_URL, "refresh_via": "gateway"}))


# --------------------------------------------------------------------------
# adp codex — starting the proxy
# --------------------------------------------------------------------------


class TestCodexStartsProxy:
    """The whole point: one command, one terminal, proxy handled for you."""

    @pytest.mark.parametrize("tool", ["codex", "claude"])
    def test_launch_without_arguments_works_on_system_bash(self, launch, adp_home, tool) -> None:
        # macOS /bin/bash is 3.2, whose nounset rejects empty array expansions.
        # Recording tools keep this a launcher test, with no live model calls.
        _seed_valid_session(adp_home)

        result = launch([tool], shell="/bin/bash")

        assert result.returncode == 0, result.stderr
        assert launch.record.launched(tool)

    def test_starts_proxy_then_execs_codex_with_passthrough_args(self, launch, adp_home: Path) -> None:
        _seed_valid_session(adp_home)

        result = launch(["codex", "--model", "gpt-5.6-sol", "-q", "hello world"])

        assert result.returncode == 0, result.stderr
        assert launch.record.launched("codex"), "codex was never exec'd"
        # Flags AND a quoted multi-word arg survive: the launcher must not
        # re-split or swallow anything the tool was meant to see.
        assert launch.record.argv == ["--model gpt-5.6-sol -q hello world"]

    def test_sets_the_dummy_env_var_itself(self, launch, adp_home: Path) -> None:
        """The `ADP_GATEWAY_DUMMY=unused codex` prefix users had to remember.

        Codex refuses to start a provider whose env_key names an unset variable.
        Since #5686 the value is not a placeholder but the capability the proxy
        published: the proxy requires it, because binding to loopback does not stop
        a web page the user visits from driving the proxy and spending their token.
        The launcher reads it from the proxy's 0600 identity file, which is the
        boundary a page cannot cross.

        The old literal `unused` must NOT appear — it is printed in our own README,
        so a capability equal to it would be public knowledge and no protection.
        """
        _seed_valid_session(adp_home)

        launch(["codex"])

        published = json.loads((adp_home / ".bedrock-gateway" / "proxy.json").read_text())["capability"]
        assert launch.record.dummy_env == [published]
        assert published != "unused"
        assert len(published) >= 16

    def test_a_stale_placeholder_in_the_users_shell_is_overridden(self, launch, adp_home: Path) -> None:
        """A shell that still exports the pre-#5686 placeholder must not win.

        Users were told for months to put `export ADP_GATEWAY_DUMMY=unused` in their
        shell config, and many did. If the launcher deferred to whatever the shell
        already exported, those users' Codex would present a value printed in our
        README — guessable by any web page — and the capability check would be
        decorative for exactly the population most likely to have followed the docs.
        """
        _seed_valid_session(adp_home)

        launch(["codex"], extra_env={"ADP_GATEWAY_DUMMY": "unused"})

        published = json.loads((adp_home / ".bedrock-gateway" / "proxy.json").read_text())["capability"]
        assert launch.record.dummy_env == [published], "the stale shell export was not overridden"

    def test_waits_for_readiness_before_launching(self, launch, adp_home: Path) -> None:
        """Codex's first request must not race the proxy's bind, or it 502s."""
        _seed_valid_session(adp_home)

        result = launch(["codex"])

        assert result.returncode == 0, result.stderr
        assert launch.record.launched("codex")
        # The proxy was accepting connections by the time the tool ran — asserted
        # after the fact, which is only sound because the proxy outlives the
        # launcher (it is detached, not a child that dies with it).
        assert _port_is_open(launch.port), "proxy was not listening when codex started"

    def test_proxy_survives_the_launcher_exiting(self, launch, adp_home: Path) -> None:
        """Detached, not a child: Codex outlives `adp` and still needs the proxy."""
        _seed_valid_session(adp_home)

        launch(["codex"])

        assert launch.pidfile.exists(), "no pidfile — proxy was not started via the core's serve"
        pid = int(launch.pidfile.read_text().strip())
        os.kill(pid, 0)  # raises if the proxy died with the launcher

    def test_proxy_output_goes_to_a_log_not_the_terminal(self, launch, adp_home: Path) -> None:
        """A detached proxy with nowhere to write is a proxy whose failure is invisible."""
        _seed_valid_session(adp_home)

        result = launch(["codex"])

        assert launch.proxy_log.exists(), "proxy.log was not created"
        assert "listening on" in launch.proxy_log.read_text()
        # The proxy's banner must not bleed into the terminal Codex is taking over.
        assert "point Codex at" not in result.stderr


# --------------------------------------------------------------------------
# adp codex — idempotence and the spawn race
# --------------------------------------------------------------------------


class TestCodexReusesProxy:
    """Starting a second proxy would clash on the port. Health-check first."""

    def test_reuses_an_already_running_proxy(self, launch, adp_home: Path) -> None:
        _seed_valid_session(adp_home)

        first = launch(["codex"])
        assert first.returncode == 0, first.stderr
        pid_after_first = launch.pidfile.read_text().strip()
        starts_after_first = len(_listening_lines(launch.proxy_log))

        second = launch(["codex"])

        assert second.returncode == 0, second.stderr
        assert launch.record.tools == ["codex", "codex"], "both launches must reach the tool"
        assert "already running" in second.stderr, "second run should say it is reusing the proxy"
        # Three independent witnesses that nothing was started twice.
        assert launch.pidfile.read_text().strip() == pid_after_first
        assert len(_listening_lines(launch.proxy_log)) == starts_after_first == 1
        assert "Starting the auth proxy" not in second.stderr

    def test_replaces_a_verified_legacy_proxy_without_a_capability(self, launch, adp_home: Path) -> None:
        """An upgraded CLI must never preserve a pre-#5686 vulnerable process."""
        _seed_valid_session(adp_home)
        first = launch(["codex"])
        assert first.returncode == 0, first.stderr

        identity_file = adp_home / ".bedrock-gateway" / "proxy.json"
        legacy_identity = json.loads(identity_file.read_text())
        old_pid = legacy_identity["pid"]
        legacy_identity.pop("capability")
        identity_file.write_text(json.dumps(legacy_identity))

        second = launch(["codex"])

        assert second.returncode == 0, second.stderr
        secured_identity = json.loads(identity_file.read_text())
        assert secured_identity["pid"] != old_pid
        assert len(secured_identity["capability"]) >= 16
        assert launch.record.dummy_env[-1] == secured_identity["capability"]
        assert "Replacing an insecure legacy auth proxy" in second.stderr

    def test_two_concurrent_launches_start_exactly_one_proxy(
        self, adp_bin: Path, adp_home: Path, stub_tools: Path, proxy_port: int, tmp_path: Path
    ) -> None:
        """The mkdir-lock. Without it both callers health-check, both see nothing,
        and both spawn — the second dying on a bind clash it does not report.
        """
        _seed_valid_session(adp_home)
        invocation_log = tmp_path / "concurrent.log"
        env = os.environ.copy()
        env.pop("ADP_GATEWAY_DUMMY", None)
        env.update(
            {
                "HOME": str(adp_home),
                "PATH": f"{stub_tools}:{os.environ.get('PATH', '')}",
                "STUB_INVOCATION_LOG": str(invocation_log),
                "ADP_PROXY_PORT": str(proxy_port),
            }
        )

        # Five, not two: the lock's loser path (health-check again, find the
        # winner's proxy) is where a bug would hide, so it is worth exercising
        # more than once per run.
        processes = [
            subprocess.Popen(
                ["bash", str(adp_bin / "adp"), "codex", f"run-{index}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            for index in range(5)
        ]
        outputs = [process.communicate(timeout=60) for process in processes]

        try:
            record = Invocations(invocation_log)
            assert len(record.tools) == 5, f"every launch must reach codex, got {record.tools}"
            assert all(process.returncode == 0 for process in processes)

            # Exactly one process reported starting a proxy...
            spawn_announcements = sum("Starting the auth proxy" in _strip_ansi(stderr) for _, stderr in outputs)
            assert spawn_announcements == 1, f"expected 1 spawn, {spawn_announcements} processes tried"
            # ...and exactly one proxy ever bound the port.
            assert len(_listening_lines(adp_home / ".adp" / "logs" / "proxy.log")) == 1
        finally:
            pidfile = adp_home / ".bedrock-gateway" / "proxy.pid"
            if pidfile.exists():
                try:
                    os.kill(int(pidfile.read_text().strip()), signal.SIGTERM)
                except (OSError, ValueError):
                    pass

    def test_a_lock_abandoned_by_a_crashed_process_is_reclaimed(self, launch, adp_home: Path) -> None:
        """Otherwise one crashed launch wedges every later one until it is deleted by hand.

        Same stale-reclaim contract as the core's refresh lock (#4837).
        """
        _seed_valid_session(adp_home)
        lock = adp_home / ".bedrock-gateway" / "proxy-spawn.lock"
        lock.mkdir(parents=True)
        stale = time.time() - 120
        os.utime(lock, (stale, stale))

        result = launch(["codex"])

        assert result.returncode == 0, result.stderr
        assert launch.record.launched("codex")
        assert not lock.exists(), "the lock must be released, not leaked to the next launch"

    def test_the_spawn_lock_is_released_after_a_normal_launch(self, launch, adp_home: Path) -> None:
        _seed_valid_session(adp_home)

        launch(["codex"])

        assert not (adp_home / ".bedrock-gateway" / "proxy-spawn.lock").exists()

    def test_a_proxy_on_another_port_fails_fast_and_legibly(self, launch, adp_home: Path) -> None:
        """The core keeps ONE pidfile regardless of port and refuses to run two.

        Found by smoke-testing: without this check the refusal surfaces as a 10s
        "did not come up" timeout with the real reason buried in proxy.log.
        """
        _seed_valid_session(adp_home)
        launch(["codex"])  # occupies the pidfile

        started = time.monotonic()
        result = launch(["codex"], extra_env={"ADP_PROXY_PORT": str(_free_port())})
        elapsed = time.monotonic() - started

        assert result.returncode != 0
        assert "already running" in result.stderr
        assert "Stop the original proxy session" in result.stderr, "must tell the user how to resolve it"
        assert elapsed < 8, f"should fail fast, not wait out the readiness timeout ({elapsed:.1f}s)"
        assert launch.record.tools == ["codex"], "codex must not launch against a proxy-less port"


# --------------------------------------------------------------------------
# Session preflight
# --------------------------------------------------------------------------


class TestSessionPreflight:
    """A dead session must stop the launch, not surface as a 401 mid-session."""

    @pytest.mark.parametrize("verb", ["codex", "claude"])
    def test_no_session_prints_login_hint_and_does_not_launch(self, launch, verb: str) -> None:
        result = launch([verb])

        assert result.returncode != 0
        assert LOGIN_HINT in result.stderr
        assert not launch.record.tools, f"{verb} was launched despite having no session"

    @pytest.mark.parametrize("verb", ["codex", "claude"])
    def test_dead_session_prints_login_hint_and_does_not_launch(self, launch, adp_home: Path, verb: str) -> None:
        """Expired access token AND an unreachable refresh — genuinely signed out."""
        _seed_dead_session(adp_home)

        result = launch([verb])

        assert result.returncode != 0
        assert LOGIN_HINT in result.stderr
        assert not launch.record.tools, f"{verb} was launched with a dead session"

    def test_dead_session_does_not_start_a_proxy(self, launch, adp_home: Path) -> None:
        """Preflight runs BEFORE the proxy: a signed-out user gets no stray process."""
        _seed_dead_session(adp_home)

        launch(["codex"])

        assert not launch.pidfile.exists()
        assert not _port_is_open(launch.port)

    def test_expired_access_token_alone_still_launches(self, launch, adp_home: Path, mock_aws_cli: Path) -> None:
        """The "wrongly declares a valid session expired" failure mode.

        An expired ACCESS token is the normal steady state — it refreshes on next
        use. Only a refresh that cannot succeed means signed-out. Here refresh
        succeeds (mock aws CLI), so the launch must proceed.
        """
        write_adp_session(adp_home, ttl=-60)
        (adp_home / ".bedrock-gateway" / "config.json").write_text(
            json.dumps({"gateway_url": DEAD_GATEWAY_URL, "client_id": "test-client", "region": "us-east-1"})
        )

        result = launch(
            ["claude"],
            extra_env={"PATH": f"{mock_aws_cli}:{os.environ.get('PATH', '')}"},
        )

        # PATH above drops the stub dir, so `claude` resolves to nothing and the
        # exec fails — but the PREFLIGHT is what is under test, and it must not be
        # the thing that stopped us.
        assert LOGIN_HINT not in result.stderr, "a refreshable session was wrongly called expired"

    def test_preflight_never_prints_the_token(self, launch, adp_home: Path) -> None:
        """The preflight calls `token`; its output must not reach a terminal."""
        _seed_valid_session(adp_home)
        access_token = json.loads((adp_home / ".bedrock-gateway" / "tokens.json").read_text())["access_token"]

        result = launch(["codex"])

        assert access_token not in result.stdout
        assert access_token not in result.stderr
        assert access_token not in launch.proxy_log.read_text(errors="replace")


# --------------------------------------------------------------------------
# adp claude — thin by design
# --------------------------------------------------------------------------


class TestClaudeLauncher:
    """Claude Code needs no proxy and no env var. Adding either would be a lie."""

    def test_launches_claude_with_passthrough_args(self, launch, adp_home: Path) -> None:
        _seed_valid_session(adp_home)

        result = launch(["claude", "--resume", "-p", "fix the build"])

        assert result.returncode == 0, result.stderr
        assert launch.record.tools == ["claude"]
        assert len(launch.record.argv) == 1
        assert launch.record.argv[0].endswith(" --resume -p fix the build")
        assert launch.record.argv[0].startswith("--settings ")

    def test_never_starts_the_proxy(self, launch, adp_home: Path) -> None:
        _seed_valid_session(adp_home)

        launch(["claude"])

        assert not _port_is_open(launch.port), "adp claude must not start the Codex proxy"
        assert not launch.pidfile.exists()
        assert not launch.proxy_log.exists()

    def test_never_sets_the_dummy_env_var(self, launch, adp_home: Path) -> None:
        """That variable exists only because Codex demands one. Claude has no use for it."""
        _seed_valid_session(adp_home)

        launch(["claude"])

        assert launch.record.dummy_env == ["<unset>"]


# --------------------------------------------------------------------------
# Verb routing — the setup subcommands must survive
# --------------------------------------------------------------------------


class TestVerbRouting:
    """`codex setup` and `codex` share a verb; neither may shadow the other."""

    def test_codex_setup_still_writes_config_and_does_not_launch(self, launch, adp_home: Path) -> None:
        _seed_valid_session(adp_home)

        result = launch(["codex", "setup"])

        assert result.returncode == 0, result.stderr
        assert (adp_home / ".codex" / "config.toml").exists()
        assert not launch.record.tools, "setup must configure, not launch"
        assert not _port_is_open(launch.port), "setup must not start the proxy"

    def test_claude_setup_still_writes_settings_and_does_not_launch(self, launch, adp_home: Path) -> None:
        _seed_valid_session(adp_home)

        result = launch(["claude", "setup"])

        assert result.returncode == 0, result.stderr
        assert (adp_home / ".claude" / "settings.json").exists()
        assert not launch.record.tools

    def test_codex_setup_points_at_the_port_the_launcher_uses(self, launch, adp_home: Path) -> None:
        """One port variable, so config.toml cannot disagree with what we serve."""
        _seed_valid_session(adp_home)

        launch(["codex", "setup"])

        config = (adp_home / ".codex" / "config.toml").read_text()
        assert f"http://127.0.0.1:{launch.port}/openai/v1" in config

    @pytest.mark.parametrize("verb", ["codex", "claude"])
    def test_double_dash_escapes_a_literal_setup_arg(self, launch, adp_home: Path, verb: str) -> None:
        """`adp codex -- setup` must reach the tool, not our config writer."""
        _seed_valid_session(adp_home)

        launch([verb, "--", "setup"])

        assert launch.record.tools == [verb]
        if verb == "claude":
            assert len(launch.record.argv) == 1
            assert launch.record.argv[0].endswith(" setup")
        else:
            assert launch.record.argv == ["setup"]

    def test_usage_documents_the_launchers_and_the_asymmetry(self, launch) -> None:
        """The Claude/Codex difference is documented, not hidden (issue's own words)."""
        result = launch(["help"])

        assert result.returncode == 0
        assert "codex [args...]" in result.stdout
        assert "claude [args...]" in result.stdout
        assert "daemon install" in result.stdout
        # Bare `claude` keeps working — users must not think this verb is required.
        assert "bare 'claude' also works" in result.stdout


# --------------------------------------------------------------------------
# adp daemon — opt-in always-on proxy
# --------------------------------------------------------------------------

_ON_MACOS = os.uname().sysname == "Darwin"


@pytest.fixture
def fake_launchd(stub_tools: Path, fake_launchd_factory) -> Path:
    return fake_launchd_factory(stub_tools)


class TestDaemonOnLinux:
    """Not silently broken off macOS: launchd is macOS-only and says so."""

    @pytest.mark.skipif(_ON_MACOS, reason="macOS has launchd; this is the fallback path")
    @pytest.mark.parametrize("subcommand", ["install", "uninstall"])
    def test_refuses_with_an_actionable_message(self, launch, adp_home: Path, subcommand: str) -> None:
        _seed_valid_session(adp_home)

        result = launch(["daemon", subcommand])

        assert result.returncode != 0
        assert "launchd" in result.stderr
        # ...and points at the path that does work here.
        assert "adp codex" in result.stderr
        assert not (adp_home / "Library" / "LaunchAgents" / "com.adp.gateway-proxy.plist").exists()

    def test_unknown_subcommand_is_rejected(self, launch, adp_home: Path) -> None:
        _seed_valid_session(adp_home)

        result = launch(["daemon", "restart"])

        assert result.returncode != 0
        assert "Unknown" in result.stderr or "unknown" in result.stderr

    def test_missing_subcommand_is_rejected(self, launch, adp_home: Path) -> None:
        _seed_valid_session(adp_home)

        result = launch(["daemon"])

        assert result.returncode != 0
        assert "install" in result.stderr


class TestDaemonInstall:
    """install/uninstall must round-trip cleanly — an orphan agent is a stray process."""

    @staticmethod
    def _plist(home: Path) -> Path:
        return home / "Library" / "LaunchAgents" / "com.adp.gateway-proxy.plist"

    @staticmethod
    def _proxy_status(port: int, capability: str) -> int:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request(
                "POST",
                "/openai/v1/responses",
                body=b"{}",
                headers={"Authorization": f"Bearer {capability}", "Content-Type": "application/json"},
            )
            return connection.getresponse().status
        finally:
            connection.close()

    @staticmethod
    def _seed_named_deployment(launch, home: Path, name: str, gateway_port: int) -> tuple[Path, str]:
        added = launch(["deployment", "add", name, "--url", f"http://127.0.0.1:{gateway_port}/api"])
        assert added.returncode == 0, added.stderr
        registry = json.loads((home / ".adp" / "deployments.json").read_text())
        deployment_id = registry["deployments"][name]["id"]
        root = home / ".adp" / "deployments" / deployment_id
        (root / "config.json").write_text(json.dumps({"gateway_url": f"http://127.0.0.1:{gateway_port}/api"}))
        (root / "tokens.json").write_text(
            json.dumps(
                {
                    "id_token": "id-token",
                    "access_token": f"token-for-{name}",
                    "refresh_token": "refresh-token",
                    "expires_at": int(time.time()) + 3600,
                }
            )
        )
        return root, deployment_id

    def test_install_writes_the_plist_and_the_rc_export(self, launch, adp_home: Path, fake_launchd: Path) -> None:
        _seed_valid_session(adp_home)
        rc = adp_home / ".zshrc"
        rc.write_text("# my shell config\nexport EDITOR=vim\n")

        result = launch(["daemon", "install"], extra_env={"SHELL": "/bin/zsh"})

        assert result.returncode == 0, result.stderr
        plist = plistlib.loads(self._plist(adp_home).read_bytes())
        capability_file = adp_home / ".adp" / "daemon-capability"
        capability = capability_file.read_text()
        assert plist["Label"] == "com.adp.gateway-proxy"
        assert plist["KeepAlive"] is True
        assert plist["ProgramArguments"][-1] == str(launch.port)
        assert plist["EnvironmentVariables"]["ADP_GATEWAY_DUMMY"] == capability
        assert len(capability) >= 16
        assert capability_file.stat().st_mode & 0o777 == 0o600
        assert capability not in result.stdout
        assert capability not in result.stderr
        # Bare `codex` needs the variable from the shell — no launcher sets it there.
        assert "daemon-capability" in rc.read_text()
        assert capability not in rc.read_text(), "the capability must not be embedded in a normally-readable rc file"
        assert "export EDITOR=vim" in rc.read_text(), "must not clobber the user's rc"

    def test_install_replaces_an_existing_on_demand_proxy(self, launch, adp_home: Path, fake_launchd: Path) -> None:
        _seed_valid_session(adp_home)

        started = launch(["codex"])
        assert started.returncode == 0, started.stderr
        first_identity = json.loads((adp_home / ".bedrock-gateway" / "proxy.json").read_text())

        installed = launch(["daemon", "install"], extra_env={"SHELL": "/bin/zsh"})

        assert installed.returncode == 0, installed.stderr
        replacement = json.loads((adp_home / ".bedrock-gateway" / "proxy.json").read_text())
        durable = (adp_home / ".adp" / "daemon-capability").read_text()
        assert replacement["pid"] != first_identity["pid"]
        assert replacement["capability"] == durable
        old_state = subprocess.run(["ps", "-o", "stat=", "-p", str(first_identity["pid"])], capture_output=True, text=True, timeout=5).stdout.strip()
        assert not old_state or old_state.startswith("Z"), f"old proxy is still running with state {old_state}"

        bare = subprocess.run(
            ["bash", "-c", '. "$HOME/.zshrc"; codex'],
            env={
                "HOME": str(adp_home),
                "PATH": f"{fake_launchd}:{os.environ.get('PATH', '')}",
                "STUB_INVOCATION_LOG": str(launch.record._log),
            },
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert bare.returncode == 0, bare.stderr
        assert launch.record.dummy_env[-1] == durable
        assert self._proxy_status(replacement["port"], first_identity["capability"]) == 403
        assert self._proxy_status(replacement["port"], durable) != 403

    def test_install_is_idempotent_and_restart_stable(self, launch, adp_home: Path, fake_launchd: Path) -> None:
        _seed_valid_session(adp_home)
        rc = adp_home / ".zshrc"
        rc.write_text("export EDITOR=vim\n")

        launch(["daemon", "install"], extra_env={"SHELL": "/bin/zsh"})
        first = rc.read_text()
        first_capability = (adp_home / ".adp" / "daemon-capability").read_text()
        launch(["daemon", "install"], extra_env={"SHELL": "/bin/zsh"})

        assert rc.read_text() == first, "a second install must not stack rc lines"
        assert rc.read_text().count("ADP_GATEWAY_DUMMY") == 1
        assert (adp_home / ".adp" / "daemon-capability").read_text() == first_capability
        plist = plistlib.loads(self._plist(adp_home).read_bytes())
        assert plist["EnvironmentVariables"]["ADP_GATEWAY_DUMMY"] == first_capability

    def test_rc_export_matches_the_verified_first_process(self, launch, adp_home: Path, fake_launchd: Path) -> None:
        _seed_valid_session(adp_home)

        result = launch(["daemon", "install"], extra_env={"SHELL": "/bin/zsh"})
        assert result.returncode == 0, result.stderr
        identity = json.loads((adp_home / ".bedrock-gateway" / "proxy.json").read_text())

        shell = subprocess.run(
            ["bash", "-c", '. "$HOME/.zshrc"; printf %s "$ADP_GATEWAY_DUMMY"'],
            env={"HOME": str(adp_home), "PATH": os.environ.get("PATH", "")},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert shell.returncode == 0, shell.stderr
        assert shell.stdout == (adp_home / ".adp" / "daemon-capability").read_text()
        assert shell.stdout == identity["capability"]

    def test_multiple_deployments_share_one_capability_through_switch_and_uninstall(self, launch, adp_home: Path, fake_launchd: Path) -> None:
        dev_root, dev_id = self._seed_named_deployment(launch, adp_home, "dev", 1)
        integration_root, integration_id = self._seed_named_deployment(launch, adp_home, "integration", 2)
        rc = adp_home / ".zshrc"
        environment = {"ADP_PROXY_PORT": "", "SHELL": "/bin/zsh"}

        dev_install = launch(["--deployment", "dev", "daemon", "install"], extra_env=environment)
        integration_install = launch(["--deployment", "integration", "daemon", "install"], extra_env=environment)

        assert dev_install.returncode == 0, dev_install.stderr
        assert integration_install.returncode == 0, integration_install.stderr
        durable_file = adp_home / ".adp" / "daemon-capability"
        durable = durable_file.read_text()
        dev_identity = json.loads((dev_root / "runtime" / "proxy.json").read_text())
        integration_identity = json.loads((integration_root / "runtime" / "proxy.json").read_text())
        launch_agents = adp_home / "Library" / "LaunchAgents"
        dev_plist = plistlib.loads((launch_agents / f"com.adp.gateway-proxy.{dev_id}.plist").read_bytes())
        integration_plist = plistlib.loads((launch_agents / f"com.adp.gateway-proxy.{integration_id}.plist").read_bytes())
        assert dev_identity["capability"] == integration_identity["capability"] == durable
        assert dev_plist["EnvironmentVariables"]["ADP_GATEWAY_DUMMY"] == durable
        assert integration_plist["EnvironmentVariables"]["ADP_GATEWAY_DUMMY"] == durable
        assert rc.read_text().count("ADP_GATEWAY_DUMMY") == 1

        switched = launch(["deployment", "use", "integration"])
        assert switched.returncode == 0, switched.stderr
        shell = subprocess.run(
            ["bash", "-c", '. "$HOME/.zshrc"; printf %s "$ADP_GATEWAY_DUMMY"'],
            env={"HOME": str(adp_home), "PATH": os.environ.get("PATH", "")},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert shell.stdout == durable
        assert self._proxy_status(dev_identity["port"], durable) != 403
        assert self._proxy_status(integration_identity["port"], durable) != 403

        integration_uninstall = launch(["--deployment", "integration", "daemon", "uninstall"], extra_env=environment)
        assert integration_uninstall.returncode == 0, integration_uninstall.stderr
        assert durable_file.read_text() == durable
        assert "ADP_GATEWAY_DUMMY" in rc.read_text()
        assert (launch_agents / f"com.adp.gateway-proxy.{dev_id}.plist").exists()
        assert not (launch_agents / f"com.adp.gateway-proxy.{integration_id}.plist").exists()
        assert self._proxy_status(dev_identity["port"], durable) != 403

        dev_uninstall = launch(["--deployment", "dev", "daemon", "uninstall"], extra_env=environment)
        assert dev_uninstall.returncode == 0, dev_uninstall.stderr
        assert not durable_file.exists()
        assert "ADP_GATEWAY_DUMMY" not in rc.read_text()

    def test_install_migrates_the_marked_legacy_placeholder(self, launch, adp_home: Path, fake_launchd: Path) -> None:
        _seed_valid_session(adp_home)
        rc = adp_home / ".zshrc"
        rc.write_text("# user setting\n# Added by adp daemon install\nexport ADP_GATEWAY_DUMMY=unused\n")

        result = launch(["daemon", "install"], extra_env={"SHELL": "/bin/zsh"})

        assert result.returncode == 0, result.stderr
        assert "unused" not in rc.read_text()
        assert "daemon-capability" in rc.read_text()
        assert "# user setting" in rc.read_text()

    def test_uninstall_removes_both_the_plist_and_the_rc_export(self, launch, adp_home: Path, fake_launchd: Path) -> None:
        _seed_valid_session(adp_home)
        rc = adp_home / ".zshrc"
        rc.write_text("export EDITOR=vim\n")
        launch(["daemon", "install"], extra_env={"SHELL": "/bin/zsh"})

        result = launch(["daemon", "uninstall"], extra_env={"SHELL": "/bin/zsh"})

        assert result.returncode == 0, result.stderr
        assert not self._plist(adp_home).exists()
        assert not (adp_home / ".adp" / "daemon-capability").exists()
        assert "ADP_GATEWAY_DUMMY" not in rc.read_text()
        assert "export EDITOR=vim" in rc.read_text()

    def test_uninstall_without_an_install_is_not_an_error(self, launch, adp_home: Path, fake_launchd: Path) -> None:
        """Idempotent both ways, so it is safe in a teardown script."""
        _seed_valid_session(adp_home)

        result = launch(["daemon", "uninstall"], extra_env={"SHELL": "/bin/zsh"})

        assert result.returncode == 0, result.stderr

    def test_uninstall_leaves_a_users_own_dummy_export_alone(self, launch, adp_home: Path, fake_launchd: Path) -> None:
        """Only OUR marked line is removed — an unmarked one is the user's."""
        _seed_valid_session(adp_home)
        rc = adp_home / ".zshrc"
        rc.write_text("export ADP_GATEWAY_DUMMY=mine\n")

        launch(["daemon", "uninstall"], extra_env={"SHELL": "/bin/zsh"})

        assert "export ADP_GATEWAY_DUMMY=mine" in rc.read_text()
