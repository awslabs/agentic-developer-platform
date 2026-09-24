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
capacity reservation, and any artifact claims. The immutable input digest must
be the RFC 8785 digest of the exact accepted public payload; caller artifact IDs,
immutable bootstrap bindings, and envelope references must agree exactly. A
cancelled transaction exposes none of them. Replays verify the locator, work,
envelope digest, and current task binding before returning the original task.

Artifact records use a gateway-derived
`tasks/<tenant-hash>/<principal-hash>/<artifact-id>/<version>` key. Unclaimed
uploads expire after 24 hours; admission atomically verifies owner, version,
digest, and content type, binds them to the task, and removes their TTL. Replays also verify the
gateway-derived object key, so an existing artifact ID cannot be retargeted.

Finite fractional and exponential JSON numbers are valid request data. Digests
use RFC 8785 number formatting and DynamoDB persistence converts Python floats to
exact decimal descriptors instead of rejecting otherwise valid JSON.

## Attempts and reports

`TaskStore.bind_runtime_attempt` atomically updates task metadata, run history and
the protected run grant under the prior-attempt, task-version, policy and binding
fences. `TaskStore.append_report` commits the `TASK_REPORT` deduplication row,
next ordered event and sequence counter in one cross-table transaction. The
transaction checks the exact current task binding, policy version, runtime
attempt, and immutable run-grant input/model/limit/capability values. The grant is
read consistently, verified against its protected digest, then compared again in
the mutation transaction so a racing protected-field change commits neither
report nor event. An identical report UUID returns its original receipt; changed
content conflicts; stale generation/attempt or racing revocation commits neither
report nor event. Generic transitions cannot rewrite owner, input, generation,
digest, artifact or attempt fields, and result/outcome fields require a current
attempt binding.

## Separate leases

Recovery uses `recovery_lease_token` and `recovery_lease_expires_at` for 45
seconds. Before mutation it recomputes the sparse due key from the persisted work
UUID and due timestamp, rejects future work, and conditionally compares both
values so an inconsistent or stale index projection cannot authorize an early
lease. Publication uses the independent `publication_lease_token` and
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
Expired/superseded tokens fail the same transaction that would settle work and
advance an accepted task, so a stale token cannot mutate state or event history.
Queue settlement never regresses a running or terminal task.

The adapter-facing repository values match the closed HTTP bodies without adding
a tenant or caller-selected task identity: recovery claims return only `work_id`,
`task_id`, `kind`, `due_at`, `lease_token`, and `lease_expires_at`; dispatch claims
return the exact envelope and publication lease; dispatch settlement returns
`dispatch_id`, `queue_ack_status`, and `task_status`; recovery settlement returns
`work_id`, `operation_status`, and `task_status`. Dispatch publication changes an
accepted task to `queued` while queue acknowledgement remains `pending`; SQS
message deletion is separate evidence and is never fabricated from send success.

Reusable scenario IDs and exact test selectors are versioned in
`storage-fixtures-v1.json` and registered in
`docs/task-api/evaluation-manifest.json`.

### Finite JSON numbers and physical DynamoDB encoding

The public JSON contract is not restricted to DynamoDB's numeric exponent range.
Repository JSON columns normally remain native DynamoDB maps/lists. If a known
JSON column contains a finite number that DynamoDB cannot represent, its physical
value is RFC8785 canonical UTF-8 JSON text. The server-owned root attribute
`_task_json_encoding_v1` lists exactly those encoded column names. The repository
hydrates them before returning records or computing grant/request digests.
Nested caller objects, including objects containing that same field name, never
act as encoding metadata. Unknown marker columns and noncanonical text fail
closed. Ordinary records remain physically unchanged.

`src/tasks/json_storage.py` owns the closed per-record column list: task input and
result/error fields, run results, grant input/model/limits, event data, command
payloads, turn input/messages, and model-operation JSON content/receipts. Integers
used for versions, generations, counters and keys remain native and bounded.
Native integral values outside the RFC8785 safe integer range hydrate as JSON
floats so a value such as `1e20` retains its canonical digest.

Complete rows pass through `_serialize` or `_serialize_authority`; reads pass
through `_deserialize`. Direct JSON column updates must call
`encode_json_updates(snapshot, updates)` and persist **all** returned entries,
including any changed encoding metadata, in their existing version/authority
transaction. The known `:grant_input`, `:grant_model_binding` and `:grant_limits`
condition operands receive the same physical encoding as protected grant columns.
This preserves equality fences instead of comparing hydrated JSON to encoded
storage. Active TTL and legacy-index isolation rules are unchanged.
