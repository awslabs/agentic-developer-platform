# #5329 — Propose, preview and approve additions with explicit graph placement

**Issue:** [#5329](https://github.com/aws-e/adp/issues/5329) (EPIC
[#4191](https://github.com/aws-e/adp/issues/4191))
**Builds on:** [#4529](https://github.com/aws-e/adp/issues/4529) pending-amendment
store, [#5331](https://github.com/aws-e/adp/issues/5331) `adp flow` foundation

#4529 built the two ends of an amendment — an author files an inert `PENDING`
draft, a named human accepts it and `amend_plan` writes the successor version.
What had no surface was the middle. A reviewer could not enumerate what was
waiting, read one, see what it would do to the graph, disagree with it, or decline
it. And nothing let a human say *where* new work goes.

This note records the resulting contract. It introduces **no second amendment
store and no second authoring coordinator**: every refusal rule still belongs to
the layer that enforces it.

---

## Placement resolves to addresses and edges

A placement request says what to add (`work`) and where it goes (`placement`), and
the server resolves it into concrete node addresses plus the exact incoming and
outgoing edges, each carrying the rule that produced it.

| `kind` | Resolution | Required |
|---|---|---|
| `insert_before` | Anchor's incoming predecessors → new work → anchor | `anchor` |
| `insert_after` | Anchor → new work; anchor's successors gain the new work as a prerequisite *in addition to* the anchor | `anchor` |
| `parallel_branch` | Anchor's predecessors → new work → the named rejoin | `anchor`, `rejoin_at` |
| `existing_wave` | Joins the named wave, defaulting to feed its single eval | `epic_ref`, `wave_ref` |
| `new_wave` | A new wave; both ends stated by `requires`/`precedes` | `epic_ref`, `wave_ref` |

**Wave membership is not a placement.** Waves are derived from the four-segment
address `flow/epic/wave/node` and are never stored; the engine releases work from
*edges*. A node given a wave label and no edges is a node that becomes eligible
immediately. So a placement that cannot be resolved into edges comes back as named
blockers with `document=None` — an unresolved prerequisite or a list of
alternatives — and cannot be previewed into an approvable document. Ambiguity is
surfaced before acceptance is possible rather than guessed one way and filed for a
human to rubber-stamp.

Resolution is strictly additive. **An existing dependency is never silently
removed** to make room for inserted work: dropping an edge is how insertion would
accidentally *release* downstream work early. `insert_after` therefore adds itself
to the successors' prerequisites rather than replacing the anchor in them.

`parallel_branch` requires an explicit `rejoin_at` because a branch with no rejoin
is not parallel work, it is an orphan: nothing downstream waits for it, so the plan
can complete with the branch unfinished.

---

## The preview is the whole change

`assess_amendment` asks `validate_proposal`, `amend.prerequisites_change_refused`,
`amend.definition_change_refusal` and `state.transition` exactly the questions
acceptance asks, and reports:

- **node dispositions** — added, removed and changed, each with its current state,
  attempt count and who concluded it
- **edge changes** — every added and removed edge
- **wave membership changes** — including waves that are new and waves emptied
- **proposed execution order** — staged, with mutually dependent waves reported as
  an unstageable stage (`stage: null`) rather than omitted
- **downstream impact** — only nodes whose prerequisite set moves, with
  `newly_blocked` flagging the direction that parks work behind something nobody
  scheduled
- **gate changes** — separately, because a removed gate is a removed *human*
  decision point and reads as an ordinary edit in a document diff
- **blocking diagnostics** — *all* of them. `amend_plan` raises on the first
  problem; a reviewer fixing one at a time would learn the next only by trying
  again.

`acceptable` is the single answer derived from `blocking`, and `is_noop`
distinguishes "this changes nothing" from "this is fine."

The preview computes nothing itself. A preview that derived its own answers would
drift from the accept that follows it, and a preview nobody can rely on is worse
than none.

---

## Approval binds the exact revision reviewed

Editing writes a **new revision with a new hash** and supersedes the earlier draft,
voiding any approval of the earlier text. An edit producing an identical document
writes no revision at all, so a no-op edit cannot churn the hash a reviewer is
holding.

An edit returning to the content of an already superseded or rejected revision is
refused without changing the current pending revision. Terminal revisions are never
reopened; request a fresh replan to propose that content again.

Acceptance carries `expected_proposal_hash`, `expected_base_plan_version` and
`expected_base_plan_hash`, all compared inside the transaction that writes the successor. The base version
and hash are the ones captured **at request time**, not at authoring time — the
window a concurrent edit exploits is the whole interval a human was thinking, not
just the authoring call.

Mismatches are refused as conflicts and **never rebased** — rebasing would apply a
document against a graph the approver never saw. The acceptance conflicts are
`draft_not_pending`, `revision_mismatch`, `base_version_mismatch`, `base_hash_mismatch`
and `stale_base`.

`draft_not_pending` is the one to expect. Accept checks the draft's **state before**
it compares the revision hash, and an edit marks the reviewed draft `superseded`
rather than mutating it — so "review revision 1, someone edits it, accept the id you
read" arrives as a state refusal naming the successor to read, not as
`revision_mismatch`. A client grouping the stale codes must include it.

Other invariants carried from #4529 and re-pinned here:

- **No agent self-approval.** Accept, reject *and* edit live on the `PLAN_APPROVE`
  router. `draft_routes.py`'s allowlist says it outright: no acceptance endpoint may
  ever be added to the `PLAN_DRAFT` router. Edit is on the approval side because an
  edit chooses what a human will be asked to approve.
- **Retries are idempotent.** Re-accepting the same draft at the same revision
  replays the recorded decision and reports `already_accepted`, rather than writing
  a second version.
- **Concurrent acceptance yields one successor.** The losers come back as
  `superseded_draft_ids`, so a reviewer holding one learns it is gone.
- **Nothing underneath is lost.** Completed work, attempts, evidence, assignments,
  budgets and answered human gates are preserved. Started-node guards reject changed
  incoming dependencies and execution-bearing criteria, inputs, evidence contracts,
  repository bindings or execution ownership. Presentation-only edits remain allowed.
  An incompatible amendment is refused, not applied by resetting progress; in-flight
  work must settle before crossing an ordinary plan-version boundary.

---

## HTTP surface

All eight are registered on `routes.py`'s operator router. Mutation and ordinary
operator readback require `Permission.PLAN_APPROVE`; an assignment-bound author may
list/show/preview only its own draft under `PLAN_DRAFT`. The prefix is
`/orchestration` — CloudFront strips `/api` before the ALB.

| Method | Path | Returns |
|---|---|---|
| POST | `/flows/{flow_id}/amendments/requests` | durable queued text-authoring request (202) |
| POST | `/flows/{flow_id}/amendments/proposals` | draft id, resolved edges, impact (201) |
| GET | `/flows/{flow_id}/amendments/drafts` | summaries, newest first, no documents |
| GET | `/flows/{flow_id}/amendments/drafts/{draft_id}` | one draft with its document |
| POST | `…/drafts/{draft_id}/preview` | the impact report; applies nothing |
| POST | `…/drafts/{draft_id}/edit` | the new revision plus its impact |
| POST | `…/drafts/{draft_id}/reject` | the declined draft |
| POST | `…/drafts/{draft_id}/accept` | the plan version produced |

REST acceptance requires all reviewed identities in the body; none has a default:

```json
{
  "expected_proposal_hash": "<64-character proposal hash>",
  "expected_base_plan_version": 3,
  "expected_base_plan_hash": "<64-character accepted-plan hash>",
  "reason": "Reviewed graph, inputs, evidence, and policy changes"
}
```

`list` omits documents deliberately: documents are whole plan proposals, and a page
of them is megabytes.

### API walkthrough

These examples are illustrative, not deployed-run evidence. They require Bash,
`curl`, `jq`, a compatible gateway and an authenticated human with `PLAN_APPROVE`.
Set `ADP_GATEWAY_URL` to the private API base URL (including `/api` when applicable),
`FLOW_ID` to the accepted flow and `ADP_ACCESS_TOKEN` from the existing login. Keep
these values private. No example authorizes deployment or live execution.

Save this as `placement-request.json`, replacing the fictional repository, issue and
node address with reviewed values. The issue must already exist in a repository
permitted by the flow; the anchor and affected downstream nodes must allow the added
dependencies. The example adopts checker development, not a read-only validation.

```json
{
  "request_text": "Add the existing cleanup-checker follow-up after implementation",
  "placement": {
    "work": [{
      "ref": "cleanup-checker",
      "kind": "story",
      "title": "Implement cleanup checker",
      "issue_ref": "201",
      "repository_identity": "example-org/example-repo",
      "evidence_expectations": ["verified-merge"]
    }],
    "placement": {"kind": "insert_after", "anchor": "demo/epic/wave/implementation"}
  }
}
```

Submit the inert proposal, list pending work and save its document and graph impact:

```bash
set -euo pipefail
umask 077
amendment_api() {
  curl --fail-with-body --silent --show-error \
    -H "Authorization: Bearer ${ADP_ACCESS_TOKEN}" \
    -H 'Content-Type: application/json' \
    "${ADP_GATEWAY_URL%/}/orchestration/flows/${FLOW_ID}/amendments${1}" "${@:2}"
}
amendment_api /proposals --request POST --data-binary @placement-request.json > proposed.json
DRAFT_ID=$(jq -er .draft_id proposed.json)
amendment_api /drafts > drafts.json
amendment_api "/drafts/${DRAFT_ID}" > draft.json
amendment_api "/drafts/${DRAFT_ID}/preview" --request POST > preview.json
jq '{proposal: .proposal_document, reason: "Review dependency placement"}' draft.json > revision.json
```

Review the saved graph impact. To edit placement, change the full proposal's `edges`
or work details in `revision.json`; retain unchanged nodes, identities, dependencies
and policy bounds. Submit it and review the returned revision afresh:

```bash
amendment_api "/drafts/${DRAFT_ID}/edit" --request POST --data-binary @revision.json > revised.json
DRAFT_ID=$(jq -er .draft_id revised.json)
amendment_api "/drafts/${DRAFT_ID}" > draft.json
amendment_api "/drafts/${DRAFT_ID}/preview" --request POST > preview.json
```

After reviewing that exact document and an acceptable preview, choose **either**
acceptance below **or** rejection. Acceptance copies all preconditions from the saved
reviewed draft, never from an implicit latest proposal:

```bash
jq '{expected_proposal_hash: .proposal_hash, expected_base_plan_version: .base_plan_version,
     expected_base_plan_hash: .base_plan_hash, reason: "Reviewed graph and execution impact"}' \
  draft.json > acceptance.json
amendment_api "/drafts/${DRAFT_ID}/accept" --request POST --data-binary @acceptance.json
```

Alternatively, decline the pending revision without applying it:

```bash
amendment_api "/drafts/${DRAFT_ID}/reject" --request POST --data '{"reason":"Placement needs rework"}'
```

A 409 conflict leaves the plan unchanged: read its message, refresh the draft and
preview, or request a fresh replan when its base moved. Do not replace the expected
hashes and automatically retry without review. A 422 identifies graph or execution
guards that must be resolved before acceptance. Creating/adopting an issue alone
never starts it; successful acceptance leaves scheduling to the normal engine.

### Refusal shape

Refusals carry `error`, `code` (the same value under both keys), `message` and, for
conflicts, `retryable`. `adp_common.Api` derives its `CliError` code from
`detail["error"]`; emitting only `code` would leave the CLI unable to name which
precondition moved, which is the whole point of binding one.

404s are coded too — `flow_not_found` vs. `draft_not_found` — so a mistyped draft
id is not reported to an operator as a deployment that needs upgrading. Absent and
another tenant's resources remain deliberately indistinguishable from each other;
the code separates *route* from *resource*, never tenant from tenant.

---

## CLI

`adp flow amendment propose|list|show|preview|edit|reject|accept`, with `--json` on
stdout and diagnostics on stderr. Two behaviours are load-bearing:

`propose --request-text TEXT` with no placement flags records a durable request on
#4529's existing bounded authoring queue. `propose --file REQUEST.json` submits the
shared multi-node placement/work contract directly. Neither mode applies graph state.

**`--yes` requires `--expect-revision`, `--expect-base-version`, and
`--expect-base-hash`,** refused before any request is sent. An unattended approval
naming no revision approves whatever is live — exactly the concurrent-edit hole this interaction closes. An
*empty* value (a command substitution that produced nothing) fails closed rather
than sending an unguarded accept.

**An unsupported gateway reports a capability gap and applies nothing.** There is
deliberately no fallback to `adp flow create`: applying a fresh plan is not a
degraded amendment, it is a different and destructive act against the work already
underneath the plan in force.

Stale acceptance exits **4** with the plan untouched and the conflict named. The CLI
also refuses to prompt for an accept whose preview is not `acceptable`, checked
before the confirmation rather than after.

---

## Two representation rules worth stating

**A read-only validation is an `eval`, not a story.** Represented as a code story it
would require a pull request that does not exist, and the flow would wait on
evidence that can never arrive.

**Eval cardinality is not relaxed to make room.** `validate_proposal`'s
`wave_eval_cardinality` rule gives a wave containing stories exactly one eval, and
an amendment adding a second is refused — by the preview *and* independently by
accept. A read-only validation therefore goes in its own wave, which a wave holding
only an eval is permitted to be.

**Accepted execution metadata is executable.** Repository routing uses the reviewed
repository name and its provider-issued immutable ID; a mismatch fails closed before a
claim or dispatch. Named inputs use `environment:`, `environment_connection:`,
`repository:`, or `node:` sources and hold readiness until the accepted policy or node
state resolves them. Story evidence is `verified-merge`; evaluation evidence is
`trusted-evaluation` or `trusted-live-observation`. Unsupported expectations are refused
rather than displayed and ignored. Declared evaluation criteria require matching
mandatory evidence checks, and input-node prerequisites participate in cycle detection.
Repository and workflow evidence that does not attest a live observation cannot claim
`trusted-live-observation`; a checker merge alone does not satisfy live validation.

**New work is published only at human acceptance.** Propose, edit and the agent
`register_amendment` path publish nothing: a new story/eval without an `issue_ref`
stays unbound in the draft, carrying a stable `proposal_identity` and an `owner/name`
`repository_identity`. The preview lists every such node under `issue_publications`
with its target repository and action (`create` = a NEW issue will be opened,
`pending` = an earlier attempt is reconciled, `reuse`/`update` = an issue is already
recorded). Publication blockers, reported by preview and accept alike:

- `missing_issue_binding` — no `proposal_identity` or no publishable repository;
- `amendment_issue_repository_not_permitted` — the repository is not in the in-force
  execution policy's `repository_ids` (and, when the proposal carries a policy, in that
  one too). A flow with no in-force policy permits no publication;
- `amendment_issue_limit_exceeded` — more than `MAX_ISSUES_PER_REQUEST` (20) new issues.

`POST .../accept` publishes in `publish_for_acceptance` only after the exact
proposal hash, base version and base hash match, the base is still in force, and the
full impact assessment has no blocker. Any mismatch publishes nothing and
`accept_amendment` raises the precise refusal. `accept_amendment` then re-runs the same
assessment under the flow lock with `require_published=True`, locks the issue bindings,
and requires each binding to match the reviewed content before `amend_plan`. Updates
use the same durable publication claim as creation: an in-progress or uncertain update
cannot be reused or accepted, even when its earlier content hash matches the request.
Only the current claim token can enter the external write or record completion.
Immediately before POST/PATCH, a live reservation becomes a durable, one-shot `writing`
intent. Expiry cannot transfer this intent: a suspended sender might still resume.
Each issue write carries an opaque attempt marker derived from, but not exposing, its
claim token. This is publication correlation, not an execution-evidence receipt.
After a lost response, retry once the settlement interval expires. Recovery reads the
known issue (or searches by identity after creation) and must observe that exact attempt
before trusting its content or starting another write. If it cannot, acceptance stays
blocked with `amendment_issue_write_uncertain`; an absent response or an elapsed lease
never authorizes reposting or clearing the claim. Recovery can remain blocked when a
process dies after recording intent but before sending and no provider result exists.
An explicit GitHub rejection of POST/PATCH with HTTP 401, 403, 404, 410, 422 or 429
confirms that the attempted write was refused. Only the matching `writing` attempt
returns to a retryable `pending` reservation, with its token invalidated. Once the
authorization, target, validation or rate-limit problem is resolved, the same exact
accept request can retry publication without duplicating the issue. Acceptance still
requires synchronized content; a rejected write never counts as published. Other HTTP
errors, timeouts and lost responses retain the uncertain-write protection described
above. This exception applies only to the write response, not a later recovery read.
The draft and base are rechecked under the flow lock before each publication claim,
preventing a superseded revision from overwriting work after a newer approval.
The GitHub-comment accept path does not publish; a draft
with unbound work accepted that way is refused with `missing_issue_binding` unless an
earlier REST accept already recorded the issues.

**Hash semantics.** The human approves `proposal_hash`, the hash of the stored draft
document **without** issue refs for work published at acceptance. The draft is never
rewritten. The applied document is that document plus `issue_ref` and
`provider_repository_id` for exactly the nodes the preview listed, read from the
binding table keyed by `(org_id, repository, proposal_identity)`. The same rows always
produce the same document. The accepted plan's `plan_hash` covers the bound refs.

**Publication is retry-safe.** `orchestration_amendment_issue_bindings` (migration
`085_amendment_issue_bindings`) is unique on `(org_id, repository, proposal_identity)`.
A `pending` row with a claim token is committed before the provider create call and
completed to `created` afterwards by compare-and-swap on that token. A retry that finds
a `created` row reuses it without listing anything. If the node's title or criteria
changed, the same issue is updated in place and the new content hash recorded; it
never fails as a mismatch and never opens a second issue. A racing publisher loses the
unique index and gets `amendment_issue_publication_in_progress`, which is retryable.
A `pending` row whose claim is older than the 5-minute lease (the claimant crashed) is
taken over by compare-and-swap. Recovery then matches on the body marker and the
publishing app identity only, listing from the row's own `created_at`. If the listing
cannot be exhausted, recovery refuses (`amendment_issue_listing_incomplete`) instead of
creating. Public issue content omits required-input sources and credentials, and
creating an issue neither approves nor dispatches anything.

**Accepting must not silently drop limits or strand running work.** An ordinary
amendment starts a new plan lineage. `plan_lineage` bridges only paused-wave
dependency receipts, so after acceptance:

- approved budget, retry, window and concurrency increase receipts no longer apply;
- the merge controller refuses in-flight delivery (`code_delivery_contract_changed`);
- the execution runner refuses in-flight work (`AUTHORITY_UNVERIFIABLE`).

The impact report therefore carries `effective_limits_before` and
`effective_limits_after`. Before is the in-force policy with every verified receipt
applied, via `policy_admission.load_in_force_policy`. After is the proposal's own
`execution_policy`, which is what the new version will enforce. Both are rendered by
the CLI (`EFFECTIVE LIMITS`) and the UI panel. Two families of blocking diagnostics are
computed inside `assess_amendment`, which runs for the preview and again inside
`accept_amendment` under the flow lock, so preview and accept cannot drift:

- `effective_limit_decrease`: any of `max_spend_usd`, `max_attempts_per_node`,
  `max_concurrent_actions`, `max_wall_clock_seconds` or `expires_at` would decrease.
  The message tells the author to state the limits explicitly in the proposal's
  `execution_policy`. Removing the policy entirely is not counted here: admission then
  refuses all dispatch, and `policy_after` shows the removal.
- In-flight work, from `shared_amendment.quiescence_failures`, the predicates behind
  `require_quiescence`, imported rather than restated:
  `active_nodes_cross_plan_boundary` (a node is running or awaiting merge, named in the
  message), `unfinished_execution`, `unsettled_action`,
  `unfinished_or_unverified_worker_report` and `unsettled_work_claim`. Queued or
  dispatched amendment authoring is excluded, because acceptance supersedes it.

---

## Delivery Flows UI

`GraphView` includes an amendment panel for all five placement modes, pending-draft
selection, whole-change preview, full-document revision, rejection, and exact-revision
acceptance. It renders structural, wave, downstream, gate, policy-authority, lifecycle,
and scheduling impact before enabling acceptance. Unsaved edits, failed current
readback, mismatched base version/hash and pending mutations keep acceptance disabled.

## Deployment validation

- **A live deployed run** — `5329/validation-04` and `5329/validation-08` are
  mandatory-live per `cli_live_contract.py` and remain outstanding. An authorized
  operator owns that qualification after compatible deployment and existing permission
  gates. Local database/provider fixtures support code-stage review, not live acceptance.
