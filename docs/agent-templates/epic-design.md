# Epic design template

Copy and fill the sections below. Remove guidance after filling it; use “not
applicable” with a reason where needed. Keep the design proportional to the work.
Follow the [design guidelines](README.md).

## Identity and approval

- Epic: issue link and user outcome.
- Status: draft, approved or superseded.
- Source audit: immutable repository revision and date.
- Approval: owner/review reference, date and approved revision.
- Supersedes: earlier decisions and the stories affected.

Record approval only when it exists. Link the merged revision from child issues;
do not require a commit to contain its own hash.

## Outcome and scope

Describe what users will be able to do, the measurable success conditions, and
what this epic will not change. Identify current behavior and the actual gap.
Separate implementation, deployment, exposure and final qualification.

## Existing system and chosen architecture

Identify existing components to reuse, with source links. Show the request/data
flow and the changes at each boundary. Explain the chosen approach and material
tradeoffs briefly; avoid a catalogue of hypothetical alternatives.

For relevant boundaries specify:

- Authentication, server-derived identity, authorization and credential access.
- Data ownership, storage, retention and any migration/rollback behavior.
- Shared API/event contracts, error semantics, versioning and compatibility.
- Concurrency, idempotency, durable state, recovery and cleanup responsibilities.
- Operational limits, observability and cost/performance targets.

Give concrete request/response examples or link versioned schemas and fixtures
where downstream implementation depends on exact semantics. Label future
protocol behavior separately from currently supported behavior. Assign each shared
contract to an owner; downstream stories must not invent incompatible versions.

## Story decomposition and dependencies

| Story | Owned deliverable | Required predecessor or contract | Deferred work and owner |
|---|---|---|---|
| Fill in | One bounded implementation outcome | Exact prerequisite | Explicit destination |

Explain the dependency order without cycles. Distinguish code dependencies from
deployed-feature dependencies needed only for live tests. A shared test harness
must have an early usable baseline; it must not depend on the final qualification
that will consume it. Feature stories own adding their executable scenarios.

Each child must contain or link an [approved story design](story-design.md).
Do not copy all epic acceptance requirements into every child's merge gate.

## Integrated acceptance and evidence

Assign stable criterion IDs. For each criterion specify observable success and
failure, owning stories, fixtures and evidence, and whether validation is offline,
integration or live. Include positive authorized behavior and relevant negative,
failure and recovery cases. Define measurable thresholds and a baseline for any
performance claim.

Explain how evidence from child stories establishes epic acceptance. Missing,
blocked or skipped required cases remain outstanding. Unit-test success does not
prove deployed isolation, live compatibility or user-perceived performance.

## Delivery, rollout and recovery

State what may merge before integrated qualification and what cannot. Identify
merge-triggered deployments and the authority or guards they require. Describe
feature defaults, migrations, staged exposure, monitoring, rollback and cleanup.
Identify who authorizes live execution and who accepts its evidence.

## Decisions still needed

List only real unresolved decisions, each with an owner, affected stories and
resolution needed. Do not approve dependent implementation until its blocking
decisions are settled. Independent stories may proceed under their approved scope.
