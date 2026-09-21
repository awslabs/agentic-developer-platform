# PR continuity and historical delivery

A repaired story keeps its implementation pull request. Before dispatch consumes
another attempt, ADP confirms the existing immutable repository/PR identity and
current head with GitHub and checks that the accepted story scope is unchanged.
Within the dispatch transaction it moves the binding to the new attempt and
increments its revision. The previous run, head, scope, and registration actor
remain in append-only decision snapshots. The worker receives this PR in
`bound_pull_request` and must register its final head before handing off.

Old worker credentials cannot register after the node advances to another
attempt. Provider failures or a scope/repository change prevent dispatch, without
consuming another attempt. Review/check observations name binding revisions;
carrying the PR invalidates those observations and requires current evidence.
Existing attempt and policy limits are unchanged.

For implementation that was merged before a story was ever dispatched, an
operator with `PLAN_APPROVE` can POST the existing recovery endpoint with
`adopt_delivery: true`, an explicit repository/PR, and a reason. ADP verifies the
immutable identity, exact head, merge, checks, and accepted review evidence before
writing an attributed binding. `run_id` is NULL and the attempt remains zero.
There is no worker, queue message, synthetic dispatch, or direct database edit.

The human-only state edges `pending -> awaiting_merge` and
`ready -> awaiting_merge` reserve this verified delivery for reconciliation.
They do not satisfy dependencies. Result observation re-verifies the current
binding and GitHub evidence and waits for all graph predecessors, including
human gates, before passing the story. A policy-bound story stays held until its
execution-policy obligations are reconciled; adopting a merged PR does not prove
deployment or evaluation. Downstream deployment/evaluation/gate nodes remain
ordinary graph dependencies.

Adoption refuses dispatched attempts, active issue ownership, rejected/halted
nodes, and changed accepted scope. Concurrent adoption and dispatch serialize on
the same node lock. Repeating the same adoption is idempotent. Ordinary recovery
without `adopt_delivery` retains its existing current-dispatch association-only
contract. Migration `063_pr_binding_adoption` makes the binding run reference
nullable; downgrade refuses while historical bindings exist instead of inventing
run identities or deleting provenance.

## Delivery status

The story journey now shows the current stage, responsible actor, all known
blockers, the next action, and a scheduled controller check when one is actually
recorded. Provider checks distinguish failed, pending, and missing CI; review
state distinguishes requested changes, stale approval, missing approval, and
unverified evidence. A moved PR head, missing handoff, and unavailable GitHub
response have separate diagnoses. These fields are diagnostic and do not grant
merge or completion authority.

Provider evidence is shown only for the current binding revision. Its timestamp
is the recorded evidence observation, never an unrelated tick's attempt to check
the node. Execution scheduling is shown only for the current node cycle and
accepted execution policy. Human holds suppress schedules. Legacy flows state
that automatic review and repair are not configured, and historical adoption
states that no worker was dispatched while retaining predecessor and policy
holds.

## Shared-role model budget coverage

The model gateway authenticates `X-Adp-Report-Credential` against the durable run
assignment. Requests for an accepted shared continuation use the existing bounded
provider quote and atomic flow reservation accumulator, including developer,
reviewer, and repair calls. The original ASGI body frames are replayed unchanged,
and streaming responses keep the ordinary usage reconciliation path. Expired,
stale, or invalid supplied proof is rejected; upload completion rechecks both the
assignment and policy before spending.

A request naming a known shared-continuation run without its capability is
rejected. A valid report for a legacy flow with no execution policy retains
ordinary budget checks and server-derived run attribution. A present or malformed
policy never falls back to this legacy treatment. Human traffic, protected
workers, and unrelated legacy traffic keep their existing behavior.

This budget covers authenticated platform-run model traffic. It is not an IAM
isolation boundary: the accepted shared worker role retains its configured AWS
permissions, and a worker can omit run identifiers or use its administrator
credentials directly. The engine does not claim those requests are bound to an
accepted flow budget. A stronger provider boundary would require reducing the
role's access separately; it is not introduced by this compatibility path.
