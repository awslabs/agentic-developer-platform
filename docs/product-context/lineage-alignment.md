# Context Model v1 — alignment with ADP lineage

**Review date:** 2026-10-15. **Result:** compatible as a proposed context/control-plane
model with the boundaries below; **not** a drop-in Insights event format.
Reviewed the context proposal at `6ec5b98fd` against lineage on main
`2950dd41f819cca363023b477d7b9c22c32f3862`. The referenced lineage schemas and
validator were unchanged from the proposal's base. This review's corrections are
part of the same draft PR; no runtime emitter, ingestion policy or existing lineage
schema has been changed.

**Profile alignment follow-up, 2026-10-19:** the
[Profile shared model](../product-onboarding/design/data-model.md) and
[Insights integration contract](../engineering-insights/lineage-schema.md#52-profile-and-context-integration-contract)
now explicitly share cross-product Contributions, Product–Release relationships,
canonical registry Project identity and independent record/link/mapping versions.
The Insights overview and lineage tables distinguish planned work, observed
artifact inclusion and completed production delivery. This follow-up updates the
design in both directions; existing v1/v2 producer admission remains unchanged.

The optional [Portfolio contract](README.md#portfolios-and-product-membership)
adds a typed collection of Products and dated `portfolio_product` membership to
Context. It is distinct from Program's grouping of Initiatives. Insights uses
the same membership/identity/history rules for authorized reporting; this addition
does not admit a Portfolio raw event or change the three-field Product first save.

The [Context Model](README.md) owns product knowledge, source authority and
coordination. [Lineage](../engineering-insights/lineage-schema.md) owns reproducible
relationships between work, execution and outcomes. A context view reads actual
producer facts; a context summary cannot create an execution fact or an acceptance.

## Review findings and resolutions

| Area | Finding | Resolution in the context proposal |
|---|---|---|
| Cross-project references — material incompatibility if emitted directly | Context coordination can link Checkout to Accounts. Lineage §11.1 and the v2 validator prohibit a raw record from referencing another Project, even in the same tenant. | Keep cross-project links in the authorized context/control-plane view. Compose authorized Project views for analytics. No direct serialization into v2 and no rewriting Project IDs to make links pass. |
| Milestone/link revisions — ambiguous identity | `mapping_version` was being used for the complete milestone outcome-set revision; link corrections had no separate explicit version. | Separate `relationship_revision`, source namespace and optional `supersedes_revision` from `mapping_version`. Each milestone requirement carries its own `requirements_revision`, matching the milestone's complete set. |
| Shipment versus acceptance — ambiguous outcome | `release_delivers_work` could be read as implying accepted production delivery, while a coordination milestone may require only publication or a test deployment. | Define it as artifact/work inclusion; keep criterion acceptance and first complete production shipment separate. Historical shipment and current rollback/readiness are distinct observations. |
| Coverage shortcut — missing semantic boundary | `work_test` looked like a direct coverage edge. Existing v2 coverage uses exact criterion/scenario/test revisions plus each run and attempt. | Treat `work_test` as navigation derived from v2 mappings, never an acceptance predicate or replacement coverage record. |
| Append-only history — ambiguous write behavior | “Upsert by source revision” could overwrite history or discard a changed mapping of the same source. | Upsert only the current-view pointer. Append immutable normalized revisions; deduplicate identical deliveries, reject conflicting bytes and retain mapping/parser versions. |
| Optional story links — existing schema limitation | Context permits routine work without a Story; checked-in v1 `task` requires `story_id`. | Preserve source execution facts and mark the ingestion gap. A new versioned contract must support non-story runs; no dummy Story IDs or silent v1 relaxation. |
| Shared business identity — incomplete join rule | Same Program/Initiative IDs in separate Project projections are not sufficient identity proof. | Resolve through explicit versioned catalog/namespace/alias mappings; retain original qualified endpoints and project grants. Never merge permissions or duplicate execution facts. |
| Program/Contribution/Milestone records — extension, not existing support | These have no corresponding admitted v1/v2 producer schemas today. | Mark them as proposed context extensions. Insights requires owned/versioned adapter contracts before ingesting them; profile and coordination views need not wait for that analytics integration. |

## What already aligns

- **Scope:** tenant and governed `project_id` remain the access/evidence boundary.
  Portfolio, Product, Program and Initiative membership cannot expand it. Missing optional
  business context does not invalidate a saved profile.
- **Native identity:** repositories, issues, PRs, runs and releases retain source
  identity. Tracker project IDs never overwrite the governed Project ID. Parent
  membership is a dated relationship, not encoded into a new Story ID.
- **Authority:** tracker owns authored work/hierarchy; the catalog/registry owns
  product identity; finance/budget services own funding and reconciled cost;
  release/deployment producers own publication and observed runtime outcomes.
- **Attribution:** explicit Product ↔ Work associations and Contribution work links
  resolve at effective time. Initiative membership alone never attributes every
  story to every product. Business-case funding and cost-allocation paths remain
  distinct and are deduplicated before aggregation.
- **Evidence:** planned scope, observed artifact inclusion, actual deployment,
  test results and acceptance assessments are distinct. Context needs exact refs
  and source revisions; text generated by an agent is not sufficient evidence.

## Adapter contract

The six-field `Ref` has the existing v2 shape; the containing payload's version and
semantics determine whether it is an admissible lineage reference. The context
schema imposes some additional length/non-blank limits; an oversized source value
needs a visible mapping/schema decision, never silent truncation or a changed ID.

| Context input | Required lineage handling | Current compatibility |
|---|---|---|
| `tenant_id`, `project_id` | Resolve through current authority and preserve exact scope on producer events and nested refs. | Same meaning; validation of JSON shape is not authorization. |
| `record_type`, native ID, normalized revision | Use the registered producer's exact type and immutable revision. A context `work_item` is not automatically `story_revision`; preserve a versioned mapping between those views. | No blind rename or ID regeneration. |
| Context observation/snapshot | Adapter obtains `source_system`, scoped `source_event_id`, true source `event_time`, actual ingestion time and source bytes/revision. `recorded_at` is context capture; `observed_at` is a source read; neither automatically equals source event time. | Context snapshots are missing parts of the raw event envelope by design. Unavailable required metadata is an explicit ingestion gap, not invented timestamps. |
| Product/Project/Initiative/context links | Emit via the owned registry/relationship adapter with namespace, relationship revision, mapping version and effective/recorded time. | Designed in lineage §§3.0–4, but not among the admitted record types in the checked-in validator. |
| Portfolio/Program/Contribution/CoordinationMilestone | Preserve business IDs and catalog projections; adapt only through a new admitted context/relationship contract. Portfolio membership is dated and many-to-many. | Proposed extension; no existing v2 record type accepts these. |
| Context cross-project association | Keep in the context coordination registry/view. Query each contributing Project separately, check grants and join through approved business mappings. | Not a valid cross-scope v2 raw record. Extending raw-event scope requires separate explicit design; this proposal does not do that. |
| Work-item metadata | Existing v1 Story may remain legacy metadata. v2 needs brief, URL, source update/action, immutable source snapshot and criteria-extraction state, plus envelope. Preserve existing AC/scenario IDs. | A title/status import alone cannot produce a complete v2 story revision. |
| `work_test` navigation | Resolve `story_revision → acceptance_criterion → coverage_definition → test_definition` through versioned mappings. Keep `test_run → run_test → test_result` and each result attempt. | Reuse v2 records; no new shortcut replaces them. |
| Tracker task versus execution Task | Planning `kind=task` remains a work item. Execution Task ID comes from gateway/platform; each run/session retains its own identity and applicable Story link. | Different grains; v1 Task currently requires Story, which constrains the optional-story adapter. |
| Release/manifest and deployment refs | Designated release authority supplies identity/manifest; deployment producer supplies deployment ID/target/result. A missing legacy release link stays unresolved. | Existing v1 deployment permits missing `release_id`; keep valid legacy evidence with gaps visible. |
| Owner, estimate and progress fields | Resolve dated team ownership separately. Preserve source estimate/unit. A user owner is not an inferred `team_id`, arbitrary points are not a shared size scale, and no cohort is assumed. | Missing team/cohort/classification stays a coverage gap for dependent metrics. |
| Allocation / funding proposal | Map finance-approved attribution separately from ledger actuals and from runtime enforcement policy. | The context allocation is not `cost_allocation_id` evidence and is not a new budget ledger. |

The Insights validator admits five v1 producer types (`story`, `task`, `commit_pr`,
`deployment`, `test_run`) plus quarantine, and nine v2 requirements/test types.
An envelope or six-field reference alone does not admit another record type.
No snapshot should be routed to an event adapter based only on `schema_version:
"1.0"`; select the named schema/producer contract as well.

## Historical and financial semantics

Use the original scoped identity plus immutable revision for evidence. Keep two
times distinct for mappings: the interval they apply to (`valid_from`/`valid_to`)
and when the service learned them (`recorded_at`). A correction can restate
history explicitly, but cannot mutate the bytes previously used to make a decision.
An incoming duplicate identity/revision with different bytes is a conflict to
quarantine, consistent with lineage §11.0. Changed normalization rules create a new
normalized revision even if the provider's source revision has not changed.

For product/program views, deduplicate original ledger/work identities before any
aggregation. If two Projects contain projections of one catalog Program, that does
not make their independent execution records duplicates, nor does it prove two
same-numbered Stories are the same. Cross-source aliases and shared-cost allocation
must be explicit, effective-dated and finance-approved.

Retain the [Insights metric definitions](../engineering-insights/agentic-engineering-insights-overview.md#611-exact-definitions-and-collection-methods):

- First completed production delivery is not a closed tracker item, published
  release, successful test or test-environment milestone.
- Recompute medians/rates/unit costs from underlying eligible populations; never
  average product/team rates or add overlapping portfolio summaries.
- Keep story, deployment and team-period denominators distinct. Do not remove
  retry, abandoned, unfinished or unlinked in-scope cost/effort from team totals.
- Human effort estimates and reconciled cost retain coverage, method and uncertainty;
  a profile budget or an agent's elapsed runtime does not replace them.
- Product attribution resolves dated epic membership and explicit story exceptions.
  A Contribution to an epic does not create duplicate counts for its child stories.
- Source freshness, link coverage, mapping version and reconciliation state remain
  visible. No eligible denominator means unknown/not enough data, not zero or 100%.

## Verification and remaining implementation work

Review checks exercise the existing lineage validator, not a recreated assertion
of its policy: a valid v2 mapping passes; changing only the target Project is
rejected; a v1 Task without its required Story is rejected. Both existing v1/v2
fixture suites remain valid. Context examples also validate under their own schema,
including the explicitly separate cross-project coordination association.

Context fixture checks distinguish mapping, link and milestone requirement-set
versions, preserve project-scoped identities and resolve endpoints across the two
example snapshots. These are offline contract checks, not live access or analytics
evidence. Existing lineage JSON schemas and validators remain unchanged.

Remaining work belongs to the relevant implementation slices:

| Owner | Work needed before dependent integration |
|---|---|
| #5411 / #7245 | Registry/authority adapters, immutable source observations, dated identity aliases, context-view authorization and append-only relationship persistence. |
| #7295 | Admit the proposed context/relationship producer contracts, implement versioned ingestion/joins and preserve data-trust/metric definitions. Program entities are not implicitly supported by a generic v2 envelope. |
| #7298 / execution owners | Resolve canonical project identity through actual dispatch; reconcile optional-story context with the current required-story v1 Task contract through an explicit version/migration. |
| #7628 / #7356 | Link exact release manifests, deployment outcomes and qualification/acceptance evidence; retain each attempt and distinguish historical shipment from current readiness. |

These are integration boundaries, not reasons to add mandatory Program, Initiative,
release or cloud fields to initial onboarding. The shared contract is designed now;
source-specific services can implement it incrementally.

## Product-to-Test drill-down

The [concrete trace contract](product-to-test-lineage.md) connects Product → Epic →
Story → criterion → scenario → Test ID and actual run/results. Work spanning
Stories is an Epic; existing coverage feature labels add no work hierarchy tier.
An explicit `work_story_revision` relationship bridges the context Story to its
exact Insights revision. The companion context snapshot reuses the existing
Insights traceability bundle without copying its Test IDs or changing raw schemas.
