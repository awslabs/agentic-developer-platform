# Product DevOps Agent implementation backlog

[DevOps epic](https://github.com/aws-e/adp/issues/8865) is a native child of [L5 Services #8150](https://github.com/aws-e/adp/issues/8150), alongside
[Security #8682](https://github.com/aws-e/adp/issues/8682). The 19 DevOps stories share the
[architecture proposal](architecture.md), [configuration mapping](configuration/product-profile-mapping.md)
and [delivery/file plan](delivery-plan.json). GitHub story bodies own their stable
acceptance and scenario IDs; this table is an index, not a second acceptance owner.

**Requirements R2 — project portability:** This builds one shared capability for
any onboarded Product/project. ADP repository paths and roadmap links are development
lineage, never workload defaults. The [portability contract](architecture.md#project-portability-contract)
requires project-derived inputs, capability-specific prerequisites and one platform
artifact set for multiple projects. EC-10 requires two non-ADP projects, including a
non-EKS workload, to prove operation/results and Context reuse without a platform
code change or redeployment. ADP may be an additional fixture. Previous acceptance
does not transfer to changed R2 criteria; existing IDs are preserved.

**Requirements R3 — secure native UI ingress:** DA08 uses the existing
`launchNativeBrowser` gateway/L5 operation with no Function URL or separate public
launcher. DA18 verifies its infrastructure boundary; DA19 verifies deployed
denials before federation, ingress and owned POC cleanup. The architecture's
[Human access contract](architecture.md#human-access) is authoritative. Existing
AC/SCN IDs and unaffected R2 evidence are preserved; affected criteria need R3
proof and the final flow evaluator must require the two new SSO observations.

## Delivery waves

| Wave | Construction work | Completion signal |
|---|---|---|
| 1: Contracts and Profile foundation | DA01–DA03 | Reviewed schemas, Profile normalization/handoff and configuration integration tests |
| 2: Persistent setup and access | DA04–DA08; DA18 release tooling | Scoped space/account/source/knowledge setup, production access implementation and packaging checks |
| 3: Operations and clients | DA09–DA17, ordered by their explicit dependencies | Investigation/results, release capabilities, prevention, Context and all clients tested offline |
| 4: Combined release and qualification | DA19 harness construction, then DA19 release/operate phase using DA18 tooling | Native results, clients, Context, recovery/cleanup and two non-ADP projects on identical artifacts, including non-EKS |

Dependencies control execution within a wave; wave labels do not authorize
overlapping edits. DA19 may build fixture/verifier code after DA01 freezes the
contract, but its final harness integration waits for compatible construction
receipts from DA04–DA18. Its release/operate phase waits for that harness merge.
These are phases of one issue, not a circular dependency on its own live acceptance.

Use existing machine evaluations and one initial execution authorization, with
no recurring human approval nodes. Registering/activating a runnable flow is a
separate step; this JSON is planning metadata, not an engine submission. Current
private targets, policies and budgets must be bound at that step. Historical
spike permissions and the Security flow budget do not transfer automatically.

Do not require a deployment after each code wave. Gateway/frontend automatic
deployments may proceed; DA18/DA19 coordinate one accumulated L5 release plus any
necessary corrective release. Code construction checks can pass before live
criteria, while issue/epic acceptance retains the mapped live requirements.

## Stories and planned artifacts

All new paths are **proposed**, inspected against source `33fa3ffbb2c428fc67f70d5514cb62099ed9517e`. “Modify after”
names the dependency that creates a shared file. Migration filenames are logical
locations, not allocated Alembic revision IDs. Resolve current heads during implementation.

File groups are optional, non-exhaustive display metadata for humans and future
flow nodes. They never restrict agent diffs, create repair-path allowlists or fail
a node because a necessary file was not listed. DA03 coordinates shared schema/API
edits, DA02 Profile changes, DA18 packaging/migrations and DA19 final integration.

| Story | Deliverables | Dependencies | Planned artifact groups |
|---|---|---|---|
| [DA01](https://github.com/aws-e/adp/issues/8866) | Publish the DevOps architecture, schemas and capability qualification contract<br>• Freeze shared contracts, conditional prerequisites and project portability rules. | None; shared #8683 | **Architecture and generated contracts** (modify): `docs/l5-services/devopsagnent/architecture.md`, `docs/l5-services/contracts/v1/catalog.py`, `docs/l5-services/contracts/v1/validate.py`<br>**Capability fixtures and compatibility** (create): `docs/l5-services/devopsagnent/capability-matrix.md`, `modules/L5-services/tests/contracts/test_devops_contracts.py` |
| [DA02](https://github.com/aws-e/adp/issues/8867) | Normalize Product environments and connect the DevOps capability handoff<br>• Make saved Product selections available consistently and link DevOps configuration without losing Profile progress. | DA01; shared #8684, #8685 | **Profile and handoff** (modify): `modules/gateway/src/products/configuration.py`, `modules/gateway/src/products/source_routes.py`, `modules/gateway/src/product_onboarding/handoffs.py`<br>**Migration and Product regression coverage** (create): `modules/gateway/alembic/versions/normalize_profile_environments.py`, `modules/gateway/tests/products/test_devops_handoff.py` |
| [DA03](https://github.com/aws-e/adp/issues/8868) | Implement DevOps configuration, readiness and delegated access<br>• Configure any onboarded project and report only its capability-specific readiness gaps. | DA01, DA02; shared #8684, #8685, #8688 | **DevOps configuration and storage** (create): `modules/L5-services/src/l5_services/devops/configuration.py`, `modules/L5-services/src/l5_services/models/devops.py`, `modules/L5-services/src/l5_services/migrations/versions/devops_state.py`<br>**Shared API integration** (modify after SA03): `modules/L5-services/src/l5_services/api/configuration.py`, `modules/gateway/src/l5_integration.py`<br>**Configuration integration tests** (create): `modules/L5-services/tests/integration/test_devops_configuration.py` |
| [DA04](https://github.com/aws-e/adp/issues/8869) | Manage persistent DevOps Agent Spaces and recover their lifecycle<br>• Provide recoverable create, adopt, update, suspend, resume and retirement operations for a scoped Agent Space. | DA03; shared #8686, #8687, #8688 | **Provider lifecycle and reconciliation** (create): `modules/L5-services/src/l5_services/adapters/devops/spaces.py`, `modules/L5-services/src/l5_services/devops/lifecycle.py`<br>**Shared binding integration** (modify after SA04): `modules/L5-services/src/l5_services/api/bindings.py`<br>**Lifecycle integration tests** (create): `modules/L5-services/tests/integration/test_devops_spaces.py` |
| [DA05](https://github.com/aws-e/adp/issues/8870) | Configure DevOps AWS resource access with conditional EKS support<br>• Verify selected AWS resource access; require EKS setup only for Kubernetes workloads. | DA04; shared #8684, #8688 | **AWS/EKS provider access** (create): `modules/L5-services/src/l5_services/adapters/devops/aws_access.py`, `modules/L5-services/src/l5_services/devops/resource_scope.py`<br>**Access infrastructure** (create): `modules/L5-services/infra/devops-access.tf`<br>**Permission and scope tests** (create): `modules/L5-services/tests/integration/test_devops_access.py` |
| [DA06](https://github.com/aws-e/adp/issues/8871) | Connect DevOps repositories, pipelines and telemetry with verified readiness<br>• Connect each project’s supported sources and deployment evidence without ADP layout assumptions. | DA04, DA05; shared #8684 | **Repository and telemetry adapters** (create): `modules/L5-services/src/l5_services/adapters/devops/integrations.py`, `modules/L5-services/src/l5_services/devops/source_readiness.py`<br>**Integration scenario tests** (create): `modules/L5-services/tests/integration/test_devops_integrations.py` |
| [DA07](https://github.com/aws-e/adp/issues/8872) | Publish versioned Product instructions, runbooks and skills to DevOps<br>• Synchronize entitled Product knowledge into native instructions, skills and qualified source context. | DA06; shared #8684 | **Knowledge synchronization and native assets** (create): `modules/L5-services/src/l5_services/devops/knowledge.py`, `modules/L5-services/src/l5_services/adapters/devops/assets.py`<br>**Knowledge fixtures and tests** (create): `modules/L5-services/tests/integration/test_devops_knowledge.py` |
| [DA08](https://github.com/aws-e/adp/issues/8873) | Provide Product-authorized DevOps native UI access from ADP sign-in<br>• Implement gateway-only launch, current Product authorization and person/session policy; no Function URL. | DA04; shared #8684 | **Launch API and native session adapter** (create): `modules/L5-services/src/l5_services/api/devops_access.py`, `modules/L5-services/src/l5_services/devops/access.py`, `modules/L5-services/src/l5_services/adapters/devops/federation.py`<br>**Access policy and session tests** (create): `modules/L5-services/tests/integration/test_devops_sessions.py`, `modules/L5-services/tests/e2e/test_devops_sso.py`<br>**Scoped deployment roles** (create): `modules/L5-services/infra/devops-ui-access.tf` |
| [DA09](https://github.com/aws-e/adp/issues/8874) | Admit DevOps investigations from manual requests and verified incident events<br>• Submit scoped investigations through shared L5 admission and durable event-driven dispatch. | DA03, DA05, DA06; shared #8687, #8688, #8691 | **Investigation submission and incident events** (create): `modules/L5-services/src/l5_services/adapters/devops/investigations.py`, `modules/L5-services/src/l5_services/devops/incident_events.py`<br>**Shared execution integration** (modify after SA05/SA06): `modules/L5-services/src/l5_services/api/runs.py`, `modules/L5-services/src/l5_services/workers/execution.py`<br>**Admission/producer-consumer tests** (create): `modules/L5-services/tests/integration/test_devops_investigations.py` |
| [DA10](https://github.com/aws-e/adp/issues/8875) | Track DevOps native work, monitoring health and reusable results<br>• Reconcile DevOps work into scoped L5 runs and results with honest origin and monitoring state. | DA09; shared #8688, #8692 | **Native events, readback and results** (create): `modules/L5-services/src/l5_services/adapters/devops/observations.py`, `modules/L5-services/src/l5_services/devops/monitoring.py`, `modules/L5-services/src/l5_services/devops/results.py`<br>**Run/result integration tests** (create): `modules/L5-services/tests/integration/test_devops_results.py` |
| [DA11](https://github.com/aws-e/adp/issues/8876) | Deliver native DevOps release-readiness reviews with exact change evidence<br>• Run release-readiness reviews and return source-grounded native findings through L5. | DA06, DA07, DA10; shared #8684 | **Release-readiness adapter** (create): `modules/L5-services/src/l5_services/adapters/devops/release_readiness.py`<br>**Contract and live release evidence** (create): `modules/L5-services/tests/integration/test_devops_release_readiness.py`, `modules/L5-services/tests/live/test_devops_release_readiness.py` |
| [DA12](https://github.com/aws-e/adp/issues/8877) | Deliver native DevOps release verification against an isolated deployment<br>• Execute bounded native release-verification scenarios with independently checked deployment identity. | DA06, DA10; shared #8684 | **Release-verification adapter and fixture validation** (create): `modules/L5-services/src/l5_services/adapters/devops/release_verification.py`, `modules/L5-services/src/l5_services/devops/verification_targets.py`<br>**Target and scenario tests** (create): `modules/L5-services/tests/integration/test_devops_release_verification.py`, `modules/L5-services/tests/live/test_devops_release_verification.py` |
| [DA13](https://github.com/aws-e/adp/issues/8878) | Configure and reconcile recurring DevOps prevention<br>• Manage explicit prevention enablement, cadence, pause and individual execution tracking. | DA04, DA10; shared #8691 | **Prevention policy and adapter** (create): `modules/L5-services/src/l5_services/devops/prevention.py`, `modules/L5-services/src/l5_services/adapters/devops/prevention.py`<br>**Schedule and budget semantics tests** (create): `modules/L5-services/tests/integration/test_devops_prevention.py` |
| [DA14](https://github.com/aws-e/adp/issues/8879) | Publish DevOps findings and learned assets into Product Context<br>• Publish selected DevOps results with provenance, freshness and revocation-aware reuse. | DA07, DA10; shared #8693, #8149 | **Context contribution translation** (create): `modules/L5-services/src/l5_services/devops/context.py`<br>**Shared publication integration** (modify after SA11): `modules/L5-services/src/l5_services/publication/contributions.py`<br>**Contribution, invalidation and reuse tests** (create): `modules/L5-services/tests/integration/test_devops_context.py`, `modules/L5-services/tests/live/test_devops_context_reuse.py` |
| [DA15](https://github.com/aws-e/adp/issues/8880) | Add the Product DevOps configuration, operations and native UI tab<br>• Provide the complete DevOps service journey in the existing Product L5 Services UI. | DA03, DA08, DA10, DA11, DA12, DA13, DA14; shared #8694 | **Product DevOps tab and client integration** (create): `modules/gateway/frontend/src/pages/products/services/DevOpsServiceTab.tsx`, `modules/gateway/frontend/src/__tests__/pages/products/services/DevOpsServiceTab.test.tsx`<br>**Existing L5 client and route** (modify after SA12): `modules/gateway/frontend/src/services/l5Services.ts`<br>**Browser journeys** (create): `modules/gateway/frontend/tests/e2e/l5-devops.spec.ts` |
| [DA16](https://github.com/aws-e/adp/issues/8881) | Expose complete DevOps configuration and operations in the installed CLI<br>• Extend the installed adp service CLI with DevOps configuration, bindings, capabilities and results. | DA03, DA10, DA11, DA12, DA13, DA14; shared #8695 | **Installed CLI and command manifest** (modify after SA13): `modules/gateway/cli/adp-service.py`, `modules/gateway/cli/command-manifest.json`<br>**CLI reference and installed tests** (create): `docs/l5-services/devopsagnent/cli.md`, `modules/gateway/tests/cli/test_devops_service.py` |
| [DA17](https://github.com/aws-e/adp/issues/8882) | Expose scoped DevOps capabilities through the existing MCP transport<br>• Expose the DevOps operation matrix through existing authenticated L5 MCP tools. | DA03, DA10, DA11, DA12, DA13, DA14; shared #8696 | **MCP contract and host registration** (modify after SA14): `docs/l5-services/contracts/v1/mcp-tools.json`, `modules/gateway/src/internal/tools_discovery.py`, `modules/gateway/src/internal/tools_l5.py`<br>**MCP transport integration tests** (create): `modules/gateway/tests/internal/test_devops_mcp.py` |
| [DA18](https://github.com/aws-e/adp/issues/8883) | Package and deploy DevOps through the shared L5 release pipeline<br>• Ship one shared runtime; onboard further projects through configuration and scoped resource setup. | DA01, DA03; shared #8698 | **Shared package and worker publication** (modify after SA15): `modules/L5-services/pyproject.toml`, `modules/L5-services/scripts/build-package.sh`, `modules/L5-services/scripts/build-workers.sh`<br>**DevOps queues/events and release wiring** (create): `modules/L5-services/infra/devops-events.tf`<br>**GitHub Actions and host filters** (modify after SA15): `.github/workflows/l5-services-release.yml`, `.github/workflows/l5-services-ci.yml`, `.github/workflows/gateway-deploy.yml`<br>**Packaging and infrastructure tests** (create): `modules/L5-services/tests/deployment/test_devops_release.py` |
| [DA19](https://github.com/aws-e/adp/issues/8884) | Qualify DevOps across projects and reusable context on one shared release<br>• Qualify all capabilities plus two non-ADP projects, including non-EKS, on the same release. | DA04, DA05, DA06, DA07, DA08, DA09, DA10, DA11, DA12, DA13, DA14, DA15, DA16, DA17, DA18; shared #8698 | **Live fixtures, drivers and independent verifier** (create): `modules/L5-services/tests/live/devops/`, `modules/L5-services/scripts/qualify-devops.py`<br>**Qualification workflow and combined release** (create): `.github/workflows/l5-devops-live.yml`<br>**Release integration** (modify after DA18): `.github/workflows/l5-services-release.yml`<br>**Acceptance and operations documentation** (create): `docs/l5-services/devopsagnent/qualification.md` |

## Acceptance and readiness

DA01 owns real Module/Feature catalog mapping before dependent dispatch. Product
scope comes from authorized APIs, not the repository name or these issue labels.
Unknown source permissions, native API eligibility, SSO session behavior and live
fixtures are explicit story-owned qualification work. Independent implementation
can proceed against reviewed contracts; missing live evidence cannot pass final
acceptance. A supported negative result is useful investigation evidence but does
not complete a committed operational capability.

Pre-merge validation reuses `L5 contract and module tests` and relevant host checks.
DA19 assembles the full live matrix with exact requirements/source/runtime revisions,
actual provider observations, each attempt and cleanup. No self-reported success
boolean, mock result or closed issue replaces that evidence.
