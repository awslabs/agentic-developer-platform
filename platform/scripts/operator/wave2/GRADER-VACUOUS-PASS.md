# The vacuous-pass defect: reported here, fixed by #5825, now pinned behaviourally

Issue #3968, Wave 2. **Owner: #5825**, which holds `platform/scripts/agent-control-eval.py`.
#3968 has never edited that file.

**Status: RESOLVED in the evaluator.** Defect 1 below was real, was reported from
here, and #5825 has landed the fix (`_assert_native_interrupt_outcome`). Defect 2
was a place-to-look rather than a demonstrated defect. This document is kept as the
record of what the defect was, what replaced it, and — the part that matters going
forward — why the tests guarding it are shaped the way they now are.

The defect's shape is the same one this PR fixes elsewhere in its own files: **an
absent observation treated as a satisfied one.** The collectors were never at fault
— `21-assemble-pause-artifacts.py` writes explicit `null` for anything an
experiment did not measure. The grader read those nulls as passes.

## Defect 1 (was) — `native_interrupt_status: null` passed W2-06 vacuously

The guard was:

```python
if neutrality["native_interrupt_status"] == ABORTED_STATUS:
    raise AssertionError(...)
```

An inequality against one string, so **every** other value passed: `None`, `""`,
`{}`, `"unknown"`, and — sharpest — `"aborted "` and `"ABORTED"`, which are the
very case the guard was aimed at. The declared-keys list did not help: it enforces
*presence*, and `null` is present.

The intended property was stated in the raise message: *"Only a confirmed abort
finalization may carry this status."* That is a claim about what the status IS, and
it cannot be established by ruling out one string. A run whose native-interrupt
observation was never made graded identically to one made and clean.

### What #5825 landed

`_assert_native_interrupt_outcome` now separates three outcomes that the old guard
collapsed into one:

| Recorded | Result | Why |
|---|---|---|
| `None`, `""`, `{}`, `[]`, or no `status` | `PrerequisiteMissingError` → **not_run** | Nothing was measured, so there is nothing to judge. Not a *failure*: a failure would accuse the deployment of a defect on the strength of an experiment nobody performed. |
| present, not in the writer's vocabulary | **failed** | A value the deployment could not have written is a typo or a fabrication. |
| exactly `"aborted"` | **failed** | Unchanged — that IS the defect W2-06 exists to catch, and the message still says so specifically. |
| in `NATIVE_INTERRUPT_ALLOWED_STATUSES`, with provenance | **passed** | An observation. |

Two details worth not losing:

* **Provenance is required**, not just an outcome. `NATIVE_INTERRUPT_KEYS` is
  `("status", "run_id", "observed_by")`: a bare `"failed"` cannot say *which* run
  was interrupted or *how* its outcome was read back, and an outcome with no
  provenance is indistinguishable from an expectation somebody typed. Each key must
  be non-empty — `run_id: null` satisfies an `in` check while recording nothing,
  which is the original defect one level down.
* **`active`/`in_progress` are deliberately absent** from the allowlist. The
  experiment interrupts a turn, so its subject has stopped; a still-running row
  means the experiment never reached the state it claims to describe. This reads
  like an oversight and is not one, so a test pins it.

The general form, which was the recommendation and is what landed: **enumerate what
is acceptable, do not enumerate what is not.** A denylist of one value passes
everything nobody thought of.

## Defect 2 — `is not True` is the correct pattern, kept stated

W2-05 uses `is not True` / `is not False`, which a `null` correctly fails. That is
the right pattern and is stated here so it is not "simplified" later:

```python
if expiry.get("auto_resumed") is not True:   # null FAILS. correct.
```

Relaxing these to truthiness checks (`if not expiry.get(...)`) would reintroduce
the defect class: a legitimately measured `False` would fail, and the string
`"false"` would pass.

The numeric clamp block reading `granted_ms`, `remaining_ms` and
`finalization_margin_ms` was named as a place to look, not a demonstrated defect. I
did not trace it then and have not since; it is still the place to look.

## The "8/10 ceiling", and why that paragraph is now obsolete

This document used to argue that W2-01 and W2-10 sat in `PENDING_CHECK_OWNERS`, so
`report_is_passing` — which requires `failed`, `skipped` and `not_run` all zero
plus `cleanup_ok` — made the suite **unpassable by construction**, and that "8/10"
was the arithmetic of 10 minus 2 rather than a score.

#5825 closed that too. Every ID in the wave-2 manifest now has a predicate and
`PENDING_CHECK_OWNERS == {}`. The consequence is the useful part: **a NOT RUN from
this evaluator now names a missing *input*, not a missing implementation.** For
#3968 that is a direct statement about the fixture — if a check does not run, this
fixture did not supply what it needed, and that is our problem to fix rather than a
grader limitation to note.

`report_is_passing` still requires zero `not_run`, so a missing input cannot be
rounded into a pass either.

## How the tests are shaped, and why that changed

`tests/test_grader_vacuous_pass.py` was written to demonstrate the defect against
the real module, and it said so: *"WHEN #5825 LANDS THE FIX, THIS WILL FAIL. That is
intended."*

It did fail — but for a worse reason than intended. Two of its assertions matched
the **defective source text verbatim**:

```python
assert 'if neutrality["native_interrupt_status"] == ABORTED_STATUS:' in source
assert "W2-01" in ev.PENDING_CHECK_OWNERS
```

Those are characterization of a defect, and they expired the moment it was fixed.
On the merge checkout they were 2 of 507 tests red, reporting "the thing I describe
is no longer true" as a broken build rather than as information — and a red suite
whose redness is expected is a suite nobody reads.

The rewrite asserts **the contract, by calling the guard**, rather than the source
text:

* A behavioural test survives a refactor of the guard and still fails if the
  guard's *decision* regresses. A string match fails on both and cannot tell them
  apart — which is exactly what happened.
* Every value that passed the old guard (`None`, `""`, `{}`, `[]`, `"aborted "`,
  `"ABORTED"`, `"unknown"`) is now asserted to be *refused*, so the defect cannot
  return quietly.
* A measured non-aborted outcome is asserted to *pass*, parametrized over the whole
  allowlist. Without this, a guard tightened to refuse everything would look like a
  fix.
* The guard is located by searching for the class that defines it, not by a
  hardcoded class name, and exactly one owner must exist. Moving it between classes
  is a refactor; two copies would mean one call site keeps the fixed version and
  another keeps the broken one.

Two source assertions are kept deliberately, each marked and narrow: W2-05's
`is not True` (no behavioural seam without constructing a live expiry artifact) and
the unowned-`not_run`-is-a-failure branch (reaching it behaviourally means driving
the whole check driver). Both guard failures that are invisible in behaviour until
a specific rare input arrives.

**Verified by mutation, not by inspection.** Restoring the pre-#5825 guard makes 18
of these tests fail, the `None` case with `DID NOT RAISE PrerequisiteMissingError`
— the vacuous pass itself. The evaluator was restored byte-identical afterwards and
`git diff origin/main -- platform/scripts/agent-control-eval.py` is empty.

## What #3968 does about it

Still nothing in `agent-control-eval.py` — that is #5825's file, and the original
reason holds: a three-line "fix in passing" would have put two changes in conflict
over one function during an active repair, and a merge resolution is exactly where
a security check quietly reverts to the weaker version. The reported-and-fixed
route worked.

What this branch does:

1. The collectors keep writing explicit `null` for unmeasured fields, and
   `20-collect-pause-evidence.sh` says so out loud.
2. `tests/test_grader_vacuous_pass.py` now pins the *fixed* contract behaviourally,
   including every value the old guard let through.
3. This document records what the defect was and what replaced it, so the fix is
   not re-litigated from the old prose.
