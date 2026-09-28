# AI Superplane on ADP: overarching integration design

Status: reviewer-agreed architecture; product-owner decisions pending.
Updated: 2026-09-27 with the permissions design in sections 4.1–4.6; other
architecture and historical implementation observations retain their review dates.
Tracking: [EPIC #4904](https://github.com/aws-e/adp/issues/4904).
Review baseline: [agreed design snapshot](https://github.com/aws-e/adp/issues/4904#issuecomment-5621154807).

This document describes the target design, not a claim that its APIs and
integrations already exist. It does not authorize deployment, credential
migration or infrastructure spending. Source revisions are listed at the end.
The agreement settles architectural boundaries, not production readiness or
validated capacity for 100 concurrent users. This revision replaces the earlier
domain-local Jobs recommendation. Earlier planning drafts must be reconciled
with section 15 before authoring the remaining EPICs and child stories.

Reading guide: start with [the proposal](#1-the-proposal-in-plain-terms) and
[architecture](#2-architecture-and-responsibility-boundaries). Implementation
details are grouped into [databases](#5-database-and-record-ownership),
[neocloud vault](#6-neocloud-vault-extend-adp-do-not-create-a-second-vault),
[permissions](#41-permissions-outcome-and-implementation-status),
[APIs](#7-api-contracts), [CLI](#10-cli-and-agent-tools), and
[rollout](#12-migration-and-rollout). The final sections identify gaps, tests
and proposed child stories.

## 1. The proposal in plain terms

AI Superplane connects research intent to infrastructure execution and
verifiable results. Researchers describe the work and its constraints; agents
coordinate the experiment and find suitable, authorized GPU capacity across
AWS Regions, on-premises infrastructure and neocloud providers.

For example:

> Compare this model's baseline and quantized configurations on our dataset.
> Preserve our quality threshold, measure latency, and stay within the approved
> budget. Use any eligible connected capacity. Track results in MLflow and ask
> before deploying a qualifying model.

The researcher should not have to select a cloud or configure clusters. They
still control the scientific question and material changes to the experiment.

**Install AI Superplane as an optional ADP domain app. Reuse ADP's identity,
agents and multi-tenant vault; retain Superplane's specialized capacity and
workload-management capabilities.** GitHub, an ADP page, local assistants and
the CLI should all use the same APIs and durable records.

The first milestone is one question, one baseline, two variants, tracked runs,
evidence-backed comparison and an approved next step. It includes MLflow and
safe provider credentials. It does not require a new research IDE, arbitrary
workflow graphs, unrestricted autonomous research or an OpenResearch deployment.

## 2. Architecture and responsibility boundaries

The application **control plane** makes decisions and records progress.
**Data planes** execute workloads. These are distinct from EKS's AWS-managed
Kubernetes control plane.

```text
Researcher: GitHub / ADP interface / local assistant / ADP CLI
                             |
                   Existing ADP authentication
                             |
                  ADP management EKS cluster
  +-------------------------------------------------------------+
  | ADP agents + Gateway + identity + multi-tenant vault          |
  | Shared harness Jobs + HITL/policy + action provenance        |
  |                                                             |
  | AI Superplane domain app                                    |
  |   API + research records + shared-job projections            |
  |   capacity planner + workspace factory + fleet monitor       |
  |   isolated provider executors + MLflow integration           |
  +-------------------------------------------------------------+
        |                    |                       |
   AWS workspace       On-prem workspace       Neocloud backend
   EKS + EC2 GPUs      EKS Hybrid Nodes        SkyPilot/provider API
   regional clusters   local GPU fabric       or separate Kubernetes
        |                    |                       |
        +----------- workload execution -------------+
                             |
                MLflow + authorized artifact storage

ADP vault: metadata in ADP PostgreSQL; values in AWS Secrets Manager
Jobs/approvals: shared ADP-owned stores; not domain-owned state machines
Domain state: separate Superplane database; no copied secret values
```

ADP research/operations agents run centrally. Experiments and model services run
in workspace data planes. Agents that are themselves part of a simulation are
workload processes and belong in the data plane. Job deadlines, recovery and
cleanup must work even when the orchestration agent has exited.

| Environment | Execution approach | Boundary |
|---|---|---|
| AWS Regions | Regional workspace EKS clusters with normal EC2 GPU nodes; other supported provider execution where appropriate | Do not use EKS Hybrid Nodes for EC2 instances. |
| On-premises GPUs | EKS Hybrid Nodes in a workspace cluster | Reliable private connectivity and a validated OS/CNI/GPU stack are required. |
| Neoclouds | Provider/SkyPilot adapters or separate provider Kubernetes environments | AWS does not support EKS Hybrid Nodes on other clouds. |
| Existing lab schedulers | Future adapters for existing Kubernetes, Slurm or Ray where needed | Do not require wholesale scheduler replacement. |

Never join tenant GPU nodes to the ADP management cluster. Prefer dedicated
workspace clusters/accounts for strong isolation; namespace sharing is an
explicit policy choice, not an equivalent security boundary.

"Capacity anywhere" means configured, supported, authorized capacity. A catalogue
entry is not a reservation. A shared API does not create a high-speed GPU fabric;
keep tightly coupled training workers together on suitable local interconnects.

| Owner | Responsibilities |
|---|---|
| ADP Gateway / vault | Login, canonical user/org/team identity, service authentication, exact credential authorization/binding, trusted executor delivery and rotation/revocation integration. ADP also supplies the agent runtime and LLM gateway. |
| Shared harness Jobs (`modules/harness/jobs/`) | One execution authority: job/attempt IDs and state, idempotency, admission ordering, dispatch/outbox, leases/fencing, recovery and cancellation. |
| Shared human-in-the-loop (HITL) service + policy | Approval validity, current approver authority and permission to admit an action. Superplane supplies domain/workspace policy; shared consumers enforce it. |
| AI Superplane | Research plans, hypotheses and lineage, provider capabilities, placement, allocation inventory, budget ledger, scientific conclusions and domain evidence references. Provider adapters carry out harness-authorized operations. |
| Existing ADP provenance | Action lineage connecting requests, actors and operations; not a full artifact store or an authorization record. |
| Workspace controllers | Reconcile local resources and report observations for their assigned workspace. |
| MLflow | Parameters, measurements, model artifacts, comparisons and registry. |
| Researcher/approver | Scientific decisions and approval of spending, policy changes and deployments. |

The reviewed harness documentation marks generic Jobs/Artifacts as not started.
Build the minimal durable Jobs service in `modules/harness/jobs/`, with Superplane
as its first consumer; reuse compatible shared work if it has since landed.
Do not build a domain-local queue/state machine to promote later. The Superplane
run API is a facade: it combines harness execution state with domain research,
cost and evidence records. Existing artifact utilities and action provenance do
not establish a general artifact graph; domain evidence links suffice initially.

## 3. Component migration and repository organization

Preserve the agreed incremental approach: ADP owns the integration and domain
experience; existing Superplane services and Terraform are pinned dependencies
initially. Do not copy the entire research repository or deploy from floating
branches. A later source transfer can consolidate ownership service by service.

| Existing component | Proposed action |
|---|---|
| `config/personas/`, `src/superplane-skill/` | Port selected personas/skills into the ADP domain app; use ADP runtime and identity. |
| Superplane FastAPI API | Keep an independently deployed domain service; add ADP auth and versioned domain APIs. Build from pinned AISuperPlane source initially. |
| Go Superplane controller | Retain in supported workspace clusters; narrow permissions and remove unsupported hybrid-cloud onboarding assumptions. |
| Go platform monitor | Run fleet/budget monitoring centrally; report through owned API/event contracts rather than independently writing domain tables. |
| Bootstrap/Terraform/scripts | Wrap from ADP using isolated state, pinned versions and ADP Connect onboarding. |
| SkyPilot | Validate isolated provider execution without neocloud-to-EKS joins in a dedicated spike; isolate credential/config/state by tenant and provider account. See section 8. |
| Python Superplane CLI | Reuse command implementation as an ADP extension; replace independent login and vault paths. |
| Superplane agent gateway | Propose retirement only after ADP Agent Factory parity, migration/rollback gates and product-owner cutover approval. |
| Local JWT minting, API-key login, org/user/SSO management | Propose retirement after identity/domain-data migration and product-owner cutover approval; ADP becomes the identity authority. |
| Credential registry and vault sync | Move secret ownership to ADP; retain provider metadata and explicit workspace bindings only. |
| Portal/research views | Port useful views into ADP without a separate login. |
| Research scanner, alerts and recipes | Preserve useful domain behavior; use ADP agents/LLM gateway and re-authorize automatic actions. |
| Historical model/experiment manifests | Curate versioned examples; do not deploy old experiments as part of platform installation. |

Proposed ADP layout:

```text
modules/domain-apps/superplane/
  README.md
  agent/personas/
  agent/skills/
  tools/superplane-mcp/        # thin API tools
  cli/                        # packaging/auth adapter for domain CLI
  contracts/                  # domain OpenAPI/events; references shared Jobs/HITL
  integrations/mlflow/
  ui/                         # mounted within ADP
  events/
  infra/control-plane/        # wrapper; separate Terraform state
  infra/workspaces/           # bootstrap and backend profiles
  releases/superplane.lock.yaml # revisions, image digests, schema compatibility
  tests/acceptance/
```

Shared Jobs code, contracts, migrations and tests live in `modules/harness/jobs/`;
first-consumer HITL enforcement remains a shared harness/platform responsibility.
Vault changes stay in `modules/gateway/`; shared auth utilities go in
`libs/python/adp-common`; core CLI dispatch remains in `modules/gateway/cli/`.
New domain server routes/migrations initially live beside the Superplane API in
AISuperPlane, not shared Jobs/approval tables. ADP installs and tests the pinned
release. If source is transferred later, move its tests, CI and ownership too;
do not maintain writable duplicates.

Record these paths in SP-01. `modules/domain-apps/superplane/` follows the user's
plan and deployed cyber-app convention; the documented `apps/` target layout is
not a reason to relocate unrelated apps or put harness concerns inside this one.

## 4. Identity and authorization

Users sign in once through ADP. The domain API validates ADP access tokens,
resolves the canonical principal and checks workspace membership. Authentication
answers who is calling; authorization answers what they may do to this resource.
Same-organization membership alone is insufficient.

Required rules:

1. Require access tokens, expected issuer and allowed clients; validate
   audience/resource binding where applicable. The reviewed shared validator
   accepts ID tokens and defaults to no client allowlist, so importing it alone
   does not enforce this stricter domain policy.
2. Derive tenant identity from verified context, never request-body `org_id`.
   Treat ADP IDs as opaque strings, not assumed legacy UUIDs.
3. Check workspace and provider-connection permission on admission, sensitive
   operations and credential renewal.
4. Separate service identity from delegated authority. M2M authentication does
   not grant access to every user's vault. Bind operations to a server-held
   invocation/run authorization record.
5. Use ADP's registered workload identity/SigV4 internal path. Strip client
   identity headers at ingress and trust only the protected authorizer path.
   Do not add another shared internal token.
6. Extend invocation binding for CLI/UI runs; do not fabricate GitHub webhooks
   or accept caller-supplied user IDs as authority.
7. Bind approval to an immutable plan version/hash, workspace, input manifest,
   constraints and expiry. Changes outside approved bounds require fresh
   authorization. Agents cannot approve their own changes.

Use explicit scoped domain permissions. Display roles such as developer,
workspace-admin and org-admin are not permission identifiers or verified ADP
role names. Sections 4.1–4.6 define the permissions design and separate current
implementation from the remaining role-integration work.

### 4.1 Permissions outcome and implementation status

A user must be able to answer: “What can I do in this organization and workspace,
who granted that access, and why is an action unavailable?” An authorized
administrator must be able to assign and revoke access. UI, CLI, API and agent
tools must consume the same server-enforced policy.

Delivery is tracked by [permissions epic #6483](https://github.com/aws-e/adp/issues/6483),
a native child of [integration epic #4910](https://github.com/aws-e/adp/issues/4910).
This section extends the identity boundaries in the maintained
[domain design, section 2.2](../../modules/domain-apps/superplane/DESIGN.md#22-tenant-user-and-principal-alignment).
It does not declare that the permissions epic is implemented.

The source review at `5476e3b14` establishes these foundations:

| Area | Implemented foundation | Remaining delivery |
|---|---|---|
| Workspace policy | Five permissions, explicit implications, server-held scoped grants and an API endpoint inventory. | Complete mapping from authoritative ADP roles and current membership to effective domain access. |
| Role translation | `ADP_ROLE_PERMISSIONS` and `permissions_for_adp_role()` define a table and helper. | No production caller applying that helper was found. Its `workspace_*` names are not established ADP role integration. |
| Initial administrator | Installation verifies the human's current selected-org `org_admin` membership and records organization authority; workspace bootstrap also records an initial workspace administrator grant. | General assignment, membership-change synchronization and revocation are separate from bootstrap. Resume must not restore revoked grants. |
| Identity and sharing | Explicit organization bindings, typed principals and scoped grant foundations exist. | Current membership/delegation integration remains in #6127; complete shared-cluster isolation remains in #6048. |
| User experience | Existing workspace/workload surfaces can consume capabilities. | Complete access-management and effective-permission workflows, plus composed live acceptance. |

Source references: [policy](../../modules/domain-apps/superplane/auth/superplane_auth/policy.py),
[served endpoint inventory](../../modules/domain-apps/superplane/src/superplane-api/app/endpoint_inventory.py),
[authorization](../../modules/domain-apps/superplane/src/superplane-api/app/auth.py),
and [installation bootstrap](../../modules/domain-apps/superplane/src/superplane-api/app/installation_bootstrap.py).
These are reviewable implementation references, not evidence of an installed
release or completed security acceptance. Earlier implementation observations
elsewhere in this document retain their original review dates.

### 4.2 Permission vocabulary and resource scopes

Permissions belong to a resource scope, not to a display role or cloud account.
For workspace grants the exact vocabulary is:

| Permission | User-facing authority | Examples and limits |
|---|---|---|
| `workspace:read` | Inspect authorized work | Workspace details, nodes, events, costs, workload status and results. Does not mint cluster credentials or authorize spending. |
| `workspace:spend` | Commit workload budget | Submit batch jobs, create/delete deployments and change workspace quota where inventoried. Still requires applicable budget, approval and operation admission checks. |
| `workspace:provision` | Manage workspace infrastructure and cluster access | Retire/delete a workspace, request kubeconfig and invoke the currently inventoried workload cancellation routes. Organization-scoped creation/adoption needs organization authority as well. |
| `workspace:renew_credential` | Manage provider credential bindings | Register, validate, rotate or disable workspace provider connections. This permission does not make secret values generally readable. |
| `workspace:administer` | Manage workspace authorization | Change authorization records through an authorized administration path; includes the four permissions above. It does not make the holder a cluster or platform administrator. |

Every non-read permission implies read. Spend and provision do not imply each
other; neither independently grants credential renewal. An unrecognized role or
stored permission contributes no authority. Route-specific checks remain
necessary: deleting a deployment currently requires spend, while explicit
cancellation requires provision. The reviewed function matrix must enumerate
these differences rather than infer permissions from HTTP verbs or UI labels.

Organization authority (`organization:read`, `organization:administer`) is
separate from workspace grants. The endpoint inventory also records the action
required by each organization-scoped route; the organization authorization path
must resolve that requirement explicitly. A workspace grant cannot be treated as
an organization-wide grant. Conversely, organization administration does not
implicitly permit spending, kubeconfig access or credential rotation in every
existing workspace. Initial workspace creation may use the documented narrow
organization-administration path; an existing or revoked workspace grant must
never be bypassed as “first workspace” onboarding.

Cluster **use**, **administration** and **observation** are separate authorities
owned by the sharing contract in #6048. Workspace membership in a cluster is an
infrastructure relation, not a user grant. A user may administer workspace A
without administering its shared cluster or reading workspace B. GPU allocation
ownership remains allocation → workspace → exactly one bound data-plane cluster.
Platform management-cluster eligibility requires separate platform authorization;
no domain role creates that eligibility.

### 4.3 ADP roles and domain access presets

ADP owns identities, actual role assignments and organization membership.
Superplane owns scoped domain grants and their interpretation. Do not introduce
another identity directory or treat similarly named roles as equivalent.

| ADP identity or role context | Superplane interpretation |
|---|---|
| `platform_admin` / platform administrator | Platform administration is not an implicit grant over customer workspaces, provider credentials or GPUs. Any domain access must follow the reviewed explicit authorization path. |
| Current selected-org `org_admin` | Eligible for the existing administrator bootstrap path. Ongoing organization authority and access assignment must follow explicit grants and the approved delegation rules, not automatic ownership of every workspace. |
| Other organization members | Organization membership alone grants no workspace access. Resolve effective access from approved role mapping and explicit scoped grants. |
| One human belonging to multiple organizations | Only the verified selected organization applies. Never union memberships or permissions across tenants. |
| Service/agent principal | Requires its own scoped grant and operation/run delegation. It does not inherit the initiating human's role or become a human approver. |
| Unknown role or ambiguous identity mapping | Deny the authority that cannot be established; do not guess from email, display name, team name or cloud account. |

The existing helper contains the following **candidate domain access presets**.
This documents its current contents, not a decision to add these roles to ADP:

| Name currently in helper | Expanded workspace permissions |
|---|---|
| `workspace_viewer` | read |
| `workspace_operator` | read, spend |
| `workspace_provisioner` | read, provision, renew_credential |
| `workspace_owner` | read, spend, provision, renew_credential, administer |

[P1 #6484](https://github.com/aws-e/adp/issues/6484) must inventory actual ADP role
sources and decide whether these names become supported domain presets or are
replaced by a mapping from canonical ADP roles. Its versioned function matrix
must specify every supported action, route/tool, resource scope, required grant,
additional admission conditions and evidence owner. It must also settle who may
assign each privilege, last-administrator recovery and the revocation bound.
Until those decisions and production integration land, do not present this table
as working ADP RBAC or automatically create these roles during platform deployment.

### 4.4 Effective access, assignment and revocation

The target authorization path is:

1. Verify the access credential and principal type. Resolve the immutable ADP
   subject, current selected-org membership and explicit domain organization
   binding; a local user projection is not membership evidence.
2. Resolve the requested organization, workspace, cluster and operation from
   trusted records. Load current grants and expand only documented implications.
3. Check the action's exact scope and permission. For service calls, also check
   the bounded run/operation delegation. For mutations, separately check current
   approval, budget, placement, credential binding and execution fencing.
4. Revalidate at admission, credential renewal/delivery and execution boundaries.
   A preview, cached capability list or earlier approval is not durable authority.
5. Record a scoped allow/deny decision with a useful reason and correlation to
   the actor, run and operation, without tokens or secret material.

Administration APIs must check the administrator's current authority at commit,
limit assignments to the reviewed delegable scope and prevent cross-org grants
and unauthorized self-escalation. Concurrent changes and retries need explicit
conflict/idempotency behavior. Persist actor, target, scope, before/after access,
reason, timestamp and correlation in access-controlled audit records. Users must
be able to inspect their effective access and grant provenance without seeing
another tenant's assignments.

Revoked grants take precedence over onboarding fallbacks. Membership removal,
disabled principals and role changes must invalidate authority within the bound
specified by P1, including caches and restarted services. Revocation blocks
subsequent admissions and renewals; it is not a claim that already-issued cloud
credentials disappear immediately or that admitted workloads have been cancelled.
Use existing governed cancellation and recovery authority for those cases, retain
ownership evidence, and never resurrect revoked access during rollback or resume.

### 4.5 Agent tools, human approval and executor authority

Ordinary ADP agent workers do not have a platform-account provisioning role.
They request Superplane operations through maintained tools. Dedicated governed
executors perform resource mutations using scoped, operation-bound credentials;
a domain permission must not be implemented by granting broad cloud IAM to the
reasoning worker or allowing generated infrastructure/direct allocator bypasses.

A delegation binds initiating human provenance, service/run identity, organization,
workspace, permitted actions, operation and lifetime. Effective execution requires
both the current scoped grant and valid delegation; an agent cannot widen either
through retries, provider substitution or switching workspaces.

Approval request, human decision and execution are three distinct actions. The
approval endpoints' coarse read gate does not make every reader an approver:
the approval service also validates the selected distinct human and current
eligibility. Approval binds the exact plan/scope/generation, budget and expiry;
it cannot be replayed for another requester or operation, and it does not supply
missing execution permission. Reuse shared approval, admission, fencing and
credential-delivery contracts instead of creating a second approval engine.

### 4.6 User experience, delivery and acceptance

UI, CLI and tool discovery consume server-derived capabilities. Show the selected
organization/workspace, effective access and readable reasons for unavailable
actions. Refresh after access changes or tenant switches. A disabled button is
not enforcement: direct API requests and stale clients must receive the same
authorization decision. Administrators use the supported access workflow;
ordinary users see only access information they are authorized to inspect.

Superplane remains optional. Domain roles, services, migrations and infrastructure
must not become prerequisites for ordinary ADP login, deployment, navigation or
agent use when Superplane is absent or disabled. Keep domain policy in the app;
necessary shared interfaces require separately scoped review by their owners.

| Story | Deliverable | Order / existing ownership |
|---|---|---|
| [#6484](https://github.com/aws-e/adp/issues/6484) | Approved ADP role/function/scope contract | First; coordinate identity #6127 and sharing #6048. |
| [#6485](https://github.com/aws-e/adp/issues/6485) | Production role-to-access integration | After #6484; consume #6127 identity/current-membership implementation. |
| [#6486](https://github.com/aws-e/adp/issues/6486) | Safe assignment, revocation and audit | After #6485; consume cluster authority interfaces from #6048. |
| [#6487](https://github.com/aws-e/adp/issues/6487) | Effective access and administration workflows | After #6486; coordinate existing UI/CLI/tool owners. |
| [#6488](https://github.com/aws-e/adp/issues/6488) | Bounded agent delegation and approval integration | After #6485; may overlap #6486/#6487; reuse #5526/#5527/#5528. |
| [#6489](https://github.com/aws-e/adp/issues/6489) | Live permission matrix acceptance | After all five and required identity/sharing/composition/security prerequisites. |

Existing stories retain their ownership and acceptance: #6127 owns identity and
current grants, #6048 shared-cluster enforcement, and #5535 production composition.
The missing policy evidence under #5044 and enforcement/credential repairs under
#5055/#5386 and #5046/#5462 are not waived by this new epic.

Remote contract/integration tests must demonstrate permitted operations as well
as denials: distinct read/spend/provision/credential/admin access; tenant switching;
same-org disjoint grants; human/service substitution; stale approvals; revocation
between stages; concurrent assignment; restart/upgrade preservation; and direct
endpoint bypass attempts. Use real composed authorization/database paths with
provider fixtures, without spending as part of default tests.

Live acceptance requires an explicitly authorized target and pinned release,
actual ADP human/service identities, two organizations and disjoint/shared
workspaces. Record the expected-versus-observed function matrix across supported
UI/CLI/API/tools, sanitized audit/operation evidence, absence of unauthorized
side effects, and provider costs/cleanup where applicable. Demonstrate ordinary
ADP flows with Superplane absent or disabled. A code merge or mock-only result
does not establish this outcome. #6483 progresses Planned → Building → Ready for
live test → Live accepted; #6414 retains its separate review and trigger hold.

### Shared HITL approval contract

Reuse `contracts/hitl-ticket/v1`; do not invent a second domain approval system.
At review time this is a schema with **no implemented consumers**. Shared durable
storage, decision handling, timeout and replay protection are first-consumer work
in EPIC B, not capabilities obtained simply by importing the schema.

| Plan approval need | v1 mapping and enforcement |
|---|---|
| Decision class | `scope` is the closed enum `gate-stage`, `tool-use`, `spend`, `destructive-action`. Use `spend` for a cost envelope, `tool-use` for deployment and `destructive-action` for decommission. A plan hash is not a scope. |
| Exact plan/input binding | Extensible `context` carries `plan_id`, `plan_hash`, `workspace_id`, `input_manifest_id`; the immutable plan includes the approved spend/runtime envelope and expiry. |
| Eligible approvers | `approvers.mode = named`; Superplane workspace policy resolves membership/roles to principal IDs in `identities`. Routine research does not automatically require an org admin. |
| Current authority | Shared consumer verifies authenticated `answered_by`, membership in `identities` and current authority at decision time; admission revalidates applicable domain/workspace authorization. Generic Jobs entry points cannot bypass these checks. |
| One-time permission | `allowed-once` is the only permitting ticket result and is bound/consumed for one admission. Declared retries remain within that admission's bounds; they do not replay approval to gain another budget. Timeout, silence and other results fail closed. |

Current admission policy determines whether a human ticket is required; a legacy
approval or agent assertion cannot satisfy it. A persistent authorization, if
needed, is a separately governed policy object, not an invented `allowed-always`
ticket result. Shared approval consumption must be durable and recoverable.

Direct `kubectl` access is optional: broker an authorized workspace role, use a
proper EKS authentication token through an exec credential plugin, and map it
through EKS Access Entries. A raw STS session token is not a Kubernetes bearer
token. Validate TLS against the cluster CA. Prefer brokered workload operations.

## 5. Database and record ownership

### Storage recommendation

Use a separate logical PostgreSQL database for AI Superplane, with its own role,
migrations and connection limits. Initially it may share ADP's managed PostgreSQL
instance after compatibility/capacity checks. That saves overhead but does not
isolate instance outages or noisy queries; a separate production instance remains
an option. Do not move a working existing database merely to co-locate servers.

Keep ADP identity/vault data and shared Jobs/HITL state under their ADP owners.
The Jobs story must name its storage/transaction boundary; sharing a PostgreSQL
server does not make separate logical databases one SQL transaction. Superplane
calls shared services, not their private tables. Cross-database references are
opaque API-validated IDs, not SQL foreign keys. Store large snapshots/logs in
object storage and metadata in PostgreSQL. MLflow keeps its own tracking
backend/artifact store.

The following are domain-owned records, not replacements for shared job or
approval storage:

| Logical record | Purpose |
|---|---|
| `organization_settings` | ADP org ID plus Superplane quotas, allowed providers and preferences; not another identity directory. |
| `workspaces`, `workspace_memberships` | Tenant, isolation mode, permitted principals/roles and lifecycle. |
| `execution_targets` | Provider/region/site/cluster, capabilities, connectivity and health; several targets per workspace. |
| `provider_connections` | Provider/account metadata and ADP credential reference; no secret values or copied secret ARN. |
| `workspace_provider_bindings` | Explicit workspace authorization, allowed operations/regions and approval metadata. |
| `research_findings`, `research_proposals` | Existing scanner findings/proposals, source references and proposal-to-project mapping; preserve legacy approval history without treating it as execution authority. |
| `research_projects`, `experiments` | Question/criteria/constraints; baseline/variant hypothesis, parent and versioned configuration. |
| `plans` | Immutable plan/hash, actions, input manifest, cost/runtime envelope and references to shared HITL tickets/decisions; no domain-owned approval authority. |
| `runs` | Research-facing links to harness job/attempt IDs, plan/input manifest and MLflow run; execution fields are read-only projections of the harness, not a second lifecycle. |
| `resource_allocations` | Provider/controller IDs, owner, lease/deadline and cleanup/retention state. |
| `budget_reservations`, `cost_entries` | Admitted commitments, estimates and reconciled actual charges with source/time. |
| `evidence_references`, `deployments` | Evidence locators/digests linked to action provenance; approved model/config, target, controller, endpoint and retention policy. Execution operations reference harness jobs. |

Harness Jobs owns job/attempt state, ordered execution events, dispatch outbox,
leases and fencing records. Shared HITL owns tickets, decisions and one-time
consumption. Cached domain views retain source IDs/versions and can be rebuilt;
they cannot dispatch work or authorize spending independently.

These are proposed logical names, to reconcile with existing models, not a
requirement to create a new table for every concept. Reuse existing workspace,
cost and deployment records where their contracts fit. Every
tenant-owned row carries `org_id`; workspace-owned rows also carry `workspace_id`.
Use scoped queries, composite constraints and indexes for tenant/workspace/state/
time access. Add row-level security where supported by the connection design;
it supplements authorization rather than replacing it.

Use a tenant/workspace-scoped unique idempotency key plus request hash. Harness
durably assigns job/attempt IDs and orders admission as `reserve → confirm →
enqueue`, calling the domain budget ledger through an idempotent reservation
hook keyed by `(job_id, attempt)`. After confirmation, job admission and dispatch
outbox commit atomically **in the harness store**. Do not claim one transaction
with the domain ledger. Recover incomplete steps through reconciliation using
the release/fencing rules in section 8; no dispatch before confirmed reservation.

One experiment has many execution attempts. Inputs are immutable references to
source, container, model, dataset and evaluation protocol. A retry does not
overwrite prior evidence or grant another approved envelope. Execution status,
scientific conclusion and resource cleanup are separate states. Each service
owns its own schema/writes; controllers and monitors submit observations through
versioned contracts rather than updating either service's tables directly.

### Research lineage and safe draft creation

Preserve the existing path:

`research_finding → research_proposal → research_project → experiments → harness jobs`

Keep original references and make proposal-to-project creation idempotent. A
human can also create a project directly. Preserve the scanner capability, but
automated scanning is optional for milestone 1. Importing/linking a proposal
creates a non-executing draft under normal authenticated workspace write
authorization; it does not require a spending ticket. Starting agents, paid
model calls or infrastructure work is a separate operation governed by current
admission policy and fresh HITL approval where required.

### Preserve existing data

Superplane's organization table also holds quotas, allowed clouds, defaults and
billing information. Move those before retiring identity tables. Build an
operator-reviewed old-ID-to-ADP-ID mapping; do not infer identity from matching
names/emails. Preserve cost history, resource inventory and audit provenance.
Use expand/backfill/validate/contract migrations with backups and a recovery
period, not immediate table deletion.

The reviewed proposal approval route accepts body-supplied `approved_by` without
the required workspace authorization checks. Compatibility routes must derive
identity server-side and enforce current policy, not preserve that bypass.
Mark imported approvals, for example `legacy_approval=true`; they record past
intent, never a new spending grant. Explicitly map legacy records to authorized
workspaces or quarantine them, including null/unscoped records.

## 6. Neocloud vault: extend ADP, do not create a second vault

The "neocloud vault" is a provider onboarding, permission and delivery layer
on ADP's existing vault. It is not a new secret database or HashiCorp Vault.

### Existing ADP capabilities

| Capability | Current behavior |
|---|---|
| Metadata | `UserCredential` / `user_credentials` stores service/type/label, owner, secret ARN and metadata. |
| Values | `SecretsManagerHelper` stores raw values in AWS Secrets Manager, not PostgreSQL. |
| Ownership | User, team, org and domain-app scopes; workspace is not a native vault owner scope. |
| Management | `/auth/credentials` create/list; `/{id}` patch metadata/delete. Responses omit values and secret ARNs. |
| Resolution | User → team → org → domain-app fallback, with `strict`/`scope_hint` safeguards. |
| Delivery | Internal credential proxy, file materialization and gated raw-read, with service identity/invocation binding mechanisms. |
| Audit | Credential access events and `last_used_at`. |

The service name is free-form. Existing types include API keys, bearer tokens,
SSH keys, certificates, config files and AWS roles. Some providers require JSON
bundles or token exchanges, not a single key; validate the supported format in
each adapter rather than adding an unnecessary storage engine.

### Onboarding and bindings

1. An authorized user/admin registers a credential through a provider-specific
   ADP vault form. Values go directly to the vault endpoint over TLS, with no
   prompt, trace or request-body logging.
2. Create a provider connection referencing the returned credential ID, with
   provider account/project identity, regions and policy metadata.
3. Validate using documented read-only provider operations. Report credential
   validity, permissions, quota and observed capacity separately. A valid key
   does not establish available GPU capacity.
4. Bind the connection to specific workspaces. A workspace admin may delegate
   only credentials they are authorized to use/share, not every org credential.
5. Capacity discovery and execution operate through those bindings. Public
   catalogue data may be shared; account quotas/availability remain tenant-scoped.

Prefer team/org/domain-app credentials for persistent shared infrastructure;
user credentials can support personal experiments. Vault ownership determines
who manages a key. A workspace binding determines where it may be used. Both
checks are required.

For provisioning, resolve the **exact bound credential**, not whichever record
wins fallback resolution. Add a narrowly scoped vault operation checking the
credential ID, tenant, binding, action and delegated principal. Current
`strict`/`scope_hint` behavior is not an exact-binding mechanism: a user context
may skip a strict team key, and a scope hint does not choose a specific record.
Preserve general resolver behavior for unrelated ADP clients.

### Credential delivery

```text
Authorized run / workspace operation
  -> registered executor identity + operation reference
  -> server-resolved principal and exact workspace/credential binding
  -> policy checks provider, account, action and credential status
  -> approved provider proxy OR narrowly scoped executor delivery
  -> redacted operation result + audit event
```

Use the existing proxy where the provider's protocol/authentication permits it.
Register and enforce service-to-host bindings, HTTPS and redirect/destination
rules. Never inject a key into an arbitrary agent-supplied URL. The current
implementation has rollout/shadow modes and unmapped services, so fail-closed
provider binding is an implementation requirement, not an existing guarantee.

For SDKs/SkyPilot requiring credential files, extend ADP with a trusted executor
delivery path, not a general agent raw-read command:

- Authenticate the executor and bind it to an active run, allocation or workspace
  operation. Never trust body-supplied user/org IDs as authority.
- Deliver only the selected credential through a recipient-bound, short-lived
  channel with audit and bounded renewal.
- Materialize in an executor-only restricted temporary/in-memory filesystem and
  remove it after use. Never return it in a model-visible tool result.
- Isolate SDK configuration, credentials, caches and backend state by tenant/
  workspace/provider account. Do not share one SkyPilot home across tenants.
- Keep provider-management keys out of training/inference containers. Dataset
  and MLflow access use separate, least-privileged workload credentials.

Current file materialization returns a presigned URL for file-type credentials;
it is not a generic API-key lease system. Do not relabel keys as files or enable
broad raw-read to bypass restrictions. Implement and test the missing delivery
contract. A delivery lease does not make a provider's long-lived key expire.

ADP remains the Secrets Manager reader. Agents/controllers must not receive
tenant-wide `GetSecretValue`. Reuse existing KMS controls, assessing whether
customer-managed keys are needed; do not invent custom encryption.

### Rotation, revocation and migration

Vault PATCH currently changes metadata only and rejects secret-value updates.
Rotate by registering a new record with a distinct label/version, validating it,
and atomically switching the connection reference. Record credential versions
used, drain/refresh executor sessions and revoke the old key at the provider
after a safe transition. Do not delete the old key before replacement validation.

Disablement blocks new admissions and renewals. Delivered long-lived credentials
may remain usable until provider revocation/session termination. Surface that
limitation. Check active allocations/deployments before deleting a referenced
credential; cleanup may require an authorized replacement or audited operator
recovery. Secret deletion is not provider-side revocation. Preserve audit history.

Map Superplane's `credential_registry` and `cluster_vault_assignments` to ADP
credential IDs, provider connections and workspace bindings using an audited
vault-owned migration. Do not give Superplane broad read access to ADP secrets.

The reviewed `KubernetesExternalSecretClient` in `vault_sync.py` builds a manifest
and returns success without applying it. Do not treat its `synced` flag as proof
of working delivery. Replace/retire that broad secret-replication pattern and
verify real provider operations, rotation and revocation end to end.

## 7. API contracts

### Public domain API

Proposed base: `/api/superplane/v1` on the existing ADP origin. Route that prefix
to the domain service without changing other ADP routes. Current Superplane
paths include `/workspaces`, `/vault/credentials` and `/api/v1/research`; supply
a temporary compatibility adapter instead of silently breaking clients.

All paths below are proposed, not existing endpoints. Every operation is
tenant/workspace scoped even where the workspace is not repeated in the URL.

| Path relative to proposed base | Purpose |
|---|---|
| `GET/POST /workspaces` | List authorized workspaces or request provisioning. |
| `GET /workspaces/{id}` | Configuration, lifecycle and capabilities. |
| `GET/PUT /workspaces/{id}/memberships` | Inspect/manage membership with separately authorized administrative permissions. |
| `POST /workspaces/{id}/decommission` | Approved, tracked release rather than immediate cascading deletion. |
| `GET/POST /provider-connections` | Safe metadata; create using an ADP credential reference. |
| `POST /provider-connections/{id}/validate` | Read-only validation; asynchronous operation ID. |
| `POST /provider-connections/{id}/rotate` | Switch to a validated replacement credential reference. |
| `POST /provider-connections/{id}/disable` | Block use and initiate defined revocation handling. |
| `GET/POST /workspaces/{id}/provider-bindings` | Inspect/authorize workspace access to a connection. |
| `DELETE /workspaces/{id}/provider-bindings/{binding_id}` | Revoke future use while retaining audit and handling existing allocations explicitly. |
| `POST /workspaces/{id}/capacity/queries` | Structured requirements → eligible observations/prices/rejection reasons; no provisioning. |
| `GET /workspaces/{id}/research-findings` and `/research-proposals` | Authorized existing scanner/proposal records and lineage. |
| `POST /research-proposals/{id}/project` | Idempotently create/link a non-executing draft in an authorized workspace; legacy approval grants no spending authority. |
| `GET/POST /workspaces/{id}/research-projects` | Read/create draft research questions and constraints; no automatic execution. |
| `GET/POST /research-projects/{id}/experiments` | Baseline/variant lineage. |
| `POST /experiments/{id}/plans` | Immutable plan with actions, requirements and cost envelope. |
| `POST /plans/{id}/approvals` | Facade for the shared HITL workflow bound to the exact plan; only the shared consumer can record an authorized decision. |
| `POST /workspaces/{id}/input-manifests` | Finalize validated, immutable source/model/data/environment references; authorize any required staged uploads separately. |
| `POST /experiments/{id}/runs` | Request harness admission against an authorized plan; idempotently link the domain record to the harness-assigned job/attempt. |
| `GET /runs/{id}` | Harness execution projection plus domain evidence, spend and cleanup state; expose `job_id` and `attempt`. |
| `GET /runs/{id}/events` and `/logs` | Authorized cursor-based harness progress/log access; optional event streaming. |
| `POST /runs/{id}/cancel` | Forward cancellation to the harness; provider reconciliation verifies termination and accounting. |
| `GET /runs/{id}/evidence` | Authorized references/download access. |
| `GET /research-projects/{id}/comparison` | Evidence-backed comparison and scientific outcome flags. |
| `POST /workspaces/{id}/deployments` | Deploy an approved model/config version. |
| `POST /deployments/{id}/decommission` | Remove desired state and verify release. |
| `GET /operations/{id}` | Facade over harness-tracked asynchronous onboarding/validation/retirement work, not an independent domain task queue. |
| `GET /workspaces/{id}/cost` and `/budget` | Estimated/reconciled spend and reservations. |

Raw secrets are accepted only by ADP's existing vault management API
(`POST /auth/credentials` at its service boundary, under the configured gateway
base externally). The Superplane API accepts references only:

```json
{
  "name": "research-neocloud-account",
  "provider": "nebius",
  "provider_account_id": "provider-account-reference",
  "vault_credential_id": "adp-credential-reference",
  "allowed_regions": ["approved-provider-region"]
}
```

The server checks ownership/delegation before accepting the reference. Account
IDs are non-secret metadata but still access-controlled.

Illustrative run admission:

```http
POST /api/superplane/v1/experiments/exp-variant-a/runs
Idempotency-Key: experiment-variant-a-attempt-1
Content-Type: application/json

{
  "plan_id": "plan-approved-v3",
  "input_manifest_id": "manifest-immutable-v1"
}
```

```json
{
  "id": "run-123",
  "job_id": "job-456",
  "attempt": 1,
  "state": "queued",
  "scientific_outcome": "not_evaluated",
  "cleanup_state": "not_required",
  "status_url": "/api/superplane/v1/runs/run-123",
  "events_url": "/api/superplane/v1/runs/run-123/events"
}
```

Use `202 Accepted` plus `Location` for asynchronous work; `201` for metadata
created synchronously. A repeated idempotency key with the same body returns
the same resource; a different body returns `409`. Standard errors: `401`
unauthenticated, `403` forbidden, `404` inaccessible/missing as appropriate,
`409` stale state/plan or admission conflict, `422` invalid requirements, `429`
request-rate limit. Include machine-readable error code, request ID and
retryability; never provider secrets. Paginate lists and use versions/ETags for
concurrent edits. Distinguish expired quotes, quota exhaustion, unavailable
capacity and policy denial.

Run requests do not carry raw credentials, arbitrary kubeconfigs or an asserted
org/user identity. Requirements and permissions come from the approved plan and
server-resolved authorization context.

Approval endpoints cannot turn a caller-supplied name, `approved_by` or boolean
into authority. Domain APIs forward to shared HITL/Jobs using verified context.
Any endpoint that starts paid model calls, agents or provisioning must follow
current admission policy, even if its name is `create` or `plan`. Metadata draft
creation and the subsequent start operation remain distinct.

### Internal contracts

Version the shared admission/idempotency, approval binding, budget reservation
(confirm/release/reconcile), dispatch, observation, heartbeat and cleanup-evidence
contracts. Jobs owns ordering and execution state; domain services implement
research policy, budget accounting and provider adapters. Require a verified
executor plus server-resolved job/attempt/workspace authority. Adapters expose
`discover`, `validate`, `reserve/provision`, `submit`, `inspect/logs`, `cancel`
and `release`. Explicitly declare unsupported operations, including reservation,
checkpoint resume or provider-side key revocation. Never report success for stubs.
Provider capacity reservation is distinct from a financial budget reservation;
neither implies the other has succeeded.

The exact-binding/credential-delivery operation extends ADP's existing internal
API. It does not create another independently authenticated vault.

## 8. Execution, placement and budgets

```text
Harness job:
  admission -> queued -> provisioning -> running -> collecting -> succeeded / failed
  cancellation or runtime/budget limit -> stopping -> cancelled / failed

Allocation lifecycle: active -> releasing -> released / cleanup_failed
Scientific outcome: passed / failed / inconclusive / unsupported
```

This is a conceptual lifecycle owned by shared Jobs, not a separate Superplane
state machine. Admission checks current authorization, approval where required,
input/plan agreement, compatible targets and budget headroom. Use the reservation
protocol and harness-local outbox transaction in section 5.

### Recovery and budget release

Leases limit how long a worker owns a job; fencing prevents an expired worker
from continuing to act after another worker takes over. Bounded retries retain
the same admitted plan and aggregate spend/runtime envelope.

- Before dispatch, release a reservation only after a durable, fenced transition
  makes the job ineligible for dispatch. Recovery must not race an enqueuer or
  dispatcher, including after a crash between reservation confirmation and enqueue.
- After dispatch may have happened, timeout, cancellation or lease expiry is not
  proof that provider resources stopped. A lost response means an unknown outcome,
  not "never ran." Reconcile durable provider handles/idempotency identities before
  repeating an operation; do not blindly launch a replacement.
- Keep unresolved cost exposure accounted for. Confirm termination and account
  for incurred cost before releasing the unused reservation. Delayed billing
  requires conservative accrual/reconciliation, not declaring cost to be zero.

The job-side recovery worker coordinates these decisions with the domain ledger
and provider adapter. This does not promise exactly-once provider execution or
a cross-database transaction.

### Placement and supported backends

Placement filters GPU type/memory/count, CPU/RAM, container/model compatibility,
data locality, networking, provider quota, deadline and cost. Rank only eligible
candidates. Cache observations with collection time/expiry and recheck at
allocation. Choosing another already-approved target is transparent. Changing
forbidden locations, scientific settings or spending envelope needs new approval.

After execution starts, relocation is not automatically live migration.
Checkpoint/resume depends on the workload. Otherwise record an interrupted
attempt and a new attempt with explicit cost/provenance. Record hardware changes
and account for them when comparing benchmark results.

### Dedicated neocloud backend spike

The existing "SkyPilot launch → EKS hybrid-node join on neoclouds" path is not
the supported model. Validate this first hypothesis: submit a versioned workload
to an isolated SkyPilot/provider execution target **without joining it to EKS**.
EKS remains the management platform and AWS/on-prem execution option. Compare
native provider Kubernetes only where it materially helps the initial workload;
do not require a new Kubernetes cluster on every provider for visual uniformity.

The spike must demonstrate source/environment staging, trusted credential
delivery, private result access, durable backend handles, status/logs,
timeouts/cancellation, cost observation and verified resource cleanup. Serving
needs additional endpoint authentication/reachability and owning-controller
lifecycle validation; a successful batch job is insufficient evidence.

The spike gates A-neocloud, not independent A-core AWS/on-prem integration.
Product-owner approval selects the supported backend and any cutover/retirement.
Neither reviewer consensus nor this document authorizes live credentials,
spending or infrastructure deletion.

### Ongoing cost and cleanup

Budget enforcement combines admission reservations, conservative rate/runtime
limits, metering, reconciliation and an independent stop/cleanup worker. Include
controllers, storage and transfer, not just GPU price. Billing is delayed: define
shutdown headroom and label estimates versus actuals. ADP's LLM budget and
infrastructure spend remain distinct ledgers; summaries must not double-count.

Cancel the owning SkyServe service/Kubernetes job/provider workload before its
children. Verify desired state and remaining compute/storage/network resources.
Do not erase unresolved allocations or claim cleanup succeeded after losing
credentials. Retained deployments/shared controllers have separate owners and
budgets. On-prem cleanup releases allocations, not customer-owned hardware.

## 9. MLflow and research evidence

Milestone 1 integrates an existing authorized MLflow deployment. Provisioning
and authenticating a new MLflow service is a separate optional story, not part
of the minimal research slice. Reuse ADP credential management if access
requires a secret; the endpoint and delegated authorization model need a
product-owner decision.

Recommended mapping:

- Research project → MLflow experiment (its grouping/container concept).
- Domain hypothesis/variant → metadata/tags relating MLflow runs.
- Harness execution attempt → one MLflow run, tagged with domain project,
  experiment/run and harness job/attempt IDs; retries retain earlier evidence.
- Qualifying artifact → registered model version; registration is not approval
  to deploy it.

Record source/config/container/model/dataset versions, seeds, evaluation protocol,
hardware/region, raw measurements and derived results. Link the workspace, plan,
domain run, harness job/attempt and originating request. MLflow does not own job
authorization, resource allocation or the billing ledger.

### Action provenance versus research evidence

Reuse ADP's action-provenance API and its actor/root-human, `action_kind`,
`source_event`, `correlation_id` and `parent_invocation_id` fields. Versioned
payloads describe actions such as plan creation, approval, job admission, run
completion and deployment approval. This is action lineage, not a general
artifact store or full experiment graph.

Superplane keeps domain `evidence_references` and MLflow IDs, linked to action
lineage by correlation IDs. For immutable artifact bytes or a versioned immutable
manifest, record the hash algorithm and digest alongside its artifact/version
locator. An MLflow run ID or mutable URL is only a locator. If evidence cannot
yet be materialized and hashed, record that limitation explicitly; do not invent
a digest or claim immutability. Hashes assist future shared Artifacts adoption
but do not replace integrity verification when evidence is retrieved.

Durable shared approval/job state is authoritative for permission and execution.
A best-effort provenance write is never spending authorization; failed event
delivery must be retried and reconciled.

Store data/evidence in permitted locations. Keeping GPUs on-prem does not itself
prevent dataset, log or artifact egress. If tracking upload fails, preserve
evidence durably, retry collection and report incomplete evidence. Numerical
comparisons use tested calculations; unsupported treatments and projections are
not measured results. A successfully executed job can disprove its hypothesis.

The initial ADP view needs the question, experiment tree, progress, comparison,
evidence links, spend and approvals. MLflow remains the detailed inspection tool.

## 10. CLI and agent tools

ADP's existing `modules/gateway/cli/adp` is a thin shell wrapper with shared
authentication through `bg-cognito-auth.sh` and installer/update handling.
Superplane's existing Python/Typer CLI already has workspace, deployment, cost,
account and vault commands. Reuse both rather than add a third CLI/login system.

Propose `adp superplane ...`: the existing entry point delegates domain commands
to a packaged, versioned Python extension. Extend installer/download/update and
rollback handling with pinned artifacts. Do not install unpinned packages on
every command invocation. Domain installation remains optional.

Split delivery ownership: EPIC A provides core dispatch/packaging, shared login
and the operational/provider commands; EPIC C adds research commands against
the shared contracts from B. The CLI never owns a queue, approval database or
budget ledger.

Illustrative proposed commands (not implemented by this document):

```sh
adp login
adp superplane workspace list --json
adp superplane provider connect nebius --scope team
adp superplane provider bind CONNECTION_ID --workspace WORKSPACE_ID
adp superplane capacity query --workspace WORKSPACE_ID --file requirements.json
adp superplane project create --workspace WORKSPACE_ID --file research-request.yaml
adp superplane project from-proposal PROPOSAL_ID --workspace WORKSPACE_ID
adp superplane experiment plan EXPERIMENT_ID
adp superplane plan approve PLAN_ID
adp superplane run submit EXPERIMENT_ID --plan PLAN_ID --json
adp superplane run watch RUN_ID
adp superplane run results RUN_ID --json
adp superplane run cancel RUN_ID
```

Provider onboarding uses hidden input or explicit stdin, not `--api-key VALUE`.
Send values directly to ADP's vault API; use returned IDs for domain metadata.
Disable command/body tracing. JSON stdout contains safe machine-readable output,
progress/warnings go to stderr, and errors have stable exit codes.

Project creation/import makes an authorized draft without starting work. Plan
approval uses the shared HITL workflow and current human authority; run submission
uses harness admission and returns linked run/job/attempt IDs. A cancelled CLI
session does not imply remote cancellation or release of provider resources.

Reuse the current token refresh helper through a narrow client adapter. Workspace
selection is convenience, not authorization. Preserve existing ADP login, local
assistant setup, status and update behavior.

Keep `superplane ...` temporarily as a compatibility entry point using the same
API and ADP credentials. Redirect its login to ADP. Deprecate independent
org/user/SSO administration in favor of ADP UI/APIs, and replace its vault commands
with the ADP-backed provider flow.

MCP/CLI are thin API adapters, not schedulers or policy engines. Separate read-only
capacity discovery from spending/mutation tools. Never expose provider secrets
as model-visible tool results. Approvals require approver authority, not merely
an agent's ability to call a tool.

GitHub is the first natural-language entry point, but run identity must be
surface-independent. Existing workers require issue/repo context: change the
invocation envelope deliberately when enabling UI/local submission. Research
agents propose experiments; operations agents execute the agreed specification.

## 11. Deployment, operations and scaling

Run domain services in an isolated management namespace. Separate trusted
provider executors from untrusted research code. Workspace controllers manage
their own Kubernetes environments, not GPU nodes on the ADP management cluster.
Use per-workspace/provider credential, filesystem, network and IAM boundaries.

API-startup reconcilers must move into controlled workers or use leader election/
leases before scaling API replicas. Otherwise replicas can repeat provisioning
or vault operations. APIs enqueue long work instead of holding requests open.
Controllers submit authenticated observations; each owning service controls its
schema and state transitions.

Use ADP Agent Factory for reasoning sessions. Shared harness Jobs owns durable
workload dispatch/reconciliation, distinct from the lifetime of those sessions.
Set global and tenant concurrency limits,
fair scheduling, finite retries and bounded database pools. Agent completion is
not workload completion.

The topology is a reasonable starting point for 100 users, not a proven capacity
claim. Test concurrent submissions, active jobs, log streams, database connections,
credential renewal and provider quotas separately. A 100-client inference test
does not validate 100 concurrent research workflows.

Correlate request/plan/ticket/job/attempt/run/allocation IDs. Monitor queue age,
placement failures, time to start, heartbeats, credential failures, spend,
evidence upload failures and overdue cleanup. Cleanup alerts must reach operators
after the agent exits.

Use separate Terraform state and migration jobs, pinned image digests and a
release compatibility manifest. Reuse ADP VPC/EKS/Cognito. Gateway vault/auth
changes, worker image changes, CLI releases and domain deployments are separate
delivery steps; a domain manifest update does not deploy them all.

## 12. Migration and rollout

These are readiness gates, not a blanket serial ordering of the three EPICs.
Contract and scaffold work in A/B can progress together; C consumes specific
functioning contracts. The neocloud spike gates A-neocloud, not A-core.

| Phase | Work | Exit condition |
|---|---|---|
| 1. Contracts/inventory | Confirm versions, shared ownership/paths and map identities/data/resources; define the neocloud spike. | Jobs/HITL/database/vault boundaries explicit; backend decisions tracked. |
| 2. Additive integration | Scaffold, strict auth policy, Connect onboarding, pinned deployment and CLI packaging. | Existing ADP unaffected; domain remains feature-gated. |
| 3. Vault/execution | Harness Jobs, shared HITL first consumer, domain budget hook, provider bindings/delivery and cleanup reconciliation. | Isolation, key lifecycle, crash/unknown-outcome recovery pass; neocloud additionally passes its spike and backend approval. |
| 4. Research vertical slice | Proposal/draft lineage, baseline/two variants, existing authorized MLflow, comparison and approved deployment. | Real measurements and verified resource release; automated scanning not required. |
| 5. Controlled migration | Backfill identities/settings, migrate credential references and selected workspaces. | Inventories reconcile; exactly one controller owns each resource. |
| 6. Retirement/hardening | Remove duplicate auth/gateway/vault paths only after parity, migration gates and product-owner approval; load/recovery tests. | Legacy callers retired and rollback/runbooks exercised. |

Select a mode per migrated workspace/tenant. Never fall back to legacy auth after
an ADP denial. Do not dual-write secrets or run two provisioning controllers.
SP-07/SP-15 retirement work may be drafted, but must not be implemented before
its parity/migration gates are defined and met and cutover is authorized.

Rollback stops new admissions, preserves evidence/state, restores compatible
releases and reconciles jobs through their existing owner. It must not resurrect
revoked credentials or restart legacy controllers over newly owned resources.
Use expand/backfill/contract migrations and a tested recovery window.

## 13. Known implementation gaps

| Reviewed observation | Required treatment |
|---|---|
| JWT validator accepts ID tokens and an empty client allowlist | Explicit domain access-token/client policy and regression tests. |
| Cognito Terraform uses pre-token trigger `V2_0` | If M2M custom claims are used, configure supported V3 behavior; do not assume user claims on M2M tokens. |
| Credential binding uses webhook records and rollout modes | Enforce provider-operation binding; add non-GitHub run/workspace authority. |
| Resolver can select wider scopes; providers may lack host mappings | Exact credential binding and enforced destination policy. |
| Vault-sync client returns simulated success | Real verified delivery, rotation and revocation, not a status flag. |
| Kubeconfig/proxy token and TLS handling are incorrect | Proper EKS authentication and CA validation before direct access. |
| Shared Jobs is not yet a demonstrated service | Implement/reuse minimal Jobs in `modules/harness/jobs/`; no domain-local lifecycle or "promote later" fallback. |
| HITL v1 has a schema but no implemented consumers | First shared consumer in B: authenticated decisions, current-authority checks, durable storage, timeout and one-time consumption. |
| Existing provenance records actions, not a full artifact graph | Reuse action lineage; retain domain evidence/MLflow links with honest immutable-content hashes. |
| Budget and execution state live under separate owners | Idempotent reservation protocol plus harness-local admission/outbox transaction, fencing and unknown-outcome reconciliation. |
| Existing neocloud path joins provisioned nodes to EKS | Dedicated isolated provider-execution spike without EKS join; serving requires additional validation. |
| Proposal approval accepts body-supplied identity without workspace checks | Server-derived identity, scoped compatibility routes, legacy markers and draft/start separation; no inherited spending grant. |
| Workspace model largely references one cluster | Multiple execution targets without losing authorization boundaries. |
| Startup reconcilers and monitor DB coupling | Explicit leases/ownership and versioned observation contracts. |
| Legacy identity/vault tables contain domain data/history | Migrate instead of blindly dropping. |

## 14. Validation and acceptance

Keep the architectural acceptance invariants here; place executable test matrices
and failure-injection cases in each implementation story's `## Validation` section:

- Preserve existing ADP login, local assistant setup and unrelated agents.
- Enforce tenant/workspace isolation, current approval authority and exact
  credential binding through every entry point; prevent secret disclosure.
- Recover duplicate delivery, crashes and ambiguous provider outcomes without
  blind duplicate provisioning, replayed approval or premature budget release.
- Keep placement, retries and resource retention inside approved limits; make
  unsupported operations explicit. Cancellation verifies controller/resource
  cleanup and accounts for cost even after the agent exits or credentials change.
- Trace scientific conclusions to immutable inputs and real evidence; incomplete
  evidence stays visible. Legacy draft import cannot silently start paid work.
- Rehearse migration, rollback, restore and credential recovery; validate neocloud
  batch execution and serving separately and load-test the intended concurrency.

Unit/contract CI uses mocked providers and no real secrets. Integration tests use
explicit test tenants/sandbox accounts. Live GPU tests need a named account,
provider/location, budget, deadline and cleanup owner; they are not part of an
unbounded default test command.

The re:Invent demonstration is one request, a visible baseline/two variants,
eligible distributed capacity, a controlled capacity failure, MLflow evidence,
a justified recommendation, approved deployment and verified temporary-resource
cleanup. This balances research outcomes with transparent capacity orchestration.

## 15. EPIC mapping and decisions to settle

SP identifiers below are planning IDs, not GitHub issues. The umbrella is
[EPIC #4904](https://github.com/aws-e/adp/issues/4904); the three ownership EPICs
and their child stories remain to be authored. This file records the agreed
architecture for SP-01 and the subsequent implementation stories.

### Three ownership EPICs under one umbrella

The concise umbrella index carries the full product story: research intent to
reproducible results, with policy-constrained capacity discovery and execution.
It links the three EPICs and shared decisions; it is not another agent-run
orchestrator. Use per-story `blocked by` edges rather than a blanket serial
dependency between EPICs.

| EPIC | Scope | Accountable owner |
|---|---|---|
| A — Superplane on ADP: integration | **A-core:** strict shared auth integration, Connect onboarding, personas/skills/MCP, pinned control-plane infrastructure wrapper, agent ScaledJob, alert events, deploy-mgmt checks, AWS-region EKS/on-prem Hybrid Node execution, provider metadata and core CLI packaging. **A-neocloud:** supported provider execution, placement and cleanup, gated on the backend spike. | Domain-app/integration team, coordinating shared auth/CLI changes with ADP owners. |
| B — Harness / gateway foundations | Minimal shared Jobs, admission/budget-hook protocol, leases/fencing/recovery, shared HITL first consumer, exact credential authorization and trusted executor delivery. | ADP harness / gateway owners. |
| C — Research experiment lifecycle + demo | Findings/proposals/projects/experiments, domain run projections and evidence, existing MLflow integration, experiment view, bounded research workflow, research CLI and end-to-end demo. | Research / product team. |

A-core can ship without the neocloud spike. It is not the complete multi-provider
research demo, which also requires A-neocloud and C. Agent ScaledJobs/inbox queues
in A host reasoning sessions; they do not duplicate the workload Jobs lifecycle
owned by B.

### Reconcile the planning IDs by owner

| Planning IDs / work | Revised allocation |
|---|---|
| SP-01–SP-15 | Integration in A with explicit shared-library owners. SP-01 records all service paths/contracts. SP-07/SP-15 are conditional retirement stories with parity, migration, rollback and product-owner approval gates. |
| SP-16 | C: existing proposal-to-project lineage, draft/start separation, research model and harness job/attempt references; no local execution or approval state machine. |
| SP-17 | B: minimal Jobs in `modules/harness/jobs/`, its explicit storage boundary and recovery contract. Domain-specific projections/integration belong in C; do not make the generic service depend on research tables. |
| HITL first-consumer work | B: explicit child story for the v1 mapping, current-authority checks, storage, timeout and one-time consumption; allocate a planning ID during issue authoring. |
| SP-18 | Split into A-core/A-neocloud execution, placement and provider cleanup. C consumes the resulting execution contract; B owns generic admission/recovery. |
| Neocloud backend spike | Dedicated A-neocloud prerequisite with section 8 exit criteria; allocate a planning ID during issue authoring. |
| SP-19–SP-22 | C: existing authorized MLflow, experiment UI, bounded workflow and research/demo acceptance. Provisioning a new MLflow service is separate optional work. |
| SP-23 | Split: A owns provider connections/workspace bindings, validation and domain migration; B owns shared vault exact-binding enforcement. |
| SP-24 | B owns trusted executor delivery and non-GitHub job/operation authority; A owns provider-adapter integration. Split cross-repository tasks explicitly. |
| SP-25 | Split: A owns core CLI dispatch, shared login, pinned packaging/update/rollback and provider operations; C owns research commands. |

Important dependency edges:

- A/B contract and scaffold work can progress together. Generic Jobs can be
  implemented/tested with mock adapters and a mock budget hook before C's model
  is complete; real research admission requires the functioning C/domain hook.
- Provider bindings need SP-01/SP-06 authorization contracts; trusted delivery
  needs those bindings and B's job/operation authority. SP-18 must not use live
  neocloud credentials before SP-23/SP-24 are validated and the live test is
  explicitly authorized.
- C's live runs depend on B's Jobs/HITL contracts and the selected A execution
  adapter. MLflow/view/CLI scaffolds can proceed against mocked contracts.
- A-neocloud execution depends on the spike and backend approval; A-core does not.
  SP-22 multi-provider acceptance depends on all of the specific research,
  credentials and backend capabilities it demonstrates.
- Retirement does not block independent additive work; SP-07/SP-15 cannot be
  implemented until their explicit gates and cutover authorization are satisfied.

Keep orchestrator bodies approximately 1–3 KB with 3–6 stories per wave. Put
rationale/invariants here and detailed tests in child-story `## Validation`
sections. Remove rough effort estimates from EPIC/story bodies; they are not
delivery commitments. Reconcile older planning IDs/waves with this ownership
mapping before issuing work, and split cross-repository tasks rather than
assuming an ADP-only agent can edit AISuperPlane.

### Pending product-owner decisions

These are choices to make, not unresolved reviewer disagreements:

1. Approve the supported backend model (Hybrid Nodes for supported on-premises
   use only) and conditional retirement/cutover of duplicate gateway/auth and
   local org/user surfaces. Initial integration remains pinned and incremental.
2. Choose hosting for the separate Superplane logical database: shared ADP
   instance or separate instance, with isolation/backup/retention/restore targets.
3. Select the existing MLflow endpoint and delegated authorization model,
   including permitted artifact locations and on-prem data-egress rules.
4. Select initial neocloud providers and credential formats, with shared
   delegation and authorized cleanup/recovery after access loss.
5. Name the development account and credential label for authorized delivery
   tests; never put credential values in issue bodies or this document.
6. Choose the demo model/dataset, quality thresholds, regions/providers/sites,
   spend/runtime limits and resource/evidence retention policy.

## 16. Evidence and source references

Source review only, not a deployment or live infrastructure verification:

- ADP: `35a708314e9c34f0ad22d173c0cff93544dcf0f7`.
- AISuperPlane: `5d543c952493f0765133b92e93301b0b24d028ee`.
- OpenResearch reference review: `00fe5ab7b7166b1a4e3c689cccd9d193a6728a4c`.

This revision incorporates the 2026-09-10 architectural consensus, published as
an [agreed design snapshot](https://github.com/aws-e/adp/issues/4904#issuecomment-5621154807)
on EPIC #4904. It does not imply the proposed Jobs or HITL consumers have since
shipped.

Recheck interfaces before implementation. Relevant source paths:

| Repository | Files |
|---|---|
| ADP | `ARCHITECTURE.md`, `modules/harness/README.md` |
| ADP HITL | `contracts/hitl-ticket/v1/README.md`, `contracts/hitl-ticket/v1/models.py` |
| ADP action provenance | `modules/gateway/src/internal/provenance_routes.py`, `modules/gateway/src/shared/models/provenance.py` |
| ADP vault | `modules/gateway/src/shared/models/vault.py`, `auth/vault_routes.py`, `auth/vault_schemas.py`, `shared/services/secrets_manager.py`, `shared/services/credential_resolver.py` (latter paths relative to `modules/gateway/src/`) |
| ADP delivery | `modules/gateway/src/internal/auth_deps.py`, `credential_routes.py`, `credential_binding.py`, `credential_egress.py` (latter files in the same directory) |
| ADP CLI/auth | `modules/gateway/cli/`, `modules/gateway/src/auth/cli_login.py`, `cognito_jwt.py`, `modules/gateway/infra/modules/cognito/` |
| Superplane | `src/superplane-api/app/models/`, `routers/`, `services/vault_sync.py`, `services/kubeconfig.py`, `services/proxy.py` (API paths under `app/`) |
| Superplane execution | `src/superplane-controller/`, `src/superplane-platform-monitor/`, `src/superplane-cli/`, `infra/control-plane/` |
| Superplane research lineage | `src/superplane-api/app/models/research_finding.py`, `src/superplane-api/app/models/research_proposal.py`, `src/superplane-api/app/routers/research.py` |

- [ADP at reviewed revision](https://github.com/aws-e/adp/tree/35a708314e9c34f0ad22d173c0cff93544dcf0f7).
- [AISuperPlane at reviewed revision](https://github.com/aws-innovate/AISuperPlane/tree/5d543c952493f0765133b92e93301b0b24d028ee).
- [AWS EKS Hybrid Nodes support boundary](https://docs.aws.amazon.com/eks/latest/userguide/hybrid-nodes-overview.html).
- [OpenResearch reference](https://github.com/alphaXiv/OpenResearch/tree/00fe5ab7b7166b1a4e3c689cccd9d193a6728a4c): selective reuse requires retaining MIT notices and checking dependencies; no runtime dependency is proposed.
- [Superplane #193](https://github.com/aws-innovate/AISuperPlane/issues/193): explicit budget requirements must not become optional.
- [Superplane #238](https://github.com/aws-innovate/AISuperPlane/issues/238): durable artifacts and evidence-based completion are necessary.
- [Superplane #325](https://github.com/aws-innovate/AISuperPlane/issues/325): independently checked measurement/cost calculations matter.
- [Superplane #335](https://github.com/aws-innovate/AISuperPlane/issues/335): remove controller desired state to prevent unwanted resource recreation.
