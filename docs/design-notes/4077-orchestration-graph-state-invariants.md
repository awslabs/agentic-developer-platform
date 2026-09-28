# #4077 — Two state-model invariants to fix before the schema freezes

**Status:** requirements accepted as input to #4077 (no schema written here)
**Issue:** [#4182](https://github.com/aws-e/adp/issues/4182) — design input to
[#4077](https://github.com/aws-e/adp/issues/4077) (EPIC: materialize the delivery
loop as an explicit, durable orchestration graph)
**Parent synthesis:** #4174 → EPIC #1219
**Source:** `docs/research/orca-fit-assessment.md` §Q2 item 1, §Q4 items 1–2, and
its closing recommendation

This note carries **two requirements** into #4077's state model. It writes no
schema, builds no detector, and changes no running code. Its only job is to be
read before #4077's durable state vocabulary is fixed, because both requirements
cost a paragraph now and a migration plus an unrecoverable backfill later.

> **Why now, in one sentence.** The ORCA investigation's closing line is *"the
> highest-value single action in this document is not about ORCA at all: write
> the `unverifiable` state into #4077 before its schema freezes."* Once rows
> accumulate under a vocabulary that cannot say "we could not find out," the
> rows whose true state was indeterminate can no longer be classified
> retroactively — the information was never captured.

---

## Requirement 1 — indeterminate lifecycle states must be representable

**The rule: loss of contact is not evidence of exit.**

For every lifecycle edge the graph records but does not directly observe, the
value set must include an explicit indeterminate member. At minimum:

| Concept | Values | Rule |
|---|---|---|
| dispatch start | `started` / `not_started` / **`start_unknown`** | `start_unknown` may never be collapsed to `not_started` |
| dispatch stop | `stopped` / `running` / **`stop_unknown`** | `stop_unknown` may never be collapsed to `stopped` |

### Why the enum alone is insufficient

A schema can carry `start_unknown` and still produce the bug, if a consumer
reads it as "near enough to failed." So the following **consumer obligations**
are part of the requirement, not commentary on it:

1. **No consumer may treat an unknown value as terminal.** It is not a
   completion, not a failure, and not a reason to release the unit's slot.
2. **No consumer may dispatch new work for a unit whose prior dispatch is in an
   unknown state**, without first re-verifying that prior dispatch.
3. **A verification attempt that fails produces an unknown value.** It never
   produces a determinate one. A timeout, a transport error, a missing pod, an
   expired visibility window — none of these are observations of exit.
4. **Transitions out of an unknown state require positive evidence**, and the
   evidence source should be recorded alongside the transition (which
   observation, from which authority, at what time).

### Why the asymmetry is deliberate

The two possible errors are not equally expensive:

| Error | Cost |
|---|---|
| Recording a live run as stopped | The coordinator starts a second run against work that is still live: two agents on one issue, competing branches, a corrupted lineage chain. |
| Recording a stopped run as live | Wastes a slot; self-corrects on the next inspection. |

The vocabulary must be biased toward the cheap error. That is the entire content
of the rule.

### Prior art (two precedents, one external and one internal)

**External — ORCA.** Its `worker_dispatches.state` enum is
`('starting','ready','start_unknown','failed','succeeded','stopping','stop_unknown','stopped','abandoned')`.
Both transitions whose outcome can be genuinely unobservable get their **own
persisted state** rather than being collapsed into success or failure, and its
coordinator re-blocks drifted gated tasks rather than assuming them resolved.
See `docs/research/orca-fit-assessment.md` §Q4 item 2 for the file-level
citations. This is a worked example of a system that built essentially #4077's
model, hit both problems in this note, and solved both.

**Internal — ADP already does this once, and states the reasoning.** The
webhook-ingress gateway client returns three distinct states rather than
collapsing everything except success into `None`, and says so in the docstring
of `resolve_installation_by_id`
(`modules/agent-factory/webhook-ingress/lambda/common/gateway_client.py`):

> `{"state": "resolved", ...}` / `{"state": "not_found"}` — authoritative
> gateway 404 / `{"state": "error", "reason": ...}` — *we could not find out*
>
> callers must treat that as "unknown", never as "untrusted"

`not_found` is returned **only** for the one answer that authoritatively means
"not a known tenant"; every other outcome (missing config, non-404 status,
timeout, malformed body) is `error`. This requirement is therefore an
**established ADP pattern applied to a second surface**, not an import.

### Vocabulary must match the read side

The three-value read-side liveness verdict (`live` / `unverifiable` / `exited`)
is being added as a derived field on Agent Activity under **#4176**, from the
same source recommendation. The graph's durable states and that derived verdict
must use the same names with the same semantics, so the read side and the graph
do not diverge into two vocabularies that need translating. Concretely:
`unverifiable` on the read side and `*_unknown` in the durable model mean the
same thing — *no positive evidence either way* — and neither is a claim of exit.

---

## Requirement 2 — a human gate must never auto-resolve

**A unit in a human-gated state transitions out only on a recorded human
response**, and that response must carry an identity and a timestamp.

### The paths this explicitly covers

No code path may unblock a gated unit without a recorded response. The
non-obvious paths are the ones that break gates in practice, so they are named:

| Path | Required behaviour |
|---|---|
| **Process restart** re-deriving state | Re-block. A gate with no recorded response is still a gate. |
| **Reconciliation sweep** | Re-block. Repairing drift means restoring the block, never clearing it. |
| **Retry** | Re-block. A retry does not inherit an approval it never had. |
| **Timeout / expiry** | Re-block, and **deny** — never treat elapsed time as implicit approval. |

The deny-on-expiry semantics come from the `hitl-ticket` contract work (**#4178**)
rather than being re-invented here; #4077 should adopt that contract's
vocabulary for the timeout case.

### The invariant, stated so it can be tested

> For all reachable state transitions, no transition from *gated* to *ungated*
> exists whose precondition is not a recorded human response.

This is a property of the transition table, checkable statically over the
declared edges — not a runtime assertion and not a review guideline. The
requirement is that the model **cannot express** an auto-resolution, rather than
merely avoiding one by convention. If the transition table can express one, the
table is wrong.

ORCA states the same rule in its coordinator, and the reason, in one line: *"the
coordinator never auto-resolves gates (humans do) — that would defeat them as
approval checkpoints"*, followed by a repair loop whose comment is *"gate exists
but task isn't blocked — re-block to restore the invariant"*
(`orca-fit-assessment.md` §Q4 item 1).

### The existing gate is extended, never replaced

**ADP's current durable gate mechanism is the reference, and it is explicitly
retained.** Its shape:

1. The worker commits its state.
2. It posts a gate marker comment on the issue
   (`modules/agent-factory/agent/src/aidlc-gate-enforcer.ts` — deterministic
   commit + gate comment, issue #3231).
3. It **exits, holding zero compute** across the human round trip.
4. Only a fresh dispatch carrying a real human turn advances it — recorded as a
   synthetic `HUMAN_TURN` audit event derived from the answering comment's
   metadata, because there is no interactive human session
   (`modules/agent-factory/agent/src/aidlc-presence.ts`, issue #3232 / EPIC #3158).

That is a durable ticket answered out-of-band with no process parked on it, and
it is architecturally better than a live-connection pause for multi-day work.
**#4077's graph model must be able to represent it faithfully. If the model
cannot express this mechanism, the model is wrong — not the gate.** #4077 does
not redesign the gate, does not change its stage list, and does not introduce a
second gate mechanism with different semantics.

### One authz note, flagged early

The recorded human response carries an identity, and a consumer must check that
identity against an approver set. Approver sets are tenant-scoped. This is
called out here only so it is not discovered late — #4177/#4181-class work on
approver checking is where it lands, not this note.

---

## Test obligations transferred to #4077

Recorded here so they survive the handoff. Both belong in #4077's own test
suite, not in this issue:

1. A test asserting that **no state-machine path transitions a gated unit to
   ungated without a recorded human response** — enumerated over the declared
   transition table, so a newly added edge fails the test rather than silently
   passing.
2. A test asserting that **an unknown dispatch state is never treated as
   terminal by the coordinator** — specifically, that the coordinator does not
   dispatch new work for a unit whose prior dispatch is `start_unknown` or
   `stop_unknown` without a re-verification step.

---

## Explicit non-goals of this note

- No schema is written here.
- No detector, reaper, or reconciler is built.
- No change to the existing gate mechanism or its stage list.
- No change to #4077's scope beyond these two vocabulary/invariant requirements.
- No `docs/decisions/` ADR location is created; `contracts/README.md` points at
  such a path but it does not exist, and this note does not block on it.

## Completion criterion

The merge of this note is not the completion criterion. **#4077's schema, when
it lands, containing the indeterminate values and a transition table that cannot
express an auto-resolved gate** — that is the criterion. If it does not, this
note failed regardless of having merged.
