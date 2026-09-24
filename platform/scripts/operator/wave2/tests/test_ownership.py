#!/usr/bin/env python3
"""Behavioural tests for the Wave 2 ownership ledger (issue #3968).

These are the failure paths root's review named. Each test encodes a way the
PUBLISHED scripts would have reported success while doing the wrong thing, so a
regression here is a regression toward deleting somebody else's resource.

Run: python3 -m pytest platform/scripts/operator/wave2/tests/ -q
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))

import ownership  # noqa: E402

ACCOUNT = "879318057152"
RUN = "w2-test-run"


@pytest.fixture()
def ledger(tmp_path: Path) -> Path:
    path = tmp_path / "ledger.json"
    ownership.init_ledger(path, RUN, ACCOUNT, "us-east-1", "nonce0123")
    return path


# ---------------------------------------------------------------------------
# ownership evidence
# ---------------------------------------------------------------------------
def test_k8s_without_uid_is_refused(ledger: Path) -> None:
    """A ledger entry with no server-assigned uid cannot prove ownership.

    The published version recorded `run_bound: true` before creation with no uid,
    so teardown had nothing to verify against.
    """
    with pytest.raises(ValueError, match="no metadata.uid"):
        ownership.record_k8s(
            ledger, run_id=RUN, account_id=ACCOUNT, kind="Deployment",
            name="d", namespace="ns", uid="", created_by_this_run=True,
        )


def test_adopted_resource_cannot_be_recorded_as_deletable(ledger: Path) -> None:
    """`kubectl apply` adopts a pre-existing object; adoption must not authorise deletion."""
    with pytest.raises(ValueError, match="not created by this run"):
        ownership.record_k8s(
            ledger, run_id=RUN, account_id=ACCOUNT, kind="Deployment",
            name="pre-existing", namespace="ns", uid="uid-x", created_by_this_run=False,
        )


def test_same_name_different_uid_is_refused(ledger: Path) -> None:
    """A same-name replacement is a DIFFERENT object and must not be silently merged."""
    ownership.record_k8s(
        ledger, run_id=RUN, account_id=ACCOUNT, kind="Deployment",
        name="d", namespace="ns", uid="uid-a", created_by_this_run=True,
    )
    with pytest.raises(ValueError, match="already recorded with uid"):
        ownership.record_k8s(
            ledger, run_id=RUN, account_id=ACCOUNT, kind="Deployment",
            name="d", namespace="ns", uid="uid-b", created_by_this_run=True,
        )


def test_recording_is_idempotent_for_the_same_object(ledger: Path) -> None:
    """Re-recording the identical object must not duplicate the entry.

    A resumed run re-records what it already created; duplicates would make the
    teardown report double-count and obscure whether anything actually remained.
    """
    for _ in range(3):
        ownership.record_k8s(
            ledger, run_id=RUN, account_id=ACCOUNT, kind="Service",
            name="svc", namespace="ns", uid="uid-s", created_by_this_run=True,
        )
    assert len(json.loads(ledger.read_text())["k8s"]) == 1


def test_existing_queue_cannot_be_recorded_as_deletable(ledger: Path) -> None:
    """SQS CreateQueue returns the EXISTING queue on a name match.

    Recording that as run-bound is how the protected probe queue could have been
    deleted.
    """
    with pytest.raises(ValueError, match="EXISTING queue"):
        ownership.record_queue(
            ledger, run_id=RUN, account_id=ACCOUNT, name="q",
            url="u", nonce="n", created_by_this_run=False,
        )


def test_queue_requires_owner_nonce(ledger: Path) -> None:
    """SQS has no uid, so the run nonce tag is the only ownership evidence."""
    with pytest.raises(ValueError, match="no run nonce"):
        ownership.record_queue(
            ledger, run_id=RUN, account_id=ACCOUNT, name="q",
            url="u", nonce="", created_by_this_run=True,
        )


# ---------------------------------------------------------------------------
# foreign / malformed ledgers
# ---------------------------------------------------------------------------
def test_foreign_run_ledger_is_refused(ledger: Path) -> None:
    with pytest.raises(ValueError, match="belongs to run"):
        ownership.load_ledger(ledger, run_id="w2-someone-else", account_id=ACCOUNT)


def test_foreign_account_ledger_is_refused(ledger: Path) -> None:
    """The #5195 account confusion, applied to the ledger.

    The same resource name in two accounts is two different resources.
    """
    with pytest.raises(ValueError, match="account"):
        ownership.load_ledger(ledger, run_id=RUN, account_id="605440105851")


def test_v1_ledger_is_refused(tmp_path: Path) -> None:
    """A v1 ledger records `run_bound: true` and no uid; it cannot drive a teardown."""
    path = tmp_path / "v1.json"
    path.write_text(json.dumps({
        "run_id": RUN, "account_id": ACCOUNT, "synthetic_rows": [],
        "k8s": [{"kind": "Deployment", "name": "x", "namespace": "ns",
                 "delete": True, "run_bound": True}],
        "queues": [],
    }))
    with pytest.raises(ValueError, match="version"):
        ownership.load_ledger(path, run_id=RUN, account_id=ACCOUNT)


def test_malformed_ledger_reports_precisely(tmp_path: Path) -> None:
    """A truncated ledger must say so, not be treated as empty.

    Treating unparseable as empty would report "nothing to clean up" for a run
    that created resources.
    """
    path = tmp_path / "bad.json"
    path.write_text('{"ledger_version": 2, "run_id": ')
    with pytest.raises(ValueError, match="not valid JSON"):
        ownership.load_ledger(path)


def test_ledger_write_is_atomic(ledger: Path) -> None:
    """After any successful write the ledger must parse.

    The published version rewrote in place, so an interruption mid-write left the
    one file that records what to delete unparseable.
    """
    for index in range(20):
        ownership.record_row(
            ledger, run_id=RUN, account_id=ACCOUNT,
            event_id=f"evt-{index}", arrived_at=f"2026-09-24T00:00:{index:02d}Z",
        )
        json.loads(ledger.read_text())  # must parse after every write
    leftovers = list(ledger.parent.glob(".ledger-*.tmp"))
    assert not leftovers, f"temp files left behind: {leftovers}"


# ---------------------------------------------------------------------------
# rows
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("event_id", "arrived_at"),
    [("e1", ""), ("", "2026-09-24T00:00:00Z"), ("", "")],
)
def test_partial_row_key_is_refused(ledger: Path, event_id: str, arrived_at: str) -> None:
    """Both key halves or nothing: a guessed range key can match an unrelated item."""
    with pytest.raises(ValueError, match="both"):
        ownership.record_row(
            ledger, run_id=RUN, account_id=ACCOUNT,
            event_id=event_id, arrived_at=arrived_at,
        )


# ---------------------------------------------------------------------------
# resume semantics
# ---------------------------------------------------------------------------
def test_resume_with_a_different_nonce_is_refused(ledger: Path) -> None:
    """A new nonce means a new run, and a new run must not inherit an old ledger."""
    with pytest.raises(ValueError, match="run_nonce"):
        ownership.init_ledger(ledger, RUN, ACCOUNT, "us-east-1", "a-different-nonce")


def test_resume_with_the_same_nonce_succeeds(ledger: Path) -> None:
    led = ownership.init_ledger(ledger, RUN, ACCOUNT, "us-east-1", "nonce0123")
    assert led["run_nonce"] == "nonce0123"


def test_nonce_is_not_clock_derived() -> None:
    """Two runs started in the same second must not collide."""
    assert len({ownership.new_nonce() for _ in range(200)}) == 200


# ---------------------------------------------------------------------------
# CLI exit codes — the shell steps branch on these
# ---------------------------------------------------------------------------
def _cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(LIB / "ownership.py"), *args],
        capture_output=True, text=True,
    )


def test_cli_exits_nonzero_on_refusal(ledger: Path) -> None:
    """The shell must be able to branch on failure.

    Verified as an exit code rather than by reading stderr: a guard that explains
    itself but exits 0 lets the caller proceed.
    """
    result = _cli(
        "record-row", "--ledger", str(ledger), "--run-id", RUN,
        "--account-id", ACCOUNT, "--event-id", "e1", "--arrived-at", "",
    )
    assert result.returncode != 0
    assert "both key halves" in result.stderr


def test_cli_exits_zero_on_success(ledger: Path) -> None:
    result = _cli(
        "record-row", "--ledger", str(ledger), "--run-id", RUN,
        "--account-id", ACCOUNT, "--event-id", "e1", "--arrived-at", "2026-09-24T00:00:00Z",
    )
    assert result.returncode == 0, result.stderr


def test_cli_foreign_ledger_exits_nonzero(ledger: Path) -> None:
    result = _cli("validate", "--ledger", str(ledger), "--run-id", "w2-other",
                  "--account-id", ACCOUNT)
    assert result.returncode != 0
    assert "belongs to run" in result.stderr
