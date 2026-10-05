# Writing issues developers can execute and reviewers can accept

Use this guide when creating, refining, implementing or reviewing an executable
story. The [developer issue template](../templates/developer-issue.md) is the
canonical body for manual and agent-authored work. ADP's GitHub developer-task
template is a copy of that body (with GitHub metadata); update it together with
the canonical template and keep their bodies identical apart from the guide link. Worker images also ship these files under `/app/rules/agents/` and
`/app/rules/templates/`, so customer repositories do not need ADP source files.

The goal is a shared, observable completion contract. More text is not evidence
that an issue is ready. Keep the body at most 8 KB; link long procedures and
historical evidence at a published revision. Keep the goal, constraints,
unresolved prerequisites and acceptance table in the body. A small change may
need only a sentence per section and two meaningful acceptance rows.

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

Describe one-time setup, each run and failure recovery separately. Name the
execution host, identity for each account, configurable target bindings,
ownership markers, what persists, what is deleted and how cleanup survives
worker loss. Preserve existing limits, with concrete values or a verified
configuration reference. Require distinct evidence for distinct clients or
routes: a successful login or configuration read does not prove an invocation.

State which suite is required and what partial coverage means. Expected-negative
runs remain labelled negative. A required skipped, blocked or not-run check is
incomplete. Review/merge checks and post-deployment checks have different phases:
do not require main-only live runs before the PR needed to enable them can merge.

## Specify the boundaries that repeatedly fail

For stateful or cross-component stories, include the applicable cases below in
the existing Design and acceptance table. Keep simple stories simple; these are
concrete requirements to resolve before assignment, not another review stage.

- **Partial success and durable recovery:** distinguish operation completion,
  terminal reporting, queue acknowledgement and cleanup. State what happens when
  each succeeds while the next fails. Name the persisted retry record, the
  automatically scheduled consumer, its deployed configuration and when that
  record may be retired. Recovery must still run after successful acknowledgement
  and process restart without requiring a new command or a page visit. Exercise
  a failed repair followed by a later successful repair; verify necessary work
  remains discoverable and unrelated work is not indefinitely blocked.
- **Producer-to-consumer handoff:** name the shared schema and which component
  owns each field. Carry the same run, tenant, resource-instance identity and
  configuration through the real caller, serialization, consumer and execution.
  State who integrates both halves. Include a mismatched identity/configuration
  case at the actual CLI or API entry point; a helper accepting a valid object
  does not prove its caller establishes the required prerequisites.
- **Isolation and cleanup:** a permitted route does not prove other routes are
  denied. Evaluate combined permissions across attached policies/groups and
  pair the intended positive case with an extra permissive rule or replaced
  resource. Define ownership checks before mutation and authoritative absence
  observations after cleanup. Distinguish reconstructed ownership evidence from
  original creation observations; do not invent historical timestamps.

For example, epic #3959 exposed accepted-abort reporting failures after queue
acknowledgement, receipt validation bypassed by the real renderer caller, and
ALB isolation checks that accepted one valid rule despite extra open ingress.
Use these as failure patterns, not mandatory implementation choices for every
story. Link the shared design once and give each child its owned boundary and
integration evidence instead of copying competing contracts.

## Developer self-check and review feedback

Before requesting review, compare the diff and checks with every acceptance row
due at that phase. Include a compact AC → status → evidence mapping in the PR.
List post-merge checks as pending with their owner; do not claim them passed.
Run the named applicable checks, including relevant negative paths. If a
required fixture is unavailable, report the exact gap rather than substituting
a mock result for a live claim.

For critical acceptance, connect the command/entry point → shipped implementation
→ assertion → evidence. Tests of orchestration must retain the production
factory, registration and worker-bundle path; substitute external transports
where needed, not the journey whose existence is being tested. Unit tests of
helpers can supplement that check. A directory, callable or passing test count
does not establish an executable capability.

When a handoff carries callbacks or SDK options, require the consumer to invoke
those callbacks in the production shape. A mocked transport that merely accepts
an options object can hide a disconnected hook. Assert the resulting runtime
transition, such as tool work becoming active and then settling, and preserve
failure/cleanup observations from that same invocation. In epic #3959, passing
internal pause callbacks directly as SDK hook configuration looked wired in
helper tests but never exercised the SDK's tool-admission hook contract.

Ask what plausible wrong result could still make the check pass. Exercise that
counterexample through the actual predicate: e.g. unrelated usage, missing one
client's record, stale served artifacts, or cleanup with one owned resource
remaining. Expected values must come from an independent contract or pinned
release, not from the observation being checked. Compound criteria need explicit
subclaims; a per-run contribution is not proof of two full runs and recovery.

Cover the relevant state/input combinations rather than adding more repetitions
of the happy path: existing configuration, pending handoff, conflicting flags,
optional identity claims and interruption. For cross-component flows, follow the
same identity and data through producers, serialization/redaction, consumers and
cleanup. Canonical APIs can still disagree about the same caller. These checks
are proportional to the changed behavior; no new tests are required for a
prose-only change.

For broad automation, deliver one executable path through the real bootstrap,
intended operation, evidence and cleanup before expanding the matrix. Keep that
checkpoint visibly partial. Record missing implementation separately from an
implemented check blocked on a fixture, and identify who can supply that fixture.
An executor's denied access does not establish that every authorized setup owner
is blocked.

Review against the current contract and applicable correctness, security and
compatibility obligations. A blocking finding should identify the AC/invariant,
reproduction or evidence, expected result, and what clears it. Gather the
findings visible in the current revision in one review where practical. Record
whether subsequent feedback is an unresolved finding, a regression, a newly
discovered defect or a proposed scope change. Recheck the affected behavior and
necessary regressions after fixes; do not reopen resolved feedback without new
evidence. New substantive defects remain valid blockers even if an AC missed
them. Style preferences and unrelated feature requests are not new acceptance
conditions.

Name one owner who drives review findings to closure. Where authorized, that
owner may fix concrete in-scope defects directly, run the original reproductions
and affected checks, and merge under existing repository requirements. Return
substantial missing implementation to the developer with a consolidated scope;
do not create a new agent run or review round for every small correction. If the
same kind of gap recurs, update the issue and this guidance with the missing
contract or evidence requirement while continuing delivery. Do not add style
preferences as blocking criteria or waive unresolved correctness defects to
reduce the number of rounds.

Each fix response names the finding, changed revision and result of its original
reproduction, plus affected regression checks. A partial repair remains partial.
Bind the verdict to the reviewed head/base and, for deployment claims, the
verified target. Reconcile earlier findings and conflicting verdicts before
merging; an approval or green CI does not explain why a reproduced blocker is
resolved. Read ordinary review comments as well as formal GitHub reviews. Check
merge state before requesting another pre-merge review.

Keep user-approved changes in the issue body so the next executor need not
reconstruct the contract from comments. Explicit later user decisions take
precedence until incorporated; agent status prose is not a scope change.
Do not knowingly implement an unsafe or impossible design: explain the concrete
conflict, progress independent work, and obtain the missing decision if needed.

For a live build-and-run assignment, the executor fixes in-scope failures and
retries under the stated cost/time limits. An external blocker needs a concrete
owner, action and prepared artifact. An unchanged failure is not a reason to
blindly dispatch the same run. Completion maps every required AC to evidence at
the tested revision and identifies retained resources and outstanding cleanup.

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
