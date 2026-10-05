# Workload cancellation

`POST /workspaces/{workspace_id}/batch-jobs/{job_id}/cancellation` and
`POST /workspaces/{workspace_id}/deployments/{dep_id}/cancellation` accept
`{"operation_id":"<original provision operation>"}`. The operation must match
the original stored workload and paid registration. The current caller needs
workspace provision permission; spend permission and a new approval are not
required to withdraw execution. Cancellation remains possible after workspace
suspension or profile removal. These routes make no provider calls.

Repeated requests reuse the existing shared cancellation record. They never
create an operation or retarget one. A completed operation keeps its outcome;
use the separately approved teardown API for an existing workload. The response
includes the authoritative operation state, `cancellation_requested`, cleanup
status and original identities. A lost reply can be retried with the same IDs.

The shared service fences creation and withdraws never-claimed dispatch before
releasing its budget. Only a closed fence at generation zero, released shared
and domain accounting, and no queue, provider call or allocation member permits
the domain to persist `CancelledBeforeDispatch` and return model GPU quota.
The workload tombstone and admission remain. Cleanup is `not-required` in this
case; observed cost is still null. This is proof of non-execution, not a fabricated
provider absence observation.

If dispatch has been delivered, a worker holds the lease, or effects exist,
cancellation alone does not return quota or assert cleanup. Shared execution
refuses new provider steps; outstanding calls retain their original evidence.
Recovery and the original-UID teardown/finalizer paths must establish the
outcome and complete owned absence. Until then cleanup remains `unconfirmed`.
The API does not turn cancellation into a provider delete or borrow new authority.

Remote PostgreSQL regressions exercise actual admission, grants, shared fencing,
outbox, ledger and quota; linked worker tests cover cancellation before provider
steps and during a provider reply. Provider transports are fixtures. Live
cancellation and workload acceptance remain separately required.
