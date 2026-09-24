# Pause-expiry evidence: the `pause_expiry` artifact contract

Producer for Wave2 check **W2-05**. This is the field-by-field contract between
the producer in this repo and the evaluation that consumes it (#3968), so the
consumer never has to guess what a value means or where it came from.

| | |
|---|---|
| Artifact path | `artifacts/pause_expiry.json` (per `docs/runbooks/agent-control-evaluation.md`) |
| Producer | `modules/agent-factory/agent/src/control-runtime-timeout.ts` (`buildPauseExpiryArtifact`) |
| Measurement | `modules/agent-factory/agent/src/control-runtime-timeout.integration.ts` |
| Shared production emitter | `modules/agent-factory/agent/src/run-heartbeat.ts` (also started by `agent-worker.ts`) |
| Consumer | `Driver.check_w2_05` in `platform/scripts/agent-control-eval.py` |
| Automated checks | `control-runtime-timeout.test.ts`, `run-heartbeat.test.ts` and `control-runtime-timeout.assembly.test.ts`, all pinned by name in `.github/workflows/agent-control-ci.yml` |

## The rule that governs every field

**A measurement that could not be taken is `null`, never a passing default.**

`check_w2_05` requires a specific `true` / `false` / number for each field, so a
`null` fails the check. That is the intended outcome, not a bug to route around:
"we could not observe it" and "it works" must not produce the same artifact.

Three corollaries, all enforced by tests:

- No field is a hard-coded success boolean. Every one traces to an observation.
- Absent observations do not become `0` or `false` where those would read as a
  pass. An empty held-work list is `null`, not "nothing was wrongly admitted".
- Values observed as broken pass through **unrepaired**. A held-hook block whose
  state lapsed to `paused`, or whose reason is empty, is reported that way and
  fails the check. Repairing it would hide the defect the check exists to find.

## Why the split into two files

Taking the measurements needs a real model, a real CLI subprocess and real spend,
so it cannot run in ordinary CI. Deciding *what an observation proves* is ordinary
logic — and it is where the dangerous mistakes live. So all the judgement lives in
`control-runtime-timeout.ts`, which jest collects and `tsc` type-checks with no SDK
in its import graph; the `.integration.ts` only measures. Note the naming: jest's
`testMatch` is `**/*.test.ts`, so **ordinary CI cannot invoke the live SDK**, while
`tsconfig`'s `include: ["src/**/*"]` still type-checks the integration file.

## Shared execution binding

Three of W2-05's fields are about what an operator can *see* during a pause: the
heartbeat count, whether the record says "paused" rather than falling silent, and
whether the post-completion exit watchdog killed the run. These were once listed as
launcher inputs, and that was wrong in a way worth spelling out, because the
mistake is easy to repeat.

The heartbeat and the watchdog used to live inline in `agent-worker.ts`'s run loop,
reading the worker's own gate. The live runner builds its own `PauseGate` and its
own `resilientQuery` and never starts `runAgent`, so the parent worker's heartbeat
records — even from the same pod, even in the same seconds — describe a **different
execution with a different gate**. A parent worker can be perfectly healthy and
unpaused while the experiment's gate is paused. Reading its records as evidence
about the experiment's pause is a category error, and *raising the pause budget
cannot fix it*: a longer pause on gate A produces more records about gate B.

The fix is sharing the code, not the pod. `modules/agent-factory/agent/src/run-heartbeat.ts`
is the single production copy of both behaviours. `agent-worker.ts` starts it with
its own run state; the live runner starts the same module against the gate it is
about to pause. So the records in the artifact came from the production emitter,
ticking against the gate whose pause is being measured.

Two consequences for anyone reading or extending this:

- **Do not substitute a fixture emitter.** A probe-local reimplementation would
  report the probe's behaviour as production's, which is exactly the class of
  evidence this issue exists to remove. `run-heartbeat.test.ts` pins the wording,
  `phase` values, fields and thresholds literally so a refactor of the shared module
  cannot quietly change what an operator sees.
- **The pod-level fields are still the launcher's.** Whether the pod was killed, its
  UID and its container state are observations no process can make about itself, and
  nothing in the runner fakes survival or parent-execution evidence. `pod_killed` is
  and remains a launcher input.
- **The heartbeat records are the run's own, and only the run's.** They are the one
  category the launcher may *not* supply, and the assembly has no parameter to submit
  them through: `assemblePauseExpiryArtifact`'s `pod` argument is typed
  `LauncherPodObservations`, which has no `heartbeats` field at all. That is deliberate.
  A heartbeat line in a pod log proves only that something in that pod emitted it, and
  the pod also runs the ordinary worker, whose records are identical in shape and
  describe a different gate that was never paused. If the records are missing, re-run
  the experiment with a budget long enough to produce them — do not scrape the log.

The views are combined field by field, in the producer, at **two** levels — and the
second one is easy to miss. `mergeExperimentPodObservations` first reconciles the
experiments with each other, because more than one of them contributes to `pod`: the
natural-expiry run measures the heartbeats and the watchdog run measures the exit
verdict. Then `mergePodObservations` reconciles that result with the launcher. A
group-level `{...a, ...b}` at either level silently drops whichever side was folded
in second — a launcher carrying only `podKilled`, or simply a later experiment,
erasing heartbeats the run actually measured. Two rules govern conflicts:

- **The boolean fields resolve toward the failure.** A `true` from either source
  wins. These are failure reports, not opinions to average, and last-writer-wins
  would let a launcher whose log scrape came up empty overwrite a firing the
  in-process emitter recorded — a real failure downgraded to a pass. Both sides must
  report `false` for the field to be `false`.
- **Heartbeats are never concatenated, and never substituted.** Two experiments are two
  executions with two gates — one of which faults its completion clock — so appending
  one stream to the other would report a faulted run's ticks inside an unfaulted run's
  count. The first experiment that measured any wins outright.

  The two levels differ here, and this is the one place the rules are not symmetric.
  *Between experiments*, records may come from whichever one measured them: a part
  carrying only the watchdog verdict must not block records that arrive in a later part,
  in either run order. *Against the launcher*, records may come only from the run — there
  is no fallback, because a scraped record's execution is unknowable. So a run that
  measured none reports `null` and keeps the named gap, and a run that measured an empty
  set reports `0` with no gap. Neither is ever filled from elsewhere: crediting a
  launcher record to an unmeasured run manufactures a visibility verdict with no
  observation behind it, and removes the gap that would have said the measurement still
  needs taking.

  Do not "simplify" these two merges back into one function. Applying the launcher rule
  uniformly discards a real experiment's records whenever the verdict-only part happens
  to be folded in first; applying the experiment rule to the launcher restores the
  substitution. Both directions are pinned by regressions in
  `control-runtime-timeout.assembly.test.ts`.

### `exit_watchdog_fired` needs a watchdog that was actually due

This field is the one where an honest-looking observation is easiest to fake, so it
carries an extra condition: it is recorded **only from a tick on which the
post-completion bound had already elapsed**.

The reason is specific. The production watchdog fires ten minutes after the query
completes. On the natural-expiry run the query has not completed while the Write is
parked, and teardown after the result takes seconds — so the watchdog is never due,
and a `false` from that run is true but empty. Delete the `!paused` guard from
`run-heartbeat.ts` and the run still reports `false`. An observation that survives
removing the behaviour it claims to observe is not evidence of it.

So `RunHeartbeatTick.watchdogDue` reports eligibility separately from the decision,
and a dedicated scenario — *a due exit watchdog stands down while the pause holds,
then fires after release* — makes the bound genuinely elapse while the barrier holds
a real live tool call.

**Each firing is attributed to the tick it happened on.** That scenario deliberately
produces a firing after release, so a verdict recorded without asking whether the
pause was in force would let the *required* firing be reported as a firing *during*
the pause — failing the scenario exactly when the production guard behaves correctly,
and reporting a healthy paused run as watchdog-killed. Attribution therefore comes
from the emitter's own `paused` flag for that same tick, folded by
`recordWatchdogTick` in the producer (so CI covers the rule). Within the pause a
firing is sticky: one firing across many due ticks is the failure, and a later quiet
tick does not undo it — while a `false` never overwrites a firing recorded earlier.
It asserts three things together:

| Artifact field | Why it is required |
|---|---|
| `watchdog_due_while_paused > 0` | There was a decision to make. Without this the rest is vacuous. |
| `exit_watchdog_fired == false` | The production guard suppressed it, using the real `PauseGate.isPauseActive()`. |
| `watchdog_fired_after_release == true` | Suppression was a **deferral**. A guard that removed the bound instead would pass the first two and fail this. |

**One input is injected, and the artifact says so.** That scenario reports the
query's completion time as already past the bound, recorded as
`watchdog_completion_fault_ms`, and shortens the tick interval
(`watchdog_tick_interval_ms`) so both sides of the release are sampled. That changes
how often the question is asked, not the answer: the module, the gate, the parked
live Write and the suppression decision are all the production ones. Waiting out ten
real minutes of parked tool call would buy no extra fidelity for a large multiple of
the spend. No other scenario carries the fault.

## Field contract

`observed` = measured in-process by the integration file. `launcher` = must be
supplied by whoever runs the scenario in a pod; see [Launcher-collected
inputs](#launcher-collected-inputs). `observed (shared emitter)` = measured
in-process, but by the *production* heartbeat module rather than by probe code —
see [Shared execution binding](#shared-execution-binding) for why that distinction
is the whole point of those three fields.

| Field | Source | Derived from |
|---|---|---|
| `auto_resumed` | observed | A `pause_released` event with `expired: true` **and** `explicit_resume_calls == 0`. Both halves required. |
| `annotation_count` | observed | Count of `annotation` inputs whose text is the production annotation constant and whose transport result is `delivered`. |
| `extra_assistant_turn` | observed | Live stream ordering after the annotation's delivery timestamp: tool result first ⇒ `false`; model output first ⇒ `true`; neither ⇒ `null`. |
| `neutral_annotation` | observed | All gate events inside the neutral vocabulary, **and** the triggering release was the neutral `pause_released`+`expired` pair, **and** nothing was delivered as `steering`. |
| `resolved_before_release` | observed | A `pause_confirmed` or `pause_unavailable` strictly precedes the `pause_released`, in observed order. |
| `pod_killed` | **launcher** | Pod restart count and pod events across the pause window. |
| `idle_retry_fired` | observed | Whether the idle window fired **and** `resilientQuery` retried rather than re-arming. Absent when the window never fired. |
| `exit_watchdog_fired` | observed (shared emitter) | Whether the production watchdog forced an exit while the gate was paused, recorded **only from a tick on which the bound had elapsed** — see [below](#exit_watchdog_fired-needs-a-watchdog-that-was-actually-due). Recorded, never obeyed: firing during a valid pause *is* the failure. |
| `heartbeats_during_pause` | observed (shared emitter) | Count of the production emitter's own records falling inside the observed pause window. Requires a budget at or above the [observable floor](#choosing-a-budget). |
| `paused_distinguishable_from_stalled` | observed (shared emitter) | Every in-window tick took the paused branch, carries a non-empty `controlPhase`, and says so in its message. |
| `spill_output_preserved` | observed | The pre-pause locator survived the pause and matched durably, with no write during the hold. |
| `held_hook_timeout` | observed, **derived** | See [below](#held_hook_timeout-is-derived-not-asserted). |
| `deadline_clamp` | observed | `PauseGate.safeBudget` against the production deadline source. |
| `cancellation` | observed | The barrier's own decisions for work held at cancellation time. |

### `extra_assistant_turn`: read from the stream, not the flag

Derived from live stream ordering, **deliberately not** from the transport's
`shouldQuery` flag. Reading that flag would only prove what the adapter *asked
for*; the claim is about what the provider then did. On expiry the barrier admits
the tool it was holding, so the tool's result should arrive before any further
model output. `null` when the stream shows neither — an unobserved ordering is
not a pass.

### `neutral_annotation`: exactly one, and neutral

The production Claude adapter is the only component that translates an expiry into
provider vocabulary; the shared path publishes it as a runtime fact. So the
annotation must arrive as a neutral `annotation` input — never `steering`, which
would inject an instruction into a run that never asked for one. An event type
outside the neutral set anywhere in the stream means a provider-shaped event
reached the shared path, and fails the field.

### `deadline_clamp`

```json
{ "granted_ms": 300000, "remaining_ms": 420000, "finalization_margin_ms": 120000,
  "nonpositive_budget_rejected": true,
  "observed_by": "PauseGate.safeBudget with the production deadline source" }
```

`remaining_ms` is the **whole** distance to the deadline with the margin still in
it, because the evaluator's predicate is `granted <= remaining - margin`.
Recording an already-reduced remaining would make that comparison pass on
different arithmetic than the one being claimed. `granted_ms` stays `null` on a
refusal rather than becoming `0`. `nonpositive_budget_rejected` requires the
exhausted-deadline request to have been refused with the `no_safe_budget` failure
specifically.

### `cancellation`

```json
{ "held_work_admitted": false, "held_work_denied": true, "annotation_emitted": false,
  "held_work_count": 1, "unresolved_held_work": 0, "observed_by": "fixture" }
```

`held_work_denied` requires that held work **existed** and that all of it was
refused with nothing left unresolved. An empty decision list is `null`, not
`true`: a run that held nothing would otherwise satisfy the field vacuously, and
the point is that an abort must not flush the side effects the operator aborted to
prevent. The cancellation scenario uses a long budget on purpose, so an expiry is
not a rival explanation for the release.

### `held_hook_timeout` is derived, not asserted

```json
{ "exercised": true, "state": "running", "reason": "barrier_timeout",
  "hook_timeout_seconds": 1860, "pause_budget_seconds": 1800,
  "signal_aborted": true, "safety_release_used": false,
  "observed_by": "real CLI hook abandonment plus the coordinator's reported outcome" }
```

`exercised` is a conjunction of two independent observations — the CLI abandoned
the hook on its own (`signal_aborted`, no safety release) **and** the coordinator
reported the consequence (a non-null reason). It is deliberately not an input: the
hook-timeout-exceeds-budget bound is the one bound the adapter does not enforce
itself, so a producer-asserted `exercised: true` there would be self-certification.

`state` and `reason` pass through as observed. A lapsed-but-`paused` state or an
empty reason reaches the evaluator and fails, by design.

## Launcher-collected inputs

**One field is a property of a running pod and nothing else: `pod_killed`.** A
process cannot testify that it was not killed. Its restart count, its UID and its
container state are observations from outside, and nothing in the runner
manufactures them — a worker that logged its own survival is not a witness.

The visibility fields are no longer in this category; see [Shared execution
binding](#shared-execution-binding). They are measured in-process through the
production emitter, so a run that took them satisfies those fields itself and the
launcher entries remain only as optional corroboration.

When an observation is absent its field stays `null` **and** the artifact carries a
named `missing_launcher_inputs` entry stating exactly what to collect:

```json
"missing_launcher_inputs": [ { "field": "pod_killed", "collect": "..." } ]
```

`REQUIRED_LAUNCHER_INPUTS` in the producer is the authoritative text of each, and an
entry appears **only while its observation is genuinely missing**. A run that
collected the visibility fields lists `pod_killed` alone. Two things follow:

- A non-empty `missing_launcher_inputs` is an incomplete run, not a failed
  implementation — but it is still a **failure**, not a waiver. `check_w2_05` reads
  the `null`s and fails, which is correct: the story is not accepted on a run that
  did not observe it.
- "Collected" and "favourable" are different questions. An empty heartbeat array is
  a real measurement of zero and is reported as `heartbeats_during_pause: 0`, not
  relabelled as uncollected. A zero fails the check, and that is the honest outcome
  for a run with no visibility evidence to offer.

### Choosing a budget

The heartbeat emitter only logs once a run has been silent for
`HEARTBEAT_SILENCE_THRESHOLD_MS` (60s), on a `HEARTBEAT_INTERVAL_MS` (30s) tick.
**A pause shorter than that produces no heartbeat record at all**, and
`heartbeats_during_pause: 0` then fails W2-05 for a reason that has nothing to do
with the pause implementation.

So the default 12s budget — chosen to keep the in-process probes bounded — cannot
produce the visibility fields. The run that collects them must raise it to at least
`HEARTBEAT_OBSERVABLE_FLOOR_MS`, which is derived from the emitter's own two
constants rather than restated, so it cannot drift from them:

```bash
# Either: let the runner pick the floor for you.
npx ts-node src/control-runtime-timeout.integration.ts --heartbeat --json out.json

# Or: name a budget explicitly. An explicit value always wins over --heartbeat —
# an operator who named a budget gets that budget.
ADP_PAUSE_EXPIRY_BUDGET_MS=180000 npx ts-node src/control-runtime-timeout.integration.ts --json out.json
```

The observation ceiling scales with the budget automatically. A malformed or
nonpositive override is ignored and logged rather than clamped — silently
substituting a default is how a run ends up measuring a budget nobody chose.

When the configured budget clears the floor, the natural-expiry scenario **fails if
no heartbeat record arrived**. A run asked to collect visibility evidence must not
report an empty field as a pass. Below the floor the emitter is not started and the
fields stay `null`, which the check also fails — the difference is only in which
kind of gap the artifact names.

## Report contract

Alongside the artifact, each experiment emits a report with a stable `name`, an
`ok`, a human-readable `detail` and its raw `observations`. An experiment that
throws becomes a **failure carrying its cause** — never a skip, which would let an
infrastructure break read as an absence of evidence.

Report names carry the acceptance ID they answer, so a reader can map a report to
the issue's criteria without a lookup table:

| Report `name` | What it measures |
|---|---|
| a pause left to its budget auto-resumes and admits the work it held (AC-P3) | Natural timer expiry: auto-resume, the single annotation, no extra turn, the tool's post-resume side effect. |
| the real idle-retry watchdog re-arms instead of retrying a paused run (AC-P5) | An idle window firing during a pause re-arms rather than retrying. |
| the pause budget is clamped to the deadline and a nonpositive budget is refused (AC-P6) | The clamp arithmetic and the `no_safe_budget` refusal. No SDK, no spend. |
| cancellation denies the work it held and sends no resume annotation (AC-P3) | Cancellation denies held work and emits no annotation. |
| a due exit watchdog stands down while the pause holds, then fires after release (AC-P3) | The only scenario that makes `exit_watchdog_fired` mean something. Injects a completion-time fault (recorded); everything else is production. |

## Running it

The live scenario spends real money and needs a real pod. It is **not** run by
ordinary CI and not by a developer on an administrator-role host; it belongs to
the protected launcher that owns #3968's fixture execution.

```bash
# from modules/agent-factory/agent — ts-node is the package's TS runner.
# --heartbeat raises the budget to the observable floor so the visibility fields
# are collected; without it they stay null and W2-05 fails on them.
./node_modules/.bin/ts-node src/control-runtime-timeout.integration.ts \
  --heartbeat --json artifacts/pause_expiry.json
```

The `--json` path receives `{ sdk_version, reports, pause_expiry }`; the
`pause_expiry` value is the artifact this document specifies. Any
`missing_launcher_inputs` are also printed to stdout with their collection
instructions. Exit status is nonzero if any experiment failed.

To emit one artifact that also carries the held-hook-timeout and spill
measurements, run the sibling instead — it calls `runTimeoutExperiments()` and
merges both sets:

```bash
./node_modules/.bin/ts-node src/control-runtime.integration.ts --json artifacts/pause_expiry.json
```

The producer's logic, the shared emitter and the runner's own assembly of them are
covered without any spend, and this is what CI runs (all three files pinned by name in
the `Worker control tests` job):

```bash
./node_modules/.bin/jest src/control-runtime-timeout.test.ts src/run-heartbeat.test.ts \
  src/control-runtime-timeout.assembly.test.ts
```

The third suite imports `control-runtime-timeout.integration.ts` to exercise the
**real** exported assembly rather than a copy of it — a re-implementation would agree
with whatever defect it was written against. That import is safe in CI because the
only SDK-backed dependency in that file sits behind a lazy `await import` inside the
run function, so importing the module evaluates no SDK, starts no subprocess and
spends nothing. Jest still cannot collect the `.integration.ts` file itself, which is
what keeps the live experiments out of CI.
