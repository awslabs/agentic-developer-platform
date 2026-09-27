# Automatic recovery after developer failure

The engine previously settled a failed developer run but required a human to move the story from failed to ready, even when its accepted policy allowed another repair attempt. A new developer recovery pass runs after result settlement and existing review recovery, before normal dispatch.

The pass accepts only engine-observed, authenticated failures with a concluded execution and the exact released claim generation. It refuses live or changed ownership, uncertain effects, and recorded review continuations. Protected terminal outcomes for cancellation, abort and budget stop cannot become retries; a shared report explicitly identifying policy, provider refusal, contract or cancellation failure also remains blocked.

Recovery requires the engine feature to be enabled, an active unpaused flow, an unchanged accepted plan, autonomous repair permission, a valid policy and execution window, and remaining attempts. It uses the existing allowance: two maximum attempts means one initial run plus one retry. It does not add a runtime cutoff, reset counters, extend authority or enable budget enforcement. Normal dispatch still performs current identity, membership, prerequisites, claims, budget and provider checks before consuming the next attempt.

A first retry waits at least 60 seconds from the recorded failure; later waits increase exponentially to 30 minutes. The decision is durable and keyed to the failed node/attempt. Node locks and the unique decision identity prevent concurrent ticks from scheduling duplicates. Existing failed stories are considered, subject to the same checks.

The retry envelope carries prior run, failure decision, category and exit-code references when available. Worker prompt guidance requires preserving existing branch/PR work, using prior evidence, and changing the investigation when no new evidence is being found. Missing prior logs must be acknowledged; the engine does not manufacture a diagnosis. Existing branch adoption and provider-verified PR carry-forward remain responsible for repository preservation.

The graph explains a pending retry or a concrete blocker instead of always claiming human approval is required. Unknown recovery evidence is rechecked without starting another worker. Halts and gates retain their existing human-only controls. The state-machine exception is internal to verified recovery, not a new public service resume permission.

Validation covers real PostgreSQL settlement and recovery, concurrent ticks, exhausted/expired/paused policies, changed claims and plans, unresolved effects, protected cancellation/refusal, next-dispatch PR/context retention, worker environment isolation, and prompt parsing. No live flow is restarted by the development or tests for this change.

## Renewing an elapsed execution window for a live retry

The existing authenticated `/orchestration/flows/{flow_id}/window/preview` and
`window/accept` controls also support protected-worker flows. The original human
platform administrator previews and accepts a receipt bound to the current plan
version, content hash and observed deadlines. Protected flows retain their first
committed dispatch as the elapsed-time anchor; renewal never restarts that clock.
An already future policy expiry may remain unchanged when explicitly increasing
the wall-clock ceiling by at most 24 hours (seven days total maximum). Expiry
extensions retain the existing 24-hour bound and expired policies still require
explicit reacceptance. Malformed continuation markers retain the shared-path
refusal instead of falling back to protected authority.

Renewal leaves the accepted plan, node attempts, released claims, concluded
executions and budget enforcement setting intact. The next scheduled tick must
independently pass automatic recovery and ordinary dispatch admission. Renewing
time does not authorize retries after exhausted attempts or policy refusals.
