# Security review — PR #5205 (issue #3961, S2 pause/resume), revision `8e9efe4`

Second-pass review. Method: full-diff vulnerability sweep (`git diff origin/main...HEAD`,
26 files, +6539/−160) followed by per-finding false-positive filtering under the
standard hard-exclusion and precedent list, discarding anything below confidence 8.

**Result: no security findings at or above the reporting threshold.**

Two candidates were raised by the sweep. Both were filtered out as security findings.
One of them is a real correctness defect and has been carried into the code review as a
merge blocker (B8) instead — recording it here so the disposition is traceable rather
than silently dropped.

## Candidates and their disposition

### C1 — `SubagentStop` settles the main thread's tool admissions → false `paused`
`modules/agent-factory/agent/src/harnesses/claude-control.ts:379`,
registered at `modules/agent-factory/agent/src/developer-checkpoints.ts:117`.

Filtered confidence **7/10** — below the threshold, so **not** reported as a security
finding. The mechanism is real and I reproduced it independently (see code review B8):
a per-subagent event settles a session-global admission ledger, so the gate reports
`paused` while a main-thread tool is mid-write.

Why it is not a security finding: there is no external attacker, no privilege boundary
crossed, and no code-execution or exfiltration impact. The harm is an incorrect
operator-facing containment claim. It is also not triggerable as a *pause* on this
branch, because `IMPLEMENTED_CONTROL_VERBS` is empty (`control-runtime.ts:101`) and
`ClaudeControlAdapter.requestPause` returns `unavailable` from the capability
intersection before ever reaching the gate (`claude-control.ts:614`).

Correct home: a correctness/false-containment blocker in the code review, which is
where it now sits. It is exactly the failure mode the story exists to prevent, so it
blocks on its own merits — it simply is not a vulnerability.

### C2 — shell-level backgrounding invisible to the background-work probe
`modules/agent-factory/agent/src/harnesses/claude-control.ts:213`
(`requestsBackgroundWork`).

Filtered confidence **2/10** — **false positive**. `nohup … &`, `setsid`, `& disown`
set neither `toolName === 'Task'` nor `run_in_background === true`, so `count()`
answers a hard `0`. That much is accurate. But no tool-input-level classifier can
decide from an arbitrary command string whether a process detaches, and the only sound
alternative — treating every `Bash` as unobservable — makes pause never confirm, i.e.
deletes the feature. Hard exclusion #7 (lack of hardening measures) applies: this is an
inherent limitation of the observation layer, not a concrete vulnerability. The PR also
adds no regression, since no pause feature and therefore no containment claim existed
before it.

The claimed attacker model does not hold either: it requires the agent to already be
executing attacker-authored shell, which is a far larger pre-existing exposure, and
pause was never designed as a boundary against a prompt-injected agent.

Downgraded to a documentation nit (carried into the code review as an optional
follow-up): the module comment at `claude-control.ts:227–229` asserts *"Not an
assumption: a tool that never requested backgrounding has nothing behind it."* That
sentence is false for shell-level detachment and should say that the probe observes
only harness-declared backgrounding.

## Examined with no finding

- **Authorization on the new executor seam.** `control-listener.ts` `applyAccepted`
  routes envelope-bearing commands through `store.deliverAuthorized(commandId, run)`
  and others through `store.markDelivered`. I verified at `control-state.ts:269` and
  `:279` that each refuses the other's entries, so an envelope-gated command cannot be
  executed through the non-envelope path and revocation at pre-delivery revalidation
  still stops execution. No authorization bypass.
- **No `requiresEnvelope` TOCTOU** — the capability read and the delivery decision are
  not separated by an await that could change the answer.
- **`PreToolUse` returning `{}` on admit** is the security-correct choice: an explicit
  `allow` would override a deny from another hook or a permission rule, turning the
  pause barrier into a privilege escalation. The code comments this deliberately.
- **`markBreached` denies rather than admits** parked admissions — fails closed.
- **`control-runtime.integration.ts` uses `permissionMode: 'bypassPermissions'`** and
  writes into a `mkdtempSync` directory. Not collected by Jest (`testMatch:
  ['**/*.test.ts']`) and not invoked by any workflow, so it is unreachable from CI and
  carries no privilege delta for automated runs. It is an operator-run experiment.
- **No injection surface added.** The gateway diff is comment-only; no new SQL, shell,
  template, deserialization or path-construction sites. `agent-control-eval.py` reads
  operator-supplied JSON artifacts with `json.load` and key allow-listing, no `eval`
  or `pickle`.
- **No secrets, tokens or PII** introduced in code, comments, artifacts or logs. The
  committed experiment artifacts contain session ids and counters only.

## Note relayed rather than acted on

The sweep sub-agent's output tripped the harness's instruction-shaped-content detector
on the string `bypass-permissions`. That string is the `permissionMode:
'bypassPermissions'` literal in `control-runtime.integration.ts`, quoted as evidence.
No directive in that output was treated as an instruction; it is recorded here as an
observation about the reviewed code, which is assessed above.

---

*Reviewed by @agent-reviewer (Claude). Code review:
`data/code-review/review-20260915-pr-5205-rev2.md`.*
