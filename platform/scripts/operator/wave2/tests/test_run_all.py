#!/usr/bin/env python3
"""Tests for the Wave 2 orchestrator (issue #3968, root's blocker 7).

Root asked for "an executable orchestration path". The README previously had a
copy-paste runbook, which leaves the ordering, the run-id threading, the
stop-on-failure decision and the cleanup guarantee to whoever is pasting.

These tests drive `run-all.sh` with every step replaced by a stub, so the
ORCHESTRATION is under test rather than the steps. The three properties:

  1. cleanup always runs -- on success, on failure, on a gate failure, on a signal;
  2. nothing is created without --apply;
  3. a skipped step is never reported as a pass, and a run with skips exits non-zero.

Property 3 is the one that matters most. A green exit over a partially-skipped run
is the vacuous pass this whole PR exists to remove.

Run: python3 -m pytest platform/scripts/operator/wave2/tests/ -q
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

WAVE2 = Path(__file__).resolve().parents[1]
RUN_ALL = WAVE2 / "run-all.sh"

# Every step script the orchestrator invokes, and the cleanup.
STEP_SCRIPTS = [
    "00-verify-target.sh",
    "10-create-fixture.sh",
    "15-verify-isolation.sh",
    "20-collect-pause-evidence.sh",
    "22-collect-suite-evidence.sh",
    "30-seed-and-count.sh",
    "40-verify-edge-sessions.sh",
    "90-cleanup-ledger.sh",
]

STUB = """#!/usr/bin/env bash
echo "$(basename "$0") $*" >> "$W2_TEST_CALLLOG"
exit ${W2_STUB_RC_%s:-0}
"""


@pytest.fixture
def harness(tmp_path: Path):
    """A wave2 directory whose steps are stubs, so only ordering is exercised."""
    sandbox = tmp_path / "wave2"
    sandbox.mkdir()
    (sandbox / "lib").mkdir()
    (sandbox / "run-all.sh").write_text(RUN_ALL.read_text())
    (sandbox / "run-all.sh").chmod(0o755)

    # The real session library: account assertion is part of the orchestration and
    # must not be stubbed away. Its aws/kubectl calls go to stub binaries below.
    (sandbox / "lib" / "session.sh").write_text((WAVE2 / "lib" / "session.sh").read_text())

    for name in STEP_SCRIPTS:
        var = name.split("-")[0]
        script = sandbox / name
        body = STUB % var
        if name == "10-create-fixture.sh":
            # Faithful to the real script: the ledger is written BEFORE each
            # resource is created, so even a partial creation leaves a cleanable
            # record. The orchestrator keys its cleanup on the ledger's existence,
            # so a stub that never wrote one would make cleanup correctly skip and
            # the cleanup tests would be testing nothing.
            # It also writes the expected-identity document, because the real script
            # writes one whenever it BINDS a worker pod, and step 20 requires it on the
            # live path. A stub that skipped it would make every downstream assertion
            # about step 20 test the no-bound-worker skip instead.
            # W2_STUB_NO_WORKER=1 reproduces the other real outcome: a fixture created
            # with no bound worker (not created, or created and refused).
            body = body.replace(
                'exit ${W2_STUB_RC_10:-0}',
                'case "$*" in *--check-only*) : ;; '
                '*) printf \'{"run_id":"w2-test","resources":[]}\\n\' > "$W2_TEST_LEDGER"\n'
                '   if [ "${W2_STUB_NO_WORKER:-0}" != "1" ]; then\n'
                '     printf \'{"expected_identity":{"pod_uid":"stub-uid"}}\\n\' \\\n'
                '       > "$(dirname "$W2_TEST_LEDGER")/expected-identity.json"\n'
                '   fi ;; esac\n'
                'exit ${W2_STUB_RC_10:-0}',
            )
        script.write_text(body)
        script.chmod(0o755)

    # `python3 ../../agent-control-eval.py` is resolved relative to the script dir.
    # It logs its argv like every other stub: the orchestrator's contract with the
    # evaluator (notably --evidence-dir) is only observable in what it was passed.
    evaldir = sandbox.parent.parent
    (evaldir / "agent-control-eval.py").write_text(
        "import os, sys\n"
        "with open(os.environ['W2_TEST_CALLLOG'], 'a') as fh:\n"
        "    fh.write('agent-control-eval.py ' + ' '.join(sys.argv[1:]) + '\\n')\n"
        "sys.exit(int(os.environ.get('W2_STUB_RC_60', '0')))\n"
    )

    bindir = tmp_path / "bin"
    bindir.mkdir()
    # aws sts get-caller-identity must return the expected account or the
    # orchestrator refuses before any step -- which is correct, and is covered by
    # test_wrong_account_refuses_before_any_step.
    #
    # It must emit real JSON: w2_assert_account parses the document rather than
    # splitting text. A plain-text stub made every test here fail at the account
    # gate, which is the library behaving correctly -- an identity it cannot read is
    # not a passing identity.
    (bindir / "aws").write_text(
        '#!/usr/bin/env bash\n'
        'echo "aws $*" >> "$W2_TEST_CALLLOG"\n'
        'case "$*" in\n'
        '  *get-caller-identity*)\n'
        '    printf \'{"Account":"%s","Arn":"arn:aws:sts::%s:assumed-role/r/s","UserId":"u"}\\n\' \\\n'
        '      "${W2_STUB_ACCOUNT:-879318057152}" "${W2_STUB_ACCOUNT:-879318057152}" ;;\n'
        '  *) : ;;\n'
        'esac\n'
    )
    (bindir / "aws").chmod(0o755)
    (bindir / "kubectl").write_text(
        '#!/usr/bin/env bash\n'
        'echo "kubectl $*" >> "$W2_TEST_CALLLOG"\n'
        'echo "${W2_STUB_POD:-}"\n'
    )
    (bindir / "kubectl").chmod(0o755)

    calllog = tmp_path / "calls.log"
    calllog.touch()

    def run(*args, env=None, expect=None):
        environment = dict(os.environ)
        environment.update({
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "W2_TEST_CALLLOG": str(calllog),
            "W2_TEST_LEDGER": str(tmp_path / "ev" / "cleanup-ledger.json"),
            "HOME": str(tmp_path),
            "AWS_PROFILE": "adp-embark1",   # exercise the env-credential mode
            "W2_EVIDENCE_ROOT": str(tmp_path / "ev"),
        })
        for key in ("W2_OWNER", "W2_NONOWNER", "W2_OTHER_TENANT", "W2_ADMIN"):
            environment.pop(key, None)
        if env:
            environment.update(env)
        proc = subprocess.run(
            ["bash", str(sandbox / "run-all.sh"), "--run-id", "w2-test",
             "--evidence-dir", str(tmp_path / "ev"), *args],
            capture_output=True, text=True, env=environment, cwd=str(sandbox), timeout=120,
        )
        if expect is not None:
            assert proc.returncode == expect, (
                f"expected exit {expect}, got {proc.returncode}\n"
                f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
            )
        return proc, calllog.read_text()

    return run


# ---------------------------------------------------------------------------
# property 2: nothing is created without --apply
# ---------------------------------------------------------------------------
def test_default_run_creates_nothing(harness) -> None:
    """The default invocation must be safe to run while reviewing."""
    proc, calls = harness()
    assert "10-create-fixture.sh" in calls, "the dry-run should still happen"
    for line in calls.splitlines():
        if line.startswith("10-create-fixture.sh"):
            assert "--check-only" in line, "creation must not run without --apply"
    assert "created" not in proc.stdout.lower() or "--apply" in proc.stdout


def test_paid_step_never_runs_without_apply(harness) -> None:
    """Spending real money must require an explicit act."""
    _, calls = harness()
    assert "20-collect-pause-evidence.sh" not in calls


def test_apply_runs_creation_and_isolation(harness) -> None:
    _, calls = harness("--apply")
    assert any(
        line.startswith("10-create-fixture.sh") and "--check-only" not in line
        for line in calls.splitlines()
    ), "--apply must run the real creation"
    assert "15-verify-isolation.sh" in calls


def test_isolation_is_verified_before_the_experiment(harness) -> None:
    """A control measurement through an unproven boundary is not attributable.

    Ordering is the property, so it is asserted on the call log rather than assumed
    from the source order.
    """
    _, calls = harness("--apply")
    lines = [line.split()[0] for line in calls.splitlines() if line.strip()]
    assert "15-verify-isolation.sh" in lines and "20-collect-pause-evidence.sh" in lines
    assert lines.index("15-verify-isolation.sh") < lines.index("20-collect-pause-evidence.sh")


# ---------------------------------------------------------------------------
# property 1: cleanup always runs
# ---------------------------------------------------------------------------
def test_cleanup_runs_after_a_successful_apply(harness) -> None:
    _, calls = harness("--apply", "--no-paid")
    assert "90-cleanup-ledger.sh" in calls


def test_cleanup_runs_after_a_gate_failure(harness) -> None:
    """The case an operator most reliably gets wrong.

    A fixture left running is a control-flag-ON gateway plus a live queue. Root
    already found orphaned resources of unknown ownership in this account.
    """
    _, calls = harness("--apply", env={"W2_STUB_RC_15": "5"}, expect=1)
    assert "90-cleanup-ledger.sh" in calls
    assert "20-collect-pause-evidence.sh" not in calls, "a gate failure must stop the run"


def test_a_failure_before_creation_reports_cleanup_as_unnecessary(harness) -> None:
    """When step 00 fails there is no ledger, so cleanup has nothing to act on.

    The property is that the cleanup decision is always REACHED and REPORTED --
    not that a delete always happens. Asserting the delete here would have been
    wrong in the other direction: it would demand teardown of resources that do
    not exist, and a cleanup script invoked with no ledger is exactly the
    "treat any error as absence" shape this PR removes elsewhere.

    What the report may NOT do is claim nothing was created. A missing ledger means
    nothing was RECORDED as created, and a crash between creating a resource and
    writing its ledger entry produces a byte-identical state. This previously
    printed the flat assertion "nothing was created", which is the same
    unobserved-value-reported-as-observed defect in miniature -- so the SKIP is
    counted (the run exits non-zero) and the wording is now the claim actually
    supported by the evidence.
    """
    proc, calls = harness("--apply", env={"W2_STUB_RC_00": "1"}, expect=1)
    assert "10-create-fixture.sh" not in calls, "nothing may be created after a target failure"
    assert "90-cleanup-ledger.sh" not in calls, "no ledger, so there is nothing to tear down"
    assert "nothing is RECORDED as created" in proc.stdout, (
        "the cleanup decision must still be reported")
    assert "nothing was created" not in proc.stdout, (
        "a missing ledger does not establish that nothing was created; an interrupted "
        "creation looks identical, so the report must not claim absence")
    assert "skipped steps: cleanup" in proc.stdout, (
        "an unperformed cleanup must be counted as skipped, not silently omitted")


def test_keep_fixture_skips_cleanup_but_says_so_loudly(harness) -> None:
    """An escape hatch that leaves a trace beats one operators improvise."""
    proc, calls = harness("--apply", "--no-paid", "--keep-fixture", expect=1)
    assert "90-cleanup-ledger.sh" not in calls
    assert "STILL RUNNING" in proc.stdout
    assert "90-cleanup-ledger.sh" in proc.stdout, "it must print the teardown command"


def test_a_failing_cleanup_is_reported_not_swallowed(harness) -> None:
    """"I ran cleanup" is not "the resources are gone"."""
    proc, _ = harness("--apply", "--no-paid", env={"W2_STUB_RC_90": "1"}, expect=1)
    assert "CLEANUP DID NOT FULLY SUCCEED" in proc.stderr
    assert "Do NOT assume absence" in proc.stdout or "Do NOT assume absence" in proc.stderr


# ---------------------------------------------------------------------------
# property 3: a skip is never a pass
# ---------------------------------------------------------------------------
def test_a_run_with_skips_exits_non_zero(harness) -> None:
    """THE property. Every step could be skipped and the run must not look green."""
    proc, _ = harness(expect=1)
    assert "skipped" in proc.stdout


def test_skipped_steps_are_counted_separately_from_passes(harness) -> None:
    proc, _ = harness()
    summary = proc.stdout
    assert "SKIP" in summary, "skips must be visible in the summary, not folded into passes"
    assert "passed" in summary and "failed" in summary and "skipped" in summary


def test_missing_tokens_skip_identities_rather_than_inventing_them(harness) -> None:
    _, calls = harness("--apply", "--no-paid")
    assert "40-verify-edge-sessions.sh" not in calls


def test_identities_run_when_tokens_are_present(harness) -> None:
    """With a fixture pod discovered, the sessions must be fixture-scoped."""
    _, calls = harness("--apply", "--no-paid", env={
        "W2_OWNER": "a", "W2_NONOWNER": "b", "W2_OTHER_TENANT": "c",
        "W2_STUB_POD": "w2-fixture-gateway-test-abc",
    })
    line = next(l for l in calls.splitlines() if l.startswith("40-verify-edge-sessions.sh"))
    assert "--fixture-pod w2-fixture-gateway-test-abc" in line
    assert "--run-id w2-test" in line


def test_seed_is_skipped_without_an_admin_token(harness) -> None:
    """An org-scoped token silently reports every delta as 0, so absence must
    refuse rather than proceed and produce zeros that look like real counts."""
    proc, calls = harness("--apply", "--no-paid")
    assert "30-seed-and-count.sh" not in calls
    assert "W2_ADMIN" in proc.stdout


def test_harness_is_skipped_when_no_config_was_produced(harness) -> None:
    """Running the evaluator over absent artifacts would grade nothing as something."""
    proc, calls = harness("--apply", "--no-paid")
    assert "agent-control-eval.py" not in calls
    assert "fixture-config.json" in proc.stdout


# ---------------------------------------------------------------------------
# run identity and the account gate
# ---------------------------------------------------------------------------
def test_wrong_account_refuses_before_any_step(harness) -> None:
    """The #5195 failure mode: an ambient credential resolving to another account.

    This must stop before step 00, and -- because the refusal lives in
    `w2_require_account` rather than a command substitution -- must not continue
    with an empty account id.
    """
    proc, calls = harness("--apply", env={"W2_STUB_ACCOUNT": "605440105851"}, expect=1)
    assert "00-verify-target.sh" not in calls
    assert "10-create-fixture.sh" not in calls
    assert "605440105851" in proc.stdout + proc.stderr


def test_one_run_id_is_threaded_through_every_step(harness) -> None:
    """Two run ids would leave the ledger naming resources cleanup cannot find."""
    _, calls = harness("--apply", "--no-paid")
    ids = {
        part
        for line in calls.splitlines()
        for i, part in enumerate(line.split())
        if i and line.split()[i - 1] == "--run-id"
    }
    assert ids == {"w2-test"}, f"expected one run id, saw {ids}"


def test_from_resumes_and_skips_earlier_steps(harness) -> None:
    _, calls = harness("--from", "22")
    assert "00-verify-target.sh" not in calls
    assert "10-create-fixture.sh" not in calls
    assert "22-collect-suite-evidence.sh" in calls


# ---------------------------------------------------------------------------
# the exit-status accounting root reproduced directly
# ---------------------------------------------------------------------------
# Root's reproduction, quoted: "Executed the unmodified run_cleanup and on_exit
# functions with a cleanup command that exits 19 and no other failed/skipped steps.
# It logs FAIL cleanup but exits 0. run_cleanup does not increment FAILED on
# failure, or SKIPPED for --keep-fixture/missing ledger. Consequently --from 61 can
# also run zero checks and exit green."
#
# These are EXECUTION regressions, not source assertions: each drives the real
# script to a real exit status. The distinction matters because the defect was
# entirely in the accounting -- the FAIL line was always printed correctly, and any
# test that only grepped stdout would have passed against the broken version.


def test_cleanup_exit_19_makes_the_whole_run_exit_nonzero(harness) -> None:
    """The exact reproduction: a cleanup that exits 19 and nothing else wrong.

    19 is not a code the script special-cases -- it stands for "the teardown
    reported failures" generically. A control-flag-ON gateway and a live queue are
    still up; a zero exit here tells CI and the operator that the run was clean.
    """
    proc, calls = harness("--apply", "--no-paid", env={"W2_STUB_RC_90": "19"}, expect=1)
    assert "90-cleanup-ledger.sh" in calls, "cleanup must have actually been attempted"
    assert "exit 19" in proc.stdout, "the summary must record the real exit status"
    assert "failed steps:" in proc.stdout and "cleanup" in proc.stdout.split("failed steps:")[1]


def test_cleanup_failure_is_counted_in_the_failed_total(harness) -> None:
    """Not just the exit code: the counter itself has to move.

    The FAIL line and the FAILED counter were separate, so the summary could read
    "failed 0" directly above a FAIL cleanup row. Asserting the total closes the
    gap between what the run says happened and what it counted.
    """
    proc, _ = harness("--apply", "--no-paid", env={"W2_STUB_RC_90": "19"}, expect=1)
    totals = next(l for l in proc.stdout.splitlines() if l.startswith("passed "))
    assert "failed 0" not in totals, (
        f"cleanup failed but the totals line does not count it: {totals!r}")


def test_keep_fixture_is_counted_as_a_skip_not_ignored(harness) -> None:
    """--keep-fixture leaves the fixture live, which is the least green outcome there is.

    The escape hatch is legitimate, so this is not a failure -- but it is a skip,
    and a skipped teardown must be in the skipped total and in the exit status.
    """
    proc, _ = harness("--apply", "--no-paid", "--keep-fixture", expect=1)
    totals = next(l for l in proc.stdout.splitlines() if l.startswith("passed "))
    assert "skipped 0" not in totals, f"--keep-fixture was not counted: {totals!r}"
    assert "cleanup" in proc.stdout.split("skipped steps:")[1]


def test_missing_ledger_is_a_skip_and_claims_no_absence(harness) -> None:
    """A missing ledger is indistinguishable from an interrupted creation.

    Driven by making step 10 succeed WITHOUT writing a ledger -- which is precisely
    the crash-after-create-before-record shape. Recording this as "clean" would be
    the false-absence defect at the orchestration level.
    """
    proc, calls = harness(
        "--apply", "--no-paid",
        env={"W2_TEST_LEDGER": "/dev/null/unwritable/ledger.json"}, expect=1)
    assert "90-cleanup-ledger.sh" not in calls
    totals = next(l for l in proc.stdout.splitlines() if l.startswith("passed "))
    assert "skipped 0" not in totals, f"a missing ledger was not counted: {totals!r}"
    assert "interrupted creation would look identical" in proc.stdout


def test_from_past_the_last_step_refuses_instead_of_exiting_green(harness, tmp_path) -> None:
    """`--from 61`: matched no step, ran nothing, exited 0.

    The worst shape in the script, because it is silent. Nothing failed and nothing
    was skipped -- there was nothing at all -- so every counter stayed 0 and the
    run reported success over an empty execution.

    A LEDGER IS PLANTED FIRST, deliberately. Without one the cleanup step skips,
    which pushes SKIPPED above zero and makes the run exit non-zero for an
    unrelated reason -- so the test would pass against the broken script and prove
    nothing. With a ledger present, cleanup runs and passes, and the ONLY thing that
    can make this invocation non-zero is the empty-run guard itself.
    """
    (tmp_path / "ev").mkdir(exist_ok=True)
    (tmp_path / "ev" / "cleanup-ledger.json").write_text('{"run_id":"w2-test","resources":[]}')
    proc, calls = harness("--from", "61", expect=1)
    assert "00-verify-target.sh" not in calls
    assert "22-collect-suite-evidence.sh" not in calls
    combined = proc.stdout + proc.stderr
    assert "not a step" in combined or "reached no step" in combined
    assert "0 10 15 20 22 30 40 60" in combined, (
        "the refusal must name the valid resume points, or the operator is guessing")


def test_a_valid_from_still_discloses_what_was_not_run(harness) -> None:
    """A resume must not let earlier steps read as passed.

    Their results live in a previous run's evidence directory and are not carried
    forward. Silence here reads as "everything before 22 was fine".
    """
    proc, _ = harness("--from", "22")
    assert "NOT RUN in this invocation" in proc.stdout
    not_run = proc.stdout.split("NOT RUN in this invocation")[1].splitlines()[0]
    for step in ("0", "10", "15", "20"):
        assert step in not_run, f"step {step} precedes 22 but is not disclosed as not-run"
    assert "establishes nothing about" in proc.stdout


# ---------------------------------------------------------------------------
# the --apply path actually seeds, using verified identities
# ---------------------------------------------------------------------------
def _sessions(harness_dir: Path, payload: dict) -> None:
    artifacts = harness_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "identity_sessions.json").write_text(json.dumps(payload))


VERIFIED_SESSIONS = {
    "all_authenticated": True,
    "distinct_principals": True,
    "problems": [],
    "sessions": {
        "owner": {"status": 200, "user_id": "u-owner-1", "org_id": "t-tenant-1"},
        "nonowner": {"status": 200, "user_id": "u-non-2", "org_id": "t-tenant-1"},
    },
}


def test_apply_seeds_using_the_verified_owner_identity(harness, tmp_path) -> None:
    """Root's finding: "The advertised full --apply path unconditionally skips
    30-seed-apply."

    So the full run never seeded, and W2-06 -- which reads back an aborted row --
    had no row to read on every --apply invocation. The ids are not inferred: they
    come from step 40's artifact, where each is the gateway's verdict on a real
    token rather than a locally decoded JWT.
    """
    _sessions(tmp_path / "ev", VERIFIED_SESSIONS)
    _, calls = harness("--apply", "--no-paid", "--gateway-url", "https://gw.example",
                       env={"W2_ADMIN": "admin-token"})
    seeds = [l for l in calls.splitlines()
             if l.startswith("30-seed-and-count.sh") and "--check-only" not in l]
    assert seeds, "the --apply path must actually seed, not skip unconditionally"
    assert "--owner-user-id u-owner-1" in seeds[0]
    assert "--owner-tenant-id t-tenant-1" in seeds[0]


@pytest.mark.parametrize("payload,reason", [
    ({"all_authenticated": False, "distinct_principals": True, "sessions": {}},
     "sessions that did not all authenticate"),
    ({"all_authenticated": True, "distinct_principals": False, "problems": ["collapsed"],
      "sessions": {"owner": {"status": 200, "user_id": "u", "org_id": "t"}}},
     "owner and nonowner resolving to the same principal"),
    ({"all_authenticated": True, "distinct_principals": True,
      "sessions": {"owner": {"status": 403}}},
     "an owner session the gateway rejected"),
    ({"all_authenticated": True, "distinct_principals": True,
      "sessions": {"owner": {"status": 200, "user_id": "u-only"}}},
     "an owner session with no tenant"),
])
def test_seeding_fails_closed_when_the_identity_is_not_established(
    harness, tmp_path, payload, reason
) -> None:
    """Every way the identity can be unestablished must SKIP, never guess.

    Seeding under an unverified owner writes a synthetic row into a real tenant
    that nothing in the ledger can attribute -- strictly worse than not seeding,
    because the row outlives the run and looks like real traffic.
    """
    _sessions(tmp_path / "ev", payload)
    proc, calls = harness("--apply", "--no-paid", "--gateway-url", "https://gw.example",
                          env={"W2_ADMIN": "admin-token"}, expect=1)
    seeds = [l for l in calls.splitlines()
             if l.startswith("30-seed-and-count.sh") and "--check-only" not in l]
    assert not seeds, f"seeded despite {reason}: {seeds}"
    assert "no verified owner identity to seed under" in proc.stdout


def test_seeding_skips_when_step_40_produced_no_artifact(harness) -> None:
    """No artifact at all -- step 40 was skipped for missing tokens."""
    proc, calls = harness("--apply", "--no-paid", "--gateway-url", "https://gw.example",
                          env={"W2_ADMIN": "admin-token"}, expect=1)
    assert not [l for l in calls.splitlines()
                if l.startswith("30-seed-and-count.sh") and "--check-only" not in l]
    assert "wrote no identity_sessions.json" in proc.stdout


def test_the_harness_receives_the_runs_evidence_dir(harness, tmp_path) -> None:
    """Root's finding: the evaluator was invoked without its required --evidence-dir.

    It does not error -- it DEFAULTS to ./test-results/agent-control, so the verdict
    for this run was written outside this run's evidence directory, next to whatever
    a previous invocation had left there. Every other step is passed the evidence
    dir; the one producing the grade was the one that was not.
    """
    (tmp_path / "ev").mkdir(exist_ok=True)
    (tmp_path / "ev" / "fixture-config.json").write_text("{}")
    _, calls = harness("--apply", "--no-paid")
    line = next(l for l in calls.splitlines() if "agent-control-eval" in l)
    assert f"--evidence-dir {tmp_path / 'ev'}" in line, (
        f"the harness must write its verdict into this run's evidence dir: {line!r}")


# ---------------------------------------------------------------------------
# the paid step must be BOUND to the workload it measures
# ---------------------------------------------------------------------------
# Root's requirement 1: "The consumer document must actually reach the bound worker."
# 10- wrote expected-identity.json into the evidence dir and nothing carried it
# forward: the orchestrator invoked step 20 with --evidence-dir alone. Step 20
# requires --expected-identity on its live path, so an --apply run with model access
# could only ever exit 1 -- and its message ("--expected-identity is required") named
# the missing flag rather than the missing binding, sending the reader to add the flag
# by hand from wherever a document happened to be.

def test_the_paid_step_is_given_the_bound_identity_document(harness, tmp_path) -> None:
    """The one axis tying paid evidence to a workload must be threaded, not assumed."""
    _, calls = harness("--apply")
    line = next((l for l in calls.splitlines()
                 if l.startswith("20-collect-pause-evidence.sh")), None)
    assert line is not None, (
        f"step 20 never ran on an --apply run with a bound worker:\n{calls}")
    assert f"--expected-identity {tmp_path / 'ev' / 'expected-identity.json'}" in line, (
        "step 20 must receive the document 10- wrote when it bound the worker pod. "
        "Without it the experiment can describe only itself, and self-description "
        f"establishes nothing about what was measured. Got: {line!r}")
    assert f"--ledger {tmp_path / 'ev' / 'cleanup-ledger.json'}" in line, (
        "the run nonce and the recorded target come from the SHARED ledger, not from a "
        f"value step 20 mints: a minted nonce is unique but names nothing. Got: {line!r}")


def test_an_unbound_fixture_skips_the_paid_step_instead_of_failing_on_a_flag(
        harness) -> None:
    """No bound worker is a reason to not spend, and the reason must be the real one.

    Passing the flag unconditionally would hand step 20 a path that does not exist, so
    it would refuse with "expected-identity document not found" -- true, but it reads
    as a missing file rather than as a fixture that bound no workload.
    """
    proc, calls = harness("--apply", env={"W2_STUB_NO_WORKER": "1"}, expect=1)
    assert "20-collect-pause-evidence.sh" not in calls, (
        "model calls must not be spent when there is no identified workload to "
        "attribute the measurement to")
    assert "no bound worker" in proc.stdout, (
        f"the skip must name the missing BINDING, not a missing flag:\n{proc.stdout}")


def test_an_unbound_fixture_is_a_skip_and_never_a_pass(harness) -> None:
    """And it still counts as a skip, so the run cannot exit green over it.

    A green run whose pause evidence was never collected is precisely the vacuous
    pass this orchestrator exists to remove.
    """
    proc, _ = harness("--apply", env={"W2_STUB_NO_WORKER": "1"}, expect=1)
    summary = [l for l in proc.stdout.splitlines() if "20-pause" in l]
    assert any(l.startswith("SKIP") for l in summary), summary
    assert not any(l.startswith("PASS") for l in summary), summary


# ---------------------------------------------------------------------------
# the staged sequence (root requirement 2: "wire a real staged lifecycle/run-all")
# ---------------------------------------------------------------------------
# The orchestrator's part of the deadlock fix. lib/stage_gate.py decides whether a
# stage may run and tests/test_staged_lifecycle.py drives the two invocations of
# step 10; what is left -- and what these cover -- is that run-all.sh
#
#   * passes the stage down instead of always creating everything,
#   * cannot tear down the gateway stage's fixture (the next stage needs it),
#   * does not run the experiments against a fixture that has no worker yet,
#   * and does not exit green over a half-finished sequence.
#
# The last one is the one that matters: a mid-sequence invocation established nothing
# about W2-03/04/05, and CI reading exit 0 would conclude the evaluation had run.


def _line_for(calls: str, script: str) -> str:
    matches = [l for l in calls.splitlines() if l.startswith(script)]
    assert matches, f"{script} never ran:\n{calls}"
    return matches[-1]


def test_the_gateway_stage_passes_the_stage_down_and_creates_no_worker(harness) -> None:
    """Step 10 is invoked with --stage gateway, and with no worker flags at all.

    Passing --worker-job here would require the endpoint that cannot exist yet, which
    is the deadlock; omitting --stage would create everything, which is the old
    all-or-nothing behaviour this replaces.
    """
    proc, calls = harness("--apply", "--stage", "gateway", expect=1)
    line = _line_for(calls, "10-create-fixture.sh")
    assert "--stage gateway" in line, line
    assert "--worker-job" not in line, line
    assert "--edge-receipt" not in line, line


def test_the_gateway_stage_never_tears_down_the_fixture_the_next_stage_needs(
        harness) -> None:
    """--keep-fixture is FORCED, not requested.

    An operator who forgot it would destroy the Service #5836's edge is built in front
    of, and the sequence could not continue -- so the orchestrator decides, and says
    what that costs.
    """
    proc, calls = harness("--apply", "--stage", "gateway", expect=1)
    assert "90-cleanup-ledger.sh" not in calls, (
        f"the gateway stage tore down the fixture the worker stage needs:\n{calls}")
    assert "STILL RUNNING" in proc.stdout
    # And the cost is stated in the terms that matter, not as a bare flag name.
    assert "live fixture queue" in proc.stdout or "live queue" in proc.stdout


def test_the_gateway_stage_does_not_run_the_experiments_against_a_workerless_fixture(
        harness) -> None:
    """20/40/60 need the protected worker. Running them would produce real errors
    about a fixture that is merely incomplete, and the harness would file those as
    failed checks of the software under review."""
    proc, calls = harness("--apply", "--stage", "gateway", expect=1)
    for script in ("20-collect-pause-evidence.sh", "40-verify-edge-sessions.sh",
                   "agent-control-eval.py"):
        assert script not in calls, f"{script} ran during the gateway stage:\n{calls}"
    assert "Continue with --stage worker" in proc.stdout


def test_the_gateway_stage_exits_non_zero_because_it_established_nothing(
        harness) -> None:
    """The load-bearing one. A mid-sequence invocation is not a passing evaluation.

    Exit 0 here would tell CI and the operator that the Wave 2 checks had run, when in
    fact the worker-dependent ones were never attempted. `expect=1` above is that
    assertion; this states it as its own case so a future change that makes the gateway
    stage exit 0 fails with the reason rather than as a surprise in five other tests.
    """
    proc, _ = harness("--apply", "--stage", "gateway", expect=1)
    assert "NOT a passing evaluation run" in proc.stdout
    totals = next(l for l in proc.stdout.splitlines() if l.startswith("passed "))
    assert "skipped 0" not in totals, totals


def test_the_gateway_stage_points_at_the_handoff_rather_than_prose(harness, tmp_path) -> None:
    """The next commands carry this run's nonce and uids, so they are read from the
    document step 10 wrote rather than retyped from the terminal."""
    proc, _ = harness("--apply", "--stage", "gateway", expect=1)
    assert str(tmp_path / "ev" / "stage-handoff.json") in proc.stdout
    assert "--stage worker" in proc.stdout
    assert "90-cleanup-ledger.sh" in proc.stdout


def test_the_worker_stage_passes_the_receipt_down_and_tears_both_stages_down(
        harness, tmp_path) -> None:
    """The second invocation creates the worker AND owns the teardown of both stages.

    Cleanup is keyed on the shared ledger, which already holds the gateway stage's
    objects -- so the run that finishes the sequence is the run that cleans it up.
    """
    receipt = tmp_path / "edge-ownership.json"
    receipt.write_text("{}")
    proc, calls = harness("--apply", "--no-paid", "--stage", "worker",
                          "--edge-receipt", str(receipt), expect=1)
    line = _line_for(calls, "10-create-fixture.sh")
    assert "--stage worker" in line, line
    assert "--worker-job" in line, line
    assert f"--edge-receipt {receipt}" in line, line
    assert "90-cleanup-ledger.sh" in calls, (
        f"the worker stage must tear down BOTH stages from the shared ledger:\n{calls}")


def test_the_worker_stage_skips_the_dry_run_and_says_why(harness, tmp_path) -> None:
    """--check-only on this stage would have to dry-run against the gateway stage's own
    objects, which establishes nothing the stage gate has not already established
    against the recorded uids. Skipped explicitly rather than silently dropped."""
    receipt = tmp_path / "edge-ownership.json"
    receipt.write_text("{}")
    proc, calls = harness("--apply", "--no-paid", "--stage", "worker",
                          "--edge-receipt", str(receipt), expect=1)
    assert "--check-only" not in calls, calls
    assert "not applicable to the worker stage" in proc.stdout


def test_the_worker_stage_without_a_receipt_is_refused_before_anything_runs(
        harness) -> None:
    """The endpoint is the whole reason this stage is separate.

    Without a receipt it would either be refused deep inside step 10 or -- worse, if
    the flag were ever defaulted -- point a control-enabled worker at production.
    """
    proc, calls = harness("--stage", "worker", expect=2)
    assert "requires --edge-receipt" in proc.stderr
    assert "00-verify-target.sh" not in calls, "nothing may run before this refusal"


def _argv_refusal(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    """Drive the real run-all.sh for refusals that happen at ARGUMENT PARSE time.

    The harness always supplies --run-id and --evidence-dir, so the missing-argument
    cases are unreachable through it. These refusals sit above the preamble -- before
    the account assertion, before mkdir, before any step -- so invoking the real script
    is hermetic: it exits 2 having made no cloud call and created no directory.

    That ordering is itself the thing worth pinning. A refusal placed after the preamble
    would already have created the evidence directory it is complaining about.
    """
    # `aws` and `kubectl` are shimmed to touch a marker and fail. The caller asserts the
    # marker is absent, which is what pins the ORDERING: a refusal that moved below the
    # preamble would have called the account gate first, and these tests would then be
    # passing for a reason unrelated to the argument check.
    shim = tmp_path / "shim-bin"
    shim.mkdir(exist_ok=True)
    for tool in ("aws", "kubectl"):
        path = shim / tool
        path.write_text(f'#!/usr/bin/env bash\ntouch "{tmp_path}/CLOUD_WAS_CALLED"\nexit 1\n')
        path.chmod(0o755)
    return subprocess.run(
        ["bash", str(RUN_ALL), *args],
        capture_output=True, text=True, cwd=str(tmp_path), timeout=60,
        env={**os.environ, "PATH": f"{shim}:{os.environ['PATH']}",
             "HOME": str(tmp_path)},
    )


def _no_cloud_call(tmp_path: Path) -> None:
    assert not (tmp_path / "CLOUD_WAS_CALLED").exists(), (
        "the refusal must happen at argument-parse time, before the account gate: "
        "otherwise it would run after the evidence directory it complains about exists")


def test_the_worker_stage_without_an_evidence_dir_is_refused(tmp_path) -> None:
    """A fresh evidence directory carries no ledger, so there would be no fixture to
    join and no record authorising teardown of what the gateway stage created."""
    receipt = tmp_path / "r.json"
    receipt.write_text("{}")
    proc = _argv_refusal(tmp_path, "--stage", "worker", "--run-id", "w2-x",
                         "--edge-receipt", str(receipt))
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "requires --evidence-dir" in proc.stderr
    _no_cloud_call(tmp_path)
    assert not list(tmp_path.glob("w2-evidence-*")), (
        "nothing may be created on the way to the refusal")


def test_the_worker_stage_without_a_run_id_is_refused_not_given_a_fresh_one(
        tmp_path) -> None:
    """A generated run id would name a fixture that does not exist.

    Silently minting one is the worse failure: the stage gate would then refuse every
    object for "not recorded in this ledger", which reads as a broken gate rather than
    as a missing argument.
    """
    receipt = tmp_path / "r.json"
    receipt.write_text("{}")
    proc = _argv_refusal(tmp_path, "--stage", "worker",
                         "--evidence-dir", str(tmp_path / "ev"),
                         "--edge-receipt", str(receipt))
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "requires --run-id" in proc.stderr
    _no_cloud_call(tmp_path)
    assert not (tmp_path / "ev").exists(), (
        "the refusal must come before the evidence directory is created")


def test_an_unknown_stage_is_refused_rather_than_defaulted(harness) -> None:
    """A typo falling through to "all" would try to create a protected worker in an
    invocation the operator meant to stop at the gateway."""
    proc, calls = harness("--stage", "getway", expect=2)
    assert "is not a stage" in proc.stderr
    assert "00-verify-target.sh" not in calls


def test_the_default_stage_is_still_the_whole_sequence(harness) -> None:
    """Every existing caller means --stage all, so the default must not change.

    Without a receipt it is a gateway-only fixture: the worker-dependent steps skip
    with their real reason rather than being faked.
    """
    proc, calls = harness("--apply", "--no-paid", expect=1)
    line = _line_for(calls, "10-create-fixture.sh")
    assert "--stage all" in line, line
    assert "--worker-job" not in line, line
    assert "90-cleanup-ledger.sh" in calls


def test_reviewed_images_and_expected_alb_are_forwarded(harness, tmp_path):
    receipt = tmp_path / 'edge.json'
    receipt.write_text('{}')
    gateway = '879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-gateway@sha256:' + 'ab' * 32
    worker = '879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-agent-runtime@sha256:' + 'cd' * 32
    alb = 'arn:aws:elasticloadbalancing:us-east-1:879318057152:loadbalancer/app/fixture/123'
    proc, calls = harness('--apply', '--no-paid', '--stage', 'worker',
        '--edge-receipt', str(receipt), '--edge-alb-arn', alb,
        '--gateway-image', gateway, '--worker-image', worker, expect=1)
    line = _line_for(calls, '10-create-fixture.sh')
    assert f'--edge-alb-arn {alb}' in line
    assert f'--gateway-image {gateway}' in line
    assert f'--worker-image {worker}' in line
