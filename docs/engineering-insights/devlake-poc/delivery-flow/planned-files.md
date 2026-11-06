# Planned implementation files

The 2026-11-01 [existing-connection refinement](../existing-connection-reuse.md#delivery-and-verification)
adds the current repair's integration file plan. #9437 retains one implementation
owner; the coordinator includes its reviewed source/component dependencies in
the live launch amendment. The historical authored proposal below is not replayed
over the running plan, and no per-story deployment is added.

Future implementation ownership; these are not files implemented by this planning PR. `create` is new, `modify` already exists, and `modify_after` is ordered behind its creator. The proposal JSON is authoritative. Operations repair authority is the smaller explicit set in [repair scope](repair-scope.json); file plans alone grant none.

## S01 — #8888

| Group | Change | File |
|---|---|---|
| Evaluation contract and mappings | create | `docs/engineering-insights/devlake-poc/pilot-contract.json` |
| Evaluation contract and mappings | create | `docs/engineering-insights/devlake-poc/source-field-map.json` |
| Evaluation contract and mappings | create | `docs/engineering-insights/devlake-poc/readiness.md` |
| Evaluation contract and mappings | modify | `docs/engineering-insights/devlake-poc/architecture.md` |
| Evaluation contract and mappings | modify | `docs/engineering-insights/devlake-poc/verification.md` |

## S02 — #8889

| Group | Change | File |
|---|---|---|
| Separate connector infrastructure and deployment | create | `modules/connectors/devlake/infra/main.tf` |
| Separate connector infrastructure and deployment | create | `modules/connectors/devlake/infra/variables.tf` |
| Separate connector infrastructure and deployment | create | `modules/connectors/devlake/infra/outputs.tf` |
| Separate connector infrastructure and deployment | create | `modules/connectors/devlake/infra/versions.tf` |
| Separate connector infrastructure and deployment | create | `modules/connectors/devlake/deploy.sh` |
| Separate connector infrastructure and deployment | create | `modules/connectors/devlake/k8s/connector-api.yaml` |
| Separate connector infrastructure and deployment | create | `.github/workflows/devlake-connectors-deploy.yml` |
| Separate connector infrastructure and deployment | create | `.github/workflows/insights-devlake-deploy.yml` |
| Separate connector infrastructure and deployment | modify | `platform/scripts/deploy-all.sh` |
| Separate connector infrastructure and deployment | modify | `modules/gateway/k8s/deployment.yaml` |
| Collection cell infrastructure and renewable IAM | create | `modules/insights/poc/devlake/infra/main.tf` |
| Collection cell infrastructure and renewable IAM | create | `modules/insights/poc/devlake/infra/variables.tf` |
| Collection cell infrastructure and renewable IAM | create | `modules/insights/poc/devlake/infra/outputs.tf` |
| Collection cell infrastructure and renewable IAM | create | `modules/insights/poc/devlake/infra/versions.tf` |
| Collection cell infrastructure and renewable IAM | create | `modules/insights/poc/devlake/deploy.sh` |
| Collection cell infrastructure and renewable IAM | create | `modules/insights/poc/devlake/k8s/collector-worker.yaml` |
| Collection cell infrastructure and renewable IAM | create | `modules/insights/poc/devlake/internal/database/iam.go` |
| Collection cell infrastructure and renewable IAM | create | `modules/insights/poc/devlake/cmd/migrate/main.go` |
| Collection cell infrastructure and renewable IAM | create | `modules/insights/poc/devlake/finalize/runtime.py` |
| Collection cell infrastructure and renewable IAM | create | `modules/insights/poc/devlake/finalize/database.py` |
| Collection cell infrastructure and renewable IAM | modify after S09 | `modules/insights/poc/devlake/schema-manifest.json` |
| Collection cell infrastructure and renewable IAM | modify after S09 | `modules/insights/poc/devlake/finalize/__main__.py` |
| Collection cell infrastructure and renewable IAM | modify after S09 | `.github/workflows/insights-poc-finalize.yml` |
| Default-off admission and runtime checks | create | `modules/gateway/src/connector_scope/feature_flags.py` |
| Default-off admission and runtime checks | create | `modules/insights/tests/devlake_poc/test_s02.py` |
| Default-off admission and runtime checks | modify | `modules/gateway/src/features/routes.py` |
| Default-off admission and runtime checks | modify after S12 | `modules/gateway/src/connector_scope/service.py` |

## S03 — #8890

Use the [repaired collection handoff](../collection-contract-handoff.md). Preserve
the existing S03 branch helpers; integrate current authority, IAM and SQL APIs.

| Group | Change | File |
|---|---|---|
| Cell collection and immutable sealing | modify | `modules/insights/poc/devlake/cmd/collector-worker/main.go` |
| Cell collection and immutable sealing | create | `modules/insights/poc/devlake/cmd/collector-worker/internal/collection/jobs.go` |
| Cell collection and immutable sealing | modify preserved S03 work | `modules/insights/poc/devlake/cmd/collector-worker/internal/collection/generation.go` |
| Cell collection and immutable sealing | create | `modules/insights/poc/devlake/cmd/collector-worker/internal/collection/seal.go` |
| Cell collection and immutable sealing | modify preserved S03 work | `modules/insights/poc/devlake/cmd/collector-worker/internal/collection/normalize.go`, cell schema/migrations; retain needed pinned upstream tasks/DAL as specified in `normalization-reuse.md` |
| Cell collection and immutable sealing | consume; extend only if required | `modules/insights/poc/devlake/migrations/schema-v2.sql` |
| Cell collection and immutable sealing | modify after S09 | `modules/insights/poc/devlake/schema-manifest.json` |
| Cell collection and immutable sealing | modify after S09 | `modules/insights/poc/devlake/Dockerfile` |
| Trusted job admission and lease lifecycle | create | `modules/gateway/src/internal/connector_collection_routes.py` |
| Current work authority and source context | consume; extend only if required | `modules/gateway/src/internal/connector_work_authority.py` |
| Trusted job lifecycle and worker HTTP integration | create | `modules/gateway/tests/products/test_connector_collection_routes.py` |
| Native normalization and replay scenarios | create | `modules/insights/tests/devlake_poc/test_s03.py` |

## S04 — #8891

| Group | Change | File |
|---|---|---|
| Read-only sealed export and durable admission | create | `modules/insights/poc/devlake/exporter.py` |
| Read-only sealed export and durable admission | create | `modules/insights/poc/devlake/observation_mapper.py` |
| Read-only sealed export and durable admission | create | `modules/insights/poc/devlake/export_checkpoint.py` |
| Read-only sealed export and durable admission | create | `modules/insights/schemas/observations/v1/observation.schema.json` |
| Read-only sealed export and durable admission | create | `modules/insights/schemas/observations/v1/policy-envelope.schema.json` |
| Read-only sealed export and durable admission | create | `modules/insights/tests/devlake_poc/test_s04.py` |

## S05 — #8892

| Group | Change | File |
|---|---|---|
| Versioned requirement and test evidence | create | `modules/insights/poc/devlake/requirement_mapper.py` |
| Versioned requirement and test evidence | create | `modules/insights/poc/devlake/test_evidence_mapper.py` |
| Versioned requirement and test evidence | create | `modules/insights/poc/devlake/evidence_chain.py` |
| Versioned requirement and test evidence | create | `modules/insights/tests/devlake_poc/test_s05.py` |

## S06 — #8893

| Group | Change | File |
|---|---|---|
| Immutable authorized work and cost publication | create | `modules/insights/poc/devlake/dataset_builder.py` |
| Immutable authorized work and cost publication | create | `modules/insights/poc/devlake/publisher.py` |
| Immutable authorized work and cost publication | create | `modules/insights/poc/devlake/publication_reconciler.py` |
| Immutable authorized work and cost publication | create | `modules/insights/poc/devlake/quality_report.py` |
| Immutable authorized work and cost publication | create | `modules/insights/tests/devlake_poc/test_s06.py` |
| Immutable authorized work and cost publication | modify | `modules/insights/projection.py` |
| Immutable authorized work and cost publication | modify | `modules/gateway/cli/adp-insights.py` |

## S07 — #8894

| Group | Change | File |
|---|---|---|
| Shared campaign runner and independent evidence reader | create | `modules/insights/poc/devlake/campaign.py` |
| Shared campaign runner and independent evidence reader | create | `modules/insights/poc/devlake/verify_campaign.py` |
| Shared campaign runner and independent evidence reader | create | `modules/insights/poc/devlake/campaign.schema.json` |
| Shared campaign runner and independent evidence reader | create | `modules/insights/tests/devlake_poc/conftest.py` |
| Shared campaign runner and independent evidence reader | create | `modules/insights/tests/devlake_poc/test_s07.py` |
| Shared campaign runner and independent evidence reader | create | `.github/workflows/devlake-poc-qualify.yml` |
| Shared campaign runner and independent evidence reader | create | `docs/engineering-insights/devlake-poc/operating-report.md` |
| Shared campaign runner and independent evidence reader | create | `modules/insights/poc/devlake/resume_campaign.py` |
| Shared campaign runner and independent evidence reader | create | `modules/insights/poc/devlake/complete_issues.py` |
| Shared campaign runner and independent evidence reader | create | `modules/insights/tests/devlake_poc/test_unattended.py` |
| Shared campaign runner and independent evidence reader | modify | `.github/workflows/insights-ci.yml` |
| Shared campaign runner and independent evidence reader | modify | `modules/insights/deploy.sh` |
| Integrated schema and source qualification | modify after S09 | `modules/insights/poc/devlake/schema-manifest.json` |
| Integrated schema and source qualification | modify after S09 | `modules/insights/poc/devlake/scripts/build.sh` |
| Integrated schema and source qualification | modify after S09 | `modules/connectors/devlake/scripts/build.sh` |
| Integrated schema and source qualification | modify after S09 | `.github/workflows/devlake-poc-ci.yml` |

## S08 — #8895

| Group | Change | File |
|---|---|---|
| Three decisions and sanitized completion index | create | `docs/engineering-insights/devlake-poc/decision.md` |
| Three decisions and sanitized completion index | create | `docs/engineering-insights/devlake-poc/completion-index.json` |

## S09 — #8901

| Group | Change | File |
|---|---|---|
| Pinned extraction and independent packaging | create | `modules/connectors/devlake/source-lock.json` |
| Pinned extraction and independent packaging | create | `modules/connectors/devlake/go.mod` |
| Pinned extraction and independent packaging | create | `modules/connectors/devlake/go.sum` |
| Pinned extraction and independent packaging | create | `modules/connectors/devlake/Dockerfile` |
| Pinned extraction and independent packaging | create | `modules/connectors/devlake/cmd/connector-api/main.go` |
| Pinned extraction and independent packaging | create | `modules/connectors/devlake/scripts/build.sh` |
| Pinned extraction and independent packaging | create | `modules/connectors/devlake/scripts/extract.sh` |
| Pinned extraction and independent packaging | create | `modules/connectors/devlake/patches/series` |
| Pinned extraction and independent packaging | create | `modules/connectors/devlake/patches/0001-injected-clients-and-schema-checks.patch` |
| Pinned extraction and independent packaging | create | `modules/connectors/devlake/NOTICE` |
| Pinned extraction and independent packaging | create | `modules/connectors/devlake/LICENSE` |
| Pinned extraction and independent packaging | create | `modules/connectors/devlake/reuse-inventory.json` |
| Pinned extraction and independent packaging | create | `modules/insights/poc/devlake/go.mod` |
| Pinned extraction and independent packaging | create | `modules/insights/poc/devlake/go.sum` |
| Pinned extraction and independent packaging | create | `modules/insights/poc/devlake/Dockerfile` |
| Pinned extraction and independent packaging | create | `modules/insights/poc/devlake/schema-manifest.json` |
| Pinned extraction and independent packaging | create | `modules/insights/poc/devlake/scripts/build.sh` |
| Pinned extraction and independent packaging | create | `.github/workflows/devlake-poc-ci.yml` |
| Pinned extraction and independent packaging | create | `modules/connectors/devlake/scripts/verify-poc.sh` |
| Pinned extraction and independent packaging | create | `modules/connectors/devlake/scripts/check-repair-coverage.py` |
| Bootstrap finalizer and publication ownership | create | `.github/workflows/insights-poc-finalize.yml` |
| Bootstrap finalizer and publication ownership | create | `modules/insights/poc/devlake/finalize/__init__.py` |
| Bootstrap finalizer and publication ownership | create | `modules/insights/poc/devlake/finalize/__main__.py` |
| Bootstrap finalizer and publication ownership | create | `modules/insights/poc/devlake/finalize/artifacts.py` |
| Bootstrap finalizer and publication ownership | create | `modules/insights/poc/devlake/finalize/ledger.py` |
| Source-build and finalizer checks | create | `modules/insights/tests/devlake_poc/__init__.py` |
| Source-build and finalizer checks | create | `modules/insights/tests/devlake_poc/test_s09.py` |

## S10 — #8902

| Group | Change | File |
|---|---|---|
| Extracted connector API and broker transport | create | `modules/connectors/devlake/internal/api/server.go` |
| Extracted connector API and broker transport | create | `modules/connectors/devlake/internal/broker/transport.go` |
| Extracted connector API and broker transport | create | `modules/connectors/devlake/internal/connectors/github.go` |
| Extracted connector API and broker transport | create | `modules/connectors/devlake/internal/connectors/jira.go` |
| Extracted connector API and broker transport | create | `modules/connectors/devlake/internal/operations/context.go` |
| Extracted connector API and broker transport | modify after S09 | `modules/connectors/devlake/cmd/connector-api/main.go` |
| Extracted connector API and broker transport | modify after S09 | `modules/connectors/devlake/source-lock.json` |
| Extracted connector API and broker transport | modify after S09 | `modules/connectors/devlake/go.mod` |
| Extracted connector API and broker transport | modify after S09 | `modules/connectors/devlake/go.sum` |
| ADP connection discovery and credentials | create | `modules/gateway/src/product_onboarding/devlake_discovery.py` |
| ADP connection discovery and credentials | modify | `modules/gateway/src/product_onboarding/source_discovery.py` |
| ADP connection discovery and credentials | modify | `modules/gateway/src/internal/credential_routes.py` |
| ADP connection discovery and credentials | modify | `modules/gateway/src/internal/credential_authorization.py` |
| ADP connection discovery and credentials | modify | `modules/gateway/src/internal/credential_binding.py` |
| ADP connection discovery and credentials | modify | `modules/gateway/src/shared/identity/oauth_providers.py` |
| Connector scenarios | create | `modules/insights/tests/devlake_poc/test_s10.py` |

## S11 — #8903

| Group | Change | File |
|---|---|---|
| Profile enrichment, revisions and provenance | create | `modules/gateway/src/product_onboarding/devlake_enrichment.py` |
| Profile enrichment, revisions and provenance | modify | `modules/gateway/src/product_onboarding/routes.py` |
| Profile enrichment, revisions and provenance | modify | `modules/gateway/src/product_onboarding/discovery.py` |
| Profile enrichment, revisions and provenance | modify | `modules/gateway/src/products/source_routes.py` |
| Profile enrichment, revisions and provenance | modify | `modules/gateway/src/products/models.py` |
| Served Profile UI and installed CLI | modify | `modules/gateway/frontend/src/pages/products/ProductSources.tsx` |
| Served Profile UI and installed CLI | modify | `modules/gateway/frontend/src/pages/products/ProductOnboarding.tsx` |
| Served Profile UI and installed CLI | modify | `modules/gateway/frontend/src/services/products.ts` |
| Served Profile UI and installed CLI | modify | `modules/gateway/frontend/src/__tests__/pages/products/ProductSources.test.tsx` |
| Served Profile UI and installed CLI | modify | `modules/gateway/frontend/src/__tests__/pages/products/ProductOnboarding.test.tsx` |
| Served Profile UI and installed CLI | modify | `modules/gateway/src/cli_capabilities/contract.py` |
| Served Profile UI and installed CLI | modify | `modules/gateway/cli/adp-product.py` |
| Served Profile UI and installed CLI | modify | `modules/gateway/cli/adp-onboard.py` |
| Profile scenarios and live response fields | create | `modules/insights/tests/devlake_poc/test_s11.py` |
| Profile scenarios and live response fields | create | `modules/insights/tests/devlake_poc/profile-contract-fields.json` |

## S12 — #8919

| Group | Change | File |
|---|---|---|
| Gateway scope and cell authority | create | `modules/gateway/src/connector_scope/__init__.py` |
| Gateway scope and cell authority | create | `modules/gateway/src/connector_scope/models.py` |
| Gateway scope and cell authority | create | `modules/gateway/src/connector_scope/routes.py` |
| Gateway scope and cell authority | create | `modules/gateway/src/connector_scope/service.py` |
| Gateway scope and cell authority | create | `modules/gateway/src/connector_scope/policy.py` |
| Gateway scope and cell authority | create | `modules/gateway/alembic/versions/devlake_cell_authority.py` |
| Gateway scope and cell authority | modify | `modules/gateway/src/app.py` |
| Controlled authority verification | create | `modules/insights/tests/devlake_poc/test_s12.py` |
| Controlled authority verification | create | `modules/gateway/tests/connector_scope/test_authority.py` |

## S13 — #8920

| Group | Change | File |
|---|---|---|
| Fresh serving authorization and policy descriptors | create | `modules/insights/dataset_policy.py` |
| Fresh serving authorization and policy descriptors | create | `modules/insights/schemas/results/v2/dataset-policy.schema.json` |
| Fresh serving authorization and policy descriptors | create | `modules/gateway/src/insights_results/policy_authority.py` |
| Fresh serving authorization and policy descriptors | create | `modules/insights/tests/devlake_poc/test_s13.py` |
| Fresh serving authorization and policy descriptors | modify | `modules/insights/results.py` |
| Fresh serving authorization and policy descriptors | modify | `modules/gateway/src/insights_results/routes.py` |

## S14 — provider expansion

The seven-provider foundation is merged in #9554. The
[parallel recovery](parallel-recovery.md) first extracts the shared served API
from #9439, then #9449 proceeds alongside #9437 campaign repair. Do not recreate
the merged adapters, Go fixtures or staging. The coordinator serializes shared
consumer/workflow edits before the combined release; the complete story remains
open until all its acceptance criteria pass. File plans grant no deployment authority.

| Group | Change | File |
|---|---|---|
| Seven injected providers | merged foundation; consume | `modules/connectors/devlake/patches/0003-provider-expansion.patch`, `0004-workflow-providers.patch` |
| Independent adapters and tests | merged foundation; consume | `modules/connectors/devlake/providers/`, `modules/connectors/devlake/scripts/test-providers.sh` |
| Source/provenance | modify | `modules/connectors/devlake/source-lock.json`, `patches/series`, `README.md` |
| API dispatch and image staging | extend existing served path | `modules/connectors/devlake/cmd/connector-api/main.go`, `jenkins_profile.go`, `expanded.go`, `scripts/build.py`, `scripts/test-discovery.sh` |
| Current ADP authority | modify after shared API merge | `modules/gateway/src/internal/connector_source_broker.py`, `modules/gateway/src/product_onboarding/discovery.py` |
| Profile/capability types | modify | `modules/gateway/src/product_onboarding/source_discovery.py`, `modules/gateway/frontend/src/pages/products/ProductOnboarding.tsx`; related versioned schema and installed CLI definitions |
| Actual served process checks | extend existing process fixture and provider cases | `modules/gateway/tests/products/test_jenkins_profile.py`, `modules/connectors/devlake/cmd/connector-api/jenkins_profile_test.go`, related provider tests |
| Live test tools | extend existing operations | Minimal declarative tool manifests and seed/readback steps under `modules/insights/poc/devlake/`; reuse campaign ownership ledger/finalizer, no new lab framework |
| Isolated provider CI | merged foundation; consume | `.github/workflows/devlake-provider-tests.yml`; test-only workflow, no publication or deployment |
| Upstream normalized collection and cell handoff | wire existing upstream task entry points into worker | `modules/connectors/devlake/normalization/pipelines.go`, `pipelines_test.go`, `modules/insights/poc/devlake/cmd/collector-worker/internal/collection/normalize.go`, cell schema/migrations; retain pinned upstream tasks/DAL as specified in `normalization-reuse.md` |
| Pilot and closure consumers | coordinated integration with #9437 | `modules/insights/poc/devlake/provider_profile_source.py`, `criterion_producers.py`, `verify_campaign.py`, `complete_campaign.py` |
| Expansion evidence tests | create | `modules/insights/tests/devlake_poc/test_provider_expansion.py` |
| Revised runtime contracts | modify | `docs/engineering-insights/devlake-poc/pilot-contract.json`, `source-field-map.json`, `delivery-flow/acceptance-mapping.json`, `delivery-flow/launch-contract.json` |
| One campaign and cleanup | coordinated integration with #9437 | `.github/workflows/devlake-poc-qualify.yml`, `_devlake-poc-window.yml`, `insights-poc-finalize.yml`; module-local implementation helpers |

Original S01–S13/repair file groups and stable acceptance IDs are retained.
