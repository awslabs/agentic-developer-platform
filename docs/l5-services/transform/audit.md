# Transform repository and delivery audit

## Coverage boundary

The complete tracked tree at main
`388584bdd80a4a5ff142abe3234e7d1eac1cfd64` contains **12,710 artifacts**.
Every path, including hidden files, has a component and explicit disposition;
there are no unmapped paths or gitlink omissions. This audit enumerates **4,553
entry/candidate artifacts**, **161 workflows**, **2,057 Terraform declarations**,
**162 gateway migration files**, **24 L5 migration files** and **20 L5 model
tables**. It is not a representative sample. The entry count intentionally
includes all structured configuration candidates to avoid silently omitting
indirect build/deploy configuration; it is not a count of executable programs.

| Artifact | Complete content |
|---|---|
| [tracked-artifacts.tsv](tracked-artifacts.tsv) | Every baseline path, Git blob ID, component, disposition and entry roles. |
| [entry-points.tsv](entry-points.tsv) | All detected scripts, declared-command containers, build manifests, workflow/action/deployment/migration/handler and structured-config candidates. |
| [workflows.tsv](workflows.tsv) | Every workflow's events, jobs, path/branch filters and static local dependencies. |
| [resources.tsv](resources.tsv) | Every Terraform resource/data/module declaration, source line and logical name; no deployed identifiers or credentials. |
| [migrations.tsv](migrations.tsv) | Topologically ordered complete gateway migration DAG and operation locations. Current source head: `145_conversation_list_indexes`. |
| [l5-models.tsv](l5-models.tsv), [l5-migrations.tsv](l5-migrations.tsv) | Every L5 SQL model/table/column and migration revision/parents/dependencies. Source metadata, not a live schema dump. |
| [related-pr-artifacts.tsv](related-pr-artifacts.tsv) | Complete pinned branch diffs: #9080 (6 paths), #8932 (90), #9081 (11), historical #7991 (9), #8020 (11). Each path has blob, component and disposition. |
| [inventory-summary.json](inventory-summary.json) | Counts, baseline, related heads, classification totals and explicit limits. |
| [audit_inventory.py](audit_inventory.py) | Deterministic Git-object scan, reusing the existing shared audit without changing its historical baseline. |

For related PRs, inventory means all changes from their merge base to the inspected
head, including earlier commits in that branch. Historical merge bases for #7991
and #8020 were obtained through GitHub's compare API and pinned in the script;
they do not depend on a shallow checkout finding a common ancestor with new main.
The architecture cites historical POC reports separately from current runtime.

Classification is exhaustive at artifact level. It does not imply line-by-line
security review of unrelated services. Dynamic shell dispatch, downloaded binaries,
external GitHub Actions, generated runtime commands, actual IAM grants/cluster
objects, private fixtures and deployed schema remain outside static enumeration.
All such limits are recorded, not deferred as missing inventory work. TF01/TF07
own live qualification; they do not need to finish this repository inventory.

## Deployment and build entry points

This matrix identifies Transform integration points from the exhaustive records;
other entries keep their recorded owner/exclusion. It does not substitute for
the full inventory. Line numbers are at the pinned baseline unless a PR is named.

| Entry point | Observed behavior and required ownership |
|---|---|
| `modules/L5-services/pyproject.toml`; `scripts/build-package.sh`, `scripts/stage-gateway.sh`, `scripts/build-workers.sh` beneath that module | Existing installable wheel/gateway staging; worker build currently installs `devops-worker` extras. TF07 reconciles the Security release branch's changing build-worker interface before adding Transform dependencies. Do not assume the same script arguments across revisions. |
| `modules/gateway/Dockerfile:84`; `modules/gateway/scripts/stage-l5-wheel.sh` if supplied by release work | Main consumes a verified L5 wheel. TF07 ensures schemas and migrations from the same package/revision are staged; the latter helper is proposed in #8932, not main. |
| `codebuild/bs-gateway-build.yml`, `codebuild/bs-gateway-smoke.yml`; `platform/scripts/publish-shared-image.sh`, `platform/scripts/zip-source.sh` | Existing source archive/image build entry points. TF07 maintains exact-case `modules/L5-services` staging and installed-release identity; source checkout presence alone is insufficient. |
| `.github/workflows/l5-services-ci.yml:3` | PR/manual contract and module checks. Its current path filters cover runtime/contracts and named build files, not this new prose-only Transform directory; local design validation must be reported explicitly. |
| `.github/workflows/gateway-ci.yml:20`; `.github/workflows/gateway-deploy.yml:7`, `:203` | Existing outer filters and inner changed-file predicate include L5 source/contract changes. Preserve both and normal main auto-deploy; prose alone does not deploy Transform. |
| `.github/workflows/gateway-frontend-deploy.yml:5` | Separate main-triggered frontend publisher; TF05/TF07 retain it and verify consumed contract/client compatibility. |
| `.github/workflows/gateway-infra-plan.yml`, `gateway-infra-apply.yml`, `run-gateway-migrations.yml`, `postgres-schema-check.yml`; `modules/gateway/alembic/env.py`, `alembic.ini` | Existing plan/manual apply/schema entry points. L5 metadata/version-location registration and release merge migrations are in #8932, not verified on main. TF07 owns additive ordering and old/in-flight-row tests. |
| [#8932 release workflow](https://github.com/aws-e/adp/blob/1477aed6761d4f7e09be64344fe3c27ddeb605fd/.github/workflows/l5-services-release.yml#L3), worker lifecycle workflow, L5 `infra/security/` and publish/lifecycle scripts | Proposed shared explicit `workflow_dispatch` release requires reviewed main SHA and protected target. Reuse/extend with SA15 owner; do not copy a parallel Transform release workflow or claim current worker availability. All changed artifacts are enumerated. |
| `platform/scripts/deploy-all.sh`; deployment manifest and quickstart | Existing customer launcher; #8932 proposes L5 integration. TF07 verifies both cold-start and existing-install paths with the shared release owner; no superseded Phase 0/GitHub setup path. |
| `modules/tools/validation/Dockerfile`, `infra/main.tf`, `isolation-infra/main.tf`, `isolation-infra/checks.Dockerfile`, `qualify-isolation.py`, `qualify-executor.py` | Concrete protected executor build/provision/qualification entry points. Service defaults off and uses Task authority/120-second checks. L5 non-blocking authority bridge is proposed. TF07 must wire/verify its release publisher; no observed workflow establishes that this service automatically deploys. |
| `modules/agent-factory/agent-worker-image/lib/codex_kubernetes_validation.py`; `lib/codex_validation_service_runner.py`; `modules/agent-factory/webhook-ingress/infra/codex-validation.tf` | Existing source projection, no-credential Pod, termination receipts and optional namespace ownership. Preserve Task compatibility. Standalone isolation and webhook infra must never both own the same resources. TF01/03 coordinate executor primitives; TF07 installs them. |
| `.github/workflows/codex-harness-tests.yml`, `agent-factory-infra-plan.yml`; `modules/tools/orchestrate.sh`, `.github/workflows/tools-platform-deploy.yml` | Existing peer test/provision/release owners, not implicit L5/validation publishers. Cross-owner changes require explicit TF07 integration; no whole agent/persona rollout merely to onboard a Product. |
| Gateway `src/l5_integration.py`, `src/internal/l5_product_routes.py`, `l5_snapshot_routes.py`, `l5_credential_routes.py` | Existing mounted auth/source/credential host boundaries. TF02/03 extend through current delegated L5 scope; HTTP route existence is not provider readiness. |
| Gateway `frontend/src/pages/products/`, `frontend/src/services/products.ts`, `cli/`; `docs/l5-services/contracts/v1/` and existing tools transport | TF05 adds Transform selection/parity, not a new provider API. Exact installed CLI/MCP integration needs contract tests; source-backed shared operation matrix is the design starting point. |

## Explicit exclusions and remaining coverage gaps

All unrelated deployment/build paths still appear in the TSVs. Superplane's
infrastructure-operation executor, Dev Box, cyber, nightly Security POCs, mainframe/
VMware/Windows products and sibling FinOps/Well-Architected designs are excluded
from modification. Context/Insights own their runtimes; only authorized shared
contribution integration is in scope. No sibling architecture is rewritten.

The selected executor requires a new L5 authority bridge; the existing Task
endpoint cannot be called with fabricated identity. Provider `atx json` protocol
support, credential-safe file-tool mediation, lost-ID recovery, remote cancel,
installed release wiring and live fixture availability remain **unverified**.
These are implementation/live qualification gaps, not omitted repository paths.
The architecture's prerequisite table names the owner and affected phase.

GitHub denied Roadmap project 4 resolution and branch protection/rules reads.
Those calls do not establish absence of a project or review policy. No AWS target
or credentials were requested. Public artifacts contain only source paths,
logical declarations and fictional example data; operational evidence stays private.

## Reproduction and checks

From repository root, use Python 3.12+ and an isolated environment containing
`PyYAML`, `jsonschema` and `openapi-spec-validator`. Git objects for the baseline,
related heads and two historical bases in `audit_inventory.py` must be fetched
first; missing objects should fail, not silently narrow coverage.

```sh
python3 docs/l5-services/transform/audit_inventory.py --check
python3 docs/l5-services/contracts/v1/validate.py
python3 scripts/check-public-docs.py
```

The inventory check recomputes and compares all nine records; schema validation
checks the existing shared contract, **not** proposed Transform schema support.
The documentation checker screens publication patterns, not live access/security.
See the PR for results at the tested design revision. Runtime tests, provider
jobs, deployment and EC live acceptance are intentionally not run here.
