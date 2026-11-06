# Implementation backlog and child design contracts

> Delivery update (2026-10-21): the original 14 feature contracts are retained.
> Four operational stories add a dev-baseline checkpoint and deployment/integration
> reviews for stages 1–3; MR-14 remains final qualification. See the
> [staged delivery plan](../../delivery-flow/plans/product-epic-delivery-8394/README.md)
> for current sequencing, evidence ownership and the five human outcome reviews.
> This is a draft delivery amendment, not approval or a claim of live completion.
> [Parallelism review](../../delivery-flow/plans/product-epic-delivery-8394/parallelism-review.md):
> MR-12 and MR-13 now run together after stage 3; MR-14 joins both and verifies
> the combined candidate before final live qualification. All existing ACs and stage gates remain.

Canonical shared design: [architecture](architecture.md). All 14 stories remain
**M (medium)**. The Product/epic amendment is reviewed and pending existing design
approval; no human approval reference exists yet. The source baseline is
`d8e8a2a5c5e8e540f243e4aee77dcf54b3689ecc`. Source paths below resolve against the
[complete pinned inventory](inventory/artifacts.tsv); new paths are explicitly
marked proposed. Child AC/SCN IDs and native parents stay unchanged. MR-07 adds MR-06 and
MR-11 adds MR-07 as implementation dependencies for verified epic publication.

The requester names a coordinator and executors before dispatch. The coordinator
integrates shared model, route and controller changes in dependency order; one
owner changes a shared contract at a time. Missing consumer catalog metadata is
not filled with guessed ADP-internal identifiers. MR-01..13 finish at reviewed,
merged code plus their pre-merge evidence; MR-14 and the epic require live proof.
The architecture PR uses `Refs`, not automatic closure.

## Dependency and acceptance map

| Child | Outcome | Depends on | Epic criteria | Rollout unit |
|---|---|---|---|---|
| [MR-01 / #8395](https://github.com/aws-e/adp/issues/8395) | Accepted identity/projection | None | EC-01, EC-02, EC-06 | Additive DB + gateway |
| [MR-02 / #8396](https://github.com/aws-e/adp/issues/8396) | Verified connection resolver | MR-01 | EC-01 | Gateway/tick |
| [MR-03 / #8397](https://github.com/aws-e/adp/issues/8397) | Initial dispatch/replay | MR-02 | EC-03 | Gateway/tick + envelope/grant compatibility |
| [MR-04 / #8398](https://github.com/aws-e/adp/issues/8398) | PR lifecycle/recovery | MR-03 | EC-03 | Gateway/tick + report consumers |
| [MR-05 / #8399](https://github.com/aws-e/adp/issues/8399) | Coordinator/wave routing | MR-03 | EC-02, EC-03 | Gateway authority |
| [MR-06 / #8400](https://github.com/aws-e/adp/issues/8400) | Commands/tracker bindings | MR-02 | EC-02, EC-04 | Additive DB + gateway/tick |
| [MR-07 / #8401](https://github.com/aws-e/adp/issues/8401) | Intake/CLI authoring | MR-02, MR-06 | EC-04 | Gateway packaged CLI |
| [MR-08 / #8402](https://github.com/aws-e/adp/issues/8402) | Visible reviewed targets | MR-07 | EC-04 | Gateway API then frontend |
| [MR-09 / #8403](https://github.com/aws-e/adp/issues/8403) | New evidence schema | MR-01 | EC-05 | Shared contract + build staging |
| [MR-10 / #8404](https://github.com/aws-e/adp/issues/8404) | Repository/CI evaluation | MR-04, MR-09 | EC-05 | Gateway/tick |
| [MR-11 / #8405](https://github.com/aws-e/adp/issues/8405) | Deployed evaluation/corrections | MR-10, MR-07 | EC-05 | Gateway/tick + runtime contract consumers |
| [MR-12 / #8406](https://github.com/aws-e/adp/issues/8406) | Legacy migration/config retirement | MR-05, MR-06, MR-08, MR-11 | EC-06 | Compatible release, config/infra/manual steps |
| [MR-13 / #8407](https://github.com/aws-e/adp/issues/8407) | Qualification implementation | Stage 3 accepted; parallel with MR-12 | EC-03, EC-05, EC-06 | Offline CI + manual harness artifact |
| [MR-14 / #8408](https://github.com/aws-e/adp/issues/8408) | Live qualification/acceptance | MR-12, MR-13 | EC-01..EC-06 | Authorized live operation only |

MR-01 defines fields/interfaces with optional compatibility; MR-02 implements
verification and wires acceptance. Do not enable new acceptance during the gap.
MR-09 defines both evaluation modes, but adapters remain unavailable until
MR-10/MR-11 integrate them. MR-06 origin/tracker bindings remain optional for existing
reads; new epic publication in MR-07 consumes its verified parent contract.
MR-11 reuses MR-07 issue/link recovery. These two added dependencies do not make
Project integration or the whole #8148 epic a prerequisite.

All test selectors introduced below are **planned**, not already passing.
Existing test files are named as extension targets, not claims that they cover the
new behavior. Each AC-n maps to the child's existing SCN-n. Shared determinism:
isolated tenant/flow fixtures, fixed clock/seed, literal expected provider IDs,
bounded pagination/timeouts and independent expected state/receipt assertions.
Tests must enter through the stated API/tick/worker path, not only the resolver.

<a id="mr-01"></a>

## MR-01 — Flow Product and accepted story identity

**Product/epic amendment:** MR-01 owns the small Product addition: new execution acceptance requires an accessible Product ID; validate with existing tenant/human/delegation checks. Store it with accepted JSON and the flow projection, preserve it on resume, and prevent changing Product after any execution reservation. Ordinary Product edits retain identity. Legacy absence remains valid and omitted from old hashes. Reuse existing Product services; no Project picker, relationship-version framework or new permissions model.

[Shared contract](architecture.md#3-accepted-identity-and-persistence-mr-01).
Own `modules/gateway/src/orchestration/{proposal,models,compile,repository,amend,
amendment_impact,accepted_node,preview}.py` and a proposed additive Alembic migration.
Reuse `ProposedNode.repository_identity`/`provider_repository_id`; add the optional
connection selector and nullable query projections described in the architecture.
`POST /orchestration/flows` and amendment acceptance remain existing surfaces, with
repository-qualified duplicate validation and unchanged error envelopes.

Inputs are selected Product, proposed nodes and current accepted version; outputs are a canonical
accepted document, matching projections and attributed decision. Refuse stale
preview, inconsistent projection and started-node retarget. Authority comes from
existing flow membership/approval, never field presence. Preserve hashes and
commit plan/projection/decision atomically. No provider side effect is introduced.
Define MR-02's verifier interface; unresolved new bindings cannot dispatch.

| AC / scenario | Independent assertion |
|---|---|
| AC-01 / SCN-01 | Given: an accessible Product and A#42/B#42 stories; When: accepting through the flow API, including missing/unknown/inaccessible Product variants; Valid Product and distinct stories persist in accepted JSON and projections without an artificial edge; invalid Product variants are refused. |
| AC-02 / SCN-02 | Same repository/#42 unordered pair is rejected; explicit reachable ordering is accepted. |
| AC-03 / SCN-03 | Given: an untouched flow/node and then reserved or started execution; When: previewing Product or repository changes and resuming work; Untouched changes require normal accepted amendment; started Product/repository retarget is refused without mutation. Resume and Product rename retain the binding. |
| AC-04 / SCN-04 | Given: existing schema and legacy accepted documents without Product IDs; When: upgrading and reading/retrying them; Nullable Product/repository projections succeed; missing Product alone does not block legacy work; existing bytes/hash, attempt and decision identities remain unchanged. |

Extend `test_proposal.py`, `test_amend.py` and `test_flow_create.py` under gateway
orchestration tests, plus existing PostgreSQL migration fixtures. Handoff includes
migration ordering and golden serialization fixtures; keep expanded columns on
rollback. Integrate before MR-02/09. Reuse Product identity/access from #8141. Preserve compatible shared-file changes;
no new Project-binding workflow is required.

<a id="mr-02"></a>

## MR-02 — Tenant-owned repository resolver

**Product/epic amendment:** Keep the resolver repository-specific. A valid Product is not an access grant or a default repository. Verify each story issue against its submitted repository using immutable provider metadata; reject issue-in-A/target-B substitution. The planning repo can differ from both implementation repos. Existing tenant/connection/policy checks remain sufficient for repository admission in this scope; do not add a Product-to-repository catalog.

[Shared contract](architecture.md#4-repository-connection-resolution-mr-02).
Own proposed `modules/gateway/src/orchestration/repository_binding.py`; adapt
`work_admission.py`, `planning.py`, acceptance/admission callers and existing
`admin/installations/resolver.py`/`admin/connections` integration. Do not create a
second installation store or credential API. Return typed verified metadata or a
per-story refusal, never credentials. Use authenticated provider metadata and
canonical installation ownership, not the tenant's assertion list.

Inputs: tenant, authority, name/ID, reviewed connection selector and operation.
Output: verified ID/name/installation plus observation for downstream freezing.
Bound network reads; cache only tenant/installation/ID/operation-aware observations
with revocation checks. No persisted access result substitutes for current use-time
authority. Retry transient provider failures without changing selection. Existing
policy `repository_ids` stays name-based. Transfer/connection change requires
reviewed reauthorization; same-ID rename is observed without rewriting history.

| AC / scenario | Independent assertion |
|---|---|
| AC-01 / SCN-01 | Two installations resolve two story targets through acceptance/admission; adding an unrelated third does not alter either. |
| AC-02 / SCN-02 | Missing, ambiguous, revoked, wrong-tenant and disallowed connections create no grant/dispatch. |
| AC-03 / SCN-03 | Given: name/ID mismatch, reused repository name or an issue in A claimed as a story targeting B; When: verifying it; Refuse identity/issue-target substitution; a verified rename preserves numeric identity and still requires valid access. |
| AC-04 / SCN-04 | Revocation or selection change after preview causes acceptance/dispatch refusal, not fallback. |

Extend installation ownership, planning and dispatch/admission tests; add resolver
integration cases with controlled provider pagination. No new AWS grants/schema
beyond MR-01. Gateway/tick rollout follows MR-01 and supplies MR-03/06/07. Hold
new acceptance until this verifier and compatible downstream readers are ready.

<a id="mr-03"></a>

## MR-03 — Initial dispatch and protected replay

**Product/epic amendment:** Retain the owning flow reference in saved assignments so Product attribution survives publication, retry and result handling. Where an envelope explicitly needs product_id, derive it from that flow and reject mismatches. Planning-repo location never overrides a story target. Use existing lineage; no new Product fields in every runtime table.

[Shared contract](architecture.md#5-dispatch-review-reports-and-recovery-mr-03mr-04).
Own `dispatch_pass.py`, `protected_dispatch_recovery.py`, `results.py`, dispatch
readiness in `routes.py`/`controls.py`, `agentauth/engine.py` and affected
worker-envelope consumers. `tick_handler.py`/`tick.py` are the real scheduled entry
points. Remove global readiness/routing and single-installation selection from
these paths; runtime queue wiring remains configuration.

Persist accepted assignment and claim generation in existing action/protected
execution/outbox records before publication. Workers get one repo; reports and
result observation use that same assignment. Preserve SQL/DynamoDB conditional
reservation and FIFO deduplication. External publication is at-least-once; recovery
reuses operation and invocation identity rather than creating a new attempt.
No DDB key redesign/new queue. MR-02 errors become visible existing holds.

| AC / scenario | Independent assertion |
|---|---|
| AC-01 / SCN-01 | Given: a Product-bound epic flow with A/B stories and planning repo C; When: scheduled dispatch runs without a global repo setting; Envelopes, grants and claims retain correct A/B targets and owning flow/Product attribution; C never replaces either target. |
| AC-02 / SCN-02 | Given: committed dispatch interrupted before publish or after publish before acknowledgement; When: recovery ticks resume; Replay the same assignment and flow/Product linkage; no duplicate active claim, second logical attempt or Product reassignment. |
| AC-03 / SCN-03 | Restart after setting global B or deleting it cannot move queued A work. |
| AC-04 / SCN-04 | Pause/supersession/revocation/exhaustion forbids grant expansion or replay. |

Extend `test_dispatch_pass.py`, `test_tick_dispatch_preparation.py` and protected
handoff/reconciliation tests. Verify actual grant serialization and worker source
consumption. Coordinate compatible gateway/tick/worker releases; keep pending
inventory during rollback. MR-04/05 consume the frozen assignment.

Product-bound story dispatch remains held by default (`ADP_PRODUCT_STORY_DISPATCH_ENABLED=false`).
Enable it only for the Stage 2 combined rollout after MR-04/05 readers, the gateway,
tick and worker consumers are compatible with the frozen assignment. On rollback,
disable this switch without deleting accepted plans, claims or pending outbox
operations; re-enable only after compatible readers return so saved assignments
can replay.

<a id="mr-04"></a>

## MR-04 — PR lifecycle, review, repair and diagnosis

**Product/epic amendment:** Reviews, repairs, reports and diagnosis preserve the owning Product through the existing flow reference. The story issue and every replacement PR stay in the same implementation repository; neither the epic tracking location nor a Product display-name edit changes routing. Continue exact-head and attempt validation.

Own `pr_bindings.py`, `merge_evidence.py`, `review_cycle_dispatch.py`,
`shared_cycle.py`, `report_dispatch.py`, `diagnose.py`, `delivery_adoption.py` and
associated protected/lifecycle/review recovery readers. Keep review paths already
using `binding.repo`/installation/PR node ID; replace remaining global comparisons
and source derivation. Integrate [PR #8428](https://github.com/aws-e/adp/pull/8428)'s
reserved-review timeout recovery if merged; do not reset its attempt/deadline rules.

Inputs are accepted lineage and frozen execution/PR binding; output is exact-head
review/repair/merge eligibility or typed refusal. Registration/recovery verifies
repo ID plus PR identity, not URL/number alone. Diagnosis and replacement PRs stay
in the story repository. Reports retain nonce/signature/current-attempt fencing.
Reconcile pending provider effects before retry, preserving action records and
existing limits; no new table or broader reviewer scope.

| AC / scenario | Independent assertion |
|---|---|
| AC-01 / SCN-01 | Given: a Product-bound story in B under an epic tracked in C; When: review requests repair and the repaired head is reviewed; All workers/reports retain B, its installation and original flow/Product attribution; only the current verified head becomes merge-ready. |
| AC-02 / SCN-02 | Submit B's equal-number PR/report as A evidence; A state and merge eligibility remain unchanged. |
| AC-03 / SCN-03 | Restart pending report/repair/diagnosis after global mutation; resume original attempt exactly once logically. |
| AC-04 / SCN-04 | Superseded/revoked/exhausted continuations produce existing hold, no new worker scope. |

Extend review/shared-cycle/report/diagnosis/merge tests under gateway orchestration,
including PostgreSQL recovery suites. Release gateway/tick and report-contract
consumers compatibly after MR-03; supply exact-source evidence to MR-10.

<a id="mr-05"></a>

## MR-05 — Coordinator and wave identity

**Product/epic amendment:** Coordinate one Product-bound flow, usually for an epic whose native children reside in A/B. Resolve each accepted child by its own repository identity and preserve flow/Product linkage. Native epic parentage is planning context; accepted graph dependencies, gates and explicit parent delegation still decide execution. GitHub hierarchy edits do not mutate the accepted graph automatically.

Own `modules/gateway/src/agentauth/{graph_dispatch,coordinator,waves,routes}.py`.
Existing authenticated graph dispatch remains the API. Resolve graph address first,
then verify accepted repository ID/issue. Remove `configured_repo` equality as an
authorization substitute; preserve explicit parent authority intersection, narrow
child grants and control/wave replay fencing. Add repository IDs to existing wave
snapshot payloads; unqualified legacy targets are allowed only if unambiguous.
No new database table; no authority inferred from coordinator launch repository.

| AC / scenario | Independent assertion |
|---|---|
| AC-01 / SCN-01 | Given: one Product-bound epic flow, planning repo C and eligible A#42/B#42 stories; When: requesting each through graph dispatch; Select the accepted child and issue a one-repo grant for its target; both retain their original flow/Product attribution. |
| AC-02 / SCN-02 | Valid parent token without target delegation authority cannot create a child. |
| AC-03 / SCN-03 | Wave/control references with equal numbers in different repositories coexist; same-target conflict still fails. |
| AC-04 / SCN-04 | Unsatisfied predecessor/gate and replay cannot create premature or duplicate children. |

Extend `tests/agentauth/test_graph_dispatch.py` and coordinator/wave suites. Handoff
includes legacy ambiguity errors and snapshot compatibility. Gateway authority
release follows MR-03; MR-12 verifies all global consumers are retired.

<a id="mr-06"></a>

## MR-06 — Origin, commands and tracker references

**Product/epic amendment:** A Product planning repository may hold the epic, while its native child issues and PRs live in their change repos. MR-06 owns the verified epic/tracker reference supplied to MR-07, including separate parent repo/connection/provider identity. It does not create a planning repo or epic implicitly. Product attribution remains on the flow; no new Project binding is required.

[Shared contract](architecture.md#6-coordination-commands-and-tracker-targets-mr-05mr-06).
Own `engine_commands.py`, `command_attribution.py`, `tracker_projection.py`,
`tracker_provider.py`, `authoring_dispatch.py` and proposal/flow projection fields.
Add hashed typed `origin_ref`/`tracker_refs` and nullable flow JSON projections via
an additive migration. Keep display epic metadata non-authoritative and omitted
new fields compatible with old hashes. No new creation of trackers is implicit.

Verified event repository/installation selects qualified targets; graph address
can disambiguate only within authorized scope. Existing human membership/command
checks remain. Publication to C needs C's connection and issues-write authority,
not A/B execution authority. Persist target/content operation identity and reuse
projection/replan recovery; unresolved legacy writes are held, reads still work.

| AC / scenario | Independent assertion |
|---|---|
| AC-01 / SCN-01 | Signed events for A#42/B#42 select only the corresponding flow/node; unknown/ambiguous scope fails. |
| AC-02 / SCN-02 | Given: a Product-bound flow with epic/tracker C and stories A/B; When: resolving the parent reference and publishing status; Resolve C independently for native linking; status writes touch only C and link correctly to A/B without altering story targets or Product ID. |
| AC-03 / SCN-03 | Revoked C or forged event repo/installation cannot mutate or dispatch authoring. |
| AC-04 / SCN-04 | Lost publication ack/restart resumes the same target/content idempotently; old hashes remain identical. |

Extend engine-command quarantine, tracker and replan PostgreSQL tests. Ship schema
before gateway/tick writers; keep additive fields on rollback. Depends only on
MR-02; coordinate shared proposal/route writes with MR-01/07.

<a id="mr-07"></a>

## MR-07 — Intake and CLI authoring

**Product/epic amendment:** Use MR-06 parent references and existing gateway publication/provider records. Discover native children with bounded pagination and distinct repo/provider IDs. The current sub-issue helper assumes one owner/repo for both issues; implement a qualified parent/child path. Authorized issue creation and native linking are separate recoverable steps: reconcile actual parent before retry, never duplicate the issue or force-reparent. A failed link remains visible/incomplete; no dispatch based on unverified parentage. Flows without epics retain their path.

Own `planning.py`, `intake_routes.py`, `authoring_input.py`,
`amendment_issue_publisher.py`, `draft_binding.py` and
`modules/gateway/cli/adp-flow.py`. Preserve existing start/resume/amendment interfaces
and exit behavior. `--product` or selected Product context supplies the flow binding. For epic
intake `--repo`/`--issue` identify the epic; resolve native children in their own
repos. Direct story intake uses its issue repo. Per-node payloads support mixed
repositories; never stamp planning repo C onto all stories. MR-02 resolves each existing issue
or proposed issue publication. CLI preview prints qualified target and binding
status; conflict on resume requires reviewed change, not silent overwrite.

For amendment issue publication use immutable repository identity plus proposal
identity for deduplication. Add a partial unique index on tenant/provider repo
ID/proposal identity for non-null IDs; keep the legacy name index. Verify/backfill
consistent IDs, reconcile legacy conflicts before activation, preserve historical
names/rows and reconcile provider creation before retry. No duplicate issue on verified rename or acknowledgement loss.
Unbound/inaccessible/ambiguous/denied stories return actionable per-address errors
with no implicit acceptance or unauthorized issue creation.

| AC / scenario | Independent assertion |
|---|---|
| AC-01 / SCN-01 | Given: selected Product, epic C and native child issues A/B; When: CLI-to-intake resolves the epic, and direct-story intake resolves an issue; Product and epic reference round-trip; native children carry their own verified repos. Direct-story target equals its issue repo; C is never a blanket target. |
| AC-02 / SCN-02 | Given: an authorized Product/epic plan or amendment with new A/B stories; When: previewing, accepting publication and reading GitHub parentage; Create issues in A/B and verify native parent C; preserve Product, qualified targets and epic after round-trip. Existing story issue repo must equal target. |
| AC-03 / SCN-03 | Given: inaccessible Product/issue, ambiguous connection, denied link, parent conflict or issue/target mismatch; When: intake/publication runs; Refuse the affected operation with a visible prerequisite; no unauthorized write, silent reparent or dispatch on incomplete parentage. Retain any already-created issue. |
| AC-04 / SCN-04 | Given: issue-created/link-pending or link-ack loss, repository rename, or conflicting resumed Product/target; When: retrying/resuming; Reuse issue/operation and reconcile parent idempotently; no duplicate/reparent. Conflicting input requires reviewed change and existing exit/error behavior remains. |

Extend `test_planning.py`, `test_amendment_issue_publisher.py` and
`tests/cli/test_adp_flow.py`. Add rename/ack-loss publication assertions. Ship CLI in
its existing gateway image/download surface; coordinate additive index migration.
Depends on MR-02/MR-06; API and issue/link contract feed MR-08/MR-11.

<a id="mr-08"></a>

## MR-08 — Repository visibility and review UI

**Product/epic amendment:** Display Product identity, planning epic and story repo separately in creation, plan summary and review. Provide Product selection via existing Product UI/API patterns and expose missing/inaccessible state without a Project step. A new issue whose native parent link is pending is not shown as fully published or ready. Reuse existing states/read models and preserve legacy empty Product display.

Own `GraphNodeResponse`/graph projection in `routes.py`, frontend
`types/orchestration.ts`, services and orchestration `NodeChip`, `PlanSummary`,
`AmendmentReviewPanel`/node detail. Backend adds nullable accepted/proposed binding
and status; frontend does not infer identity from dispatch/global configuration.
Display verified `owner/repo#issue` before execution, old/new target diff during
amendment, and specific unbound/denied/stale holds. Fix the owner/name form label;
provider ID is separate inspectable data. Server still rejects started retargets.

| AC / scenario | Independent assertion |
|---|---|
| AC-01 / SCN-01 | Given: a Product-bound epic C and unstarted A/B stories; When: selecting Product and opening creation/approval views; Product and epic are distinct from story targets; A/B show correct qualified links before dispatch, with no Project picker. |
| AC-02 / SCN-02 | Target amendment shows scope change; server refusal cannot be bypassed by UI. |
| AC-03 / SCN-03 | Given: missing/inaccessible Product, unbound story or issue-created/link-pending publication; When: rendering readiness; Show the specific prerequisite and existing hold; no fabricated links, Product identity or claim of completed native linkage. |
| AC-04 / SCN-04 | Legacy, loading and error cases retain accessible controls and do not imply verified identity. |

Extend existing frontend orchestration component tests and graph response tests;
include keyboard/accessible-name assertions. No new schema beyond earlier
projections. Deploy backend additions before frontend via gateway/frontend
workflows. Depends on MR-07; no live access required for component tests.

<a id="mr-09"></a>

## MR-09 — Versioned combined evaluation contract

**Product/epic amendment:** Use existing flow/node lineage to attribute evaluation sources and receipts to their Product. The planning epic/repo is not an evidence source or harness default. A consumer receiving an explicit Product value must verify it against the owning flow; do not add redundant Product fields to the evidence schema solely for attribution.

[Shared contract](architecture.md#8-versioned-combined-evidence-mr-09).
Own proposed `contracts/multi-repository-evaluation/v1/models.py` and golden
fixtures; adapt `evaluation_contract.py`, contract self-check/consumer selectors,
`modules/gateway/scripts/stage-contracts.sh` and relevant image/CI path selection.
Inventory each consumer; do not mark new modes runnable before MR-10/11 exist.
Separate harness identity, exact accepted input set and optional target contract.
Versioned spec/receipt union uses repository and deployed modes with bounded sets,
source lineage, exact revisions and independent golden digest vectors.

| AC / scenario | Independent assertion |
|---|---|
| AC-01 / SCN-01 | Given: a Product-bound flow with epic C, harness H and A/B predecessor spec; When: previewing/validating it; Preserve distinct source/harness identities and canonical digest; flow/node lineage retains Product attribution and C is not an implicit source. |
| AC-02 / SCN-02 | Missing/extra/duplicate inputs fail; changing any source/harness/target revision invalidates the old digest. |
| AC-03 / SCN-03 | Literal existing evaluation documents retain bytes/hash and old semantics. |
| AC-04 / SCN-04 | A recognized schema without an enabled adapter reports unavailable; no weak fallback. |

Extend contract, proposal and packaged self-check tests; build both relevant
consumer artifacts offline and validate root-contract-only change selection.
No live runner or new IAM in this story. Depends on MR-01; MR-10/11 integrate modes.

<a id="mr-10"></a>

## MR-10 — Repository/CI combined evidence

**Product/epic amendment:** Resolve combined evidence back to its owning Product-bound flow using existing source lineage. The planning repo does not supply substitute CI evidence. Keep independent connection authorization, exact revisions and human/machine acceptance semantics; no new Product-specific evaluation subsystem.

Own `repository_evaluation.py`, `repository_evaluation_provider.py`,
`repository_producer.py`, `repository_producer_controller.py` and version dispatch
in `evaluation_controller.py`. Consume MR-09 inputs; use MR-02 separately for each
source and harness. Preserve v1's existing path. Replace runner-equals-all-sources
only in the new mode. Verify PR node ID, repository ID, exact head/merge SHA,
required successful checks and app/workflow identities. Persist combined receipt
in existing action evidence with exact set hash and source observations.

Partial reads survive only as pending work, not a successful verdict. Recheck
freshness on bounded retry and existing lineage on acceptance. Keep human/machine
mode separate from evidence validity; source verification grants no extra scope.

| AC / scenario | Independent assertion |
|---|---|
| AC-01 / SCN-01 | Given: Product-bound A/B PR predecessors, successful exact-revision checks, epic C and harness H; When: an evaluation tick runs; Collect authorized A/B evidence, bind the verdict to both inputs and the owning flow/Product; C is not an evidence substitute. |
| AC-02 / SCN-02 | Failed/missing/stale/inaccessible mandatory source prevents pass and identifies the blocked input. |
| AC-03 / SCN-03 | Equal-number PR/check-name substitution across repos fails on provider identity. |
| AC-04 / SCN-04 | Changed source revision and interrupted read invalidate old acceptance; bounded replay preserves mode/gates. |

Extend repository evaluation/provider/producer tests and acceptance regressions.
Gateway/tick rollout follows MR-04/09 with new mode enabled only after self-check.
No new data store; MR-11 consumes exact source lineage/receipt semantics.

<a id="mr-11"></a>

## MR-11 — Deployed evaluation and corrections

**Product/epic amendment:** Corrections retain the source flow/Product and affected story repo. For a new correction story under the epic, reuse MR-07 issue-create/native-link recovery: create in the affected repo, verify the parent link, and never fall back to planning repo or harness. Link failure remains pending with the issue retained; retry does not duplicate or reparent it. This adds MR-07 as an implementation dependency.

[Shared contract](architecture.md#9-repository-and-deployed-evaluation-correction-mr-10mr-11).
Own `evaluation_controller.py`, `evaluation_plan.py`, `evaluation_runtime.py`,
`evaluation_evidence.py`, `evaluation_corrections.py`,
`evaluation_correction_state.py`, `evaluation_issue_provider.py` and correction
accepted-lineage reader. New deployed mode requires complete source receipts plus
one approved compatible target contract, not a single source repository. Merge-only
inputs cannot satisfy runtime dependencies.

Correction actions add immutable affected-source authorization snapshot/digest;
server-created correction nodes inherit that accepted target through existing
linkage, not arbitrary row fields. Reconcile issue creation and dispatch using
existing operation key plus source identity. Unknown/multiple attribution holds
for existing review; never pick H. Repaired B invalidates combined A/B verdict and
forces retest within existing correction limits. No topology or new authority.

| AC / scenario | Independent assertion |
|---|---|
| AC-01 / SCN-01 | A/B deployment receipts and independent probe agree on target/revision set; only accepted mode advances. |
| AC-02 / SCN-02 | Merge-only, wrong-target, stale or unauthenticated source keeps dependents blocked. |
| AC-03 / SCN-03 | Given: failed integration attributed to B in a Product-bound epic flow; When: publishing/admitting a correction, including a parent-link failure; Create the issue and PR in B with original flow/Product and verified epic parent. Link failure stays pending; ambiguous attribution creates no worker or harness fallback. |
| AC-04 / SCN-04 | Given: repaired B or interrupted evaluation/correction publication/link; When: scheduled recovery resumes; Reuse issue/link operation, preserve flow/Product and reevaluate the changed set; no stale verdict, duplicate child or scope/budget expansion. |

Extend evaluation-controller, evidence and correction integration suites. Release
compatible gateway/tick/runtime consumers after MR-10/MR-07; hold adapter unavailable
until producer/consumer packaging is verified. Pass receipt/correction migration
cases to MR-12 and fixture scenarios to MR-13.

<a id="mr-12"></a>

## MR-12 — Legacy migration and global-config removal

**Product/epic amendment:** Treat legacy missing Product attribution separately from unresolved repository identity. Missing Product alone must not break an existing flow. Permit explicit authorized attribution as audited metadata without rewriting accepted JSON, assignments or history; never infer it from repo, epic or slug. New flows still require Product validation. Product association versions and Project backfill are not added here.

The repository inventory is already [delivered](audit.md), not delegated here.
Own proposed stored-record inventory/dry-run/apply tooling, compatibility diagnostics,
runbook and production global-config consumers in
[inventory/global-readers.tsv](inventory/global-readers.tsv). The complete
config/build scope is in the audit map: Terraform root/tick, ConfigMap, deployment
workflow, release defaults, deploy-all, upgrade-state, shared-runtime preservation
and readback. Historical documentation/test false positives are explicitly excluded
from runtime changes. MR-13 exclusively owns qualification-package/workflow edits;
MR-12 audits their coverage without competing ownership. Both join at MR-14.

Classify explicit bindings, trustworthy started provenance, untouched amendable
work and ambiguous holds. Apply only reviewed expected-version amendments or
rebuildable projections; never rewrite accepted bytes or move a pending effect.
Retain a durable private inventory across process loss. Prove compatible rollback
with expanded schema; old incompatible binaries require quiesced writers.

| AC / scenario | Independent assertion |
|---|---|
| AC-01 / SCN-01 | Given: legacy flows with no Product ID, drafts, active dispatches and explicit repository bindings; When: inventory/dry-run runs; Classify Product absence separately from repository provenance with zero mutation; do not fabricate ownership or suspend legacy work solely for missing Product. |
| AC-02 / SCN-02 | Given: authorized explicit Product attribution, a verified untouched repo binding and ambiguous repo provenance; When: applying migration twice; Record authorized metadata/amendment once; preserve accepted bytes, assignments and history. Never infer Product; unresolved repo work stays held. |
| AC-03 / SCN-03 | Fresh and upgrade configs with global setting absent run full offline lifecycle/readiness/recovery. |
| AC-04 / SCN-04 | Rollback fixture preserves assignments/history and prevents incompatible writers consuming new plans. |

Extend PostgreSQL migration, dispatch lifecycle, deployment config and platform
upgrade preservation tests. Integrate only after MR-05/06/08/11. Hand operator an
explicit compatibility floor, pending inventory and config removal order; no live
migration is executed by this implementation story without separate authority.

<a id="mr-13"></a>

## MR-13 — Qualification harness and owned fixtures

**Product/epic amendment:** Require the main fixture to include a Product and a distinct planning repo C with native A/B children. Verify issue/PR co-location, Product continuity, actual parentage and denied/mismatched inputs. Inject failure after issue creation and after native linking before acknowledgement; recover without duplicate issues or reparenting. Include legacy flows without Product attribution. Preserve existing owned-fixture cleanup and finite budgets.

Own `tests/e2e/orchestration/{config,fixtures,inventory,run,report}.py`, scenario
registration/providers/cleanup and workflow selection in
`.github/workflows/orchestration-harness-ci.yml` / `orchestration-live-tests.yml`.
Extend the existing four modes, not a second CLI. Version a proposed multi-repo
scenario/config with Product, epic C, native A/B children, distinct connections
and optional H. C differs from A/B in the main fixture. Include collision,
revocation, cross-repository dependency and committed-publication/review/report/
evidence handoff faults. Exercise actual handlers with transport stubs offline.

Private config supplies target and credentials references, ownership and finite
spend/attempt/concurrency/deadline/retention limits. Unknown targets/limits fail
preflight before creation. Retained run inventory supports restart and cleanup;
verify provider absence/restoration, report unresolved resources as incomplete.
No paid scenario is automatically invoked by CI.

| AC / scenario | Independent assertion |
|---|---|
| AC-01 / SCN-01 | Given: owned Product, epic in C and native story fixtures in A/B; When: the existing runner executes the scenario offline; Assert Product/epic linkage, same-repo story issue/PR, actual parentage, correct targets and acceptance through real entry points. |
| AC-02 / SCN-02 | Given: faults after issue creation/link acknowledgement, dispatch, review/report and evidence handoff; When: resuming the run; Recover without duplicate/reparented issues, wrong targets or changed Product attribution; retain attempts and test legacy Product absence. |
| AC-03 / SCN-03 | Given: missing/inaccessible Product, unknown target, missing link access or absent spend/deadline limits; When: preflight runs; Refuse before fixture creation with a precise prerequisite; offline CI launches no paid run. |
| AC-04 / SCN-04 | Interrupted creation/reporting can resume cleanup from inventory and accounts for every owned resource. |

Extend existing `test_config.py`, `test_run.py`, `test_inventory.py`,
`test_cleanup.py` and scenario tests. First runnable checkpoint is offline config+
preflight validation; full acceptance also requires the complete offline delivery
fault matrix. Start after stage 3 acceptance, in parallel with MR-12; migration
tooling is not a harness build prerequisite. MR-14 waits for both merges, reruns
the full offline matrix and migration/config/rollback regressions on their combined
candidate before final live qualification, then executes the versioned live procedure.

<a id="mr-14"></a>

## MR-14 — Live qualification and acceptance report

**Product/epic amendment:** Run the Product/epic fixture with C distinct from A/B. Read native parentage from GitHub, observe A/B issues and PRs in their respective change repos, and verify Product identity survives recovery and correction. Include link failure/retry and existing access-denial cases. Final proof remains installed/live evidence and cleanup, not just a merged design or green offline checks.

No new application implementation. Join MR-12 and MR-13 reviewed merges and rerun
the complete offline matrix plus migration/config/rollback regressions on the
combined candidate before final live qualification. Named operator owns approved artifact deployment
and the existing manual `orchestration-live-tests.yml` workflow with MR-13's scenario.
Use its current `config_path`, `environment`, `mode`, `qualification_id` and
`source_run_id` interfaces; preflight before run, restored originating inventory
before resume/cleanup. Follow canonical deployment authority/credential selection;
this design contains no approved live target or runtime grant.

| AC / scenario | Independent assertion |
|---|---|
| AC-01 / SCN-01 | A candidate containing both MR-12/MR-13 passes the complete offline matrix and migration/config/rollback regressions before final live qualification. Installed gateway/tick/frontend/worker/contract revisions, selected target/role, ownership and finite budgets match reviewed inputs; missing/failed checks stop the run. |
| AC-02 / SCN-02 | Given: qualified Product-bound epic C with A/B children and no global repo setting; When: running review, repair, merge and combined evaluation; Observe native cross-repo parentage, same-repo story issue/PR lifecycles, stable Product attribution, gates and exact-revision verdict. |
| AC-03 / SCN-03 | Given: the same run with identity/collision/revocation, issue/link and restart faults; When: executing/resuming them; Observe scoped denials and idempotent recovery without duplicate/reparented issues or changed Product/targets; retain failed/successful attempts. |
| AC-04 / SCN-04 | Provider reads verify cleanup/retention and final report maps all 56 child ACs plus EC-01..06 to exact revisions and evidence. |

Independent oracles: provider PR/check APIs, engine decisions/claims, authenticated
deployment receipts and integration assertions, not worker promises. Substantial
missing implementation returns to its owning child. Required blocked/not-run rows
keep MR-14 and epic open. Publish sanitized report with private evidence references;
never commit deployment identities or raw private operational evidence.

## Review and publication record

The architecture and every section above are authored; they are not approved
application behavior. Child Design sections link to an immutable revision of this
bundle when published. Two native dependencies are added for shared publication contracts. Approval synchronization
requires an actual approval reference through the existing process, not a fabricated
status. Outstanding non-design prerequisites are named owners, authoritative consumer
lineage metadata and live target/access/installed-revision verification.
