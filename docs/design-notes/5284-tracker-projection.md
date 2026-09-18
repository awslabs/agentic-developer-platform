# #5284 — Projecting execution state onto the EPIC tracker region

**Status:** implemented (gateway orchestration); live acceptance open under #5134
**Issue:** [#5284](https://github.com/aws-e/adp/issues/5284)
**EPIC:** #4191 · intent #4120 · delivery coordinator #5134
**Code:** `modules/gateway/src/orchestration/tracker_projection.py`,
`tracker_provider.py`, wired in `tick_handler.py`
**Adjacent scope, deliberately not entered:** #5142 (durable execution ledger),
#5331 (CLI)

---

## The defect

An AI-DLC EPIC issue body carries a generated region fenced by
`<!-- aidlc-tracker:start -->` and `<!-- aidlc-tracker:end -->`. Everything inside is
machine-written; everything outside is the user's own text.

That region was only ever written by the **inception persona**, which writes it at its
own run start and again before it hands off. Nothing wrote it afterwards. Execution
then proceeds — stories dispatch, run, merge, release their dependents — and none of
it reached the issue, so the region showed the planning snapshot indefinitely.

Observed on 2026-09-16 (flow `a94cb68e`): the engine held seven stories `passed` and
one `running`, GitHub held the seven merged pull requests, and the region still read
"U1 just dispatched". A supervisor edited it by hand. That repaired one issue's text
and fixed nothing.

**The engine's state was correct throughout.** No transition was wrong and no story
was in the wrong state. The only missing thing was a step that copies engine state
out to GitHub — so this is a projection gap, and the fix stays a projection.

---

## Where it runs, and why that placement is the design

The tick already runs ordered passes inside **one transaction** and does all remote
IO **after** `session.commit()`. Projection follows that same split, and the split is
what makes the safety properties structural rather than aspirational:

| Phase | Function | What it may do |
|---|---|---|
| Inside the transaction | `run_tracker_projection_pass` | Read graph/plan/binding state, render text, resolve the installation id. No network. |
| After the commit | `flush_tracker_projections` | GitHub reads and writes under a target-scoped advisory lock held by a separate session. **Never raises.** |

Two consequences worth naming:

- **A provider failure cannot touch execution (AC3).** The flush runs after the
  transition transaction commits. Its separate lock transaction reads and writes no
  engine rows, so a failed GitHub call has no path back to a node, gate, or dispatch.
- **No HTTP happens inside the node-transition transaction.** The post-commit lock
  transaction holds only one advisory lock, not row locks, across the GitHub round
  trip.

The pass runs **last** among the tick's passes, so the snapshot reflects the dispatch
that same tick produced — the "dependent work started" event is the one a reader most
wants to see.

---

## One direction, and it is a security property

The engine writes the region; it never reads it back as input. Nothing in
`tracker_projection.py` returns a value any caller uses to decide a transition. The
only thing read from GitHub is the current body, used solely to locate the sentinels
and preserve the text around them.

If projection ever became an input, a human editing generated prose would be steering
execution, and the accepted plan would stop being the sole authority over what runs.

Related, and already true: body edits do not re-trigger dispatch. The webhook parser
handles `issues.opened`, `issues.labeled` and `issue_comment.created` — **not**
`issues.edited` — so the projection cannot summon an agent by writing.

---

## The four invariants

### Only the region is ever written (AC2)

`splice_region` replaces text between exactly one start and one end sentinel and
preserves everything else byte-for-byte. It refuses — returning a reason, never a
body — when the sentinels are missing, duplicated, or inverted:

| Condition | Outcome |
|---|---|
| Zero start or end sentinels | `region_missing`, no write |
| More than one of either | `region_duplicated`, no write |
| End before start | `region_malformed`, no write |

**There is deliberately no "append the region if missing" path.** Creating the region
is the inception persona's job. An engine that appended one would write a progress
block onto whatever issue it was pointed at, including one that is not a tracker.
Refusals are distinct values because they call for different responses: *missing* is
benign (never initialised), *duplicated* means two writers disagree and needs a human.

### A stale snapshot never overwrites a fresher one (AC2/AC3)

Each rendered region embeds
`<!-- aidlc-tracker-snapshot: v{version} w{watermark} -->`, holding the flow's
in-force accepted-plan version and a monotonic engine watermark. Before replacing,
the flush takes a PostgreSQL advisory lock derived from the repository and issue
number, then parses the marker already on the issue and declines to go backwards.

The region also embeds `<!-- aidlc-tracker-flow: {flow_id} -->`. Plan versions and
watermarks are ordered only within one flow, while the schema permits two approved
flows to name the same EPIC issue. Once the first execution-time writer binds a
legacy/persona region, a different flow is therefore refused rather than allowed to
stale-block or overwrite it. Clearing that marker is an explicit human handover;
silently choosing whichever flow writes last would not be a target binding.

Version leads the comparison, so an amended plan is never blocked by the old plan's
larger watermark — after an amendment the node set changes and the two watermarks are
not comparable.

An **absent** marker reads as older than anything. That is required, not incidental:
the inception persona emits no marker, so the first execution-time projection has to
be able to land on a persona-written region. Treating absence as
unknown-and-therefore-unsafe would reproduce the reported defect exactly.

This stores **no new column** — which is what keeps the story out of #5142's ledger
scope — and needs no clock. The advisory lock serializes the whole
read/compare/PATCH sequence across Lambda invocations. The marker still supplies the
ordering decision: after a newer writer releases the lock, an older writer reads its
marker and refuses. A marker check without that serialization is not a compare-and-
swap; both writers could read the old body and the older PATCH could land last.

#### The watermark's monotonicity is structural (review finding, PR #5337)

The watermark is the **count of that flow's append-only decision rows**, and the
reason it is that rather than something derived from current node states is worth
stating, because the first implementation got it wrong in a way the test suite could
not see.

This number gates every write: the flush refuses when the published snapshot is
greater. So a watermark that can *decrease* while the flow really moves forward makes
the engine permanently decline to publish real progress — and since a stale decline is
a correct outcome that leaves the tick green, no one is told. It reproduces this
story's own defect through the guard meant to prevent it.

The original watermark was a weighted sum over current node states (attempts, plus 2
per complete/gate node and 1 per running one). That decreased on three ordinary flows:

| Flow | Old watermark | Consequence |
|---|---|---|
| A human rejects a gate (`awaiting_gate` → `rejected_at_gate`) | 3 → 1 | The tracker keeps asking for a decision the human already made |
| Execution fails (`running` → `failed`) | 2 → 1 | A failure can never be published |
| An amendment supersedes a node | 6 → 3 | Post-amendment progress is refused |

`controls.py` is explicit that a gate answer does **not** increment `attempts`, so
nothing compensated. Verified as permanent, not transient: five subsequent ticks each
reported `written=0 stale=1 success=True`.

`orchestration_decisions` fixes this by construction. It is append-only, enforced three
ways in `models.py` (no repository update method, a `before_update` mapper hook, and a
statement-level `do_orm_execute` hook that catches the bulk `update()` the mapper event
cannot see), and every writer of `OrchestrationNode.state` appends a row in the same
transaction as the transition. A count of rows that can only be appended cannot go
backwards — no arithmetic for a future state to invalidate. It keeps every property
this section claims: no new column, no lock, no clock, and the same number from two
overlapping ticks.

Because the guard refuses only on a *strictly* greater snapshot, non-decreasing is
sufficient; no claim is made that every render strictly advances the watermark. Two
ticks with no decision between them render the same number and the second is skipped as
unchanged, which is what idempotence wants anyway.

Regression tests: `test_a_rejected_gate_does_not_lower_the_watermark`,
`test_a_failure_does_not_lower_the_watermark`,
`test_superseding_a_node_does_not_lower_the_watermark` and
`test_a_rejected_gate_actually_reaches_the_issue`. All four fail against the original
weighted sum. They pair each state change with the decision row the engine writes
beside it, because a test that sets `.state` alone models something the engine never
does — and that gap is why the original passed 113 tests.

### Idempotence falls out of purity (AC1)

`render_region` takes `observed_at` as a parameter instead of reading a clock, so its
output is a function of its inputs alone. An unchanged graph renders byte-identical
text, which compares equal to the body already on the issue, so the pass **skips the
write entirely**. An issue edited on every tick floods its watchers and buries real
changes in no-op revisions.

### The target is bound, never guessed (AC3)

| What | Resolved from | On ambiguity |
|---|---|---|
| Tenant | The flow's own `org_id` | — |
| Authorization | A `PLAN_ACCEPTED`/`PLAN_AMENDED`/`GATE_APPROVED` decision for this flow | Refuse (an unapproved flow is not published) |
| EPIC issue | `epic-<N>` in the node's graph address | Refuse (two EPICs → no basis to choose) |
| Installation | `resolve_installation_id` (shared helper) | Refuse (zero or >1) |
| Repository | `BG_ORCH_DISPATCH_REPO`, as `dispatch_pass` reads it | Refuse if unset |

Guessing an ambiguous EPIC would publish one EPIC's progress onto another's issue.
The token is minted per tenant, scoped to the one repository, with `issues` at the
least verb the call needs — `read` on the common path, `write` only when something
actually changed.

#### The write target must be authorized, not merely named (review finding, PR #5337)

The authorization row above was added in review. Without it the answer to "which issue
does the engine write?" came entirely from author-supplied data: `epic_ref` is stored
verbatim by `compile.upsert_nodes` from segment 2 of the submitted node address, nothing
binds it to a server-side record of the flow's true EPIC, and the repository is one
process-wide variable shared by every tenant in the process.

`Permission.PLAN_DRAFT` reaches every ordinary member — `admin/config.py` maps `member`,
`user` **and `viewer`** to `AdminRole.MEMBER` — and draft registration still compiles
nodes and records an accepted-plan row, so a draft was a fully projectable flow. A
low-privileged user holding no GitHub credential of their own could therefore have the
engine PATCH another team's EPIC tracker region under its own bot identity, with rows
and a flow name they chose, on an issue an approver reads to answer a gate. A confused
deputy, and the docstring's claim that the target came from "the flow's own nodes rather
than from anything a caller supplied" did not hold, because the nodes carry caller data.

The fix requires the same human approval that arms dispatch, importing
`APPROVAL_DECISION_KINDS` from `genesis.py` rather than restating it — for the reason
that module gives, that a second copy of "what counts as acceptance" is free to drift.
`PLAN_DRAFTED` is deliberately absent from that set, which is exactly the distinction
needed. The authority to make the engine publish about an EPIC is now the authority that
decides what runs there. Filtered on `org_id` in SQL as well as `flow_id`, matching
`dispatch_pass._latest_approval_decision_id`, so a flow id from another tenant resolves
to nothing.

Refused, not failed: an unapproved flow is an ordinary state, and reddening it would
page someone for every draft in the system.

Regression tests: `test_an_unapproved_draft_is_never_projected`,
`test_a_draft_decision_alone_does_not_authorize_a_projection`,
`test_an_approval_in_another_tenant_does_not_authorize_this_flow`,
`test_a_gate_approval_authorizes_a_projection` (the discriminating half — without it a
guard that refused everything would pass all the others) and, through the real tick,
`test_the_tick_never_projects_an_unapproved_flow`.

### Author-supplied text cannot forge the region's own structure (review finding, PR #5337)

Every string interpolated into the rendered region — a node's `title` and `issue_ref`,
the flow's `slug` — is author-supplied and stored verbatim. The original render escaped
only `|` in `title`, and nothing at all in `issue_ref`.

A title containing a literal `<!-- aidlc-tracker:end -->` therefore rendered inside the
region, the write landed once, and from then on the body carried two end sentinels — so
`splice_region` answered `region_duplicated` on **every subsequent tick**. That refusal
is deliberately not a failure, so the tick stayed green and nothing was surfaced: this
story's own invisible-staleness defect, reachable by anyone who can name a story. Worse
than the human-edit case, because the poison lives on the node row and re-renders even
after a supervisor repairs the issue body by hand; clearing it needs a plan amendment.
An unescaped newline was a second channel: it breaks out of the table row, which is how
a forged row claiming another story `✅ passed` with a PR number gets published under
the region's "written by the engine from its own records" attribution.

`_neutralized` replaces the `<` of **every** HTML comment opener with `(`, one character
for one, so it is length-preserving and composes with the row truncation in either
order. Same shape and same reason as `adapters/github_comments._neutralize_marker`,
which exists in this codebase for exactly this: caller text must not be able to forge a
machine-read marker. It matches every opener rather than only `aidlc-tracker` because a
narrower rule would have to agree with two different readers — `splice_region`'s literal
`count()` and `_SNAPSHOT_RE`, which accepts arbitrary whitespace after `<!--` — and a
neutralizer that disagrees with either is a hole shaped like the bug it closes. Nothing
legible is lost: an HTML comment renders as nothing, so no title can be relying on one
to say something to a reader.

`flow.slug` turned out **not** to be a channel — `address.ADDRESS_PATTERN` restricts it
to `[A-Za-z0-9][A-Za-z0-9._-]*` and every node address must repeat it — but it is
neutralized anyway rather than argued about in a comment, because that validation lives
in another module and is free to move.

Snapshot-marker forgery is blocked twice over, verified by mutation to be blocked by
either guard alone: the neutralizer stops the marker rendering, and `read_snapshot`
takes the *first* match while the engine emits its own marker as the region's first
line. `test_the_engines_own_snapshot_marker_is_the_one_read_back` is kept as the only
check on that second, implicit ordering guard, and its docstring states plainly that it
does not discriminate the neutralizer.

Regression tests: `test_a_forged_end_sentinel_in_a_title_cannot_duplicate_the_region`
(which asserts the *second* tick still writes, the part the counters hide),
`test_a_forged_start_sentinel_in_an_issue_ref_is_neutralized`,
`test_a_newline_in_a_title_cannot_forge_a_status_row`,
`test_the_engines_own_snapshot_marker_is_the_one_read_back` and
`test_neutralizing_leaves_ordinary_titles_byte_identical` — the last because a fix that
mangles ordinary prose gets reverted for a good reason.

A third candidate finding was investigated and **rejected**: that `read_snapshot`
searches the whole body rather than the region, letting someone with issue-write access
forge a high marker outside the sentinels and pin the tracker. Real mechanically, but no
privilege gain — the same actor can already freeze projection permanently and just as
invisibly by pasting one duplicate sentinel (`region_duplicated`, an equally silent
counter), and scoping the search to the region would not remove the capability, since
the region is human-editable too. Recorded here so it is not rediscovered as new.

---

## What it does not do

- **Does not assert merge.** There is no `merged` column on a PR binding; merge is
  live provider evidence (`MergeEvidence`) that this pass never reads. A node in
  `awaiting_merge` renders as *in review / awaiting merge verification*, and the
  engine's durable answer to "did this merge and was it accepted" remains the node
  reaching `passed`. Rendering "merged" from a binding's existence would claim a
  verification nobody performed — worse than being a tick behind.
- **Does not add a state mapping.** Reuses `display_state.ENGINE_TO_DISPLAY`, whose
  parity test already pins it against the frontend. A second copy is the drift that
  module exists to end.
- **Does not add a feature flag.** Reuses `FEATURE_ORCHESTRATION_ENGINE_ENABLED`: a
  separate flag would let this pass publish progress claims about an engine that is
  switched off, and would enter the three-place parity contract in
  `test_feature_flag_parity.py`.
- **Does not truncate silently.** Over the per-pass flow cap sets `capped`, logs, and
  splits capacity between the most recently active flows and a stable cross-tick
  rotation. The rotation matters for terminal flows: one omitted final transition
  will not advance again and must still receive a later reconciliation turn. Over
  the per-flow row cap, the rendered table states the overflow.

---

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `FEATURE_ORCHESTRATION_ENGINE_ENABLED` | off | Enabled **only** on the literal `true`; `1`, `yes` and malformed values are off. |
| `BG_ORCH_DISPATCH_REPO` | unset | Target repository. Unset ⇒ the pass reports itself disabled. |
| `ORCH_MAX_PROJECTIONS_PER_PASS` | 20 | Flows per pass. Malformed or `< 1` degrades to the default rather than raising. |

`from_env()` never raises: it runs on the tick path, where the alternative to a usable
config is a broken tick.

---

## Observability

`TickReport` carries `projection_flows_examined`, `projections_written`,
`projections_unchanged`, `projections_refused`, `projections_stale`,
`projections_failed`, `projection_errors`, `projections_capped` and
`projections_enabled`, in the summary log and as CloudWatch metrics (totals and
per-org).

`unchanged` is counted apart from `written` on purpose — it is the steady state on a
quiet flow, and folding the two together would make a pass that changed nothing
indistinguishable from one that could not write. `refused` and `stale` are **not**
failures: declining to go backwards and leaving an uninitialised issue alone are both
correct. Only `failed` and `errors` force a non-success report, because an update that
never arrived is the invisible staleness this story removes.

---

## Verification

Three files, 127 tests (113 as authored, plus 14 added in review — 4 for the watermark,
9 for the two security findings, 1 pinning the marker ordering):

| File | Tests | Boundary |
|---|---|---|
| `tests/orchestration/test_tracker_projection.py` | 68 | the pass, rendering, sentinel rules, authorization, the flush |
| `tests/orchestration/test_tracker_provider.py` | 37 | the GitHub adapter — HTTP shape, token scope, pre-request refusals |
| `tests/orchestration/test_tick_tracker_projection.py` | 22 | the real `tick_handler._run()` and the operator-facing summary |

Full module suite on the reviewed head: **2261 passed, 17 skipped** (baseline 2247/17,
so every added test is accounted for and nothing regressed).

Both test fixtures were corrected in review to seed the flow's `PLAN_ACCEPTED` decision
alongside its accepted-plan row, because that is what `compile_proposal` writes and a
fixture that seeded the plan alone modelled a flow the engine cannot hold. The two tests
that asserted the decisions table was *empty* now assert the pass **added** no decision
of its own, which is the property they were written for — an empty-table assertion would
have silently become a test of the fixture.

AC4 asks for the actual integration rather than the pass in isolation, which is what
the third file is for: the properties that matter most here are properties of the
*wiring*. It asserts the commit-before-write ordering directly (rather than arguing it
from placement), that the counters reach the summary the smoke check greps, and that an
undelivered projection turns `status` red while a *refused* one does not — a pair,
because a guard that reddened refusals too would page someone for every hand-run flow
and get tuned out.

The negative tests were checked against deliberately broken builds. Fifteen mutants,
each killed by the test written for it:

| Mutation | Killed by |
|---|---|
| Staleness guard removed | `test_stale_snapshot_is_refused` |
| Staleness comparison inverted | `test_older_snapshot_is_overwritten` (+3) |
| Idempotence check removed | `test_unchanged_render_writes_nothing` |
| Missing region appended, not refused | `test_a_missing_region_is_never_appended` (+4) |
| Provider failure not counted | `test_a_provider_read_failure_...` (+4) |
| Ambiguous EPIC guessed | `test_two_epics_in_one_flow_are_refused_not_guessed` |
| Superseded plan version used | `test_the_in_force_plan_is_named_not_the_highest_version` |
| Read token widened to `write` | `test_read_asks_for_read_and_write_asks_for_write` |
| Repository scoping dropped | same |
| Provider detail leaked into the error | `test_failures_never_carry_provider_detail` |
| Empty-body guard removed | `test_writing_an_empty_body_is_refused_...` |
| `type(...) is int` loosened to `isinstance` | same test, `True` case |
| Redirects followed | 5 adapter tests |
| Tick's projection guard removed | `test_projection_failure_does_not_discard_engine_work` |
| Source-level mutation check neutered | `test_the_check_would_notice` (+2) |

### A gap the integration tests found

Writing the third file exposed a real hole in the placement argument above. The pass
call was the **last statement inside `_run()`'s `try`**, and that `try`'s `except` does
`rollback()` and re-raises. So while a *provider* failure could not affect engine state
(the flush runs post-commit, holding no session), an unforeseen raise anywhere in the
pass itself — the flow query, an outright bug — would have propagated into that handler
and discarded every correct transition the tick had just made. A display feature would
have been able to cost the engine its durable work.

The pass call is now wrapped in its own `try`, deliberately unlike every pass above it:
those are the engine's durable work and a failure there *must* surface, whereas
projection does not get to cost the tick anything. The asymmetry is commented at the
call site so it does not read as an oversight and get "tidied up".

This is worth recording as a method point: the structural argument ("post-commit flush,
no session, therefore isolated") was correct about the failure mode I had in mind and
silently incomplete about the one I hadn't. Writing the test that went through the real
entrypoint is what found the difference.

### A bug the tests found

`_project_one` computed the snapshot time as
`max(node.updated_at or node.created_at ...)`. Those columns do not agree on timezone
awareness: `created_at` is set by the Python-side `utcnow` default and stays aware in
the identity map, while `updated_at` is an `onupdate` whose awareness after a reload
is whatever the driver returns — aware on Postgres `TIMESTAMPTZ`, **naive on SQLite**.
The comparison raised `TypeError`, the pass caught it per-flow, and the flow was
counted as an error and skipped: a tracker silently stuck at kickoff, i.e. this
story's own defect reintroduced on the dev path. Now normalised through
`_observed_at`, which reads naive values as UTC (every writer here stores UTC) and is
pinned by a test that sets a naive `updated_at` directly.

---

## AC5 remains open

Stubbed tests establish code readiness. They are **not** evidence of live projection.
AC5 requires a real authorized flow advancing and its tracker updating with no
supervisor edit, which needs the coordinated deployment #5134 owns. That requirement
is tracked separately from code readiness and is not satisfied by anything in this
note.
