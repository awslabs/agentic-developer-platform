# T1 storage repository contract v1

This additive contract implements issue #5794's 2026-09-24 execution
clarification without changing the frozen HTTP schemas. T3 owns the four HTTP
adapters and queue send; it consumes `modules/gateway/src/tasks/store.py`.

## Work identity and lookup

- Every work UUID resolves through the protected authority-table key
  `pk=TASK_WORK_ID#<work_uuid>`, `sk=BINDING`.
- A locator stores immutable work kind, tenant, task, invocation/generation when
  applicable, and the exact request-table `event_id`/`arrived_at` key.
- Dispatch `work_id` is its stable UUIDv4 `dispatch_id`. Other work kinds get a
  separate UUIDv4 and keep the existing `RECONCILE` request-table sort key.
- Replacement revokes the old locator and atomically creates a new locator. It
  never changes the old locator's target, and the changed work UUID invalidates
  every old lease condition.
- The sparse `task-work-index` discovers candidates only. Every mutation first
  reads the locator, request record, and current task binding by exact primary
  key; a missing, stale, revoked, or mismatched record fails closed.

## Acceptance and envelope

`TaskStore.accept` submits one stable-token DynamoDB transaction containing the
idempotency row, task/run/event/work rows, complete schema-valid envelope,
protected envelope digest, locator, task binding, run grant, policy condition,
capacity reservation, and any artifact claims. A cancelled transaction exposes
none of them. Replays verify the locator, work, envelope digest, and current task
binding before returning the original task.

Artifact records use a gateway-derived
`tasks/<tenant-hash>/<principal-hash>/<artifact-id>/<version>` key. Unclaimed
uploads expire after 24 hours; admission atomically verifies owner, version, and
digest, binds them to the task, and removes their TTL.

## Separate leases

Recovery uses `recovery_lease_token` and `recovery_lease_expires_at` for 45
seconds. Publication uses the independent `publication_lease_token` and
`publication_lease_expires_at` fields. For dispatch recovery the required order
is:

1. claim recovery work and retain its recovery token;
2. claim dispatch with `dispatch_id=work_id` and receive the exact envelope;
3. publish with FIFO deduplication ID `dispatch_id` and message ID
   `invocation_id`;
4. settle publication with its publication token and actual SQS message ID;
5. settle recovery with the original recovery token.

Recovery settlement reads committed publication state and SQS evidence. The
request's boolean observation cannot create send evidence or advance a task.
Expired/superseded tokens fail their conditional write, and queue settlement
never regresses a running or terminal task.

Reusable scenario IDs and exact test selectors are versioned in
`storage-fixtures-v1.json` and registered in
`docs/task-api/evaluation-manifest.json`.
