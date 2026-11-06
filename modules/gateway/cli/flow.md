# Following and controlling delivery flows with the ADP CLI

A *flow* is a unit of delivery the AI-DLC engine runs: a plan of stories, the
evaluations that validate them, the gates a human answers, and the dependencies
between them. Once you approve a plan the engine schedules eligible work itself —
you do not trigger each item.

```sh
adp flow list                       # your flows, worst news first
adp flow list --needs-me            # only the ones waiting on a decision from you
adp flow list --include-archived    # show live and archived flows; --status archived shows only archived
adp flow show FLOW_ID               # progress, blockers, outstanding gates, next eligible work
adp flow archive FLOW_ID --reason "Plan superseded by a correction"
adp flow unarchive FLOW_ID --reason "Resume review of this flow"
adp flow watch FLOW_ID              # follow it; Ctrl-C detaches, it does NOT cancel
adp flow plans FLOW_ID              # plan versions, and which revision is proposed
adp flow decisions FLOW_ID          # who approved what, in what role
adp flow cost FLOW_ID               # recorded spend
adp flow gate approve GATE_ID --expect-plan-hash HASH
adp flow gate reject  GATE_ID --reason "scope changed"
adp flow create --file plan.json    # submit a plan document you already have
adp flow amendment list FLOW_ID     # changes proposed to a plan already in force
```

The default list shows live flows only. `--include-archived` includes archived flows and their status count;
`--status archived` selects only archived flows without needing the include option.
Archive and unarchive require a human session with `PLAN_APPROVE` and a reason of 8–4000
characters. The API refuses archive while nodes or durable work remain active;
archiving is not cancellation. The slug remains reserved. Detail, decisions and
recorded cost remain readable, and unarchiving does not resume paused execution,
renew expired plans, or reset attempts. A refusal exits nonzero, including with
`--json`; check its error rather than assuming an interrupted request changed state.

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

### Revising an existing unapproved draft

Read the existing flow's version and hash with `adp flow plans FLOW_ID`. Prepare
an authored proposal for the same flow, omitting the server-inserted acceptance
gate, then preview it:

```sh
adp flow draft preview FLOW_ID --file proposal.json \
  --expect-plan-version 1 --expect-plan-hash BASE_HASH --json
adp flow draft save FLOW_ID --file proposal.json \
  --expect-plan-version 1 --expect-plan-hash BASE_HASH \
  --expect-proposal-hash PREVIEW_HASH --json
```

Use the preview's `proposal_hash` for `PREVIEW_HASH`, after reviewing its complete
effective proposal, proposed policy and node/edge changes. This hash binds the
entire draft, including descriptions; it is distinct from the execution plan
hash. Save appends a draft revision to the same flow, preserves its original
gate and pause setting, and retains removed nodes as superseded history.
Submitted execution policy remains proposed. Neither command approves a gate,
activates policy or dispatches work. Successful preview/save exits 0, even though
the flow remains unapproved; `--yes` is neither needed nor supported.

Saving requires the same base version/hash and reviewed file. A stale request
exits 4; read the plan again and preview the new revision. Retrying a successful
save with the same actor and exact bindings replays that revision without another
write. A gateway lacking this capability reports an upgrade requirement and
performs no fallback. These operations require a human account with `PLAN_APPROVE`.

Draft revisions are refused after any approval or execution history. They cannot
replace the original gate, reuse superseded node addresses, discard existing
proposed policy, or bind runtime evaluation specifications. Accepted plans use
the amendment path; implemented evaluators use evaluation acceptance.

### Creating a new flow

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

## Changing a plan that is already running

A plan in force has started work, recorded evidence and answered gates underneath
it. Changing it is an **amendment**, not a new plan — and the two are not
interchangeable: submitting a fresh plan over a running one discards that history.

```sh
adp flow amendment list FLOW_ID                  # what is waiting, newest first
adp flow amendment show FLOW_ID DRAFT_ID         # one draft, and the document it would apply
adp flow amendment preview FLOW_ID DRAFT_ID      # the whole proposed graph change; applies nothing
adp flow amendment edit FLOW_ID DRAFT_ID --file revised.json
adp flow amendment reject FLOW_ID DRAFT_ID --reason "not this sprint"
adp flow amendment accept FLOW_ID DRAFT_ID \
    --expect-revision HASH --expect-base-version N --expect-base-hash HASH --yes
```

The loop is **propose → preview → edit → approve → a new plan version**, and the
engine then schedules the eligible work in it.

With no placement flags and no `--file`, `--request-text` records a durable request
through the same attributed, bounded authoring queue as `replan:`. It does not apply
or approve a plan. Repeating the same text against the same accepted plan returns the
same request rather than commissioning a second author.

### Placement is addresses and edges, never a wave number

When you propose work you say *where it goes* relative to what is already there,
and ADP resolves that into concrete node addresses with explicit incoming and
outgoing edges:

```sh
adp flow amendment propose 7f3c2a10 \
    --request-text "add cleanup validation after U1"

adp flow amendment propose 7f3c2a10 \
    --ref migration-check --kind eval --title "Validate the migration" \
    --placement new_wave --epic-ref epic-1 --wave-ref wave-1b \
    --requires  demo-flow/epic-1/wave-1/eval \
    --precedes  demo-flow/epic-1/wave-2/deploy-gate \
    --request-text "validate the migration before the deploy gate"

# Or submit multiple development/validation nodes and explicit dependencies:
adp flow amendment propose 7f3c2a10 --file amendment-placement.json \
    --request-text "add the checker and its live validation"
```

| `--placement` | Where the work lands | Also needs |
|---|---|---|
| `insert_before` | Between the anchor's predecessors and the anchor | `--anchor` |
| `insert_after` | After the anchor; the anchor's successors wait for both | `--anchor` |
| `parallel_branch` | Alongside the anchor, rejoining where you say | `--anchor`, `--rejoin-at` |
| `existing_wave` | Joins a wave, defaulting to feed that wave's eval | `--epic-ref`, `--wave-ref` |
| `new_wave` | Forms a new wave; both ends explicit | `--epic-ref`, `--wave-ref` |

"Put it in Wave 1" is **not** a placement. The engine releases work from edges, and
wave membership is derived from the address — so a node carrying a wave label with
no edges is a node that runs *immediately*. That is why the response lists every
resolved edge with the rule that produced it, and why a placement whose edges
cannot be determined is refused with the ambiguity named rather than guessed one
way and filed for approval.

Resolution is strictly additive: **an existing dependency is never silently
removed** to make room for inserted work. Dropping an edge is how inserted work
would accidentally *release* downstream work early.

### Approval binds the exact revision you reviewed

Editing a draft creates a new revision and voids any approval of the earlier text:

```sh
adp flow amendment preview 7f3c2a10 draft-abc123   # prints the revision hash and base version
adp flow amendment accept  7f3c2a10 draft-abc123 \
    --expect-revision <hash from above> --expect-base-version <version from above> \
    --expect-base-hash <base hash from above> --yes
```

All three preconditions are sent with the request and compared **inside the same
transaction** that writes the new plan version. An amendment that was edited,
superseded, or written against a plan that has since moved is refused with the plan
untouched and exit **4**, naming which of the two moved:

| Code | Means |
|---|---|
| `draft_not_pending` | It was edited or displaced; the message names the revision to read |
| `revision_mismatch` | The draft's text is not what you reviewed |
| `base_version_mismatch` | The base version you named is not the draft's |
| `stale_base` | The plan it was written against is no longer in force |

The common case is the first one, because an edit writes a *new* draft and marks the
one you read `superseded` — so the id you typed is refused on its state before its
text is ever compared.

Do **not** retry these with the same hash, and do not script around them by
omitting the flags — that approves whatever is live. Re-read, preview again, and
approve what you actually reviewed. With `--yes` both flags are **required**, and
an empty value (a command substitution that produced nothing) fails closed rather
than approving unguarded.

Re-accepting the same draft with the same revision is safe: it replays the original
decision and reports `already_accepted`, rather than creating a second version.

### What the preview tells you

Blocking diagnostics come **first**, so an unapprovable amendment cannot be skimmed
past. Then the graph change — added, removed and changed nodes with their current
states and attempt counts, every edge change, wave membership including new and
emptied waves, and the proposed execution order with which waves run concurrently.
Then what it does to work that already exists: **newly blocked** downstream nodes
and the prerequisites now holding them, and gate changes called out separately
because a gate is where a *human* is required.

**EFFECTIVE LIMITS** shows the limits dispatch enforces now and after you accept. "Now"
includes any approved budget, retry, window or concurrency increase. Verified receipts
carry across content-only amendments without renewing their expiry. If an amendment
changes the original limits or execution authority, the preview warns which receipts
will be dropped and shows their before/after effective values and reason. The accept
response reports the committed disposition, including on replay. Re-apply dropped
increases explicitly if needed; the preview is not an approval and never reapproves
one automatically. Accept is also refused while work is in flight, because the new
plan would strand it: a node running or awaiting merge
(`active_nodes_cross_plan_boundary`), an unfinished execution or action, an unverified
worker report, or a held work claim. Wait for the work to settle, then preview again.

**ISSUES TO PUBLISH ON ACCEPT** lists new stories and evals that have no `issue_ref`
yet. For each one it shows the target repository and whether accepting will create a
**new** issue. Proposing, editing, or an agent registering a draft publishes nothing.
Issues are created only by `accept`, after the revision and base checks pass and every
other blocker is resolved. The repository must be in the flow's in-force policy
`repository_ids`, and one accept may create at most 20 issues. A retried accept reuses
the issue it recorded and never opens a second one. If you get
`amendment_issue_publication_in_progress` (exit 4), the accept was not applied: wait
and retry it.

Some proposals are refused on rules that exist for good reason. Adding an
evaluation to a wave that already has one is refused — a wave containing stories
has exactly one eval — so a read-only validation belongs in its own wave, as in the
`new_wave` example above. Note it is proposed as `--kind eval`, not as a story: a
read-only validation represented as a code story would require a pull request that
does not exist.

### Nothing falls back

The gateway must provide both pending amendments (#4529) and the amendment review
API (#5329). Deploy the compatible server before updating clients. `adp update`
installs the client served by the selected gateway; it does not enable missing
server routes. Compatibility is determined by the available API, not by the CLI
version string alone.

If a deployment's gateway does not serve the amendment API, every verb reports that
as a capability gap and exits 4 having changed nothing. It does **not** fall back to
`adp flow create`, because applying a fresh plan is not a degraded amendment — it is
a different and destructive act against the work already underneath the plan.

Approving an amendment needs **plan-approve**, including `edit`: an edit chooses what
a human will be asked to approve, so anything that could edit a draft could rewrite
it between a reviewer reading it and accepting it.

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

`--product PRODUCT_ID` selects an accessible Product for the session; selection
does not authorize publication or execution. For epic intake, `--repo` and
`--issue` locate the epic, while native children retain their own verified
repositories. Direct-story intake targets that issue's repository. The preview
keeps the Product, planning epic and qualified story targets distinct.

The request id is printed before sending and the session id before polling.
Retry an interrupted opening request with the same text, Product, repository,
issue and `--request-id`. A resumed answer can also use `--request-id`. The ingest
service claims the turn before dispatch; retries return the original acknowledgement.
A claim with an uncertain outcome is reported for inspection instead of being
sent a second time. Retry records are retained for seven days. Session storage
uses the existing chat service's 24-hour inactivity expiry; this is not permanent
conversation archival. Registered plans and decisions remain in the engine.

`--resume` restores the selected Product, repository and issue. Changing them on
a resumed session is refused. Repository access is resolved within the active
tenant; `--issue` reads that issue's current body through the tenant's GitHub installation.
A missing issue or mismatched engine dispatch target is a named prerequisite.

Without native epic children, the generator proposes one story per declared
outcome, a single wave and a human evaluation. Stories sharing an issue are
serialized. Its proposed policy permits development, review, repair and evaluation
in the selected story repositories, with a $5 total ceiling, one-hour runtime
ceiling, two attempts per node, one concurrent action and expiry 24 hours after
the conversation's last activity. It
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
refused rather than re-clocked, because the expiry is part of what was approved.
The gate endpoint returns HTTP 409 with `execution_policy_expired`; the CLI tells
operators to derive a new plan, review its new revision and policy, then approve
that exact revision.

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


### Wave display names and descriptions

Proposals can include `wave_metadata` alongside `nodes` and `edges`:

```json
"wave_metadata": [
  {
    "epic_ref": "epic-1",
    "wave_ref": "wave-1",
    "title": "API and persistence",
    "description": "Build durable task storage and authenticated APIs; evaluate authorization and storage."
  }
]
```

Metadata matches both refs, which must identify a wave in the proposal. Each pair
can occur once. Titles are 1–120 characters; descriptions, when supplied, are
1–500 characters. Blank strings are rejected. Names and descriptions appear in
wave previews, graph cards and flow summaries. Plans without metadata keep their
existing labels. AI-DLC emits this same format for new plans.

For an unapproved registered draft, use `draft preview` then `draft save` with the
full proposal to improve wording. Keep addresses and edges unchanged. Display
metadata is excluded from the execution plan hash but included in the draft
preview hash, so saving is bound to the exact wording reviewed. Saving does not
approve execution. Full replacement plans retain metadata for retained waves and
remove entries for waves no longer present.


Epic descriptions use an additional optional `epic_metadata` list:

```json
"epic_metadata": [
  {
    "epic_ref": "epic-1",
    "title": "External task invocation",
    "description": "External services need to submit work and retrieve results without a GitHub issue. Provide durable task acceptance, retained progress and follow-up input while preserving tenant isolation."
  }
]
```

Epic titles are 1–200 characters and descriptions are 1–3000 characters, both
nonblank. Use short paragraphs to explain the user need, what is being built,
why it matters and the key scope boundaries. The graph shows the description
directly below the epic heading. Both metadata lists share the same hash and
revision rules above. AI-DLC and the hosted intent-refinement model choose this
wording from the known intent and requirements; users review the generated plan
without filling in naming fields or answering a separate naming question.
