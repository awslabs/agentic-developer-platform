# Project conventions — code review

Apply these standards when reviewing code.

## Mindset
- Correctness first — does the code do what it claims to do?
- Security always — scan for secrets, injection, auth bypasses, and insecure
  defaults.
- Maintainability — will the next developer understand this code in six months?
- Pragmatic — flag real issues; do not block on style preferences. Separate
  blocking concerns from suggestions.

## Conventions / quality bar
- All tests (existing and new) must pass.
- Error handling covers the failure paths, not just the happy path.
- No credentials, tokens, or secrets committed in code.
- When something is wrong, explain what is wrong and suggest a concrete fix.
- Judge changes against the conventions already established in the surrounding
  codebase, not personal preference.

## Engine attribution (mandatory)
When posting a review verdict comment, include an `**Engine**:` attribution line
indicating which engine produced the review:
- Codex CLI ran successfully → `**Engine**: Codex CLI <version>`
- Codex CLI failed, Claude finished → `**Engine**: Claude (Codex CLI failed: <reason>)`

## Remote branch review (issue #3301)
When reviewing a PR whose branch isn't checked out locally (the common case on a
worker pod sitting on `main`), pass the branch name as the head ref:
`review-diff <base-ref> <head-ref>` (e.g. `review-diff main agent/issue-1234`).
The wrapper fetches remote-only refs automatically — no manual checkout needed.
