# Issue #4211 — Stall/halt detection, bounded defect cycles, and a notification path that delivers

Wave 4 of EPIC #4191 / intent #4120. Added `src/orchestration/stall.py` and
`src/orchestration/notify.py` to the gateway, plus an SNS topic in the
`orchestration-tick` Terraform module.

## Three claims in the issue body were wrong — verify before coding

The issue is written with confident file-level detail, and three of its
specifics did not survive contact with the repo:

1. It said the node table ships in `026_*`. It ships in
   `029_orchestration_graph.py`; `026` is `channel_tenant_map_installation_id`.
   Alembic head is `031_usage_graph_address`.
2. It implied a migration was needed for a `started_at` column.
   `orchestration_nodes` already has `attempts`, `created_at` and `updated_at`.
3. It suggested reusing an existing alarm path for notification. Both existing
   metric→alarm→SNS precedents have their address variables unset in *every*
   tfvars file, so reusing one would have delivered exactly the log line the
   issue explicitly rules out.

Lesson: an issue body is a hypothesis about the codebase, not a description of
it. Check the shipped schema and the actual tfvars values before accepting a
design premise. All three corrections went into the plan comment *before*
implementation, which is what made them cheap.

## `updated_at` was already the timestamp I needed

The instinct was to add `started_at` to track how long a node has been
`running`. But `apply_guarded_transition` writes `updated_at` on every state
change, so for a node currently in `running`, `updated_at` **is** the moment it
entered `running`. Adding a column would have created a second source of truth
that could disagree with the first — and a migration, and backfill questions
for existing rows.

Lesson: before adding a column for "when did X start", check whether an
existing audit timestamp already means that *given the state you're filtering
on*. State-scoped queries often make a general column precise.

## Notify-once as structure, not as a flag

The obvious dedupe designs are a `notified` boolean or a dedupe table — both of
which need their own write, which can fail independently of the state change,
which reintroduces the double-notify you were preventing.

Instead: the guarded UPDATE is `WHERE id = :id AND state = :observed`. The
loser of a race matches **0 rows**. So notifying only when `rows == 1` makes
"exactly one notification per transition" a consequence of the existing
optimistic-concurrency write, with no new state and no new failure mode.

Lesson: when you need at-most-once, look for a write in the system that is
already exactly-once and hang the side effect off its success signal.

## Order of evaluation was a correctness decision, not style

Halt is checked **before** stall. A defect that has exhausted its cycle bound
is also, usually, a node that has been running a long time. If stall wins, the
node goes to `failed`, a human resumes it, and it re-enters the very cycle the
bound existed to stop. Checking halt first makes the terminal outcome win.

Lesson: when two detectors can match the same row, the tie-break is part of the
spec. Write the test that puts a row in both categories at once.

## Restructure your own code before touching a pinned regression test

I widened `_run()` to return `tuple[TickReport, StallReport]` and
`_emit_metrics` to two args. This broke
`test_token_is_emitted_at_info_under_a_preconfigured_root_logger`, which
monkeypatches `_run` with a minimal stub and `_emit_metrics` with
`lambda _r: None`:

```
TypeError: cannot unpack non-iterable _TickReportStub object
```

Fixing the test would have been two lines. But that test pins the greppable
`tick_report` token surviving `awslambdaric`'s logging setup — a bug that had
already shipped once — and the issue's regression contract required existing
tick tests green *unchanged*. So I changed my code instead: kept `_run()`
returning one `TickReport` with the stall report attached via `setattr`, kept
`_emit_metrics(report)` single-arg, and made `handler` tolerate a missing stall
report. Result: 441 passed with `tick.py`, `state.py` and `test_tick.py`
byte-for-byte unchanged (verified with `git diff --stat`).

Lesson: a test that breaks when you widen a signature is often telling you the
signature is load-bearing. "The test is easy to update" and "the test should be
updated" are different claims. Wiring detection into `tick_handler.py` rather
than `tick.py` fell out of the same reasoning.

## Two tests that passed for the wrong reason

`test_an_undeclared_state_is_not_flagged` never reached the `except ValueError`
branch it was written to cover — the SQL `state.in_(watched)` filter excluded
the row before `_is_candidate_state` ever saw it. Green, and proving nothing.
Replaced with two-layer defence-in-depth tests plus four direct unit tests of
the guard. Also added coverage for the `(0, False)` authority-rejection and
`(0, True)` lost-race branches. Coverage 93% → 99%.

Lesson: coverage percentage is what found this — the branch I believed was
tested showed as unhit. When a test targets a defensive branch, assert the
branch actually executed, or test the function directly rather than through the
layer that filters its input.

## I caught myself writing a hack

I imported `or_`, stopped needing it, and appended `del or_` at the end of the
module to silence the unused-import error. That is lint-suppression disguised
as code. Removed the import properly.

Lesson: if the fix makes the linter quiet without making the code better, it's
the wrong fix.

## Mirrored constants are drift risk — pin them from both sides

`AGENT_POD_DEADLINE_SECONDS` mirrors Terraform's `agent_pod_deadline_seconds`
and `MAX_CHAIN_DEPTH` mirrors `spawn_persona.py`. Nothing enforces either at
runtime. Both are documented as drift risks *and* have tests asserting the
derived invariants (threshold strictly below the deadline, bound strictly below
the chain depth) so a future change to one side fails loudly rather than
silently producing a threshold above the deadline.

## Environment notes

- No `requirements*.txt` in `modules/gateway`; dev deps are in `pyproject.toml`
  under `[project.optional-dependencies] dev`. Set up with
  `python3 -m venv .venv-4211 && .venv-4211/bin/pip install -e ".[dev]"`.
  The venv is **not** gitignored — stage files explicitly, never `git add -A`.
- `pytest --cov` needs **dotted module paths** (`--cov=src.orchestration.stall`),
  not file paths, or it reports "No data was collected" and looks like a
  tooling failure rather than a typo.

## Pre-existing failure found in the full suite (not mine)

`tests/budget/test_budget_overshoot.py::TestReservationLifecycle::test_reservation_is_released_when_the_request_raises`
fails on `main` as of 2026-08-28. Verified pre-existing by running it in a
clean worktree at `HEAD`. Cause: the test hard-codes
`period_start="2026-08-27"` while reconciliation computes the current day's
period, so the release writes to a different key than the reservation. A
date-rollover time bomb that armed itself today, unrelated to #4211 — worth its
own issue.

Lesson: when the full suite shows a failure outside your diff, prove it with a
clean-worktree run before dismissing it. That takes a minute and converts "I
think it's unrelated" into a fact you can put in the PR.
