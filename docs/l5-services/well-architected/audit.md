# Well-Architected source and delivery audit

## Coverage and reproduction

Main baseline `388584bdd80a4a5ff142abe3234e7d1eac1cfd64`, fetched 2026-10-27.
**All 12,710 tracked artifacts are enumerated**, including hidden files: each has
a Git blob, component, disposition and entry role. None is unmapped. All 2,806
statically detected entry artifacts, all 161 workflows, 2,057 Terraform
resource/data/module declarations and 271 migration revisions across all detected
migration roots are recorded. This is exhaustive tracked-tree enumeration plus
source review of relevant integration points, not proof that every executable or
live environment was tested. Exclusions are explicit, not omitted paths.

| Artifact | Coverage |
|---|---|
| [tracked-artifacts.tsv](tracked-artifacts.tsv) | Every pinned-tree path; source identity, component and inclusion/exclusion rationale. |
| [entry-points.tsv](entry-points.tsv) | Executable/script, package/build, deployment/IaC, workflow/local action, migration and recognizable runtime handler artifacts. |
| [entry-declarations.tsv](entry-declarations.tsv) | Recognizable Python handlers and package command declarations; commands themselves remain at the source path, avoiding copied deployment values. |
| [workflows.tsv](workflows.tsv) | Every workflow's actual events, jobs, branch/path filters and statically referenced local dependencies. |
| [resources.tsv](resources.tsv) | Every Terraform resource/data/module declaration, source line and logical name, not copied resource values. |
| [migrations.tsv](migrations.tsv) | Topologically ordered revision/parent/dependency graph for all detected gateway, L5, Context and Superplane roots. These are independent migration domains, **not one deployable database DAG**. |
| [reviewed-overlays.tsv](reviewed-overlays.tsv) | Every changed path in the explicitly inspected open PR revisions #9080, #8932, #9081 and historical helper #8018, mapped to component/exclusion; not presumed main. |
| [inventory-summary.json](inventory-summary.json) | Counts, components, migration heads and structural/semantic limits. |
| [audit_inventory.py](audit_inventory.py) | Deterministic Git-object scan using shared audit classifiers, without changing the older shared/Security audit. |

Run from the repository root with Python 3.12+ and PyYAML installed:

```sh
python3 docs/l5-services/well-architected/audit_inventory.py --check
```

The generator's pinned Git objects must be available; missing history is a check
failure, not permission to substitute current main. The design files added by this
PR are outside the baseline tree and are checked by `validate_design.py`; their
source identity is the design PR commit. Generated TSVs contain paths/logical
names only, not private operational values. Root publishing checks cover them.

Migration heads at the baseline: gateway `145_conversation_list_indexes`, L5
`l5_results_knowledge_merge`, Context `015_product_knowledge`, Superplane
`055_sso_provider_ownership`. The L5/host registration bridge is still open release
work; don't confuse model availability with a migrated gateway deployment.
Product source snapshots already have bounded expiry checks in
[`source_snapshots.py`][snapshots]; no generic WA remote-work retention hold was
found in the tracked L5 models. The proposal adds one through shared ownership,
not a new source database. Existing L5 configuration, binding, attempt, event,
result, setup and work table definitions are cited in the architecture.

## Deployment and build entry points

Every detected entry is in the inventory. This table identifies the required
integration points and exclusions; it is not offered instead of that inventory.
All evidence paths are relative to the pinned main unless a PR revision is named.
Line numbers are available in immutable source links and generated declaration
records. No command below was used to deploy.

| Entry | Current source fact | WA delivery disposition / owner |
|---|---|---|
| [`deploy.sh`][launcher], `platform/scripts/deploy-all.sh`, `deploy-phases.sh`, `undeploy.sh`, `undeploy-phases.sh` | Platform launcher and staged deployment/teardown, not provider-profile setup | Preserve canonical deployment ordering; WA07 coordinates any L5 release inventory integration with #8698. No new service-specific launcher. |
| [`gateway-deploy.yml`][gateway-deploy] (lines 40–52, 203), `gateway-ci.yml` | Main filters and internal change predicate already include L5 runtime/package/staging/schema inputs | Preserve gateway auto-deploy; coordinated WA runtime changes use existing wheel staging. Prose-only WA design does not publish runtime. |
| `.github/workflows/gateway-frontend-deploy.yml`, frontend package test/build scripts | Separate frontend delivery/check ownership | WA06/WA07 include consumed UI contracts and normal frontend triggers; no worker publication in this workflow. |
| [`.github/workflows/l5-services-ci.yml`][l5-ci] | PR/offline contracts and module tests, not worker publisher | Add WA fixtures/selectors to shared check ownership. This design adds no runtime schema mutation. |
| `modules/L5-services/scripts/build-package.sh`, `stage-gateway.sh`, [`build-workers.sh`][build-workers], `pyproject.toml` | Existing wheel packaging and worker archive build; current worker builder installs `devops-worker` dependencies | WA01/WA07 pin qualified WA SDK/model and shared release dependencies. Packaging a wheel does not wire its worker factory. |
| `modules/gateway/Dockerfile:84`, `platform/scripts/publish-shared-image.sh:76`, `codebuild/bs-gateway-smoke.yml:23` | Verified L5 wheel staging/install path | Reuse source/package identity; both cold-start and CI build paths must select the compatible wheel. |
| `codebuild/bs-gateway-build.yml`, `platform/scripts/zip-source.sh`, release manifests and workflows | Source bundles/build orchestrators are distinct from image/worker publication | WA07 checks packaged exact-case module/docs contract inputs and release hashes; inventory retains all CodeBuild and platform entries. |
| `modules/gateway/alembic/env.py:19`, `alembic.ini`, `.github/workflows/run-gateway-migrations.yml`, `postgres-schema-check.yml` | Gateway metadata/version runner; L5 own migration directory exists separately | Reuse pending #8932 bridge, additive migrations and compatibility checks. Do not infer deployed L5 tables from model files. |
| `modules/gateway/scripts/migrate-before-rollout.py`, `sync-gateway-engine.py`, `sync-gateway-writers.py` | Migration/writer rollout ordering | WA07 verifies mixed-version workers and gateway plus rollback retention; preserve other writers' contracts. |
| `.github/workflows/gateway-infra-plan.yml`, `gateway-infra-apply.yml`; gateway infra root | Plan and protected/manual apply; not automatic provider-profile provisioning | #8698/WA07 integrate private shared L5 queue/worker IAM and provider-role delegation under existing authority. |
| `modules/L5-services/infra/devops-events.tf` | Existing DevOps event infra | Explicit exclusion: not a WA source-discovery schedule; opt-in WA ticks reuse common schedule admission. |
| `l5_services.workers.execution.lambda_handler`, `workers.recovery.lambda_handler`, `dispatcher.py`, `work_store.py` | Existing bounded private SQS/runtime-factory and due-work repair code | WA04 adapter registration and late-outcome recovery; WA07 installed worker/transport proof. No Lambda URL. |
| `modules/gateway/src/app.py:53`, `l5_integration.py`, `internal/l5_product_routes.py`, `internal/l5_snapshot_routes.py`, `internal/l5_credential_routes.py` | Existing gateway routing, Product/source transport and broker boundary | WA02/WA07 extend service-specific authorization/registration while retaining current scope/work checks. |
| `modules/gateway/cli/adp`, `cli/adp-product.py`, frontend Product workspace, MCP/tool facade and build definitions | Existing host/client packaging; shared `adp service` and L5 MCP matrix is a **proposed** service surface, not a command currently installed on baseline | WA06 implements/coordinates parity with shared SA12–SA14; verify installed clients, not just Python calls. |
| Context, Insights, tools, harness, agent-factory, domain apps and other entries | Every path/entry mapped in the TSVs | Peer-owned integration where named in architecture; otherwise explicit exclusion. Do not make them WA publishers, migrate their SQL or modify nightly security/spikes. |
| Open [PR #8932][release] at `1477aed6761d4f7e09be64344fe3c27ddeb605fd` | Adds `l5-services-release.yml`, `l5-services-worker-lifecycle.yml`, security infra, publish/lifecycle scripts, migration bridge and factory/transport wiring | Proposed shared release dependency; manual workflow dispatch in inspected source. WA07 rebases/integrates actual landed contracts, not a copy/fork publisher. |
| Open [PR #9081][devops] at `25e62f04bf750a7bcb36663e94105edfab587218` | Extends `run_input.py`, admission, execution, gateway and incident ingress for DevOps | Shared-edit collision with WA01/WA04; WA07 integrates typed service variants and preserves DevOps tests. |
| Open [PR #9080][expansion] and historical #8018 | Scope/evidence proposal and POC helper outside current main | Reference immutably; do not ship copied one-off scripts as service runtime or overwrite sibling design files. |

## Main follow-up

Main advanced during authoring to `eba7f5b95a0ea2bbc70a655201099e727d4479f7`.
[main-delta.tsv](main-delta.tsv) enumerates **all 55 changed paths** from the
original baseline with blob/component/disposition/entry roles. The independent
validator reconstructs this newer complete tracked-path set from baseline plus
all additions/deletions and compares it to that Git tree; no changed artifact is
deferred to implementation. Existing main inventory identities stay pinned rather
than silently rewriting historical evidence used in child design references.

The new DevLake connector design's 32 documentation artifacts are explicitly
excluded from WA ownership. Relevant peer changes in knowledge search, billing,
frontend and Context ACL/transport were inspected: the [new scoped search][search]
and [Context search ACL path][context-search] preserve current Product visibility
and separate failed authority from empty results. They do not add WA provider
registration or the shared L5 contribution/receipt/publication workflow; WA05's
consumer qualification gap remains. No L5 source, shared contract, migration or
workflow changed in this delta. No deployed behavior is inferred from these merges.

## Unresolved coverage limits

- Static enumeration is complete for the pinned tracked tree. Dynamic shell
  dispatch, downloaded third-party actions, SDK internals and externally fetched
  IaC modules are not expanded. Entry detection may miss unconventional
  declarations; all supporting paths remain explicitly mapped for inspection.
- Terraform declarations and migration graphs are source evidence, not live
  resource inventories or exhaustive SQL effect interpretation. No AWS identity,
  deployed schema, IAM policy or environment was queried in this design run.
- Open-PR deltas are inventoried at exact reviewed revisions, not counted as main.
  Concurrent sibling edits after those revisions need release reconciliation.
- The provider model's untyped `additionalContext` leaves archive format/limits
  unresolved; public SDK shapes are not source-consumption proof. Historical
  Support failures are reported evidence, not current selected-account status.
- Catalog IDs and roadmap membership remain unverified. `gh project view 4
  --owner aws-e --format json` returned `Could not resolve to a ProjectV2 with the
  number 4`; this does not prove the project is absent. PranavSharma1000 owns
  access/membership correction. Native issue relationships are checked separately.
- Review approval, installed clients, eligible provider results, cleanup and
  entitled reuse have not been established. They remain named work in the design
  and live acceptance backlog, not hidden gaps in the tracked artifact inventory.

[launcher]: https://github.com/aws-e/adp/blob/388584bdd80a4a5ff142abe3234e7d1eac1cfd64/deploy.sh
[gateway-deploy]: https://github.com/aws-e/adp/blob/388584bdd80a4a5ff142abe3234e7d1eac1cfd64/.github/workflows/gateway-deploy.yml#L40
[l5-ci]: https://github.com/aws-e/adp/blob/388584bdd80a4a5ff142abe3234e7d1eac1cfd64/.github/workflows/l5-services-ci.yml#L1
[build-workers]: https://github.com/aws-e/adp/blob/388584bdd80a4a5ff142abe3234e7d1eac1cfd64/modules/L5-services/scripts/build-workers.sh#L1
[snapshots]: https://github.com/aws-e/adp/blob/388584bdd80a4a5ff142abe3234e7d1eac1cfd64/modules/gateway/src/products/source_snapshots.py#L141
[release]: https://github.com/aws-e/adp/pull/8932
[devops]: https://github.com/aws-e/adp/pull/9081
[expansion]: https://github.com/aws-e/adp/pull/9080

[search]: https://github.com/aws-e/adp/blob/eba7f5b95a0ea2bbc70a655201099e727d4479f7/modules/gateway/src/knowledge/search.py#L126
[context-search]: https://github.com/aws-e/adp/blob/eba7f5b95a0ea2bbc70a655201099e727d4479f7/modules/agent-context/door/server.py#L941
