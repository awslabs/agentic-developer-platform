# Engineering Insights — delivery architecture

Status: proposed architecture for [Engineering Insights (EI-E1)](https://github.com/aws-e/adp/issues/7295), not a deployed service. The [overview](agentic-engineering-insights-overview.md) defines the six outcome metrics and product boundary; the [lineage schema](lineage-schema.md) defines their evidence records and joins. This document traces how those records reach the scorecard and, eventually, the systems that plan and control work. **Current** means an existing ADP source or integration, not an available Insights pipeline. **Target** means work still subject to its owning story and the relevant review or live gate.

Wave 1 has filed delivery stories. EI-E1 owns the later-wave goals, but the W2–W5 delivery story IDs have not been assigned in its [issue map](https://github.com/aws-e/adp/issues/7295); they are marked **story pending** below rather than given invented IDs. EI-D01 owns this documentation, not the implementation of those later waves.

The EI-C01 module skeleton now provides the deployment infrastructure and
integration described in [section 6](#6-engineering-insights-deployment-boundary-ei-c01).
That implementation does not establish deployment or the later service capabilities
described as targets below.

```text
ADP / tracker / CI producers → bounded export + emitter → project Evidence Lake
    → evidence store + catalog → metric engine → scorecard + data-trust panel
    → policy-gated reverse-ETL publishers → work trackers / PMO tools / ADP Control
```

## 1. Produce and emit lineage

**Current:** ADP already records engine decisions, PR bindings, deployment and evaluation receipts, task boards in Git, and model-call usage. Source trackers and CI systems hold their own events. These sources do not yet make a joined Insights feed. The [CI artifact bucket](ci-artifact-storage.md) holds diagnostic artifacts; it is not the generic Insights Evidence Lake or analytics store.

**Target (EI-S01, EI-S02, EI-S02b, EI-S03, EI-S04, EI-S05; EI-C01):** Enriched task boards bind acceptance criteria to commits (EI-S01); typed node lineage and labels replace issue-body lineage comments in the flow plan, with UI chips and prompt projection delivered separately (EI-S02 and EI-S02b). Flows bind to the registered `project_id` (EI-S03). A bounded read-only gateway export exposes engine-owned decisions and receipts; the `modules/insights` emitter combines those with boards at the merged revision, maps them to records, and publishes by cursor (EI-S04). The distiller makes delivery records from emitted evidence, not direct engine table reads (EI-S05). The module and its pipeline are EI-C01. Related producers #7269 (tool audit/cost), #7293 (deployment receipts) and #1461 (agent activity) provide evidence as their own work lands; they are not dependencies that imply an implemented Insights feed.

Emissions carry the lineage schema's standard scope, source, event identity and provenance. EI-S04 proposes idempotency on `source_event_id`, replay by cursor range, per-day manifests and quarantine with a reason and coverage count when the flow's project cannot be resolved. A record with missing or ambiguous identity is not silently assigned to a different project.

### Lineage v1 contract

The first producer wave shares [versioned JSON Schemas](../../modules/insights/schemas/README.md)
for Story, Task, Commit/Pull Request, Deployment and Test Run records. Their
standard project/sourcing envelope is defined by [lineage section 3.0](lineage-schema.md#30-standard-fields-on-every-record),
and each record's own fields by sections 3.3, 3.4 and 3.6–3.8. Quarantined
events retain the standard fields and a reason as required by section 6.1.
The validator checks five example records in CI and reports a missing project
scope as a validation failure. CI also checks each schema field against its
designated source section, rejecting undocumented fields; changes to the source
design document trigger the same check. `schema_version` is fixed to `1.0`; adding a
different record shape requires a new schema version rather than changing the
meaning of v1. Later emitters consume these files; this change adds no emitter
or deployed service.

The additive [requirements/test analytics v2 contract](lineage-schema.md#11-requirements-and-test-analytics-v2)
now distinguishes story and criterion revisions, coverage mappings, test
definitions, pack runs, planned instances, results/attempts and acceptance
assessments. v1 delivery contracts remain unchanged. Both versions have offline
schema tests; neither implies an implemented ingestion or acceptance service.

The [worked fixtures](../../modules/insights/schemas/README.md) use a public
closed story as context. Its deployment example is synthetic and must not be
interpreted as observed rollout or live acceptance.

## 2. Resolve identity and bind the Evidence Lake

**Current:** The existing gateway supplies tenant authority and some delivery identifiers; ADP does not yet have a complete project-bound Insights lineage feed. The [project-registry design (#5411 §11)](https://github.com/aws-e/adp/issues/5411) specifies where each ID originates and how projects own their source connections. A design in an issue is not proof that the registry or project binding is deployed.

**Target (EI-S03, EI-S04, EI-S00; #5411 §11):** The canonical Project registry, not
Insights, mints `project_id`. Profile owns Product/Project identity, managing
bindings and selected source connections. Context/portfolio owns optional Portfolio
collections and dated Product membership, cross-product Initiatives, Contributions
and business attribution; the investment/finance
authority owns funding links. The registry does not become a second owner of all
those records. Tracker, CI and delivery sources retain their native identifiers.
Flows inherit one registered Project; emitted facts carry its exact tenant/Project
scope and applicable Story/Task/source identities.

Resolve business context from source-owned dated relationships at event effective
time and retain both relationship revision and mapping-policy version. The
[Profile/Context integration contract](lineage-schema.md#52-profile-and-context-integration-contract)
specifies change-feed/revision readback, immutable binding history, scoped replay,
source ownership and #7295's pending versioned adapter admission. No gateway table
reads or new generic v1/v2 producer types are implied. A missing optional Initiative
is represented as a `not_applicable` context state, never a fabricated native ID;
an unresolved required execution Project remains a quarantine condition.

Product–Initiative and Product–Release attribution is many-to-many. Profile v1's
one-managing-Product-per-Project rule only governs configuration and team mapping.
Cross-Project reporting checks every participating scope and preserves each
original execution fact; business links cannot widen access. Planned releases,
observed artifact inclusion and deployment attempts remain separate evidence.
An inaccessible Product's Contribution cannot be silently omitted while claiming
overall Initiative completion. Initial Product onboarding remains usable before
these optional analytics and coordination integrations are available.
Portfolio views use the same authorized source facts and dated memberships;
overlapping collections and repeated Project projections never multiply delivery
or cost. Enabling Insights requires no Portfolio, Product or Initiative parent.

**Target (EI-S04; EI-C01 for the wave-1 module; EI-E1 W2 store story pending):** The emitter publishes versioned JSON Lines records and manifests into the project's Evidence Lake namespace, `assurance/v1/tenant=<tenant>/project=<project>/insights/<record_type>/dt=<day>/`, with a separate quarantine prefix. EI-S04 describes an interim bucket/prefix; the later dedicated store must not be mistaken for an existing service. An object key supplied by an uploader does not confer authority to publish there.

## 3. Store, catalog and compute results

**Current:** ADP's operational tables and private diagnostic artifact storage are not an Insights evidence catalog, cross-source linker or computed ROI scorecard. Existing receipts can be inspected at their sources, subject to their existing access controls.

**Target (documentation: EI-D01; delivery: EI-E1 W2 store/catalog story pending):** The Insights-owned evidence store retains immutable source records, revisions and manifests under the project boundary. For initial requirements/test analytics, section 3.1 selects S3 Parquet and DuckDB. The broader [lineage storage proposal](lineage-schema.md#8-storage-on-aws) retains Iceberg/S3 Tables and Athena-class views as a later option. The catalog exposes accepted records, quarantined records and mapping versions. Transactional project grants, connection state, idempotency and publisher state belong in the operational control plane rather than in analytics tables. Insights readers consume emitted records and authorized source-connection evidence, **never engine or gateway tables directly**.

**Target (documentation: EI-D01; delivery: EI-E1 W3 metrics/scorecard stories pending):** Link the story to its tasks, calls, PRs, tests, deployment and incidents. Calculate the [six outcome metrics](agentic-engineering-insights-overview.md#61-layer-1-the-six-outcome-metrics-in-the-ai-roi-scorecard) using their stated denominators and evidence windows, with detail and agent/cohort comparisons. The scorecard UI reads those versioned views and the relevant business-case targets; it does not calculate spend by counting tokens or claim a production result from a merge. Every rendered number has an adjacent data-trust state (source freshness, coverage, link rate, reconciliation and mapping version). Suppress a number below its trust floor rather than displaying an apparently precise but unsupported value. Team/cohort reporting is the default, not developer ranking.

### 3.1 Requirements and test analytics storage decision

**Design decision, 2026-10-10; implementation pending:** the first requirements
and test analytics query path uses **DuckDB over curated Parquet in private S3**.
This is a bounded extension of delivery lineage, not replacement of the existing
cost, PR, deployment or incident records and scorecard definitions. The checked-in
change delivers schemas, examples and validation, not a database, collector,
query API or AWS deployment.

```text
GitHub Issues (brief + identified criteria)    CI / test harnesses
                  ↓                                 ↓
        Verified project-scoped collectors / admitted publishers
                  ↓                                 ↓
        Immutable S3 source snapshots, reports and normalized JSONL
                                  ↓
                Validate links → Compact curated Parquet
                                  ↓
                    Published dataset manifest
                                  ↓
                 DuckDB behind authorized Insights API
                                  ↓
         Requirements coverage / execution history / acceptance evidence
```

| Data | Authoritative source / retained representation |
|---|---|
| Story brief, acceptance criteria and tracker state | GitHub Issues for ADP; other registered trackers via adapters. Immutable JSON snapshots in S3 preserve observed revisions |
| Modules, features, scenarios, definitions and mappings | Consumer's versioned catalog; ADP keeps its JSON registry in GitHub. Snapshot each catalog publication |
| Run, intended selection, test results and retries | Execution provider plus authenticated evidence; normalized JSONL under project scope |
| Accepted analytical datasets | Curated Parquet files and immutable dataset manifests referencing exact inputs, object versions/checksums, schema/parser/mapping versions and completeness |
| Query execution | DuckDB in bounded service workers; local database/cache files are disposable, not an authoritative shared database |
| Project grants, connector cursors, publication state and idempotency | Existing operational control plane through explicit interfaces; no direct gateway database access |
| Spreadsheet review | Generated CSV exports from authorized versioned views; no independently edited CSV source of truth |

Collectors capture issue creation/edits/state changes and reconcile missed events;
[lineage section 11.2](lineage-schema.md#112-story-revision-story_revision) defines
identity, ordering, deletion and extraction behavior. Requirements snapshots are
independent of runs. A run pins catalog, requirements and intended-selection
manifests; changing a GitHub issue later cannot change its historical verdict.
Stories without identifiable criteria remain visible as requirements gaps.

The normalizer validates records and resolves references before publishing a
dataset. It micro-batches and compacts files by tenant/project, record type and
time window; do not partition by high-cardinality test/run IDs. Write new files,
then publish an immutable manifest, and advance the authorized catalog pointer
conditionally. A failed publication must leave the previous complete snapshot
queryable. Readers query only manifest-listed files, never an unrestricted S3
glob containing unfinished writes. Serialize publication per dataset or use
compare-and-swap; S3 does not supply multi-object transactions. Replays,
corrections and late events append evidence and create a new dataset version.
Each dataset also selects one effective revision per logical record, with
source-order/correction provenance. Aggregate those selected revisions rather
than counting every historical copy. Preserve all result-attempt identities;
choosing a corrected result revision must not erase an earlier failed attempt.

DuckDB workers use approved parameterized queries and server-resolved authorized
object lists. Arbitrary SQL, S3 paths, external URLs, extension loading and
filesystem access are not exposed to callers. Apply project grants before
choosing data, constrain service IAM to the selected storage scope, and bound
memory, spill space, scanned bytes, concurrency and execution time. Scope caches,
exports and pagination to project grants and dataset version; reauthorize reads
and exports. This requires service integration and isolation tests: a SQL WHERE
clause or a Parquet partition name alone is not authorization. Keep sensitive
raw requirements, screenshots and transcripts under their retention/access rules.

S3 Tables/Iceberg remains a later option when transactional table management,
multi-writer ingestion or cross-engine access is needed. It is not required for
the first DuckDB path, and these schema changes do not provision it. Before that
cutover, prove supported catalog/authentication integration, snapshot consistency,
schema evolution, scoped access and retention/deletion with the chosen DuckDB or
Athena versions. Preserve logical IDs and reference semantics across engines;
copying JSONL/Parquet into S3 is not an Iceberg commit.

### 3.2 Contract and delivery follow-through

The [epic/story authoring contract](issue-authoring.md) specifies the upstream
IDs, acceptance criteria and scenario mappings for humans and agents. Templates
and an offline format checker are available; issue ingestion remains follow-on work.

The [v2 lineage](lineage-schema.md#11-requirements-and-test-analytics-v2) is the
canonical grain for new requirements/test analytics. The shared README's
analytical tables project those records; v1 job/test-attempt evidence requires
the explicit compatibility mapping in section 11.11. Do not silently convert
old job summaries into test-level results. Acceptance remains a separately
authorized assessment with a pinned policy or accountable human decision.

Follow-on delivery work must include the issue connector and reconciliation,
catalog/selection publishers, reference resolver and legacy adapter, Parquet
publisher, authorized DuckDB views and acceptance assessor. These are requirements
for EI-E1 planning, **not newly filed or completed delivery stories**. Before
release, demonstrate:

- Duplicate/out-of-order issue events preserve version history; a missed edit
  and ambiguous criterion IDs cannot produce complete requirements coverage.
- An edited criterion does not inherit acceptance from an older run; manual
  evidence and policy identity remain attributable.
- Failed then passed retries remain two results for one planned instance;
  unreported planned tests cannot become passes or disappear from completeness.
- Repeated source IDs across projects cannot join or leak through queries,
  artifact references, caches or exports; revoke access during an active query.
- A crashed Parquet publication leaves the prior snapshot usable; replay does
  not double-count results; v1 job evidence stays out of test-level denominators.

## 4. Publish results back to planning and Control

**Current:** The [overview's Control feedback](agentic-engineering-insights-overview.md#72-feeding-back-into-control) describes intended decisions, not deployed reverse-ETL publishers. Jira, Linear, GitHub Issues/Projects and PMO tools remain systems of record for human-entered plans and status; ADP does not currently push an Insights scorecard into them.

**Target (documentation: EI-D01; delivery: EI-E1 W4 reverse-ETL stories pending):** Connection-scoped publishers use the project's registered source connections and explicit publisher grants to write selected, versioned facts to work trackers (actual lead time, cost-to-date, rework state and evidence links on stories; governed epic roll-ups), PMO/portfolio tools (initiative ROI and forecast versus business case), and ADP Control (budget history, quality signals and risk-class autonomy inputs). A mapping specifies allowed destination fields and `mapping_version`; each write is policy-gated, field-scoped, idempotent and auditable, with the data-trust state beside the pushed value. Never overwrite a human-entered field without a recorded policy. Revoking a connection or grant stops subsequent writes; reversible write-back must preserve tracker ownership of intent. See the [overview §7.2](agentic-engineering-insights-overview.md#72-feeding-back-into-control) for the decision feedback, distinct from direct scorecard presentation.

## 5. Module and trust boundary

**Current:** [`modules/engineering-insights/`](../../modules/engineering-insights/README.md) is an earlier shared-capability proposal, not the EI-E1 implementation module. Gateway and other producers own their operational data. No Insights pipeline or dedicated `modules/insights` service is available merely because these documents describe one.

**Target (EI-C01; EI-S02, EI-S03 and EI-S04 for bounded gateway changes; EI-E1 W2–W4 stories pending):** The EI-E1 [module-boundary decision 8](https://github.com/aws-e/adp/issues/7295) places every Insights-specific service, job, schema, Terraform resource and pipeline in `modules/insights`, with its own CI, `deploy.sh` and engine-contract deployment pipeline. Gateway changes are limited to lineage/labels on its own plan models (EI-S02), `project_id` on flows (EI-S03) and a bounded, read-only export endpoint (EI-S04); Insights does not reach into gateway/engine tables. Downstream integrations use explicit interfaces, not hidden shared database access. The older shared-capability design remains useful for external-project publishing and isolation rules, but its proposed module path is not the EI-E1 delivery location.

**Target (EI-C01, EI-S03, EI-S04; EI-E1 W2–W4 access stories pending):** Authenticate producers, exports, queries and publishers against the tenant and registered project. Scope objects, catalogs, caches, grants and exports to that boundary; require access to each project for an aggregate. Source-provided IDs, bucket keys, labels and untrusted issue content cannot authorize a read or write. Quarantine failed validation and expose coverage gaps; restrict individual evidence, raw transcripts and sensitive cost details. Apply least privilege to service identities and Evidence Lake prefixes. Agents do not receive AWS roles; each owning component deploys via its existing authorized pipeline.

## 6. Engineering Insights deployment boundary (EI-C01)

This section describes the independently deployable W1 Insights module. It is
implementation architecture, not evidence that the service has been deployed or
that emitter/distiller implementations are complete. The broader shared-product
proposal and lineage schema remain separate design documents.

### Ownership and flow

The independently deployable module lives in `modules/insights/`. Its
`emitter/`, `distiller/` and `schemas/` directories currently mark ownership;
the emitter and distiller implementations are delivered by their own stories.
The gateway continues to own its own data and may later add a bounded internal
export endpoint, but does not host these services. Agents never receive a deploy
role or direct AWS permissions.

An Insights-only pull request selects `.github/workflows/insights-ci.yml`, not
Gateway CI. CI runs offline contract tests, Terraform validation and a mocked
IAM-policy test; it does not apply infrastructure. On a path-filtered push to
`main`, `.github/workflows/insights-deploy.yml` selects the dev protected
environment. An engine or operator can dispatch the same workflow with an
approved source revision, definition revision, environment, stage and mode.
Prod is dispatch-only and requires a reviewer on its protected GitHub
environment. The workflow checks source ancestry and the unchanged workflow
definition before assuming the protected deployment role, then checks the
selected account against SSM. Plan mode cannot publish a deployed-release
artifact. A successful apply publishes a run-bound context artifact and an
`adp-release-insights` artifact; those artifacts are **not** runtime acceptance.

`modules/insights/deploy.sh` owns `--infra`, `--emitter`, `--distiller` and
`--verify`, plus `--plan` and policy-only `--bootstrap-ci`. Infra converges an
isolated Terraform state. Emitter and distiller updates require immutable,
account-bound ECR digests; a later stage retains the other service's previously
recorded digest. Missing service images do not become placeholder Deployments.
When actual images are supplied, Deployments begin at zero replicas until the
owning rollout explicitly enables them. Re-applying the same saved configuration
is intended to produce no changes; the reviewer verifies that against a real
dev target after deployment, not from mocked CI tests.

The Terraform root defines the `adp-<env>-insights-evidence` versioned S3 bucket,
KMS encryption, the `assurance/v1/…/insights/` object layout, private-bucket
controls, separate emitter/distiller IRSA roles and ECR repositories. In
`adp-insights`, quota and restricted pod security accompany a deny-all ingress
and egress policy. Egress is then allowed only to DNS in `kube-system`, the
gateway backend, the region's S3 prefix list, the platform's private regional STS
endpoint, and explicitly supplied GitHub API CIDRs. STS exchanges the service's
identity token for its scoped AWS credentials. Terraform discovers the existing
endpoint in the platform VPC and permits HTTPS only to its interface IPs as `/32`
destinations; no additional endpoint is created. Both service accounts request
regional STS. Planning fails if that endpoint lacks private DNS or network
interfaces, rather than opening broader egress. Re-apply Insights after endpoint
replacement to refresh those addresses. The bootstrap policy includes the
region-scoped EC2 read permissions needed for endpoint and prefix-list discovery.
Empty GitHub CIDRs mean no GitHub egress; unrestricted CIDRs are rejected.
The emitter can put only under the Insights object prefix; the distiller may
read other assurance evidence but can put only into Insights results. No role
is attached to an agent.

### Machine identity and deployment authority

The Kubernetes service accounts `insights-emitter` and `insights-distiller`
select their *separate* AWS IRSA roles for scoped evidence access; they are not
ADP user accounts. A platform administrator must register the service workload
identities under the selected organization using the existing `adp admin
service-account create --identity-type sql-iam` flow, binding each exact IRSA
role ARN to its own ADP machine service account. The CLI registration mints no
AWS credential. The service implementations will use those ADP machine
identities for authorized model calls and gateway-mediated GitHub App token
minting; do not copy a GitHub App private key or a human token into a Pod.
Registration, grants and actual invocation checks belong to the service
stories, not to this infrastructure skeleton.

Before the first authorized pipeline run, the platform administrator confirms
the account and exact runner role using the canonical deployment guide, checks
that `/adp/<env>/platform/account-id` exists in SSM in that account, reviews the
IAM policy printed by `deploy.sh --bootstrap-ci --account <confirmed-account>`,
and applies it to the protected deployment runner role. This supplemental policy
has only exact-role IAM reads: it grants no role creation, policy or trust writes,
boundary changes, or `PassRole`. Those permissions remain exclusively with the
platform's operator-owned workload admission mechanism in
`platform/automation-infra/workload-inventory.tf`. The operator first bootstraps
`adp-<env>-insights-emitter` and `adp-<env>-insights-distiller` with independently
reviewed boundaries, then admits their exact role-to-boundary entries through
that mechanism. Import the bootstrapped roles into the Insights Terraform state
at `aws_iam_role.service["emitter"]` and `aws_iam_role.service["distiller"]` before
the first module apply. Boundary policies are never created or modified by this
module. Complete platform admission before granting the deployment identity
access to the `adp-insights` Kubernetes namespace.

Copy only these two approved inventory entries into the protected GitHub
environment variable `ADP_INSIGHTS_ROLE_BOUNDARIES` as a JSON object mapping
exact role ARNs to their boundary policy ARNs. The workflow passes it as
`TF_VAR_deployment_role_boundaries`; local operator runs must supply that same
Terraform input. Missing entries, incorrect environment/account targets and
malformed boundaries fail planning. Both role resources preserve their configured
boundaries; a different but syntactically valid boundary cannot override the
platform's IAM enforcement of the operator-approved value. Do not attach a
separate wildcard IAM grant to make deployment succeed.

They must configure
`adp-insights-deploy-dev` without a reviewer and
`adp-insights-deploy-prod` with a reviewer, binding the existing trusted
role through `ADP_INSIGHTS_DEPLOY_ROLE_ARN`. The trusted deployment action
requires the reviewed `adp-…-trusted-deployment` role convention. Account,
region, role, GitHub API CIDRs and organization identities are private operator
configuration, never values committed to public docs. This prerequisite has
not been verified or performed by this change.

### Verification and recovery

The deployment engine maps only Insights paths to the `insights` component.
Its manifest entry remains **unresolved** until an owner proves the registered
environment connection's account, cluster and `adp-insights` namespace, pins the
reviewed workflow revision and approves an exact service image input. D3's
`insights-health-verification` re-reads the Evidence Lake bucket controls and
Kubernetes network/identity restrictions, and requires both Deployments to
have owned, ready Pods; at least one service image digest must match the release. An infra-only
or zero-replica release is intentionally not a healthy-service receipt. After
emitter and distiller images exist, the engine owns an actual dispatch artifact
and receipt check at L1; the reviewer owns the dev plan and repeat-apply
transcript; the L1 evaluator owns the live out-of-prefix AccessDenied check.
None of those live checks is established by this PR.

An operator can use `platform/scripts/deploy-all.sh --insights-only` for the
module-only entrypoint; `--ci` makes that route plan-only. The phased
`platform/scripts/undeploy.sh --insights-only` selects Insights before its
shared platform prerequisites and still requires typed account confirmation.
**This confirmed teardown permanently deletes Insights evidence, including object
versions and delete markers, before removing the bucket.** The orchestrator empties
the bucket explicitly; Terraform's `force_destroy = false` does not prevent this
purge. Before confirming teardown, satisfy the applicable retention requirements
and create and verify a backup outside the selected resources. Do not use teardown
as a deployment retry or rollback while evidence must be retained. For an
image regression, run the owning pipeline again with a previously reviewed
immutable digest after the usual approval; do not bypass the protected
pipeline or mutate the gateway deployment.

Offline checks from the repository root:

```bash
python3 -m unittest discover -s modules/insights/tests -p 'test_*.py'
terraform -chdir=modules/insights/platform init -backend=false -input=false
terraform -chdir=modules/insights/platform validate
terraform -chdir=modules/insights/platform test
```

The Terraform test uses mock providers and changes no cloud resources. A real
`--plan` or repeat-apply check requires a separately confirmed target and the
platform administrator's permissions; it must not run merely to validate a PR.
