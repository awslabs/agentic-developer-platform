<!-- Epic authoring contract: ../agents/issue-authoring.md.
Replace prompts; preserve the six sections. Create native child links after filing.
Do not add agent mentions or dispatch labels. Keep the body below 8 KB. -->

## The problem in plain terms

<Who needs which capability, current limitation and measurable desired outcome.>

**The fix in one line:** <Outcome achieved when the epic is complete.>

## Description

- **Delivery owner:** <Accountable owner across children and integration.>
- **Completion boundary:** <Reviewed / merged / deployed and live-verified;
  name the integration and acceptance owners.>
- **In scope:** <Capability boundaries delivered by this epic.>
- **Out of scope:** <Explicit deferred capabilities and links.>
- **Product / initiative:** <Registered references, or not_applicable with reason.>

### Lineage

| Field | Value |
|---|---|
| Kind | epic |
| Project reference | <Registered project ID; unresolved while drafting> |
| Parent epic | none |
| Module IDs | <Catalog IDs; unmapped while discovery is pending> |
| Feature IDs | <Catalog IDs; unmapped while discovery is pending> |

<Provider identity is assigned on filing; never invent issue or project IDs.
Product/initiative membership does not grant project access.>

## Impact analysis

<Affected users/surfaces, important failure consequences, resource/cost limits,
and baseline/target for quantitative outcomes.>

## Design

<Shared architecture/contracts at a revision; producer/consumer boundaries,
compatibility, cross-story integration and observable outcomes.>

| Child story | Owned outcome | Epic criteria | Depends on / integration owner |
|---|---|---|---|
| <Full issue URL, or unresolved until filed> | <Independently verifiable outcome> | EC-01 | <Specific predecessor capability; owner> |

<Establish native sub-issue relationships. This table is a readable index, not
the hierarchy authority. Identify unverified prerequisites and their owner/action;
no implied implementation readiness from having a child list.>

## Deployment

- **Rollout boundary:** <Release/environment/config references and pipeline.>
- **Integration owner:** <Who assembles and verifies the cross-story result.>
- **Recovery / cleanup:** <Owner, retained resources and verification.>
- **Handoff:** <Post-merge steps and required evidence; none if inapplicable.>

## Validation

| ID | Outcome / acceptance rule | Child stories / criteria | Integration evidence | Phase and owner |
|---|---|---|---|---|
| EC-01 | <Observable epic outcome with exact predicate/bounds> | <Full child issue URL#AC-01> | <Cross-story scenario/run; independent oracle and cleanup> | <Phase; owner> |

<EC IDs are stable within this epic. Retire without reuse. Child references carry
both the full issue URL and its local criterion key; the suffix is a reference,
not a guaranteed browser anchor. Every EC needs contributing child evidence or
explicit direct integration proof. Do not duplicate child AC text here.>

**Completion report:** For each EC, cite child AC assessments and the integration
run/evidence at compatible code, requirements and target revisions. Distinguish
planned, blocked, not-run and observed results. Closed children, merged PRs and
passing unit checks alone do not establish the epic's live completion boundary.
