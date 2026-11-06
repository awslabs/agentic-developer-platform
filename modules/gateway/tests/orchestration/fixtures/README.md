# Emit-core preview fixture

`proposal.bounded.json` is an authored, Product-bound example with a code-only
wave: its story has a `develop` executor and no story has an `operate` executor.
`proposal.unbounded.json` is the policyless negative preview case, not an
acceptable emission. Both use fictional identities. Preview tests substitute
the resolved tenant in the request without changing the authored file and set
a fresh 14-day expiry for repeatable future runs.

## Step 7d lint review for the code-only wave

| Rule | Result | Reason |
|---|---|---|
| 1 — CI apply path | N/A | No `operate` executor in this wave; no infrastructure apply workflow is required. |
| 2 — Account and credential label | N/A | No `operate` executor in this wave; an AWS target is not an input to code delivery. |
| 3 — Maintained version pins | N/A | No `operate` executor in this wave; the operations-only pin review does not gate code delivery. |
| 4 — Hotfix branches | N/A | No `operate` executor in this wave; the operations hotfix protocol does not apply. |
| 5 — Live API contract | Evaluated; N/A | This story names no frontend TypeScript response type or backend endpoint, so the cross-boundary trigger is absent. |

A code-only draft missing an AWS credential label therefore does not fail Rule 2.
If a code-only wave does introduce a typed frontend/backend API contract, Rule 5
still applies and its evaluation must check the real response keys. A wave
containing an `operate` story runs Rules 1–4 as well as Rule 5.
