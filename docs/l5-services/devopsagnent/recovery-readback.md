# Retained DevOps recovery readback

## Recover an investigation report

For an ADP-submitted investigation with a retained native task, use **Collect
existing report** on its run card, or
`POST /v1/products/{product_id}/projects/{project_id}/services/devops/runs/{run_id}/reconcile`
with a reason and an `Idempotency-Key`. Reuse the same key and reason after a
lost response. This queues observation or collection of that task; it does not
submit a replacement investigation. An active worker lease returns
`409 work_pending`; refresh the run before trying again.

After the execution deadline, an explicit reconciliation opens a fifteen-minute
readback window. The original deadline, input, native task and accepted attempt
remain unchanged. Current owner consent, connection, configuration, binding and
worker lease are still required. The broker grants read-only access to the
original space, bounded by the consent, lease and recovery expiry. Expired or
revoked consent is not extended. Cancellation and new submission are excluded.

If the original grant expired, **Collect existing report** records fresh,
task-specific read-only consent for up to fifteen minutes. The request includes
`authorize_readback: true`, `expected_revision`, the exact `native_job_ref`, and
`reason` on the same reconciliation endpoint. Keep the entire payload and key
for retries. The caller must have a current human owner session. The server
requires the original accepted task, unchanged configuration and binding, and
current Product and connection access. Revoked original consent cannot use this
continuation. The original grants and admission are preserved; a separate audited
grant can only read the selected report under a live worker lease. It cannot
start, cancel or modify investigations or schedules. This action needs no budget.

The run card displays collection failures and their next action, and follows
progress while report recovery is pending. A successful recovery retains the
native report in Results. Native-origin tasks and other DevOps capabilities do
not use this investigation recovery window.

Saved setup, bindings and results remain readable when resource verification
expires. Reading retained metadata checks current Product, source and connection
access and keeps the original resource and policy references. It does not refresh
provider verification or grant permission to run. Saving resource selections,
obtaining an access policy and starting new work still require fresh verification.

## Inspect retained recovery metadata

`GET /v1/products/{product_id}/projects/{project_id}/services/devops/runs/{run_id}/recovery-state`
returns existing SQL recovery metadata for an ADP-submitted DevOps run. The
response uses the same `run`, `attempts`, `work` and `publications` envelope as the
Security recovery export. The existing route's legacy OpenAPI operation ID is
unchanged. Responses are marked `Cache-Control: no-store`.

The caller must be the current human Product owner with a valid access JWT and
current source, connection, binding and delegated capability authority. The
run must match the requested tenant, Product, project, configuration revision
and binding generation. Authority is checked before and after collection;
changed access returns `404 not_found` without recovery records.

The export preserves original attempt IDs, idempotency tokens, input hashes,
first fences, work IDs, generations, fences and publication identities. It
includes both Product Context and Engineering Insights publication metadata.
Prepared provider requests, publication payloads, worker identities and raw
worker errors are excluded. Reads neither claim work nor alter attempts,
leases, schedules, results, setup operations or native jobs.

The complete response is bounded to 8 MiB, 8,192 work rows, twenty attempts and
twenty publications. The work bound accommodates the canonical 2,880-poll limit
and its separate 2,880-check submit reconciliation phase, plus lifecycle work.
Exceeding a bound returns `409 recovery_readback_incomplete` with the relevant
limit; records are never silently truncated or filtered to fit. Such histories
require a reviewed larger or paginated export, not deletion or a new run.
Two ordered SQL reads must agree;
concurrent changes return `409 recovery_readback_changed`. Retry the same GET
after the existing work settles. A successful read proves retained metadata,
not that recovery ran or that a native result is qualified.

This is one prerequisite for WA07/AC-01 and WA07/SCN-01. Installed Gateway
deployment and original before-transition, after-transition and after-recovery
observations remain required. The compatibility collector reads this complete
four-key envelope twice and compares it with the exact selected run, result,
scope and human reader. Its DevOps recovery read uses the 8 MiB streaming and
compact serialization bound while ordinary reads retain 64 KiB. Compatibility
checkpoints have a separate 9 MiB journal bound so long histories can be retained
without truncation. Original checkpoints without DevOps recovery remain readable
and incomplete; later observations never fill that historical gap.
Imported native runs, setup history, old CLI/MCP/UI journeys, worker/repair
transport, an actual rollback/recovery receipt, Product-origin linkage and native
acceptance remain outside this export. Preserve original board IDs and evidence;
do not manufacture missing historical checkpoints or restart native work to
substitute for them.
