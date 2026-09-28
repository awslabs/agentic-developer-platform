# PR Review Workflow

## Purpose and owner

The assigned reviewer owns **review → repair → verification → final report** for
one existing PR. Fix concrete issues within the accepted scope and available
authority instead of handing them back merely because they were found in review.
The review task ends with a verified result or a specific blocker. The configured
merge owner handles merging; review is not blanket merge/deployment authority.
Apply `rules/personas/reviewer.md` for the complete repair and evidence contract.

## 1. Verify the target and ownership

Use the engine's bound PR or the explicitly assigned repository/PR. Read its issue,
accepted design and applicable repository instructions. Verify the current state:

```bash
gh pr view "$PR_NUMBER" --repo "$TARGET_REPO" \
  --json number,state,isDraft,headRefName,headRefOid,baseRefName,body
```

Stop on a missing, ambiguous, closed or draft target. Do not choose a PR by title
similarity, mark a draft ready, or create another PR to publish the review.
Record the head SHA. Fetch and inspect that revision and its diff; do not review
an unrelated checkout or trust the description as proof.

Before any repair, verify the current branch-writing owner/claim and run evidence.
If another writer is active or ownership is uncertain, report that concrete hold.
Do not race another developer/reviewer/supervisor, reset its work or force-push.

## 2. Verify requirements and security

Extract the acceptance criteria, invariants and prohibited changes. Verify them
against current implementation, including unchanged code, and appropriate tests.
An absent diff line is not proof of missing functionality. Separate criteria due
at code merge from explicitly deferred live/deployment acceptance.

For each finding record:

- The violated requirement or demonstrated failure and its practical impact.
- Reproduction/evidence, impact severity, confidence and approval impact.
- Whether it is fixed, an unresolved blocker, or an optional follow-up.

Block on real correctness/security defects, applicable unmet acceptance criteria
and required checks. Do not turn preferences, speculative hardening or unrelated
inherited debt into mandatory work. Follow the project's threat model and verify
reachability and impact before calling inherited code an active vulnerability.

Run `/security-review` before approving. Inspect affected authentication,
authorization, input handling, secret exposure and dependency/configuration risks.
Scanner output is input to investigation, not automatic proof. Changes to live
permissions, credentials, resources or security policy need their existing
explicit authority; do not perform them merely to clear a finding.

## 3. Repair in the same review task

For confirmed in-scope defects with a clear solution, **make the correction on the
existing PR branch**. Fix missing behavior, logic/configuration errors, failing
tests and inaccurate required handoff evidence. Work size alone does not require
a developer handoff if scope, ownership, authority and remaining budget allow it.
Group related findings into a bounded repair batch; avoid unrelated cleanup.

A read-only delegated review produces findings. The owning reviewer carries out
the authorized corrections using its editing path; do not send the story back
because that particular review tool is read-only. Respect an explicitly read-only
parent task, unavailable write authority and any concurrent writer.

Reproduce the defect and add a regression where it protects meaningful behavior.
Inspect your repair diff. Stage only intended files, commit with the fix described,
and push through the authorized path to the same PR branch. Verify the remote
head has not changed before writing. If it has, reconcile the new work and
ownership before proceeding; never overwrite it.

Do not manually dispatch a developer or another reviewer solely because you
found or fixed an issue. Preserve the current action, lineage, claim and budget;
cooperate with an existing engine-scheduled review and do not reset limits.

Handoff only when a concrete missing decision/scope, authority/input, active
writer, external failure or exhausted limit prevents a verified repair. Complete
independent authorized repairs first, then report the remaining blocker, why you
cannot fix it, the owner and next action. A billing/runner outage is an external
check failure, not a reason to ask the developer for arbitrary code changes.

## 4. Verify the repaired revision

Run affected tests, integrations and required checks with pinned tools. After a
bounded repair, reuse identified evidence for unchanged areas and broaden testing
only when the change, new failures or unresolved concerns justify it. Reassess
security on the changed surface. Report absent tooling, skipped checks and missing
live inputs honestly; they are not passes.

```bash
gh pr checks "$PR_NUMBER" --repo "$TARGET_REPO"
```

For failed CI, read the job/step evidence. Repair failures caused by the change;
report an external billing, runner or credential failure with its owner. Never
suppress required checks or rerun an unchanged external failure indefinitely.

Re-read the remote head before publishing the final result. Changed code
invalidates earlier approval: record fresh functional/security evidence for the
final SHA. The reviewer is the author of its repair; preserve attribution and
satisfy any independently required approval. Do not claim a self-review is that
independent approval or launch a redundant reviewer to satisfy a made-up rule.

## 5. Publish one clear final outcome

During work, report actual phase/owner transitions: reviewing, reviewer fixing,
verifying, or blocked on a named input/check. Mark interim findings provisional.
Do not publish a final REQUEST CHANGES before attempting the repairs you own.

Use the existing structured review/artifact channel and the assigned PR. The
report starts with:

- Final verdict and verified head SHA.
- Fixed findings, repair author and commit(s), with verification evidence.
- Remaining blockers, check status and the next owner/action.
- Optional follow-ups, clearly separate from required repairs.

Publish any required formal GitHub review with `adp-review submit`, the authorized
path:

```
adp-review submit --repo OWNER/NAME --pr N --event APPROVE --body-file FILE
```

It asks the gateway for the distinct reviewer identity, submits the real verdict, and
if GitHub refuses it (the pull request's author and this reviewer are the same GitHub
App — HTTP 422) it publishes the analysis with the pending human approval named
instead of losing it. Exit `0` means the verdict is recorded; exit `3` means it was
published but only a human can supply the formal approval — report that pending
approval explicitly and do not call it approved. Do not substitute `--event COMMENT`,
`gh pr review`, or a committed review document for a required approval; none of them
set `reviewDecision`.

Follow with the complete criteria matrix and functional/security evidence. Keep
engine attribution. A final REQUEST CHANGES identifies unresolved code defects
that the reviewer could not repair and explains the handoff; BLOCK identifies the
specific missing authority/input/check. Never report a fixed finding as still
blocking. A process exit alone proves neither approval nor merge readiness.

A local `data/code-review/review-YYYYMMDD-pr-NNN.md` may hold the report for
publication, but is not an implementation change. Publish multiline comments
using `--body-file`; do not commit review logs or create report-only PRs that
trigger another review. Use the artifact service when available.

## 6. Leave merging to its authorized owner

In an engine review action, publish the final evidence for the engine's merge
phase. If the task explicitly authorizes the reviewer to merge, verify final-head
checks, required independent approvals, no unresolved blockers and repository
rules first. Merge only the verified head through the normal guarded path:

```bash
gh pr merge "$PR_NUMBER" --repo "$TARGET_REPO" --squash \
  --match-head-commit "$VERIFIED_HEAD"
```

Verify the merge result and report it only after it exists. Do not bypass required
checks, approve live gates or deploy as an incidental part of review.
