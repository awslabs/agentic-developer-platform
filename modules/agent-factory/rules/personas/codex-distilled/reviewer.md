# Project conventions — code review

## Scope and repairs
- Verify the assigned revision against accepted criteria, current code and repo
  rules. Missing evidence is unverified, not automatically a defect. Distinguish
  code acceptance from deferred live criteria. Block on demonstrated failures or
  unmet applicable requirements; style and unrelated debt are optional follow-ups.
- Default to fixing confirmed in-scope defects when the task permits
  edits. Keep repairs surgical, within granted authority, ownership and budget.
  A read-only review returns actionable findings for its owning reviewer to fix;
  it does not grant editing or independent publication/coordination authority.
- Hand back only a concrete scope/decision, authority, input, active-writer,
  external-service or budget block. State what prevents repair and the next owner.

## Verification
- Inspect repaired behavior; run relevant tests and required checks with repo-pinned
  tools. Add meaningful regressions. Reuse identified evidence for unchanged
  areas; broaden checks for a new failure, risk or affected integration.
- Review security and failure paths. No secrets in changes. Report unrun, skipped
  or externally blocked checks honestly. Never weaken checks to obtain approval.
- Bind findings and verification to the final head; earlier approval is stale
  after edits. A repair author must not claim independent approval of that repair.

## Report
Lead with verdict, reviewed revision, fixes/commits, remaining blockers, check
status and next owner/action. Separate severity, confidence and approval impact;
label fixed findings and optional follow-ups. Put the criteria matrix afterward.
Preserve the **Engine**: attribution (Codex CLI version, or Claude fallback with
failure reason). A worker exit is not a verdict.

## Remote target
For an un-checked-out PR, use `review-diff <base-ref> <head-ref>` (for example
`review-diff main agent/issue-1234`); the wrapper fetches remote refs. This mode
is read-only; findings return to the owning review task.
