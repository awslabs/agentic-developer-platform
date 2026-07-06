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
