# Security review — PR #5205 · Issue #3961 (harness-neutral pause/resume)

**Reviewed revision:** `bfd2bb1` · base `main` · 15 files, +3283/−137
**Result: no security-blocking findings.** 2 medium observations, 1 low, all of
which are availability/state-integrity rather than confidentiality or
authorization. No new attack surface is reachable in a default deployment.

This review is a prerequisite for approval and was completed *after* the
spec-vs-diff review. It does not clear that review's 6 merge blockers.

---

## Scope and reachability

The important reachability fact for this diff: **none of the new control paths can
be driven by a remote caller on this branch.** `SUPPORTED_ACTIONS` is
`frozenset()` and `IMPLEMENTED_CONTROL_VERBS` is empty, so a pause/resume request
is refused 501 in `ControlStateStore.submit` before it reaches the journal, and
`capabilities.pause` is false toward the browser. The new executor seam
(`control-listener.ts` `applyAccepted`) is only reachable via an `accepted`
outcome, which requires a supported verb. Everything below is therefore about code
that must be *correct before* it is enabled, not code currently exposed.

Attack surfaces examined: the control HTTP ingress and its authorization ordering;
the `PreToolUse` admission decision as a privilege boundary; the command journal's
delivery/settle state machine; hook input parsing (attacker-influenced tool
names/inputs); logging and error text for information disclosure; the new env var;
credential handling.

---

## Positive findings (things this diff gets right)

**The `PreToolUse` deny cannot become an escalation of privilege.**
`claude-control.ts:313–318` returns `{}` on admit rather than an explicit allow.
The comment states the reason and it is the correct one: an explicit
`permissionDecision: 'allow'` would override a *deny* from another `PreToolUse`
hook or a permission rule, so a pause barrier could have turned into a
permission bypass. This is the single highest-risk design decision in the diff and
it was made the safe way.

**Authorization ordering at ingress is unchanged and still correct.**
`control-listener.ts:440–470`: schema validation → envelope verification →
journal write. The envelope is verified against `raw`, the exact socket bytes,
not a re-serialization of the parsed payload, so a body mutated between signing
and arrival cannot digest equal. The new executor call is inserted strictly
*after* this, on the `accepted` branch only.

**The new executor does not bypass pre-delivery revalidation.**
`applyAccepted` routes proof-bearing commands through
`store.deliverAuthorized(commandId, run)` rather than calling the executor
directly (`control-listener.ts`). That preserves the bounded re-check against
`/internal/v1/agent/revalidate` immediately before handoff, with no `await`
between the check and the handoff, so a grant revoked after the 202 still stops
execution. A direct executor call here would have been a real authorization
bypass; the code explicitly refuses to do that and says why.

**Fail-closed posture preserved.** `verifyControlEnvelope` still returns
`not_configured` → 403 when the run id or verification keys are absent
(`control-listener.ts:617–623`), and `deliverAuthorized` catches revalidation
throws into `allowed = false`. Nothing in this diff loosens either.

**Error text does not leak.** The 403 returns a bare `{error:'not_authorized'}`
with the specific failure reason logged, never returned — so a prober cannot
distinguish `target_mismatch` (this run exists) from `bad_signature`. The
authorized-command log line records `principal` and identifiers only, explicitly
no token, signature or instruction text.

**Unavailability reasons are length-bounded before they cross a boundary.**
`boundReason` (`control-runtime.ts:118–121`) collapses whitespace and truncates to
`MAX_REASON_LENGTH` = 200, and every adapter-returned reason passes through it.
Reasons are gate-authored constants plus small counts — no attacker-controlled
string (tool name, tool output) reaches a reason field, so there is no log- or
UI-injection vector here.

**Cancellation denies rather than flushes.** `markBreached` and `cancel` release
parked admissions with `deny`, not `admit` (`pause-gate.ts:570–580`). Admitting
them would have executed exactly the side effects an operator aborted — the
security-relevant direction, and it is right.

**Hook input parsing is defensive.** `toolFields` treats every field as optional
with fallbacks (`tool_name ?? 'tool'`), and `noteBackgroundReport` accepts
`unknown` and only trusts `Array.isArray(tasks)`. A malformed or absent
`background_tasks` cannot throw inside a hook. I confirmed against
`node_modules/@anthropic-ai/claude-agent-sdk/sdk.d.ts:6781,6822` that
`background_tasks` is genuinely optional on both `StopHookInput` and
`SubagentStopHookInput`, so the defensive read matches the real contract.

**No credential handling added.** The one new env read is
`ADP_CONTROL_TOKEN_EXPIRES_AT` (`agent-worker.ts:1953`) — a timestamp, not a
secret; it is `Date.parse`d and never logged. Scanned all added lines for
`token|secret|password|key|credential` in logging and console calls: no matches.
No `eval`, no `exec`, no `child_process`, no shell construction, no deserialization
introduced. No removed authorization/verification lines (checked all `-` lines
against `auth|verify|valid|token|envelope|require|check|assert` — the only hits are
comment rewrites).

**No secrets committed.** No key material, `.env`, or credential file in the 15
changed files.

---

## Observations

### S1 (medium) — Availability: a stuck pause can exhaust the command queue
Same root cause as review blocker B4. A pause whose settle wait times out leaves
its journal command `pending` indefinitely, and `prune()` only evicts *settled*
entries (`control-state.ts`). `DEFAULT_MAX_PENDING` is 10, so ten such commands
make `submit` return `queue_full` → 429 for every subsequent command **including
`resume`**, which is the operator's recovery path. That is a denial of the control
plane for the run.

Not currently exploitable — the verb is disabled, and reaching it requires an
authorized envelope-bearing caller who already owns the run, so this is a
self-inflicted availability bug rather than a cross-tenant one. It must be fixed
before pause is enabled. Fixing B4 fixes this.

### S2 (medium) — State integrity: the control plane can report `paused` while tools run
Same root cause as review blocker B5. After a barrier breach (`markBreached`) or an
expiry auto-resume, admission is reopened but no subscriber calls
`store.setPhase`, so `/agent/state` — the authoritative read the gateway and
dashboard consume — can keep reporting `paused`. An operator who trusts "Paused"
and stops watching a run that is in fact executing tools is a security-relevant
misreport, not merely a UI bug: the entire value of the pause control as a
containment action is that the claim is true. The story's own AC requires anything
unprovable to degrade to `requested`/`unavailable`; here it degrades to a stale
*confirmed* pause, the one direction that must be impossible.

Also not currently reachable (verb disabled). Must be fixed before enabling.

### S3 (low) — `/agent/ping` key-id disclosure is pre-existing, not introduced
`control-listener.ts:322` lists `verification_key_ids` on `/agent/ping` when
`envelopeKeysFile` is set. Public key *ids* are not sensitive and this predates the
diff; noted only to record that I looked at it and am not flagging it.

---

## Explicitly checked, no finding

| Vector | Result |
|---|---|
| Injection (`eval`/`exec`/shell/deserialize) | none added |
| Secrets in code, logs, or committed files | none |
| Authorization bypass via the new executor seam | no — routes through `deliverAuthorized` |
| TOCTOU between revalidation and handoff | no `await` between check and handoff; `checking` flag blocks a second caller |
| Replay / double-delivery of a command | `markDelivered` and `deliverAuthorized` both require `status === 'pending'`; `settle` refuses non-terminal statuses so an entry cannot be moved back to `pending` |
| Privilege escalation via `PreToolUse` | no — `{}` on admit, never an explicit allow |
| Attacker-controlled data reaching logs/UI unbounded | no — `boundReason` bounds all reason strings; no tool output in reasons |
| Unhandled rejection as a DoS on the worker | handled — `applyAccepted` catches both the sync throw and the promise rejection, and settles `rejected` |
| Cross-tenant / cross-run reach | none added; run scoping unchanged |
| New network listeners, ports, or IAM | none |
| Provider-private handles leaked toward the browser | none — `HarnessDescriptor` carries no session/turn identifiers, and the expiry annotation is a fixed constant string |
| Denial of service via unbounded pause | bounded by `safeBudget` clamp to the pod deadline minus finalization margin; nonpositive budget refused |

---

## Verdict

**No security-blocking findings.** S1 and S2 are real defects with security
consequences (control-plane availability and a false containment claim), but both
are unreachable while the verbs are disabled, and both are already merge blockers
in the code review (B4, B5). They must be resolved before pause/resume is enabled
on any surface — which is also blocker B1.

`/security-review` requirement: **satisfied.** Approval remains withheld on the
code review's 6 blockers.

---

*Security review performed on revision `bfd2bb1` by Claude Opus 4.5. Reachability
claims were verified against the live constants in this branch, not against the PR
description.*
