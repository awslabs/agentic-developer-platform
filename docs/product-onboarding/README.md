# Product Onboarding requirements

[Epic #5411](https://github.com/aws-e/adp/issues/5411) owns the current brief,
acceptance and completion decision. The [detailed requirements baseline](requirements-2026-10-13.md)
preserves its full requirements as captured on 2026-10-13, including 74 checklist
obligations, acceptance IDs PO-01–PO-13 and proposed work packages PON-01–PON-13.
The epic links an immutable revision of these documents. Future substantive
changes must update that reference and explain changed requirements; old evidence
does not automatically accept new requirements. Native GitHub relationships own
the child hierarchy. The PON aliases do not establish those relationships.

The proposed [ADP Context Model v1](../product-context/README.md) turns the profile
and delivery context into field-level contracts: types, source mappings, authority,
relationships, provenance and daily delivery examples, with a checkable JSON
Schema. It is a design for review, not a deployed service or accepted implementation.

The [Product Profile and Onboarding detailed design](design/README.md) for
[#8153](https://github.com/aws-e/adp/issues/8153) adds the source audit, database/API
contracts, UI/CLI journeys and phased delivery/evaluation backlog for #8141.
It is proposed for review; it does not approve implementation or claim deployment.

The [interactive HTML mockup](mockups/product-onboarding.html) and
[placement and usage notes](mockups/product-onboarding.md) demonstrate the proposed
experience. Download the HTML and open it in a browser; no build or server is needed.

[Project budgets and base Projects](project-budgets.md) describe the execution
scope shared by delivery flows and agents, and the Product workspace's budget
and spend view.

## Product experience

Start with name, purpose/intended users, an owner defaulted from authorized tenant
membership and Idea / Existing assets / Running product. An Idea/Draft can be
saved without repositories, environments or budgets. Getting Started requires at
least one connected repository; a greenfield user can create one through guidance.

The four progressive sections are Product Information, Product Context — Basic,
Product Context — Advanced and Product Financials. Boards such as Jira, GitHub
Projects and Linear are alternatives. Financials accept direct entry, connected
data or confirmed extraction, with field authority, provenance and overrides.

Getting Started / Advanced / Pro describe available information, not pricing or
usage. Each capability uses its own prerequisites. Repositories plus an AWS
environment can expose relevant AWS setup offers. The
[AWS agent capability design](../product-context/aws-agent-capabilities.md)
adds DevOps, Security, Well-Architected and FinOps integration: shared inputs,
separate provider spaces/profiles, authorized setup and reuse of their exposed
knowledge and findings. Documents can unlock threat-model setup and billing
context can unlock FinOps without an environment/repository respectively;
availability and feature-specific prerequisites must be verified before use.
Completeness, readiness and execution authority are separate. Saving context
does not provision assets or authorize spending.

The ongoing Product Context view and equivalent CLI summary show sourced
understanding, confirmed/inferred/missing/stale/conflicting facts, capabilities
and one or two useful next steps. One shared backend governs direct operations
and a resumable agent experience across UI and CLI.

Selected capabilities are provisioned or linked from the same UI/CLI experience,
with explicit tenant/Project scope, provider authorization and recoverable setup.
Product Context shows provider readiness, discovery/analysis progress, findings
and freshness. Product Financials can consume FinOps cost analysis while its
allocations and spending enforcement remain with the designated finance/budget
services. The capability design is a proposed extension; the historical baseline
and its acceptance IDs remain unchanged pending adoption of a new epic reference.

## Find the detailed contract

| Topic | Baseline section |
|---|---|
| Four inputs, levels, Financials, Context view and shared UI/CLI | [Progressive onboarding](requirements-2026-10-13.md#product-profile-and-progressive-onboarding-requirement-revision-2026-10-13) |
| Proposed AWS capability setup, context reuse and provider boundaries | [AWS agent capabilities](../product-context/aws-agent-capabilities.md) |
| Product versus governed Project identity | [Identity](requirements-2026-10-13.md#product-and-project-identity-in-adp) |
| Registry, lifecycle and composable service profiles | [Design](requirements-2026-10-13.md#design-of-record) |
| Greenfield, existing assets and production adoption | [Journeys](requirements-2026-10-13.md#onboarding-journeys) |
| Release, assurance and evidence activation | [Service contracts](requirements-2026-10-13.md#project-facing-release-continuous-assurance-and-evidence-services) |
| Checklist groups 1–10 | [Scope](requirements-2026-10-13.md#scope-and-acceptance-criteria) |
| Checklist group 11, source registry and identity chain | [Lineage](requirements-2026-10-13.md#lineage-root-and-supporting-systems-added-2026-10-09) |
| Proposed work packages | [PON-01–PON-13](requirements-2026-10-13.md#suggested-delivery-order) |

## Tenant scope, lineage and service ownership

Tenant identity comes from authenticated context, with explicit active-tenant
selection where needed. Owners and resources must be entitled. Product roll-ups,
search, context, caches and actions preserve individual Project access, including
revocation. Products do not replace execution `project_id` or expand authority.

Use the [lineage schema](../engineering-insights/lineage-schema.md), especially
sections 3.0, 3.11, 4 and v2 section 11. Onboarding owns canonical identities,
source registration and field authority. Executing services emit scoped facts;
Engineering Insights joins those emissions without querying engine databases.
Profile, budget and relationship revisions retain actor, effective/recorded time
and corrections. Context snapshots retain inputs, derivations and confirmations;
actions reference the snapshot used. Optional story/initiative links stay
optional: older typed-lineage examples do not require fabricating associations.

| Epic | Responsibility and integration boundary |
|---|---|
| [#5411](https://github.com/aws-e/adp/issues/5411) | Product/profile registry, onboarding, context, Financials and selected capability activation/integration |
| [#7628](https://github.com/aws-e/adp/issues/7628) | Product release planning and management; owns the release implementation formerly proposed as PON-12, while #5411 retains activation/integration |
| [#7356](https://github.com/aws-e/adp/issues/7356) | Shared Test Director execution, run/test outcomes and result reuse; onboarding binds suite profiles and activates the service |
| [#7295](https://github.com/aws-e/adp/issues/7295) | Engineering Insights ingestion, evidence/analytics and emitted lineage; onboarding coordinates evidence ownership/federation |
| [#7245](https://github.com/aws-e/adp/issues/7245) | Tools, credential references and authorized connections |
| [#7298](https://github.com/aws-e/adp/issues/7298) | Delivery-flow project binding; interim IDs must map to the canonical registry without expanding access |
| [#5408](https://github.com/aws-e/adp/issues/5408) | ADP's own release lifecycle, a reference for reusable project capabilities |

The older service sections describe required integrated outcomes; they do not
require duplicate Test Director or Insights implementations inside onboarding.
Profile creation can ship independently of analytics. Full epic completion still
includes the selected-service integration, lifecycle and lineage obligations.

## Change record and delivery status

2026-10-15: added the proposed four-provider AWS capability design, including
FinOps, reuse of exposed context/evidence, typed binding requirements, tenant
boundaries and provisioning qualification gaps. No provider integration was
deployed or accepted; adoption requires updating the epic's immutable reference.

2026-10-15: added the proposed typed context contract and reflected the epic's
existing ownership split with #7628. The immutable requirements baseline below
retains its original wording and IDs; it is not silently rewritten.

2026-10-13: moved the oversized issue details into a versioned baseline and
restructured the epic using the canonical six-section template. No PO/PON IDs
were renumbered and no checklist obligations were removed. This is a documentation
refinement, not implementation, deployment or acceptance evidence.

At migration, the epic had no assignee or native sub-issues. Delivery/integration
ownership, registered project and catalog mappings remain unresolved. Assign
accountable owners and real child issues before marking dependent work ready;
retain direct integration acceptance until qualified child criteria exist.
Historical claims in the baseline must be revalidated at implementation planning.

The [shared Product Profile](shared-profile.md) describes the product-level sections, APIs, persistence and source-authority boundaries.
