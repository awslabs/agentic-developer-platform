"""Behavioral tests for the codex-bridge run-codex.sh wrapper (issue #2705).

These prove the safety properties called out in the issue's impact table:
  * the instruction reaches Codex as a SINGLE literal argv — quotes, `$()`, and
    newlines are NOT shell-interpreted (no injection from issue text);
  * the hard timeout path returns non-zero within the limit (no pod hang);
  * static/assumed AWS_* credentials in the env are cleared before invoking
    Codex (credential isolation — Codex signs with pod IRSA).

The real `codex` binary is not present in CI, so the tests point CODEX_BIN at a
stub script that records exactly what argv it received.
"""

from __future__ import annotations

import os
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
RUN_CODEX = (
    HERE.parent.parent
    / "skills"
    / "codex-bridge"
    / "scripts"
    / "run-codex.sh"
)


def _write_stub(tmp_path: Path, body: str) -> Path:
    """Write an executable stub `codex` and return its path."""
    stub = tmp_path / "codex-stub.sh"
    stub.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return stub


def _run(args, *, env=None, cwd=None):
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(
        ["bash", str(RUN_CODEX), *args],
        capture_output=True,
        text=True,
        env=full_env,
        cwd=cwd,
        timeout=30,
    )


def test_script_exists_and_is_executable() -> None:
    assert RUN_CODEX.exists(), f"missing wrapper at {RUN_CODEX}"
    assert os.access(RUN_CODEX, os.X_OK), "run-codex.sh must be executable"


def test_instruction_is_single_argv_no_word_splitting(tmp_path: Path) -> None:
    # Stub dumps each received argument on its own line.
    stub = _write_stub(
        tmp_path,
        """
        # $1 == "exec"; the trailing arg is the instruction. Print every arg.
        for a in "$@"; do echo "ARG:$a"; done
        """,
    )
    tricky = 'add func; rm -rf /; echo "$(whoami)" `id` and \'quotes\''
    result = _run(["write", tricky], env={"CODEX_BIN": str(stub)})
    assert result.returncode == 0, result.stderr

    # The whole instruction must arrive as exactly ONE argv line, byte-for-byte.
    arg_lines = [
        ln[len("ARG:") :] for ln in result.stdout.splitlines() if ln.startswith("ARG:")
    ]
    assert tricky in arg_lines, (
        f"instruction was split/interpreted; got args={arg_lines!r}"
    )
    # And the injected command must NOT have executed (no side effect on stdout).
    assert "root" not in result.stdout  # `whoami`/`id` never ran


def test_newline_in_instruction_preserved_as_single_arg(tmp_path: Path) -> None:
    stub = _write_stub(
        tmp_path,
        """
        # Emit the final arg framed so we can assert it survived intact.
        last="${!#}"
        printf 'BEGIN>%s<END' "$last"
        """,
    )
    multiline = "line one\nline two\nline three"
    result = _run(["write", multiline], env={"CODEX_BIN": str(stub)})
    assert result.returncode == 0, result.stderr
    assert f"BEGIN>{multiline}<END" in result.stdout


def test_timeout_path_returns_nonzero_within_limit(tmp_path: Path) -> None:
    # Stub sleeps well past the (overridden) 1s timeout.
    stub = _write_stub(tmp_path, "sleep 30\n")
    result = _run(
        ["write", "anything"],
        env={"CODEX_BIN": str(stub), "CODEX_TIMEOUT": "1"},
    )
    assert result.returncode != 0
    assert result.returncode == 124  # `timeout` SIGTERM exit code
    assert "timed out" in result.stderr.lower()


def test_nonzero_exit_is_surfaced(tmp_path: Path) -> None:
    stub = _write_stub(tmp_path, "echo 'boom' >&2\nexit 7\n")
    result = _run(["write", "task"], env={"CODEX_BIN": str(stub)})
    assert result.returncode == 7
    assert "boom" in result.stderr
    assert "non-zero" in result.stderr.lower()


def test_aws_static_credentials_are_cleared(tmp_path: Path) -> None:
    # Stub reports whether the leaked static creds survived into Codex's env.
    stub = _write_stub(
        tmp_path,
        """
        echo "KEY=${AWS_ACCESS_KEY_ID:-unset}"
        echo "TOKEN=${AWS_SESSION_TOKEN:-unset}"
        echo "PROFILE=${AWS_PROFILE:-unset}"
        echo "REGION=${AWS_REGION:-unset}"
        """,
    )
    result = _run(
        ["write", "task"],
        env={
            "CODEX_BIN": str(stub),
            "AWS_ACCESS_KEY_ID": "AKIAEXAMPLE",
            "AWS_SECRET_ACCESS_KEY": "secret",
            "AWS_SESSION_TOKEN": "assumed-customer-token",
            "AWS_PROFILE": "customer",
        },
    )
    assert result.returncode == 0, result.stderr
    assert "KEY=unset" in result.stdout
    assert "TOKEN=unset" in result.stdout
    assert "PROFILE=unset" in result.stdout
    # Region is pinned to the mantle region.
    assert "REGION=us-east-1" in result.stdout


def test_review_mode_requires_existing_file(tmp_path: Path) -> None:
    stub = _write_stub(tmp_path, "echo ok\n")
    missing = tmp_path / "nope.py"
    result = _run(["review", str(missing)], env={"CODEX_BIN": str(stub)})
    assert result.returncode == 2
    assert "not found" in result.stderr.lower()


def test_review_mode_passes_readonly_prompt_with_path(tmp_path: Path) -> None:
    stub = _write_stub(tmp_path, 'printf "%s" "${!#}"\n')
    target = tmp_path / "changed.py"
    target.write_text("x = 1\n")
    result = _run(["review", str(target)], env={"CODEX_BIN": str(stub)})
    assert result.returncode == 0, result.stderr
    assert str(target) in result.stdout
    assert "do not modify" in result.stdout.lower()


def test_bad_mode_is_rejected(tmp_path: Path) -> None:
    stub = _write_stub(tmp_path, "echo ok\n")
    result = _run(["frobnicate", "x"], env={"CODEX_BIN": str(stub)})
    assert result.returncode == 2


def test_wrong_arg_count_is_rejected(tmp_path: Path) -> None:
    stub = _write_stub(tmp_path, "echo ok\n")
    result = _run(["write"], env={"CODEX_BIN": str(stub)})
    assert result.returncode == 2


@pytest.mark.skipif(
    subprocess.run(["which", "shellcheck"], capture_output=True).returncode != 0,
    reason="shellcheck not installed",
)
def test_shellcheck_clean() -> None:
    result = subprocess.run(
        ["shellcheck", str(RUN_CODEX)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
