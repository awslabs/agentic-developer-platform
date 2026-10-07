# Repository audit and supporting evidence

This audit supports the [proposed epic design](README.md). It inventories the
entire tracked tree at `fc5fe6f21100df5d49c29f4c3a882907ff8e659e`, then follows
the affected gateway behavior and integration/deployment paths. It is not an
audit of a live environment or a claim that every unrelated component's code was
semantically reviewed. Public source locations are evidence; no private campaign
artifact locations or deployment identifiers are reproduced here.

## Exhaustive artifact inventory

[inventory.tsv](inventory.tsv) contains **one row for each of 8,818 tracked
artifacts**, including explicit unrelated exclusions. Each row records path,
component, scope, entry-point/reference signals and Git mode. No artifacts are
unmapped; there are no submodule/non-blob entries at this revision. The checked-in
[summary](inventory-summary.json) gives exact reproducible category counts.
The design package itself is a later documentation addition, not part of the
audited baseline; this fixed boundary prevents a self-referential inventory.
The supplemental [design artifact ledger](design-artifacts.tsv) enumerates every
file added by this proposal, including its audit helpers and the ledger itself.
`validate.py` checks that the union of both ledgers equals the tracked tree plus
the intended new package files; a new unrelated artifact or deletion is a failure.
Thus the baseline inventory is immutable without leaving design additions unmapped.

### Re-review source comparison

The reviewed design head was `f51fb801f6f113e1f4b52874244c638b5fb112ef`.
On 2026-10-06 the default branch was observed at
`87a57904ce9e328b7d6217f38038476080c7cb3e`. The design branch's runtime source is
unchanged from the audited baseline. No newer admission reservation, quote,
settlement, migration, provider or deployment source is silently incorporated.
Current main has adjacent orchestration changes, notably
[policy admission at that revision](https://github.com/aws-e/adp/blob/87a57904ce9e328b7d6217f38038476080c7cb3e/modules/gateway/src/orchestration/policy_admission.py#L396):
running nodes can use an initialized flow meter while awaiting usage. That meter
still refuses absent/expired/unbounded state; it is not the proposed durable
per-attempt hierarchy/person authority. Implementation must integrate with that
change rather than replace flow/node holds with model-attempt sums. Other changed
orchestration claim/recovery files and unrelated feature/workflow tests do not
add the five proposed tables or the provider dispatch guard. Main's changes are
not part of the pinned inventory or evidence of a deployed revision.

[audit.py](audit.py) reads `git ls-tree` and every tracked blob via `git cat-file`,
including dot-directories, without importing or executing repository code.
It detects all workflows, composite action descriptors, shell/executable entry
points, package build manifests, Docker/buildspec paths, Terraform inputs,
Kubernetes/compose files and deployment/build command references. Classification
is intentionally over-inclusive: a document quoting `kubectl` is a reference,
not a confirmed executable deployment. The ledger, not a handful of examples,
is the enumeration; the entry-point table below explains the affected chains.

| Scope value | Meaning / boundary |
|---|---|
| `component-audit` | Every gateway artifact and gateway performance artifact: 2,636 paths, including source/tests, frontend/CLI, schema, dependencies, infra, manifests and security/build metadata. Component assignment is not a promise every path changes. |
| `integration-reference` | Other tracked files with explicit gateway/deployment/tracker/harness references. Retained for integration/dependency review, not assumed to share the inference serving path. |
| `entrypoint-audit` | Other repository build/deploy/package/infra/shell candidates, mapped to their owning component. Excluded from gateway implementation unless an integration dependency below identifies them. |
| `excluded-unrelated` | No affected-component or entry-point/reference signal. Explicitly excluded from this epic's behavior changes; still present individually in the ledger. |

Reproduce from repository root, with Python 3 and Git and the baseline object
available (no cloud credentials required):

```bash
python3 docs/design-notes/7030-gateway-performance/audit.py --check
```

Without `--check`, the script regenerates only the inventory TSVs and summary in
this design directory. It does not regenerate prose or change runtime files.

### Component ownership and exclusions

| Inventory component family | Epic responsibility / exclusion |
|---|---|
| Gateway proxy/pool/auth/approval/budget/ratelimit/usage/shared models and tests | #7031–#7033 transaction/stream/accounting contracts; #7035 instrumentation. Auth/tenant protections remain mandatory even where files do not change. |
| Gateway chat logging/pricing/tracker/Lambda shared policy | #7032 journal versus optional transcript, compatible pricing and recovery; #7035 export visibility. Pricing rates themselves are not changed. |
| Gateway migrations/alembic | Complete 88-revision graph below; no new SQL table preselected. Runtime timeouts must not alter migration sessions. |
| Gateway k8s/infra/scripts/Dockerfile/locks/compose | #7034 deployment/profile/resource consistency, #7032 tracker packaging/recovery infra, #7035 actual-role policy. Local compose is a test environment, not evidence of pre-production. |
| Gateway frontend/CLI and unrelated admin/knowledge/chat/task features | Explicit behavior-change exclusion. Shared gateway image/DB may carry them; negative compatibility and resource-budget accounting remain in scope. No UI feature requested. |
| Gateway security/build evidence and module documentation | Build/dependency constraints, not new vulnerability remediation. Historical evidence is not a live performance result. |
| Performance harness | #7036 all four existing tracked files, then proposed controller/recovery/report automation. |
| Shared platform infrastructure/scripts, root launcher, environment inputs | #7034 node/readiness/role/render integration; #7035 scoped role policy. No new platform deployment or teardown in this task. |
| CI workflows/actions/scripts and CodeBuild recipes | Build/deployment/test trigger inventory; preserve existing authoritative release paths. Unrelated security/frontend/application pipelines excluded from capacity changes. |
| Agent Factory/hosted clients and harness | Traffic provenance/fan-out and path compatibility; no watchdog/runtime redesign or new journal permissions. Agent gateway is a distinct component, not the FastAPI inference gateway. |
| Agent Context/research/MCP/tools/user services | Other consumers/builds are inventoried but excluded from implementation; count their actual shared DB use where applicable, never assume zero. No code-search/vector/wiki performance claims. |
| Domain apps, experiments, demos, contracts unrelated to inference, remaining docs/tests | Explicit exclusions from gateway behavior and certification. Their deploy recipes remain enumerated; not all applications become covered by a gateway capacity result. |

## Source evidence

Line links identify inspected entry points. Resolve them at the baseline above
for immutable evidence, or use this
[baseline tree](https://github.com/aws-e/adp/tree/fc5fe6f21100df5d49c29f4c3a882907ff8e659e).
The design branch does not modify these source files. Statements concern source
behavior, not proof that a selected environment runs it.

| ID | Verified source and finding |
|---|---|
| S01 | [database.py:107](../../../modules/gateway/src/shared/database.py#L107), [config.py:22](../../../modules/gateway/src/shared/config.py#L22): one cached async engine/factory; IAM opt-in pool, otherwise NullPool; password callback, TLS, pre-ping/recycle; settings default 5+5 and 10s checkout. Password-auth branch uses 20+10. |
| S02 | [proxy context:228](../../../modules/gateway/src/proxy/routes.py#L228), [auth dependency:76](../../../modules/gateway/src/auth/middleware.py#L76), [approval factory:195](../../../modules/gateway/src/auth/approval_middleware.py#L195), [rate-limit factory:80](../../../modules/gateway/src/ratelimit/service.py#L80): actual streaming route consumes verified middleware context or validates Cognito; legacy auth dependency also exists. Source does not identify campaign blocker. |
| S03 | [Responses iterator:556](../../../modules/gateway/src/proxy/mantle_service.py#L556), [metering:763](../../../modules/gateway/src/proxy/mantle_service.py#L763), [S3 scheduling:903](../../../modules/gateway/src/proxy/mantle_service.py#L903): relays upstream bytes, classifies missing terminal/HTTP errors, shields cleanup, logs duration before awaiting usage; optional S3 scheduling is later. |
| S04 | [Chat metering:479](../../../modules/gateway/src/proxy/service.py#L479), [persistence deadline:685](../../../modules/gateway/src/proxy/service.py#L685), [wrapper:596](../../../modules/gateway/src/chat_logging/service.py#L596): current Claude pricing path already has a bounded SQL persistence variant and finalizer machinery; do not design as if none exists or assume it applies identically to Responses. |
| S05 | [usage commit/dedup:127](../../../modules/gateway/src/usage/service.py#L127), [settlement:16](../../../modules/gateway/src/budget/settlement.py#L16): claim tenant/request receipt, verify replay amount/owner/allocation, update hierarchical ledger in deterministic order; usage row dedup serialized by receipt lock. Concrete hot rows are candidates, not proven blockers. |
| S06 | [log scheduling:125](../../../modules/gateway/src/chat_logging/service.py#L125), [S3 write:165](../../../modules/gateway/src/chat_logging/s3_writer.py#L165): optional logging/model filtering and `asyncio.create_task`; current writer has circuit-breaker/boolean failure and canonical tenant/user/date/request key. Scheduling is not acknowledgement. |
| S07 | [tracker processing:367](../../../modules/gateway/lambda/budget-usage-tracker/handler.py#L367), [event handling:570](../../../modules/gateway/lambda/budget-usage-tracker/handler.py#L570), [reservation recovery:529](../../../modules/gateway/src/budget/reservations.py#L529): validates settlement version/key, uses receipt and per-record commit/retry; trusted scope recovery exists. Tracker bridges usage rows rather than inserting a missing full row. |
| S08 | [Chat formatter:142](../../../modules/gateway/src/proxy/stream_handler.py#L142), [message stop:264](../../../modules/gateway/src/proxy/stream_handler.py#L264): final SSE emitted on iterator exhaustion; OpenAI marker also emitted for provider `message_stop`. Add missing-provider-terminal regression; not the campaign root-cause conclusion. |
| S09 | [client config:72](../../../modules/gateway/src/pool/simple_pool.py#L72), [client selection:110](../../../modules/gateway/src/pool/simple_pool.py#L110), [threaded stream:844](../../../modules/gateway/src/proxy/service.py#L844): streaming read 300s/connect 10s, configurable single-attempt path; same-account singleton versus explicit per-call routed clients; one threaded `next` per event and body close in `finally`. |
| S10 | [base deployment:111](../../../modules/gateway/k8s/deployment.yaml#L111), [health:460](../../../modules/gateway/src/app.py#L460), [default HPA](../../../modules/gateway/k8s/hpa.yaml#L42): default resources/scaling differ from performance profile; both probes use static health handler. |
| S11 | [profile patch](../../../modules/gateway/k8s/profiles/llm-proxy/deployment-patch.yaml#L1), [profile HPA](../../../modules/gateway/k8s/profiles/llm-proxy/hpa.yaml#L1), [PDB](../../../modules/gateway/k8s/profiles/llm-proxy/pdb.yaml#L1), [profile instructions](../../../modules/gateway/k8s/profiles/llm-proxy/README.md#L1): opt-in two workers/two-pod floor, 12 ceiling, 1-core target; explicitly warns ordinary deployment resets defaults. |
| S12 | [EMF namespace:35](../../../modules/gateway/src/shared/metrics.py#L35), [direct helper:32](../../../modules/gateway/src/admin/cognito_claims.py#L32), [pricing direct API:139](../../../modules/gateway/lambda/shared/pricing_fallback.py#L139): multiple export paths; campaign denial alone does not identify which. |
| S13 | [gateway IAM:356](../../../modules/gateway/infra/main.tf#L356), [platform role:490](../../../platform/infra/modules/eks/main.tf#L490), [pricing namespace precedent:83](../../../modules/gateway/infra/modules/budget-lambda/iam.tf#L83), [orchestration precedent:72](../../../modules/gateway/infra/modules/orchestration-tick/iam.tf#L72): policy owners differ; namespace-conditioned direct metric grants already have precedents. |
| S14 | [Locust shape:23](../../../tests/performance/gateway/locustfile.py#L23), [parser:125](../../../tests/performance/gateway/locustfile.py#L125), [samples:223](../../../tests/performance/gateway/locustfile.py#L223): cumulative stage times, one token/process counters, content+terminal validation, cancellation records and local concurrency samples. No durable cleanup controller or rolling fleet guard here. |

## Database and storage state

### Additional source evidence for R1–R3

| ID | Verified source and implication |
|---|---|
| S15 | [ordinary reservation fallback:1163](../../../modules/gateway/src/budget/enforcement_service.py#L1163), [period key:279](../../../modules/gateway/src/budget/reservations.py#L279), [strict snapshot:468](../../../modules/gateway/src/budget/reservations.py#L468). Ordinary expiry/degrade differs from strict initialized policy semantics; neither substitutes for the new SQL authority. |
| S16 | [gap model:31](../../../modules/gateway/src/budget/enforcement_settings.py#L31), [scope check:68](../../../modules/gateway/src/budget/enforcement_settings.py#L68), [migration 065](../../../modules/gateway/alembic/versions/065_budget_enforcement_controls.py#L1). Existing gap controls are global/flow, not general hierarchy/person holds. |
| S17 | [person fallback:1512](../../../modules/gateway/src/budget/enforcement_service.py#L1512), [identity fusion:205](../../../modules/gateway/src/budget/person_ledger.py#L205), [partition spend:309](../../../modules/gateway/src/budget/person_ledger.py#L309), [workspace link:156](../../../modules/gateway/src/shared/identity/workspaces.py#L156). New admission must retain alias exposure and resolve cross-org person scope even when legacy behavior would skip the person layer. |
| S18 | [late quote check:1089](../../../modules/gateway/src/budget/enforcement_service.py#L1089), [confirmation:709](../../../modules/gateway/src/orchestration/provider_quotes.py#L709), [cached generation:165](../../../modules/gateway/src/budget/pricing_v2_reader.py#L165). Confirmation awaits a revision read; proposed local transport fence and publication coordination are additions, not existing guarantees. |
| S19 | [threaded Botocore dispatch:96](../../../modules/gateway/src/pool/simple_pool.py#L96), [HTTPX send:483](../../../modules/gateway/src/proxy/mantle_service.py#L483), [quote adapter registry:672](../../../modules/gateway/src/orchestration/provider_quotes.py#L672). Do not put the last check before thread/pool waits or assume every translated route already has a bounded quote adapter. |
| S20 | [settlement lock/debits:16](../../../modules/gateway/src/budget/settlement.py#L16), [legacy writer:288](../../../modules/gateway/src/budget/service.py#L288), [correction writer](../../../modules/gateway/src/budget/pricing_correction.py#L1), [usage service:127](../../../modules/gateway/src/usage/service.py#L127), [tracker:367](../../../modules/gateway/lambda/budget-usage-tracker/handler.py#L367). All denominator-changing paths must use the same scope-first ordering for active cohorts. |
| S21 | [human admin controls:53](../../../modules/gateway/src/budget/enforcement_routes.py#L53), [person-cap rules:39](../../../modules/gateway/src/budget/person_cap_routes.py#L39). Reconciliation reuses authenticated human/platform authority and revision checks; org admins cannot waive cross-org person exposure. The new API does not exist yet. |

[migrations.tsv](migrations.tsv) enumerates all **88** migration files in
topological order, including merge branches and each revision's parent(s).
There is one source head: `084_reservation_receipt_scopes`. The audit detects
missing parents/cycles. Its literal-table-operation column is an index into
source (includes upgrade/downgrade references), **not** a SQL schema simulator:
dynamic loops/raw SQL still require inspection. No live Alembic state was read.

The affected final schema was checked against models and cumulative accounting
migrations, not merely the initial schema:

| Existing state | Source and design consequence |
|---|---|
| `budget_usage`: scoped aggregate uniqueness by organization/entity/period | [model:24](../../../modules/gateway/src/shared/models/budget.py#L24), initial schema plus revisions 030/032/035 and settlement code. Different users can still update a shared organization row; no guessed new aggregate table. |
| `budget_settlement_receipts`: tenant/request PK, owner/cost/tokens/allocation, nullable trusted reservation scopes | [074](../../../modules/gateway/alembic/versions/074_budget_settlement_receipts.py#L1), [084](../../../modules/gateway/alembic/versions/084_reservation_receipt_scopes.py#L1), [model:236](../../../modules/gateway/src/shared/models/budget.py#L236). Reuse for exact business debit deduplication; 074 refuses destructive downgrade, 084 preserves nullable data on downgrade. |
| `usage_logs`: request/tenant/user, cache tokens, run/persona/pricing/routing evidence; scoped request index | [model:10](../../../modules/gateway/src/shared/models/usage.py#L10), revisions 028/031/033/037/061 and later pricing evidence, [080 index](../../../modules/gateway/alembic/versions/080_usage_request_index.py#L1). Index is not unique; duplicate-row protection must remain under receipt serialization. |
| Pricing correction and generation records exist | [079](../../../modules/gateway/alembic/versions/079_budget_pricing_corrections.py#L1), [pricing contract](../../../modules/gateway/lambda/shared/pricing_settlement.py#L1). Do not reprice recovered historical work against a fresh generation or invent a new ledger. |
| Redis admission/reservation state exists | [reservations:443](../../../modules/gateway/src/budget/reservations.py#L443), [trusted recovery:529](../../../modules/gateway/src/budget/reservations.py#L529). Unknown/pending markers are not measured zero; durable SQL/S3 evidence remains necessary across loss. |
| Person budgets and enforcement controls already exist | [034 person caps](../../../modules/gateway/alembic/versions/034_person_budget_configs.py#L75), [036 defaults](../../../modules/gateway/alembic/versions/036_person_budget_defaults.py#L81), [065 controls/gaps](../../../modules/gateway/alembic/versions/065_budget_enforcement_controls.py#L15). Reuse their policy meaning; they are not a complete admitted-attempt index. Proposed gates/attempts/scope bindings/aliases/audit in [the additive schema](accounting.md#additive-schema) are genuinely new, not duplicate charge tables. |
| Private versioned chat-log S3 bucket and `.json` tracker notification exist | [bucket](../../../modules/gateway/infra/modules/s3-chat-logs/main.tf#L16), [notification](../../../modules/gateway/infra/modules/budget-lambda/main.tf#L262), [key generator](../../../modules/gateway/src/chat_logging/s3_writer.py#L165). Proposed time-partitioned journal namespace enables bounded recovery discovery; preserve transcript keys and review lifecycle for unresolved records. |
| Existing tracker grants and pricing-specific failure queues | [tracker IAM](../../../modules/gateway/infra/modules/budget-lambda/iam.tf#L35), [pricing delivery](../../../modules/gateway/infra/modules/budget-lambda/pricing-delivery.tf#L1). Pricing-refresh retry destinations are not evidence of tracker recovery. Add scoped tracker recovery as an explicit #7032 deliverable. |
| Gateway identity indexes remain separate from accounting | [identity index](../../../modules/gateway/infra/main.tf#L1494) uses identity-type/value keys. No new DynamoDB table or key convention is proposed for metering; unrelated chat/task/agent stores remain excluded. |
| Credential storage unchanged | [identity design](../../user-identity-and-credentials-design.md#L1) and existing vault paths. No new Secrets Manager path, credential cache or broker is proposed. Operator configuration stays private. |

## Deployment and build entry points

The complete individual inventory is in the TSV (all **129 workflows**, plus
all other detected entry-point artifacts). This table maps the relevant chains
to behavior, owner and exclusions; it is not a substitute sample inventory.

| Entry chain | Verified role / epic treatment |
|---|---|
| [root launcher](../../../deploy.sh#L1) → [deploy-all](../../../platform/scripts/deploy-all.sh#L29) → [rollout helper](../../../platform/scripts/gateway-rollout.sh#L1) | Canonical install/update path. Launcher builds gateway, renders base manifests and coordinates pricing/migrations; #7034 profile selection must survive this path, not only CI. |
| [gateway-deploy workflow](../../../.github/workflows/gateway-deploy.yml#L5) | `main` push path filters + manual dispatch; source/Docker/k8s/migrations/tracker/shared pricing can trigger. Docs excluded. Applies top-level `k8s/*.yaml`, not the nested profile. Actual target deployment must be verified privately. |
| [gateway buildspec](../../../codebuild/bs-gateway-build.yml#L1) → [shared image publisher](../../../platform/scripts/publish-shared-image.sh#L1) → [Dockerfile](../../../modules/gateway/Dockerfile#L1) | Source identity and immutable image publishing; preserve staged contracts, locked dependencies and build checks. Shared platform [CodeBuild infra](../../../platform/infra/modules/codebuild/main.tf#L1) provisions build projects; it does not certify performance. |
| [archive builder](../../../modules/gateway/scripts/build-budget-lambda-archives.py#L1), [pricing rollout](../../../modules/gateway/scripts/pricing-rollout.py#L1), [pre-serving migration](../../../modules/gateway/scripts/migrate-before-rollout.py#L1) | Tracker/pricing shared-package assembly and ordered deployment. #7032 dual-reader before journal producer; maintain migration/pricing compatibility. |
| [gateway infra apply](../../../.github/workflows/gateway-infra-apply.yml#L9), [platform infra apply](../../../.github/workflows/platform-infra-apply.yml#L11) | Manual infrastructure workflows for respective Terraform ownership; role/notification/lifecycle changes do not arrive merely because gateway source deployed. Plan/test counterparts are inventory entries, not deployment proof. |
| [migration workflow](../../../.github/workflows/run-gateway-migrations.yml#L1), [pricing finalize](../../../.github/workflows/pricing-finalize.yml#L1), [release reservation](../../../.github/workflows/gateway-release-reservation.yml#L1) | Existing guarded operations; do not create performance-only bypasses. Source permits identification of paths, not authority to execute them. |
| [gateway CI](../../../.github/workflows/gateway-ci.yml#L1), [hardening CI](../../../.github/workflows/gateway-hardening-ci.yml#L1), [smoke](../../../.github/workflows/gateway-smoke.yml#L1), [manual live tests](../../../.github/workflows/gateway-live-tests.yml#L1), [local test compose](../../../modules/gateway/docker-compose.test.yml#L1) | Reuse offline test conventions; #7036 adds explicitly protected manual campaign entry, not billed PR tests or relabelled smoke acceptance. |
| [release workflow](../../../.github/workflows/adp-release.yml#L1), [upgrade](../../../.github/workflows/adp-release-upgrade.yml#L1), [promotion](../../../.github/workflows/adp-release-promote.yml#L1) | Packaged/release distribution paths remain compatible with selected profile and source/image provenance. This epic does not authorize a release or promotion. |
| [frontend deploy](../../../.github/workflows/gateway-frontend-deploy.yml#L1), [frontend script](../../../modules/gateway/scripts/deploy-frontend.sh#L1), broker/bootstrap/wire-ALB scripts enumerated in ledger | No frontend/auth onboarding deployment changes proposed. Preserve routing and baseline access; frontend publish success is not inference health. |
| Agent-gateway/runtime/chat-agent, webhook-ingress, Agent Context, domain-app and other CodeBuild/launcher paths | Each recipe is individually mapped in the ledger. Excluded as deployment targets; explicit traffic clients/dependency consumers only where the campaign uses them. No hidden “gateway” rename may substitute the separate agent gateway for the inference proxy. |

A read-only workflow-history check on 2026-10-05 found a recent
[failed gateway run](https://github.com/aws-e/adp/actions/runs/37376326868) and an
earlier [successful run](https://github.com/aws-e/adp/actions/runs/37372263184).
Neither establishes what the future selected target runs or whether model
streams are healthy. This review did not diagnose the unrelated workflow failure.

## External semantics checked

Official references retrieved during this review (not evidence of live behavior):

- [PostgreSQL runtime timeouts](https://www.postgresql.org/docs/current/runtime-config-client.html):
  lock, statement and idle-in-transaction timeouts serve different boundaries;
  cancellation/rollback still needs verification through the driver.
- [S3 event notifications](https://docs.aws.amazon.com/AmazonS3/latest/userguide/EventNotifications.html):
  at-least-once delivery requires deduplication; a local scheduled task is not S3 acknowledgement.
- [Kubernetes HPA](https://kubernetes.io/docs/tasks/run-application/horizontal-pod-autoscale/):
  metric-driven desired replicas differ from available capacity; readiness and
  stabilization affect response to load.
- [Botocore configuration](https://botocore.amazonaws.com/v1/documentation/api/latest/reference/config.html):
  `total_max_attempts` includes the initial call; distinguish it from retry-count
  settings and account for effective SDK behavior.
- [CloudWatch authorization reference](https://docs.aws.amazon.com/service-authorization/latest/reference/list_amazoncloudwatch.html):
  `PutMetricData` is namespace-conditioned, not resource-ARN scoped. Existing
  repository policies demonstrate the same necessary wildcard exception.

## Full design coverage and unresolved gaps

| Spec section | Coverage | Open boundary |
|---|---|---|
| Description | Six existing children, exact completion owners pending, review versus deployed/live completion distinguished. | Coordinator must name actual executors/operators; no dispatch here. |
| Impact analysis | Tenant/auth/budget/price compatibility, provider duplicates, cost, resource budget, privacy, cancellation, recovery and non-goals in epic/stories. | Live quotas, cost caps and target constraints not accessed. |
| Design | Shared interfaces/state machines, investigation outputs, pool isolation, journal/receipt recovery, streaming truth, metrics and harness contracts. | Blocking SQL and Opus cause unknown; D2 exact-usage crash recovery unresolved; settings/SLO proposed, not measured. |
| Deployment | Full tracked entry-point enumeration; primary launcher/CI/build/infra chains traced; reader-before-writer, profile persistence and non-destructive rollback specified. | External build images/generated manifests, actual target image/config/IAM, dynamic operator overrides and provider infrastructure cannot be certified from the tracked tree. |
| Validation | Every epic AC and child AC retained and mapped; offline versus live tests, qualification matrix, shutdown/runner-loss cleanup and evidence ownership. | No live campaign, provider crash-recovery capability, SQL blocker graph, exporter-role attribution or cleanup receipt obtained in this design-only task. |

Re-review adds complete proposed admission authority, schema/mutation fencing,
quote dispatch and reconciliation contracts plus fourteen required fault cases.
Remaining implementation evidence includes transport-hook feasibility at the
actual send boundary, true bounds for each supported route, complete strict
policy reconstruction and measured SQL/alias contention cost. These are explicitly
tested activation prerequisites, not claims established by this design check.
The unknown-usage policy is still a **product decision**, not an implementation
test to silently redefine. No provider recovery capability is invented here.

Inventory has **zero unmapped tracked files**, not zero coverage uncertainty.
Runtime-generated resources, untracked private operator tooling, effective SDK
connections, admission identity mix, live migration state, actual database limits,
and protected campaign evidence are explicitly unresolved. These are live/behavior
evidence gaps, not deferred repository enumeration. The implementation backlog
must resolve them before its corresponding acceptance claim, without replacing
the complete inventory with a representative sample.

## Final review disposition — 2026-10-06

The earlier pending-R2 statements above describe the architect revision before
final review. The final contract uses [conservative containment](accounting.md#r2-conservative-recovery-boundary):
no financial exception, manual write-off or automatic release. The reviewer
explicitly corrects the assistant-authored AC-02 while retaining its ID. Unknown
usage remains unknown; saved trusted receipts settle exactly once. Runtime causes,
implementation proofs and qualification SLOs remain future checkpoints.
