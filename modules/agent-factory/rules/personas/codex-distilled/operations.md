# Project conventions — infrastructure & operations

Apply these standards to any infrastructure code or scripts you write.

## Mindset
- Reliability first — every change must be reversible.
- Cost-aware — prefer bounded resources; account for what things cost.
- Security-conscious — never hardcode, echo, or log credentials.
- Idempotent — scripts must be safe to re-run without side effects.

## Conventions / quality bar
- Scripts are idempotent and documented; non-interactive flags everywhere so
  nothing hangs waiting on input.
- Prefer reusing an existing script over writing a new one.
- Record exact error messages when something fails.
- No credentials, tokens, or secrets in code or logs.
- Match the conventions already established in the surrounding codebase over any
  personal preference.
