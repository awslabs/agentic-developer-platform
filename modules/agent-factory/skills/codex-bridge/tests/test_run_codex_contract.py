"""Contract tests for the codex-bridge wrapper `run-codex.sh` (issue #2711).

These pin the safety contract that #2705 established for the wrapper, so a later
edit that weakens it fails CI instead of a live run:

- **argv integrity** — the instruction reaches Codex as ONE literal argv, never
  shell-evaluated (quotes, `$()`, backticks, newlines survive verbatim). This is
  the injection-regression guard.
- **hard timeout** — the wrapper kills a hung Codex at `CODEX_TIMEOUT` and
  returns exit 124 with a diagnostic on stderr (a TTY-less pod cannot be
  un-hung by a human).
- **failure surfacing** — a non-zero Codex exit is propagated and its stderr is
  surfaced; the wrapper never swallows it or retries.
- **review-mode guard** — a missing review target exits 2 before Codex runs.
- **usage guard** — wrong arg count exits 2 with usage.
- **credential isolation** — static/assumed AWS credential vars are cleared so
  Codex signs with the pod's IRSA identity, never assumed customer creds.
- **shellcheck clean** — the script passes `shellcheck --severity=error`.

The tests drive the real script with a **stub** `CODEX_BIN` (a tiny shell script
that records its argv / sleeps / exits non-zero) rather than the real Codex CLI,
so they are hermetic and safe to run in CI. This mirrors the repo's existing
pattern of testing shell scripts via pytest + subprocess (see
`modules/agent-factory/tests/test_scripts.py`), since `bats` is not available.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

# --- Locate the script under test -------------------------------------------
SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "run-codex.sh"


def _run(args, *, env_extra=None, cwd=None):
    """Invoke run-codex.sh with a clean-ish env plus overrides."""
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
        timeout=30,
    )


def _make_stub_codex(tmp_path: Path, body: str) -> Path:
    """Write an executable stub that stands in for the `codex` binary."""
    stub = tmp_path / "codex-stub.sh"
    stub.write_text("#!/usr/bin/env bash\n" + body)
    stub.chmod(0o755)
    return stub


# A stub that wraps each argv element in unique sentinels so we can recover it
# exactly — even when the argv itself contains newlines. `codex exec ...
# "<instruction>"` → the last argv is the instruction.
_ARGV_OPEN = "<<<ARGV_BEGIN>>>"
_ARGV_CLOSE = "<<<ARGV_END>>>"
_ARGV_ECHO = r"""
for arg in "$@"; do
    printf '<<<ARGV_BEGIN>>>%s<<<ARGV_END>>>' "$arg"
done
"""


def _parse_argv(stdout: str) -> list[str]:
    """Recover the argv list the stub was invoked with, newlines preserved."""
    argv = []
    rest = stdout
    while _ARGV_OPEN in rest:
        _, _, rest = rest.partition(_ARGV_OPEN)
        value, _, rest = rest.partition(_ARGV_CLOSE)
        argv.append(value)
    return argv


class TestScriptShape:
    def test_script_exists_and_executable_bit_is_documented(self):
        assert SCRIPT.is_file(), f"missing wrapper: {SCRIPT}"

    def test_usage_error_on_wrong_arg_count(self):
        # No args → usage + exit 2.
        result = _run([])
        assert result.returncode == 2
        assert "Usage:" in result.stderr

    def test_usage_error_on_unknown_mode(self, tmp_path):
        stub = _make_stub_codex(tmp_path, _ARGV_ECHO)
        result = _run(["frobnicate", "something"], env_extra={"CODEX_BIN": str(stub)})
        assert result.returncode == 2
        assert "Usage:" in result.stderr


class TestArgvIntegrity:
    """The instruction must reach Codex as a single, literal argv."""

    @pytest.mark.parametrize(
        "instruction",
        [
            "Add a hello() function to hello.py",
            "print $(whoami) and `id` literally",  # command-substitution attempt
            "use \"double\" and 'single' quotes",
            "line one\nline two\nline three",  # embedded newlines
            "semicolons; && pipes | and > redirects",
            "unicode: café — 日本語 — 🚀",
        ],
    )
    def test_write_instruction_passed_as_single_literal_argv(self, tmp_path, instruction):
        stub = _make_stub_codex(tmp_path, _ARGV_ECHO)
        result = _run(["write", instruction], env_extra={"CODEX_BIN": str(stub)})
        assert result.returncode == 0, result.stderr
        # The instruction must be exactly ONE argv element, byte-identical to the
        # input — proving it was neither split on whitespace/newlines nor
        # shell-evaluated. Sentinel parsing preserves embedded newlines.
        argv = _parse_argv(result.stdout)
        assert instruction in argv, (
            f"instruction not passed as a single literal argv; got: {argv!r}"
        )
        # No shell evaluation: literal markers must survive untouched.
        if "$(whoami)" in instruction:
            assert "$(whoami)" in argv[-1] and "`id`" in argv[-1]

    def test_codex_receives_exec_subcommand_and_flags(self, tmp_path):
        stub = _make_stub_codex(tmp_path, _ARGV_ECHO)
        result = _run(["write", "do a thing"], env_extra={"CODEX_BIN": str(stub)})
        assert result.returncode == 0, result.stderr
        argv = _parse_argv(result.stdout)
        assert argv[0] == "exec"
        assert "--dangerously-bypass-approvals-and-sandbox" in argv
        assert "--skip-git-repo-check" in argv
        # The instruction is the final argv.
        assert argv[-1] == "do a thing"


class TestReviewMode:
    def test_review_missing_file_exits_2_before_running_codex(self, tmp_path):
        stub = _make_stub_codex(tmp_path, "echo SHOULD_NOT_RUN\n")
        result = _run(
            ["review", str(tmp_path / "does-not-exist.py")], env_extra={"CODEX_BIN": str(stub)}
        )
        assert result.returncode == 2
        assert "review target not found" in result.stderr
        assert "SHOULD_NOT_RUN" not in result.stdout

    def test_review_existing_file_builds_readonly_prompt(self, tmp_path):
        target = tmp_path / "changed.py"
        target.write_text("def f():\n    return 1\n")
        stub = _make_stub_codex(tmp_path, _ARGV_ECHO)
        result = _run(["review", str(target)], env_extra={"CODEX_BIN": str(stub)})
        assert result.returncode == 0, result.stderr
        argv = _parse_argv(result.stdout)
        prompt = argv[-1]
        assert str(target) in prompt
        # Read-only intent must be explicit in the synthesized prompt.
        assert "Do not modify any files." in prompt


class TestTimeout:
    def test_hard_timeout_kills_hung_codex_and_returns_124(self, tmp_path):
        # Stub that hangs forever; CODEX_TIMEOUT=1s must kill it.
        stub = _make_stub_codex(tmp_path, "sleep 30\n")
        result = _run(
            ["write", "hang please"],
            env_extra={"CODEX_BIN": str(stub), "CODEX_TIMEOUT": "1"},
        )
        assert result.returncode == 124
        assert "timed out after 1s" in result.stderr


class TestFailureSurfacing:
    def test_nonzero_exit_is_propagated_and_stderr_surfaced(self, tmp_path):
        # Stub that writes to stderr and exits 3.
        stub = _make_stub_codex(tmp_path, "echo 'boom: model unreachable' >&2\nexit 3\n")
        result = _run(["write", "do a thing"], env_extra={"CODEX_BIN": str(stub)})
        assert result.returncode == 3
        # Codex's own stderr is surfaced …
        assert "boom: model unreachable" in result.stderr
        # … plus the wrapper's diagnostic naming the exit code.
        assert "exited non-zero (3)" in result.stderr

    def test_success_exit_zero_passes_through(self, tmp_path):
        stub = _make_stub_codex(tmp_path, "echo 'done'\nexit 0\n")
        result = _run(["write", "do a thing"], env_extra={"CODEX_BIN": str(stub)})
        assert result.returncode == 0
        assert "done" in result.stdout


class TestCredentialIsolation:
    def test_static_aws_creds_are_cleared_before_invoking_codex(self, tmp_path):
        # Stub prints whether the static-cred vars are visible in Codex's env.
        stub = _make_stub_codex(
            tmp_path,
            'echo "AKID=[${AWS_ACCESS_KEY_ID:-unset}]"\n'
            'echo "SECRET=[${AWS_SECRET_ACCESS_KEY:-unset}]"\n'
            'echo "TOKEN=[${AWS_SESSION_TOKEN:-unset}]"\n'
            'echo "PROFILE=[${AWS_PROFILE:-unset}]"\n',
        )
        result = _run(
            ["write", "do a thing"],
            env_extra={
                "CODEX_BIN": str(stub),
                "AWS_ACCESS_KEY_ID": "AKIACUSTOMER",
                "AWS_SECRET_ACCESS_KEY": "customer-secret",
                "AWS_SESSION_TOKEN": "customer-token",
                "AWS_PROFILE": "customer-profile",
            },
        )
        assert result.returncode == 0, result.stderr
        # Codex must NOT see the assumed-customer static credentials — they are
        # unset so the SDK falls back to the pod IRSA web-identity chain.
        assert "AKID=[unset]" in result.stdout
        assert "SECRET=[unset]" in result.stdout
        assert "TOKEN=[unset]" in result.stdout
        assert "PROFILE=[unset]" in result.stdout
        # The customer secret must never leak into stdout/stderr.
        assert "AKIACUSTOMER" not in result.stdout
        assert "customer-secret" not in result.stdout

    def test_aws_region_is_pinned(self, tmp_path):
        stub = _make_stub_codex(tmp_path, 'echo "REGION=[${AWS_REGION:-unset}]"\n')
        result = _run(["write", "x"], env_extra={"CODEX_BIN": str(stub)})
        assert result.returncode == 0, result.stderr
        assert "REGION=[us-east-1]" in result.stdout


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
class TestShellcheck:
    def test_shellcheck_severity_error_clean(self):
        result = subprocess.run(
            ["shellcheck", "--severity=error", "--format=gcc", str(SCRIPT)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"shellcheck errors:\n{result.stdout}"


class TestBashSyntax:
    def test_bash_n_parses_clean(self):
        result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
