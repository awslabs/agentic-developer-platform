# Repository and delivery audit

## Coverage boundary

Baseline: main `31148f8edfda23f162cf4b4705b7820710621a66`, fetched 2026-10-24.
The working branch begins with a no-content WIP commit on that revision.
The inventory covers **all 12,019 tracked artifacts**, including hidden paths,
not only files whose names mention Security. It classifies 2,651 executable,
build/package, deployment, migration, workflow or local-action entry artifacts,
all 155 workflows, all 148 gateway migrations and 2,004 Terraform declarations.
No baseline paths are omitted/unmapped. This is exhaustive tracked-tree
enumeration and source-level integration review, **not** proof that every dynamic
command, external action or deployed resource has been executed or audited.

| Record | Content and reproduction |
|---|---|
| [tracked-artifacts.tsv](tracked-artifacts.tsv) | Every path, Git blob ID, component, disposition and entry roles. Explicit exclusions preserve other modules and nightly/spike ownership. |
| [entry-points.tsv](entry-points.tsv) | Every detected executable/build/deployment/migration/action/workflow entry, not a sample. Includes scripts without execute bits and declared package entry files. |
| [workflows.tsv](workflows.tsv) | Every workflow's actual event names, job IDs, path/branch filters and statically referenced local dependencies. |
| [resources.tsv](resources.tsv) | Every Terraform resource/data/module declaration by path/line/logical name; no real account/resource values copied. |
| [migrations.tsv](migrations.tsv) | Complete topological upgrade DAG, parents/dependencies and operation locations. SQL operations point back to source rather than copying private values. |
| [inventory-summary.json](inventory-summary.json) | Counts, migration head, component totals and explicit semantic coverage limits. |
| [audit_inventory.py](audit_inventory.py) | Deterministic Git-object scan; `--check` compares every generated artifact to the pinned tree. |

The migration head at audit is `133_merge_dev_box_conversations`. Earlier duplicate
numeric prefixes are branches, not an ordering key. L5 has no baseline tables or
package. Inspected Product foundation/source/operation/handoff migrations
`112_product_profile_foundation`, `117_product_source_bindings`,
`118_product_verification_operations`, `122_product_handoff_callbacks` and current
`src/products/models.py` establish existing Product ownership. Tracker publication
`126_tracker_publication_outbox` and `130_shared_publication_intent` are specifically
orchestration-owned; do not reuse those tables for L5 jobs. SQL-inferred ownership
is not a live schema query, and raw SQL/dynamic migration effects remain a stated
limit of automated schema extraction.

## Deployment change matrix

Every entry appears in the TSV inventory; this matrix selects the changes needed
for L5 rather than claiming unrelated entries should change. Paths are existing
unless marked proposed. SA15 owns coordinated host/build changes; domain files
stay module-owned. An entry not selected here retains the classified component's
existing ownership and does not become an L5 publisher.

| Entry / source evidence | Current role | Required design change or explicit exclusion |
|---|---|---|
| `.github/workflows/gateway-ci.yml` | Gateway tests/path filters | Add exact-case `modules/L5-services/**` and `docs/l5-services/**` inputs to the applicable runtime/contract/documentation checks. |
| `.github/workflows/gateway-deploy.yml:7`, `:197`, `:203`, `:377` | Main path filters + separate internal changed-file predicates; named budget/pricing Lambda publication | Extend **both** filters and internal predicates for L5 runtime/migrations/contracts. Add explicit L5 worker publish job; existing budget Lambda loop is not reusable L5 discovery. |
| `.github/workflows/gateway-frontend-deploy.yml` | Separate frontend delivery | Retain publisher; trigger on frontend and consumed L5 UI contract changes. No Lambda publication here. |
| `.github/workflows/gateway-infra-plan.yml`, `gateway-infra-apply.yml` | Plan and manual infra apply | Add L5 infra/package inputs to plan. Apply remains manual/protected; instantiate proposed module from gateway root. |
| `.github/workflows/run-gateway-migrations.yml`, `postgres-schema-check.yml` | Migration execution/schema checks | Install same L5 wheel, register additional version location and metadata, upgrade heads before deployment. Test old rows and rollback reader. |
| `modules/gateway/alembic/env.py:19`, `:32`; `alembic.ini` | Host metadata and version search | Add explicit L5 metadata/version-location registration; no copied domain migration bodies. |
| `modules/gateway/scripts/migrate-before-rollout.py`, `sync-gateway-engine.py`, `sync-gateway-writers.py` | Ordered gateway writer rollout | Include L5 schema compatibility/worker quiescence in release gates; retain independent engine writer contracts. |
| `modules/gateway/src/products/runtime_proof_routes.py`; `modules/gateway/src/orchestration/deployment_runtime_reader.py`; `modules/gateway/scripts/pricing-rollout.py` | Runtime schema/release proof and existing migration consumers | Audit single-head assumptions against the L5 branch/version-location design; update only compatibility checks that otherwise reject the intended multi-head state. |
| `modules/gateway/scripts/stage-contracts.sh`; `Dockerfile:1`; `pyproject.toml:1` | Module-local build context and pinned dependencies | Proposed `modules/L5-services/scripts/build-package.sh` packages versioned schema artifacts from `docs/l5-services/contracts/v1/` into one wheel/lock; stage digest-verified wheel before host build; install it in final image. Test no checkout imports or runtime reads from docs. |
| `codebuild/bs-gateway-build.yml:1`; `platform/scripts/publish-shared-image.sh:68` | Shared-image publisher calls stage-contracts then builds inside gateway | Stage the L5 artifact through this shared path; preserve source/digest receipts. |
| `.github/workflows/_build-python.yml`; `codebuild/security_image_targets.py`; `codebuild/security-image-recipes.json` | Reusable builds/security target inventory | Include new worker artifact/dependency scan in relevant inventories; do not assume the existing Python image job builds Lambda ZIPs. |
| `modules/gateway/cli/adp`, `command-manifest.json`, `install.sh`; `modules/gateway/src/cli_download/`, `modules/gateway/src/cli_capabilities/` | Installed CLI routing/distribution | SA13 includes the new helper/manifest entry; SA15 verifies served/installable artifact identity. |
| `modules/gateway/src/app.py:139`; `src/internal/route_authority_manifest.py` | API/router registration and sensitive-route authority | Mount installed L5 router and verified internal integration; do not bypass middleware. |
| `modules/gateway/src/internal/tools_discovery.py`; `src/agentauth/task_tool_routes.py`; `modules/tools/facade/` | Existing tool discovery and authenticated transport | SA14 registers every matrix operation and corresponding transport route/strict schema; no parallel MCP service. |
| `modules/agent-factory/webhook-ingress/infra/task-recovery.tf:11`; `task-api.tf`; `eventbridge.tf` | Task recovery and verified event delivery | Reuse bounded EventBridge/SQS patterns; add only L5 event adapter target. Do not reuse Task DynamoDB rows or replace Task recovery Lambda. |
| `modules/agent-factory/infra/chat-agent-infra.tf:55`, `:197`, `:269`; `modules/gateway/infra/task-api.tf:25` | Chat artifact DDB/S3 and chat queue; gateway Task access grants | Existing artifact conventions/access checks are reusable. Chat/Task state and queue remain peer-owned, not L5 outbox. |
| `modules/gateway/infra/main.tf`; proposed `modules/L5-services/infra/` | Gateway Terraform root / proposed child module | Child owns L5 worker/relay, queues/DLQ, repair rule, IAM, VPC/DB/artifact access, alarms, flags. Preserve manual authority and state ownership. |
| `deploy.sh`; `platform/scripts/deploy-all.sh`; `platform/scripts/publish-shared-image.sh` | Customer launcher / core deployment / shared build | Add ordered L5 package, schema, infra and worker stages with admissions off; source and published-release modes must select compatible artifacts. |
| `.github/workflows/adp-release.yml`, `adp-release-promote.yml`, `adp-release-upgrade.yml`, `adp-release-tests.yml`; `platform/scripts/release/build.py`, `platform/scripts/release/artifacts.py`, `platform/scripts/release/upgrade.py` | Published release production, promotion, upgrade/testing and bundle construction | Include L5 wheel/worker/digests and migration files; L5-only change cannot yield a release missing workers. Extend these actual bundle/upgrade selectors. |
| `remove.sh`; `platform/scripts/undeploy.sh`; `.github/workflows/undeploy.yml`; `status.sh` | Teardown/status | Report L5 alarms/owed work; quiesce/drain before removal. Retain data per policy and customer-linked spaces; teardown is not permission for provider cleanup. |
| `.github/workflows/security-agent-nightly.yml`; `.github/security/`; `.github/scripts/code_review_request.py`, `normalize_security_findings.py`, `dedup_security_findings.py`; `platform/infra/securityagent-nightly-iam.tf` | Existing scheduled Security implementation and scoped role/profile | Preserve #4438. Extract/adapt reusable mechanics without altering nightly profiles, grants, cadence or triage ownership. |
| `modules/agent-context/`, `modules/insights/` publishers | Existing knowledge/analytics services | Peer-owned; not proof of #8149 contribution admission or #7295 findings acceptance. Coordinate specific consumer contracts only. |
| Other domain/app/agent/tool workflows, Terraform and scripts | Fully enumerated independent components | Explicitly excluded from L5 runtime implementation except named integration above. No blanket deployment or shared-role permission expansion. |

## Open PR and provider artifacts

Fetched both PR heads and inspected **all changed-path inventories**, runner
source and relevant reports. Neither PR is merged. These additional artifacts are
outside the main-tree inventory and must not be counted as baseline implementation.

| PR/revision | All changed tracked artifacts | Component / disposition |
|---|---|---|
| #7984 / `499eade9a8e1376a3f5bcfdcd61c16aa0bbf835d` | `docs/spikes/aws-agent-setup/inputs/adp-security-architecture.md`; `docs/spikes/aws-agent-setup/security-adp.md`; `scripts/aws-agent-onboarding/examples/security.yaml`; `scripts/aws-agent-onboarding/security.py` | Threat-model fixture/report/runner. Adapt protocol; do not copy private targets, expiry, local journal or cleanup authority. |
| #8022 / `24338ad0eb533613df3354bf65e342bd5c12d2b9` | `docs/frontier-agents/README.md`; `docs/frontier-agents/spikes/security-code-review-fixture.md`; `docs/frontier-agents/spikes/security-code-review.md`; `scripts/frontier-agent-spikes/README.md`; `scripts/frontier-agent-spikes/examples/security-code-review-config.json`; `scripts/frontier-agent-spikes/examples/security-code-review-profile.yaml`; `scripts/frontier-agent-spikes/requirements.txt`; `scripts/frontier-agent-spikes/security-code-review.py`; `scripts/frontier-agent-spikes/tests/test_security_code_review.py` | Product mapping/source-access failure and static ZIP runner. Preserve #7919/#8013 ownership; remove runtime git-history imports when adapting. |
| #8722 / `c408a73e94de623bdc74da0654b53fc153d55856` | Open Product workspace denied-actor fix surfaced by PR search | Adjacent frontend compatibility; rebase SA12 against accepted host behavior. Not incorporated or claimed reviewed here. |

The Product architecture PR #8155 is merged (merge revision
`3e13c0834f2e08a881316643461ca24af1ef180a`); current Product source was inspected
rather than treating older issue wording about its review prerequisite as current.

## Remaining coverage gaps

- All tracked paths are classified, but static entry detection does not expand
  downloaded binaries, external GitHub Actions, dynamic shell-generated commands
  or runtime plugin registrations. Source paths/filters are inventory evidence,
  not an end-to-end execution graph. SA15's package/launcher tests verify those
  dynamic boundaries; the requested tracked inventory is already delivered here.
- No AWS/deployed-state query, live database inspection or catalog credentials were
  used. Current role grants, registered Module/Feature IDs, actual consumer availability,
  source-access qualification and current pricing remain owned prerequisites.
- Automated migration parsing preserves raw-SQL operation locations rather than
  pretending to compute a complete live schema. All migration files/edges are
  enumerated; L5's new tables cannot be mistaken for existing Product tables.
- Search of open PRs identified relevant provider and Product work, not a guarantee
  that no concurrently authored change affects these contracts. The source baseline
  is pinned; later implementation must recheck heads and schema compatibility.
- Public reports are sanitized secondary evidence, not private operational receipts.
  Do not use their account/resource information as runnable configuration.
