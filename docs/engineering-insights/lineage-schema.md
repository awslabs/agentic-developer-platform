# Agentic Engineering Insights — Lineage Schema

**Purpose:** the detailed lineage design supporting the [Agentic Engineering Insights overview](agentic-engineering-insights-overview.md). Start with the overview's [high-level lineage](agentic-engineering-insights-overview.md#53-lineage-at-a-glance), then use this document for proposed record fields, identifier sources, relationships and validation checks.

**Status:** lineage design with checked-in v1 producer schemas and additive v2 requirements/test schemas; not a deployed API or ingestion service. The overview owns the [six scorecard metric definitions](agentic-engineering-insights-overview.md#611-exact-definitions-and-collection-methods) and [data-trust rules](agentic-engineering-insights-overview.md#64-layer-4-data-trust-shown-next-to-every-number). Section 7 here maps those metrics to evidence. Sections 3.3/3.8 preserve v1 semantics; section 11 specifies v2. Other adapter contracts and the implementation questions in section 10 remain open. The shared service's [tenant/project authority and initial scope](README.md#product-boundary-and-ownership) also apply; these outcome extensions do not establish that the service implements them today.

**Scope:** software delivery only. Cost comes from the AI Budget Controller. Business results such as checkout conversion stay in product analytics and arrive only as a read-only feed linked by Release ID.

---

## 1. Design principles

1. **Use the IDs your tools already create.** Jira keys, Git SHAs, Git tags and full test names go in unchanged. Our system only generates an ID where no tool does, for example Task IDs for desktop sessions.
2. **Identity is stable and scoped.** Preserve source-native identifiers and qualify them by tenant, project, source connection and entity type. Identical Jira keys, PR numbers or test names in separate sources must not collide. When a human-readable key changes, retain the stable source identity and an alias history.
3. **Parents are links, not parts of the ID.** `PROJ-123` does not contain its epic. Stories move between epics, so the link is stored with the dates it applied.
4. **The Story joins work and outcomes.** Metric denominators remain distinct: lead time and rework use story cohorts, change failure rate uses deployments, and throughput, unit cost and human effort use team-period totals. Recompute aggregates from their underlying populations; do not sum rates or medians.
5. **Records are append-only.** Corrections are new records. Nothing is edited in place, so any number can be traced back and recalculated.
6. **Every record carries its source and arrival time.** This lets the data trust panel show how complete and how fresh the data is.
7. **Product and initiative describe business context.** `product_id` identifies an enduring product or service. Optional `initiative_id` identifies a business objective that normally spans products. A Contribution records one Product's commitment to that objective within its governed Project. `project_id` retains its existing meaning: the Insights evidence and access boundary. Business membership cannot grant access to another Insights project.

---

## 2. The model at a glance

```text
SCOPE AROUND EVERY RECORD
Tenant + Insights Project

BUSINESS CONTEXT AND EXECUTION
Portfolio ↔ Product                    (optional collections, many-to-many)
Initiative ↔ Product                   (optional, many-to-many)
Contribution = Initiative × Product × Project commitment
Contribution → selected Epic / Story
Product ↔ Epic → Story → Task
Product ↔ Story → Task                  (work can omit Epic and Initiative)
Business Case ── funds ──→ Initiative and/or Epic
Task → Call record (cost, tokens)
Task ↔ Commit SHA ↔ Pull Request
Human effort records → Task / Story / Team reporting period

ACCEPTANCE AND QUALITY
Story revision → Acceptance criterion revision ↔ Scenario revision ↔ Test definition
Test Run → Planned test instances → Test results (each attempt retained)
Acceptance assessment → Criterion revision + Results / manual evidence
Commit SHA + Build → Test Run, Coverage, Scan finding
Pull Request → Review, Quality gate result

PRODUCTION AND LATER OUTCOMES
Product ↔ Release
Release ── planned scope ──→ Story
Release ── observed artifact inclusion ──→ Story
Release → Deployment attempts (per environment) → causally linked Incident
Release / feature reference → Product analytics outcome feed

MODEL EVALUATION
Eval ID → Eval Run → Model version + dataset/prompt versions
```

These are dated relationships, not a compulsory hierarchy. An Initiative coordinates Contributions from several Products; each Product can participate in several Initiatives. Work keeps its tracker identity, and routine work need not have an Initiative or Epic. See section 4 for cardinalities, attribution and aggregation rules. Tenant + Insights Project scopes every endpoint, including permitted projections of shared business records.

**The two joins that matter:**
- **Story revision → Criterion revision ↔ Scenario:** links the exact requirement to what was tested; section 11 defines the v2 contracts.
- **Commit SHA:** links the work that produced the code (Task) to the quality results on that code (Test Run, scans, reviews).

---

## 3. Entities and fields

Every record also includes the standard fields in section 3.0. The [overview's system-of-record table](agentic-engineering-insights-overview.md#55-recommended-systems-of-record-for-a-typical-enterprise) identifies the recommended authority for each ID. Source columns below describe where the proposed fields originate or how adapters obtain them; branch names, tags and copied fields must resolve to the configured authority. Section 3.11 records the onboarding choices.

### 3.0 Standard fields (on every record)

| Field | Type | Notes |
|---|---|---|
| `tenant_id`, `project_id` | string | Resolved from ADP tenant authority and the canonical Project registry. `project_id` is the persistent delivery/operating and Insights evidence/access scope. A source's business delivery project can map to Initiative under explicit authority; it cannot replace `project_id`. Report fields cannot grant access |
| `source_connection_id` | string | Registered source namespace within the authorized project |
| `source_system` | string | e.g. `jira`, `github`, `gateway`, `ci`, `pipeline` |
| `source_event_id` | string | Source delivery/event ID. For snapshot APIs, include an immutable source revision or change marker so later updates are not discarded as duplicates |
| `event_time` | timestamp (UTC) | When it happened in the source |
| `ingested_at` | timestamp (UTC) | When it arrived |
| `schema_version` | string | Version of this schema |
| `team_id` | string or unknown | Dated ownership mapping at event time; missing ownership remains a coverage gap |
| `cohort` | string or unknown | Registered rollout assignment, e.g. `baseline`, `wave-1`, `not-onboarded`; missing assignment is not inferred as baseline |

Identifiers in the following tables are source-native examples. Canonical lookup and join keys include `(tenant_id, project_id, source_connection_id, entity_type, source-native ID)`. Cross-source relationships store both qualified endpoints. Source IDs and shared-service run IDs remain distinct and are mapped explicitly. Exact serialized key formats and per-entity required fields remain adapter-contract decisions.

Product and initiative context is resolved through versioned relationships, rather than requiring every raw test or call event to repeat it. Derived views can expose `product_ids`, optional `initiative_ids`, `contribution_refs`, `business_case_ids`, `context_mapping_version` and `attribution_status`. Preserve the qualified references behind display IDs; these are not additional raw producer fields. Missing product mapping is `unknown`; an initiative deliberately not used is `not_applicable`. A source tracker project can be retained as `source_project_id`; it must not overwrite the Insights `project_id`.

The proposed [Context Model](../product-context/README.md) adds optional Portfolio, Program,
Product Contribution and Coordination Milestone context. Its
[alignment review](../product-context/lineage-alignment.md) distinguishes proposed
registry/relationship adapters from currently admitted lineage records. These
entities do not become new v1/v2 producer types by using the same reference shape.
Cross-project coordination composes authorized views; the same-project raw-reference
rule in section 11.1 remains unchanged.

### 3.0.1 Product

| Field | Example | Source |
|---|---|---|
| `product_id` | `PRD-CHECKOUT` | Stable product/service catalog identifier, or registered identity where no catalog exists |
| `name` | Checkout | Product/service catalog |
| `owner_team_id` | `TEAM-COMMERCE` | Dated ownership mapping |
| `lifecycle_status` | `draft`, `active`, `retired` | Product/service catalog/Profile registry; saving a draft is not activation, and identity remains stable after retirement |

Product represents the enduring customer or internal capability. Its links to initiatives and epics live in the relationship records in section 4. A product may span repositories, teams and separately authorized Insights projects; cross-project comparisons require explicit mappings and access to every included project. A shared product label never merges evidence or permissions automatically.

The [Profile v1 registry](../product-onboarding/design/data-model.md) has one optional
current team mapping shared by its Projects and one managing Product per Project.
Those are configuration/administration constraints. They neither restrict explicit
many-to-many work/release attribution nor rewrite historical reporting teams when
the current team changes. An accountable user is not automatically a reporting team.

### 3.0.2 Initiative (optional cross-product objective)

| Field | Example | Source |
|---|---|---|
| `initiative_id` | `INIT-2026-014` | Portfolio/delivery tracker or a registered mapping |
| `name`, `objective` | Checkout modernisation; improve saved-card checkout | Portfolio/delivery tracker |
| `status`, `owner_team_id` | `in_progress`, `TEAM-COMMERCE` | Dated tracker/ownership records |
| `planned_start_at`, `planned_end_at`, `completed_at` | Dates or unknown | Tracker; actual completion remains distinct from the plan |

An initiative groups work for a defined business objective and normally spans
products. Its identity is independent of any one Product. Omit this grouping for
routine product work and link Product directly to Epic or Story. The Business Case
Registry links funding and outcome targets to the initiative or to individual epics
through scoped relationship records.

### 3.0.3 Contribution (optional product commitment)

Reuse the [Context Contribution](../product-context/README.md#cross-product-programs-contributions-and-milestones)
record, rather than minting another Insights business entity or backlog.

| Field | Meaning / authority |
|---|---|
| Qualified identity and `record_revision` | Source-owned Context/portfolio record in the participating Project; preserve the six-field reference |
| `initiative_ref`, `product_ref` | Exact authorized projections of the shared Initiative and participating Product |
| `objective`, `owner_ref` | Agreed Product outcome and accountable delivery owner; neither supplies an access grant |
| `valid_from`, `valid_to` | Effective commitment interval; at most one current Contribution per Initiative × Product × Project tuple in Context v1 |
| `planned_start_date`, `target_date` | Optional commitment dates, separate from observed completion |

`contribution_work` links existing epics/stories. When Contributions exist, derive
Product–Initiative and Initiative–Work views from their typed refs and work links;
do not require a second set of manually maintained associations. Preserve explicit
Product–Work attribution and story exceptions. A common Initiative never makes all
of its work belong to every participating Product. Contribution is proposed context,
not a type admitted by the existing v1/v2 producer schemas; section 5.2 defines the
integration boundary.

### 3.0.4 Portfolio (optional collection of Products)

`portfolio_id` is the source-owned catalog identity, represented by `record_id` on
Context's `portfolio` record. Reuse the
[typed Portfolio and membership contract](../product-context/README.md#portfolios-and-product-membership):
name, optional objective, accountable owner and catalog lifecycle. A Portfolio
groups enduring Products; a Program groups Initiatives. Neither is a required
onboarding parent, execution scope or source of access grants.

`portfolio_product` is optional many-to-many dated membership. Commerce can contain
Checkout and Accounts, while Shared services also contains Accounts. An Initiative
can involve Products across both collections through their existing Contributions.
Authorized Project projections retain one business identity and original qualified
references under approved catalog mappings. No nested Portfolios in Context v1.

Resolve membership at event effective time and preserve recorded-time history.
Portfolio reports deduplicate work, Product projections and ledger facts before
recomputing the existing metrics. Use approved allocation/primary-attribution rules
for additive totals; never sum overlapping Portfolio summaries. Current-membership
regrouping is a separately labelled view. Hidden members/counts stay hidden and
access-limited coverage stays incomplete. Portfolio is context for the versioned
registry adapter in section 5.2, not an admitted v1/v2 raw producer type.

### 3.1 Business Case

| Field | Example | Source |
|---|---|---|
| `business_case_id` | `BC-2026-014` | Existing investment approval system; retained by the AI Business Case Registry. The Registry owns it only when no existing authority is designated |
| `name` | Checkout and payments | Authoritative investment record, referenced by the Registry |
| `funding_code` | SAP WBS element | Finance ERP, e.g. SAP or Oracle; referenced by the Registry |
| `unit_of_work` | `story` | Registry |
| `metric_pack` | `software_delivery_v1` | Registry |
| `targets` | per core metric | Registry |
| `boundary` | `release`; business results from product analytics | Registry |

Funding targets are recorded as qualified Business Case → Initiative/Epic relationships. One case can fund several targets, and a target can have several cases. Preserve direct and inherited funding provenance so the same allocation is not counted twice.

### 3.2 Epic

| Field | Example | Source |
|---|---|---|
| `epic_id` | `CHK-12` | Jira / Linear / GitHub |
| `title` | Checkout redesign | Tracker |
| `product_ids`, `initiative_ids`, `business_case_ids` | Qualified references; initiative list may be empty | Derived view of dated product, initiative and funding links; inherited and direct sources retained |
| `status`, `owner` | | Tracker |

### 3.3 Story

These fields are the preserved **v1 metadata contract**. New story briefs and
versioned criteria use [section 11](#11-requirements-and-test-analytics-v2).

| Field | Example | Source |
|---|---|---|
| `story_id` | `PROJ-123` | Tracker |
| `title` | Add saved cards | Tracker |
| `size` | `M` or story points | Tracker |
| `assignee` | developer or agent identity | Tracker |
| `status` | `in_progress`, `done` | Tracker |
| `risk_class` | `payments-core`, `ui`, `tests` | Tracker label; used by autonomy rules |
| `type` | `feature`, `bug`, `cve-fix`, `fix-of` | Tracker |
| `fixes_story_id` | optional | Tracker link; used for rework |

### Delivery-flow node metadata (EI-S02)

The delivery-flow engine can now carry **optional context**, not a verified
Insights story record: each executable node's typed `lineage` block may contain
`story_id`, `size`, `type`, `risk_class`, `fixes_story_id`, `team_id`, and
`cohort`. Each supplied value must be a non-blank string of at most 128
characters; unknown fields are rejected. Optional `epic_id` belongs on the
plan's `epic_metadata` entry, not on a derived epic node. Neither field
establishes tracker identity, registry membership, or an Insights project
permission boundary. In particular, a node's declared `risk_class` is not a
verified policy classification: existing autonomy rules do not use node labels
or this untrusted lineage block to grant authority.

Nodes may also carry `labels`, a context map of at most 16 entries with keys
from the checked-in [controlled vocabulary](../../modules/gateway/src/orchestration/lineage_vocabulary.yaml)
(at most 32 characters) and non-blank string values (at most 128 characters).
`lineage_invalid`, `labels_too_many`, `label_key_unknown`, and `labels_invalid`
name invalid metadata in preview/revision validation; malformed lineage shapes
receive a named HTTP 422 schema error. Accepted metadata is stored on the
node row and appears in draft preview and the flow graph's node read model;
metadata-only revisions change the plan hash, not the acceptance gate. Plans
without metadata remain valid. UI chips, worker-prompt projection, and value
registry lookups are separate increments; this block does not by itself make
the later outcome metrics available.

### 3.4 Task (the unit of work that runs)

A Task is **one developer session on the desktop, or one agent run in the cloud**. Directly attributed call cost links through tasks to a story. That story-lifetime drill-down is distinct from EI-05, which includes all in-scope team-period cost, shared allocations and unfinished work. A Jira sub-task is a planning item; it can link to several execution tasks and must not serve as the ID for repeated runs.

| Field | Example | Source |
|---|---|---|
| `task_id` | `TSK-7f3a…` | Stable execution identity generated by the gateway or agent platform; mapped to the source session/run ID |
| `story_id` | `PROJ-123` | Branch name (Stage 1); dispatch (Stage 2+) |
| `executor_type` | `human_session`, `cloud_agent`, `workflow_step`, `nightly_agent` | Gateway / platform |
| `executor_id` | developer or agent identity | Identity provider / platform |
| `session_id` / `agent_run_id` | | Coding tool / ADP Task API |
| `model_ids` | models used | Gateway |
| `started_at`, `ended_at`, `active_minutes` | | Gateway / platform |
| `outcome` | `pr_opened`, `abandoned`, `killed_by_limit` | Platform |
| `transcript_ref` | pointer only | Separate restricted store |

Session duration and `active_minutes` alone do not establish complete human delivery effort. Use the effort evidence in section 3.10, with an approved estimation method and overlap removal.

### 3.5 Call record (cost)

| Field | Example | Source |
|---|---|---|
| `call_id` | | Gateway |
| `provider_request_id` | optional source-native reference | Model provider response or invocation evidence; retained separately from the gateway's call ID |
| `task_id`, `story_id` | | Gateway |
| `model_id`, `serving_path` | `bedrock` or `hyperpod` | Gateway / router |
| `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens` | | Gateway |
| `estimated_cost_usd` | | Gateway price table |
| `reconciled_cost_usd` | | AI Budget Controller, after the daily CUR check |
| `budget_scope` | `story` or `personal_allowance` | Budget Controller |
| `decision` | `allow`, `alert`, `stop` | Budget Controller |

### 3.6 Commit and Pull Request

| Field | Example | Source |
|---|---|---|
| `commit_sha` | | GitHub |
| `task_id`, `session_id` | from commit trailer | Coding tool adds the trailer |
| `story_id` | from branch or PR title | GitHub |
| `authored_by_type` | `human`, `agent`, `mixed` | Trailer / platform |
| `pr_id`, `merged_at`, `reverted_by_pr` | | GitHub |
| `lines_added`, `lines_removed`, `files` | | GitHub |

### 3.7 Release, Deployment, Incident

| Field | Example | Source |
|---|---|---|
| `release_id` | `REL-2026.11.2` | Designated release registry or governed repository release record; a tag or pipeline propagates that identity under the configured authority |
| `story_ids` | Stories linked to this deployment's artifacts | Observed manifest/commit-to-work mapping; may include partial work and is not by itself a shipped-story verdict |
| `deployment_id` | | Pipeline |
| `environment` | `test`, `staging`, `canary`, `prod` | Pipeline |
| `result` | `success`, `failed`, `rolled_back` | Pipeline |
| `incident_id`, `caused_by_deployment_id`, `opened_at`, `resolved_at` | | Incident tool |

A release does not sit above an epic. A story can appear in several releases; record which successful production deployment first satisfies its complete acceptance criteria. Release views aggregate the relevant evidence using each metric's denominator, not by summing story-level rates.

Product ↔ Release is many-to-many. Keep Context's `release_plans_work` (versioned
planned scope) separate from `release_delivers_work` (observed artifact inclusion).
A release can contain only part of a Story; a plan, tag, published artifact or
successful staging deployment cannot count it as shipped. Each deployment attempt
retains its own identity, environment and result. Coordinated cross-Project views
compose authorized records; release actions in the Context v1 design still execute
in one Project.

The table retains the existing v1 Deployment wire fields. Its optional `release_id`
and `story_ids` can be absent in valid legacy evidence; record unresolved links
without inventing them. Planned scope and the business relationship records are
separate adapter inputs, not new fields silently added to that producer schema.

### 3.8 Scenario, Test, Test Run

This table preserves **v1 semantics**: its Test Run is an individual test attempt,
not a regression-pack execution. New producers use section 11's separate
`test_run`, `run_test` and `test_result` contracts. Do not combine these grains in
one run-count metric. Section 11.11 defines the historical mapping rules.

| Entity | Field | Example | Source |
|---|---|---|---|
| Scenario | `scenario_id` | `SCN-0042` | Stable Gherkin tag or test management identity; story membership is a separate link |
| | `story_ids` | many-to-many | Tag on the scenario |
| | `type` | `acceptance`, `regression` | |
| Test | `test_id` | `checkout/saved_cards_test.py::test_expired_card` | Test framework full name |
| | `scenario_ids` | many-to-many | Versioned test annotations or mapping records |
| | `layer` | `unit`, `integration`, `e2e` | |
| Test Run | `test_run_id` | opaque run/attempt record ID | CI/adapter identity qualified by build attempt, job, full test instance and retry; build + short test name alone is insufficient |
| | `test_id`, `commit_sha`, `build_id`, `environment` | | CI report (JUnit XML) |
| | `result`, `duration_ms`, `retry_index`, `initial_result` | Preserve every attempt; a pass after a failure is a retry recovery signal, with flakiness classification recorded separately | CI |
| | `report_ref` | raw file in S3 | CI upload |

### 3.9 Other quality records

| Entity | Key fields | Linked by |
|---|---|---|
| Coverage | `build_id`, `commit_sha`, `line_coverage`, `delta_vs_base` | Commit SHA, PR |
| Scan finding | `finding_id`, `tool`, `severity`, `opened_at`, `closed_at` | Commit SHA, PR |
| Review | `review_id`, `pr_id`, `reviewer_type` (human / agent), `decision`, `review_minutes` | PR |
| Quality gate result | `gate_id`, `pr_id` or `deployment_id`, `result`, `reason` | PR or Deployment |
| Eval | `eval_id`, `dataset_version`; per run: `model_version`, `prompt_version`, `score` | Model version, Story (where relevant) |

### 3.10 Evidence for human effort and allocated cost

These proposed records support EI-05 and EI-06 in the overview. Their adapter schemas remain to be implemented.

| Record | Required evidence | Purpose |
|---|---|---|
| Human effort estimate | `effort_record_id`, restricted actor reference, `team_id`, work links where available, activity type, interval or sampled period, estimated active minutes, `method_version`, coverage and uncertainty | Cover planning, implementation, prompting, supervision, review, validation and correction; deduplicate concurrent intervals and allocate effort only once |
| Non-call cost allocation | `cost_allocation_id`, period, `team_id`, amount, currency, cost category, invoice/ledger reference, `allocation_policy_version`, reconciliation status | Include licences, desktop/runtime and shared platform costs once, without adding invoices to the same metered usage again |

Retain unlinked in-scope cost and effort in team-period totals, expose their missing work links, and withhold conclusions requiring attribution until the coverage checks pass. EI-06 supplies the labor component of EI-05.

### 3.11 System-of-record configuration

Use the [approved enterprise mapping in overview section 5.5](agentic-engineering-insights-overview.md#55-recommended-systems-of-record-for-a-typical-enterprise) as the onboarding default. Store the customer's actual choices as versioned configuration. The following fields describe the proposed authority configuration; they are not additional required fields on every raw event.

| Configuration field | Purpose |
|---|---|
| `authority_mapping_id`, `mapping_version`, `valid_from`, `valid_to` | Stable identity and effective history of the ownership decision |
| `tenant_id`, `project_id`, `entity_type`, `source_namespace` | Scope of the decision, including provider instance, repository or catalog namespace where relevant |
| `authoritative_source_connection_id`, `native_id_field` | Registered connection that owns the ID and the source field that carries it |
| `business_owner`, `technical_owner` | Accountable business function and connector/data steward, recorded as approved role or team references |
| `authoritative_fields`, `relationship_authorities` | Which attributes and relationships the system controls; ID ownership does not imply ownership of every associated fact |
| `conflict_resolution_rule`, `exception_owner` | Precedence and review responsibility for conflicting values, stale copies or ambiguous mappings |
| `alias_mapping_ref`, `migration_rule` | Retain renamed keys and mappings during source-system migrations without silently merging unrelated records |

#### Ownership and conflict rules

- **One authority per scoped ID type:** require a single effective authority for the same entity/namespace and interval. Multi-tool enterprises can register separate namespaces. Do not choose a winner merely because its event arrived most recently.
- **Work ownership:** the selected tracker owns Epic/Story identity and dated backlog relationships. Branch names, commit trailers and PR fields carry references. Preserve discrepancies and route unresolved conflicts to the configured owner.
- **Investment and financial ownership:** the investment approval system owns its Business Case ID and approval state. The AI Business Case Registry retains that reference and owns the registered AI baselines and targets. Finance ERP owns funding/WBS references and posted actuals. The Budget Controller owns reconciled delivery-cost attribution derived from usage, invoices and approved allocation rules, with provenance to each source.
- **Execution ownership:** the session/run platform owns the source execution ID and runtime state; the gateway owns its call ID. Keep provider request IDs and normalized Insights record IDs distinct. The mapping connects them without turning a planning sub-task into an execution run.
- **Release and deployment ownership:** the designated release registry owns release identity and manifest. The deployment system owns the executed deployment ID and result. Approval/change records link to deployments; approval alone does not establish that production changed.
- **Incident and test ownership:** resolve alerts to the designated incident record and managed test cases to versioned executable tests. Preserve each native identity, attempt and retry. Similar titles or a common short test name are insufficient to merge records.
- **Scope and source availability:** ADP tenant authority and the Insights control plane retain ownership of authorization scope. A copied product, project or tenant ID cannot grant access. If the authoritative source is unavailable, retain the latest known version with freshness status and withhold conclusions requiring a fresh value; do not silently promote a downstream copy to authority.

---

## 4. Relationships

| From | To | Cardinality | Stored as | Changes over time? |
|---|---|---|---|---|
| Portfolio | Product | Optional n → n | Catalog-owned `portfolio_product`; one effective membership per logical pair, projected into authorized Projects | Yes, dated immutable versions |
| Product | Project | 1 → n current Profile scopes | Registry `product_project`; v1 permits one managing Product per Project, not exclusive work attribution | Yes, immutable dated versions |
| Product | Initiative | n → n | Scoped associations; derive from Contribution commitments when present | Yes, with valid-from / valid-to dates |
| Initiative × Product × Project | Contribution | At most 1 current per tuple in Context v1 | Source-owned commitment with typed refs | Yes, append revisions |
| Contribution | Epic / Story | n → n | `contribution_work` references the existing backlog | Yes, dated work links |
| Product | Epic | n → n | Explicit product attribution links, including epics with no initiative | Yes, with dates |
| Product | Story | n → n | Explicit attribution exceptions to the epic mapping, with precedence recorded | Yes, with dates |
| Initiative | Epic | n → n | Delivery-grouping links; no link required when initiative is not applicable | Yes, with dates |
| Business Case | Initiative | n → n | Funding/target links from the Registry | Yes, with dates and allocation policy |
| Business Case | Epic | n → n | Direct funding/target links; retain provenance separately from inherited links | Yes, with dates and allocation policy |
| Epic | Story | 1 → n | link table | Yes, with dates (stories move) |
| Story | Task | 1 → n | `story_id` on Task | No |
| Task | Call record | 1 → n | `task_id` on Call | No |
| Task | Commit | n → n | Provenance links from trailers and PR/platform metadata; preserve mixed contributions | Corrections appended |
| Commit | PR | n → n | Provider-qualified commit/PR links, including squash or cherry-pick provenance where available | Corrections appended |
| Product | Release | n → n | `product_release`, authorized registry/plan associations | Yes, dated versions |
| Release | Story (planned) | n → n | `release_plans_work`, exact plan revision | Yes, append plan changes |
| Release | Story (observed) | n → n | `release_delivers_work`, observed manifest/artifact evidence; inclusion can be partial | Immutable evidence; append corrections |
| Release | Deployment | 1 → n | `release_id` on each attempt when resolved; retain legacy unresolved links | Preserve attempts and results |
| Deployment | Incident | 1 → n | `caused_by_deployment_id` | Set during the incident review |
| Story revision | Criterion revision | 1 → n | Exact story reference; stable story-qualified criterion IDs | Append revisions |
| Criterion revision | Scenario revision | n → n | v2 `coverage_mapping` | Append revisions |
| Scenario revision | Test definition revision | n → n | v2 `coverage_mapping` | Append revisions |
| Test Run (v2 pack) | Run test | 1 → n | Intended selection and pinned definition/requirement references | Explicit plan amendments |
| Run test | Test result | 1 → n | One result per attempt; corrections append revisions | Preserve retries |
| Acceptance assessment | Criterion revision / Test results | n → n | Exact evidence references, accountable assessor and policy | Append assessments |
| Commit | Test Run, Coverage, Scan finding | 1 → n | v2 run `source_revision`; legacy quality records `commit_sha` | No |

### 4.1 Business-context links and rollups

Reuse Context's relationship contract: `relationship_id`, `source_connection_id`,
`relationship_revision`, optional `supersedes_revision`, qualified `from_ref` and
`to_ref`, `relationship_type`, `valid_from`, optional `valid_to`, `recorded_at`,
`source_refs` and `mapping_version`. Corrections append a superseding version.
Effective time says when a link applied; recorded time says when it became known.
`mapping_version` versions the attribution/normalization rule, not the link, a
Profile concurrency token or a normalized record revision. Any cost allocation
also records its policy version and allocation basis.

- **Product attribution:** resolve each epic's explicit product links, then its stories using dated Epic → Story membership. Initiative membership supplies delivery context; membership in an initiative spanning several products does not automatically attribute every story to every product. Record approved story-level exceptions as explicit scoped Product → Story links with the same dated fields.
- **Optional initiative:** `not_applicable` is valid for routine work. Reserve `unknown` for an expected but unresolved mapping. Missing optional context must not quarantine otherwise valid execution evidence.
- **Funding:** resolve direct epic funding and funding inherited through an initiative, retaining both paths. Deduplicate a case reached through several paths. A business-case relationship alone does not authorize charging the full cost to every case.
- **Aggregation:** deduplicate facts by their canonical identities before computing metrics. Allocate shared cost and human effort once under an approved policy, with any unallocated remainder visible. Product/initiative views can overlap; label this and do not sum their counts, rates or benefits. For additive portfolio counts, choose a single primary product/initiative attribution for each story and retain secondary associations for filtering. Keep the overview's six metric definitions and denominators.
- **History and access:** resolve context using the event's effective date and retain the mapping version. Publish restated history explicitly when mappings are corrected. Cross-project reporting checks access to every contributing Insights project; business links cannot widen access.
- **Onboarding and finance:** Profile completeness, maturity, lifecycle and a proposed budget are context, not delivery acceptance, reconciled cost or spending authority. Missing optional links do not block a first save or invalidate otherwise admissible raw evidence.

---

## 5. How each ID gets attached, by stage

| ID | Stage 1: Story level (desktop) | Stage 2: Epic level (cloud agents) | Stage 3: Release level |
|---|---|---|---|
| Product / Initiative IDs | Resolve product catalog and dated tracker mappings; initiative can be not applicable | Carry the validated context into dispatch with its mapping version | Resolve release stories to their dated product/initiative links; a release can span products |
| Story ID | Branch name (`PROJ-123-…`), enforced by a branch rule and PR template | The story starts the agent run, so it's always present | Inherited from commits |
| Task ID | Gateway generates one per session | ADP Task API generates one per agent run | Nightly runs get one per run |
| Session / run ID → commit | Coding tool adds a trailer | Platform adds it automatically | Platform adds it automatically |
| Epic ID | From the tracker link | From the tracker link; the planner agent creates stories under the epic | Unchanged |
| Release ID | Designated release registry or governed repository release record | Same authority; included in the manifest | Pipeline propagates the release identity at promotion and records a separate Deployment ID |
| Scenario / Test ID | Developers tag tests | Agents are asked to tag tests from acceptance criteria | Regression suites tagged |
| Typical link rate (illustrative) | 85% → 95% | ~99% | ~100% |

### 5.1 Where each ID is minted in ADP

The [project-registry identity chain (#5411 §11)](https://github.com/aws-e/adp/issues/5411)
is the authority for the ADP-specific attachment rules below. The existing
IDs listed here are not evidence that the proposed registry or Insights join
is deployed.

| ID | Minting authority | Current versus target attachment |
|---|---|---|
| `project_id` | Project registry (#5411); never minted by Insights or derived from a repository name | **Target (EI-S03):** bind each delivery flow to one registered project and inherit it on emitted records (EI-S04) |
| Optional `portfolio_id` | Designated catalog/portfolio authority or governed direct Context entry | **Target (#8149, #7295):** dated Product membership for business views; no new mandatory producer field or evidence scope |
| `product_id`, optional `initiative_id`, `business_case_id` | Product catalog or registry; tracker/PMO or registry; business-case registry, respectively (#5411 §11) | **Target (#5411):** dated product, initiative and funding links resolved at event time; `not_applicable` is valid for an unused initiative |
| Contribution identity | Context/portfolio authority (#8149 and #5411 shared model) | **Target (#7295):** resolve the Product's commitment and selected native work through a versioned context adapter; no new mandatory producer field |
| `epic_id`, `story_id` | Work tracker (source-native IDs) | **Current:** tracker issues and links exist. **Target (EI-S02):** typed node lineage carries `story_id` rather than relying on issue-body comments; retain qualified tracker identity |
| `flow_id`, node ID; `task_id` | Delivery engine; gateway Task API (or gateway desktop session), respectively | **Current:** engine and gateway mint these IDs. **Target (EI-S03, EI-S04):** carry the registered project and story through flow, node, task and emission |
| `call_id`; commit/PR IDs | Gateway usage ledger; source-control provider, respectively | **Current:** each owner records its native ID. **Target (EI-S04):** join the call and accepted commit/PR to the task with qualified source and project context |
| `release_id`, `deployment_id` | Designated release registry; deployment receipt producer | **Current:** engine-dispatched deployments have receipts. **Target (#5411, EI-S04):** bind release and deployment evidence to the project and shipped stories |

### 5.1.1 Product execution Projects and budget attribution

Each Product has one base Project in the canonical Project registry. A Product
flow, planning session or human-submitted Task uses that Project unless an
explicit active Project from the same Product is selected. A Project is execution
and budget scope; an Initiative remains an optional cross-product business link.

The Gateway captures `tenant_id`, `product_id`, `project_id`, the Product–Project
`relationship_revision`, and (when applicable) `run_id` and `flow_id` in trusted
execution attribution. Retries retain the captured Project. A started flow cannot
move to another Project. Usage rows retain these IDs, so a later Product–Project
relationship change does not relabel financial history. Pre-existing usage with
no captured attribution remains unallocated.

Project AI budgets cover flows and standalone agents together. Product spend is
an aggregate of captured usage, with no second debit at Product level. This is
separate from financial planning allocations, infrastructure costs and source
system budgets. These Gateway attachments implement the local execution side;
Insights producers must still export qualified records using the source identity
and revision rules above. See [Project budgets](../product-onboarding/project-budgets.md).

### 5.2 Profile and Context integration contract

**Target design, not an admitted producer or deployed adapter.** The
[Profile API](../product-onboarding/design/api.md),
[shared model](../product-onboarding/design/data-model.md) and
[Context alignment](../product-context/lineage-alignment.md) supply inputs for an
Insights-owned, explicitly versioned registry/relationship adapter under #7295.
The existing v1/v2 schemas continue to admit only their documented producer types.

| Input owner | Source contract | Insights responsibility |
|---|---|---|
| Profile / registry (#8141) | Product/Project identity, scoped source selections, dated team context and immutable `product_project` versions | Read authorized changes/revisions; retain identities and resolve the correct historical projection |
| Scope integration (#8148; EI-S03/EI-S04) | Exact canonical `project_id` inherited by flow, task and producer records | Validate scope without creating another Project ID or inferring it from a repository |
| Context / portfolio (#8149; #5411) | Portfolio membership, Initiative, Contribution, work-attribution and authority mappings | Consume source-owned dated links; preserve native tracker IDs and explicit source-to-Insights revision mappings |
| Release services (#7628) and deployment producers | Release plans, exact manifests/artifacts, attempts and observed outcomes | Keep planning and execution separate; apply the existing shipped-story/production definitions |
| Finance / Budget Controller | Funding approval, allocation policy and reconciled ledger actuals | Deduplicate actual cost/effort using approved attribution; never substitute Profile planning amounts |

The adapter must implement these rules before activation:

1. **Read through owned APIs.** Use scoped `listChanges` and `getRevision`, not
   gateway tables. A Profile change's `event_id` is its immutable
   `product_changes.id`; pair it with the aggregate `revision` for frozen readback.
   Profile's Product/Project exports use that change ID as `record_revision`.
   Existing records keep their exact references. Any further adapter normalization
   pins a separate normalized revision and preserves its source reference and
   mapping version. Two Products' aggregate revision 1 cannot collide when a Project
   changes managing Product.
2. **Preserve relationship history.** Consume complete `relationship_versions`
   from the change feed; never rebuild past links from current binding rows.
   Resolve both effective time and what was known at the decision time. An unlink
   or rebind does not move old execution facts, rewrite their Project IDs or infer
   new business attribution. Snapshot expiry produces an explicit gap/410; retained
   link versions do not authorize reconstruction of expired record content.
3. **Version admission explicitly.** Before publication, #7295 must supply the
   named registry/relationship producer schema, source-field mapping, endpoint/type
   checks, publisher grant and compatibility tests. A Context snapshot, Profile
   response or `Change` envelope is not an Insights event. Generic business records
   cannot use v2 `coverage_mapping` or be admitted by matching `schema_version` alone.
4. **Retain provenance and replay identity.** Preserve source namespace,
   `source_event_id`, actual source `event_time`, ingestion time and mapping version.
   For registry mutations the recorded commit is the source event; a backdated
   `valid_from` remains separate. External source event times are not invented from
   a context read. Deduplicate by tenant, Project, source event, type, logical ID
   and immutable revision; same identity/revision with different bytes is a
   conflict. Mapping-rule changes produce new normalized revisions, not rewrites.
5. **Enforce current access.** Reauthorize every page, revision and replay; filter
   revoked source details and withhold inaccessible records. A partially readable
   snapshot cannot replace an emitted immutable record under the same reference.
   Raw v2 references remain same-tenant and same-Project. Cross-Project business
   views query each authorized scope and join approved namespace/alias mappings;
   they do not clone execution facts or infer identity from matching labels.
6. **Keep readiness honest.** Expose mapping/source coverage and adapter
   unavailability. A pending Initiative or missing registry adapter does not block
   saving a Product or ingesting otherwise valid producer evidence. Existing v1
   Tasks still require Story; non-story execution needs its own versioned adapter,
   never a fictional Story added to satisfy validation.

Required integration acceptance includes the existing
[Checkout](../product-context/examples/cross-product/checkout.json) and
[Accounts](../product-context/examples/cross-product/accounts.json) Contributions
to one Initiative, Project rebind/history replay, partial release inclusion,
duplicate work/cost attribution, correction of a dated link, and loss of access to
one participating Project. The last case must hide its identities/counts and mark
the aggregate as incomplete; it cannot report an overall Initiative as delivered.
Portfolio cases include a Product in two collections, the same Portfolio projected
in two Projects, no Portfolio membership, and a corrected/moved membership without
rewriting historical work or double-counting combined totals.
These are adapter acceptance obligations, not claims of live verification.

---

## 6. Data checks

### 6.1 On arrival (each event)
- **Required fields present:** required scope, source and entity keys for that record type, plus its required relationship keys. Quarantine missing required evidence with a reason. Optional initiative membership is not a required event key; unresolved product attribution is a visible context gap and does not erase valid raw evidence.
- **No duplicates:** deduplicate within tenant, project and source connection using the immutable source event identity. If one event yields several records, include record type and source-native entity ID. Preserve revisions and retries as distinct evidence.
- **Known IDs:** the parent must exist in the registry, for example a Task's story must exist in the tracker copy. If not, the event is held and retried after the next tracker sync.
- **Authoritative source:** resolve the scoped ID through the effective authority mapping in section 3.11. Preserve source-native identity and conflicting references; quarantine ambiguous joins without treating a downstream copy as the owner.
- **Sensible values:** no negative cost, no end before start, timestamps not in the future.

### 6.2 Daily
- **Orphan and context checks:** v1 Tasks with no required Story; unresolved expected Epic membership; work without expected Product attribution; investment work with missing direct/inherited funding. A Story deliberately authored without an Epic and routine work without an Initiative are valid. Show Product, Contribution, expected Initiative and funding coverage separately from the story-linked rate.
- **Cost check:** reconcile AWS cost against the relevant AWS bill and non-AWS subscriptions against their own invoices. Include shared allocations once and disclose unlinked spend.
- **Deployment check:** deployments recorded vs the pipeline's own history.
- **Status check:** merged PRs vs story status in the tracker.
- **Freshness:** time since the latest successful source sync or heartbeat, with billing lag disclosed separately. A quiet source is not automatically stale.

### 6.3 Data trust panel (shown next to every number)

| Check | Definition | Threshold (example) |
|---|---|---|
| Linked | Events with a valid Story ID ÷ all events | ≥ 95% |
| Business context | Share of eligible work with resolved product attribution, expected initiative context and funding links, reported separately | Agree coverage before product/initiative ROI verdicts; omit not-applicable initiatives from the expected-link denominator |
| Spend captured | Reconciled ledger cost ÷ independently billed cost for the same scope and period; check AWS and non-AWS separately | ≥ 98%, with variance explained |
| Tests reported | Merged PRs with test runs attached ÷ merged PRs | ≥ 95% |
| Orphans | Records missing a required or expected parent; deliberately omitted optional Epic/Initiative links excluded | ≤ 2% |
| Human effort covered | Share of in-scope activity represented by the approved estimate, checked against a calibration sample | Coverage and uncertainty approved before a cost/capacity verdict |
| Fresh | Time since the latest successful source sync or heartbeat, excluding disclosed billing lag | < 1 hour for operational sources |

**Rule:** follow the overview's [data-trust rules](agentic-engineering-insights-overview.md#64-layer-4-data-trust-shown-next-to-every-number). Apply each check to the inputs the metric requires. Show **Provisional** estimates with coverage when evidence is incomplete, and withhold firm success verdicts or autonomy promotion based on them. Show **Not enough data** when essential sources, denominators or complete observation windows are unavailable. Missing records never imply zero incidents or zero human effort.

---

## 7. Evidence for the six scorecard metrics

The overview's [section 6.1.1](agentic-engineering-insights-overview.md#611-exact-definitions-and-collection-methods) owns the exact formulas, eligible populations, reporting windows and allocation rules. This table defines which lineage records support each metric. Version the metric pack and rebuild comparisons from retained evidence when definitions change.

| Metric ID | Scorecard metric | Evidence path and measurement level |
|---|---|---|
| EI-01 | Lead time per story | Story → linked commits → release manifest → first successful production deployment completing the story; median over the eligible shipped-story cohort |
| EI-02 | Stories shipped per team per week | Story completion evidence plus dated team ownership; distinct first shipments divided by weeks in the team's reporting period |
| EI-03 | Change failure rate | Production deployments plus confirmed causal rollback, emergency-fix and incident links; distinct deployment denominator with a complete 7-day observation window |
| EI-04 | Rework rate | First-shipped story → corrective PR/revert or `fixes_story_id` record, with reason; distinct story denominator with a complete 30-day observation window |
| EI-05 | Total cost per story | Reconciled calls + non-call cost allocations + EI-06 effort × approved loaded rate; all in-scope team-period cost divided by distinct stories first shipped in that period |
| EI-06 | Human effort per shipped story | Approved effort estimates covering all delivery activities, calibrated and deduplicated; all in-scope team-period human hours divided by the same distinct first-shipped-story count |

Unfinished, abandoned and corrective work remains in the EI-05/EI-06 numerators. Directly attributed lifetime cost for one story is a supporting drill-down and need not equal the team-period unit cost. Record human, agent and mixed contributions without forcing mixed work into a binary category. Report team aggregates and restrict individual evidence under the organization's privacy and employee-representation requirements.

---

## 8. Storage on AWS

The table below is the broader ROI storage target. For the initial requirements
and test analytics implementation, [architecture section 3.1](architecture.md#31-requirements-and-test-analytics-storage-decision)
selects private S3 snapshots/JSONL, curated Parquet and DuckDB. S3 Tables/Athena
remain a later governed-table option; raw files do not automatically populate
Iceberg tables. That decision does not alter the existing module infrastructure.

| Layer | What | Service |
|---|---|---|
| Raw files | JUnit XML, SARIF, coverage reports, webhook payloads | Amazon S3 (lifecycle to cheaper tiers; longer retention for payments code) |
| Evidence tables | One table per entity in section 3, append-only | Amazon S3 Tables (Apache Iceberg) with time travel |
| Live lookups | Scoped product/initiative/funding mappings, story → epic, task → story and ID registry for the linker; dated history retained in evidence tables | Amazon DynamoDB |
| Quarantine | Events that failed a check, with the reason | S3 Tables |
| Metrics | Versioned views for the six scorecard metrics and stage detail | Amazon Athena views, AWS Glue jobs |
| Access | Team-level by default; restricted individual and transcript access | AWS Lake Formation |
| Dashboards | Story, epic and release cards; data trust panel | Amazon Quick Sight |
| Transcripts | Agent prompts and outputs | Separate S3 bucket, tighter access; linked by `transcript_ref` |

---

## 9. Worked example: one story traced end to end (illustrative)

This example shows the record-level evidence for a single story. For the complete business-case approval, enterprise-system handoffs and pilot-team scorecard, see the overview's [checkout modernisation scenario](agentic-engineering-insights-overview.md#14-worked-scenario-business-case-to-roi-scorecard). The scope labels below are illustrative; a real deployment must preserve the same qualified project and source references throughout its own trace.

```
Scope: tenant-example / INS-PROJ-01 (Insights evidence project)

INIT-2026-014 Checkout modernisation, funded by BC-2026-014
 ├─ Accounts Contribution (separate authorized Project; independent release)
 └─ Checkout Contribution → PRD-CHECKOUT + INS-PROJ-01
     └─ CHK-12 Checkout redesign
         └─ PROJ-123 Add saved cards            shipped in REL-2026.11.2
             ├─ TSK-01 human_session  (developer-01, 52 min)    $4.10   commits a1f…, b7c…
             ├─ TSK-02 cloud_agent    (agent run 9f2)     $7.80   commit c3d…
             │
             ├─ SCN-0041  store card on success
             │    └─ test_save_card ─ 6 runs, all pass on c3d…
             ├─ SCN-0042  reject expired card
             │    └─ test_expired_card ─ 8 runs, 1 flaky retry, pass on c3d…
             ├─ Coverage +3% · 0 new findings · review 4h (human)
             └─ REL-2026.11.2 → staging ✓ → canary ✓ → prod ✓ · no incident in 7 days

Lead time 2.1 days · linked AI call cost $11.90
```

All IDs and values above are fictional. The 52-minute session and four-hour review do not establish complete, non-overlapping human effort. Total delivery cost and EI-06 remain pending until planning, supervision, validation, shared-cost allocation and effort coverage are established. No incident in seven days is evidence for this deployment only when the incident source is complete. The 30-day rework result is still pending. A single story illustrates traceability; it is not the full team scorecard.

---

## 10. Open questions

1. **Task in Jira or not:** should desktop sessions appear as Jira sub-tasks, or only in our system? Recommendation: only in our system, so developers don't have to create sub-tasks.
2. **ADP fit:** confirm that ADP's Task API can carry `story_id` and return a run ID usable as `task_id`.
3. **Scenario tooling:** Gherkin tags, a test management tool, or test annotations only. Pick one per organization.
4. **Commit trailers:** they can be removed by squash merges. Fall back to PR-level metadata, and count trailer loss in the Linked check.
5. **Loaded rate:** agree one rate with finance for metric 5, and keep it fixed across stages so the comparison stays fair.
6. **Identifier and adapter contracts:** approve scoped key serialization, source-alias rules, run/job/test-attempt identities, required fields and correction semantics against the shared service design before implementation.
7. **Effort and allocation contracts:** define the estimation method, overlap removal, calibration, coverage and finance-approved allocation rules supporting EI-05 and EI-06. Use the overview's six metric definitions and evidence thresholds throughout.
8. **Product and initiative mappings:** select the authoritative catalog/tracker and mapping owners. Approve primary attribution, story-exception precedence, shared funding/cost allocation and the expected coverage for product/initiative verdicts before publishing portfolio totals.
9. **Systems of record:** confirm the section 3.11 authority configuration, business/technical owners and conflict rules for each customer's namespaces. The overview's vendor examples are recommended choices, not assertions about an existing customer deployment.

## Stage-1 repository task-board producer

At each checkpoint, the Codex controller writes `.adp/tasks/<issue>.json` on
the story branch. Version-2 entries contain `started_at`, `finished_at`,
`run_id`, `attempt`, and a credential-masked `checkpoint_note` limited to 400
characters; `pattern_id` is optional. The header keeps `updated_at` and holds
the dispatch `flow_id` and graph `node_address` when available. Null timestamps
mean not started or not finished; null lineage means metadata was unavailable;
zero attempts mean no observed attempt. The reviewer reads these fields
without changing the existing UI read model. Version-1 boards remain readable
with absent fields defaulted in memory and no file rewrite. This repository
history is evidence for later ingestion, not a replacement for the platform's
execution-run identity or a deployed Insights service.

## 11. Requirements and test analytics v2

**Contract status:** additive, machine-readable `lineage/v2` contracts and offline
validation fixtures are checked in under `modules/insights/schemas`. Ingestion,
GitHub issue synchronization, DuckDB queries and acceptance evaluation remain
proposed services. Sections 3.3 and 3.8 preserve the existing v1 field definitions
for historical consumers. New requirements/test analytics use this section;
existing delivery, task, call, PR, deployment and incident lineage stays intact.

```text
Story revision → Acceptance criterion revision ↔ Scenario revision ↔ Test definition revision
                                                      ↑
Module revision → Feature revision ────────────────────┘

Test run → Run test (planned instance, pinned definitions and criteria)
                  → Test result (one attempt) → Artifact references

Acceptance assessment → Criterion revision + Result references / manual evidence
```

Criteria–scenario and scenario–test links are many-to-many, explicit, versioned
records. A criterion can require several scenarios; one test can support several
criteria. Executing that test counts once per instance/attempt, not once per link.
Do not infer acceptance from a tracker checkbox, a closed story, an uploaded
report or an eventual passing retry.

### 11.0 Standard v2 envelope

The scoped source fields from section 3.0 apply, with `schema_version = 2.0`.
Every v2 record additionally has:

| Field | Meaning |
|---|---|
| `record_type` | One of the v2 record types below; schema discriminator |
| `record_id` | Stable logical identity within tenant, project, connection and record type |
| `record_revision` | Immutable version of that logical record; never a moving `latest` reference |

A revision does not create another logical test or another execution attempt.
Canonical keys are `(tenant_id, project_id, source_connection_id, record_type,
record_id, record_revision)`. A run rerun has a new logical run identity, derived
from its source run and source attempt. Run state transitions instead append
revisions of the same logical run. References to an admitted run revision remain
valid after completion; current-run views group by the stable scoped identity.

Source events deduplicate by scoped delivery ID, record type, ID and revision.
Same identity/revision with different bytes is a conflict to quarantine, never
an overwrite. Identical redeliveries do not create additional results. Hash the
canonical normalized payload to detect conflicts, retaining parser version and
raw-byte hashes in the publication manifest. Do not use ingestion time as a
source revision or interpret arrival order as tracker edit order.

### 11.1 Versioned references

All record references use the following shape; source-native IDs alone are not
sufficient for joins. Artifact references select service-owned, exact objects.

| Field | Meaning |
|---|---|
| `tenant_id`, `project_id` | Exact same scope as the containing record; cross-project reporting aggregates authorized views, not cross-scope raw links |
| `source_connection_id` | Registered source of the referenced record; may differ from the containing record's connection within the same project |
| `record_type`, `record_id`, `record_revision` | Exact typed record version |
| `artifact_id`, `version_id`, `sha256` | Artifact reference fields: opaque authorized artifact ID, actual immutable object version, SHA-256 of retained bytes |

The service resolves artifact IDs to private S3 objects and checks project
ownership, immutable version and digest. An artifact ID or hash grants no read
permission. The offline validator rejects cross-tenant/project record references
and backwards execution timestamps, but does not resolve references or authorize
publishers. The future ingestion service must also validate reference existence,
connection grants, artifact ownership and graph semantics before accepting joins.

### 11.2 Story revision (`story_revision`)

| Field | Meaning |
|---|---|
| `title`, `brief` | Tracker-authored title and complete story brief for this revision; an absent brief is explicitly empty |
| `source_url` | HTTPS display link; not an instruction for a worker to fetch an arbitrary URL |
| `source_updated_at` | Source update timestamp, distinct from event and ingestion times |
| `source_action` | `created`, `edited`, `closed`, `reopened`, `deleted`, `transferred` or `reconciled` |
| `story_state` | Source state; closed/done does not imply acceptance |
| `source_snapshot` | Artifact reference to exact retained issue payload bytes |
| `criteria_extraction_status` | `complete`, `missing` or `ambiguous`; no inferred requirements from ambiguous prose |

GitHub remains authoritative for authoring. Use provider-stable issue and
repository identities, retaining number/URL as display metadata in the raw
snapshot. Scope capture to registered repositories and configured story filters.
Verify webhook signatures and delivery identities; reject unregistered sources.
Capture creation, edits, closing, reopening and available deletion/transfer events.
Reconcile periodically using API cursors and backfill existing stories. Missing
webhooks leave visible freshness gaps; a reconciliation snapshot proves only the
state fetched, not every intervening edit. Never label a later API response as
an earlier webhook's exact snapshot. Retain both observations when they differ.

Raw JSON snapshots and normalized JSONL are immutable S3 evidence; normalized
Parquet is a rebuildable projection. Changes to issue scope or transfers require
rechecking ownership; they cannot move evidence into another project implicitly.
Deletion/offboarding follows the shared retention policy, including old object
versions and analytical snapshots; immutable history is not a promise of
permanent retention against that policy.

### 11.3 Acceptance criterion (`acceptance_criterion`)

| Field | Meaning |
|---|---|
| `story_ref` | Exact `story_revision` owning this criterion version |
| `criterion_key` | Human-readable key such as `AC-001`, unique within the story |
| `text` | Verifiable expected behavior, retained verbatim from the designated criteria block |
| `lifecycle_status` | `active` or `retired`; removed criteria retain historical versions |

Use a story-qualified logical ID, e.g. `issue-42/AC-001`; another story's AC-001
is different. Preserve the logical ID for wording edits and issue transfers that
retain source identity. New criteria get new IDs; retired IDs are never reused.
Pin criteria to the exact story revision, appending a criterion revision when
that binding or its text changes. An issue template should require explicit IDs.
Missing or duplicate IDs produce `missing`/`ambiguous` extraction, not guessed
stable identities. The immutable requirements manifest enumerates the expected
active criterion revisions; omission cannot be treated as complete acceptance.

### 11.4 Coverage definition (`coverage_definition`)

| Field | Meaning |
|---|---|
| `definition_kind` | `module`, `feature` or `scenario` |
| `name` | Human-readable definition name |
| `parent_ref` | Null for a module; exact module parent for a feature or feature parent for a scenario |
| `action`, `expected_result` | Required nonempty text for scenarios; nullable for module/feature definitions |

This is the ADP-compatible optional catalog hierarchy, not a requirement to adopt
ADP IDs. An adapter for another hierarchy must preserve native identities and
explicitly map it. The catalog snapshot enumerates the versions of definitions
and mappings used. Reject cyclic or wrong-kind parent links when resolving them.

### 11.5 Test definition (`test_definition`)

| Field | Meaning |
|---|---|
| `name` | Human-readable test name |
| `source_path`, `selector`, `framework` | Exact stored test location and native selector; manual definitions reference their versioned procedure |
| `layer` | `unit`, `integration`, `e2e`, `manual` or `check`; a placeholder is not an implemented test |
| `source_revision` | Immutable source revision containing the definition |

Definition revisions preserve historical selectors and implementations. Coverage
counts distinguish E2E, local/check and manual tests. Missing implementation is a
catalog gap, not a synthetic passing test definition.

### 11.6 Coverage mapping (`coverage_mapping`)

| Field | Meaning |
|---|---|
| `mapping_kind` | `criterion_scenario` or `scenario_test` |
| `from_ref`, `to_ref` | Exact criterion → scenario or scenario → test versions, respectively |
| `lifecycle_status` | `active` or `retired`; revisions preserve mapping history |

Ingestion must confirm that coverage endpoints are scenarios rather than modules
or features. A mapping declares coverage intent, not a successful execution or
an acceptance policy. Planned scenario/criterion bindings must be supported by
the run's pinned mapping snapshot; later mapping edits do not change that run.

### 11.7 Test run (`test_run`)

| Field | Meaning |
|---|---|
| `source_run_id`, `source_attempt` | Provider-native pack/workflow run and positive attempt number |
| `source_revision`, `environment` | Tested code revision and project-scoped target; source revision is not proof of every deployed component digest |
| `execution_status` | `queued`, `running`, `completed`, `cancelled` or `abandoned`; completion is not a passing or accepted verdict |
| `started_at`, `ended_at` | UTC timestamps or null when not observed; a terminal observation requires an end time |
| `selection_status` | `complete` only with an authoritative intended-selection manifest; otherwise `unknown` |
| `catalog_snapshot`, `requirements_snapshot`, `selection_snapshot` | Exact artifact references, nullable when unavailable; missing snapshots make the corresponding coverage/completeness unknown |

One regression pack execution is one logical run, containing many tests. Keep
provider job/suite identities in instance IDs and retained reports; do not turn a
job summary into individual test results. Register the run/plan before execution
when possible. A completion reconciler records cancelled/abandoned observations;
provider/reconciler timing provenance must distinguish observed timeout from
actual process end. Historical imports disclose missing selection/requirements.

### 11.8 Planned test instance (`run_test`)

| Field | Meaning |
|---|---|
| `run_ref`, `test_ref` | Exact run and test-definition versions |
| `instance_id` | Stable instance identity including suite/job/matrix and parameter variant within this run |
| `parameter_digest` | Nullable SHA-256 of canonical non-secret instance parameters; never hash secret values as a substitute for omitting them |
| `required` | Whether the selection policy requires this instance |
| `scenario_refs`, `criterion_refs` | Exact coverage and requirement versions this instance is intended to exercise |
| `requirement_binding` | `complete` (nonempty known criterion bindings), `unknown`, or explicitly `not_applicable` |

The selection manifest enumerates planned instances and its hash is pinned by the
run. Freeze the plan before execution or record explicit plan amendments with
provenance; do not reconstruct a complete plan from only the passing results.
Selection completeness requires matching every manifest entry to a `run_test`
record. Run-level requirement coverage also requires a requirements snapshot;
nonempty bindings on one test do not prove the story's entire coverage.

### 11.9 Test result (`test_result`)

| Field | Meaning |
|---|---|
| `run_test_ref` | Exact planned test instance, resolving its run, definition and requirement versions |
| `attempt` | Positive execution-attempt number within this instance; retries create additional results |
| `outcome` | `passed`, `failed`, `error`, `skipped`, `blocked` or `cancelled` |
| `started_at`, `ended_at`, `duration_ms` | Observed timing, nullable when unavailable; duration cannot be negative |
| `failure_category` | Nullable adapter classification, retaining raw detail in evidence |
| `artifact_refs` | Exact reports/logs/screenshots; private state and credentials remain outside publishable evidence |

Unique execution keys include the scoped planned-instance identity and attempt;
corrections append revisions of that result. A retry does not overwrite its
failure. Derive first-attempt and eventual outcomes separately. No result for a
planned test means pending/missing evidence while a run is active; after trusted
completion/reconciliation it is **not run or unreported**, never implicitly
passed, skipped or failed. Preserve that distinction in analytical views. Reports
without a plan may have `run_test` records derived by the adapter, but their run
must retain `selection_status=unknown`; observed tests cannot prove completeness.

### 11.10 Acceptance assessment (`acceptance_assessment`)

| Field | Meaning |
|---|---|
| `criterion_ref` | Exact criterion revision being assessed |
| `result_refs`, `artifact_refs` | Exact supporting test results and/or manual evidence |
| `verdict` | `accepted`, `rejected`, `blocked` or `inconclusive` |
| `assessor_type`, `assessor_id` | `human` or authorized `policy`, and accountable identity |
| `assessed_at` | UTC assessment time |
| `policy_snapshot` | Required exact policy artifact for automated assessments; nullable for a human decision |
| `rationale` | Explanation, including manual verification and any policy exceptions |

Accepted assessments require at least one evidence reference. Schema validity
alone does not authorize an assessor or establish acceptance. The evaluator must
check producer trust, definition and criterion bindings, code/deployment revision,
required scenarios, completeness, cleanup and permitted evidence age according
to the pinned policy. A result mapped to an older criterion cannot silently
accept a new revision. Cross-revision reuse needs an explicit reviewed policy and
rationale. Story acceptance is a derived assessment over all active criteria in
the pinned requirements manifest, with missing criteria shown as inconclusive.

### 11.11 Compatibility and migration

The existing v1 schemas and their default CLI remain unchanged. Use
`validate.py --version v2` to validate the new fixture set, or additionally
`--schema <record_type> <file>` for one record. CI validates both versions.

| Historical input | v2 interpretation |
|---|---|
| v1 Story metadata | Retain as legacy metadata. Fetch a new source snapshot for a v2 story revision; do not invent an earlier brief or criteria |
| v1 Test Run proven to be a single test attempt | Adapter may emit run, definition, observed planned-instance and result records when all required identities/provenance can be established; absent original plan stays unknown |
| v1 Test Run representing only a CI job (including the v1 example fixture) | Retain as legacy job-level evidence. Do not count it as one tested scenario or expand it into invented test results |
| v1 record of ambiguous grain | Quarantine from v2 test-level aggregates with an explicit migration reason; preserve the original |
| Existing shared-service `runs`/`test_results`/`selection_items` proposal | Align to v2 `test_run`/`test_result`/`run_test`; distinguish logical runs from run revisions and result attempts |

Migration is a proposed adapter, not delivered by these schemas. It must retain
raw references and mapping/parser versions, deduplicate legacy/native overlap,
and state exclusions in metrics. v1 task, commit/PR and deployment records remain
valid delivery evidence. Do not mutate v1 shapes, relabel a v1 record as v2, or
reinterpret the v1 `test_run_id` as a pack ID. Other incompatible changes also
require an explicit version and migration plan.
