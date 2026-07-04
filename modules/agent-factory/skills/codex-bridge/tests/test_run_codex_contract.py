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


# ---------------------------------------------------------------------------
# JSONL observability contract (issue #2753)
#
# The wrapper now runs `codex exec --json`, tees the raw JSONL to a pod-local
# file, and pipes it through render-codex-events.py. These tests pin that the
# structured-output flag is passed, the raw log is persisted byte-for-byte, the
# renderer produces compact step lines + a verbatim final message, and — most
# importantly — none of that weakens the pre-existing safety contract above
# (Codex's own exit code still propagates through the pipe; timeout still 124).
# ---------------------------------------------------------------------------

RENDER_SCRIPT = SCRIPT.parent / "render-codex-events.py"

# One known-shape Codex `exec --json` event stream (schema verified against the
# pinned CLI 0.142.5).
KNOWN_JSONL = "\n".join(
    [
        '{"type":"thread.started","thread_id":"019f2732-abc"}',
        '{"type":"turn.started"}',
        '{"type":"item.completed","item":{"id":"i0","type":"reasoning","text":"Plan the change"}}',
        '{"type":"item.completed","item":{"id":"i1","type":"file_change",'
        '"changes":[{"path":"/tmp/x/hello.txt","kind":"add"}]}}',
        '{"type":"item.completed","item":{"id":"i2","type":"command_execution",'
        "\"command\":\"/bin/bash -lc 'ls -la'\",\"exit_code\":0}}",
        '{"type":"item.completed","item":{"id":"i3","type":"agent_message",'
        '"text":"Done: created hello.txt.\\nSecond line of the deliverable."}}',
        '{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":10,'
        '"output_tokens":20,"reasoning_output_tokens":5}}',
    ]
)


def _make_jsonl_stub(tmp_path: Path, jsonl: str, *, rc: int = 0, stderr: str = "") -> Path:
    """Stub `codex` that echoes a JSONL stream and fails loudly if --json is absent."""
    err_line = f"echo {stderr!r} >&2\n" if stderr else ""
    body = (
        'seen_json=0\n'
        'for a in "$@"; do [ "$a" = "--json" ] && seen_json=1; done\n'
        'if [ "$seen_json" -ne 1 ]; then echo "STUB: --json missing" >&2; exit 99; fi\n'
        "cat <<'JSONL_EOF'\n" + jsonl + "\nJSONL_EOF\n" + err_line + f"exit {rc}\n"
    )
    return _make_stub_codex(tmp_path, body)


class TestJsonlObservability:
    def test_json_flag_is_passed_to_codex(self, tmp_path):
        stub = _make_stub_codex(tmp_path, _ARGV_ECHO)
        result = _run(
            ["write", "do a thing"],
            env_extra={"CODEX_BIN": str(stub), "CODEX_RUNS_DIR": str(tmp_path / "runs")},
        )
        assert result.returncode == 0, result.stderr
        argv = _parse_argv(result.stdout)
        assert "--json" in argv, f"--json not passed to codex; argv={argv!r}"
        # Argv integrity still holds: the instruction is the final literal argv.
        assert argv[-1] == "do a thing"

    def test_known_stream_renders_steps_and_verbatim_final_message(self, tmp_path):
        stub = _make_jsonl_stub(tmp_path, KNOWN_JSONL)
        result = _run(
            ["write", "make hello"],
            env_extra={"CODEX_BIN": str(stub), "CODEX_RUNS_DIR": str(tmp_path / "runs")},
        )
        assert result.returncode == 0, result.stderr
        out = result.stdout
        assert "[codex reasoning] Plan the change" in out
        assert "[codex edit] add /tmp/x/hello.txt" in out
        assert "[codex exec] $ /bin/bash -lc 'ls -la' (exit 0)" in out
        # Final agent message passes through verbatim (both lines), untruncated.
        assert "Done: created hello.txt." in out
        assert "Second line of the deliverable." in out
        # Trailer carries session id + token usage.
        assert "session: 019f2732-abc" in out
        assert "input=100" in out and "output=20" in out

    def test_raw_jsonl_persisted_and_byte_matches_stub(self, tmp_path):
        runs = tmp_path / "runs"
        stub = _make_jsonl_stub(tmp_path, KNOWN_JSONL)
        result = _run(
            ["write", "make hello"],
            env_extra={"CODEX_BIN": str(stub), "CODEX_RUNS_DIR": str(runs)},
        )
        assert result.returncode == 0, result.stderr
        logs = list(runs.glob("*.jsonl"))
        assert len(logs) == 1, f"expected exactly one raw log, got {logs!r}"
        # The renderer trailer names the same path …
        assert str(logs[0]) in result.stdout
        # … and the raw log byte-matches what the stub emitted (heredoc adds \n).
        assert logs[0].read_text() == KNOWN_JSONL + "\n"

    def test_unknown_event_type_degrades_to_generic_line(self, tmp_path):
        jsonl = "\n".join(
            [
                '{"type":"thread.started","thread_id":"t1"}',
                '{"type":"item.completed","item":{"type":"web_search","query":"foo"}}',
                '{"type":"item.completed","item":{"type":"agent_message","text":"final"}}',
            ]
        )
        stub = _make_jsonl_stub(tmp_path, jsonl)
        result = _run(
            ["write", "x"],
            env_extra={"CODEX_BIN": str(stub), "CODEX_RUNS_DIR": str(tmp_path / "runs")},
        )
        assert result.returncode == 0, result.stderr
        assert "[codex web_search]" in result.stdout
        assert "final" in result.stdout

    def test_malformed_non_json_line_passes_through_without_crash(self, tmp_path):
        jsonl = "\n".join(
            [
                '{"type":"thread.started","thread_id":"t1"}',
                "this is not json at all",
                '{"type":"item.completed","item":{"type":"agent_message","text":"final"}}',
            ]
        )
        stub = _make_jsonl_stub(tmp_path, jsonl)
        result = _run(
            ["write", "x"],
            env_extra={"CODEX_BIN": str(stub), "CODEX_RUNS_DIR": str(tmp_path / "runs")},
        )
        assert result.returncode == 0, result.stderr
        assert "this is not json at all" in result.stdout
        assert "final" in result.stdout


class TestJsonlSafetyContractPreserved:
    """The JSONL pipe must not weaken the #2705/#2711 safety contract."""

    def test_nonzero_codex_rc_still_propagates_through_pipe(self, tmp_path):
        # Codex emits events, then exits 5. tee|renderer must NOT mask it —
        # rc comes from PIPESTATUS[0], not the (0-exit) renderer.
        stub = _make_jsonl_stub(tmp_path, KNOWN_JSONL, rc=5, stderr="boom: model unreachable")
        result = _run(
            ["write", "x"],
            env_extra={"CODEX_BIN": str(stub), "CODEX_RUNS_DIR": str(tmp_path / "runs")},
        )
        assert result.returncode == 5, result.stderr
        assert "boom: model unreachable" in result.stderr
        assert "exited non-zero (5)" in result.stderr
        # Events still rendered even though Codex ultimately failed.
        assert "[codex reasoning] Plan the change" in result.stdout

    def test_hard_timeout_still_returns_124_under_json_pipe(self, tmp_path):
        stub = _make_stub_codex(tmp_path, "sleep 30\n")
        result = _run(
            ["write", "hang please"],
            env_extra={
                "CODEX_BIN": str(stub),
                "CODEX_TIMEOUT": "1",
                "CODEX_RUNS_DIR": str(tmp_path / "runs"),
            },
        )
        assert result.returncode == 124
        assert "timed out after 1s" in result.stderr


class TestLiveEventsFile:
    """Live-stream events-file contract (issue #2884).

    The wrapper additionally tees the SAME JSONL stream to a stable, well-known
    path (CODEX_EVENTS_FILE) so the agent-worker's codexEventWatcher has one
    file to tail while Codex runs. These tests pin that:
      - the stable file receives the identical event stream;
      - the per-run archival tee (#2753) is unchanged and byte-matches;
      - the file is TRUNCATED (not appended) at delegation start;
      - Codex's own exit code still propagates (PIPESTATUS unaffected).
    """

    def test_events_file_receives_the_event_stream(self, tmp_path):
        events_file = tmp_path / "events" / "current.jsonl"
        stub = _make_jsonl_stub(tmp_path, KNOWN_JSONL)
        result = _run(
            ["write", "make hello"],
            env_extra={
                "CODEX_BIN": str(stub),
                "CODEX_RUNS_DIR": str(tmp_path / "runs"),
                "CODEX_EVENTS_FILE": str(events_file),
            },
        )
        assert result.returncode == 0, result.stderr
        assert events_file.is_file(), "stable events file was not written"
        # The stable file carries the identical raw JSONL stream.
        assert events_file.read_text() == KNOWN_JSONL + "\n"

    def test_events_file_matches_archival_tee_byte_for_byte(self, tmp_path):
        events_file = tmp_path / "events" / "current.jsonl"
        runs = tmp_path / "runs"
        stub = _make_jsonl_stub(tmp_path, KNOWN_JSONL)
        result = _run(
            ["write", "make hello"],
            env_extra={
                "CODEX_BIN": str(stub),
                "CODEX_RUNS_DIR": str(runs),
                "CODEX_EVENTS_FILE": str(events_file),
            },
        )
        assert result.returncode == 0, result.stderr
        logs = list(runs.glob("*.jsonl"))
        assert len(logs) == 1, f"expected exactly one archival log, got {logs!r}"
        # Both sinks of the single tee see the same bytes (#2753 tee unchanged).
        assert events_file.read_text() == logs[0].read_text()

    def test_events_file_is_truncated_not_appended(self, tmp_path):
        events_file = tmp_path / "events" / "current.jsonl"
        events_file.parent.mkdir(parents=True)
        # Pre-seed the stable file with a prior delegation's content.
        events_file.write_text("STALE PRIOR DELEGATION CONTENT\n")
        stub = _make_jsonl_stub(tmp_path, KNOWN_JSONL)
        result = _run(
            ["write", "make hello"],
            env_extra={
                "CODEX_BIN": str(stub),
                "CODEX_RUNS_DIR": str(tmp_path / "runs"),
                "CODEX_EVENTS_FILE": str(events_file),
            },
        )
        assert result.returncode == 0, result.stderr
        # tee (no -a) must have truncated the stale content.
        assert "STALE PRIOR DELEGATION CONTENT" not in events_file.read_text()
        assert events_file.read_text() == KNOWN_JSONL + "\n"

    def test_nonzero_rc_still_propagates_with_events_file_set(self, tmp_path):
        events_file = tmp_path / "events" / "current.jsonl"
        stub = _make_jsonl_stub(tmp_path, KNOWN_JSONL, rc=5, stderr="boom")
        result = _run(
            ["write", "x"],
            env_extra={
                "CODEX_BIN": str(stub),
                "CODEX_RUNS_DIR": str(tmp_path / "runs"),
                "CODEX_EVENTS_FILE": str(events_file),
            },
        )
        # Adding the second tee sink must not disturb PIPESTATUS[0].
        assert result.returncode == 5, result.stderr
        assert "exited non-zero (5)" in result.stderr
        assert events_file.read_text() == KNOWN_JSONL + "\n"


class TestRenderer:
    """Unit tests for render-codex-events.py driven directly on stdin."""

    def _render(self, jsonl: str, *, jsonl_path=None) -> str:
        args = ["python3", str(RENDER_SCRIPT)]
        if jsonl_path:
            args += ["--jsonl-path", jsonl_path]
        result = subprocess.run(args, input=jsonl, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        return result.stdout

    def test_final_message_precedes_trailer_and_path_is_named(self):
        out = self._render(KNOWN_JSONL, jsonl_path="/tmp/codex-runs/x.jsonl")
        assert out.index("Done: created hello.txt.") < out.index("── codex run summary ──")
        assert "log:     /tmp/codex-runs/x.jsonl" in out

    def test_empty_input_is_silent(self):
        assert self._render("") == ""

    def test_intermediate_messages_kept_compact_final_verbatim(self):
        jsonl = "\n".join(
            [
                '{"type":"item.completed","item":{"type":"agent_message","text":"'
                + "x" * 400
                + '"}}',
                '{"type":"item.completed","item":{"type":"agent_message","text":"FINAL"}}',
            ]
        )
        out = self._render(jsonl)
        # The first (intermediate) message is compacted to one truncated line.
        assert "[codex message]" in out
        assert "…" in out
        # The last message is the verbatim deliverable.
        assert out.rstrip().endswith("FINAL")
