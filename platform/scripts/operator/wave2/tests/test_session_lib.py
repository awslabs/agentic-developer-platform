#!/usr/bin/env python3
"""Tests for lib/session.sh (issue #3968).

These exist because of a real defect found by running the orchestrator, not by
reading the code:

    w2_assert_account's STDOUT IS ITS RETURN VALUE ("<account> <arn>"), and it
    calls w2_warn_if_ambient_worker, which used w2_note -- which writes to stdout.
    So on any in-pod run with adp-cred present, the four-line #5195 warning was
    captured as part of the account id.

It failed CLOSED (the emptiness check in w2_require_account refused), so it was
never a security hole. But it broke every legitimate in-pod run -- the exact
environment the worker credential path exists for -- and the symptom ("account
assertion produced no account id") names nothing that would lead an operator to
the cause.

The general rule these tests pin: a function whose stdout is a value must send
diagnostics to stderr.

Run: python3 -m pytest platform/scripts/operator/wave2/tests/ -q
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SESSION_SH = Path(__file__).resolve().parents[1] / "lib" / "session.sh"
GOOD_ACCOUNT = "879318057152"
WRONG_ACCOUNT = "605440105851"


@pytest.fixture
def shell(tmp_path: Path):
    """Run a snippet against the real session.sh with stubbed aws/adp-cred."""
    bindir = tmp_path / "bin"
    bindir.mkdir()

    def write_stub(name: str, body: str) -> None:
        path = bindir / name
        path.write_text("#!/usr/bin/env bash\n" + body)
        path.chmod(0o755)

    # Built by concatenation rather than %-formatting: the shell body contains its
    # own literal %s for printf, which collides with Python's format operator.
    write_stub("aws", (
        'acct="${STUB_ACCOUNT:-' + GOOD_ACCOUNT + '}"\n'
        'case "$*" in\n'
        '  *get-caller-identity*)\n'
        '    printf \'{"Account":"%s","Arn":"arn:aws:sts::%s:assumed-role/r/s","UserId":"u"}\\n\''
        ' "$acct" "$acct" ;;\n'
        '  *) : ;;\n'
        'esac\n'
    ))
    # Present so w2_cred_mode can select vault and the ambient-worker warning can
    # trigger; --exec runs the rest of the argv.
    write_stub("adp-cred", 'while [ "$1" != "--exec" ]; do shift; done; shift; exec "$@"\n')

    def run(snippet: str, env=None):
        environment = dict(os.environ)
        environment.update({"PATH": f"{bindir}:{os.environ['PATH']}"})
        for key in ("W2_CRED_MODE", "AWS_PROFILE", "KUBERNETES_SERVICE_HOST",
                    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
            environment.pop(key, None)
        if env:
            environment.update(env)
        return subprocess.run(
            ["bash", "-c", f". {SESSION_SH}\n{snippet}"],
            capture_output=True, text=True, env=environment, timeout=60,
        )

    return run


# ---------------------------------------------------------------------------
# the defect: diagnostics must not contaminate a returned value
# ---------------------------------------------------------------------------
IN_POD = {"KUBERNETES_SERVICE_HOST": "10.100.0.1"}


def test_account_value_is_clean_inside_a_pod(shell) -> None:
    """THE regression. In a pod with adp-cred and no explicit W2_CRED_MODE, the
    ambient-worker warning fires -- and must not end up inside the account id."""
    proc = shell('ident="$(w2_assert_account)"; printf "[%s]\\n" "$ident"', env=IN_POD)
    assert proc.returncode == 0, proc.stderr
    assert f"[{GOOD_ACCOUNT} arn:aws:sts::{GOOD_ACCOUNT}:assumed-role/r/s]" in proc.stdout
    assert "NOTE" not in proc.stdout, "the warning must not be captured into the value"


def test_the_warning_is_still_emitted_on_stderr(shell) -> None:
    """Moving it off stdout must not silence it: it is the #5195 guard."""
    proc = shell("w2_assert_account >/dev/null", env=IN_POD)
    assert "W2_CRED_MODE" in proc.stderr
    assert "WRONG ACCOUNT" in proc.stderr


def test_require_account_sets_the_account_inside_a_pod(shell) -> None:
    """What the orchestrator actually does. Before the fix this refused with
    "account assertion produced no account id" on every in-pod run."""
    proc = shell('w2_require_account; printf "ACCOUNT=[%s]\\n" "$W2_ACCOUNT"', env=IN_POD)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"ACCOUNT=[{GOOD_ACCOUNT}]" in proc.stdout


def test_arn_does_not_absorb_the_warning(shell) -> None:
    """W2_ARN is taken with `${ident#* }`, so warning text would land there too."""
    proc = shell('w2_require_account; printf "ARN=[%s]\\n" "$W2_ARN"', env=IN_POD)
    assert "ARN=[arn:aws:sts::" in proc.stdout
    assert "NOTE" not in proc.stdout


def test_no_value_returning_helper_writes_diagnostics_to_stdout() -> None:
    """Guards the general rule, not just the one instance.

    w2_cred_mode, w2_assert_account, w2_kubeconfig and w2_warn_if_ambient_worker
    all run inside `$(...)`, so a w2_note/w2_ok in any of them corrupts a value.
    """
    source = SESSION_SH.read_text()
    for name in ("w2_cred_mode", "w2_warn_if_ambient_worker", "w2_kubeconfig"):
        start = source.index(f"{name}() {{")
        body = source[start:source.index("\n}\n", start)]
        for forbidden in ("w2_note ", "w2_ok "):
            assert forbidden not in body, (
                f"{name} writes to stdout via {forbidden.strip()}; its stdout is a value, so "
                "this would corrupt it. Use w2_diag (stderr) instead."
            )


# ---------------------------------------------------------------------------
# the account gate itself still holds
# ---------------------------------------------------------------------------
def test_wrong_account_refuses(shell) -> None:
    """The #5195 failure mode."""
    proc = shell("w2_require_account; echo REACHED",
                 env={"STUB_ACCOUNT": WRONG_ACCOUNT, "AWS_PROFILE": "p"})
    assert proc.returncode != 0
    assert "REACHED" not in proc.stdout
    assert WRONG_ACCOUNT in proc.stdout + proc.stderr


def test_refusal_is_not_swallowed_by_a_subshell(shell) -> None:
    """Regression for the already-fixed subshell bypass.

    `read ... <<<"$(w2_assert_account)"` let the script continue after a refusal,
    because `exit 1` inside a command substitution kills only the subshell.
    """
    proc = shell("w2_require_account; echo REACHED",
                 env={"STUB_ACCOUNT": WRONG_ACCOUNT, "AWS_PROFILE": "p"})
    assert "REACHED" not in proc.stdout, "execution continued past an account refusal"


def test_unreadable_identity_refuses(shell, tmp_path: Path) -> None:
    """An identity that cannot be read is not a passing identity."""
    broken = tmp_path / "bin" / "aws"
    broken.write_text("#!/usr/bin/env bash\nexit 255\n")
    broken.chmod(0o755)
    proc = shell("w2_require_account; echo REACHED", env={"AWS_PROFILE": "p"})
    assert proc.returncode != 0
    assert "REACHED" not in proc.stdout


def test_explicit_cred_mode_suppresses_the_ambient_warning(shell) -> None:
    """An operator who chose a mode does not need to be told to choose one."""
    proc = shell("w2_assert_account >/dev/null",
                 env=dict(IN_POD, W2_CRED_MODE="vault"))
    assert "no explicit W2_CRED_MODE" not in proc.stderr


def test_invalid_cred_mode_is_rejected(shell) -> None:
    proc = shell("w2_cred_mode", env={"W2_CRED_MODE": "banana"})
    assert proc.returncode != 0
    assert "banana" in proc.stderr
