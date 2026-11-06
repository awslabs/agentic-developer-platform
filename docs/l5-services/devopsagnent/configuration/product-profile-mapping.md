# Product Profile → DevOps Agent configuration mapping

This review answers two different questions: **what ADP can capture today**, and
**what still has to be configured in AWS DevOps Agent**. A supported Profile field
is not evidence that a particular Product has populated it or that AWS has received it.
Use this table with the [configuration manual](README.md).

These fields belong to whichever onboarded Product/project is selected. ADP
source links identify the implementation of the Profile API; they do not select
ADP as the workload. Resolve values separately for every project. See the proposed
[portability contract](../architecture.md#project-portability-contract) for the
shared runtime and conditional connector requirements.

Baseline: ADP main commit
[`925ed8d3d`](https://github.com/aws-e/adp/commit/925ed8d3d9a67aa7b0eb33ea91343e7356d5c5be),
reviewed 2026-10-26. The older [Profile architecture][profile-design] and
[Context capability design][context-design] describe intent. The runtime files
linked below establish the implemented contract at this baseline; they do not
prove that every installation has deployed that revision.

## Reading the mapping

| Coverage | Meaning |
| --- | --- |
| Captured | A typed field/value exists in the Profile contract. Check the selected Product's actual value. |
| Reference | A typed `RecordRef` slot exists; the content must be resolved through its authorized source. A reference does not mean a connector or provider adapter exists. |
| Discovered | Current source code can return bounded metadata from an authorized connection. Inspect freshness, permissions and coverage. |
| Missing | No dedicated Profile field or completed DevOps integration supplies this setting; collect/store it with the indicated owner. |

Paths beginning `configuration.` are in the `getProfile` response. Paths beginning
`binding.` refer to a separate source-binding response, not extra Profile JSON fields.
All `RecordRef` values use `tenant_id`, `project_id`, `source_connection_id`,
`record_type`, `record_id`, and `record_revision`. They are not credentials or
native AWS service-association IDs.

“Required” below is scoped to a capability. AWS can create an empty Agent Space;
that does not make it useful for incident investigation, release testing or ADP
integration. Sources S1–S9 at the end link to the implementation behind the paths.

## Identity, accounts and access

| DevOps configuration requirement | When required | What Product Profile has | Coverage and remaining configuration |
| --- | --- | --- | --- |
| Stable Product/Project scope | Every ADP-managed setup | `tenant_id`, `product_id`, `project_id`, `revision`; current Product–Project binding (S1, S3) | **Captured.** Carry as ADP configuration provenance. AWS owns `agentSpaceId`; do not substitute a Product UUID for it. |
| Name, description, purpose and audience | Space naming and useful operating context | `product.name`, `purpose`, `intended_users`, `domain`, `goals`, `starting_point`, `lifecycle_status` (S1) | **Captured.** Draft AWS name/description and instructions; select final space identity separately. These fields do not establish readiness or authority. |
| Responsible people and access policy | Shared operation and native UI access | `product.owner_ref`, optional `product.team_id`, Product People API, `configuration.basic.user_mappings` (S1, S2, S6) | **Captured/reference.** Resolve current members. Still configure IAM/Identity Center/IdP assignments and native operator permissions; an ADP team is not an AWS group assignment. |
| Monitoring account and Agent Space home Region | AWS resource investigation | `configuration.advanced.environments` references; AWS `binding.verified_environment.account_id`, `regions`, `role_ref` (S2, S4, S5) | **Discovered candidates.** Choose the hosting account and supported Region explicitly. Observed `regions` contains the connection's default Region, not all Regions hosting workloads. See the environment-path mismatch below. |
| Primary and secondary workload accounts | Each account being investigated | Environment bindings have `selection.connection_ref`, `namespace`, `native_id` and verified account metadata (S4, S5) | **Discovered.** Decide `monitor` versus `source`, create/select compatible roles and save each AWS service association. A connected ADP account is not an AWS DevOps association. |
| Environment name and production status | Routing and execution scope | `binding.selection.environment.name`, `.environment_class`, `.production` (S4) | **Captured on binding.** Use to propose scope and defaults; ADP has no dedicated DevOps verification-environment ID here. |
| DevOps resource role and effective permissions | AWS account association | `binding.verified_environment.role_ref`, `permission_tier`, verification time/source revision (S5) | **Partial discovery.** Current `permission_tier` is `unknown`. The referenced ADP role is not proof of `aidevops.amazonaws.com` trust or resource access. L5/provider setup owns compatible role selection, policies and verification. |
| Native web-app operator role and authentication | Human access to DevOps Agent | Owner/team plus policy references; no `operatorAppRoleArn`, `authFlow`, Identity Center instance or OIDC client contract (S1, S2) | **Missing provider settings.** Store with the DevOps binding; store secrets in the credential system. Use AWS-supported setup or qualify the separate ADP SSO adapter. |
| EKS clusters, namespaces and access entries | Kubernetes investigation | Environment references and optional architecture/runbook references (S2) | **Missing typed inventory/access selection.** Resolve cluster identity and network needs; configure the relevant cloud role's access entry and `AmazonAIOpsAssistantPolicy`. Do not infer this from account verification. |
| Resource/Region boundaries and discovery coverage | Application isolation and useful topology | Environment/deployment/architecture references and `configuration.governance` (S2) | **Reference; selectors missing.** L5 setup must capture the selected application resources, dependencies, relevant tags/stacks, Region policy and scope verification. A topology view does not enforce access. |
| Encryption and retention choices | Organizational requirements and data lifecycle | `configuration.governance.data_classification`, `.retention_policy_ref`, `.access_review_date` (S2) | **Captured/reference.** No native KMS key or provider retention/lifecycle setting is resolved. Select supported AWS options and record them with the binding; a retention-policy reference is not enforcement. |

## Code, telemetry and operational knowledge

| DevOps configuration requirement | When required | What Product Profile has | Coverage and remaining configuration |
| --- | --- | --- | --- |
| Repository identity and authorized source | Code/deployment investigation or release features | `configuration.basic.repositories`; repository binding `selection.provider`, `.namespace`, `.native_id`, `.connection_ref`; current GitHub/GitLab source discovery (S2, S4, S5) | **Reference + discovery.** Resolve repository name, owner and provider-native ID through the connection. AWS still needs its own GitHub/GitLab registration and space association. ADP's installation ID is not AWS's `serviceId`. |
| Repository access mode and event support | Every repository association | Existing ADP connection reference; no DevOps registration-mode or GitHub permission-level field (S4) | **Missing provider settings.** Choose GitHub App versus PAT and read-only versus additional writes. PAT has no GitHub webhook events. Private/public repository eligibility and automation defaults must be checked with AWS. |
| Pipeline/workflow and deployed revision | Deployment correlation or release testing | `configuration.advanced.pipelines`, `.deployments`, `.deployment_strategy`, `.release_strategy`; GitHub pipeline binding with `repository_id`, workflow native ID and `source_revision` (S2, S4, S5) | **Reference + bounded workflow discovery.** An observed workflow/repository `updated_at` is not a deployed commit SHA or successful release. Resolve actual deployment/run evidence and configure provider linkage separately. |
| CloudWatch and third-party telemetry | Useful incident diagnosis | `configuration.advanced.observability`; authorized AWS environment metadata (S2, S5) | **Reference/partial discovery.** No generic typed log-group, metric, trace, provider endpoint or API-token mapping. Verify CloudWatch permissions/data; register/associate external providers and resolve their secret references. |
| Ticketing, paging and collaboration | Incident routing and updates | `configuration.advanced.support`, `configuration.basic.collaboration`, `configuration.governance.support_channel_ref`; source-binding kinds include `support` and `communication` (S2, S4) | **Reference.** Source-kind support is not proof of an implemented ServiceNow/Slack/etc. connector. Choose actual service, channel/queue, callback behavior and posting authority in the provider configuration. |
| Architecture, product documents and runbooks | Evidence-backed diagnosis and operational knowledge | `configuration.basic.architecture`, `.documents`, `.decisions`; `configuration.advanced.runbooks`, `.test_strategy`, `.security_strategy` (S2) | **Reference.** Resolve entitled content and revision, transform it into useful instructions/skills or expose it through an authorized source. No automatic AWS knowledge publication is implemented by these fields. |
| Standing agent instructions | Product-specific behavior across sessions | Purpose plus `configuration.advanced.agent_policy_ref`, `configuration.governance.execution_policy_ref`, `.criticality`, `.data_classification` (S1, S2) | **Captured/reference.** Generate a reviewable instruction document and publish to the selected global/agent scope. Store provider asset/version and source provenance with L5. Instructions do not replace IAM or execution policy enforcement. |
| Specialized skills | Targeted investigation procedures | `configuration.advanced.skills`, `.runbooks` (S2) | **Reference.** Missing transformation, provider asset identity, agent-type scope, activation and synchronization policy. AWS learning may create new skills; do not automatically treat those as Profile facts. |
| Custom MCP/A2A or private tool access | Optional external knowledge/tools or ADP interoperability | Source/policy references may describe intent; no dedicated DevOps endpoint/auth/tool-allowlist/private-connection contract (S2) | **Missing provider settings.** Record connection direction, endpoint, authentication reference, selected tools, network attachment and readiness in the service configuration. ADP Knowledge and DevOps knowledge are not implicitly connected. |

## Automation, execution and lifecycle

| DevOps configuration requirement | When required | What Product Profile has | Coverage and remaining configuration |
| --- | --- | --- | --- |
| Capability selection and handoff link | Every ADP-managed DevOps configuration | `configuration.selected_capabilities[].capability`, `.ownership_mode`, `.configuration_ref`, `.reason` (S2) | **Captured selection/reference.** Modes are `managed`, `integrated`, `observed`, `not_applicable`. The referenced record belongs to L5. The current peer handoff returns `integration_deferred` (S1, S9); selecting a capability does not execute setup. |
| Agent Space, service and association IDs | Every configured native integration | No dedicated Profile fields; `configuration_ref` can point to an owning L5 record (S2, S9) | **Missing provider state.** L5 owns space/account/Region, native provider and association IDs, desired/effective configuration, verification time and lifecycle status. Reuse existing registered providers only when scope matches. |
| Alarm/webhook routing, severity filters and maintenance exclusions | Automatic incidents | Environment labels, support/policy/runbook references; no typed incident-trigger policy (S2, S4) | **Missing service settings.** Configure actual source/event schema, destination space, authentication reference, filters, correlation/deduplication and pause behavior. Store one-time webhook secrets outside Profile. |
| Automatic PR review and verification testing | Release automation | Repository/pipeline refs and strategy refs; no branch/event filters or provider verification environment contract (S2) | **Missing service settings.** Explicitly set automation flags, repository eligibility, target branches, environment/network and test-credential references. A repository association must not accidentally enable paid work through provider defaults. |
| Recurring prevention/custom work | Scheduled native operations | No dedicated DevOps schedule/cadence field (S2) | **Missing service settings.** Record enabled/paused state and cadence. Inspect native defaults, including weekly prevention; don't equate `ListTriggers` with all managed-agent schedules. |
| Budget, execution permission and limits | Provider work that can incur cost | `configuration.financials.allocations`, `.budget_policy_ref`; `configuration.governance.execution_policy_ref`; project-owned policy refs (S2, S3) | **Captured/reference.** Allocations are proposed financial context, not enforced grants. Resolve existing budget/execution policy. Native UI and provider-triggered work can bypass ADP admission; usage monitoring is not a hard spend cap. |
| Status, findings, topology, memories and learned skills | Operation and later context enrichment | Profile has `provenance`, revision/change reads and source refs, but no automatic DevOps result sink (S1, S2) | **Missing adapter.** Preserve native investigation/execution/asset IDs and versions in L5; publish selected results to Context through its authorized interface. AWS EventBridge currently covers investigations/mitigations, not all custom or prevention work. |
| Configuration refresh and drift | Continuing operation | Profile `revision`, `provenance`, immutable revision/change APIs; binding generation, revision, health and timestamps (S1, S3, S4) | **Captured source evidence.** L5 still needs reconciliation, override preservation, credential rotation handling and native readback. Reauthorization is required; an old healthy observation is not a continuing permission grant. |

## Implementation findings that affect the mapping

### 1. Environment references have two inconsistent locations

The typed `ProjectConfiguration.Advanced` model defines `environments`; `Basic`
does not, and its configuration forbids extra fields (S2). However, the current
AWS environment-binding write path appends references to
`configuration.basic.environments` (S4, the `basic["environments"]` assignment).

This is a **source-observed implementation mismatch**, not a recommended second
field. A consumer reading only `advanced.environments` can miss a saved selection;
a full typed configuration update may reject the unexpected basic field. Those
failure modes were not reproduced against a live target as part of this review.
Until normalization is implemented, resolve AWS selections through the authorized
binding API and its `verified_environment` projection. A fix should normalize the
write path, migrate existing values, preserve source provenance, and test
create/read/update across UI, CLI and API. Do not silently discard either location.

### 2. AWS verification is narrower than operational readiness

`aws_environment` verifies a version-bound connection and exposes account,
configured Region, role reference and verification timestamp. It explicitly returns
`permission_tier: unknown` (S5). It does not certify DevOps Agent trust, EKS access,
Resource Explorer coverage, log access or all deployed Regions. Those require
provider-specific setup and evidence.

### 3. References exceed today's discovery coverage

Profile can store references for a broad set of sources. Current connection
projection covers GitHub installations plus GitHub/GitLab/AWS credentials;
implemented discovery includes repositories, GitHub workflows, AWS environment
metadata and selected stored planning/document references (S5). This is not
full support for every telemetry, ticketing, knowledge or private MCP provider.

### 4. Product-to-L5 dispatch and worker authority remain gaps

`peer_availability()` reports `integration_deferred`; `requestProductPeer` returns
503 without dispatch (S1). The current Product member resolver requires a human
workspace identity (S6). An autonomous L5 worker must use an approved delegated
Product API path; a role session or owner ID in a payload is not that delegation.
The [shared L5 design](../../architecture.md) describes the intended boundary,
but is initially Security-focused and does not implement a DevOps adapter.

### 5. Saved profile progress is not provider readiness

Profile progress describes applicable onboarding checks. An Idea can be fully
saved with no AWS account or repository. Track DevOps readiness separately for
sign-in, resource access, code/telemetry, knowledge, triggers and result readback.
Evaluate the selected Product's saved configuration and binding health in step 1
of the manual before prepopulating anything.

## Recommended ownership and implementation order

Keep Product Profile as the common product/source authority. Do not add native
AWS space IDs, webhooks, IAM secrets, incident runs or generated knowledge as
unstructured fields in its configuration JSON.

| Owner | Configuration it should own |
| --- | --- |
| Product/Profile | Identity, team, purpose, source selection, environment labels, policy references, revisions and provenance. |
| Existing connections/credential service | Provider credentials, secret versions, current source entitlement and delegated connection use. |
| L5 DevOps service | Profile reference and inherited values, explicit overrides, space/account/Region binding, native service associations, access/auth choices, knowledge publication policy, triggers, verification and lifecycle. |
| AWS DevOps Agent | Native topology, investigations, executions, recommendations, memories/skills and their native identifiers/versions. |
| Product Context | Authorized, sourced interpretation of selected provider outputs; not an automatic overwrite of Profile. |

Recommended next implementation increments, all **proposed**:

1. Normalize environment references and establish the scoped Product/binding read
   contract, including delegated worker access and the existing Profile handoff.
2. Implement a DevOps configuration adapter that proposes values from Profile,
   identifies unresolved choices and records native space/association state. Keep
   secrets in the existing connection service and preserve explicit overrides.
3. Implement provider verification per enabled capability, including AWS/EKS,
   GitHub authorization/automation settings, telemetry and human access.
4. Add versioned knowledge publication and native trigger/status/usage integration.
   Preserve source provenance and distinguish investigation events from other work.
5. Publish selected results through Product Context's contract and test lifecycle
   changes: membership removal, source revocation, rotation, drift and retirement.

These extend the [L5 configuration/binding design](../../architecture.md#configuration-and-profile-handoff).
They do not assert that the current Security contract already supports every
DevOps option or that the configuration API has been deployed.

## Sources inspected

| ID | Repository source | What was verified |
| --- | --- | --- |
| S1 | [Product routes](../../../../modules/gateway/src/products/routes.py) | `profile`, `getProfile`, `getRevision`, `listChanges`, `updateProject`, peer availability and deferred dispatch. |
| S2 | [Typed configuration](../../../../modules/gateway/src/products/configuration.py) | Actual fields, reference shape, strict validation, financial/governance/capability models. |
| S3 | [Product models](../../../../modules/gateway/src/products/models.py) | Product/Project ownership, bindings, configuration storage, provenance, revisions and operations. |
| S4 | [Source routes](../../../../modules/gateway/src/products/source_routes.py) | Binding input/view, AWS environment selection and persistence, connection authorization/freshness and source APIs. |
| S5 | [Source discovery](../../../../modules/gateway/src/product_onboarding/source_discovery.py) and [repository verification](../../../../modules/gateway/src/products/verification.py) | Supported connection candidates, AWS metadata, workflow discovery and metadata-revision limits. |
| S6 | [Product scope resolver](../../../../modules/gateway/src/shared/identity/product_scope.py) and [People API in Product routes](../../../../modules/gateway/src/products/routes.py) | Current owner/team membership and human-only scope resolution. |
| S7 | [Installed Product CLI](../../../../modules/gateway/cli/adp-product.py) and [frontend client](../../../../modules/gateway/frontend/src/services/products.ts) | Actual read/configuration routes and CLI subcommands. |
| S8 | [Profile architecture][profile-design] and [typed data model][profile-model] | Intended boundaries; design statements were not treated as proof of runtime support. |
| S9 | [Profile delivery plan](../../../product-onboarding/delivery/README.md) and [L5 architecture](../../architecture.md) | Deferred handoff story #8266, current unavailable-state behavior and peer ownership. |

AWS-side requirements are sourced in the corresponding steps of the
[manual](README.md), especially account access, GitHub, EKS, telemetry, operator
authentication, instructions/skills, webhook and EventBridge guides.

[profile-design]: ../../../product-onboarding/design/architecture.md
[profile-model]: ../../../product-onboarding/design/data-model.md
[context-design]: ../../../product-context/aws-agent-capabilities.md
