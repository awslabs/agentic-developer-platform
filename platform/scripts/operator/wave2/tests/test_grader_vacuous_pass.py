#!/usr/bin/env python3
"""The grader must not pass a check on an UNMEASURED field (issue #3968, blocker 8).

OWNERSHIP: `platform/scripts/agent-control-eval.py` belongs to #5825. #3968 does
not edit it. This file asserts a property #3968 depends on: Wave 2's fixture exists
to produce measurements, and a grader that passes on an absent one makes the whole
exercise unfalsifiable. So these tests stay, pointed at the real module.

HISTORY, because it explains the shape of this file. The defect this file was
written to demonstrate was:

    if neutrality["native_interrupt_status"] == ABORTED_STATUS: raise

an inequality against ONE string, so `None`, `""`, `{}` and `"invented"` all
passed -- and `None` is precisely what the collectors write for an observation that
was never made. #5825 has since landed the fix (`_assert_native_interrupt_outcome`),
and the earlier revision of this file then FAILED on the merged evaluator, because
two of its tests asserted the defective source text verbatim:

    assert 'if neutrality["native_interrupt_status"] == ABORTED_STATUS:' in source
    assert "W2-01" in ev.PENDING_CHECK_OWNERS

Those assertions were characterization of a defect, and they expired the moment it
was fixed -- correctly reporting "the thing I describe is no longer true", but as a
red suite rather than as information.

So this revision asserts the CONTRACT rather than the source text, and it does it
by CALLING the guard instead of grepping for it. A behavioural test survives a
refactor of the guard and still fails if the guard's decision regresses, which is
the property worth pinning; a string match fails on both and cannot tell them
apart. Where a source assertion is genuinely the only available seam it is marked
as such and kept narrow.

Run: python3 -m pytest platform/scripts/operator/wave2/tests/test_grader_vacuous_pass.py -q
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[5]
EVAL_PATH = REPO_ROOT / "platform" / "scripts" / "agent-control-eval.py"

# A wrong path here would make every test below SKIP, and a green run of 11 skips
# reads like a passing report. Fail loudly at import instead: this file's whole
# purpose is to assert things about that module.
if not EVAL_PATH.exists():  # pragma: no cover
    raise AssertionError(
        f"agent-control-eval.py not found at {EVAL_PATH}. Fix the path rather than "
        "letting these tests skip: a silent skip would hide the defect report."
    )


def _load_evaluator():
    """Import the hyphenated script by path (not a package, cannot be imported)."""
    spec = importlib.util.spec_from_file_location("agent_control_eval", EVAL_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["agent_control_eval"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def ev():
    return _load_evaluator()


@pytest.fixture(scope="module")
def guard(ev):
    """The merged guard itself, which is what these tests exercise.

    Reached through the class rather than re-implemented, so a change to its
    decision shows up here as a failure rather than as a test that still agrees
    with a copy of the old logic.
    """
    owners = [obj for obj in vars(ev).values()
              if isinstance(obj, type)
              and "_assert_native_interrupt_outcome" in vars(obj)]
    # Located by search rather than by a hardcoded class name so that moving the
    # guard between classes -- a refactor, not a regression -- does not fail this
    # file. Exactly one owner must exist: two would mean a copy, and a copy is how
    # one call site keeps the fixed guard while another keeps the broken one.
    assert len(owners) == 1, (
        f"expected exactly one class defining _assert_native_interrupt_outcome, found {owners}. "
        "If the guard was removed, the vacuous-pass defect is back; if duplicated, one copy "
        "will be the stale one."
    )
    return owners[0]._assert_native_interrupt_outcome


def _measured(**over) -> dict:
    """A native-interruption record that is valid in every respect.

    Carries the full provenance the merged contract requires, so a test that varies
    one field is varying exactly that field. `observed_by` records HOW the outcome
    was read back; `run_id` names WHICH run was interrupted.
    """
    record = {
        "status": "failed",
        "run_id": "w2-native-interrupt-0001",
        "observed_by": "GET /me/agent-invocations/w2-native-interrupt-0001",
    }
    record.update(over)
    return record


# ---------------------------------------------------------------------------
# an absent measurement is NOT RUN, not a pass
#
# This is the defect that existed, now asserted from the other side. Each of these
# values passed the old inequality; each must now be refused. `None` is the one
# that matters in practice -- it is what a collector writes for an experiment that
# was never performed.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("absent", [None, "", {}, []])
def test_an_unmeasured_native_interrupt_is_not_run_rather_than_a_pass(ev, guard, absent) -> None:
    """Nothing was observed, so there is nothing to judge.

    NOT RUN rather than FAILED is the right answer and the distinction is load
    bearing: a failure would accuse the deployment of a defect on the strength of
    an experiment nobody performed. `report_is_passing` still refuses to call a
    report with a not_run a pass, so this cannot be rounded away either.
    """
    with pytest.raises(ev.PrerequisiteMissingError) as exc:
        guard(absent)
    assert "NOT RUN rather than a pass" in str(exc.value)


def test_a_missing_status_key_is_also_unmeasured(ev, guard) -> None:
    """An object with provenance but no outcome measured nothing either."""
    with pytest.raises((ev.PrerequisiteMissingError, AssertionError)):
        guard({"run_id": "w2-native-interrupt-0001", "observed_by": "api"})


# ---------------------------------------------------------------------------
# the value must be one the deployment could actually have written
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bogus", ["unknown", "aborted ", "ABORTED", "not_measured", "invented"])
def test_a_status_outside_the_writers_vocabulary_is_refused(guard, bogus) -> None:
    """Every one of these passed the old `!= "aborted"` guard.

    `"aborted "` and `"ABORTED"` are the sharpest cases: they are what the OLD
    guard was aimed at and still let through, so a fix that only tightened the
    comparison to the exact string would leave them passing. A value the writer
    could not have produced is a typo or a fabrication, not an outcome.
    """
    with pytest.raises(AssertionError) as exc:
        guard(_measured(status=bogus))
    assert "not one of the writer's" in str(exc.value)


def test_the_exact_aborted_status_is_still_the_defect_it_always_was(ev, guard) -> None:
    """The original check's one true positive, preserved.

    A provider's interrupted turn recorded as an ADP abort is the specific error
    W2-06 exists to catch; the message must keep saying so rather than folding into
    the generic vocabulary complaint, because the operator's next action differs.
    """
    with pytest.raises(AssertionError) as exc:
        guard(_measured(status=ev.ABORTED_STATUS))
    assert "pattern-matching a provider's interrupt string" in str(exc.value)


@pytest.mark.parametrize("status", ["failed", "complete", "completed", "skipped", "budget_stopped"])
def test_a_measured_non_aborted_outcome_passes(ev, guard, status) -> None:
    """The guard is not merely strict -- a real observation must still pass.

    Without this, tightening the guard to refuse everything would look like a fix.
    Parametrized over the whole allowlist so an entry silently dropped from
    NATIVE_INTERRUPT_ALLOWED_STATUSES fails here.
    """
    assert status in ev.NATIVE_INTERRUPT_ALLOWED_STATUSES
    guard(_measured(status=status))  # must not raise


@pytest.mark.parametrize("running", ["active", "in_progress"])
def test_a_still_running_subject_is_not_an_interruption_outcome(ev, guard, running) -> None:
    """The experiment interrupts a turn, so its subject has stopped.

    A still-active row means the experiment did not reach the state it claims to
    describe. Pinned because `active` is a legitimate status elsewhere in the
    writer's vocabulary, so its absence from this allowlist is a deliberate choice
    that reads like an oversight.
    """
    assert running not in ev.NATIVE_INTERRUPT_ALLOWED_STATUSES
    with pytest.raises(AssertionError):
        guard(_measured(status=running))


# ---------------------------------------------------------------------------
# an outcome with no provenance is an expectation, not an observation
# ---------------------------------------------------------------------------
def test_a_bare_status_string_is_refused_for_want_of_provenance(ev, guard) -> None:
    """A bare `"failed"` cannot say which run was interrupted or how it was read.

    This is the shape the fixture actually carried while the check was passing on
    unmeasured fields, which is why it is refused explicitly rather than by
    accident.
    """
    with pytest.raises(AssertionError) as exc:
        guard("failed")
    assert "must be an object" in str(exc.value)


@pytest.mark.parametrize("key", ["run_id", "observed_by"])
def test_each_provenance_key_is_individually_required(ev, guard, key) -> None:
    """Dropped one at a time, so neither is satisfied by the other's presence."""
    assert key in ev.NATIVE_INTERRUPT_KEYS
    record = _measured()
    del record[key]
    with pytest.raises(AssertionError) as exc:
        guard(record)
    assert key in str(exc.value)


@pytest.mark.parametrize("empty", ["", None])
def test_a_present_but_empty_provenance_key_does_not_satisfy_the_check(guard, empty) -> None:
    """Presence is not measurement -- the same error one level down.

    A collector that writes `run_id: null` satisfies any `in` check while recording
    nothing, which is the precise mechanism of the original defect.
    """
    with pytest.raises(AssertionError):
        guard(_measured(run_id=empty))


# ---------------------------------------------------------------------------
# presence-checking cannot close the gap, which is why the predicate must
# ---------------------------------------------------------------------------
def test_required_keys_check_presence_not_measurement(ev) -> None:
    """`null` satisfies the declared-keys list, because the key IS present.

    Kept from the original file unchanged: it is the reason the guard above has to
    carry the weight, and it is as true of the fixed evaluator as of the broken
    one.
    """
    required = ev.REQUIRED_ARTIFACT_KEYS["harness_neutrality"]
    assert "native_interrupt_status" in required
    artifact = {
        "adapter_a": {"completed": 1, "failed": 0, "active": 0, "aborted": 0},
        "adapter_b": {"completed": 1, "failed": 0, "active": 0, "aborted": 0},
        "native_interrupt_status": None,
        "shared_code_imports_sdk": False,
    }
    assert all(key in artifact for key in required), \
        "a null-valued field satisfies a presence check"


# ---------------------------------------------------------------------------
# the W2-05 pattern that is CORRECT, recorded so it is not "simplified" later
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value", [None, "", 0, "true", [], {}])
def test_is_not_true_correctly_rejects_unmeasured_values(value) -> None:
    """W2-05 uses `is not True`, which a null correctly FAILS.

    Anyone tempted to relax these to truthiness checks (`if not expiry.get(...)`)
    would reintroduce the defect class -- `"false"` would pass.
    """
    assert (value is not True), "only the actual boolean True may satisfy these guards"


def test_w2_05_uses_identity_comparisons_for_its_booleans() -> None:
    """A source assertion on purpose, and the narrowest one available.

    `check_w2_05` needs a live expiry artifact to invoke, so there is no behavioural
    seam for this one property short of constructing the whole fixture. Kept
    because the failure it guards -- `is not True` relaxed to a truthiness check --
    is invisible in behaviour until a legitimately measured `False` arrives.
    """
    source = EVAL_PATH.read_text()
    assert 'if expiry.get("auto_resumed") is not True:' in source, \
        "W2-05's boolean guards changed shape; confirm nulls still fail"


# ---------------------------------------------------------------------------
# every wave-2 check is now implemented, so a NOT RUN names a missing INPUT
# ---------------------------------------------------------------------------
def test_every_wave2_check_in_the_manifest_has_a_predicate(ev) -> None:
    """The replacement for "W2-01 and W2-10 are owned but unimplemented".

    That test recorded the state #5825 has since closed: the two checks now have
    predicates and PENDING_CHECK_OWNERS is empty. Asserting the CURRENT invariant
    is what makes this durable -- a check added to the manifest without a predicate
    fails here, and it is worth failing on, because the driver grades an unowned
    not_run as a harness bug.

    For #3968 the consequence is direct: a NOT RUN from this evaluator now means an
    input the fixture did not supply, so it is a statement about the fixture rather
    than about the grader.
    """
    for check_id in ev.WAVE2_PREDICATES:
        assert check_id in ev.CHECK_PREDICATES, \
            f"{check_id} is in the wave-2 manifest with no predicate"
    pending_wave2 = {
        check: owner for check, owner in ev.PENDING_CHECK_OWNERS.items()
        if check in ev.WAVE2_PREDICATES
    }
    assert pending_wave2 == {}, (
        "a wave-2 check is pending again. That is legitimate mid-wave, but it means a complete "
        "wave-2 report is unreachable until it lands, so it must be stated rather than "
        "discovered: record the owner and say so in GRADER-VACUOUS-PASS.md"
    )


def test_an_unowned_unimplemented_check_is_a_failure_not_a_quiet_not_run(ev) -> None:
    """The rule that makes the assertion above safe to rely on.

    If PENDING_CHECK_OWNERS is empty and a manifest entry lost its predicate, the
    driver must not record a bland not_run -- that is how a check stops being
    anyone's job. Source-asserted narrowly because reaching this branch
    behaviourally means driving the whole check driver.
    """
    source = EVAL_PATH.read_text()
    assert "no predicate and no owning" in source, \
        "the unowned-not_run failure path changed; confirm it still FAILS rather than not_runs"


def test_report_is_passing_requires_zero_not_run(ev) -> None:
    """So an unimplemented check, or a missing input, cannot be rounded into a pass."""
    source = EVAL_PATH.read_text()
    assert "def report_is_passing" in source
    start = source.index("def report_is_passing")
    body = source[start:start + 1200]
    assert "not_run" in body, \
        "report_is_passing no longer considers not_run; a pending check could grade as a pass"
