# Design guidelines for agent delivery

Use an [epic design](epic-design.md) to settle shared architecture and ownership.
Use an [approved story design](story-design.md) to define exactly what one PR must
deliver. The developer and reviewer must work from the same approved revision and
the same definition of done.

These are authoring and review guidelines, not a new engine schema or an automated
validation feature. Keep simple stories short. A story section in the epic design
is sufficient if it contains the story-level information below; a separate design
PR for every story is not required.

## What belongs at each level

| Topic | Epic design | Story design |
|---|---|---|
| Outcome | User outcome across the whole feature | Observable behavior delivered by this change |
| Architecture | Shared components, boundaries and contracts | Existing code to extend and concrete integration points |
| Ownership | Which story owns each capability | What this story implements now and explicitly defers |
| Dependencies | Ordering and shared decisions | Exact prerequisites needed before this work can start |
| Acceptance | Integrated feature and rollout qualification | Minimum executable result, tests and conditions for PR merge |
| Evidence | How story evidence combines into epic acceptance | Commands, fixtures and expected results for this PR |

## Approval and availability

1. Write against an identified source revision. Distinguish existing behavior from
   proposed behavior and link the implementation behind factual claims.
2. Resolve decisions that determine implementation before approving the affected
   story. Record the approving owner/review, date and exact revision. A document
   titled “approved,” a draft PR or elapsed time is not approval evidence.
3. Merge the approved design into the repository before dependent implementation
   starts, or have the controller supply its exact approved contents as task
   context. A URL to an unfetched branch is not sufficient for an offline agent.
   Verify that the worker can read the document and referenced contract fixtures.
4. Link the approved revision and story anchor from the issue. After merging,
   replace stale “draft” wording and references to superseded revisions. Keep
   issue acceptance criteria consistent with the design.
5. Amend the design explicitly when scope or a shared contract changes. Record
   the decision and affected stories; do not silently redefine acceptance during
   implementation or review. Approval does not itself authorize live spending,
   deployment or destructive operations.

## What to enforce

Before dispatch, the assigning controller or operator checks that the approved
design is readable, implementation prerequisites are met, and the story names a
minimum deliverable and a clear merge boundary. Do not dispatch repeatedly against
an unchanged missing prerequisite.

The developer implements that minimum, including wiring and meaningful tests.
Registering names, adding helpers or passing mocked grading tests is insufficient
when the story requires an executable path. Disclose remaining work and provide
evidence against the acceptance criteria; creating a PR is not completion.

For a review–fix–merge assignment, the reviewer checks the assigned PR against the
approved story, fixes in-scope defects, runs relevant validation and lets the
delivery controller verify final-head CI and merge. Findings are repair work,
not a routine handoff to another developer. Reuse valid evidence for unchanged
content; broaden testing when changes or failures justify it.

The reviewer must not reopen the entire epic design or require later feature/live
qualification when the story explicitly permits code merge first. It must not
waive missing implementation, credential separation or required checks either.
If a real design conflict blocks repair, identify the exact decision, affected
code/test and evidence checked. Continue independent repairs; escalate only the
decision that cannot be resolved within existing authority. Missing local design
context calls for supplying the approved artifact, not inventing its semantics or
declaring it unapproved.

Keep repair retries bounded by the controller's existing retry, no-progress, time
and budget limits. Report the specific unresolved blocker when stopping. Never
treat successful review execution, a repair checkpoint or PR merge as proof that
separately required deployment or live qualification passed.

## Before publishing

Check that every acceptance criterion has an owner and observable evidence, every
deferred item has a destination, and no prerequisite creates a dependency cycle.
Use the existing test harness and delivery controls where applicable. Follow
[the public-documentation policy](../PUBLISHING.md); keep live identities, secrets
and operational receipts in private records. Run the public-doc scan and check
links, examples and whitespace. These checks validate documentation, not runtime
correctness.
