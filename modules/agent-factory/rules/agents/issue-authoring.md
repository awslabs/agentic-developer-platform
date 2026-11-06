# Writing issues developers can execute and reviewers can accept

Use this guide when creating, refining, implementing or reviewing an executable
story or epic. Use the [developer issue template](../templates/developer-issue.md)
or [epic template](../templates/epic-issue.md) as the canonical body for manual
and agent-authored work. ADP's GitHub Developer task and Engineering epic
templates copy these bodies (with GitHub metadata); update each pair together
and keep their bodies identical apart from the guide link. Worker images also ship these files under `/app/rules/agents/` and
`/app/rules/templates/`, so customer repositories do not need ADP source files.

The goal is a shared, observable completion contract. More text is not evidence
that an issue is ready. Keep the body at most 8 KB; link long procedures and
historical evidence at a published revision. Keep the goal, constraints,
unresolved prerequisites and acceptance table in the body. A small change may
need only a sentence per section and the acceptance rows its behavior requires.

## What belongs in the six sections

| Section | Questions the author must answer |
|---|---|
| The problem in plain terms | What does the user experience, what should change, and why? No paths or implementation jargon. End with the fix in one line. |
| Description | What is this issue's bounded outcome? Who owns it? Does completion mean reviewed code, merge, or deployed and live-verified behavior? What belongs elsewhere? |
| Impact analysis | Which users, callers and operational surfaces change? Which plausible failures matter? What resource/cost limits apply? |
| Design | What exists, what must change, what must be reused? What are the inputs, configuration, outputs, errors and compatibility rules? Which facts are unverified? |
| Deployment | What runs where, with whose authority? What setup is retained versus created per run? What deploys automatically, what needs an explicit action, and who owns recovery? |
| Validation | For each stable AC ID, what action produces which observable result, where is its evidence, and who runs it at which phase? |

For API changes, give request/response/error shapes and their schema source. For
UI changes, specify the affected route, user action, states and visible outcome.
For CLI changes, specify the command/config interface, errors, output and exit
behavior. Include schema/migrations and tenant/authorization effects when
relevant; otherwise state that they are unaffected. Do not force an API design,
new tests or live deployment onto a documentation-only change.

## Author check before assignment

The author/coordinator performs this check while preparing the issue. It is not
a new reviewer persona, approval gate or workflow job.

1. **Inspect before specifying.** Read the relevant code and current PRs. Link
   exact existing entry points and reusable components. Mark new paths/commands
   as proposed, not available. Verify a deployment trigger rather than assuming
   a merge deploys every component. A function name guessed from memory is not
   a design reference.
2. **Make the behavior decisive.** State observable success, relevant failures
   and compatibility requirements. Give configuration keys and where values
   come from. Account IDs, regions, tenant IDs and fixture names in an example
   are configuration unless explicitly fixed by the product contract. Files to
   modify guide the developer; an incomplete file list must not erase a stated
   behavior or prevent a necessary related fix.
3. **Separate fact, decision and investigation.** Record prerequisite status,
   evidence/date, owner/action and the exact phase it blocks. Selected account
   does not mean access verified; merged does not mean deployed. If the answer
   changes product behavior, security boundaries or scope, resolve it before
   dependent implementation. A bounded technical investigation can be the first
   owned checkpoint, with a specified output and no invented result. Missing
   live access need not block independent code work.
4. **Name the completion owner.** Default persona roles are insufficient. State
   whether the developer delivers code to a named operator or owns build →
   integrate → deploy → validate. Match this to existing authority. An issue
   cannot grant AWS access or waive branch protection. If post-merge acceptance
   belongs to this issue, use `Refs #N` until that acceptance passes; avoid an
   automatic closing reference on the implementation PR.
5. **Write one acceptance table.** Use stable IDs and action → expected result
   → evidence → phase/owner. Derive meaningful tests from behavior, including
   relevant negative and regression cases. Do not use a test that merely mirrors
   the implementation or demand a new test file for a prose-only edit. Name
   existing commands/checks; describe new tests as deliverables. Required live
   evidence must identify the actual observation source, fixtures and access,
   or a named investigation that resolves them before live acceptance.
6. **Check dependencies and scope.** Link parent via native sub-issues. List
   each blocking PR/issue with the capability it supplies and integration owner.
   Mark optional dependencies as optional. For parallel work, identify shared
   files/contracts and integration order; do not dispatch overlapping owners.
   Use the current dispatch mechanism in core-workflow; filing does not dispatch.

Record readiness for the next step accurately: e.g. "implementation can start;
live run waits for destination access, owned by coordinator." Do not describe
the whole issue as unambiguously execution-ready while hiding a required
decision or permission behind "verify later."

## Live automation: include the lifecycle when it applies

Specify one-time setup, each run, failure recovery, ownership, bounds and cleanup.
Keep pre-merge checks separate from post-deployment acceptance. Read the
[detailed lifecycle guidance](issue-delivery-review.md#live-automation-include-the-lifecycle-when-it-applies)
when authoring live automation.

## Specify the boundaries that repeatedly fail

For stateful/cross-component work, require durable recovery after partial success,
producer-to-consumer identity continuity, and proof of denied routes and cleanup.
Read the [boundary cases](issue-delivery-review.md#specify-the-boundaries-that-repeatedly-fail)
before assigning that work; helper tests do not establish a production path.

## Developer self-check and review feedback

Map every AC due at the current phase to evidence at the tested revision. Exercise
plausible counterexamples through the actual entry point and independent oracle;
preserve missing-fixture and cleanup gaps. One owner reconciles review findings
and closes in-scope defects under existing authority. Read the
[delivery/review procedure](issue-delivery-review.md#developer-self-check-and-review-feedback)
before implementing or reviewing work. A green check does not resolve a reproduced
blocker, and merge does not establish live acceptance.

## Specialized issues

- **Test coverage only:** keep the six sections short; Design states the
  existing behavior and tests to add, Deployment states how CI consumes them,
  and Validation defines what the tests prove. Live infrastructure work needs
  the full applicable lifecycle and impact details.
- **Run-only evaluation:** use the delivery-loop evaluation template. It must
  point to already implemented checks and name its defect owner/protocol. Do not
  assign a build-and-run task using a template that prohibits implementation.
- **Epic/orchestration:** use a lean index of native children. Acceptance rolls
  up actual child outcomes; implementation merge alone cannot close live work.
- **Discovery:** state the question, bounded output, owner and decision it
  enables. Do not invent implementation details to fill the template.

A template reduces preventable ambiguity. It does not replace code review,
verify infrastructure access, or guarantee that no defect will be discovered.

## Requirements lineage contract: epics and stories

For epics use [epic-issue.md](../templates/epic-issue.md); for stories use the
existing developer template. Keep the same six top-level sections. GitHub Issues
owns the author-authored brief and acceptance; GitHub's native parent/sub-issue
relationship owns the hierarchy. Labels, title prefixes, branch names and copied
IDs are hints, not independent identity or authorization. Where a repository has
another registered tracker, preserve its source-native identity instead of
inventing GitHub identities. Do not introduce a second tracker for this contract.

### Identity and change rules

1. **Create the epic first.** It states a user/business outcome and completion
   boundary; it is not just a collection of implementation files. Each story
   delivers one independently verifiable outcome contributing to the epic.
   Implementation units can be native children of a story, but do not become
   replacement acceptance owners. Preserve existing AIDLC decisions and gates.
2. **Fill the Description lineage table.** `Kind` is `epic` or `story`;
   `Project reference` names an existing registered project (or `unresolved`
   while drafting); `Parent epic` is a full issue URL or explicit `none` for a
   standalone story. List stable `Module IDs` and `Feature IDs` from the consumer
   catalog, or `unmapped` with an owner/action during discovery. Never mint a
   project/tenant ID from a repository name or use an issue field as access proof.
   Product/initiative context belongs on the epic and is resolved through
   registered relationships, not copied into every acceptance row.
3. **Let the provider mint issue identity.** Filing assigns the issue/node ID;
   the connector resolves repository/issue identity and records source revisions.
   Never type a made-up story ID into a dispatch context. Human-friendly labels
   such as `US-1.2` can remain aliases. Confirm the native parent link after filing;
   the body link helps readers but does not establish that link by itself.
4. **Use stable, local acceptance keys.** Stories use `AC-01`, `AC-02`, etc.;
   epics use `EC-01`, `EC-02`, etc. Existing IDs such as `AC-001` stay unchanged.
   The canonical identity includes source/repository/issue plus the key. The
   same AC-01 in two stories is different. Keep IDs on wording edits; add IDs
   for new requirements, record retired IDs and never recycle or renumber them.
5. **Version requirements and mappings.** A substantive edit creates a new
   source snapshot and requirement revision. Explain the change in the body or
   linked change record. Reassess affected tests and acceptance; an old pass
   does not accept new wording automatically. A pre-implementation refinement
   is not a reason to retain ambiguous criteria just to avoid editing them.
6. **Keep instructions separate from credentials.** Issue bodies contain public
   examples and private configuration *references*, never access tokens, secret
   values, real deployment identities or raw private evidence. Do not put
   `@agent-*` mentions or dispatch labels in templates. Filing does not dispatch.

### Epic contract

An epic's six sections answer these questions:

- **Problem/Description:** who benefits, measurable outcome, registered project,
  optional product/initiative, scope exclusions, accountable delivery owner and
  completion boundary. State a baseline/target when making a numeric claim.
- **Impact/Design:** affected modules, capability boundaries, shared contracts,
  native child-story links, dependencies, integration order and integration owner.
  Separate child completion from the cross-story outcome the epic must prove.
- **Deployment:** release/environment selection, rollout and recovery ownership,
  retained resources, and any post-merge acceptance. Do not promise deployment
  from merge unless the named workflow actually performs it.
- **Validation:** one table of EC IDs, observable outcome/acceptance rule,
  qualified child-story AC references, integration evidence and phase/owner.
  An EC with no contributing child or direct integration proof is a coverage gap.

Use full child issue URLs plus AC keys, for example
`https://github.example.com/team/product/issues/42#AC-01` as a display reference
(the suffix is a criterion key, not a promised GitHub heading anchor). Once child
stories are filed, record their real URLs and establish native relationships.
Draft children can be named `unresolved`; they cannot support a ready/accepted
rollup. A live epic is not complete merely because all implementation PRs merged
or all child issues closed. Required child criteria and the epic integration
proof must meet the named completion boundary on compatible revisions/targets.

### Story acceptance: write an executable behavioral contract

Keep one authoritative acceptance table. For each AC specify:

- **Given:** actor/role, initial state, owned fixtures, relevant configuration
  and exact interface/version. State who can supply unavailable fixtures.
- **When:** one observable action through the real API/CLI/UI or worker entry
  point, with concrete inputs. A helper invocation cannot stand in for a missing
  production route. Name required sequencing for stateful flows.
- **Then:** independently specified outputs, status/error, state transition,
  side effects and prohibited effects. Use exact values or a defined predicate,
  units and bounds. “Works,” “robust,” “secure,” “fast” and “tests pass” are not
  sufficient expected results. Split unrelated behaviors into separate AC IDs.
- **Evidence / phase / owner:** required test/report/assertion, tested revision,
  fixture mechanism, pre-merge versus live phase and accountable executor. Mark
  proposed selectors/commands as planned; do not claim they already execute.

Add a scenario mapping table under Validation. Each row has a stable scenario
ID, local AC IDs, a case contract, verification layer, test reference and state.
Reuse catalog scenario IDs where available. A new issue-local `SCN-01` is scoped
by the issue until catalog admission records an explicit alias; do not silently
replace or renumber it. The case contract is `Given: …; When: …; Then: …;
Controls: …`, or a reviewed procedure identified by path, immutable revision and
scenario anchor. Multiple tests can map to a scenario and a test can cover
multiple ACs; do not duplicate executed-result counts because of those links.

Every required AC needs a scenario or an explicit manual verification procedure.
Use `planned` while writing missing tests, `implemented` for existing selectors,
and `manual` for manual procedures. Implementation readiness can include planned
tests; acceptance cannot claim those tests executed. `automatic-unit`,
`automatic-integration`, `automatic-e2e`, and `manual` are distinct verification
classes. A unit pass is not live E2E evidence. The adapter's eventual mapping to
Insights must preserve this distinction. Documentary/discovery outcomes can use
an accountable review procedure instead of an invented automated test.

### Deterministic test design recipe

For every automated scenario, record the following controls in the case contract
or its versioned test strategy. Use only controls relevant to the behavior.

| Element | Required specification |
|---|---|
| Fixtures | Initial records, roles/tenants, feature flags, schema and target revision; generate run-owned names without depending on existing user data |
| Stimulus | Exact API request, CLI command or UI interaction; payload, actor and execution mechanism |
| Oracle | Independent expected response and state invariants from the product contract; never compute expected output using the function under test |
| Clock/randomness/order | Fixed/injected clock and seed where supported; stable ordering or set comparison; avoid “today,” random collisions and order-dependent suites |
| Async work | Observable completion condition, configured deadline and bounded polling; no arbitrary sleep as the correctness assertion |
| Isolation | Separate users/resources per run; scope identifiers carried through the real producer/consumer boundary; no shared mutable fixture hidden in setup |
| Negative and boundary cases | Relevant unauthorized/wrong-tenant, invalid/empty, expiry/limit, duplicate/retry, partial-failure and compatibility states; document exclusions instead of inventing irrelevant cases |
| Cleanup | Owner and deletion/restoration assertions, including worker loss if part of the story; cleanup failure remains failure/incomplete |
| Evidence | Stable test/scenario/AC mapping, source and requirements revisions, every attempt, result and authorized artifact references |

A live provider or model call is not made deterministic by setting a seed alone.
Use pinned/stubbed dependencies for deterministic contract tests, plus a separately
labelled live integration/E2E scenario with bounded invariants. For model behavior,
assert a schema, required tool/side effect or independently evaluated property;
if semantic scoring is needed, version the rubric, dataset and evaluator and
classify it as an evaluation. Do not promise byte-for-byte model text or use an
unversioned model judge as the sole acceptance oracle. Statistical/performance
claims need a sample design, environment/load, metric, threshold and aggregation
rule; one fast response proves neither p95 latency nor reliability.

### Ready, implement, verify and report

Before marking an issue ready for implementation, the author resolves its project
and parent references, scope/completion owner, AC IDs, scenario mappings, oracles
and dependency owners. Unknown deployment access may block the live phase while
independent implementation proceeds; record that boundary. An optional offline
format checker can catch malformed tables and broken local AC references, but
cannot establish identity, native hierarchy, fixture availability, determinism or
permission. Human/agent review must still verify those facts against sources.

The implementer preserves the issue/AC/scenario keys in the versioned catalog or
adapter mapping, links the PR to the story, and keeps `.adp/tasks/<issue>.json`
criterion keys aligned where task boards apply. Do not hand-edit engine-owned
run IDs or signed contexts. Execution binds source/catalog/requirements/selection
snapshots; results preserve each retry. If automatic propagation is not yet
implemented, supply a reviewed mapping artifact and report that integration gap.

The completion report uses `AC/EC → scenario/test → run/attempt → outcome →
evidence → tested revision`, with accepted/rejected/blocked/inconclusive separate
from test outcomes. Explain missing implementations separately from missing
fixtures and missing result publications. A required not-run check is incomplete.
Closing an issue, approving a PR or passing a schema validator is not acceptance.
The future Insights collector/assessor remains separate implementation work; these
authoring instructions do not claim deployed ingestion or automatic gate changes.
