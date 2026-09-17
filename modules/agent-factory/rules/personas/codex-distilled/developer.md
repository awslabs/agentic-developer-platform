# Project conventions — code authoring

Apply these standards to any code you write or edit.

## Mindset
- Consistency first — match the existing code patterns, naming conventions, and
  project structure already present in the files around you. Read before you write.
- Test what matters — cover happy paths, error paths, and edge cases that could
  cause data loss.
- Incremental, minimal change — the smallest diff that solves the stated problem.
  No speculative features, no unrelated refactors.
- Surgical edits — do not rename variables, upgrade dependencies, or tidy imports
  in files you were not asked to change.

## Conventions / quality bar
- Code must compile and pass the existing tests before it is considered done.
- New code has unit tests covering its main paths.
- No hardcoded secrets, tokens, or credentials; no leftover debug code.
- Handle failure paths explicitly — do not swallow errors silently.
- Follow the conventions already established in the surrounding codebase over any
  personal preference.

## Human-readable findings and handoff

When a plan is required, lead with **My understanding of the task** and **How I
plan to implement it**. Explain the requested change, expected behavior,
logical implementation steps and verification in simple language and good detail.
Assume the reader has not read the issue, earlier discussion or code; explain
necessary technical terms and put supporting file paths and links afterward.

In the outcome, lead with the user-visible result and PR state. Separate tests
you ran from CI results and deployed checks. Name any rollout or migration
needed before users benefit. If another run already delivered the work,
distinguish its contribution from your verification or follow-up.

Do not put a full debugging diary or generic learnings section in the human
summary. Keep required handoff details in the linked record.
Apply this format to your findings or handoff. It does not grant authority to
post comments, coordinate other agents, or perform supervisor duties.
