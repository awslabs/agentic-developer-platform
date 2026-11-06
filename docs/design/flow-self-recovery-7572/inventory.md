# Repository-wide coverage audit

## Boundary and complete enumeration

Audit revision: `849f6163e59855c1554bc263e9ad96695730e9bd` (2026-10-13).
This is a **complete tracked-tree enumeration**, not a representative module
sample. It is not a claim that every component is deployable, that every dynamic
dependency was resolved, or that production resources were inspected.

| Artifact | Coverage and interpretation |
|---|---|
| [tracked-artifacts.tsv](tracked-artifacts.tsv) | Exactly 10,139 tracked artifacts, one row each: path, blob identity, mode, audit component/disposition, actual first matching engine rule (or UNMAPPED), detected surface. Covers runtime source, infrastructure, docs, tests, data and records. |
| [entrypoints.tsv](entrypoints.tsv) | All 1,967 conservatively detected build/deploy/validation surfaces, with owner, observed workflow event names, existing manifest classification, statically referenced components and reverse source callers. Includes every tracked workflow/container/Terraform/package entry and shell script, not only affected services. |
| [source-references.tsv](source-references.tsv) | All 16,211 detected local path-reference occurrences from those surfaces, with source line and tracked-match count. Dynamic/generated/missing references are explicitly marked, not dropped. A text reference is not proof of execution. |
| [gateway-migrations.tsv](gateway-migrations.tsv) | All 101 gateway migration files, revision and actual parent/merge-parent declarations, and statically named tables for create/add/drop-column operations. This last column is an index, not a complete SQL interpreter. |
| [audit-results.md](audit-results.md) | Counts by existing engine component and audit ownership; generated rather than hand-maintained. |
| [audit.py](audit.py) | Reproducer reads the pinned Git tree without executing application/configuration code, prints an apply_patch-compatible artifact update with `--patch`, or checks committed outputs without it. |

Every tracked row has an audit owner and disposition. These are review groupings,
not minted module/feature IDs and not deployment authorization. Non-runtime
records are explicitly excluded; test surfaces are validation rather than release
targets. Unmapped runtime surfaces retain an owner and remain coverage gaps.
The audit itself does **not** change the runtime manifest.

### Enumeration method

`git ls-tree -r` supplies exact tracked paths/blob identities; `git archive` reads
the same immutable tree. The reproducer applies all ordered inline `path_rules`
from the packaged engine manifest with Python `fnmatchcase`, matching the
engine's documented first-match semantics (`*` crosses `/`). It asserts that all
rule declarations were parsed and every tracked artifact appears once.

Entrypoint enumeration includes 148 workflows, 13 local composite actions,
70 container definitions, 31 buildspec-family artifacts, 41 package/build
entries, 449 Terraform definitions, 99 Kubernetes/manifest definitions,
572 script entries and 1,087 files referencing build/deploy commands. Categories
overlap; their sum is not the number of unique surfaces. Command-reference
detection intentionally includes tests and validators rather than quietly
missing a deployment helper embedded in Python/TypeScript. Source-reference
matching includes directories/globs and records unresolved/generated inputs.

The 101 migration declarations have no duplicate revision IDs or missing parent
references and yield one static head, `094_credential_agent_consent`. This is
source-graph validation, **not** `alembic upgrade` or live schema verification.
Relevant existing stores were inspected in the [design's storage section](design.md#storage-fit-and-compatibility);
the proposed archive column is absent in this baseline. No new credential path,
table/bucket family or alternative deployment identity service is invented.

## Coverage findings

Actual engine rules classify **3,658 paths as UNMAPPED**, **3,767 as ignore** and
the rest into existing components. **1,391 detected entrypoint/command-reference
surfaces are unmapped**; this includes tests and supporting infrastructure, not
1,391 independently deployable services. Full per-path evidence is in the TSVs.

| Gap / scope | Concrete evidence and consequence | Disposition / owner |
|---|---|---|
| Runtime Markdown incorrectly ignored | 51 tracked files under agent-factory rules/skills match the generic `*.md` ignore despite being runtime policy/skill assets; worker Dockerfile/workflow consume those trees | 7457: map runtime assets to hosted-worker consumers before generic documentation exclusions. Worker owner validates image-trigger parity. |
| Worker build vs legacy chat mapping | Manifest maps `chat-agent-deploy.yml` and `agent/**`, but hosted image also consumes codex-harness/reviewer, task SDK, rules and skills; these are partly ignored/unmapped | 7457: multi-consumer mappings and actual hosted-worker workflow/verification, not assumed chat rollout. |
| Unmapped infrastructure and lifecycle | Shared platform, automation state, platform scripts, module infra/workflow families not covered by existing entries | Explicit platform/automation/lifecycle owners in inventory; retain manual/privileged boundary. Unknown deployable paths must block, not become documentation. |
| Gateway catch-all hides distinct releases | Backend mapping covers gateway CLI, infra, Lambda and helper paths not all released by the same push trigger; frontend has its own workflow | Separate gateway-backend/frontend/migrations/CLI/infra/Lambda consumption. Do not equate a matched rule with a workflow trigger. |
| Tools roots and SDK consumers | Tools task SDK is copied into worker images; tools validation/AgentCore have their own infrastructure and workflows; top-level platform ECR root and nested platform infra are separately tracked | Keep distinct roots/owners, verify Terraform ownership before state split; no inferred live state ownership. |
| Other application families | Superplane, Cyber, agent-context outside Door, harness jobs/MCP, dev-box, insights, research/source-control and deploy-management trees all enumerated | Existing owners retained; 7457 must add reviewed mappings or explicit non-release exclusions, not force them through tools/gateway. This PR does not redesign these applications. |
| Path matching semantics | Existing first-match single-component rules cannot directly describe one shared file requiring multiple consumer releases; generic CI/Markdown ignores can swallow runtime inputs | Add reviewed multi-consumer selection and regression fixtures; do not silently switch glob semantics in a schema migration. |
| Dynamic/external dependencies | 1,297 reference occurrences (687 distinct strings) are dynamic, generated or unresolved by static matching | All recorded in source-references.tsv. Generated working-directory paths, shell variables and external actions/images need their component owner's runtime/build evidence before enabling autonomous delivery. No assertion they are all defects. |
| Private target configuration | Tracked source cannot establish which role/environment/state/RBAC is actually installed | Platform/environment owner supplies selected-connection readback under existing authority; never a reason to publish private target values. |

Unknown mappings are fully inventoried here rather than deferred discovery.
Closing a mapping gap means reviewing a component/exclusion decision and its
actual consumer/trigger/verification contract, not making the UNMAPPED count zero
with a blanket ignore. Existing CI definitions can themselves publish artifacts
or drive privileged reusable workflows; filename suffix is not an exclusion rule.

## Delivery paths

The complete entrypoint registry is in `entrypoints.tsv`. This table explains
the consequential paths for the nine stories and their supporting release
families; it is a guide into that registry, **not a substitute for it**. Links
resolve in this branch; blob identities and original lines are retained in the
inventory at the stated baseline.

| Component | Actual entry points and consumers | Trigger / scope / design consequence |
|---|---|---|
| Gateway backend and engine | [gateway-deploy.yml](../../../.github/workflows/gateway-deploy.yml), [migrate-before-rollout.py](../../../modules/gateway/scripts/migrate-before-rollout.py), [sync-gateway-engine.py](../../../modules/gateway/scripts/sync-gateway-engine.py) | Selected source/migration/build paths on main push, or manual dispatch; maintain matching gateway/engine release. Workflow has explicit path filters. |
| Gateway database | [run-gateway-migrations.yml](../../../.github/workflows/run-gateway-migrations.yml), [versions](../../../modules/gateway/alembic/versions) | Reusable/manual migration path plus gateway release coordination; apply archive schema before compatible writers. Source graph alone does not verify deployed DB. |
| Gateway UI | [gateway-frontend-deploy.yml](../../../.github/workflows/gateway-frontend-deploy.yml), gateway deploy's frontend job | Frontend-only main pushes have their own publishing workflow. API/UI compatibility spans different completion times. |
| Gateway CLI | [cli](../../../modules/gateway/cli), [gateway Dockerfile](../../../modules/gateway/Dockerfile) | Included gateway paths trigger its build, but installed client refresh is separate; check distribution/install route when implementing archive/amend display. |
| Gateway budget Lambdas | [archive builder](../../../modules/gateway/scripts/build-budget-lambda-archives.py), [pricing finalization](../../../.github/workflows/pricing-finalize.yml), gateway workflow | Usage tracker/pricing packages and shared policy are distinct artifacts; gateway workflow selects affected archives. 7571 must preserve existing settlement replay. |
| Hosted agent worker | [agent-worker-image.yml](../../../.github/workflows/agent-worker-image.yml), [worker Dockerfile](../../../modules/agent-factory/agent-worker-image/Dockerfile), [bs-agent-runtime.yml](../../../codebuild/bs-agent-runtime.yml), [rollout-worker-image.py](../../../platform/scripts/rollout-worker-image.py) | PR tests, main push build/test/promotion, manual option; immutable image for new jobs, running jobs retain old image. This is the relevant 7571 worker path. |
| Legacy/chat worker surfaces | [chat-agent-deploy.yml](../../../.github/workflows/chat-agent-deploy.yml), [agent source](../../../modules/agent-factory/agent) | Separate push filters/legacy approval boundaries; do not infer hosted KEDA worker delivery merely from a chat build. Shared proxy can affect both. |
| Webhook/factory infrastructure | [webhook-ingress-deploy.yml](../../../.github/workflows/webhook-ingress-deploy.yml), [webhook-code-deploy.yml](../../../.github/workflows/webhook-code-deploy.yml), [agent-factory-infra-apply.yml](../../../.github/workflows/agent-factory-infra-apply.yml) | Code publication and infrastructure setup have distinct identities/triggers; factory infrastructure apply remains explicit/manual. No apply required by this design run. |
| Gateway infrastructure | [gateway-infra-apply.yml](../../../.github/workflows/gateway-infra-apply.yml) | Manual protected apply for grants/config/resources; not automatic because a gateway code story merged. |
| Automation permissions | [automation-infra README](../../../platform/automation-infra/README.md), [tools-deployment.tf](../../../platform/automation-infra/tools-deployment.tf), [trust inventory](../../../platform/automation-infra/deployment-workflows.json) | Default-off role exists; checked-in environment opt-in is true, which is not evidence of apply. Separate state owner reviews/applies. Scope defect must be fixed before relying on role. |
| Tools façade | [tools-facade-deploy.yml](../../../.github/workflows/tools-facade-deploy.yml), [tools deploy launcher](../../../modules/tools/deploy.sh), [façade infra](../../../modules/tools/platform/infra/facade.tf) | Main push filters/manual dispatch, guarded source/definition and protected environment; build, release and deployment context. Needs foundational identity/cluster/registry/config. |
| Tools platform | [tools-platform-deploy.yml](../../../.github/workflows/tools-platform-deploy.yml), [nested infra root](../../../modules/tools/platform/infra), [top-level ECR root](../../../modules/tools/platform) | Main push/manual apply path; plan/execute stage guards and private overlay. Split privileged bootstrap resources before claiming release-only authority. |
| Tools interceptor | [tools-interceptor-deploy.yml](../../../.github/workflows/tools-interceptor-deploy.yml), [release script](../../../modules/tools/interceptor/deploy.sh) | Main push/manual publication, hash-verified numbered version and alias; requires existing function/config. Missing from trust inventory at baseline. |
| Other tools services | [AgentCore tools](../../../modules/tools/agentcore), [validation infra](../../../modules/tools/validation/isolation-infra), [task SDK](../../../modules/tools/task-sdk) | Separate manual/domain services and shared runtime inputs, enumerated in full; not automatically included in the three tools story workflows. |
| Other modules | [agent-context](../../../modules/agent-context), [domain-apps](../../../modules/domain-apps), [harness](../../../modules/harness), [user-services](../../../modules/user-services), [insights](../../../modules/insights) | Every workflow/script/container/infra surface listed in entrypoints.tsv and linked to static inputs; release authority stays with its component. No unrelated implementation proposed. |
| Full install/release/teardown | [deploy.sh](../../../deploy.sh), [deploy-all.sh](../../../platform/scripts/deploy-all.sh), [undeploy.sh](../../../platform/scripts/undeploy.sh), [deploy guide](../../adp-platform-deployment/deploy-with-agent.md) | Launcher orders core/broker/bootstrap/webhook/factory/final frontend; GitHub App wiring at end. Not a substitute for incremental component rollout, and never invoked here. |

## Observed workflow state, not live health

Read-only GitHub run listings were checked during this review. The following
observations are a bounded sample of recent runs, **not** an exhaustive deployment
history or proof of live resources. No raw job logs or target configuration were
published. The all-repository source entrypoint enumeration above is exhaustive;
this runtime observation is deliberately not presented as one.

| Workflow | Observed run | What it proves / does not prove |
|---|---|---|
| Gateway Deploy | [38346326949](https://github.com/aws-e/adp/actions/runs/38346326949), pending at read, audit SHA | The latest audited source was queued; it was not verified live here. An earlier success at another SHA cannot establish this revision's availability. |
| Hosted legacy/chat release | [38328677659](https://github.com/aws-e/adp/actions/runs/38328677659), failure | That run did not succeed; no conclusion about the currently running hosted worker image. |
| Tools façade | [38334266919](https://github.com/aws-e/adp/actions/runs/38334266919), failure | Recent pipeline failure persists in the listing; source inspection cannot attribute it to the historical missing-environment cause. |
| Tools platform | [38339749818](https://github.com/aws-e/adp/actions/runs/38339749818), failure | No successful current apply proven; do not claim gateway absence from this alone. |
| Tools interceptor | [38339749807](https://github.com/aws-e/adp/actions/runs/38339749807), failure | No successful current publication proven; actual function existence/configuration remains unverified. |

The claim in the supplied issue that tools resources did not exist is historical
operator evidence. It is not repeated as a current verified fact. AWS and private
GitHub environment configuration were deliberately not inspected or changed.

## Reproduction and unresolved coverage

From repository root, Python 3.9+ and Git with the pinned revision available:

```bash
python3 docs/design/flow-self-recovery-7572/audit.py
```

Expected: all five generated artifacts match the pinned source. This script
does not need application dependencies, AWS/GitHub credentials or network.
It checks enumeration and recorded mappings, not behavior of the planned engine.

Remaining coverage limits are explicit: dynamic/generated dependency resolution,
external actions/images, actual protected environment bindings, actual deployed
schema/resources/roles, selected connection access, all nine implementation test
results and the 24-hour live window. Those do not make the tracked inventory
incomplete; they prevent treating static enumeration as permission, deployment
readiness or live acceptance. The implementation backlog closes specific mapping
and contract defects, while each runtime owner supplies authorized live evidence.
