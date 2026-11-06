# Story file plan

This is the grouped file map for all 16 Security story nodes in the
[delivery wave plan](delivery-waves.json). That JSON contains the canonical
`file_groups`, creating-story references and existing planned test selectors.
Every path below is repository-relative. **These are implementation deliverables;
listing a new file does not mean its runtime implementation exists.**

File existence was checked against repository revision `99ad7be4224518cee8d3c3df5bee69f205deaeac`.

- **Create:** absent at that revision; one story creates the file.
- **Modify:** already tracked at that revision.
- **Modify after SAxx:** a prior story creates it; extend that shared file.
- New helper and migration filenames are proposed implementation locations.
  Keep published test selectors stable. Resolve actual Alembic identities/heads
  at implementation; a planned filename is not an allocated revision.
- Shared router/model/package edits and migration heads are serialized by their
  integration owner. Recheck current main before editing and update this map for
  necessary related changes; it is not a substitute for scoped execution authority.

## Repository groups

| Group | Home |
|---|---|
| Architecture, contracts and user/operations documentation | `docs/l5-services/` |
| Domain APIs, state, migrations, adapters and workers | `modules/L5-services/src/l5_services/` |
| Domain, deployment and live verification | `modules/L5-services/tests/` |
| L5 infrastructure and packaging | `modules/L5-services/infra/`, `modules/L5-services/scripts/` |
| Product UI and installed CLI | `modules/gateway/frontend/`, `modules/gateway/cli/` |
| Product/broker/API/MCP host integration | `modules/gateway/src/`, `modules/tools/facade/` |
| Build, release and existing event producers | `.github/workflows/`, `platform/scripts/`, `codebuild/`, existing webhook ingress |

All groups retain the [architecture](architecture.md), [backlog](backlog.md) and
[autonomous delivery schedule](delivery-plan.md). Existing nightly/spike runners
are reuse references, not files to move or reassign to this module.

## SA01

[Story #8683](https://github.com/aws-e/adp/issues/8683)

### Architecture and contract specifications

Maintain the shared design and one versioned API/data/MCP contract.

**Modify**

- `docs/l5-services/architecture.md`
- `docs/l5-services/backlog.md`
- `docs/l5-services/verification.md`
- `docs/l5-services/contracts/v1/README.md`
- `docs/l5-services/contracts/v1/peer-contracts.md`
- `docs/l5-services/contracts/v1/catalog.py`
- `docs/l5-services/contracts/v1/schemas.json`
- `docs/l5-services/contracts/v1/openapi.json`
- `docs/l5-services/contracts/v1/mcp-tools.json`
- `docs/l5-services/contracts/v1/operations.tsv`
- `docs/l5-services/contracts/v1/examples.json`
- `docs/l5-services/contracts/v1/scenarios.json`
- `modules/L5-services/README.md`

### Contract verification

Keep offline specification checks separate from the planned handler/compatibility tests.

**Create**

- `modules/L5-services/tests/contracts/test_contracts.py`

**Modify**

- `docs/l5-services/contracts/v1/validate.py`
- `docs/l5-services/contracts/v1/test_contracts.py`

**Existing planned test selectors**

- `modules/L5-services/tests/contracts/test_contracts.py::test_security_contracts`
- `modules/L5-services/tests/contracts/test_contracts.py::test_lifecycle_boundaries`
- `modules/L5-services/tests/contracts/test_contracts.py::test_compatibility_fixtures`

## SA02

[Story #8684](https://github.com/aws-e/adp/issues/8684)

### L5 Product client and package foundation

Introduce the Product API client and minimal installable package; SA15 later adds release packaging.

**Create**

- `modules/L5-services/src/l5_services/__init__.py`
- `modules/L5-services/src/l5_services/product_client.py`
- `modules/L5-services/pyproject.toml`

### Product-owned source snapshots

Implement source pin/read/content/manifest routes and retention with connector-owned credentials.

**Create**

- `modules/gateway/src/products/source_snapshots.py`
- `modules/gateway/src/products/source_snapshot_routes.py`

**Modify**

- `modules/gateway/src/products/routes.py`
- `modules/gateway/src/products/source_routes.py`
- `modules/gateway/src/products/models.py`

### Delegation and broker integration

Verify service identity, scoped delegation and transient credential sessions through existing host authority.

**Create**

- `modules/gateway/src/internal/l5_credential_routes.py`

**Modify**

- `modules/gateway/src/shared/identity/product_scope.py`
- `modules/gateway/src/internal/auth_deps.py`
- `modules/gateway/src/internal/service_authorization.py`
- `modules/gateway/src/internal/assume_role_routes.py`
- `modules/gateway/src/internal/route_authority_manifest.py`
- `modules/gateway/src/app.py`

### Product storage migration

Product snapshot storage remains Product-owned; bind the inspected host Alembic head at implementation.

**Create**

- `modules/gateway/alembic/versions/product_source_snapshots.py`

### Delegation and source tests

Preserve all published delegation selectors and add exact-content/expiry checks.

**Create**

- `modules/gateway/tests/products/test_l5_delegation.py`
- `modules/gateway/tests/products/test_l5_source_snapshots.py`

**Existing planned test selectors**

- `modules/gateway/tests/products/test_l5_delegation.py::test_scoped_profile_read`
- `modules/gateway/tests/products/test_l5_delegation.py::test_foreign_and_revoked_read`
- `modules/gateway/tests/products/test_l5_delegation.py::test_human_and_revision_compatibility`

## SA03

[Story #8685](https://github.com/aws-e/adp/issues/8685)

### Configuration API and host mount

Create the configuration API/router and inject host dependencies through one thin gateway shim.

**Create**

- `modules/L5-services/src/l5_services/api/__init__.py`
- `modules/L5-services/src/l5_services/api/router.py`
- `modules/L5-services/src/l5_services/api/configuration.py`
- `modules/gateway/src/l5_integration.py`

**Modify**

- `modules/gateway/src/app.py`
- `modules/gateway/src/products/configuration.py`
- `modules/gateway/src/product_onboarding/handoffs.py`

### Configuration state and migrations

Own L5 metadata and the first L5 migration branch; no direct Profile-table reads.

**Create**

- `modules/L5-services/src/l5_services/models/__init__.py`
- `modules/L5-services/src/l5_services/models/base.py`
- `modules/L5-services/src/l5_services/models/configuration.py`
- `modules/L5-services/src/l5_services/migrations/__init__.py`
- `modules/L5-services/src/l5_services/migrations/versions/l5_configurations.py`

### Configuration tests and shared fixtures

Cover inherited settings, revision races, handoff recovery and scope.

**Create**

- `modules/L5-services/tests/conftest.py`
- `modules/L5-services/tests/integration/test_configuration.py`

**Existing planned test selectors**

- `modules/L5-services/tests/integration/test_configuration.py::test_profile_population`
- `modules/L5-services/tests/integration/test_configuration.py::test_refresh_and_revision_race`
- `modules/L5-services/tests/integration/test_configuration.py::test_scope_replay_outage`

## SA04

[Story #8686](https://github.com/aws-e/adp/issues/8686)

### Bindings and provider setup

Implement create/link/check/update/suspend/resume/unlink/owned cleanup using immutable SetupIntent.

**Create**

- `modules/L5-services/src/l5_services/api/bindings.py`
- `modules/L5-services/src/l5_services/adapters/security/__init__.py`
- `modules/L5-services/src/l5_services/adapters/security/binding.py`

### Binding state and migrations

Separate persistent bindings and bounded setup operations.

**Create**

- `modules/L5-services/src/l5_services/models/binding.py`
- `modules/L5-services/src/l5_services/models/setup.py`
- `modules/L5-services/src/l5_services/migrations/versions/l5_bindings_and_setup.py`

### Shared registration

Register the new API and models through the existing L5 entry points.

**Modify after the creating story**

- `modules/L5-services/src/l5_services/api/router.py` — created by SA03.
- `modules/L5-services/src/l5_services/models/__init__.py` — created by SA03.

### Binding verification

Test uncertain setup, replay, scope and owned-only cleanup.

**Create**

- `modules/L5-services/tests/integration/test_security_binding.py`

**Existing planned test selectors**

- `modules/L5-services/tests/integration/test_security_binding.py::test_link_and_replay`
- `modules/L5-services/tests/integration/test_security_binding.py::test_uncertain_setup`
- `modules/L5-services/tests/integration/test_security_binding.py::test_scope_and_cleanup`

## SA05

[Story #8687](https://github.com/aws-e/adp/issues/8687)

### Run admission and API

Create atomic admission, immutable run receipts and run controls.

**Create**

- `modules/L5-services/src/l5_services/api/runs.py`
- `modules/L5-services/src/l5_services/admission.py`

### Run, receipt and work state

Introduce the durable work records consumed by SA06.

**Create**

- `modules/L5-services/src/l5_services/models/run.py`
- `modules/L5-services/src/l5_services/models/receipt.py`
- `modules/L5-services/src/l5_services/models/work.py`
- `modules/L5-services/src/l5_services/migrations/versions/l5_runs_and_work.py`

### Shared registration

Add runs and their models to the established entry points.

**Modify after the creating story**

- `modules/L5-services/src/l5_services/api/router.py` — created by SA03.
- `modules/L5-services/src/l5_services/models/__init__.py` — created by SA03.

### Execution API verification

Preserve the existing submit/replay, outbox and cancellation selectors.

**Create**

- `modules/L5-services/tests/integration/test_execution_api.py`

**Existing planned test selectors**

- `modules/L5-services/tests/integration/test_execution_api.py::test_submit_replay`
- `modules/L5-services/tests/integration/test_execution_api.py::test_outbox_recovery`
- `modules/L5-services/tests/integration/test_execution_api.py::test_admission_scope_and_cancel`

## SA06

[Story #8688](https://github.com/aws-e/adp/issues/8688)

### Bounded workers and dispatch

Implement immediate send, delayed polling and the separate repair sweep.

**Create**

- `modules/L5-services/src/l5_services/workers/__init__.py`
- `modules/L5-services/src/l5_services/workers/execution.py`
- `modules/L5-services/src/l5_services/workers/relay.py`
- `modules/L5-services/src/l5_services/workers/recovery.py`

### Worker/provider boundary and storage

Implement the common adapter protocol, fenced leases and source-manifest materialization.

**Create**

- `modules/L5-services/src/l5_services/adapters/__init__.py`
- `modules/L5-services/src/l5_services/adapters/protocol.py`
- `modules/L5-services/src/l5_services/work_store.py`
- `modules/L5-services/src/l5_services/source_manifest.py`

### Execution integration

Wire SA05 admission to the common post-commit publisher; consume its existing work/run models.

**Modify after the creating story**

- `modules/L5-services/src/l5_services/admission.py` — created by SA05.

### Worker and monitoring verification

Cover transport/provider faults, fresh unchanged observations, cancellation, revocation and deadlines.

**Create**

- `modules/L5-services/tests/integration/test_lambda_execution.py`
- `modules/L5-services/tests/fixtures/security_provider.py`

**Existing planned test selectors**

- `modules/L5-services/tests/integration/test_lambda_execution.py::test_async_lifecycle`
- `modules/L5-services/tests/integration/test_lambda_execution.py::test_loss_and_recovery`
- `modules/L5-services/tests/integration/test_lambda_execution.py::test_revoke_deadline_cancel`

## SA07

[Story #8689](https://github.com/aws-e/adp/issues/8689)

### Threat-model adapter

Consume pinned document bytes and map native jobs/tasks/threats to shared results and monitoring.

**Create**

- `modules/L5-services/src/l5_services/adapters/security/threat_model.py`

### Threat-model verification

Preserve exact-source, replay and incomplete-evidence selectors.

**Create**

- `modules/L5-services/tests/integration/test_threat_model.py`

**Existing planned test selectors**

- `modules/L5-services/tests/integration/test_threat_model.py::test_source_attributed_assessment`
- `modules/L5-services/tests/integration/test_threat_model.py::test_revision_and_replay`
- `modules/L5-services/tests/integration/test_threat_model.py::test_invalid_and_partial_evidence`

## SA08

[Story #8690](https://github.com/aws-e/adp/issues/8690)

### Static code-review adapter

Implement exact-commit/PR review and archive validation with static-only provider settings.

**Create**

- `modules/L5-services/src/l5_services/adapters/security/code_review.py`
- `modules/L5-services/src/l5_services/source_archive.py`

### Code-review verification

Preserve source-admission, scan-coverage and legacy compatibility selectors.

**Create**

- `modules/L5-services/tests/integration/test_code_review.py`

**Existing planned test selectors**

- `modules/L5-services/tests/integration/test_code_review.py::test_exact_source_scan`
- `modules/L5-services/tests/integration/test_code_review.py::test_source_admission_failures`
- `modules/L5-services/tests/integration/test_code_review.py::test_static_only_and_legacy_compatibility`

## SA09

[Story #8691](https://github.com/aws-e/adp/issues/8691)

### Trigger admission

Normalize source/manual/schedule events and freeze the selected capability, binding and source revisions.

**Create**

- `modules/L5-services/src/l5_services/events/__init__.py`
- `modules/L5-services/src/l5_services/events/handler.py`
- `modules/L5-services/src/l5_services/events/sources.py`
- `modules/L5-services/src/l5_services/events/schedules.py`

### Event state and migration

Persist semantic deduplication, source order and causal-loop information.

**Create**

- `modules/L5-services/src/l5_services/models/event.py`
- `modules/L5-services/src/l5_services/migrations/versions/l5_events.py`

**Modify after the creating story**

- `modules/L5-services/src/l5_services/models/__init__.py` — created by SA03.

### Existing producer integration

Forward authenticated events through the L5 adapter while preserving current webhook/Task behavior; unavailable feeds stay unavailable.

**Modify**

- `modules/agent-factory/webhook-ingress/lambda/github/handler.py`
- `modules/gateway/src/products/source_routes.py`

### Trigger verification

Retain the planned event-ordering, document/profile and scheduled-run selectors.

**Create**

- `modules/L5-services/tests/integration/test_triggers.py`

**Existing planned test selectors**

- `modules/L5-services/tests/integration/test_triggers.py::test_pr_event_ordering`
- `modules/L5-services/tests/integration/test_triggers.py::test_profile_and_document_events`
- `modules/L5-services/tests/integration/test_triggers.py::test_schedule_revocation_and_causality`

## SA10

[Story #8692](https://github.com/aws-e/adp/issues/8692)

### Results, comparison and triage

Implement the shared result APIs and append-only triage.

**Create**

- `modules/L5-services/src/l5_services/api/results.py`
- `modules/L5-services/src/l5_services/findings.py`
- `modules/L5-services/src/l5_services/triage.py`

### Result storage and migrations

Persist result/finding/triage data and artifact references.

**Create**

- `modules/L5-services/src/l5_services/models/result.py`
- `modules/L5-services/src/l5_services/models/finding.py`
- `modules/L5-services/src/l5_services/models/triage.py`
- `modules/L5-services/src/l5_services/migrations/versions/l5_results_and_triage.py`

### Shared registration

Register result APIs and models; serialize this with other wave-2 router/model edits.

**Modify after the creating story**

- `modules/L5-services/src/l5_services/api/router.py` — created by SA03.
- `modules/L5-services/src/l5_services/models/__init__.py` — created by SA03.

### Result verification

Preserve normalization, comparison/triage and visibility selectors.

**Create**

- `modules/L5-services/tests/integration/test_results.py`

**Existing planned test selectors**

- `modules/L5-services/tests/integration/test_results.py::test_normalization_and_readback`
- `modules/L5-services/tests/integration/test_results.py::test_comparison_and_triage`
- `modules/L5-services/tests/integration/test_results.py::test_result_visibility`

## SA11

[Story #8693](https://github.com/aws-e/adp/issues/8693)

### Product Context publication

Produce contributions, retry independently of assessment execution and propagate invalidation.

**Create**

- `modules/L5-services/src/l5_services/publication/__init__.py`
- `modules/L5-services/src/l5_services/publication/client.py`
- `modules/L5-services/src/l5_services/publication/contributions.py`
- `modules/L5-services/src/l5_services/publication/recovery.py`

### Publication state and migration

Persist contribution receipts and owed publication work.

**Create**

- `modules/L5-services/src/l5_services/models/publication.py`
- `modules/L5-services/src/l5_services/migrations/versions/l5_publications.py`

**Modify after the creating story**

- `modules/L5-services/src/l5_services/models/__init__.py` — created by SA03.

### Publication verification

Preserve contribution identity, recovery and visibility/invalidation selectors.

**Create**

- `modules/L5-services/tests/integration/test_context_contributions.py`

**Existing planned test selectors**

- `modules/L5-services/tests/integration/test_context_contributions.py::test_contribution_identity`
- `modules/L5-services/tests/integration/test_context_contributions.py::test_publication_recovery`
- `modules/L5-services/tests/integration/test_context_contributions.py::test_visibility_and_invalidation`

## SA12

[Story #8694](https://github.com/aws-e/adp/issues/8694)

### Product Services/Security screens

Group the service tab, setup, binding, run, results and monitoring UI under Product pages.

**Create**

- `modules/gateway/frontend/src/pages/products/services/ProductServices.tsx`
- `modules/gateway/frontend/src/pages/products/services/SecurityConfiguration.tsx`
- `modules/gateway/frontend/src/pages/products/services/SecurityBindings.tsx`
- `modules/gateway/frontend/src/pages/products/services/SecurityRuns.tsx`
- `modules/gateway/frontend/src/pages/products/services/SecurityResults.tsx`

**Modify**

- `modules/gateway/frontend/src/App.tsx`
- `modules/gateway/frontend/src/pages/products/ProductOverview.tsx`

### Typed API client

Keep HTTP/ETag handling in a shared client consumed by the Product screens.

**Create**

- `modules/gateway/frontend/src/services/l5Services.ts`

### UI verification

Keep the published browser selector path and add focused component/client coverage.

**Create**

- `modules/gateway/frontend/tests/e2e/l5-security.spec.ts`
- `modules/gateway/frontend/src/__tests__/pages/products/services/ProductServices.test.tsx`
- `modules/gateway/frontend/src/__tests__/services/l5Services.test.ts`

### User documentation

Document the Product Services/Security experience in the canonical L5 documentation folder.

**Create**

- `docs/l5-services/user-guide.md`

**Existing planned test selectors**

- `modules/gateway/frontend/tests/e2e/l5-security.spec.ts::configure_security`
- `modules/gateway/frontend/tests/e2e/l5-security.spec.ts::run_and_inspect`
- `modules/gateway/frontend/tests/e2e/l5-security.spec.ts::cross_client_and_revocation`

## SA13

[Story #8695](https://github.com/aws-e/adp/issues/8695)

### CLI command and installation

Add the shared service command and wire the installed CLI distribution.

**Create**

- `modules/gateway/cli/adp-service.py`

**Modify**

- `modules/gateway/cli/adp`
- `modules/gateway/cli/command-manifest.json`
- `modules/gateway/cli/install.sh`

### Download and capability integration

Advertise and serve the same installed command contract.

**Modify**

- `modules/gateway/src/cli_download/routes.py`
- `modules/gateway/src/cli_capabilities/routes.py`
- `modules/gateway/src/cli_capabilities/contract.py`

### CLI verification and documentation

Keep the existing planned installed-CLI selectors and document commands/output.

**Create**

- `modules/gateway/tests/cli/test_l5_security.py`
- `docs/l5-services/cli.md`

**Existing planned test selectors**

- `modules/gateway/tests/cli/test_l5_security.py::test_cli_configuration_parity`
- `modules/gateway/tests/cli/test_l5_security.py::test_cli_execution_and_errors`
- `modules/gateway/tests/cli/test_l5_security.py::test_cross_client_and_compatibility`

## SA14

[Story #8696](https://github.com/aws-e/adp/issues/8696)

### Gateway MCP adapter and registration

Add thin L5 tool dispatch using the shared HTTP contracts and exact ETag envelopes.

**Create**

- `modules/gateway/src/internal/tools_l5.py`

**Modify**

- `modules/gateway/src/internal/tools_discovery.py`
- `modules/gateway/src/internal/tools_catalogue.py`
- `modules/gateway/src/agentauth/task_tool_routes.py`

### Existing MCP transport and client

Integrate the existing facade and MCP CLI; do not create a second backend.

**Modify**

- `modules/tools/facade/tools_facade/server.py`
- `modules/gateway/cli/adp-mcp.py`

### MCP verification and documentation

Preserve all published discovery/authority/replay selectors and document the tool surface.

**Create**

- `modules/gateway/tests/internal/test_l5_security_tools.py`
- `docs/l5-services/mcp.md`

**Existing planned test selectors**

- `modules/gateway/tests/internal/test_l5_security_tools.py::test_tool_roundtrip`
- `modules/gateway/tests/internal/test_l5_security_tools.py::test_tool_authority`
- `modules/gateway/tests/internal/test_l5_security_tools.py::test_tool_replay_and_availability`

## SA15

[Story #8698](https://github.com/aws-e/adp/issues/8698)

### Module infrastructure

Keep workers/queues/repair, IAM, alarms and variables in the L5 Terraform child module.

**Create**

- `modules/L5-services/infra/main.tf`
- `modules/L5-services/infra/variables.tf`
- `modules/L5-services/infra/outputs.tf`
- `modules/L5-services/infra/workers.tf`
- `modules/L5-services/infra/queues.tf`
- `modules/L5-services/infra/recovery.tf`
- `modules/L5-services/infra/iam.tf`
- `modules/L5-services/infra/alarms.tf`

**Modify**

- `modules/gateway/infra/main.tf`

### Package and worker artifacts

Package versioned schemas into the shared wheel, build locked worker artifacts and publish one grouped release.

**Create**

- `modules/L5-services/scripts/build-package.sh`
- `modules/L5-services/scripts/build-workers.sh`
- `modules/L5-services/scripts/publish-workers.sh`
- `modules/L5-services/requirements-security.lock`

**Modify after the creating story**

- `modules/L5-services/pyproject.toml` — created by SA02.

### Gateway build and schema integration

Stage the identical wheel, register L5 metadata/version locations and preserve compatibility checks.

**Modify**

- `modules/gateway/Dockerfile`
- `modules/gateway/pyproject.toml`
- `modules/gateway/scripts/stage-contracts.sh`
- `modules/gateway/alembic/env.py`
- `modules/gateway/alembic.ini`
- `modules/gateway/scripts/migrate-before-rollout.py`
- `modules/gateway/scripts/sync-gateway-engine.py`
- `modules/gateway/scripts/sync-gateway-writers.py`
- `modules/gateway/scripts/pricing-rollout.py`
- `modules/gateway/src/products/runtime_proof_routes.py`
- `codebuild/bs-gateway-build.yml`
- `platform/scripts/publish-shared-image.sh`

### CI and deployment workflows

Select exact-case module/contracts, preserve gateway/frontend auto-deployments and make initial worker publication explicitly grouped.

**Create**

- `.github/workflows/l5-services-release.yml`

**Modify**

- `.github/workflows/gateway-ci.yml`
- `.github/workflows/gateway-deploy.yml`
- `.github/workflows/gateway-frontend-deploy.yml`
- `.github/workflows/gateway-infra-plan.yml`
- `.github/workflows/gateway-infra-apply.yml`
- `.github/workflows/run-gateway-migrations.yml`
- `.github/workflows/postgres-schema-check.yml`
- `.github/workflows/_build-python.yml`
- `codebuild/security_image_targets.py`
- `codebuild/security-image-recipes.json`

### Operations registration and runtime proof

Register allowed L5 workflows/component readback under existing authority; target values remain private.

**Modify**

- `modules/gateway/src/agentauth/actions_routes.py`
- `modules/gateway/src/orchestration/deployment_runtime_reader.py`
- `modules/gateway/src/orchestration/deployment_manifest.py`
- `modules/gateway/src/orchestration/manifests/orchestration-deployments.yaml`

### Published release, launcher and lifecycle integration

Include L5 artifacts and ordered schema/worker rollout, status and safe teardown in existing delivery paths.

**Modify**

- `deploy.sh`
- `remove.sh`
- `status.sh`
- `platform/scripts/deploy-all.sh`
- `platform/scripts/undeploy.sh`
- `platform/scripts/release/build.py`
- `platform/scripts/release/artifacts.py`
- `platform/scripts/release/upgrade.py`
- `.github/workflows/adp-release.yml`
- `.github/workflows/adp-release-promote.yml`
- `.github/workflows/adp-release-upgrade.yml`
- `.github/workflows/adp-release-tests.yml`
- `.github/workflows/undeploy.yml`

### Deployment verification and operations guide

Preserve installed-package, recovery/rollback and scope/cleanup selectors.

**Create**

- `modules/L5-services/tests/deployment/test_l5_security_release.py`
- `docs/l5-services/operations.md`

**Existing planned test selectors**

- `modules/L5-services/tests/deployment/test_l5_security_release.py::test_installed_release`
- `modules/L5-services/tests/deployment/test_l5_security_release.py::test_recovery_and_rollback`
- `modules/L5-services/tests/deployment/test_l5_security_release.py::test_effective_scope_and_cleanup`

## SA16

[Story #8699](https://github.com/aws-e/adp/issues/8699)

### Live harness and independent reader

Keep the accepted tests/e2e entry point; place reusable live drivers, evidence, durable attempts and cleanup in tests/live.

**Create**

- `modules/L5-services/tests/e2e/test_l5_security_acceptance.py`
- `modules/L5-services/tests/live/conftest.py`
- `modules/L5-services/tests/live/fixtures.py`
- `modules/L5-services/tests/live/clients.py`
- `modules/L5-services/tests/live/providers.py`
- `modules/L5-services/tests/live/evidence.py`
- `modules/L5-services/tests/live/continuation.py`
- `modules/L5-services/tests/live/cleanup.py`

### Protected live execution and machine completion

Run the harness through a bounded workflow and integrate exact evidence/criterion completion with existing engine paths.

**Create**

- `.github/workflows/l5-security-live.yml`

**Modify**

- `modules/gateway/src/agentauth/actions_routes.py`
- `modules/gateway/src/orchestration/repository_evaluation_contract.py`
- `modules/gateway/src/orchestration/workflow_evaluation_contract.py`
- `modules/gateway/src/orchestration/external_completion.py`

### Qualification report and verification guidance

Keep sanitized summaries in docs; raw runtime/provider evidence remains private.

**Create**

- `docs/l5-services/qualification.md`

**Modify**

- `docs/l5-services/verification.md`

**Existing planned test selectors**

- `modules/L5-services/tests/e2e/test_l5_security_acceptance.py::test_clients_and_setup`
- `modules/L5-services/tests/e2e/test_l5_security_acceptance.py::test_real_security_assessments`
- `modules/L5-services/tests/e2e/test_l5_security_acceptance.py::test_isolation_recovery_cleanup`
- `modules/L5-services/tests/e2e/test_l5_security_acceptance.py::test_context_reuse`

## Shared edit coordination

| File | Stories writing it |
|---|---|
| `docs/l5-services/verification.md` | SA01, SA16 |
| `modules/L5-services/pyproject.toml` | SA02, SA15 |
| `modules/L5-services/src/l5_services/admission.py` | SA05, SA06 |
| `modules/L5-services/src/l5_services/api/router.py` | SA03, SA04, SA05, SA10 |
| `modules/L5-services/src/l5_services/models/__init__.py` | SA03, SA04, SA05, SA09, SA10, SA11 |
| `modules/gateway/src/agentauth/actions_routes.py` | SA15, SA16 |
| `modules/gateway/src/app.py` | SA02, SA03 |
| `modules/gateway/src/products/source_routes.py` | SA02, SA09 |

SA03 coordinates the L5 router/model registry and migration-head sequencing.
SA02 owns the first Product/source authorization contract; SA15 owns release
packaging and operations registration; SA16 extends the admitted live verification
path. Earlier story completion is a prerequisite for `modify_after` entries.
Parallel stories use separate implementation files and serialize their shared
registration edits. No new human checkpoint is introduced.

SA16 deliberately keeps the already published `tests/e2e/test_l5_security_acceptance.py`
entry point while grouping live helpers under `tests/live/`. Operational SA15/SA16
phases consume these same file groups; repairs update the actual changed-file
receipt rather than creating a duplicate implementation tree.

When materializing story nodes, copy each story's `file_groups` into the supported
proposal field of the same name. The graph card shows a collapsed file count and
group summary; expand **Planned files** for purposes and exact Create, Modify and
Modify-after paths. Draft previews and registered graphs read the same metadata.
The optional field is stored in the versioned plan, so no database migration is
needed and older nodes retain their existing display.

`file_map`, `planned_test_references` and the root `file_plan` remain planning-map
fields, not proposal fields. Carry the immutable story-file anchor in supported
node context/input. File groups are informational: derive any exact repair
allowlist from the checked implementation diff and existing authority.
