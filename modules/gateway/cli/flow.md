# Following and controlling delivery flows with the ADP CLI

A *flow* is a unit of delivery the AI-DLC engine runs: a plan of stories, the
evaluations that validate them, the gates a human answers, and the dependencies
between them. Once you approve a plan the engine schedules eligible work itself —
you do not trigger each item.

```sh
adp flow list                       # your flows, worst news first
adp flow list --needs-me            # only the ones waiting on a decision from you
adp flow show FLOW_ID               # progress, blockers, outstanding gates, next eligible work
adp flow watch FLOW_ID              # follow it; Ctrl-C detaches, it does NOT cancel
adp flow plans FLOW_ID              # plan versions, and which revision is proposed
adp flow decisions FLOW_ID          # who approved what, in what role
adp flow cost FLOW_ID               # recorded spend
adp flow gate approve GATE_ID --expect-plan-hash HASH
adp flow gate reject  GATE_ID --reason "scope changed"
adp flow create --file plan.json    # submit a plan document you already have
```

You supply no tokens. These commands reuse the session `adp login` already
established — there is no second credential store and nothing to paste.

## Output

Readable output is the default. `--json` prints a machine-readable envelope on
**stdout** with diagnostics and progress on **stderr**, so a pipeline can parse one
stream while a human reads the other.

`watch --json` emits one JSON object per poll, one per line, so it can be consumed
incrementally. A single array would only be parseable once the watch ended, which
defeats watching.

## Exiting a watch detaches; it does not cancel

Ctrl-C, a closed terminal and the poll limit all leave hosted execution exactly as
it was. Nothing is approved, cancelled, paused or restarted. Reattach any time:

```sh
adp flow watch FLOW_ID
```

This matters because a user who believes Ctrl-C stopped delivery will not go
looking for work that is still running, and may start or approve it a second time.

## Approving the revision you actually read

`adp flow plans FLOW_ID` prints the current proposal's hash. Pass it back:

```sh
adp flow plans 7f3c2a10
adp flow gate approve gate-node-1 --expect-plan-hash <hash from above>
```

The approval is refused — **without being sent** — if the plan has already moved
since you read it. Before approving, the CLI also shows which flow and gate you are
answering, so you are not approving a bare identifier.

**The binding is enforced by the server, not just by this client.** The hash travels
with the request as a precondition, and ADP compares it against the plan in force
*inside the same transaction that moves the gate*, holding the same flow row lock an
amendment takes. So an amendment cannot land in the gap between the check and the
approval: a revision that has moved is refused with the gate untouched, and the
attempt is recorded as a refusal decision. The client-side re-read is kept in front
of that only so an operator learns their plan has moved *before* being asked to
consent, rather than from a conflict afterwards. If the CLI cannot determine which
plan a gate belongs to, it **refuses** rather than approving unverified.

If your response is lost and you retry the same decision — same actor, same verb,
same revision — ADP replays the original decision rather than recording a second
approval or refusing you as already-answered.

`--expect-plan-hash` works on `gate reject` too. Rejecting a revision you did not
read misattributes a decision just as approving one does, so the server applies the
same precondition to both.

Approvals prompt before they authorize anything. With no terminal to ask on they
**refuse** rather than assuming an answer — pass `--yes` to state that intent in a
script. Agents cannot self-approve; an approval is recorded against the identity
that made it, and `adp flow decisions` shows the role and whether a human or a
service decided.

## Submitting a plan you already have

`adp flow create --file plan.json` is four steps, and only the last one arms anything:

1. **Dry run.** The document is sent to a preview that computes what registering it
   would produce and writes nothing at all. A plan that would be rejected is
   reported here — with every violation at once — before any row exists.
2. **Register inert.** The plan becomes a graph that cannot run: an acceptance gate
   dominates every root, and only a human answer moves it.
3. **Preview.** The effective graph is read *back from the server* and shown:
   the gates ADP inserted that your document never declared, which waves run
   concurrently, who may mark each node complete, and the policy bounds accepting
   would **grant**. If it cannot be read back, `create` **refuses** rather than
   accepting something it could not display.
4. **Accept.** A separate, revision-bound answer to that gate. Scripts must pass
   `--yes --expect-plan-hash HASH`, where HASH came from a previously reviewed
   preview. `--yes` alone returns the preview without registering or approving.

Without a terminal, `create --file plan.json --json` registers an inert draft and
returns its preview, gate id and hash with exit 4. A missing preview, or a hash
changed during registration, prevents acceptance.

A dropped connection is safe at every step: re-running registers no second flow
(ADP recognises the identical document) and re-accepting replays the original
decision rather than approving twice.

### What the preview tells you before you accept

The preview is the whole point of splitting registration from acceptance, so it
reports what a list of node titles cannot:

| Shown | Why it is not obvious from the document |
|---|---|
| Waves grouped by **stage** | Waves at the same stage have no dependency between them and run *concurrently*. Execution order comes from the dependency edges, never from the order you listed nodes in. |
| **Who may mark each node complete** | Three different mechanisms decide this: a gate is answered by a human, a story passes on a merged pull request, an evaluation is human *unless* the policy grants machine acceptance. Nodes that conclude without a human are named individually. |
| The **policy bounds accepting would grant** | Repositories, connections, autonomous actions, limits and expiry. |
| Gates **ADP inserted** | Your document never declared them; the acceptance gate dominates every root. |

A wave may report `stage unknown`. That means the plan's wave-level dependencies
form a cycle — possible even when no individual node depends on itself — so the
order genuinely is not determined. It is named rather than omitted.

### A plan that declares an execution policy

`create --file` submits it, and accepting it grants it. The document registers with
its bounds **retained but not in force**: ADP moves the declared policy into a
proposed field that no admission check reads, so the plan is reviewable while
authorizing nothing. Your answer to the acceptance gate, bound to that exact
revision, is what promotes it.

This is why the acceptance is a separate step. A plan sent straight to the
approved-plan endpoint records the approval in the same call that compiles the
graph, so you would be granting bounds you never saw.

Two things follow that are worth knowing:

- **Read the bounds in the preview, not in `adp flow show`.** `show` reports the
  policy *in force*, and for an unaccepted draft that is correctly nothing. The
  bounds you are being asked to grant appear in `create`'s preview.
- **A plan with no policy at all is `UNBOUNDED`, not restricted.** No repository
  restriction, no action allowlist, no spend ceiling, no expiry — legacy semantics.
  The preview says so in words, because an empty field reads as the opposite.

**Never strip a policy to make a plan submit.** There is no longer any reason to,
and doing it registers the same work without its limits — running unbounded while
the document claims it is constrained.

## Cost is three-valued

| Reported | Means |
|---|---|
| `known` | A recorded amount, printed as given |
| `none_incurred` | This genuinely spent nothing |
| `unknown` | Spend was not measured — **never** shown as `$0.00` |

"We did not measure this" and "this spent nothing" are different facts, and a
figure invented for the first is one a reader would act on. A rollup covering
nodes with unmeasured spend is labelled a **lower bound, not a total**, ahead of
the number.

## Starting a flow from an outcome

```bash
adp flow start --repo acme/api --issue 812
adp flow start "add per-tenant rate limiting" --repo acme/api --issue 812
adp flow start --resume SESSION_ID
adp flow start --resume SESSION_ID --answer "Only the public API" --request-id TURN_ID --json
adp flow start --resume SESSION_ID --plan --json
adp flow start --resume SESSION_ID --plan --yes --expect-plan-hash REVIEWED_HASH
```

In a terminal, `start` prompts for an outcome if omitted, runs hosted intent
refinement and continues to a proposed plan. `--refine-only` stops after the
conversation. Scripts provide the outcome as text, use `--plan` to request a
preview and receive unresolved questions as `pending` with exit 4.

The request id is printed before sending and the session id before polling.
Retry an interrupted opening request with the same text, repository, issue and
`--request-id`. A resumed answer can also use `--request-id`. The ingest service
claims the turn before dispatch; retries return the original acknowledgement.
A claim with an uncertain outcome is reported for inspection instead of being
sent a second time. Retry records are retained for seven days. Session storage
uses the existing chat service's 24-hour inactivity expiry; this is not permanent
conversation archival. Registered plans and decisions remain in the engine.

`--resume` restores the selected repository and issue. Changing them on a resumed
session is refused. Repository access is resolved within the active tenant;
`--issue` reads that issue's current body through the tenant's GitHub installation.
A missing issue or mismatched engine dispatch target is a named prerequisite.

The current generator proposes one story per declared outcome, a single wave and
a human evaluation. Stories sharing an issue are serialized. Its proposed policy
permits development, review, repair and evaluation in the selected repository,
with a $5 total ceiling, one-hour runtime ceiling, two attempts per node, one
concurrent action and expiry 24 hours after the conversation's last activity. It
grants no merge or deployment authority. These are proposed bounds, visible before
acceptance; use a prepared plan with `create --file` for different bounds or
execution inputs.

Measuring that expiry from the last turn rather than from session creation is what
makes a resumed conversation usable: a session opened days ago and continued today
proposes bounds valid from today, so the authority an approver reads is authority
that can still be exercised. Requesting a plan on a conversation that has been idle
close to the full 24 hours is refused (`planning_session_idle`) instead of producing
a grant that expires before it could be used — continue the conversation, then ask
for the plan again. An acceptance of bounds that have nonetheless already expired is
refused rather than re-clocked, because the expiry is part of what was approved;
derive a new plan and accept that.

This does **not** complete story #5331's full inception journey. Automatic issue
materialization, the canonical multi-stage inception gates, conversational graph
editing and generated executable evidence specifications remain outstanding.
Use `create --file` for a complete prepared engine plan, including its dependencies,
evaluation contracts, credential references and bounded policy. No deployed
CLI-to-hosted-planner-to-dispatch acceptance run is claimed by these code tests.

Planning requires the existing agent-factory intake Lambda, session/context tables
and hosted worker, plus the gateway's intake environment variables and IAM grants.
It does not require a chat UI. The engine must be enabled and its configured
repository/queue must match the proposed execution target. Install the server
operations before updating clients; an unavailable preview never falls back to
unreviewed acceptance.

### "Accepted" and "running" are different claims

After an acceptance — from `create` or from `start --plan` — ADP reads its own
execution ledger and tells you which of the two it can actually confirm:

| What you see | What it means |
|---|---|
| `the engine has taken the work up` | An execution record exists and has moved past admission, or an action is recorded as dispatched. Follow it with `watch`. |
| `WAITING on ...` | The engine took the work up and something is blocked — usually on you. Named explicitly so you do not wait on work that is waiting on you. |
| `has NOT yet been confirmed to take the work up` | Your acceptance is recorded and durable, but no execution record has appeared yet. Normal when admission is queued. **Do not re-approve** — watch it. |
| `execution ledger could not be read` | The acceptance stands; only the follow-up read failed. Nothing needs re-approving. |

The distinction is deliberate. The gate approval response says a *gate* moved; it
says nothing about dispatch. A CLI that announced "your plan is running" on the
strength of it would be wrong in exactly the deployments where it matters — where
the scheduler is not running, where admission refuses the work, or where the policy
denies it — and you would have no reason to doubt it.

Non-interactive callers never block. With `--json`, or when stdin is not a
terminal, a question awaiting your answer is reported as `pending` (exit 4) with
the question text and the exact `--resume` command to continue — nothing is lost
and nothing was executed. The same is true when the reply poll or the turn limit
is exhausted.

An unsupported deployment behaves the same way: if the orchestration engine or the
planning conversation is not enabled on this environment, the command reports that
and executes nothing, rather than failing in a way that looks like a missing flow.
Reads of a flow you do not own report *not found*, never *forbidden*.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Usage error, or you declined a confirmation — nothing was submitted |
| 2 | Not signed in (`adp login`) |
| 3 | Your role does not permit this operation |
| 4 | Pending, or the capability is unavailable on this deployment — nothing executed |
| 5 | The operation failed |
| 130 | Interrupted — detached only; hosted work continues |

Exit 4 covers three distinct planning outcomes, all resumable and none a failure:

| Situation | Reported |
|---|---|
| The agent is still working when the reply poll is exhausted | `pending`, with the session id and the `--resume` command |
| A question is awaiting your answer and the caller is not interactive | `pending`, with the question text and the `--resume` command |
| The turn limit for one invocation is reached | `pending`, with the `--resume` command |
| Planning is not enabled on this deployment | `unavailable` (exit 5) — configure it; retrying will not help |

`--resume` with a session id continues that conversation; bare `--resume`
continues your most recent one. If there is nothing to resume it is a usage error
(exit 1) — it never silently starts a new conversation, because answers typed into
a fresh session are answers to questions nobody asked. Detaching (Ctrl-C, or EOF on
stdin) leaves the hosted session running; exit 130 means *you* stopped watching,
not that anything was cancelled.

Reads need usage-read; planning and inert registration need plan-draft;
approvals need plan-approve. A flow belonging to another
organization reports *not found* rather than *forbidden*, so identifiers cannot be
enumerated.
