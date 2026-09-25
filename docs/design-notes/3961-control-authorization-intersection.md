# #3961 — The control-authorization intersection: two callers, one gate


## Pause lifecycle corrections — 2026-09-19

Pause uses an absolute workload deadline returned by the authenticated bootstrap
service. The gateway reads the verified pod's controller Job by name and UID in
the worker namespace, then conservatively bounds lifetime from its creation time
plus `activeDeadlineSeconds`, intersecting any tighter pod limit. This includes
image startup, clone time and previous pod attempts. The worker pins that value
before repository bootstrap and cannot extend it by renewing a credential. A
missing, denied or malformed lifecycle read leaves pause unavailable. The
existing namespace-scoped gateway reader therefore also needs `get` on `batch`
Jobs; workers receive no Kubernetes API permission. This source change does not
apply that permission or release the #5195/#5210 rollout hold.

State capabilities intersect the implementation allowlist with the adapter's
current attempt, barrier health and remaining deadline. Detached, cancelled and
breached attempts report unavailable. Signed command admission uses the fixed
implementation allowlist so a temporarily unavailable adapter cannot bypass
authorization. Reusing one command ID for a different action is a 409 conflict,
even when the request bodies are identical.

Independent review found additional runtime/evaluator defects after the initial
background-observer repair. The query wrapper now holds task output and terminal
teardown through the neutral pause gate, keeping the current attempt and listener
alive until release, expiry or cancellation. Each retry constructs fresh hook
callbacks bound to its opaque attempt identity. Superseded callbacks cannot settle
replacement tickets or change their background observations; unobserved work from
a retired attempt remains uncertain.

Bash, direct MCP calls and unknown tools may start operations that outlive their
responses, regardless of `run_in_background`. The SDK's `background_tasks` list
cannot inventory these external effects, so even an empty report leaves their
scope unobservable. The gate reports requested/unavailable for that run. Only
the pinned local file/notebook/todo tools are treated as completion-bounded;
SDK-managed Task/Agent delegation still requires its own fresh task report.
Enabling confirmed pause after opaque work requires a trusted supervisor's real
quiescence evidence. A returned response, input flag or empty SDK task list is
insufficient.

The public store now shows `pause_requested` when admission closes, before waiting
for running tools. Explicit nonpositive/nonfinite budgets are refused. The Wave-2
evaluator requires non-empty session and attempt identities, and the standalone
SDK resume fixture now uses the production adapter and query wrapper to measure
those identities, held-tool admission, and resume ordering. These corrections do
not constitute deployed live acceptance or relax Option A.

The listener serializes signed-command revalidation and executor handoff in journal acceptance order. It does not hold that queue while a pause waits for tool settlement, so a later resume can cancel the delivered pause promptly. Gate transitions settle delivered commands only; queued commands have not yet passed delivery authorization. A cancelled pause's late result cannot overwrite a newer pause or the journal's cancellation. The shared echo contract also rejects explicit zero, negative, and nonfinite budgets. The retained SDK fixture includes a loopback HTTP service whose request counter must stay zero during the hold and reach one after resume.

Admission counts all live commands, including delivered pause waits and executors
whose journal outcome has already settled. Pause and steering each have ten slots
by default. Resume and abort each retain one independent slot when those queues
are full. Additional live resumes or aborts return 429, while same-id retries
replay the accepted command. A pending pause does not consume steering capacity. Entries
cannot be pruned while revalidation or execution is active. Resume wakes the old
pause's quiescence waits immediately, clearing their timers without waiting for
the admitted tool to finish. The signed-listener regression floods a cap of three
with 25 unique pauses, checks journal/executor/timer bounds, then resumes while the
tool remains admitted. This is worker transport evidence, separate from the real
SDK artifact and deployed acceptance.

## Implementation update — #5222, 2026-09-19

The signed envelope now distinguishes `human_session` from `delegated_grant`.
Existing envelopes without `authority_kind` retain the delegated format and
must still contain a grant ID and positive revocation epoch. Human envelopes
must omit grant, epoch and delegated authority-reference claims. Both verifiers
consume the same signed positive and negative test vectors.

The human Activity and orchestration routes require a current JWT human session,
resolve its canonical user within the authenticated tenant, and read active
membership from SQL. Before signing, the control service checks the target's
active execution, owning human and registration generation in the protected
store. Worker-writable attribution is insufficient. The signed lifetime is at
most 30 seconds and cannot outlive the user's authenticated session. Before
execution, the worker's existing revalidation endpoint checks current ownership
and membership again. Removing membership after acceptance rejects the queued
command. No human credential is sent to the worker.

Both human routes now forward the exact signed bytes through the existing
validated pod transport. The delegated route uses the same transport after its
existing grant authorization; it retains grant/epoch/flow revocation checks.
Neither path reports success merely because authorization succeeded. Public
acknowledgements project the expected command and state fields, preserving
200 versus 202 and returning an unknown outcome on transport failure.

Pause/resume are included together in the gateway, delegated-policy, worker and
CI implementation sets. An absent adapter barrier or unavailable attempt still
vetoes support. The gateway also withholds advertised controls when authority or
signing configuration is missing. Steer and abort remain unavailable.

**Deployment decision:** keep `agent_authority_enabled` default **false**. Pause
requires the existing protected bootstrap and signing configuration; do not
bypass that requirement or enable broad authority merely to distribute keys.
Verify the approved worker digest, gateway signing key and worker verification
keys through the scoped rollout, retaining #5195/#5210 release and isolation
gates. This change makes no Terraform/IAM apply and flips no live feature flag.
The rollback is to disable the control feature, then revert the implementation
sets together; preserve queued command and invocation history.

The owner selected **Option A**: #3961 remains open until this authorization
integration and deployed live acceptance are complete. The implementation and
standalone SDK fixtures do not establish #3968 or Q3 acceptance. The analysis
below records the original gap and alternatives; this update supersedes its
statements that human signing and command forwarding are unimplemented.


**Status:** blocker found during #3961 implementation. The pause barrier itself is
built and proven; **end-to-end pause on the human dashboard path cannot work**
until this is resolved. Filed as the ADR-1 change proposal the story's stop
condition requires.
**Issue:** [#3961](https://github.com/aws-e/adp/issues/3961) (S2 — harness-neutral
pause/resume), wave 2 of live-controls EPIC #3959
**Touches:** [#5028](https://github.com/aws-e/adp/issues/5028) delegated authority
(`docs/design/agent-delegated-authority.md`), #3960 revival design
**Live acceptance:** evaluation [#3968](https://github.com/aws-e/adp/issues/3968)

## The problem in plain terms

An operator presses **Pause** on a running agent from the dashboard. The worker
refuses it with "not authorized," even though the operator owns the run, is
logged in, and the pause machinery inside the worker is working correctly. Every
human-initiated pause fails this way, in every deployment, for the same reason —
so the button cannot ship even though the feature behind it is finished.

The cause is that two separate pieces of work each solved authorization for their
own caller, and nobody designed the point where they meet. #5028 built
authorization for *one agent commanding another* and requires every command to
carry a signed permission slip. #3960 built authorization for *a human commanding
their own run* and proves ownership a different way, with no permission slip.
Pause is the first verb that actually does something, so it is the first time
those two designs are forced to agree — and they don't.

**The fix in one line:** teach the gateway to sign a permission slip for a
logged-in human who owns the run, as its own kind of authority, instead of either
faking an agent's credentials or removing the check.

## Why it surfaced now and not in #5028

The worker listener decides whether a command needs a signed envelope with
(`modules/agent-factory/agent/src/control-listener.ts`):

```ts
private requiresEnvelope(action: ControlAction): boolean {
  return this.config.store.capabilities()[action] === true;
}
```

This coupling is deliberate and its comment explains why: *"When a verb joins
`SUPPORTED_ACTIONS`, it becomes envelope-gated by that fact alone, with no second
edit to remember here. That coupling is the point; a separate opt-in list is a
list someone forgets to add to."*

The same comment states the consequence that has now expired: *"this returns
false for every verb today, so the envelope path ships tested but dormant."* #3961
is the first story to enable a verb, so it is the first story for which
`requiresEnvelope` returns true — and the first to discover that the human path
has nothing to put in the header.

`grep -i envelope` over `modules/gateway/src/activity/` returns nothing.

## The four independent blockers

Enabling pause/resume is not one gap but four. Two are downstream of envelope
*minting*, so "make the gateway sign something" does not clear them.

| # | Blocker | Evidence |
|---|---|---|
| 1 | Human path sends no envelope. `_request_pod` sends only `Authorization: Bearer <control token>` and `X-Adp-Control-Generation`. | `modules/gateway/src/activity/control_service.py` |
| 2 | Nothing for a human to cite as authority. `sign_envelope` requires `grant_id` + `revocation_epoch`; `_sign` raises when `authorized.grant is None`; `policy.authorize` refuses a grantless mutation. | `src/agentauth/envelope.py`, `src/agentauth/adapter.py`, `src/agentauth/policy.py` |
| 3 | The pre-delivery re-check refuses pause regardless. `deliverAuthorized` → `/internal/v1/agent/revalidate` → `require_supported(action)`, and `SUPPORTED_AGENT_ACTIONS = frozenset({MONITOR})`. A perfectly-minted envelope yields 202 then a journaled `rejected`. | `src/agentauth/policy.py`, `src/agentauth/revalidation.py`, `control-state.ts` |
| 4 | Verification keys are not provisioned by default. Envelope keys exist only when `agent_authority_enabled` is true, default **false**; with no keys the listener returns `not_configured` → 403. The revalidation client also hard-requires `ADP_AGENT_AUTHORITY_ENABLED=true`. | `webhook-ingress/infra/agent-authority-bootstrap.tf`, `variables.tf`, `control-revalidation.ts` |

Blockers 3 and 4 mean pause cannot work end-to-end in a **default** deployment
even with blockers 1 and 2 solved.

## Why the obvious workarounds are wrong

**Fabricate a grant for the dashboard user.** Forbidden in terms, twice.
`docs/design/agent-delegated-authority.md` Decision 2: *"Another human initiation
path must add a `kind` with an equivalent verified record rather than loosen this
one."* And `grants.py`: *"Nothing here is ever built from request data — a
caller-constructible grant is not a grant."* There is also nothing real to cite:
`orchestration_decisions` is the only verified human-authority table, its
`flow_id` is `NOT NULL` with an FK to `orchestration_flows` (an ordinary webhook
run has no flow), it is append-only, and every `actor_kind=HUMAN` writer is a
gate/plan/node decision — none is "a human asked to pause run X." The invocation
row is explicitly rejected as authority and is worker-writable.

**Enforce the envelope only when the header is present.** Unsound: an attacker
omits the header and is admitted unconditionally. Strictly worse than either
extreme.

**Distinguish the human gateway by some marker.** Structurally impossible. The
listener sees one bearer token; the human gateway and an attacker holding a stolen
token present byte-identical requests. Any marker the gateway can add, a
token-holder can add too — there is no keyed material on the human path, because
the worker deliberately holds public keys only.

**Drop envelope enforcement for supported verbs.** This loses a real, documented
property: cross-run control by a compromised worker. The worker role holds
unconditioned `dynamodb:UpdateItem` on the webhook-events table, whose own
comment concedes *"any worker may write any run's row… No condition key fixes it:
the row key is data."* `envelope.py` states the read direction: a compromised
worker that reads another run's row learns that run's token. The NetworkPolicy
backstop is not enforced by default (`enable_network_policy_controller` defaults
false, and evaluation #3967 W1-04 measured a non-gateway caller still getting HTTP
200). So in a default deployment the token is the only barrier and it is
obtainable — and worker-to-worker denial is an explicit AC8 requirement.

## Proposal: `human_session` as a second authority kind (ADR-1 change)

The envelope currently conflates two claims:

- **(a)** the trusted gateway sent this command;
- **(b)** a delegated grant authorized it.

Only **(b)** needs a grant. Only **(a)** defeats the stolen-token attacker and the
compromised worker — and **(a)** is the entire property that removing the gate
would lose.

Add a `human_session` envelope variant carrying `principal` = the authenticated
user and a signed `authority_kind` claim, with **no** `grant_id` /
`revocation_epoch` rather than faked ones. This preserves (a) exactly, fabricates
nothing, and stays inside Decision 2's letter: Decision 2 governs *delegated
agent* authority derived from a past human act, whereas a logged-in user who owns
the run **is** the human, directly authenticated (tenant AND human owner).
Revocation stays bounded because the session is re-checked per request and the
envelope lives 30 seconds.

Cost, stated honestly: `grant_id`/`revocation_epoch` become conditional in
`_REQUIRED_CLAIMS` in **both** verifiers (Python `envelope.py` and TypeScript
`control-envelope.ts`), plus new shared negative vectors. The variant must be
keyed on the signed `authority_kind` claim so a *grant-path* envelope can never
omit its epoch — otherwise this becomes the hole it is meant to avoid. Blocker 3
additionally requires a decision about `SUPPORTED_AGENT_ACTIONS`, and blocker 4 a
decision about whether pause requires `agent_authority_enabled`.

That is a change to a verifier #5028 hardened deliberately, spanning two
languages, an IAM/Terraform default and a second action allowlist. It is its own
story, not a line in #3961.

## What #3961 does about it

- The barrier, gate, adapter wiring and worker runtime are built and unit-proven;
  that is what the story is actually about and it is unaffected.
- The gap is **executable**, not just asserted: `control-listener.test.ts`
  pins supported-verb + no-envelope → 403 with nothing journaled, in
  `describe('envelope enforcement boundaries')`.
- The gate is **not** weakened. Every option that would let the dashboard through
  today either admits stolen tokens or fabricates authority the design forbids.
- Per the story's stop condition, pause capability must not be advertised as
  accepted on the strength of unit tests alone.

## Decision required from the EPIC owner (#3959)

This is not a question #3961 can answer by itself, because either answer changes
the story's acceptance set. Recorded here as an explicit either/or so the choice
is made by the owner rather than absorbed into a green story.

**What is settled, and is not up for decision.** The barrier meets its contract.
That is measured, not asserted: two consecutive live-SDK runs against a real model
(`data/experiments/3961-pause-live-sdk-run1.json`, `run2.json`, 3/3 experiments
each) show a tool parked at the `PreToolUse` boundary with `new_admissions: 0`,
`fixture_writes: 0` and `confirmed.active_tool_count: 0` sampled *during* the
hold, the parked call completing after resume, session and attempt identity
unchanged, `interrupt_called: false` and `initial_prompt_replayed: false`. Unit
coverage on the new control modules is above the story's ≥85% line/branch bar.
AC-P1/P2/P3/P5/P6 are satisfied *at the barrier*.

**What is blocked.** Advertising the verb — the four surfaces in the table below —
because a supported verb becomes envelope-gated by that fact alone and the human
dashboard path has nothing to put in the header. Four blockers, above; two survive
even if the gateway learns to sign.

| Surface | Current state |
|---|---|
| `modules/gateway/src/activity/control_service.py` | `SUPPORTED_ACTIONS = frozenset()` |
| `modules/agent-factory/agent/src/control-runtime.ts` | `IMPLEMENTED_CONTROL_VERBS = new Set()` |
| `.github/workflows/agent-control-ci.yml` (Python gate) | `EXPECTED_SUPPORTED: set[str] = set()` |
| `.github/workflows/agent-control-ci.yml` (TS gate) | `EXPECTED_SUPPORTED = new Set<string>()` |

All four must move together or not at all — that is what the two CI gates are for.
A one-sided widening ships a Pause button whose handler answers 501.

### Option A — add `human_session` as a dependency of S2

File the authority variant above as its own story, block S2 on it, and enable the
four surfaces once it lands.

- **Cost:** conditional `_REQUIRED_CLAIMS` in both verifiers (`envelope.py` and
  `control-envelope.ts`) keyed on a signed `authority_kind`, new shared negative
  vectors, plus a decision on `SUPPORTED_AGENT_ACTIONS` (blocker 3) and on whether
  pause requires `agent_authority_enabled` (blocker 4).
- **Risk:** touches a verifier #5028 hardened deliberately, across two languages
  and a Terraform default. Getting the `authority_kind` keying wrong reintroduces
  the grantless-envelope hole the variant exists to avoid.
- **Consequence for S2:** stays open, no operator-visible pause until the
  dependency ships.

### Option B — re-scope S2 to the barrier, move enablement to a successor

S2 lands as explicitly-partial infrastructure: the barrier, gate, adapter, worker
runtime and evaluator predicates, with the verb off and the gap pinned by a test.
AC-P1/P2/P3/P5/P6 are recorded as *proven at the barrier*, and "operator can pause
a run" moves to a successor story that owns both the authority variant and the
four-surface flip.

- **Cost:** the EPIC carries a built-but-dark capability until the successor lands.
- **Risk:** a dark capability reads as done. Mitigation is already in the tree —
  the two CI gates fail if any surface is widened alone, and
  `control-listener.test.ts` (`describe('envelope enforcement boundaries')`) pins
  supported-verb + no-envelope → 403 with nothing journaled, so the gap is
  executable rather than a comment.
- **Consequence for S2:** mergeable now, with its acceptance set stated honestly.

### What this story does *not* claim

S2 is **not** declared accepted here under either option. Live acceptance for
AC-P1/P2/P3/P5/P6 belongs to evaluation #3968, whose W2-03/04/05 predicates this
story implements (`platform/scripts/agent-control-eval.py`) and which cannot pass
while `capabilities.pause` is false. The recommendation from the implementation
side is **Option B** — it makes the tree's actual state and the story's recorded
state agree, and it does not weaken a gate to do it — but the choice is the EPIC
owner's, and either is a legitimate answer.

## References

- `docs/design/agent-delegated-authority.md` — #5028 design, Decision 2
- `docs/runbooks/agent-control-evaluation.md` — wave-2 checks W2-03/04/05
- `docs/runbooks/network-policy-enforcement.md` — the unenforced backstop
- `modules/agent-factory/agent/src/control-listener.ts` — `requiresEnvelope`
- `.github/workflows/agent-control-ci.yml` — the two `EXPECTED_SUPPORTED` gates
