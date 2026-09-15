# Security review — PR #5205 (issue #3961, S2 harness-neutral pause/resume), rev3

**Reviewed revision:** `71de9ec` (`f674643` and `8b98837` are empty WIP markers — identical trees)
**Base:** `main` · 28 files, +7208/−160
**Date:** 2026-09-15 · Reviewer: @agent-reviewer

## Result

**No security findings at or above the reporting threshold.**

Every candidate raised during identification was either non-exploitable or fell into an
excluded class. Nothing survived to the false-positive filtering stage, so no finding is
reported. The correctness defects found in this pass are in the code review
(`data/code-review/review-20260915-pr-5205-rev3.md`), not here — they are false-containment
bugs with no attacker and no privilege boundary crossed.

## Reachability baseline (verified by execution, not assumed)

This matters more than any individual line of the diff: the entire pause path is
**latent** on this revision.

```
IMPLEMENTED_CONTROL_VERBS:        []
adapter claims:                   {"pause":{"supported":true},"resume":{"supported":true},...}
listenerActionsFor:               []
effective capabilities:           {"pause":false,"resume":false,"steer":false,"abort":false}
```

- `IMPLEMENTED_CONTROL_VERBS` (`control-runtime.ts:101`) is empty.
- `control_service.SUPPORTED_ACTIONS` (`control_service.py:112`) is still `frozenset()` —
  the gateway diff there is comment-only.
- The three-way intersection therefore yields no verb; `ControlStateStore.submit` answers
  `unsupported` → 501 before any journal write, so `PauseGate.requestPause` is not reachable
  from the wire.
- `FEATURE_AGENT_CONTROL_ENABLED` also remains off by default.

Consequence for this review: no pause-path defect on this branch is remotely triggerable
today. That is a *deployment* fact, not a *code* fact — it is why the correctness findings
are blockers for the story rather than security incidents for the fleet, and it is also why
they must be fixed before any verb joins those sets.

## Focus areas examined

### 1. Quiescence forgery in the per-thread admission scoping — no vulnerability

`claude-control.ts:326-411`. The `scopeOf`/`outstanding` fix is sound. `agent_id` is read
from the same `BaseHookInput` field on `PreToolUse`, `Stop` and `SubagentStop` (confirmed
against the installed SDK 0.3.220 `sdk.d.ts`: absent on the main thread even in `--agent`
sessions, required on `SubagentStopHookInput`), so a subagent's stop can only settle
tickets its own thread opened.

One residual gap is real but is not a security finding: `onStop` calls
`observer.noteBackgroundReport(stop.background_tasks)` **unscoped**
(`claude-control.ts:393`), so a subagent's empty report advances `lastReportSeq` past
`lastSpawnSeq` and flips `count()` from `null` to `0` — one subagent's all-clear vouching
for a still-running sibling. Measured:

```
after Task spawn                = null
after sibling all-clear         = 0
```

This is a false-quiescence *input*, but it can only influence `confirmIfClear()`, which is
gated on `phase === 'pause_requested'`, which no verb can reach. No attacker, no privilege
boundary, no exfiltration → correctness, filed in the code review.

### 2. `noteBackgroundWorkChanged` background edge — fails closed

`pause-gate.ts:381-402`. Guarded on `phase === 'pause_requested'` and `inFlight.size === 0`,
then epoch-checked inside `serialize`. It cannot bypass `confirmIfClear`, cannot resurrect a
cancelled or breached pause, and `markBreached` is sticky and *denies* rather than admits
parked work. Verified by probing all four directions (resume, cancel, live tool, breach).

### 3. Authorization / executor seam — no bypass

`control-listener.ts:536-600`. `requiresEnvelope(action)` and `submit`'s support check read
the same immutable `supported` set, so every *accepted* command is by construction
envelope-required — `markDelivered` is unreachable for it. `control-state.ts:269,279,321`
make the two paths mutually exclusive: `markDelivered` refuses entries carrying
`authorization`, and `settle('applied')` refuses an authorization-bearing entry that is not
already `delivered`. No TOCTOU — no `await` separates the capability read from the delivery
decision, and `capabilities()` is constant for the store's lifetime.

The new `403` for an envelope-less supported verb is the correct fail-closed direction.
Weakening it to "enforce only when the header is present" would admit any stolen-token
holder; it should stay as written.

### 4. Capability gating in `control_service.py` — comment-only

Allowlist still empty. `agent-control-ci.yml` asserts the Python and TypeScript sets agree
(both gates expect the empty set, lines 322 and 480), so the two cannot drift silently.

### 5. `agent-control-eval.py` — no new sink

Artifacts are read with `json.loads` plus `isinstance` shape checks and a required-key
allowlist. No `pickle`, `yaml.load`, `eval`, `exec`, `subprocess`, or shell construction
anywhere in the file. The `run_id` interpolated into a probe path comes from the operator's
own fixture config and is additionally gated on `fixture_isolated is True` and a 12-digit
`account_id` match.

### 6. `permissionMode: 'bypassPermissions'` in `control-runtime.integration.ts` — not reachable

Jest's `testMatch` is `['**/*.test.ts']`, so this file cannot be collected (independently
confirmed: only `lcm-context.integration.test.ts` matches; 90 test files total). No workflow
references it — the only `ts-node` use in `agent-control-ci.yml` is the inline `.verb-gate.ts`
script. It is guarded by `require.main === module` and writes only inside `mkdtempSync`. The
same mode is already the established setting across existing production agent code
(`agent-superpower.ts`, `run-query.ts`, `FixOrchestrator.ts`), so there is no privilege delta.

## Also examined, no finding

- **`PreToolUse` returns `{}` on admit, never an explicit `allow`.** Security-correct: an
  explicit allow would override a deny from another hook or a permission rule, converting
  the pause barrier into a privilege-escalation primitive. Worth preserving deliberately.
- **`permissionDecisionReason`** only ever receives hardcoded constants — no operator- or
  model-controlled text reaches the denial surface.
- **`PAUSE_EXPIRY_ANNOTATION`** is a fixed string delivered with `shouldQuery: false`; no
  untrusted interpolation.
- **New `attemptInputFactory` / `onAttemptHandle` / `cancellation` wiring** switches the
  prompt to a streaming iterable only when a listener actually started; `origin: {kind:
  'human'}` matches the prior string-prompt trust treatment, so no trust-attribution delta.
- **`idleSuspended` re-arm** (`resilientQuery.ts`) is bounded by the gate's expiry timer,
  itself clamped to the pod deadline. The unbounded-wait concern is DOS-class and excluded.
- **No secrets, keys, tokens, ARNs or account IDs** in added code, comments, the committed
  experiment artifacts (`data/experiments/3961-*.json` hold session IDs and counters only),
  or the new markdown. New heartbeat log fields are `controlPhase` / `heldTools` /
  `activeTools` — counts only.
- **No new SQL, XML, template, path-construction or deserialization surface** in the diff.

## Documentation accuracy note (not a security finding, worth fixing)

`claude-control.ts:227-229` asserts: *"Not an assumption: a tool that never requested
backgrounding has nothing behind it."* That is false for shell-level detachment
(`nohup … &`, `setsid`, `& disown`), which sets neither `toolName === 'Task'` nor
`run_in_background === true`, so `count()` answers a hard `0`. The comment should say the
probe observes only *harness-declared* backgrounding — the honest bound, and the one a
future maintainer needs in order to reason about the gap.
