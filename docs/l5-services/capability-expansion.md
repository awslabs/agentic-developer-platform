# Transform, Well-Architected and FinOps expansion

These three proposed epics extend the same Product-scoped L5 service used by
[Security](https://github.com/aws-e/adp/issues/8682) and
[DevOps](https://github.com/aws-e/adp/issues/8865). They are generic capabilities
for any onboarded Product/project. ADP was a POC fixture, not the implementation
target hardcoded into these services.

Status: planning proposal, 2026-10-27. This is not an Approved design entry or an
active delivery flow. The [extracted evidence](poc-evidence.md) distinguishes
reported provider results from independently retrieved execution records.
Each epic now has seven native child stories. Its first contract slice resolves
the provider interface and catalog mapping before dependent work. Architecture
reviews are tracked in [Transform PR #9103](https://github.com/aws-e/adp/pull/9103),
[Well-Architected PR #9106](https://github.com/aws-e/adp/pull/9106) and
[FinOps PR #9097](https://github.com/aws-e/adp/pull/9097).

| Service | Human and agent outcome | Design and evidence | Epic |
|---|---|---|---|
| AWS Transform | Request a bounded modernization; receive a reviewable patch, test results and source provenance | [Transform](transform/README.md) | [#9077](https://github.com/aws-e/adp/issues/9077) |
| AWS Well-Architected Agent | Review selected architecture/IaC against product goals; receive recommendations with evidence and coverage | [Well-Architected](well-architected/README.md) | [#9078](https://github.com/aws-e/adp/issues/9078) |
| AWS FinOps Agent | Ask a scoped cost question; receive a native answer reconciled with billing data and explicit attribution limits | [FinOps](finops/README.md) | [#9079](https://github.com/aws-e/adp/issues/9079) |

All three are native children of [#8150](https://github.com/aws-e/adp/issues/8150)
and members of the ADP Platform Roadmap. Their local EC IDs are stable. The
parent's acceptance requires each committed capability, not just Security evidence:

| Parent #8150 criterion | Contributing criteria in each of #9077, #9078 and #9079 |
|---|---|
| EC-01 inputs/readiness | EC-01 |
| EC-02 authorization | EC-02 |
| EC-03 native operation | EC-03 and EC-08 portable fixtures |
| EC-04 recovery/cleanup | EC-04, EC-05 and EC-08 |
| EC-05 Context/Insights | EC-07 publication receipts |
| EC-06 later reuse | EC-07 subsequent entitled use |

This mapping is planned coverage. Acceptance needs actual compatible-revision
evidence; each epic links its seven filed children and their stable AC/SCN IDs.

## One shared architecture

The [L5 architecture](architecture.md), [versioned contracts](contracts/v1/README.md),
[peer boundaries](contracts/v1/peer-contracts.md) and
[DevOps architecture](devopsagnent/architecture.md) remain the common foundation.
The source inspection baseline is
[`32880295c8`](https://github.com/aws-e/adp/commit/32880295c85595f4d11fb77c2053088ce1a4b7ca).
This baseline contains runtime implementation; it does not establish what is
deployed or accepted in a particular installation.

| Boundary | Reuse and extension |
|---|---|
| Product context | Read Product/Profile, membership, connection and pinned source APIs. Product owns these facts; L5 never reads its SQL tables directly. The POCs' temporary four-group YAML is historical input, not today's API schema. |
| Configuration | Extend the shared typed configuration/options/readiness contract. Scope by `(tenant_id, product_id, project_id, service_id)`. Autofill candidates from Profile; preserve explicit choices and identify missing inputs for the affected capability only. |
| Public interface | Reuse `/v1/products/{product_id}/projects/{project_id}/services/{service_id}` and its configuration, binding, run and result families. UI, CLI and MCP call the same contract. Proposed service keys are `transform`, `well_architected` and `finops`; freeze them in the contract slice. |
| Persistent state | Reuse configuration versions, provider bindings, setup operations, runs/attempts, work/outbox records, results and publication state. Add provider-specific extensions only where needed; keep setup, execution, result collection and publication distinct. |
| Admission and events | Preserve delegated scope, immutable configuration/source snapshots, idempotency and existing budget/policy checks. Commit intent before immediate SQS dispatch. Recurring requests use existing schedule/event admission; each firing has a distinct deduplication identity. |
| Orchestration | Reuse bounded workers and adapter `submit`, `reconcile_submit`, `observe`, `collect`, `cancel`. Delayed SQS observations and EventBridge recovery repair owed work. Use a configurable ten-minute observation default for long jobs where supported; first dispatch is immediate. Local timeout is not provider failure or cancellation. |
| Execution host | Small SDK control operations can run in the existing Lambda workers. Long-running Transform CLI/build/test work needs an isolated job executor; a browser adapter, if qualified for FinOps, also needs a protected session executor. Select integrations with existing execution infrastructure before implementation. Do not hold a Lambda open for an hours-long job. |
| Results | Retain native identity, source/billing scope, time, revisions, coverage and evidence. Publish authorized contributions to Product Context and existing Insights interfaces. A later entitled operation must retrieve/use the result; merely uploading a document is insufficient. |
| Access and operations | Reuse Product permissions, credential broker, retention and budgets. Keep credentials out of SQL payloads, queues and reports. All user-facing access passes through the authenticated gateway; no public Lambda URL or copied DevOps-only federation assumptions. |

At this baseline, [router admission][router] permits only `security` and `devops`.
The shared [adapter protocol][protocol], [worker][worker] and [SQL models][models]
exist. Adding service names to a list is insufficient: each extension needs
typed payloads, capability readiness, provider registration, persistence,
client parity and recovery tests. Existing Security/DevOps behavior must remain
compatible. Share contract edits across epics through one integration owner.

## Product Profile and service-owned settings

The [current Profile mapping](devopsagnent/configuration/product-profile-mapping.md)
identifies implemented fields and source-binding discovery. Each service page
maps only the inputs it needs. A source reference is not its content, a connected
account is not provider eligibility, and a proposed allocation is not a spending
grant. Missing irrelevant Profile fields must not prevent an otherwise valid run.

Provider-native resource IDs, selected billing views, modernization goals,
review pillars, executor choices and execution settings belong to L5
configuration/bindings. Credentials remain in the credential system. Account,
repository, region, paths, test commands and product names are configuration;
ordinary onboarding must not require editing platform code or redeploying it.

## Delivery structure and artifact homes

Each epic proposes independently verifiable child outcomes in the same sequence.
These groupings describe the filed backlog, not dispatch instructions. The
service-specific architectures and backlogs specialize them. Exact file lists are advisory for humans; necessary related
files do not fail a node. Source write permissions are a separate security policy.

| Group | Delivered outcome | Artifact homes; proposed additions unless noted |
|---|---|---|
| Contract and provider qualification | Current provider interface, Profile mapping, typed requests/results, eligibility and recovery limits | `docs/l5-services/<service>/`; extend `docs/l5-services/contracts/v1/` and existing `modules/L5-services/src/l5_services/` configuration/contract code |
| Configuration and binding | Autofill, explicit choices, readiness, create/adopt setup and owned-only cleanup | Shared `api/`, `models/`, `migrations/`, configuration and binding code under `modules/L5-services/src/l5_services/`; provider extension under `adapters/<service>/` |
| Execution and recovery | Real operation, immediate dispatch, durable observation, uncertain-write recovery and retained evidence | Existing `admission.py`, `dispatcher.py`, `work_store.py`, `workers/`, `events/`; new provider adapter/executor integration under `modules/L5-services/` |
| Results and context | Usable domain result, independent comparison, authorized Context/Insights publication and subsequent reuse | Shared `api/results.py`, result models and publication integration; provider normalizers under `modules/L5-services/` |
| User and agent interfaces | Service tab, configuration, run/status/results; equivalent CLI and MCP operations | Existing host surfaces under `modules/gateway/frontend/src/`, `modules/gateway/cli/`, gateway tool registration/client code; domain logic stays in L5 |
| Release and acceptance | Compatible package/schema/worker release, operational checks, portable live fixtures and cleanup | Existing `.github/workflows/l5-services-ci.yml`, gateway workflows, `modules/L5-services/` infrastructure/tests and service runbooks under `docs/l5-services/` |

Group development into contracts/configuration, execution/results/clients, then
one coordinated L5 runtime/infrastructure release per compatible batch. Gateway
and frontend retain normal automatic deployment. Do not deploy per story or add
human checkpoints. Check actual workflow filters and shared release readiness
at release integration; L5 CI is not proof that provider workers were deployed.
An external eligibility gap blocks activation and its live acceptance, not
independent implementation. Preserve real authorization and billing boundaries.

Each service must work with two distinct non-ADP projects using the same release
artifacts. Fixtures vary meaningful inputs: repository/build system for Transform,
architecture/source selection for Well-Architected, and account/view attribution
for FinOps. Include foreign-tenant denial, interrupted work, results after cleanup
and later Context reuse. Accepted provider resources may persist as bindings;
cleanup deletes only resources explicitly owned by the operation or service.

The current extraction/planning creates no provider resources, recurring activity
or spending allowance. Historical POC budgets are evidence, not renewed grants.

[router]: https://github.com/aws-e/adp/blob/32880295c85595f4d11fb77c2053088ce1a4b7ca/modules/L5-services/src/l5_services/api/router.py
[protocol]: https://github.com/aws-e/adp/blob/32880295c85595f4d11fb77c2053088ce1a4b7ca/modules/L5-services/src/l5_services/adapters/protocol.py
[worker]: https://github.com/aws-e/adp/blob/32880295c85595f4d11fb77c2053088ce1a4b7ca/modules/L5-services/src/l5_services/workers/execution.py
[models]: https://github.com/aws-e/adp/tree/32880295c85595f4d11fb77c2053088ce1a4b7ca/modules/L5-services/src/l5_services/models
