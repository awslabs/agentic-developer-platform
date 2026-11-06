# Repository and release audit

## Evidence boundary and reproducibility

This is an exhaustive **tracked-artifact inventory and static entry-point audit**
of main `d8e8a2a5c5e8e540f243e4aee77dcf54b3689ecc`, not a claim that every application
path was executed. No deployment, AWS operation, live database read or paid test
was performed. The design uses source inspection; live qualification remains
MR-14. Counts are not test coverage percentages.

[artifacts.tsv](inventory/artifacts.tsv) contains exactly one row for each of the
**11,543** entries returned by `git ls-tree -rz --full-tree` at that revision, with
Git blob ID, component, disposition and matching identity/global-reference line
numbers. No directory is silently omitted. `review` means an affected component;
`support` means a boundary/build/regression dependency, not a promise to modify
all its files; `reference` is documentation; `excluded` gives the reason for no
behavioral change. The architecture/backlog narrow actual edits within those
components. Every excluded build entry is still enumerated.

[entry-points.tsv](inventory/entry-points.tsv) enumerates **2,106 distinct**
workflow/build/package/infra/script/handler candidates, each assigned to a component
or explicit exclusion. Categories overlap:

| Category | Artifact count | Enumeration rule |
|---|---:|---|
| GitHub workflows | 154 | Every tracked `.github/workflows/*.yml`/`.yaml`; all are retained even if unrelated. |
| Scripts/CLI tools | 1,159 | All shell/PowerShell files; Python/JS/TS scripts plus main/argv entry candidates. |
| Terraform files | 504 | All tracked `.tf`, not just gateway modules. |
| Build/package files | 114 | Dockerfiles, Make/Just/Task files, package/pyproject/setup/Cargo/Go/Helm manifests. |
| Runtime manifests | 82 | Kubernetes/Helm/chart/manifests/Compose YAML paths. |
| CodeBuild/build support | 49 | Every CodeBuild/buildspec path, including support code. |
| Lambda handler candidates | 43 | Python `handler`/`lambda_handler` definitions, including test false positives. |
| Composite actions | 13 | Every `.github/actions/**/action.yml`/`.yaml`. |

[global-readers.tsv](inventory/global-readers.tsv) retains all **247 matches in
97 files** for global routing settings, `config.repo`, `configured_repo` and the
installation selector; both false positives and historical references remain
visible. [migrations.tsv](inventory/migrations.tsv) enumerates all **135 migration
files**, revision/down-revision edges and literal operation summaries. Static AST
parsing identifies one head, `122_product_handoff_callbacks`, and zero missing
parents. The summaries include downgrade operations; they are not a substitute
for reading a migration when implementing DDL. [summary.json](inventory/summary.json)
contains exact totals by component.

Reproduce from the repository root with Python 3 and Git; no application packages,
network, credentials or environment configuration are required:

```bash
python3 docs/design/story-bound-repositories/audit.py --output /tmp/story-repo-audit
diff -ru docs/design/story-bound-repositories/inventory /tmp/story-repo-audit
python3 docs/design/story-bound-repositories/validate.py
python3 scripts/check-public-docs.py
```

The pinned revision is the default; `--revision COMMIT` produces a separately
reviewed later inventory. Do not replace the baseline silently. The generator
reads Git blobs, never imports application code or executes deployment scripts.

## Component mapping

| Component | Count | Design disposition |
|---|---:|---|
| Engine | 193 | MR-01..12: all orchestration source artifacts mapped; shared unchanged helpers retain existing semantics. |
| Identity/access | 269 | MR-02..06: installation ownership, broker/grant and verified event boundaries; other identity features unchanged. |
| Schema | 137 | Alembic support plus all migrations; additive changes only, no unrelated data-store redesign. |
| Gateway regression tests | 586 | Existing orchestration/agentauth/CLI suites; add behavior assertions in owning child. |
| Gateway release | 244 | Infra, ConfigMap, packaging, migration/readback/synchronization entry points. |
| Frontend | 569 | MR-08 orchestration UI; other product views are regression boundaries. |
| CLI | 61 | MR-07 authoring/download distribution; unrelated tools unchanged. |
| Contracts | 13 | Shared review/evaluation contracts; version, do not reinterpret existing schemas. |
| Worker/ingress | 1,475 | Envelope/report/grant consumers and packaging; rules/personas and unrelated jobs are not rewritten. |
| Qualification | 54 | MR-13/14 existing runner, config, fixture, scenario, resume and cleanup stack. |
| Shared release | 933 | All workflow/platform/CodeBuild/config/root scripts; non-orchestration deploys excluded from changes. |
| Documentation | 2,160 | References only; historical instructions are not runtime authorization. |
| Gateway other | 1,394 | Exclude metering, proxy and unrelated product behavior, except mapped boundaries above. |
| Domain apps | 1,844 | Exclude domain feature changes; no application-specific topology in this epic. |
| Agent Context | 522 | Exclude indexing/search/knowledge behavior; similarly named lookup is not engine dispatch. |
| Harness | 72 | Exclude general harness-job redesign; existing shared packaging references remain inventoried. |
| Insights | 75 | Exclude unrelated reporting/catalog semantics; do not invent consumer metadata. |
| Platform deploy management | 23 | Exclude new deployment-management capability. |
| Research | 46 | Exclude research implementations. |
| Source control | 22 | Exclude new VCS/provider abstraction. |
| Tools | 241 | Exclude general tool/runtime features; worker build inputs remain enumerated. |
| User services | 81 | Exclude new user-service/credential products. |
| Other tests | 320 | Exclude unrelated behavior suites from epic implementation requirements. |
| Repository tooling | 209 | Explicit exclusion for root assets/tooling not implementing story routing. |

## Complete routing-reference disposition

The following partitions account for the production engine callers found by the
repository-wide global-reference scan. Full paths/line numbers are in the TSV;
filenames here are under `modules/gateway/src/orchestration/` unless qualified.
These are not a representative subset of those callers.

| Owner | Every matched engine/authority source file | Required disposition |
|---|---|---|
| MR-03 | `dispatch_pass.py`, `protected_dispatch_recovery.py`, `results.py`, `controls.py`, shared `routes.py` readiness | Separate runtime config from accepted/assigned target; observe/replay via frozen connection. |
| MR-04 | `report_dispatch.py`, `diagnose.py`, `merge_evidence.py`, `pr_bindings.py`, `delivery_adoption.py`, shared `routes.py` PR recovery | Use execution/PR identity and current authority; preserve exact-head and recovery fencing. |
| MR-05 | `agentauth/coordinator.py`, `agentauth/graph_dispatch.py`, `agentauth/waves.py`, `agentauth/routes.py` | Repository-qualified graph lookup and parent/child authority; no global equality. |
| MR-06 | `engine_commands.py`, `tracker_projection.py`, `authoring_dispatch.py` | Verified command source and separately bound origin/tracker/authoring location. |
| MR-07 | `intake_routes.py`, `amendment_issue_publisher.py` | Per-story resolver and provider-ID publication identity. |
| MR-10 | `repository_evaluation.py`, `repository_producer.py` | Independent source/harness connection resolution in new evidence mode. |

Non-global but necessary consumers are also inventoried by identity/evidence
symbols, including `proposal.py`, compile/amendment/snapshot/accepted readers,
`work_admission.py`/claims, review/shared-cycle/protected recovery, evaluation
controller/plan/evidence/correction and frontend contracts. Their story ownership
is explicit in [backlog.md](backlog.md); removing only literal global environment
references would miss them.

False-positive/exclusion decisions from the broader scan:

- `modules/agent-context/agent_context/api/github_repos.py` and
  `modules/gateway/src/knowledge/github_repos.py` have a distinct
  `_resolve_installation_id` for repository listings. Do not blanket-replace it
  with engine authority logic. The shared resolver consumes registered
  connections through the existing access boundary, not a new knowledge feature.
- `modules/agent-factory/agent/src/token-refresh.ts` uses invocation-local
  `config.repo` to narrow token refresh. Preserve that restriction; MR-03 checks
  the assignment feeding it rather than treating it as a global setting.
- `platform/evaluator-infra/legacy-existing/main.tf`'s `var.config.repository_id`
  is a reviewed artifact read scope, not flow execution routing. Preserve it.
- Harness `config.repo` occurrences are fixture/runner configuration. MR-13
  extends typed A/B/C/H fields; it does not delete harness identity.
- Budget tests, test-duration metadata, historical design docs and Agent Context
  eval configuration are not production orchestration routing. They remain
  explicitly classified; offline regression references are updated by their
  owning story only if the tested interface changes.

## Storage and schema audit

[Node/accepted-plan models](../../../modules/gateway/src/orchestration/models.py#L340)
already separate accepted documents from graph projections. Existing migration
families include `029_orchestration_graph`, `050_orchestration_work_claims`,
`051_orchestration_pr_bindings`, `052_orchestration_executions`,
`054_execution_tenant_guards`, `063_pr_binding_adoption` and
`085_amendment_issue_bindings`. The complete graph, including all later merges and
product/metering migrations, is recorded, not inferred from those examples.

Existing claims have a tenant/repository/issue uniqueness constraint; PR bindings
carry repository and provider PR IDs. Executions/actions hold durable operations
and fencing. Amendment issue bindings currently index tenant + repository **name**
+ proposal identity despite a nullable provider repository ID; MR-07 must preserve
replay identity across rename (models.py:1423). Proposed projection fields and
origin/tracker references do not already exist on node/flow models. Keep existing
`org_id` storage tenancy; new APIs use `tenant_id` consistently.

The [webhook/authority Terraform](../../../modules/agent-factory/webhook-ingress/infra/dynamodb.tf#L296)
uses authority `pk`/`sk` and a narrowly named replay TTL; event records use
`event_id`/`arrived_at`, and tenant registry uses installation ID. Do not add a
second repository/credential table or broaden TTL to pending execution history.
Existing [authority store](../../../modules/gateway/src/agentauth/store.py#L106)
and [grant producer](../../../modules/gateway/src/agentauth/engine.py#L126) remain
the protected worker boundary. No new Secrets Manager path, evidence bucket,
worker database grant or AWS wildcard permission is proposed.

## Deployment and build map

All tracked entry points, including unrelated ones, are in the TSV. The following
is the **affected delivery chain**, with explicit side branches/exclusions, not a
replacement for that enumeration. Relative source links refer to the unchanged
pinned baseline above; the inventory supplies each exact Git blob.

| Entry point / component | Verified trigger or caller | Design consequence / owner |
|---|---|---|
| [Gateway deployment](../../../.github/workflows/gateway-deploy.yml#L5) | Matching main push or manual dispatch; excludes documentation-only changes. | MR-01..12 backend delivery; root-contract-only changes currently absent from path filter and changed-file build selection. MR-09 updates both. |
| [Gateway frontend deployment](../../../.github/workflows/gateway-frontend-deploy.yml#L4) | Matching frontend main push or manual; publish job guards main. | MR-08: API first, UI second, verify installed frontend revision. |
| [Gateway infra apply](../../../.github/workflows/gateway-infra-apply.yml#L1) | Manual dispatch, not automatic from a code merge. | MR-12 config/schema wiring handoff; use existing plan/approval/destruction controls. |
| [Gateway Dockerfile](../../../modules/gateway/Dockerfile#L1) | Gateway build context; same image serves FastAPI and Lambda tick. | MR-09 package contracts and self-check; gateway/tick must agree. |
| [Gateway buildspec](../../../codebuild/bs-gateway-build.yml#L1), [shared publisher](../../../platform/scripts/publish-shared-image.sh#L68) | Buildspec calls publisher; gateway path runs contract staging before Docker build. | MR-09 verify new family in context; MR-12 retains source/digest identity. |
| [Contract staging](../../../modules/gateway/scripts/stage-contracts.sh#L1) | Copies root contract definitions into gateway build context. | MR-09 extend selectors/self-checks; no duplicate schema copy maintained by hand. |
| [Migration runner](../../../.github/workflows/run-gateway-migrations.yml#L1), [pre-rollout migration](../../../modules/gateway/scripts/migrate-before-rollout.py#L1) | Reusable/manual migration workflow; gateway release's pre-serving migration step. | Additive DB before dependent readers; retain schema on rollback. |
| [Gateway writer synchronization](../../../modules/gateway/scripts/sync-gateway-writers.py#L1), [engine sync](../../../modules/gateway/scripts/sync-gateway-engine.py#L1) | Gateway deployment calls before/around serving release; tick sync explicitly present. | MR-12 compatible writer floor and rollback inventory; do not update only web pods. |
| [Tick Terraform](../../../modules/gateway/infra/modules/orchestration-tick/main.tf#L173) | Lambda image command `src.orchestration.tick_handler.handler`; root gateway infra instantiates it. | MR-12 remove `BG_ORCH_DISPATCH_REPO` injection; keep queue/runtime config. |
| [Root gateway infra](../../../modules/gateway/infra/main.tf#L1), [variables](../../../modules/gateway/infra/variables.tf#L1), [tick variables](../../../modules/gateway/infra/modules/orchestration-tick/variables.tf#L1) | Terraform inputs/module wiring; environment tfvars supply values. | MR-12 retire global variable in each layer without changing target role authority. |
| [Coordinator SSM input](../../../modules/gateway/infra/agent-authority-coordinator.tf#L1), [release configuration](../../../modules/gateway/infra/release-configuration.tf#L1) | Persist global routing input/release state. | MR-12 controlled removal; it is not sufficient to delete only ConfigMap text. |
| [Gateway ConfigMap](../../../modules/gateway/k8s/configmap.yaml#L1), [runtime readback](../../../modules/gateway/scripts/stage1-runtime-readback.py#L1) | Deployment substitution/readback. | MR-12 readiness must not expect the retired key. |
| [Root launcher](../../../deploy.sh#L1), [deploy-all](../../../platform/scripts/deploy-all.sh#L1553) | Fresh-install and release launcher chain, not a main-push auto-deploy. | MR-12 remove SSM read/substitution in fresh and upgrade paths. No deployment run in this assignment. |
| [Preserved inputs](../../../config/release-defaults/preserved-inputs.json#L44), [upgrade state](../../../platform/scripts/upgrade-state.py#L165), [shared-runtime guard](../../../platform/scripts/shared_runtime_plan_guard.py#L192), [maintenance](../../../platform/scripts/maintain-shared-runtime.py#L1) | Release input preservation and shared-runtime maintenance. | MR-12 schema-aware retirement so upgrades do not resurrect the global setting; preserve unrelated inputs. |
| [Worker image workflow](../../../.github/workflows/agent-worker-image.yml#L15), [worker Dockerfile](../../../modules/agent-factory/agent-worker-image/Dockerfile#L429), [runtime buildspec](../../../codebuild/bs-agent-runtime.yml#L1) | PR/main path checks + manual workflow; Dockerfile copies root contracts. | MR-03/09 validate consumer compatibility; root-contract-only trigger coverage is missing here too. |
| [Webhook deploy script](../../../modules/agent-factory/webhook-ingress/scripts/deploy-webhook-ingress.sh#L1) | Launcher/manual path packages ingress plus runtime and Terraform. | Preserve verified event/source envelope and separate scaledjob authority; no broad grants. |
| [Agent factory infra apply](../../../.github/workflows/agent-factory-infra-apply.yml#L1) | Manual infra lane. | No new infra required by design; any discovered IAM delta must be separately scoped/reviewed. |
| [Gateway CI](../../../.github/workflows/gateway-ci.yml#L1), [agent control CI](../../../.github/workflows/agent-control-ci.yml#L1), [review contract CI](../../../.github/workflows/orchestration-review-contract-tests.yml#L1) | Reusable/PR/manual or main filters as enumerated in TSV. | MR-09 adds new contract-family selection; MR-01..11 run affected offline suites, not paid live qualification. |
| [Harness CI](../../../.github/workflows/orchestration-harness-ci.yml#L18) | Reusable, matching PR/main; tests disable AWS credential discovery. | MR-13 offline scenario/fixture validation; add new contract-family path when needed. |
| [Live qualification](../../../.github/workflows/orchestration-live-tests.yml#L22) | Manual only; preflight/run/resume/cleanup; resume restores source-run inventory. | MR-14 named operator, reviewed private config, finite limits; paid work not authorized by this PR. |
| Release/promote/upgrade workflows, other module/domain deployment workflows | Every entry enumerated in TSV with event summary. | Preserve release artifact compatibility; unrelated deployment targets/builds are excluded, not assumed to inherit this feature. |

The shared image publisher runs gateway review/evaluation self-checks and the
worker contract self-check after building (publish-shared-image.sh:72). MR-09 must
add the new contract family to those checks and the workflow source selectors.
A green build with stale packaged contracts is not acceptance.

## Coverage gaps and check record

**No unclassified tracked artifact remains at the pinned baseline.** This is an
exhaustive inventory, not a representative sample or a request for MR-12 to do the
inventory later. The following evidence limits remain explicit:

1. Runtime-created workflows, external fixture repositories, generated images and
   live configuration are not tracked source artifacts. Their actual installed
   contents/access are unverified; MR-14 records and verifies them. This does not
   imply those environments or connections are absent.
2. Entry-kind detection is intentionally conservative: script/handler candidates
   include helpers and tests. Event summaries list events/filter classes, not all
   job-level `if` conditions. Affected workflows' main/manual and staging paths
   were inspected directly in the map; excluded workflows are inventoried, not
   represented as executed or fully behavior-audited.
3. Text matching cannot prove absence of every indirect runtime assumption. The
   design additionally follows accepted-plan → assignment → claim/grant → PR →
   evaluation/correction flows. Complete offline lifecycle and packaged-consumer
   checks remain implementation acceptance, not claimed design-test results.
4. Open PR #8428 changes review recovery, and #7314 concerns project binding. They
   are not part of this baseline or proof of availability. Integrating owners
   recheck merged changes before touching shared files. No native dependency was
   changed as a shortcut.
5. Database migration analysis is static, including revision merge ancestry. No
   live schema or upgrade/downgrade execution was inspected. New additive
   migrations require PostgreSQL fixtures and the actual current head at coding.

This bundle is validated by deterministic regeneration, tracked-path/blob coverage,
source-link/line and local-anchor checks, public-document scanning and whitespace
checks. Exact publication check outcomes are recorded in the PR. Application,
container and live tests are not claimed for a documentation-only change.


## Product/epic amendment review

The inventory above remains pinned to its original source revision; this amendment
adds a targeted check at main `9b83cdd4ceb8b223a66209e049666485bab2747d`, not a claim
that the entire later tree was re-audited. Product identity already exists in
`modules/gateway/src/products/models.py`; existing membership code is in
`shared/identity/product_scope.py`. MR-01 consumes that identity/access contract
without adding a Project workflow. The agent's `src/sub-issues.ts::addSubIssue`
currently resolves both issues using one owner/repo pair; MR-07 must use separate
parent/child references through the gateway publication/provider path. Existing
amendment publication records already separate provider effects and durable
reconciliation, providing the extension point for issue-created/link-confirmed
progress. Exact source links are in the architecture's amendment source checks.
No provider capability, live native hierarchy or deployed Product integration is
claimed from this static inspection.
