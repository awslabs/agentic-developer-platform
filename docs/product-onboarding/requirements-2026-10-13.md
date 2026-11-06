# Product Onboarding detailed requirements — 2026-10-13

This document preserves the requirements body of [epic #5411](https://github.com/aws-e/adp/issues/5411)
as captured at source update `2026-10-13T15:32:28Z`. The source body follows
unchanged. It includes all 74 checklist obligations, PO-01–PO-13 and PON-01–PON-13.

Read the [requirements guide](README.md) for current navigation, service ownership
and interpretation. The epic incorporates this baseline by an immutable commit
reference; these requirements remain in its completion scope. GitHub owns the
current brief, acceptance and native hierarchy. This snapshot is supporting
detail, not a second live acceptance tracker. Checkbox states are captured states.

**Status:** proposed requirements, not an implementation or readiness report.
“What already exists”, “Gaps”, “Status today” and legacy implementation notes
below reflect the earlier issue narrative, not a new code/deployment audit.
Revalidate them before assigning implementation. PON labels are proposed work
packages, not filed child issue identities. The four-section profile revision
supersedes earlier navigation; existing governed Project IDs remain unchanged.

---

## Outcome

ADP Product Onboarding provides a minimal tenant-bound profile and shared UI/CLI guidance for ideas, existing assets and running products. Context and capabilities grow progressively. Product Context combines sourced understanding, financials and next steps. Completeness, readiness and authority remain distinct; existing project identities and selected-capability contracts are preserved.

## Product profile and progressive onboarding (requirement revision 2026-10-13)

This revision replaces six-group navigation with four sections and Product Context. Existing Product/Project identity, PON IDs and selected-capability contracts remain valid; prior assessments do not accept revised requirements.

### Start small within the ADP tenant

Ask once for product name, purpose/intended users and an accountable owner defaulted from authorized tenant membership. Offer Idea / Existing assets / Running product. Derive tenant from authenticated context; multi-tenant users explicitly select their active tenant. Register/select the Product and create/link its governed Project in the same resumable flow, with stable IDs and no duplicate forms or resources on replay. Product roll-ups never broaden project access. Initiative is optional; Release identity is introduced when planning a release.

An Idea/Draft can be saved without assets, environments or budgets. One or more authorized connected repositories establish the repository prerequisite for Getting Started; greenfield users may create their first repository through the guided flow. Saving a profile never provisions infrastructure or enables billable execution automatically.

### Four progressive input sections

| Section | Information and connections |
|---|---|
| Product Information | Name, purpose/users, owner, starting point, optional industry/subindustry, goals and tenant product/project associations |
| Product Context — Basic | One or more repositories; a tracker/board such as Jira, GitHub Projects or Linear; wiki/documents; team/roles, roadmap/features/issues, selected collaboration sources, decisions/history and architecture/dependencies |
| Product Context — Advanced | AWS or other cloud environments such as Azure/GCP; actual deployments; CI/CD tools; support/incident systems such as PagerDuty/ServiceNow; observability, runbooks, test/security/deployment/release strategies; enabled agents, skills and execution limits |
| Product Financials | Business case/objectives, optional initiatives, product/initiative/release allocations, release target date/period, AI budget amount/currency, budget owner and warning/approval/stop policy |

Providers are alternatives, not cumulative requirements. Basic includes repositories; boards/wiki/Advanced/Financials remain progressive. Register sources once through existing authorized tenant connections or the new-connection flow. Capture provenance, source identity, freshness and entered/discovered/needs-confirmation/not-applicable states; users can confirm/correct discoveries without approved decisions being silently overwritten. Communication ingestion is limited to selected authorized channels/documents/meetings/email and preserves source access and retention.

### Profile levels and capability unlocks

Derive Getting Started / Advanced / Pro from a versioned information-prerequisite map, not pricing or usage. Getting Started includes repositories; Advanced adds environment/delivery context; Pro adds operational/assurance/release context. Each capability unlocks when its own prerequisites are met.

Repository context enables codebase discovery, development assistance, review and scanning setup. Repositories plus an AWS environment expose setup options for AWS DevOps Agent, AWS Security Agent and AWS Well-Architected Agent. These are offers to begin guided setup: verify actual provider availability, scoped connection, telemetry/workload objectives and any remaining prerequisites before marking operational. Test configuration enables Test Director/regression setup; release strategy and deployment targets enable release-management setup. Reuse context and ask only for missing inputs.

Show completeness percentage with a visible completed/applicable denominator based on stage and selected capabilities; legitimate not-applicable sections are excluded, but required selected-capability checks cannot be dismissed. A greenfield profile does not need production history. Explain changes when scope grows. Keep completeness, capability readiness and execution authority separate; 100% or a hint never grants rights or starts work. Show one or two benefit-led next steps and allow “Add more context later.”

### Product Financials: direct, connected and extracted

Accept authorized direct entry, imported fields from registered planning/finance/work systems, and agent-proposed extraction from business-case documents. Users can combine sources per field. Each value retains source/link, source revision or synchronization time, owner and confirmation status. Configure authority per field; refresh must respect manual overrides, surface conflicts and flag stale/disconnected sources. Document extraction proposes values for confirmation; never silently treat inferred figures as approved allocations.

Example: a release in three months with $5,000 AI funding. Scope allocations to product/initiative/release, period and currency. Show attributable actual spend, remaining allocation and explicitly estimated forecast when supported; incomplete attribution remains visible. Roll up child spending without double counting and keep model spend distinct from cloud costs. Importing/confirming an allocation does not authorize spending: enforcement uses the tenant's existing hierarchical budgets, approval and limit controls. Child allocations must obey parent limits. Financials can remain absent until the user enables a capability requiring them.

### Product Context view: what ADP understands

Make this the main post-onboarding view, accessible through equivalent CLI summary/structured output. Present a readable, source-backed synthesis rather than only a connection inventory:

- Product purpose/users/domain, owners and goals.
- Delivery context: repositories, architecture/dependencies, backlog, roadmap and releases.
- Operational context: environments, observed deployments, pipelines and support.
- Financial context: business case, initiatives, allocations, budgets and supported spend/forecast.
- Capabilities available, ready for setup or needing attention.
- Suggested actions, the exact missing prerequisite and the benefit it enables.

Distinguish confirmed facts, inferred/unconfirmed understanding, unknowns, conflicts and stale observations. Users can inspect sources, correct the synthesis, confirm proposals and start the relevant setup from a recommendation. Show last refresh; source changes update affected understanding without erasing approved corrections or claiming unavailable systems were observed. Example actions: “Set up AWS DevOps Agent,” “Connect CI,” and “Confirm the extracted release budget.” Hints must not expose inaccessible projects, source titles or financial details.

### One agent, one backend, UI and CLI

The Product Onboarding Agent invokes shared operations. Persist steps for cross-surface resume. Backend owns authorization, validation, concurrency, idempotency, completion and prerequisites; conversation is not authoritative state. UI combines chat and editable sections/previews; CLI offers guidance, direct commands and structured output without requiring a model. Approval/credential handoffs resume safely; agent calls use applicable budgets.

Greenfield steps can be reordered. Reuse existing assets; import running products read-only. Optional service activation, remediation authority and release lifecycle follow the detailed contracts below.

### Tenant boundary and service ownership

All products/projects, context and financials operate under ADP tenant membership/policy, project grants, provider entitlement and action/environment policy. Owners/teams resolve to tenant members; store credential references only. IDs or connections cannot expand authority. Reauthorize API/CLI/agent/background/callback operations, search, roll-ups, derived/cached knowledge, recommendations and exports, including different projects of one product. Revocation stops unauthorized reads/actions and prevents stale cached disclosure. Personal Knowledge Layer groupings (#1728) are not authorization; execution retains canonical project_id.

Owners: this epic—profile/activation; #7356—Test Director; #7295—Insights; #7245—tools/credentials. Reuse #5408 patterns without customer hard-coding or duplicate services. Verify integration availability.

Deliver profile/direct APIs, agent/discovery, progress/unlocks, Financials/Product Context, then service integrations. Preserve PON/PO IDs. Use `Refs #5411` until the epic owner observes dev journeys, isolation and recovery.

### Lineage foundation and Insights integration

Use `docs/engineering-insights/lineage-schema.md` sections 3.0/3.11/4 and v2 section 11 for requirements/tests. Onboarding owns canonical identity/source authority; executors emit facts; #7295 joins emissions. Version profile/budget/source mappings with actor, revision and effective/recorded time. Context snapshots retain input/derivation/confirmation references; actions reference their snapshot. Preserve ledger provenance, corrections and optional links. #7298 maps interim IDs into the registry without broader access. Initial profile use need not wait for analytics.

### Additional acceptance criteria

| ID | Action and observable result | Evidence / phase / owner |
|---|---|---|
| PO-01 | Create an Idea with name/purpose/default owner and no assets; save usable profile, stable IDs and tenant/project binding without provisioning or duplicate forms | UI/CLI integration plus dev observation; implementation owner / epic owner |
| PO-02 | Read/update all four progressive sections; retain provenance, explicit unknown/not-applicable states and approved values on discovery refresh | Profile/discovery tests; before review, implementation owner |
| PO-03 | Begin in UI, resume in CLI and reverse; direct commands work without an agent; interruption/replay/concurrent edits do not duplicate resources or lose confirmed changes | Shared workflow tests and dev demonstration; implementation owner / epic owner |
| PO-04 | Vary stage, selected capabilities and applicability; percentage has an explainable denominator and useful next-step hints; no global onboarding gate | Completion/hint tests and UI/CLI readback; implementation owner |
| PO-05 | Set completeness to 100%, disconnect a credential, or omit execution approval/budget; readiness/authority stay truthful and no unauthorized work starts | Capability negative tests; before review, implementation owner |
| PO-06 | Attempt cross-tenant/project ID substitution, unauthorized source selection, search/roll-up/context access and actions after revocation | API/worker/client authorization tests including same-product project isolation; implementation owner |
| PO-07 | Follow Idea, Existing assets and Running product paths; save partial progress and enable only selected capabilities; existing pipelines/resources remain intact | Owned dev fixtures, discovery/change receipts and cleanup; epic owner after rollout |
| PO-08 | Enable a selected service through its actual scoped integration; show available capability and one real outcome; disconnect/suspend and recover with correct ownership | Service integration and lifecycle receipts, not configuration-only evidence; service owner / epic owner |
| PO-09 | Save Idea/Draft without a repo, then connect/create a repository; add AWS environment; expose appropriate setup offers without requiring every board/wiki/support tool or granting execution rights | Prerequisite tests and UI/CLI setup readback; implementation owner / epic owner |
| PO-10 | Enter/import/extract a release budget and date; confirm a proposal, refresh conflicting data and preserve an authorized manual override | Per-field provenance/authority and confirmation tests plus source-backed readback; implementation owner |
| PO-11 | Attribute release spend and roll it up; exceed parent allocation or omit approval; distinguish actuals, forecasts and unknowns | Budget/attribution tests: no duplicate counting or unauthorized enforcement; implementation owner |
| PO-12 | Open Product Context in UI/CLI; inspect/correct sourced understanding, refresh changed/stale/conflicting sources and follow a setup hint; revoke source/project access | End-to-end synthesis/correction/refresh and isolation tests with dev observation; implementation owner / epic owner |
| PO-13 | Reproduce context/actions from source/budget revisions; trace onboarding→agent→PR→tests and attributable cost through canonical scope. Replay/colliding IDs cannot duplicate/leak; optional links stay optional | Version/isolation tests; dev lineage with #7298/#7295/#7356, epic/service owners |


## Product and project identity in ADP

A **Product** is the enduring offering and user-facing context/profile, identified by product_id within its tenant. An **ADP project** remains the team-owned, governed delivery/operating scope, identified by project_id. One product may contain multiple projects. Renaming this experience does not rename or replace existing project IDs, execution scope or access checks. Product roll-ups expose only authorized project data.

It is deliberately not synonymous with any one asset:

- a project can contain one or more repositories;
- a repository may support more than one deployable component;
- an environment is a deployment target such as development, test, pre-production or production;
- an environment maps to an authorized cloud connection/account/region and may share an account with other environments when policy permits;
- an AWS account can be dedicated, shared, newly provisioned by an approved landing-zone process, or connected in place;
- an ADP organization/tenant is the identity and isolation boundary and can contain many projects.

The canonical project ID is stable even when a repository is renamed, an environment moves accounts, a team changes, or a pipeline is replaced.

The existing Agent Context “project” in #1728 is a personal, optional knowledge-retrieval view and explicitly **not a confidentiality boundary**. It may become one input to this experience, but it is not the team-owned project control plane defined here. Authorization continues to come from tenant membership, repository entitlement, explicit environment connections and execution policy.

## Capabilities a product can progressively enable

After the minimal profile is saved, users can select capabilities/service profiles over time. The table describes possible capabilities, not a creation checklist. Show what is provided, reused, owned, unavailable or not applicable. No repository or AWS account is created merely by saving a product.

| Capability | Project outcome |
|---|---|
| Project identity and catalog | Stable ID/slug, name, description, lifecycle state, business/technical owners, support channel, criticality, data classification, cost centre, service profile and audit history. |
| Source control | One or more verified GitHub/GitLab repositories connected in place, or an optional repository created from an approved template. Repository history and settings are never replaced silently. |
| Delivery | A detected existing pipeline or an approved baseline CI/CD pipeline introduced by pull request, with required checks, artifact provenance, deployment gates and rollback ownership. |
| Continuous assurance | Optional pull-request/nightly regression, performance and security qualification using one versioned suite contract, with schedules, budgets, owners and non-vacuous evidence. |
| Release management | Optional build-once immutable artifact publication, exact-evidence gates, environment promotion, release catalog, recovery and production approval appropriate to the project. |
| Assurance evidence | A tenant/project-isolated logical evidence lake and searchable catalog, either ADP-managed, customer-owned or federated from approved existing systems. |

| Environments | An explicit topology (for example dev/test/pre-prod/prod) whose entries bind account connection, account ID, region, deployment role, URLs, data class, approval policy and current release. |
| AWS/cloud | Reuse an authorized connection, connect an existing account through short-lived role assumption, or request a new account through the organization’s approved vending/landing-zone integration. ADP does not create an account merely because a project exists. |
| People and teams | Owning ADP organization/team, administrators, developers, operators, viewers, approvers, service identities, access review date and least-privilege assignments. |
| Agents and knowledge | Repository coverage, allowed agent personas/actions, indexed knowledge scope, credential bindings, deployment targets and human gates. |
| Cost management | Model-usage budget before activation, rate limits, AWS resource cost-allocation tags, account/environment attribution, alerts, owner and escalation route. Cloud spend and ADP model spend remain separately visible as well as project-aggregated where data permits. |
| Security and operations | Classification-driven controls, secrets ownership, audit/CloudTrail attribution, observability links, SLO/runbook/incident contacts, backup/recovery evidence and drift/readiness status. |
| Lifecycle | Resumable onboarding, change of ownership/topology, restriction, suspension, offboarding and archive without destroying externally owned assets by surprise. |

A project capability can be managed, connected, observed, customer-managed or not-applicable. This prevents a misleading “fully onboarded” claim when ADP only knows that an asset exists.

## What already exists

ADP has several useful building blocks, but no single project-onboarding contract or orchestration currently joins them.

### Existing capabilities

- docs/onboarding-walkthrough.md covers deployment/operator onboarding: first administrator, GitHub App/repository coverage, first agent run, roles, access requests, budgets and troubleshooting.
- #2981 and the Connections service cover GitHub-organization tenancy, App installation and selected-repository access.
- #3330 tracks the broader GitHub/GitLab source-control connection experience.
- #5180 tracks resumable CLI setup for administrators and users, including GitHub and AWS connections.
- docs/adp-platform-deployment/customer-aws-setup.md and the AWS connection UI support connecting an existing account with STS ExternalId and read-only, scoped-write or full-admin permission tiers. No long-lived AWS access key is required.
- Orchestration execution policy already models permitted repository IDs, environment connection IDs, actions and human gates; an empty deployment-target list is a meaningful fail-closed policy.
- The organization model supports platform/org/department/member roles, access requests and scoped administration.
- The gateway provides hierarchical model-usage budgets, rate limits and usage views. Resource tagging uses dimensions including Project, Environment, Owner and CostCenter; #2825 tracks making those useful in AWS Cost Explorer.
- #1728 provides an optional personal M:N grouping of repositories for knowledge queries; migrations and filtering exist, while the management API and identity propagation remain incomplete.
- #2571 owns reliable deployment of the ADP platform into a fresh account. #5408 owns ADP’s own immutable releases, nightly/release qualification and Engineering Assurance Evidence Lake design. Those provide a reference implementation; they are not yet productized as tenant/project services.

### Important legacy distinction

modules/agent-factory/runner-infra/REPO-ONBOARDING.md is an early, repository-specific ARC procedure from April 2026. It creates per-repository runner/IAM resources, copies agent code and workflows into a hard-coded organization, and uses label-triggered execution. It is not the modern project-onboarding product contract and must not be treated as the default flow. Modern onboarding should reuse the GitHub App/webhook model and shared self-hosted runner/platform capabilities unless a reviewed service profile explicitly requires repo-local CI.

### Gaps this epic owns

Today ADP has no:

- canonical team-owned project registry joining owners, repositories, environments, connections, policies, costs and readiness;
- single idempotent onboarding state machine or truthful readiness scorecard;
- approved greenfield repository/pipeline template contract;
- discovery-first import for a production workload;
- project environment catalog tied to verified account/region/role coordinates;
- policy explaining when an account is shared, dedicated, reused or newly vended;
- unified project-level model and AWS cost view;
- standard handover, access review, drift detection, suspension and offboarding procedure;
- reusable project-facing nightly regression/performance/security qualification;
- project-scoped immutable release/promotion capability;
- tenant/project-isolated assurance evidence lake/catalog or federation contract;
- brownfield import/shadow/cutover path for existing production delivery and evidence systems.

## Design of record

### 1. Control plane, not asset ownership

ADP stores a versioned project record and references provider assets by immutable provider identifiers where available. It reconciles observed state with approved desired state. It must not infer that ADP owns or may delete an existing repository, pipeline, account, stack, secret or production environment.

Every managed change produces a preview, names the target and actor, requires the appropriate approval, is idempotent, and records evidence. Import/discovery is read-only by default.

### 2. Canonical project model

The control-plane schema supports the following fields progressively. The minimal Product Profile above is sufficient for creation; additional fields become required only for the selected capability that needs them:

- project ID, tenant/organization ID, slug, display name and lifecycle state;
- business owner, technical owner, operating team, approvers and support contact;
- criticality, data classification, compliance profile and cost centre;
- repository bindings using provider + immutable repository ID + display name;
- environment bindings with environment class, connection ID, verified account ID, region, role, permission tier and production flag;
- delivery profile and references to pipeline/workflow, artifact registry and release policy;
- execution policy references for repos, environments, actions, credentials and human gates;
- model budget/rate-limit policy and cloud cost-tag/alert policy;
- readiness checks, exceptions with expiry/owner, last reconciliation and evidence links;
- asset ownership mode and deletion policy for every external asset.

Secrets and provider tokens are never stored in the project record. It stores credential references and health/status only.

### 3. Lifecycle and state machine

    Draft -> Discovering -> Awaiting approval -> Provisioning/importing
          -> Validating -> Active
    Active -> Restricted/Suspended -> Active
    Active -> Offboarding -> Archived

These transitions govern capability activation and lifecycle, not a compulsory initial wizard. A saved profile is usable before operational capabilities are active. Transitions are authorized, auditable and retry-safe; failure preserves successful steps. An execution capability is Active only when its selected-profile checks pass and exceptions are explicit, owned and time-bounded.

A project can be active for code-assistance while production deployment remains disabled. Readiness must therefore be shown per capability, never collapsed into one ambiguous green tick.

### 4. Service profiles

The first version defines composable profiles rather than assuming every project needs the full estate:

1. **Code Assist** — existing repository connection, users, agent policy, knowledge and model budget; no cloud deployment target.
2. **Managed Delivery** — Code Assist plus governed CI/CD, artifact controls and non-production/production environment bindings.
3. **Cloud Operations** — existing environments connected initially read-only, then explicitly approved scoped operations.
4. **Greenfield Full Stack** — optional repository template, delivery baseline and approved account/environment provisioning.
5. **Production Adoption** — discovery, baseline and staged control adoption for an already live service.
6. **Continuous Assurance** — pull-request/nightly regression, performance and security qualification plus trend reporting.
7. **Release Management** — immutable build/promotion, exact-candidate gates, release catalog and recovery.
8. **Assurance Evidence** — ADP-managed, customer-owned or federated evidence lake/catalog capability.


Profiles define mandatory checks, defaults and evidence but can be combined. Variance is recorded as a policy exception rather than hidden customization.

## Onboarding journeys

### A. Greenfield project

1. **Save the idea** — in the active tenant, enter name/purpose and confirm the default owner. Save the Product Profile and underlying project binding without requiring assets. Help shape a brief/first milestone when requested. The following steps are optional later capability activations; classification, service profiles, cost centre and approvals are requested only when relevant.
2. **Choose source strategy** — connect an existing empty/early repository or request creation from an approved template. ADP shows intended permissions and protected-branch settings before applying them.
3. **Choose environment topology** — select required environments and whether each uses an existing connection, a shared account, a dedicated existing account or an approved new-account request. Account vending is delegated to the enterprise landing zone; ADP tracks the request and verifies the result.
4. **Plan delivery** — choose an approved pipeline template or import an existing pipeline. Changes to a repository are proposed through a pull request. Required checks, artifact/release policy, secrets contract and deployment gates are visible.
5. **Bind people and agents** — map teams/roles, approvers and service identities; restrict repositories, actions and deployment targets; set human gates.
6. **Set financial controls** — establish model budget/rate limits before agent use; validate project/environment/cost-centre tags and cloud budget/alert ownership before managed deployment.
7. **Provision and validate** — execute approved steps, verifying immutable repository ID, AWS identity, region, role, pipeline, agent round trip, environment access, cost attribution, logging and rollback/runbook links.
8. **Handover** — owners accept the scorecard and outstanding exceptions. The project becomes Active only for the capabilities that passed.

### B. Existing project, not yet production

Save the minimal profile first, then use the same control plane to import and assess the selected existing repositories, pipelines and environments before proposing changes. ADP reuses healthy controls, opens reviewed changes only for gaps, and never forces a template merely for consistency.

### C. Existing production project (“brownfield”)

Save a Running product profile first. The following staged adoption flow applies when the user connects/enables the corresponding production capabilities:

1. **Authorize discovery** — identify accountable owners and scope; connect source and cloud with the minimum read-only permissions needed. Record immutable repo/account/region identifiers.
2. **Inventory without mutation** — discover repositories, branches/protections, CI/CD, artifacts, IaC, environments, domains, dependencies, secrets references, data stores, observability, backups, costs, incidents and compliance controls.
3. **Create an as-is baseline** — owners validate the topology and current production coordinates. Unknowns, drift and unsupported resources remain visible; ADP does not invent a clean bill of health.
4. **Assess gaps and blast radius** — compare the as-is estate with the selected profile. Produce a sequenced adoption plan, change/rollback method, maintenance needs and evidence requirements.
5. **Adopt in explicit stages**:
   - **Observe** — catalog, knowledge, audit and cost visibility; no writes.
   - **Assist** — agent code changes through reviewed PRs; no environment deployment authority.
   - **Govern** — required checks, policy, evidence and non-production validation.
   - **Operate** — narrowly authorized environment actions and promotion, with production human gates.
6. **Prove outside production first** — where feasible, reproduce the deployment in a non-production environment, qualify pipeline/rollback and establish monitoring before authorizing a production change.
7. **Authorize production separately** — an approved change record binds exact repo/release/environment coordinates. Import never grants production deployment merely because read-only discovery passed.
8. **Cut over reversibly** — if pipeline or operational ownership changes, run in shadow/parallel mode where safe, define the point of no return, observe, and retain the prior recovery path for the agreed window.

Brownfield onboarding must not, by default:

- transfer or recreate the repository;
- rewrite Git history or branch protections;
- replace a working pipeline;
- rotate production credentials;
- move an AWS account or workload;
- redeploy the application;
- claim ownership of externally managed assets;
- index or expose data beyond existing entitlements.

## Project-facing release, continuous assurance and evidence services

### Platform capability versus customer capability

#5408 governs how **ADP itself** is qualified, released and promoted. Completing that epic proves ADP can operate its own lifecycle, but it does not automatically make release management, nightly assurance or the Engineering Assurance Evidence Lake available to projects using ADP.

This epic productizes those patterns as tenant- and project-scoped services. The project-facing contract must be technology-neutral, independently authorized and safe for customer repositories/accounts. It may reuse ADP’s proven components and schemas, but it cannot hard-code ADP’s repository, test suites, artifact list, AWS accounts, environments, buckets, roles or approvers.

During onboarding, each project chooses an ownership mode per capability:

- **ADP-managed** — ADP provisions and operates the project-scoped workflow/catalog/storage under the agreed service profile.
- **Customer-managed, ADP-integrated** — existing customer workflows and storage remain authoritative; ADP receives signed results or indexes approved evidence by reference.
- **Observed** — ADP inventories and reports readiness/drift but cannot execute, promote or block.
- **Not applicable** — explicitly excluded with an owner/reason instead of appearing accidentally complete.

The selected mode, data location, retention, execution identity, cost owner and deletion policy are visible in the project record.

### Project release-management service

A project selecting managed release capability receives a versioned release contract appropriate to its technology:

- release identity binding project ID, source provider/repository ID, full source SHA, release ID and manifest hash;
- a project-defined artifact manifest supporting packages, container images, infrastructure modules, database migrations, frontend/static assets or other registered artifact types;
- build-once publication into an immutable/content-addressed artifact store;
- mapped project environments and verified account/region/role targets from the onboarding environment catalog;
- evidence-bound promotion through the project’s declared development/test/pre-production/production path;
- separation of build, approval and production authority where policy requires;
- release catalog, current-environment inventory, notes, retry, supersede/yank and incident reproduction;
- rollback/redeploy rules that preserve artifact hashes and explicitly model database/data compatibility;
- protected tag/release creation only at the project’s agreed lifecycle point.

ADP supplies the control contract and reusable workflow/template. The project supplies technology-specific build/test adapters and declares which manifest entries are production-bearing. Unsupported or excluded components remain visible and block “complete coverage” unless a time-bounded exception is approved.

A project is not required to use releases or production promotion merely to use Code Assist. Release capability is activated only for the relevant service profile and environments.

### Project continuous/nightly qualification

Projects can select scheduled qualification profiles made from reusable suites:

- functional and API regression;
- browser/end-to-end journeys;
- security: deterministic SAST, dependency, secrets, IaC, image/artifact and dynamic checks as applicable;
- AI security review as supplementary discovery and triage;
- performance, capacity and reliability baselines;
- migration, backup/restore and recovery exercises where applicable;
- project-specific suites registered through a versioned extension contract.

The same suite definitions support three orchestrators:

1. pull-request/continuous checks for fast feedback;
2. nightly or periodic broad qualification for early discovery and trends;
3. exact-candidate release qualification and promotion.

Nightly results do not authorize an unrelated release. Release may reuse them only when source SHA, manifest/artifacts where applicable, suite/config/tool/fixture versions, environment and freshness all match. Otherwise the same reusable suite runs for the candidate.

Onboarding records schedule, timezone, environment, runner pool/execution account, concurrency, maximum duration, expected cost, budget, maintenance windows, notification owner and collision constraints. Performance and destructive/recovery tests target explicitly approved non-production environments unless a separately approved production-safe probe exists.

Every expected suite must publish a terminal state. Missing, stale, vacuous and unexpected skipped results fail a blocking profile. AI-agent findings can affect the project risk decision, but stochastic AI review is not the sole deterministic gate.

### Project Engineering Assurance Evidence Lake

Each project receives a logical evidence namespace and catalog regardless of physical storage ownership.

**Large immutable evidence belongs in object storage**, including test reports, SARIF, SBOMs, provenance, signatures, performance measurements, traces, screenshots, security-agent findings, release manifests and acceptance records. **Queryable metadata belongs in the assurance catalog**, including coordinates, status, counts, hashes, retention, gate decisions, waivers and evidence URIs.

Supported storage modes are:

1. **ADP-managed tenant/project-isolated S3** — ADP owns lifecycle operations under the contracted retention policy.
2. **Customer-owned S3** — the customer owns encryption, residency and retention; ADP assumes a scoped role to publish/read an approved prefix or stores signed catalog references.
3. **Federated/existing evidence** — ADP indexes signed references from an existing CI/security/test system without copying restricted bodies unless explicitly authorized.

An illustrative logical namespace is:

    assurance/v1/tenant=<tenant-id>/project=<project-id>/
      repository=<provider-id>/source_sha=<sha>/suite=<suite-id>/run_id=<run-id>/

Release evidence additionally indexes release ID and manifest SHA256. Provider IDs and stable project IDs are authoritative; mutable repository/project display names are labels only.

The catalog records:

- tenant/project/repository/source/release/artifact coordinates;
- suite/profile/schema/tool/ruleset/fixture versions;
- trigger mode and workflow/run identity;
- environment connection, verified account, region and configuration fingerprint;
- status, severity/result counts, duration and performance summary;
- object URI, SHA256/signature, size, media type and retention class;
- gate decision, blocking findings, waiver owner/approver/expiry;
- actor, cost attribution and lineage to derived/remediation evidence.

Project users query only evidence authorized by project/tenant roles. Gate services verify object hashes/signatures and exact coordinates before relying on catalog rows. Analytics can normalize approved fields to Parquet/Iceberg for regression, flake, vulnerability aging, performance and release trends without placing report blobs in the operational database.

Raw source, secrets, credentials, production payloads and sensitive diagnostics are not collected merely because a project enables assurance. Onboarding applies classification, redaction, residency, encryption-key, retention and cross-account access policy before the first run. GitHub artifacts/comments are temporary/allowlisted delivery surfaces, not the canonical store.

Storage, queries, retention and scanner/model execution are cost-attributed to the project and surfaced alongside—not confused with—the project’s application/cloud spend.

### Greenfield service activation

After profile creation, for a new project explicitly selecting these release/assurance capabilities:

1. Select the release, qualification and evidence capabilities/service levels.
2. Choose ADP-managed or customer-managed execution/storage and approve data/residency/retention policy.
3. Register the artifact and environment topology plus suite manifest.
4. Introduce reusable workflow adapters by reviewed pull request; never write directly to a protected branch.
5. Provision/verify the evidence namespace, catalog permissions, runner identity, budgets and notifications.
6. Run deterministic fixture/preflight checks proving reports cannot be empty or cross project boundaries.
7. Execute the first broad qualification to establish a baseline.
8. Build a candidate once, bind evidence to its exact coordinates and promote through non-production acceptance before production is enabled.
9. Hand over dashboards, runbooks, waiver/incident/rollback processes and cost ownership.

The project may activate nightly assurance before release management, or Code Assist without either. Readiness remains capability-specific.

### Existing production project activation

For a brownfield project, ADP starts without changing the current pipeline, release process, buckets or production authority:

1. Discover current build/release workflows, artifact registries, schedules, test/security/performance suites, evidence locations, baselines, waivers, environments and production coordinates using read-only access.
2. Produce an owner-approved as-is map and identify evidence quality, retention, isolation and coverage gaps.
3. Import metadata and signed references where possible. Historical evidence is labeled imported/unverified unless its integrity and coordinates can be proven; importing it does not retroactively certify old releases.
4. Run ADP qualification in **shadow mode** beside the existing controls. Compare results, duration, cost, false-positive policy and release decisions.
5. Adopt incrementally: evidence catalog first, then nightly qualification, then release-candidate gates, then non-production promotion, and finally separately authorized production promotion.
6. Preserve the existing production rollback and delivery path until the new path has passed an agreed number of representative releases and the owner approves cutover.
7. Bind the first ADP-governed production release to exact pre-existing or newly built artifact coordinates. Never rebuild an already deployed release merely to import it.
8. Provide a reversible disconnect/export path so customer-owned artifacts and evidence remain accessible if ADP integration is removed.

Brownfield onboarding must not claim full release or assurance coverage merely because an existing workflow is green. It must prove completeness, non-vacuous output, identity, artifact lineage and gate semantics.

### Multi-tenant and authority boundaries

- ADP’s own release evidence and a customer project’s evidence are distinct namespaces and authorization domains.
- One project cannot select another project’s runners, environment connections, artifacts, catalog rows, waivers or encryption keys.
- Workflow templates use project-scoped OIDC/short-lived identities and verified provider/account coordinates.
- An ADP platform administrator does not automatically become a customer project release approver, and a project admin cannot change platform release policy.
- Customer-managed storage access is prefix, action and key scoped; loss of access becomes an explicit evidence error, never a false pass.
- Scheduled work obeys project budgets/rate limits and has an accountable service identity.
- Evidence export and offboarding revoke ADP access first; deletion follows the recorded owner/retention policy and preserves required audit records.


## Security and governance invariants

- Project selection can only narrow access. The #1728 soft project filter is never used as an authorization boundary.
- Provider IDs and verified STS/GitHub identities defeat rename and free-text-target ambiguity.
- No deploy action is admitted without an allowlisted environment connection and action in execution policy.
- Production is marked explicitly and requires a stronger, auditable approval boundary.
- Least privilege is progressive: read-only discovery before scoped write; elevation has owner, purpose and expiry.
- Cross-tenant repository, credential, account, evidence and cost data are fail-closed.
- All generated changes are reviewable; secrets never enter issue bodies, project records, logs, command arguments or machine-readable results.
- Exceptions are time-bounded and cannot silently turn a failed mandatory check green.
- Retry/reconciliation does not duplicate repositories, roles, stacks, memberships, pipelines or project records.
- Offboarding revokes ADP access before optional deletion and respects external asset ownership.

## Readiness contract

The project scorecard reports at least:

- identity/ownership;
- source connection and repository coverage;
- user/team access;
- agent and knowledge readiness;
- pipeline and required-check readiness;
- environment/account binding and actual identity;
- secrets/credential health without secret disclosure;
- model budget/rate limits;
- AWS tags, cost feed and alert ownership;
- security/compliance exceptions;
- monitoring, incident, backup and recovery links;
- last successful end-to-end validation and last drift reconciliation.

Every result is bound to project ID, tenant, provider asset IDs, environment connection/account/region, actor, timestamp and tested revision. Stale evidence is shown as stale, not passed.

## Scope and acceptance criteria

### 1. Project definition and registry

- [ ] Publish a canonical glossary distinguishing tenant, project, repository, component, environment, account, connection and ADP platform environment.
- [ ] Implement the team-owned project model, versioned desired/observed state and provider-ID bindings without weakening tenant isolation.
- [ ] Define ownership modes, service profiles, lifecycle states, exceptions and deletion policies.
- [ ] Reconcile or deliberately separate the personal Knowledge Layer project model in #1728; do not overload it silently.
- [ ] Provide authorized API, UI and CLI create/read/update/list operations with audit history and idempotency.

### 2. Discovery, planning and orchestration

- [ ] Provide resumable onboarding with per-step status, dependencies, actor, evidence and recovery action.
- [ ] Generate a dry-run plan before any external mutation and require policy-appropriate approvals.
- [ ] Reconcile retries and provider callbacks without duplicate resources.
- [ ] Detect configuration drift and show desired versus observed state.
- [ ] A saved minimal profile is usable immediately; never report an execution capability ready/Active from configuration presence or profile completeness alone.

### 3. Repository and delivery onboarding

- [ ] Connect one or more existing GitHub/GitLab repositories using immutable provider IDs and verified installation coverage.
- [ ] Optionally create a new repository from a versioned approved template with explicit owner, visibility and protections.
- [ ] Detect and preserve an existing pipeline; introduce baseline changes only through a reviewable pull request.
- [ ] Define required CI/security checks, artifact provenance, deployment gates, release integration and rollback ownership by profile.
- [ ] Retire or clearly mark the legacy per-repo ARC onboarding guide so it cannot be mistaken for the canonical product path.
- [ ] Verify a real agent issue/comment-to-reviewed-PR round trip for an entitled user.

### 4. AWS accounts and environment management

- [ ] Model project environments independently from ADP platform release environments in #3312/#5408.
- [ ] Bind each environment to a verified connection, account ID, region, role, permission tier and production classification.
- [ ] Support reuse of an existing connection/account, shared-account policy, dedicated-account registration and approved account-vending integration.
- [ ] Require read-only discovery before write permission for imported production environments.
- [ ] Prevent free-text or stale connection selection from targeting the wrong account.
- [ ] Verify non-production deployment/inspection and an explicit production-denied negative case.

### 5. Users, teams and policy

- [ ] Select owning organization/team, administrators, contributors, operators, viewers and approvers.
- [ ] Define onboarding/offboarding and periodic access review for humans and service identities.
- [ ] Materialize least-privilege repository/action/environment/credential policy with human gates.
- [ ] Make separate GitHub/AWS administrator handoffs resumable without sharing credentials.
- [ ] Prove a user outside the project cannot read its private metadata or invoke its repositories/environments.

### 6. Cost management

- [ ] Require an owner and model-usage budget/rate-limit policy before enabling billable agents.
- [ ] Enforce Project, Environment, Owner and CostCenter attribution on ADP-managed cloud resources.
- [ ] Ingest or link AWS cost data without claiming that gateway model budgets cap AWS infrastructure spend.
- [ ] Provide project and environment spend views, forecasts/anomaly alerts where supported, and clear data freshness/coverage.
- [ ] Define showback/chargeback export and escalation on budget or anomaly breach.
- [ ] Validate attribution with a known-cost fixture and prove one project’s cost cannot appear in another tenant.

### 7. Brownfield production adoption

- [ ] Implement a non-mutating inventory and signed-off as-is baseline.
- [ ] Grade discovered controls as reused, gap, unsupported, unknown or exception; never silently replace them.
- [ ] Support Observe -> Assist -> Govern -> Operate adoption with independent approvals and rollback.
- [ ] Prove import does not change production repository, pipeline, credentials or resources.
- [ ] Require non-production evidence or a documented, approved exception before production authority.
- [ ] Bind production changes to exact source/release/environment coordinates and capture post-change acceptance.

### 8. Operations and lifecycle

- [ ] Provide truthful readiness/drift dashboards, events, alerts and audit export.
- [ ] Define support handover, SLO/runbook/incident/backup/recovery requirements by criticality.
- [ ] Support restrict, suspend, transfer ownership, archive and offboard.
- [ ] Offboarding revokes tokens/roles/webhooks and stops new runs before any asset deletion; externally owned assets remain unless separately authorized.
- [ ] Test interrupted onboarding, approval timeout, partial provider failure, stale evidence, drift and offboarding recovery.

### 9. End-to-end evaluation

- [ ] Maintain version-controlled greenfield, existing-repository and brownfield-production fixtures.
- [ ] Run deterministic contract/security tests plus live GitHub and AWS acceptance, reporting mocked and live evidence separately.
- [ ] Bind results to the implementation revision and actual target identities.
- [ ] Prove the onboarding is resumable after interruption and idempotent on replay.
- [ ] Measure time-to-first-agent-PR and time-to-governed-non-production-deployment without weakening controls.

### 10. Project release, continuous assurance and evidence services

- [ ] Publish a technology-neutral project-facing contract derived from #5408 without hard-coding ADP’s own repository, artifacts, accounts, suites or approvers.
- [ ] Let onboarding select ADP-managed, customer-managed/integrated, observed or not-applicable ownership independently for release, nightly qualification and evidence.
- [ ] Define a versioned project suite manifest covering regression, performance, deterministic security, AI security review and project extensions.
- [ ] Provide reusable pull-request, nightly and release orchestrators that invoke the same suite definitions on authorized runner pools.
- [ ] Record schedules, timezones, environments, identities, concurrency, budgets, duration bounds, notifications and maintenance-window collisions.
- [ ] Define a project artifact manifest and adapters that support relevant package/image/IaC/migration/static artifact types and enforce declared coverage.
- [ ] Build once and promote identical hashes/digests through mapped project environments with exact evidence and human production authority.
- [ ] Provision a tenant/project-isolated logical evidence namespace plus searchable catalog using ADP-managed, customer-owned or federated storage.
- [ ] Bind evidence and decisions to project, provider repository ID, source SHA, release/manifest/artifact coordinates, suite/tool versions, environment and workflow.
- [ ] Reject missing, skipped, stale, partial, tampered, wrong-project, wrong-SHA, wrong-manifest and expired-waiver evidence.
- [ ] Apply project classification, redaction, residency, KMS, retention, export and deletion policy before the first assurance run.
- [ ] Attribute storage, query, scanner, performance and AI-agent costs to the project with budgets/alerts.
- [ ] For greenfield projects selecting managed assurance/release, capability acceptance proves first nightly baseline, immutable build, non-production promotion and evidence query. These are not prerequisites for creating the Product Profile.
- [ ] Brownfield acceptance imports without mutation, distinguishes verified from unverified history, runs in shadow, and preserves rollback before cutover.
- [ ] Cross-tenant tests prove project A cannot read/use project B’s evidence, runners, connections, artifacts, policies or waivers.
- [ ] Offboarding revokes ADP access, exports/preserves customer-owned evidence and follows explicit retention/deletion authority.


## Suggested delivery order

1. **PON-01 — Product vocabulary, minimal tenant-owned profile and canonical project/lineage binding**
2. **PON-02 — Product Onboarding Agent, shared API/UI/CLI, progressive profile, completion/hints and resumable lifecycle**
3. **PON-03 — Read-only discovery, provenance, confirmation and desired/observed reconciliation**
4. **PON-04 — Existing-repository binding and verified agent round trip**
5. **PON-05 — Greenfield repository template and reviewable pipeline bootstrap**
6. **PON-06 — Environment catalog, AWS connection binding and account-vending adapter**
7. **PON-07 — Team access, execution policy and administrator handoffs**
8. **PON-08 — Model/cloud cost attribution, budgets, alerts and reporting**
9. **PON-09 — Brownfield production adoption and reversible control cutover**
10. **PON-10 — Drift, lifecycle/offboarding and full onboarding evaluation pack**
11. **PON-11 — Assurance activation and suite-profile binding to shared Test Director #7356; reuse its execution/result APIs**
12. **PON-12 — Project immutable release, promotion, catalog and recovery service**
13. **PON-13 — Evidence capability activation, ownership/federation and import coordinated with Engineering Insights #7295**


Children must be independently deployable, state their provider and environment mutations, include negative authorization tests, preserve existing tenant/project data, and publish deterministic versus live evidence separately.

## Decisions

1. A project is a governed team workload, not a synonym for repository, account or environment.
2. Onboarding is a selectable capability bundle, not automatic creation of every asset.
3. Greenfield may provision; brownfield discovers and adopts in place.
4. Existing production import is read-only by default and grants no implicit deployment authority.
5. Repository and infrastructure changes are planned, reviewed, target-bound and reversible.
6. Project is not a security shortcut; tenant, provider entitlement and execution policy remain authoritative.
7. ADP model budgets and AWS infrastructure budgets are distinct controls with common attribution.
8. “Active” is evidence-based and capability-specific.
9. Offboarding revokes access first and never destroys externally owned assets implicitly.
10. ADP’s internal release/assurance implementation is a reference and dogfood path, not automatic entitlement or authority over customer projects.
11. Project release, nightly qualification and evidence are selectable, independently owned capabilities using one suite/evidence contract.
12. Brownfield adoption indexes and shadows before enforcing; imported history does not become certified merely by ingestion.


## Non-goals

- Requiring one repository or one AWS account per project.
- Moving a production workload simply to standardize its topology.
- Replacing healthy customer pipelines without an approved reason.
- Using Git branches as environments.
- Treating the personal Knowledge Layer project filter as authorization.
- Granting full AWS administrator access as a default onboarding shortcut.
- Claiming AWS account creation is possible without an approved organization/landing-zone integration.
- Deploying the ADP platform itself; that remains #2571 and ADP’s internal release lifecycle remains #5408.
- Copying ADP’s own test suites, accounts, buckets or approval identities into a customer project.
- Treating imported historical evidence as retroactive release certification.

## Related work

- #7356 — shared Test Director: test execution, consistent outcomes and reusable evidence; onboarding activates the capability
- #7295 — Engineering Insights: evidence/analytics contracts; onboarding supplies canonical tenant/project scope
- #7245 — tools, credential references and third-party connections

- #1728 / #1787 / #1788 — personal project-scoped Knowledge Layer and incomplete management/identity work
- #1887 — multi-tenant projects, asset management and observability umbrella
- #2981 — GitHub-org to ADP-tenant onboarding
- #308 / #627 — hosted tenant onboarding direction
- #3330 — source-control Connections service
- #5180 — CLI onboarding, AWS and GitHub setup
- #2571 / #906 — fresh-account ADP platform deployment
- #2825 / #872 — AWS cost-allocation tagging and cost observability
- #3312 / #5408 — ADP platform environments, continuous qualification, immutable release management and assurance-evidence reference design
- docs/onboarding-walkthrough.md
- docs/adp-platform-deployment/customer-aws-setup.md
- docs/agent-context/design-1728-project-scoping.md
- modules/agent-factory/runner-infra/REPO-ONBOARDING.md (legacy; not canonical)

Issue creation/refinement does not dispatch implementation agents or authorize any GitHub/AWS mutation beyond tracking this design.

---

## Lineage root and supporting systems (added 2026-10-09)

**Reference design:** [`docs/engineering-insights/lineage-schema.md`](https://github.com/aws-e/adp/blob/main/docs/engineering-insights/lineage-schema.md) (entities §3, relationships §4, ID attachment by stage §5, storage §8) and [`docs/engineering-insights/agentic-engineering-insights-overview.md`](https://github.com/aws-e/adp/blob/main/docs/engineering-insights/agentic-engineering-insights-overview.md). Agents working on this epic read those first.

The project registry defined in §2 above is the **root of lineage** for the Engineering Insights platform. The lineage schema's standard field `project_id` (the evidence/access boundary carried on every record) is the canonical project ID this epic creates. Everything below inherits it; nothing may invent a parallel project identity.

### The identity chain

| Level | Record (lineage §) | ID and who mints it | Held where | Status today |
|---|---|---|---|---|
| Product | Product (3.0.1) | `product_id` — product/service catalog, or registered here when no catalog exists | **project registry: dated Product → Project / Product → Epic attribution links** | missing |
| Initiative | Initiative (3.0.2), optional delivery project | `initiative_id` — tracker/PMO or registered here; `not_applicable` is valid | registry: dated Initiative → Epic links | missing |
| Business case | Business Case (3.1) | registry | registry: funding links to Initiative / Epic with allocation policy | missing |
| **Project** | standard field `project_id` (3.0) | **this epic**, stable across repo renames, account moves, team changes | project registry (§2) | designed here |
| Epic | Epic (3.2) | tracker-native (`#5411`, `TLS-E1`); dated Epic → Story membership | tracker + registry link | tracker only |
| Delivery flow | Initiative-level grouping of execution (engine) | engine `flow_id` + slug; **must bind to one `project_id`** | engine (`orchestration_flows`) | has `org_id` only — needs `project_id` |
| Node (story / eval / gate) | Story (3.3); Scenario / Test Run (3.8) for evals | engine node id + address; `issue_ref` = tracker-native `story_id`; `size`, `type`, `risk_class`, `fixes_story_id` | engine (`orchestration_nodes`) — needs a typed lineage block | title prefix + issue-body comment only |
| Agent run | Task (3.4) | `task_id` from the Task API / gateway, one per agent run or desktop session; `story_id` carried in dispatch | Task API / gateway | exists |
| Model call | Call record (3.5) | `call_id` from the gateway; `task_id` on every call | gateway usage ledger | exists |
| Commit / PR | 3.6 | provider-qualified SHA / PR id; task trailer on commits | engine `orchestration_pr_bindings` + Git | exists |
| Release | 3.7 | `release_id` from the release registry; binds project id, repository id, source SHA, manifest hash (see "Project release-management service") | release catalog | designed here |
| Deployment | 3.7 | `deployment_id`; `release_id`, environment, connection, verified account | engine D2/D3 receipts | exists for engine-dispatched deploys |
| Incident | 3.7 | `caused_by_deployment_id` set at review | incident source / registry | missing |

Resolution rule (lineage §4.1): product, initiative and funding context are **dated relationship records** with `valid_from` / `valid_to` and a `mapping_version`, never columns copied onto every node or event. Facts carry `project_id`, `story_id`, `task_id`; context is resolved at the event's effective date.

### Supporting systems — the registry of source connections

Every external system a project depends on is a **registered source connection** (`source_connection_id`, `source_system`) owned by the project, with the project's ownership mode and deletion policy applied. The onboarding UI/CLI must let a project owner define, verify and remove:

| Supporting system | `source_system` | What is bound | Verification |
|---|---|---|---|
| Source repositories | `github`, `gitlab` | provider + immutable repository id (§2 repository bindings); default branch; required checks | App installation / token readback of the repository id |
| Work trackers | `jira`, `github-issues`, `linear`, `azure-boards`, … | project key / board; epic and story ID patterns; status mapping to lineage `status` | read one issue by key |
| Programme / PMO tools | `jira-plans`, `productboard`, `aha`, custom | initiative and product catalogs; mapping version | read catalog entry |
| AWS environments | `aws` | environment class, connection id, verified account id, region, role, permission tier, production flag (§2 environment bindings) | STS/EKS readback (as the D1 manifest requires) |
| CI / pipelines | `github-actions`, `gitlab-ci`, `codebuild`, … | workflow identities, release registry, artifact registry | run readback |
| Evidence and quality systems | `sonarqube`, `snyk`, test-management tools, custom SARIF/JUnit producers | suite ids, scenario tag convention (`SCN-nnnn`) | artifact readback |
| Incident / change systems | `pagerduty`, `servicenow`, `opsgenie` | service id ↔ project; change record ids | read one record |
| Cost | `aws-cur`, `aws-cost-explorer` | cost allocation tags Project / Environment / Owner / CostCenter (§6) | known-cost fixture |
| Communication | `slack`, `teams` and registered email/meeting sources | explicitly selected authorized channels/sources | scoped readback; test messages only when explicitly requested |

Each connection records the `(tenant_id, project_id, source_connection_id, entity_type, source-native ID)` join key the lineage schema uses, so records from different systems about the same story reconcile without string matching. Connections for third-party OAuth reuse the credentials vault and AgentCore Identity design from the tools epic (#7245, TLS-S13–S16); the project registry stores references and health only, never secrets.

### Additional scope and acceptance criteria

#### 11. Lineage root and supporting systems

- [ ] Project registry records dated **Product → Project**, **Product → Epic**, **Initiative → Epic** and **Business Case → Initiative/Epic** relationship records with `valid_from`, `valid_to`, `mapping_version` and provenance; `initiative_id = not_applicable` is accepted for routine work.
- [ ] Every delivery flow binds to exactly one `project_id` at creation; flows without a project are refused; existing flows are back-filled under a recorded mapping version.
- [ ] Engine nodes carry a typed lineage block (`story_id`, `size`, `type`, `risk_class`, `fixes_story_id`, `team_id`, `cohort`) validated at draft preview; epic metadata carries `epic_id` and product links; values resolve against the registry, not free text.
- [ ] Task API runs and gateway call records inherit `project_id` and `story_id` from the node; commit trailers carry `task_id`; the join `project_id → flow → node → task → call` is demonstrable for one story end to end (lineage §9 worked example reproduced on real data).
- [ ] Supporting-system connections are first-class registry objects: create / verify / list / remove via API, UI and CLI with audit history; each verification is a provider readback, never a self-report.
- [ ] Release and Deployment records carry `project_id`, `release_id`, `deployment_id`, environment and verified account; Incident records can reference `caused_by_deployment_id`.
- [ ] A lineage emitter publishes schema records with the §3.0 standard fields into the project's Evidence Lake namespace (`assurance/v1/tenant=…/project=…/`); the catalog exposes the data-trust fields (coverage, link rate, mapping version) next to every number.
- [ ] Insights read models never query engine or gateway tables directly; they read emitted records only.
- [ ] Documentation: `docs/engineering-insights/lineage-schema.md` gains a "where each ID is minted in ADP" section pointing at this epic; the delivery-flow guide documents the project binding.

### Suggested placement in the delivery order

Deliver the registry (§1) **with** the product/initiative relationship records and the supporting-system connection objects in the first slice; bind delivery flows to projects immediately after. The emitter and insights read models follow as a separate Engineering Insights epic that depends only on that slice.

### Related (lineage)

- #7245 `TLS-E1` — first epic using the lineage ID convention (`TLS-Sxx`, `TLS-Sxx-ACnn`, `SCN-TLS-nnnn`) via issue-body lineage comments; to be replaced by the typed lineage block above
- #7197 `COS-E1` — same convention
- `docs/delivery-flow/README.md` — engine plan format (flow / epic / wave / node addresses) the lineage block attaches to


**Consumer epic:** Engineering Insights `EI-E1` = #7295 (ROI analytics over this lineage root; reverse ETL into Jira/PMO/project tools) depends on §1 + §11 of this epic and on nothing else here.
