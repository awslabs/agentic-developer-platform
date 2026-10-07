# Approved story design template

Use this as a story issue section, an anchored section in the epic design, or a
short repository document. It must be available to both developer and reviewer.
Follow the [design guidelines](README.md); a separate document or review ceremony
is not required when the approved epic already contains this information.

## Identity and approved scope

- Story: issue link.
- Parent design: exact approved revision and relevant anchor.
- Approval: owner/review reference and date; indicate draft until approved.
- Implementation source: revision inspected and relevant files/contracts.
- Required prerequisites: identify what is already available and what blocks start.

## Behavior to deliver now

Describe one observable before/after outcome and the smallest complete execution
path needed to deliver it. Name its entry point, inputs, processing/integration,
output and failure behavior. State the actual minimum for merge, not just a list
of files or helper functions to create.

For example, a harness story could require: “Resolve an ordinary-user fixture,
authenticate, submit one request through the registered production dispatcher,
collect correlated response evidence and grade it using the existing report.”
Adding a case name and an unused client helper would not satisfy that requirement.

## Implementation contract

Name existing modules, interfaces and harnesses to extend. Specify the input/output
schema or source-backed examples, authentication and identity source, errors,
timeouts and relevant ordering/idempotency/recovery behavior. Link reusable
fixtures or describe how this story creates them.

Distinguish the currently supported protocol from planned features. A transport
response must not be relabelled as proof of durability or replay that the server
does not provide. Preserve the epic's security boundaries; missing platform
support must be reported explicitly rather than bypassed with privileged access.

## Acceptance and merge boundary

| ID | Required observable behavior | Test/fixture and expected result | Required for code merge or later qualification? |
|---|---|---|---|
| S01 | Supported end-to-end baseline | Real dispatch path with deterministic fixtures; valid input succeeds | Code merge |
| S02 | Relevant denial/failure behavior | Invalid or unauthorized input fails without false success | Code merge |
| S03 | Authorized live proof, if required | Actual target evidence and cleanup | State the explicit boundary and owner |

Replace these examples with story-specific criteria. Include security, recovery
and regression cases appropriate to the change. Give exact test commands when
known; otherwise name the test location and observable assertions to implement.
Exercise the actual integration path at the boundary under test. Deterministic
external fixtures are useful; replacing the code being validated with a success
stub is not evidence that it works.

Explicitly state:

- Which criteria and required CI checks must pass on the final PR revision.
- What may remain pending at code merge, why, and who owns completion.
- Whether live execution is authorized and what to report when it is not.
- Whether code merge completes the story or leaves separate acceptance outstanding.

## Deferred work

| Capability or evidence | Why deferred | Owning story/qualification | Required visible status |
|---|---|---|---|
| Fill in | Dependency or agreed scope boundary | Explicit owner/link | Blocked, unimplemented or pending |

Do not mix a future scenario inventory with this PR's minimum implementation.
Conversely, do not defer required baseline wiring or security work merely because
the epic contains later feature stories. Unsupported required cases must never
count as passed.

## Delivery evidence

Record the final source revision, criterion-to-test results, CI links and any
remaining acceptance obligations. Live evidence also needs authorized target
references, deployed revision, observation time and cleanup outcome in the
appropriate private record. Keep public summaries redacted.

The reviewer owns in-scope repairs and final validation under its assignment;
the deterministic controller enforces merge checks. Escalate only a specific
unresolved decision or unavailable dependency, not ordinary implementation work.
