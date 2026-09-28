# Delivery stage attempt allowances

The accepted policy's `max_attempts_per_node` is the maximum number of attempts
**per stage of a node**. The wire name is retained so existing accepted plans and
clients remain compatible. Development, review, repair, merge, deployment and
evaluation each have an independent allowance. For example, three developer
attempts under a limit of three leave three review attempts available.

Development admissions retain `OrchestrationNode.attempts`. Durable continuation
reservations record `attempt_stage` in their action detail. Existing reservations
are classified by their established action kind and, for review-cycle dispatch,
the recorded `action`. Counts include all cycles of the node, failed actions and
unknown outcomes. Evidence receipts and notifications do not spend this allowance.
A current unresolved reservation is excluded only when reauthorizing that exact
operation; a settled failure cannot regain an attempt by reusing its key.

The cumulative execution counter remains audit history. Policy admission, runtime
reauthorization, review recovery and the runner ceiling use the stage counter.
Resume, process replacement and plan amendments do not reset stage history.
The execution API exposes `stage_attempts` for display, aggregated independently
of action and execution pagination. Shared spend, elapsed-time authority,
concurrency, claims and human approvals retain their existing scope.

Developer retries remain development-stage admissions even when the retry's
permission is `repair`. A durable review-cycle repair is a separate repair-stage
reservation. Codex can review and repair within one worker invocation; that is
one review attempt, not an attempt per model/tool call.

Once the developer publishes the implementation PR it returns to the engine.
It does not run more tests, poll CI or enter a repair loop. Local validation
receipts do not gate handoff of the published PR; Codex owns validation, repair
and merge. Publication identity and preservation of uncommitted work remain
part of the handoff protocol.
