# Product-grounded epic flows with story-bound repositories

> Delivery update (2026-10-21): the original 14 feature contracts are retained.
> Four operational stories add a dev-baseline checkpoint and deployment/integration
> reviews for stages 1–3; MR-14 remains final qualification. See the
> [staged delivery plan](../../delivery-flow/plans/product-epic-delivery-8394/README.md)
> for current sequencing, evidence ownership and the five human outcome reviews.
> This is a draft delivery amendment, not approval or a claim of live completion.
> [Parallelism review](../../delivery-flow/plans/product-epic-delivery-8394/parallelism-review.md):
> MR-12 and MR-13 now run together after stage 3; MR-14 joins both and verifies
> the combined candidate before final live qualification. All existing ACs and stage gates remain.

## Status and reading guide

**Updated following the Product/epic scope review; pending existing design
approval, not a claim of implemented behavior.** The earlier repository design
was architect-reviewed; this amendment does not invent an approval reference.
This document and the [implementation backlog](backlog.md)
answer [epic #8394](https://github.com/aws-e/adp/issues/8394) and its
[architecture amendment](https://github.com/aws-e/adp/issues/8394#issuecomment-6295848087).
No human approval of this revision is recorded. Use the existing review/acceptance
process; a later accepted revision and its approval reference must be synchronized
to every child. This document creates no new gate and grants no runtime authority.

A flow is grounded in **one stable Product ID** and usually delivers an epic.
A Product planning repository can hold that epic; create its child story issues
in the repositories where changes are required. Each story's issue and
implementation PR belong to **one verified repository**. The epic can therefore
span repositories through its stories. GitHub's native sub-issue links represent
that hierarchy; the engine's accepted graph controls execution and dependencies.

For example, an epic in `checkout-planning` has a story in `checkout-api` and
another in `checkout-web`; one Product-bound flow orchestrates both. The planning
repo is a convention, not the Product ID or an execution default. An integration
harness may live elsewhere again. No new Project selection, Product-to-repository
catalog, permission system or approval gate is required by this amendment.
The solution extends the
accepted-plan fields, transactional work claims, protected dispatch and durable
execution records already present, rather than replacing the engine.

Source baseline: main `d8e8a2a5c5e8e540f243e4aee77dcf54b3689ecc`, inspected
2026-10-20, newer than the epic's original `e032315b06f1` baseline. Source links
below are immutable. The [audit](audit.md) enumerates **all 11,543 tracked
artifacts**, maps components/exclusions, lists build/deployment entry points and
records evidence limits. Proposed interfaces below do not exist until their
owning child implements them. [Backlog sections](backlog.md) define all 14 medium
stories; no application implementation is included in this design PR.

## 1. Boundaries and architecture decisions

| Decision | Chosen contract and reason | Material alternative rejected |
|---|---|---|
| D0: Product and epic | Accepted flow `product_id` references the existing Product registry; existing typed epic/tracker references locate planning. Stories inherit Product through their flow. | Repository names and epic numbers are not Product IDs; Project integration and relationship versioning are not prerequisites for this change. |
| D1: identity | Accepted story `repository_identity` (verified owner/name) plus `provider_repository_id` (GitHub numeric ID); work key is tenant + provider + repository ID + normalized issue number. Existing claims already use this identity. | A flow execution repository cannot distinguish equal issue numbers and would make cross-repository flows impossible. Name-only keys break on rename/reuse. |
| D2: connection | Resolve the exact target against tenant-owned GitHub connections; record a non-secret connection selector in the accepted binding and freeze the concrete installation per execution assignment. | First installation, single-installation-per-tenant and worker-wide tokens either refuse valid work or enlarge authority. |
| D3: persistence | Accepted JSON remains authoritative; indexed node columns are rebuildable projections. Execution/PR/outbox records retain immutable attempt-specific assignments. | Making node columns authoritative silently changes accepted history; a second independent repository registry duplicates existing installation ownership. |
| D4: routing | Every producer and consumer uses a common binding resolver/reader. Preserve existing review and PR-binding checks that already use exact repository identity. | Removing repository checks wholesale would accept evidence from an unrelated PR with the same number. |
| D5: evidence | Add `multi-repository-evaluation/v1`, with a complete typed input set and separate harness/target identities. Keep all existing versions unchanged. | Relaxing v1's same-repository predicate changes the meaning of existing hashes/approvals. Treating workflow-evaluation/v1 as a generic runner exceeds its specialized contract. |
| D6: deployment | First version accepts one explicitly approved compatible environment contract per combined deployed evaluation; multiple repository deployments may contribute to it. | Arbitrary multi-environment orchestration and atomic cross-repository merges are outside this epic. |
| D7: migration | Expand, enable compatible readers, resolve legacy provenance, qualify, then retire global routing. Keep additive columns and old documents on rollback. | Filling missing identities from today's global setting would guess the owner of old work. Reverting to any old binary is not a safe rollback. |

The repository decisions were architect-reviewed; the Product/epic amendment is
reviewed here and awaits the existing design approval. No human approval is recorded.
Requester-designated owners, consumer catalog relationships and live destination/
access remain unresolved. They do not block architecture publication; they do
block dispatch or live qualification where the epic already requires them.

Related [#5287](https://github.com/aws-e/adp/issues/5287), under
[#4191](https://github.com/aws-e/adp/issues/4191), requests an additional use case:
a story's tracking issue in one repository and its implementation PR in another.
This epic does **not** implement that use case. A separate flow tracker is not a
substitute for cross-repository story/PR binding. Reuse connection/routing fixes
without changing #5287's parent, clearing access holds or claiming it completed.
Preserve #4191's engine-owned transitions and one-way tracker projection.

## 2. Verified starting point

| Existing mechanism | Observation and consequence |
|---|---|
| [ProposedNode][proposal], [same-issue rule][ordering] | Repository name/ID already exist, but duplicate-work grouping uses issue alone. Add repository-qualified grouping; do not create a second name field. |
| [Accepted reader][accepted], [node storage][nodes], [amendment contract][amend] | Accepted JSON is read by address; node rows lack repository query projections. Amendments already include repository fields in execution-contract comparison. Extend those paths and snapshots together. |
| [Initial dispatch][dispatch], [installation selection][installation-selection], [protected replay][replay] | Initial dispatch can replace `config.repo` with the accepted story name, but readiness and installation selection retain global/single-installation assumptions. Replay compares its saved source with current configuration. |
| [Claims][claims], [PR bindings][bindings] | Claims use tenant/repository ID/issue; PR bindings include repository ID, provider PR node ID, installation and head. Keep their collision/fencing checks. |
| [Review dispatch][review], [shared cycle][shared-cycle] | Significant review/repair paths already construct envelopes from the PR binding. Preserve them; repair remaining admission/recovery callers instead of replacing this machinery. |
| [Graph delegation][graph-dispatch], [commands][commands], [tracker][tracker] | Delegation checks configured repository; commands search bare issue references; tracker configuration is global. These are separate authority paths, not just UI fixes. |
| [Planning][planning], [UI amendment form][amendment-ui] | Planning returns the first name match across available installations. The UI labels owner/name input as an immutable ID. Both need precise per-story semantics. |
| [Repository evaluation][repository-evaluation], [runtime evaluation][runtime-evaluation] | Repository predecessors must equal the runner repository; deployed evaluation requires one repository among runtime receipts. These are explicit limitations, not evidence of multi-repository support. |
| [Correction child][correction-child] | Corrections create durable story nodes from evaluation actions, outside the ordinary proposal compilation path. Bind their authorization to accepted affected sources, not just new projection columns. |

## 3. Accepted identity and persistence (MR-01)

### Product binding on the flow

MR-01 adds optional `product_id` to `LoopProposal`/accepted JSON and a nullable
`product_id` projection on `orchestration_flows`, using the existing Product ID
shape. A new flow must have a valid, accessible Product before acceptance for
execution; drafts can be incomplete. Resolve against the authenticated tenant and
existing Product access/delegation mechanisms from [#8141](https://github.com/aws-e/adp/issues/8141).
Missing, unknown or inaccessible Product identity produces an existing-style
validation error, not a repository-derived default. Product access grants no
repository, cloud or approval authority; existing checks remain in force.

Commit Product binding with the accepted plan and flow projection. Preserve the
binding on resume and every amendment once any execution is reserved/started;
a different Product then requires a new flow. Product name/description edits do
not change this stable ID or invalidate valid work. Before execution, a Product
change uses normal preview/acceptance. Expose Product in flow creation, summaries
and readbacks. Stories, runs, reviews, corrections and evidence use their existing
flow linkage; pass `product_id` only where an existing consumer needs an explicit
value and reject disagreement with the owning flow. Do not duplicate it in every
table or introduce a new association-version framework.

Legacy accepted documents omit the new field and retain their exact hashes.
Legacy flows may remain without Product attribution under existing behavior;
missing Product alone does not suspend their work. MR-12 permits explicit audited
attribution as historical metadata without rewriting accepted bytes, moving an
assignment or treating it as new execution authority. Keep such attribution in
existing audit/decision metadata; it is not a replacement for an absent accepted
Product binding or its projection. New flows cannot use this
legacy exception. The Product registry/access contract is the concrete dependency;
completion of #8148 or Project-binding PR #7314 is not a prerequisite. Integrate
compatible shared-file changes while preserving any existing access enforcement.

### Story and work identity

Keep `issue_ref` as the existing bare or `#`-prefixed positive number; normalize it
for matching, not by rewriting historical JSON. Do not introduce URL parsing as
an alternative work identity. For new executable implementation stories require
server-verified `repository_identity` and `provider_repository_id` before
acceptance. For existing stories, verify the issue is in that same repository; issue in A
with implementation target B is refused. New story publication creates the issue
in its intended change repository. Drafts may be incomplete and must expose that
state. GitHub remains
the only provider for this epic. New interfaces say `tenant_id`; existing
Postgres `org_id` is its legacy storage synonym, not a new tenant identifier.

A **proposed additive** `repository_connection` field on `ProposedNode` carries
`{"provider":"github","installation_id":901}` (901 is fictional). It is a
non-secret selector, not authority. MR-02 populates it after verification,
including when only one usable connection exists. Multiple usable installations
require explicit reviewed selection; do not use list order. An absent new field
is omitted by the serializer, following the existing optional-field pattern.
Do not alter serialization of the existing name/ID fields, including legacy
nulls. Golden fixtures must assert exact accepted bytes and original hashes.

Within a proposal, group story/eval issue consumers by verified repository ID and
normalized issue. For unbound drafts retain conservative legacy diagnostics;
absence must not create a fake common repository. Distinct A#42/B#42 are
independent. Repeated work on A#42 must remain ordered through graph reachability,
even through an eval/gate. Runtime claims remain the cross-flow exclusivity
backstop. Different tenants never share a claim, query or resolver cache entry.

### Storage and atomic acceptance

MR-01 adds nullable `repository_identity` (String 255),
`provider_repository_id` (BigInteger) and `repository_connection` (JSON) projections
to `orchestration_nodes`, with positive-ID/all-or-none validation for new verified
bindings. Use a non-unique tenant/repository/issue lookup index: ordered follow-up
nodes on one issue are valid. Do not change the existing unique work-claim or PR
binding keys. The migration is additive, based on the actual merged Alembic head,
not a guessed numeric migration filename. The audit finds 135 version files and
head `122_product_handoff_callbacks`, with no missing predecessor revisions.

Compile, amendment preview, locked acceptance, repository insert/update, amendment
snapshots, progress projection and correction readers must agree. Acceptance
revalidates the reviewed target/connection, locks the flow, checks the preview
hash/version and writes accepted document, node projections and decision in the
same Postgres transaction. A changed identity/connection never silently replaces
the reviewed value: return a scoped conflict for a fresh preview. Network
verification can precede the lock; recheck the observed connection revision/
revocation inside admission and at privileged use. Access cannot be guaranteed
forever by a successful preview.

Untouched nodes may change target only in a newly accepted amendment. “Started”
includes a reserved execution, protected dispatch, active claim, PR binding or
pending external effect, not only `state == running`. Started repository/issue
retargeting is refused; use explicit supersession and a new node after outstanding
effects are reconciled. Preserve old plan hashes, attempts and decisions.
Projection mismatch is a typed hold, not a fallback to current configuration.

## 4. Repository connection resolution (MR-02)

Add one server-side resolver, proposed
`orchestration/repository_binding.py::resolve_story_repository`. It accepts
`tenant_id`, authenticated actor/authority context, accepted or proposed story
binding, intended operation and optional reviewed connection selector. It returns
verified provider/repository ID, observed canonical name, installation, ownership
observation and the accepted binding identity. It returns **no token** to UI/CLI.
The same resolver supplies acceptance, readiness and runtime admission; privileged
provider adapters request short-lived tokens separately.

Reuse [canonical installation ownership][ownership],
[ChannelTenantMap/revocation records][connection-storage] and the
[repository ID verifier][repository-verifier]. Do not union tenant-supplied
`Organization.github_installation_ids` into an authority grant. Attest ownership
when establishing bindings; enforce revocation and current ownership for each
privileged action. Enumerate only registered tenant candidates, use bounded
provider pagination and fail visibly on incomplete lookup rather than select the
first page. Unrelated connections do not affect a unique target match.

| Condition | Proposed typed result and behavior |
|---|---|
| Invalid owner/name or nonpositive/mismatched ID | `repository_invalid` / `repository_identity_mismatch`; no accepted binding or grant. |
| No connection, revoked access, wrong tenant, denied policy | `repository_unavailable`, `connection_revoked`, `connection_not_owned`, `repository_not_permitted`; no fallback. Avoid revealing another tenant's inventory. |
| More than one usable connection without reviewed selector | `repository_connection_ambiguous`; return only caller-visible candidate references, require explicit selection. |
| Provider timeout/rate limit/incomplete listing | `repository_verification_unavailable`; bounded retry using existing scheduling/deadlines, never “verified”. |
| Preview and acceptance/dispatch disagree | `repository_binding_changed`; preserve preview/assignment and require review or existing recovery path. |

Keep public execution-policy `repository_ids` name-based for compatibility; it is
an allowlist, not routing or a numeric-ID field. Check accepted policy against the
verified resource. Repository rename keeps the provider ID: retain original
accepted bytes, record the authenticated current name as an observation and route
only after policy/ownership checks. Name reuse with a different ID always fails.
Transfer or a changed installation is not an ordinary rename: stop for reviewed
reauthorization. A frozen in-flight assignment is never silently rebound; an
existing authorized recovery/new attempt may establish a new connection to the
same repository, recording the change and preserving old evidence.

No new credential store, Secrets Manager convention, AWS role or wildcard grant
is needed. The resolver runs in the gateway/tick, not by giving workers Postgres
access. GitHub tokens remain bounded by installation, one repository and operation
permissions; hosted scaledjobs and legacy ARC consumers keep their distinct
existing AWS roles. If implementation discovers a missing permission, specify the
exact resource/action in its PR; this design authorizes no IAM expansion.

## 5. Dispatch, review, reports and recovery (MR-03/MR-04)

Resolve from accepted story identity, then freeze a complete assignment before
publication: tenant, flow/node/address, accepted version/hash, execution/cycle,
invocation/attempt, provider repository ID, canonical routing name, installation,
issue, claim/generation, authority reference and applicable limits. Extend existing
SQL action detail/protected execution metadata; do not add a parallel work queue.
PR identity and reviewed head are added to the existing PR binding once known.
Queue URL and runtime wiring remain configuration; repository routing does not.
The saved flow reference retains Product attribution through dispatch/recovery and
reports. Consumers that carry Product explicitly validate it against that flow;
restart cannot substitute the planning repo or another Product.

The [engine grant writer][engine-grant] already emits a single-repository scope.
Preserve that boundary and the existing FIFO/deduplication and conditional-write
protocol. SQL and DynamoDB are not one transaction: retain protected-dispatch
reservation, SQL commit/outbox publication and conditional reconciliation. Never
claim exactly-once network delivery. The guarantee is one logical attempt and
one active claim despite at-least-once delivery.

| Interruption or hostile input | Durable behavior |
|---|---|
| Reserved/committed but not published | Scheduled recovery publishes the saved envelope only after current authority/limits and binding validation. |
| Published but acknowledgement lost | Reconcile invocation/protected record and replay the same operation/deduplication key; do not mint a second logical attempt. |
| Worker completed but report publication failed | Retain pending report/nonce/action and recover independently of worker lifetime. Completion is not inferred from missing reports. |
| Wrong repository/PR/head/plan/attempt in report | Refuse before state mutation, even if issue/PR numbers and check names match. |
| Pause, supersession, revoked access, exhausted limits | Keep the existing typed hold and pending inventory; never redirect or enlarge authority. |
| Global variable changes/disappears | Bound work is unchanged. Unresolved legacy work is explicitly held. |
| PR replaced or repair creates a new head | Replacement remains in the same repository; supersede old evidence and verify the exact current head. |

MR-03 owns initial dispatch, readiness, outbox replay, result observation and worker
source/grant compatibility. MR-04 owns binding registration/recovery, merge evidence,
review/repair/shared-cycle recovery, report re-dispatch and diagnosis. Both use the
same immutable assignment; review paths already using it are regression-tested,
not rewritten. A diagnosis is not authority to select a new repository. Preserve
required checks, branch protections, merge queues, accepted policy, human gates,
report signatures/nonces and finite deadlines/attempt/cost/concurrency bounds.

## 6. Coordination, commands and tracker targets (MR-05/MR-06)

Coordinator launch/tracking location is not child execution location. Graph
requests prefer a graph node ID/address; the server then verifies its
repository-qualified issue against the accepted graph. Legacy bare-issue requests
are usable only when the authorized target is unambiguous. Return an ambiguity
error rather than choose the first matching row. Coordinator delegation is the
intersection of accepted plan/policy and explicit parent delegation authority;
being launched from a repository does not grant access to all repositories in the
flow. Each child still receives only one target. Keep predecessor/gate and replay
checks. Wave/control snapshots and collisions include repository ID plus issue.

Add **proposed** hashed `origin_ref` and `tracker_refs` to `LoopProposal`:
`origin_ref` is one typed repository/connection/issue reference; `tracker_refs`
contains a flow or epic address plus such a reference, unique by addressed scope.
Each includes provider, immutable repository ID, verified name, installation
selector and positive issue number. They are separate from display-only epic
metadata, which is not write authority. Project them to nullable JSON on the flow
for lookup; accepted JSON owns meaning. MR-06 owns this additive migration and
serialization omission for legacy absence. No new issue or tracker is created
implicitly. An epic in planning repo C is a normal tracker/origin for stories in
A/B, but a flow need not have an epic. Product identity is stored separately as
section 3 specifies; the epic reference never becomes the Product ID.

MR-06 defines the verified parent reference used by MR-07. For native links,
this includes the provider-verified parent issue/node IDs alongside C's repository
ID, name, installation and issue number; missing issue identity refuses link lookup.
MR-07 performs the separate native link write and verifies child parentage. For an
epic-based intake, MR-07 reads bounded native GitHub sub-issues, resolving each
child's own repository and provider issue identity. An issue-body checklist is not proof of
parentage. Subsequent GitHub hierarchy edits do not automatically change an
accepted engine graph; normal amendment review still applies.

Verified webhook repository ID, installation and human identity select command
scope; comment text can name a graph address but cannot override event identity.
Check tenant membership and command permission as today. Distinct A#42/B#42 must
resolve independently; multiple eligible flows at the same qualified anchor
require explicit scope. Replan authoring uses the bound origin/tracker, not a
story default. Execution-only authority cannot write tracker C.

Tracker publication writes only approved references, renders each story's own
verified link, and remains a one-way projection. Keep existing bounded projection
retry/reconciliation. Persist target identity and operation/content revision so
an acknowledgement loss, restart or rename cannot move the write. Unresolved
legacy tracker references stay readable, but writes are held until explicitly
bound. Do not copy tracker text back into engine authority.

Tracker status belongs to a flow-level outbox: execution action rows require an
execution and cannot hold an epic-wide status write without inventing execution
scope or linking it to an unrelated child. Stage an immutable repository ID,
installation, issue, body region and operation key before commit; recheck the
accepted planning binding and repository identity before provider calls. Retain
unresolved or revoked operations for explicit reconciliation, but queue at most
one outstanding operation per flow and target. Cap each pass at 20 writes, reserve
half its capacity for new work while recovery rotates by last attempt, and delete
at most 20 completed operations older than 30 days per pass. This prevents stale
unresolved work from permanently monopolizing the pass or growing on each tick.
Apply the additive schema before enabling the new tick; an application rollback
keeps expanded columns and pending operations. A deliberate database downgrade
unwinds the new outbox and both planning columns and therefore is not a safe way
to preserve pending publication work in production.

## 7. Intake, CLI and UI contracts (MR-07/MR-08)

Retain `adp flow start --repo OWNER/NAME --issue NUMBER` and add Product selection
through `--product PRODUCT_ID` or existing selected Product context, forwarded to
the same server validation. For epic intake, repo/issue identify the epic: resolve
native children in their own repositories and never stamp the epic repo onto them.
For direct story intake, they identify that story's issue and implementation repo.
Any same-repo authoring convenience must expand only explicitly selected work
into verified per-story fields before preview; it is not a runtime flow constraint. Multi-target plan/amendment payloads carry the
existing per-node fields plus the verified connection selector. Resume preserves
session identity and targets; conflicting input requires a newly reviewed draft,
not an overwrite. Keep existing CLI exit-code/error envelopes. No automatic issue
creation, acceptance or worker dispatch is introduced. Product selection alone
does not create a planning repository or epic.

Existing flow creation/amendment routes remain the API surface. Return per-address
binding validation details through the existing validation/conflict response
pattern; distinguish malformed input, inaccessible issue, ambiguous connection,
stale preview and denied authority. Amendment issue publication resolves each
proposed story independently, uses existing durable publication bindings and
reconciles success-before-ack. MR-07 adds a partial unique index on
`(org_id, provider_repository_id, proposal_identity)` where the provider ID is
non-null, retaining the old name-keyed index for unresolved legacy rows. Backfill
only verified consistent IDs; conflicting legacy duplicates remain held until
reconciled before index activation. New publications require verified IDs and use
the numeric key, so rename cannot create a second issue.

For authorized epic story publication, MR-07 creates the issue in its change
repository and then links it to the selected epic with GitHub's native sub-issue
API. Resolve parent and child provider IDs using their separate repository
references. The current agent helper accepts one owner/repo for both IDs; it is
not evidence that the cross-repository gateway path already works. Extend the
existing gateway publication/provider path, reusing provider identity and durable
operation records rather than adding a new service.

Persist separate issue-created and parent-link-confirmed progress. After a lost
acknowledgement, read the actual parent and reconcile before retrying; do not
recreate the issue or silently replace an existing parent. Permission failure,
conflicting parent or incomplete listing remains a visible publication hold.
Use the authorized provider connection for the link operation; do not broaden
implementation worker grants. Do not call an epic child fully published or admit
it on the claimed hierarchy until native linkage is verified. Independent flows
without an epic keep their existing path. MR-11 reuses this publication/link
contract for a new correction story under an epic, retaining its source repo.

Graph responses add nullable accepted/proposed repository identity, numeric ID,
connection reference and binding status. Status values are proposed
`unbound`, `verified`, `denied`, `stale`, with a separate execution hold/cause;
verification is not a claim that all gates permit execution. Server derives them
from accepted/proposed state and current checks, never from client trust. Use
Delivery Flows story cards, plan summary/approval, amendment diff and node detail
to display Product, the epic reference when present, and each `owner/repo#issue`
**before** a dispatch exists. Product and repository selection use existing access
checks; no Project picker is introduced. Show old/new target as
an execution-scope change; show started-node refusal explicitly. Only verified
names produce links. Unresolved legacy flows display “repository unresolved,”
not fabricated global links. Preserve loading, errors, keyboard/accessibility
behavior; numeric provider IDs belong in details, not an owner/name input.

## 8. Versioned combined evidence (MR-09)

New family **`multi-repository-evaluation/v1`**, proposed at
`contracts/multi-repository-evaluation/v1/models.py`, is shared by producers,
gateway/tick validators and relevant worker runtimes. Its specification has a
`mode` of `repository` or `deployed`, not a permission to invoke arbitrary runners.
MR-09 defines and stages schemas/fixtures; MR-10 and MR-11 enable their respective
consumers. Unknown version or unavailable adapter yields a typed hold; never
fallback to weaker/manual-as-pass or an existing v1 adapter.

| Object | Required meaning |
|---|---|
| Accepted specification | Version, acceptance mode, criteria IDs, bounded maximum age, runner/harness identity and immutable harness revision/digest, exact direct-predecessor declarations, optional approved environment contract for deployed mode. |
| Each declared input | Accepted node address, provider repository ID, evidence kind (`merge` or `deployment`), required check identities (name + app + workflow when required), and applicable accepted-source/producer constraints. Connection selection uses the resolver, not a token in the document. |
| Each resolved source | Tenant/flow/node, accepted source plan/hash/version, execution/cycle/claim generation, source binding revision, provider repo ID and observed name, PR node ID/number and reviewed head + merge SHA, or deployment receipt/operation key and exact deployed revision. |
| Harness | Separate verified repository ID/name/connection, source revision, workflow definition revision or harness digest, adapter and permitted operation. It is not an implicit predecessor or a substitute for source authority. |
| Environment | Accepted connection/target-contract identity, runtime/service contract and deployment receipt set. Private endpoints and credentials remain private configuration. |
| Combined receipt | Eval execution/attempt, specification digest, canonical input-set digest, authenticated producer identity, observation time/expiry, evidence references/checks, per-criterion results and verdict. |

The canonical digest is SHA-256 over versioned, deterministic JSON (sorted object
keys, compact separators, UTF-8, no non-finite numbers), with inputs sorted by
unique accepted node address. Normalize documented set fields before hashing;
reject duplicates rather than deduplicate them. Include immutable repository IDs,
all exact revisions, binding/attempt lineage, check identities, harness pins and
environment contract identity. Keep credentials, transient tokens and display
labels outside the digest. Record alias observations separately. MR-09 publishes
literal golden vectors whose expected digests are computed independently of the
production serializer. Existing contract canonicalization/hashes remain unchanged.
Existing flow/node lineage supplies Product attribution for both sources and
verdicts; check ownership on readback. No new Product field is needed in the
combined evidence schema merely to repeat a value reachable through the flow.

Declared sources must exactly equal the accepted evaluation's required predecessor
set: no missing, extra or duplicate source. Check that source lineage is still
accepted using the existing unchanged-node lineage rules, rather than requiring
all source actions to share today's plan version. A changed predecessor revision,
claim generation, harness pin, target contract or criteria invalidates the old
combined receipt. An authenticated receipt alone does not authorize acceptance:
existing human versus machine mode and all gates remain controlling.

Bounds reuse existing maxima where applicable: at most 128 predecessor inputs,
64 checks per input, bounded provider pages/artifact size and existing action
attempt/deadline limits. No unbounded fan-out or provider polling. Missing proof
is blocked/failed as appropriate, never a successful empty result.

## 9. Repository and deployed evaluation, correction (MR-10/MR-11)

For repository mode, resolve A, B and harness H independently. Validate each
current PR binding/merge receipt and required checks against that repository's
GitHub metadata. Equal PR numbers or check names are insufficient: compare
repository ID, provider PR node ID, head/merge SHA, check app, workflow/run identity
and successful conclusion. Preserve required workflow source/definition pins.
Build a combined receipt only after the complete declared set is verified. Partial
reads may be retained under the operation key but must be rechecked for freshness
before acceptance. Provider failure never becomes a pass.

For deployed mode, replace the current same-repository check with exact typed
source membership, **not** removal of source checking. Require authenticated,
current deployment receipts for every runtime input and the single approved
compatible target contract. An API/UI integration may involve several services,
but their compatibility and environment identity must be explicitly bound. A
story's merged PR proves code completion only; runtime-dependent successors wait
on operations/deployment/evaluation nodes, not a bare merge. Keep environment
leases and deploy authority separate from GitHub source access. Integration probe
results must bind the same source set and target contract as deployment receipts.

Correction attribution names an accepted affected source address/repository.
Use the existing durable correction action/child linkage, adding an immutable
source authorization snapshot and digest. The generated correction node is
allowed only through that server-created linkage to accepted source authority;
ordinary caller-supplied node fields cannot manufacture it. Its issue, claim,
worker, PR and subsequent deployment stay in the affected repository. Product
attribution remains that of the source flow. When publishing a new epic child,
reuse MR-07's native-link reconciliation and retain the epic reference; do not
create the issue in the planning repo. Do not
copy the evaluation harness binding. Multiple possible affected sources, unknown
attribution or target expansion require the existing human review/amendment path,
with no dispatch while unresolved. No additional approval gate is invented for
already-authorized bounded corrections.

Reconcile correction issue creation before retrying; idempotency keys include the
evaluation action and affected source. When B changes, invalidate the whole prior
combined verdict and evaluate A plus new B at their exact current revisions.
Retain successful and failed attempts and enforce existing correction budgets,
window expiry, limits and human acceptance mode.

## 10. Legacy migration and config retirement (MR-12)

The [complete source inventory](audit.md) is delivered **now**; MR-12's separate
runtime inventory tool concerns live stored records, not deferred repository
research. Proposed operator interface: a dry-run-first gateway maintenance command
that emits private per-record classifications and reason codes, followed by an
explicit apply with expected version/hash. No invocation of that proposed command
is claimed here.

Legacy Product absence is independent of repository provenance: preserve it
unless an authorized explicit attribution is supplied. No bulk Product inference
from repository names, epic IDs or flow slugs is allowed.

| Stored state | Classification/action |
|---|---|
| Explicit accepted verified binding | Rebuild/check nullable projections without rewriting the document. |
| Started work with consistent protected dispatch/PR/claim provenance | Retain original assignment, verify provider identity/current authority, record migration provenance separately. Do not patch accepted bytes. |
| Untouched legacy work with proven target | Produce a reviewed amendment; apply only against its expected version. Re-running produces no new amendment/decision. |
| Missing, inconsistent or ambiguous provenance | `legacy_binding_unresolved`; show owner/action; no guessing from today's global setting or issue text. |
| Pending external publication | Preserve durable record and original target; reconcile before retirement or supersession. |
| Verified rename / transfer | Rename observes the same ID; transfer/connection change requires reauthorization as in section 4. |

Keep the global setting only as a temporary compatibility input for explicit
legacy classification; it is never sufficient authority for a new binding. Once
all required callers use accepted/assigned targets, remove normal readiness/
routing dependence and retire `BG_ORCH_DISPATCH_REPO`, Terraform
`orchestration_dispatch_repo`/tick `dispatch_repo`, persisted release-input keys,
ConfigMap injection, SSM readback/writers and upgrade preservation. Preserve
historical docs as historical evidence, not active operating instructions.

A compatibility release must reject unsupported multi-repository/evidence
capabilities before consuming effects. Existing old binaries do not acquire this
behavior by adding a version field. Therefore rollback before the compatibility
floor requires pausing/quiescing **all** relevant gateway and scheduled writers,
reconciling pending actions, and keeping them stopped; do not let an old tick run
against new work. Normal rollback is only to a tested compatible image with the
expanded schema retained. Downgrade is not a license to drop binding/history data.

## 11. Build, rollout and rollback

[Audit deployment map](audit.md#deployment-and-build-map) gives verified entry
points and trigger caveats. The gateway and scheduled tick use the same gateway
image; gateway rollout synchronizes the tick and other writers, so both are in the
compatibility boundary. Shared contract code is staged into the gateway Docker
context by `stage-contracts.sh`. Root-contract-only changes are not covered by the
current gateway deployment path filter: MR-09 must update trigger/build selection
and artifact self-checks, not assume a merged schema appears in running images.

Ordered implementation/rollout handoff:

1. MR-01 establishes optional Product/repository schema, serialization and resolver
   interfaces, using the existing Product identity/access contract; MR-02
   supplies verification. Install additive schema before serving readers that use
   it. Do not enable new multi-repository acceptance yet.
2. Land MR-03/04/05/06/07/08 and MR-09/10/11 in the backlog's dependency order.
   Keep unavailable evidence adapters fail-closed. Ship a compatible reader floor
   and contract self-checks before enabling new plans. Backport no routing guess.
3. Coordinator verifies backend/tick, frontend/CLI and worker/contract artifacts as
   a compatible set. Infra changes go through the manual infrastructure workflow;
   application deployment follows matching main push or explicit dispatch.
4. MR-12 dry-runs stored-record migration, resolves/holds legacy work, exercises
   rollback, removes global config from fresh-install and upgrade paths, and
   proves the offline lifecycle without it. Preserve pending-action inventory.
5. MR-13 builds the owned-fixture qualification in parallel with MR-12 after
   stage 3 acceptance, with exclusive harness/workflow ownership. MR-14 joins both
   reviewed merges and reruns the complete offline matrix and migration/config/
   rollback regressions on their combined candidate and its named operator deploys/verifies
   approved artifacts and runs the manual live qualification after existing target/
   access/budget approval. Record installed image digests, source revisions,
   migration head, contract versions and frontend/worker revisions.

No new production infrastructure or data store is proposed. Postgres retains
relational plans/claims/evidence; existing DynamoDB protected authority/outbox
records retain their current keys and TTL rules; existing artifact storage holds
bounded evidence. Do not store secrets in accepted plans, receipts or docs.

## 12. Qualification and acceptance

Extend the [existing runner][qualification-runner], fixture lifecycle and
manual workflow; do not create a second runner. MR-13's proposed scenario uses
one accessible Product, a planning epic in C with native children in owned
repositories A/B, separate verified connection paths, optional distinct harness H,
colliding issue/PR numbers and one flow with a cross-repository predecessor.
This scenario requires C to differ from A/B so it proves the agreed planning
pattern; ordinary flows do not require a dedicated planning repository. Repositories and exact provider IDs come from reviewed private config,
not this document's examples. Required configuration includes finite total spend,
per-attempt limits, concurrency, deadline, retention and fixture ownership.

| Epic criterion | Independent oracle and necessary wrong-result test |
|---|---|
| EC-01 | New flow accepts an accessible Product ID; missing/foreign/inaccessible Product is refused. Each story issue and PR use the same verified repo/connection; unrelated connections do not alter selection. Wrong-tenant, revoked, ambiguous or issue/target mismatches yield no grant. |
| EC-02 | A#42/B#42 and equal PR numbers remain distinct in proposal, claims, commands and waves; true same-repository unordered work and cross-target evidence are rejected. |
| EC-03 | A Product-bound epic flow completes both PR lifecycles with stable flow/Product attribution, gates and restart continuity. Publication/report faults cannot duplicate attempts or move story targets. |
| EC-04 | UI/CLI show Product, epic C and A/B targets before execution. Provider reads prove native C-to-A/B parentage and target-specific writes. Issue-created/link-pending replay cannot duplicate or reparent children. Unbound targets never look verified. |
| EC-05 | GitHub checks, deployment receipts and an independently asserted integration result bind the complete exact revision set. Missing/failed/stale/wrong-repository evidence and ambiguous repairs cannot pass or default to H. |
| EC-06 | Legacy bytes/hashes and absent Product attribution remain valid; explicit attribution is audited/idempotent. Fresh/upgrade configs run without global routing; provider reads verify cleanup and retained resources. |

Preserve all child AC-01..04 and SCN-01..04 IDs. The backlog maps each to additional
concrete assertions, entry points and handoff. Offline tests use isolated tenants,
fixed clocks/seeds, literal provider IDs, transport stubs at provider boundaries,
deterministic expected receipts and bounded retries. Exercise public handlers and
scheduled consumers, not just new helper functions. Real model output is assessed
by bounded observable results, not exact text. Offline checks do not prove live
availability. Green GitHub checks alone do not prove integration correctness.

Live preflight fails before fixture creation if target/role, ownership, installed
revisions or limits are missing. Retain run ID, attempt ID, private resource
inventory and evidence on every failure. Resume reconciles actual provider state;
cleanup touches only owned fixtures, verifies absence/restoration through provider
reads and records intentional retention. Reporting failure must not erase cleanup
work. Paid live qualification is manual, never launched by offline CI.

Final operator report maps every child AC and every EC to pass/fail/blocked/not-run,
run/attempt, immutable tested revisions and evidence references. MR-01..13 close at
their reviewed/merged pre-merge boundaries; MR-14 and the epic stay open until the
live criteria and cleanup are satisfied. Use `Refs`, not an auto-closing PR phrase,
for the epic and live qualification. This design does not claim those results.

## 13. Unresolved facts and handoff

- The requester must name the coordinator, implementation executors and live
  qualification operator before their respective dispatches. No authoritative
  tenant project/product/catalog relationship is supplied by the issue or public
  repository; keep fields unresolved rather than infer consumer IDs from ADP's
  own catalog. This is not a reason to leave child designs unfinished.
- No selected live environment, credentials, installed revisions, fixture access
  or operational state were checked. MR-14 verifies these under the existing
  deployment authority; documentation publication is not that authority.
- Concurrent work must be integrated, not overridden: [PR #8428](https://github.com/aws-e/adp/pull/8428)
  touches protected developer recovery/lifecycle timeout; [PR #7314](https://github.com/aws-e/adp/pull/7314)
  concerns tenant project/flow binding. Preserve compatible merged behavior without
  adding a Project workflow here. #8141 supplies Product identity/access; #8148
  remains broader integration work, not a blanket prerequisite. MR-07 depends on
  MR-06's verified epic references; MR-11 depends on MR-07's issue/link publication
  contract. Native parents remain; the parallelism amendment replaces MR-13's
  wait for MR-12 with stage-3 acceptance and makes MR-14 explicitly join both.
- No material expansion is required beyond 14 medium outcomes. MR-04 and MR-12 have
  broad call-site counts but share the MR-02 reader. If an implementation discovers
  a new credential mechanism or multi-environment topology requirement, stop that
  expansion and bring a scoped adjustment to the existing owner; do not hide it in
  these stories. This architecture has already completed the tracked-file audit.

## Sources

Every link below is pinned to the inspected main revision. The machine-readable
inventory contains blob IDs and all matching line numbers, including ancillary
callers not repeated in this narrative.

[proposal]: https://github.com/aws-e/adp/blob/d8e8a2a5c5e8e540f243e4aee77dcf54b3689ecc/modules/gateway/src/orchestration/proposal.py#L196
[ordering]: https://github.com/aws-e/adp/blob/d8e8a2a5c5e8e540f243e4aee77dcf54b3689ecc/modules/gateway/src/orchestration/proposal.py#L914
[accepted]: https://github.com/aws-e/adp/blob/d8e8a2a5c5e8e540f243e4aee77dcf54b3689ecc/modules/gateway/src/orchestration/accepted_node.py#L25
[nodes]: https://github.com/aws-e/adp/blob/d8e8a2a5c5e8e540f243e4aee77dcf54b3689ecc/modules/gateway/src/orchestration/models.py#L340
[amend]: https://github.com/aws-e/adp/blob/d8e8a2a5c5e8e540f243e4aee77dcf54b3689ecc/modules/gateway/src/orchestration/amend.py#L150
[dispatch]: https://github.com/aws-e/adp/blob/d8e8a2a5c5e8e540f243e4aee77dcf54b3689ecc/modules/gateway/src/orchestration/dispatch_pass.py#L786
[installation-selection]: https://github.com/aws-e/adp/blob/d8e8a2a5c5e8e540f243e4aee77dcf54b3689ecc/modules/gateway/src/orchestration/dispatch_pass.py#L626
[replay]: https://github.com/aws-e/adp/blob/d8e8a2a5c5e8e540f243e4aee77dcf54b3689ecc/modules/gateway/src/orchestration/protected_dispatch_recovery.py#L86
[claims]: https://github.com/aws-e/adp/blob/d8e8a2a5c5e8e540f243e4aee77dcf54b3689ecc/modules/gateway/src/orchestration/models.py#L544
[bindings]: https://github.com/aws-e/adp/blob/d8e8a2a5c5e8e540f243e4aee77dcf54b3689ecc/modules/gateway/src/orchestration/models.py#L641
[review]: https://github.com/aws-e/adp/blob/d8e8a2a5c5e8e540f243e4aee77dcf54b3689ecc/modules/gateway/src/orchestration/review_cycle_dispatch.py#L331
[shared-cycle]: https://github.com/aws-e/adp/blob/d8e8a2a5c5e8e540f243e4aee77dcf54b3689ecc/modules/gateway/src/orchestration/shared_cycle.py#L241
[graph-dispatch]: https://github.com/aws-e/adp/blob/d8e8a2a5c5e8e540f243e4aee77dcf54b3689ecc/modules/gateway/src/agentauth/graph_dispatch.py#L194
[commands]: https://github.com/aws-e/adp/blob/d8e8a2a5c5e8e540f243e4aee77dcf54b3689ecc/modules/gateway/src/orchestration/engine_commands.py#L571
[tracker]: https://github.com/aws-e/adp/blob/d8e8a2a5c5e8e540f243e4aee77dcf54b3689ecc/modules/gateway/src/orchestration/tracker_projection.py#L366
[planning]: https://github.com/aws-e/adp/blob/d8e8a2a5c5e8e540f243e4aee77dcf54b3689ecc/modules/gateway/src/orchestration/planning.py#L178
[amendment-ui]: https://github.com/aws-e/adp/blob/d8e8a2a5c5e8e540f243e4aee77dcf54b3689ecc/modules/gateway/frontend/src/components/orchestration/AmendmentReviewPanel.tsx#L243
[repository-evaluation]: https://github.com/aws-e/adp/blob/d8e8a2a5c5e8e540f243e4aee77dcf54b3689ecc/modules/gateway/src/orchestration/repository_evaluation.py#L110
[runtime-evaluation]: https://github.com/aws-e/adp/blob/d8e8a2a5c5e8e540f243e4aee77dcf54b3689ecc/modules/gateway/src/orchestration/evaluation_controller.py#L77
[correction-child]: https://github.com/aws-e/adp/blob/d8e8a2a5c5e8e540f243e4aee77dcf54b3689ecc/modules/gateway/src/orchestration/evaluation_correction_state.py#L157
[ownership]: https://github.com/aws-e/adp/blob/d8e8a2a5c5e8e540f243e4aee77dcf54b3689ecc/modules/gateway/src/admin/installations/resolver.py#L1
[connection-storage]: https://github.com/aws-e/adp/blob/d8e8a2a5c5e8e540f243e4aee77dcf54b3689ecc/modules/gateway/src/shared/models/vault.py#L244
[repository-verifier]: https://github.com/aws-e/adp/blob/d8e8a2a5c5e8e540f243e4aee77dcf54b3689ecc/modules/gateway/src/orchestration/work_admission.py#L65
[engine-grant]: https://github.com/aws-e/adp/blob/d8e8a2a5c5e8e540f243e4aee77dcf54b3689ecc/modules/gateway/src/agentauth/engine.py#L194
[qualification-runner]: https://github.com/aws-e/adp/blob/d8e8a2a5c5e8e540f243e4aee77dcf54b3689ecc/tests/e2e/orchestration/run.py#L187


## Product/epic amendment source checks

The Product addition was checked against main
`9b83cdd4ceb8b223a66209e049666485bab2747d`; the original full inventory retains its
own baseline above. Reuse [Product identity](https://github.com/aws-e/adp/blob/9b83cdd4ceb8b223a66209e049666485bab2747d/modules/gateway/src/products/models.py#L14)
and [existing Product membership](https://github.com/aws-e/adp/blob/9b83cdd4ceb8b223a66209e049666485bab2747d/modules/gateway/src/shared/identity/product_scope.py#L11)
through the existing authenticated human/delegation path. A human-only helper is
not an agent bypass; the consumer must use the supported delegated authority.
The [current sub-issue helper](https://github.com/aws-e/adp/blob/9b83cdd4ceb8b223a66209e049666485bab2747d/modules/agent-factory/agent/src/sub-issues.ts#L165)
resolves both issues in one repo and needs explicit distinct references for this
use case. [GitHub documents cross-repository sub-issues](https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/adding-sub-issues).
These checks identify reuse points and implementation gaps, not live acceptance.
