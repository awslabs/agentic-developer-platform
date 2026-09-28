# Orchestration engine glossary

Eight nouns carry most of the meaning in the orchestration engine's code, issues,
and pull requests: `flow`, `node`, `gate`, `wave`, `draft`, `tick`, `dispatch`,
`genesis`. This file defines each one in a sentence or two and names the module or
symbol that grounds it, so you can go from the word to the code in one hop.

**Audience: engineers.** This is *internal engine vocabulary*, not a source of
user-facing product copy. The user-facing surface has its own separate, closed
five-state vocabulary, and
[`design-contract.md`](../orchestration-graph-mockups/design-contract.md) §7.2
explicitly prohibits `node`, `edge`, `DAG`, `graph address`, and snake-case state
literals from anything a user reads — so mining these definitions for UI strings
would produce exactly the violation that section forbids.

Nor does this file discharge any user-facing-legibility obligation. Needing
knowledge of ADP internals to read the *graph* is a defect to fix in the
interface; a glossary does not and cannot close it. This is engineer onboarding,
nothing more.

The terms below are ordered by where they sit in the engine's lifecycle rather
than alphabetically, because each one leans on the one before it.

## `flow`

The top-level container of a delivery graph, and the one container that is a
stored row (`OrchestrationFlow` in `models.py`) — it has identity of its own
because it is the thing an operator names, addresses, and scopes queries by. A
flow fans out to EPICs and executes nothing itself; the work happens in its nodes.

## `node`

The executable unit and the graph floor (`OrchestrationNode`) — the level where
work actually happens, in exactly three kinds: story, eval, and gate (`NodeKind`).
Each node carries its own state plus its graph address (`flow/epic/wave/node`);
the container levels above it are not nodes.

## `gate`

One of the three node kinds (`NodeKind.GATE`): the point where a **human**
decision is required before any successor may proceed. The engine cannot satisfy
a gate itself — a gate is a decision someone has to make, not a status or flag
the engine can set on its own behalf.

## `wave`

A **derived** container: the group of nodes that share a `wave` segment in their
graph address, computed by rolling those member nodes up. There is no wave row, no
wave table, and no `WAVE` member of `NodeKind` — the enum's own docstring notes
that such a member "would invite exactly the container-rows-as-nodes mistake the
schema is shaped to prevent." That is also why a node's `wave_ref` is a plain
string rather than a foreign key: there is no container table for it to point at.

## `draft`

A compiled loop proposal registered as a graph a human can *see* — and nothing
else, i.e. inert (`registration.py`, `DecisionKind.PLAN_DRAFTED`). Registering a
draft starts, queues, and schedules nothing: `PLAN_DRAFTED` is deliberately
excluded from `genesis.APPROVAL_DECISION_KINDS`, so a draft cannot arm execution
no matter how many ticks run, and making it live is a separate human act.

## `tick`

The engine's heartbeat (`tick.py`): one invocation reads the durable state of
nodes that are `pending`, works out which of them have had all their predecessors
satisfied, promotes those to `ready`, and exits. It holds nothing in memory
between invocations — all continuity lives in the database — which is what makes
it safe to kill, retry, and overlap. **The tick performs no dispatch and runs no
work**; promoting a node to `ready` is where it stops.

## `dispatch`

The step after the tick, and **the first step with an outside effect**
(`dispatch.py`): it takes a `ready` node to `running` and records the run the work
will be attributed to. Everything before it is bookkeeping — this is where the
engine spends money. Dispatch is idempotent, so the same node dispatched twice
yields one run, and its authority comes from a server-resolved `genesis`, never
from the caller, the request envelope, or the pod's role.

## `genesis`

The human root behind an engine dispatch (`genesis.py`, ruling D-R12). The engine
is a service and cannot mint its own authority, so every dispatch traces back to a
real recorded human act — the SSO-attributed approver who accepted the plan —
resolved server-side from the decision row rather than named by the caller.

---

**See also:** [`docs/orchestration-issue-guide.md`](../orchestration-issue-guide.md)
for how to write an issue that drives a multi-story build,
[`ARCHITECTURE.md`](../../ARCHITECTURE.md)'s glossary for platform-level terms
(Harness, Tool, Job, HITL — none of the eight above), and
[`state.py`](../../modules/gateway/src/orchestration/state.py) for the single
declared node-state vocabulary and its transition table, which this file cites
rather than copies.
