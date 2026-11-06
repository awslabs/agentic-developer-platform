# Source audit and reuse evidence

## Evidence boundary

Inspected source: `56a8450aa41cc661c52ad90a1e421b8a16aca7f8` on 2026-10-18.
The original issue's `6934a5c...` references are starting points, not the tested
revision. No cloud account, runtime database, deployed UI or provider connection
was queried. Repository presence is not deployment or feature acceptance.

The issue's parent and current amendments preserve discovery-first behavior and
mandatory tenant/Project scope while separating optional business links. See
[#5411 scope correction](https://github.com/aws-e/adp/issues/5411#issuecomment-6202064715),
[capability feedback](https://github.com/aws-e/adp/issues/5411#issuecomment-6240125703),
and [expanded onboarding ownership](https://github.com/aws-e/adp/issues/5411#issuecomment-6246596020).
The current #8141 EC-01–15 are authoritative for this child. The historical
74 obligations are individually allocated in [requirements coverage](requirements-coverage.md),
not silently included as Profile implementation.

## Exhaustive tracked inventory

The machine-readable files enumerate **every tracked path at the baseline**, not
a representative set. Each row maps to a component and a disposition, including
explicit exclusions. Paths and metadata only are copied; no deployment values,
credentials, resource coordinates or historical private evidence are republished.

| Inventory | What it establishes |
|---|---|
| [tracked-artifacts.tsv](tracked-artifacts.tsv) | Complete baseline Git tree, one row per file; component/disposition/role |
| [entry-points.tsv](entry-points.tsv) | All workflows/actions, build/package manifests, Terraform/Kubernetes/CloudFormation/compose definitions, shell/shebang/main-guard executables, script-directory candidates and migrations from that tree |
| [workflows.tsv](workflows.tsv) | Every Actions workflow's events, job names and statically visible local script/action dependencies |
| [migrations.tsv](migrations.tsv) | Every gateway migration, topologically ordered parents/dependencies, all `op` call locations including downgrade paths |
| [inventory-summary.json](inventory-summary.json) | Exact counts, migration heads, component totals and unclassified-path count |
| [audit_inventory.py](audit_inventory.py) | Reproducer; rejects unmapped paths, cycles or unresolved migration parents |

Scope classification is not a claim that every line of all historical artifacts
was manually reviewed. Mechanical enumeration covers the whole repository;
semantic inspection concentrates on the actual ownership/data/deployment paths
listed below. Every file under the other domain apps is explicitly excluded from
Product runtime changes; their deployment definitions still appear in the entry
inventory. Knowledge Layer, personal services and experiments are not mistaken
for a governed Project registry. Tests and fixture executables are retained in
the inventory rather than mistaken for deployed components.

## Verified reuse and gaps

All source links below pin the inspected revision. **E** = existing source,
**P** = proposed change, **U** = unavailable/unverified integration.

| ID | Source and finding | Reuse / gap |
|---|---|---|
| S01 | [Organization, users, teams and membership models](https://github.com/aws-e/adp/blob/56a8450aa41cc661c52ad90a1e421b8a16aca7f8/modules/gateway/src/shared/models/organization.py#L25), [tenant memberships](https://github.com/aws-e/adp/blob/56a8450aa41cc661c52ad90a1e421b8a16aca7f8/modules/gateway/src/shared/models/onboarding.py#L36) | E: `organizations`, `users`, `teams`, `team_memberships`, `tenant_memberships`; `tenants` is a thin 1:1 wrapper. P: governed registry, not another tenant or team table. |
| S02 | [Workspace resolution](https://github.com/aws-e/adp/blob/56a8450aa41cc661c52ad90a1e421b8a16aca7f8/modules/gateway/src/shared/identity/workspaces.py#L112), [workflow access](https://github.com/aws-e/adp/blob/56a8450aa41cc661c52ad90a1e421b8a16aca7f8/modules/gateway/src/shared/identity/workflow_access.py#L8) | E: live membership, linked organization-local accounts, revocation; `is_active` selects workspace, not membership validity. Workflow membership alone does not implement Project privacy. |
| S03 | [Hierarchy API](https://github.com/aws-e/adp/blob/56a8450aa41cc661c52ad90a1e421b8a16aca7f8/modules/gateway/src/admin/hierarchy.py#L25), [team membership service](https://github.com/aws-e/adp/blob/56a8450aa41cc661c52ad90a1e421b8a16aca7f8/modules/gateway/src/admin/team_memberships.py#L1) | E: organization/department/team/member management, expected revisions, dependency-aware deletion. P: add explicit protection for new registry FKs; do not duplicate membership APIs. |
| S04 | [Organization GitHub connections](https://github.com/aws-e/adp/blob/56a8450aa41cc661c52ad90a1e421b8a16aca7f8/modules/gateway/src/admin/org_connections/routes.py#L1) | E: platform-admin gate is intentional for identity-minting operations. P: member-readable, entitlement-filtered source picker uses service logic; no privilege bypass by forwarding a member into an admin handler. |
| S05 | [Connection lifecycle routes](https://github.com/aws-e/adp/blob/56a8450aa41cc661c52ad90a1e421b8a16aca7f8/modules/gateway/src/admin/connections/routes.py#L122), [frontend contract](https://github.com/aws-e/adp/blob/56a8450aa41cc661c52ad90a1e421b8a16aca7f8/modules/gateway/frontend/src/services/connections.ts#L20) | E: install callbacks, disconnect/revocation and tri-state live verification. Cached repository configuration is not proof of current access. |
| S06 | [Vault models](https://github.com/aws-e/adp/blob/56a8450aa41cc661c52ad90a1e421b8a16aca7f8/modules/gateway/src/shared/models/vault.py#L114), [OAuth callback](https://github.com/aws-e/adp/blob/56a8450aa41cc661c52ad90a1e421b8a16aca7f8/modules/gateway/src/auth/connections_routes.py#L68), [credential routes](https://github.com/aws-e/adp/blob/56a8450aa41cc661c52ad90a1e421b8a16aca7f8/modules/gateway/src/auth/vault_routes.py#L84) | E: credential references, OAuth attempts, consent and validation evidence. P: attach verified references, never values; Product callback cannot establish tenant identity. |
| S07 | [Current onboarding frontend service](https://github.com/aws-e/adp/blob/56a8450aa41cc661c52ad90a1e421b8a16aca7f8/modules/gateway/frontend/src/services/onboarding.ts#L1), [UI routes](https://github.com/aws-e/adp/blob/56a8450aa41cc661c52ad90a1e421b8a16aca7f8/modules/gateway/frontend/src/App.tsx#L99) | E: account access request/approval and settings routes. P: Product workspace routes; do not replace the account-access guard or rename that service into product onboarding. |
| S08 | [CLI common transport](https://github.com/aws-e/adp/blob/56a8450aa41cc661c52ad90a1e421b8a16aca7f8/modules/gateway/cli/adp_common.py#L623), [command manifest](https://github.com/aws-e/adp/blob/56a8450aa41cc661c52ad90a1e421b8a16aca7f8/modules/gateway/cli/command-manifest.json#L1), [hierarchy commands](https://github.com/aws-e/adp/blob/56a8450aa41cc661c52ad90a1e421b8a16aca7f8/modules/gateway/cli/adp-hierarchy.py#L20) | E: JSON envelope, exit conventions, capability discovery, tenant-aware transport and admin commands. P: Product/onboard providers + manifest/install/serve allowlists, not a new CLI distribution. |
| S09 | [Context model](https://github.com/aws-e/adp/blob/56a8450aa41cc661c52ad90a1e421b8a16aca7f8/docs/product-context/README.md#L159), [schema](https://github.com/aws-e/adp/blob/56a8450aa41cc661c52ad90a1e421b8a16aca7f8/docs/product-context/context-model-v1.schema.json#L1) | E: proposed typed snapshot/export schema, not deployed storage/API. Reuse field meanings and provenance. New Profile transport is versioned separately; do not emit snapshots as Insights events. |
| S10 | [Knowledge scope](https://github.com/aws-e/adp/blob/56a8450aa41cc661c52ad90a1e421b8a16aca7f8/modules/gateway/src/knowledge/source_policy/scope.py#L1), [orchestration models](https://github.com/aws-e/adp/blob/56a8450aa41cc661c52ad90a1e421b8a16aca7f8/modules/gateway/src/orchestration/models.py#L1), [task routes](https://github.com/aws-e/adp/blob/56a8450aa41cc661c52ad90a1e421b8a16aca7f8/modules/gateway/src/tasks/routes.py#L1) | E: existing execution/knowledge entry points. No governed Product registry found in gateway model/migration inventory. P: #8148 supplies consumer enforcement; do not silently backfill from repo names or personal knowledge IDs. |
| S11 | [Gateway image](https://github.com/aws-e/adp/blob/56a8450aa41cc661c52ad90a1e421b8a16aca7f8/modules/gateway/Dockerfile#L80), [buildspec](https://github.com/aws-e/adp/blob/56a8450aa41cc661c52ad90a1e421b8a16aca7f8/codebuild/bs-gateway-build.yml#L1) | E: backend, staged contracts, Alembic and CLI packaged together. P: ensure all new CLI files and shared schema are actually staged/served; path trigger alone is insufficient. |
| S12 | [Gateway CI](https://github.com/aws-e/adp/blob/56a8450aa41cc661c52ad90a1e421b8a16aca7f8/.github/workflows/gateway-ci.yml#L1), [deploy](https://github.com/aws-e/adp/blob/56a8450aa41cc661c52ad90a1e421b8a16aca7f8/.github/workflows/gateway-deploy.yml#L1) | E: path-filtered main deploy, exact revision/digest evidence; docs-only changes excluded. P: Profile contract/security tests and shipped capability version assertion. |
| S13 | [Pre-rollout migration job](https://github.com/aws-e/adp/blob/56a8450aa41cc661c52ad90a1e421b8a16aca7f8/modules/gateway/scripts/migrate-before-rollout.py#L1), [call site](https://github.com/aws-e/adp/blob/56a8450aa41cc661c52ad90a1e421b8a16aca7f8/.github/workflows/gateway-deploy.yml#L1032), [standalone migration workflow](https://github.com/aws-e/adp/blob/56a8450aa41cc661c52ad90a1e421b8a16aca7f8/.github/workflows/run-gateway-migrations.yml#L5) | E: candidate-image migrations before serving rollout plus reusable/manual migration path. P: use single serialized release path, prove migration compatibility and do not add parallel apply jobs. |
| S14 | [Frontend publisher](https://github.com/aws-e/adp/blob/56a8450aa41cc661c52ad90a1e421b8a16aca7f8/.github/workflows/gateway-frontend-deploy.yml#L1), [manual publisher](https://github.com/aws-e/adp/blob/56a8450aa41cc661c52ad90a1e421b8a16aca7f8/modules/gateway/scripts/deploy-frontend.sh#L1) | E: frontend has independent main-path trigger, build/publication and cache invalidation. P: capability-gated UI tolerates older server and binds served asset hash to release. |
| S15 | [CLI evaluation](https://github.com/aws-e/adp/blob/56a8450aa41cc661c52ad90a1e421b8a16aca7f8/.github/workflows/eval-cli-uplift.yml#L1), [browser evaluation](https://github.com/aws-e/adp/blob/56a8450aa41cc661c52ad90a1e421b8a16aca7f8/.github/workflows/e2e-new-ui-playwright.yml#L1), [regression catalog](https://github.com/aws-e/adp/blob/56a8450aa41cc661c52ad90a1e421b8a16aca7f8/tests/regression/catalog.json#L1) | E: installed CLI, served browser and catalog/evidence machinery. P: add scenarios and selectors, not duplicate CI per feature. U: Product live fixtures/results. |
| S16 | [Root launcher](https://github.com/aws-e/adp/blob/56a8450aa41cc661c52ad90a1e421b8a16aca7f8/deploy.sh#L1), [core orchestrator](https://github.com/aws-e/adp/blob/56a8450aa41cc661c52ad90a1e421b8a16aca7f8/platform/scripts/deploy-all.sh#L1), [deployment manifest](https://github.com/aws-e/adp/blob/56a8450aa41cc661c52ad90a1e421b8a16aca7f8/docs/adp-platform-deployment/deployment-manifest.md#L351) | E: source/release install path, separate infra plan/manual apply and recovery entry points. P: verify fresh installs and upgrades include Profile migrations/client contracts through the existing release manifest. |
| S17 | [Metered model routes](https://github.com/aws-e/adp/blob/56a8450aa41cc661c52ad90a1e421b8a16aca7f8/modules/gateway/src/proxy/routes.py#L1), [restricted Crew tool relay](https://github.com/aws-e/adp/blob/56a8450aa41cc661c52ad90a1e421b8a16aca7f8/modules/gateway/src/agentauth/chat_tools.py#L1) | E: authenticated model surface and separately proof-bound tool relay. P: bounded proposal guidance through metered model path; no direct provider SDK or relay-auth bypass. |
| S18 | [Team creation](https://github.com/aws-e/adp/blob/56a8450aa41cc661c52ad90a1e421b8a16aca7f8/modules/gateway/src/admin/routes.py#L942), [existing-person placement](https://github.com/aws-e/adp/blob/56a8450aa41cc661c52ad90a1e421b8a16aca7f8/modules/gateway/src/admin/routes.py#L1111), [GitHub assignment](https://github.com/aws-e/adp/blob/56a8450aa41cc661c52ad90a1e421b8a16aca7f8/modules/gateway/src/admin/identity/github_enrollment_routes.py#L15) | E: team creation requires org-admin permission; person placement/GitHub enrollment are platform-admin operations. P: reuse with explicit admin handoff, then use returned organization-local user ID; do not invent an invite flow. |
| S19 | [CLI download allowlist](https://github.com/aws-e/adp/blob/56a8450aa41cc661c52ad90a1e421b8a16aca7f8/modules/gateway/src/cli_download/routes.py#L85), [deployment selection/private state](https://github.com/aws-e/adp/blob/56a8450aa41cc661c52ad90a1e421b8a16aca7f8/modules/gateway/cli/adp_deployments.py#L25) | E: served scripts and deployment-specific identity/state boundaries. P: add Product helpers to all packaging/serve lists; keep resume metadata within existing deployment/tenant partition. |

The database audit includes the full migration graph, not just initial migration
001. In particular 021 establishes tenant membership, 040 team membership, 043
person-anchor rekeying, 048 detects legacy team inconsistency, 066 adds credential
evidence/delegation and operation fingerprints, 071 installation revocation, 075
membership tombstones and 097 OAuth pointer provenance. The graph report preserves
forks/merges; a filename sort alone is not a valid schema order. Relevant final
models and subsequent migrations were compared before proposing new tables.

## Coverage limits

- **No unresolved tracked-path classification gaps.** Exact mechanical counts
  are in the summary. Shell-generated commands, external reusable actions and
  provider-side configuration are not a statically complete execution graph.
  `workflows.tsv` reports literal dependencies only; this limit is not hidden as
  a claim of exhaustive transitive execution analysis.
- **No live schema/deployment claim.** The pinned migration graph and ORM are
  source evidence; applied heads, configuration, IAM and current health remain
  unknown. Deployment owner verifies them before the first rollout.
- **No equivalent governed registry found in gateway.** Repository-wide
  `project_id` searches also find GitLab-native IDs, knowledge grouping, examples
  and future designs. None is adopted as a governed Project without an explicit
  reviewed migration. Other domain-app registries are excluded, not overwritten.
- **Peer APIs unavailable as settled contracts.** #8148, #8149 and #8150 are
  planning epics. This design specifies the Profile side of handoffs and a
  negotiation checkpoint; it does not invent a deployed Context/L5 URL.
- **Provider coverage remains bounded.** GitHub and AWS reuse existing connection
  flows; generic Jira/Linear/wiki/support/financial adapters are explicit later
  increments. FinOps console evidence is not a programmatic Agent API; Support
  eligibility and provider capability demonstrations remain with #8150.
- **External reference limits.** HTTP status semantics (RFC 9110),
  [OpenAPI 3.1](https://spec.openapis.org/oas/v3.1.0) and
  [JSON Schema 2020-12](https://json-schema.org/draft/2020-12/json-schema-core.html)
  inform the transport. No new cloud service selection or provider pricing claim
  is made; repository source and validator behavior are the evidence for reuse.
