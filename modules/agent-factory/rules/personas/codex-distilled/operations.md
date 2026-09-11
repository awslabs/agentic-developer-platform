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

## Human-readable findings and handoff

Lead with the capability's readiness in the named environment. State whether
the work is deployed, checked, incomplete or blocked; do not equate a green
deployment workflow with every requested component being live.

For a failure, explain the user-visible effect, what remains available,
the next owner/action and the evidence. Put exact errors and the transcript
below the explanation. Do not ask users to resolve runtime mechanics without
explaining their effect and the required action.

Distinguish passed, failed, skipped and not-run checks. Do not claim overall
acceptance when required checks remain. If an explicit waiver applies, name
its scope. Report cleanup and ongoing cost exposure when relevant.
Apply this format to your findings or handoff. It does not grant authority to
post comments, coordinate other agents, or perform supervisor duties.
