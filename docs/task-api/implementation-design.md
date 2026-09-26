# Task API implementation contract

**Revision:** 2026-09-24, accepted by the project owner in the design session.
**Source reviewed:** `c4635f45a26598082b20bdb0c492067b8b14c827`.
**Architecture:** [D01–D18](README.md#2-decisions-of-record).

The project owner has directed that design be completed here before the engine
starts development. This document supplies the accepted answers to O1–O8. The owner approved the
design and requested the plan refresh on 2026-09-24. That approves this design
baseline; it does not assert implemented capabilities or measured results, or
approve the engine execution gate. The engine must implement this contract as a
whole rather than resolve its architecture separately in each story.

## 1. Design and execution boundary

The handoff is an accepted, immutable source revision containing this contract,
the architecture, evaluation criteria and implementation scopes. The engine then
implements code, creates tests, repairs defects and submits work for independent
evaluation. T0 implements schemas, fixtures and conformance tooling against this
contract. V0 checks that implementation; it does not make product decisions.

All T0–T8 assignments must cite that same design revision. A material conflict,
unsupported prerequisite or required contract change returns here with the
affected decision/criterion and evidence. The engine must not generate a new
architecture, lower a limit, replace an acceptance criterion or start an AI-DLC
inception/design workflow to resolve it. Local function structure and test
implementation remain normal coding decisions. Repair loops stay within the
accepted contract and execution policy.

No new engine enforcement capability is asserted by this document. Before
execution, verify the actual developer assignment contains the pinned design and
these instructions; automatic AI-DLC reauthoring must not be enabled for this
flow. A title alone does not enforce this boundary.

## 2. Decision register

| Decision | Accepted answer | Rationale and implementation owners |
|---|---|---|
| O1 | `agent-task-investigator`: Node.js 22 / TypeScript, independent package, native JSON process protocol and host-mediated model requests; no agent SDK dependency. | A bounded investigation of supplied text/JSON evidence exercises useful progress, clarification and results without GitHub, shell tools or customer credentials. T4/T5 implement it; T3 registers its model compatibility. |
| O2 | Cognito client-credentials access tokens, exact `cognito_m2m` alias resolution to ADP's canonical service principal, gateway-owned authorization for both ingress and read/control paths. | Uses the existing identity system without duplicating its SQL directory in Lambda. T2/T3. |
| O3 | Distinct `TASK*` namespaces in the existing request table; protected task/run grants in the existing authority table; gateway is the task writer; explicit worker IAM denies. | Cross-table DynamoDB transactions can commit metadata and authority together. Legacy readers and writers remain separate. T1/T3/T6. |
| O4 | Transactional acceptance/outbox, stable FIFO dispatch ID, scheduled recovery through an alias of the existing Lambda, protected generation fencing. | Lost HTTP or SQS acknowledgements cannot lose accepted work or authorize a second active execution. T1/T3/T4. |
| O5 | TokenReview-bound queue acquisition and task-specific service-rooted bootstrap/grants; model policy and spend enforced at the gateway. | Existing repository/human-rooted helpers cannot be called with invented fields. T3/T4. |
| O6 | Incremental NDJSON over child stdin/stdout, host-authenticated gateway reporting, persisted events and conversation turns, separate logical-consumption and model-handoff receipts. | Durable input is an explicit extension of the remote-control contract. T4/T5/T6/T7. |
| O7 | The fixed pilot limits in section 10. | Bounded qualification before wider use; implementations may enforce stricter existing platform policy but cannot relax these limits. T1–T8. |
| O8 | Default-off pilot, fully compatible shared consumers before publication, controlled coexistence, drain-before-rollback; deployment handoff as described in section 11. | Existing schedules and an unresolved deployment manifest cannot establish readiness. T3/T8. |

## 3. First useful agent and model access

The investigator reads caller-supplied evidence, identifies likely causes with
citations, asks for clarification when necessary and produces a report containing
`summary`, `findings`, `uncertainties`, `recommendations` and `evidence_refs`.
Each finding names the evidence supporting it; absent evidence produces an
explicit uncertainty. Caller acceptance criteria are task content, not authority.

Stages are evidence inventory, analysis, optional clarification and synthesis.
Progress names evidence inspected and findings so far; no fabricated percentage
or private model reasoning is emitted. The first useful scenario is a service
incident investigation using synthetic logs and configuration supplied by the
client. It has no checkout, external URL fetching, shell execution, arbitrary
MCP tools, infrastructure mutation or privileged approval step.

Package in `modules/agent-factory/task-agents/investigator/`, with its own build
and lockfile. The command allowlist maps `agent-task-investigator` to that built
entrypoint with `--embedded`. Node's built-in streams provide process I/O. Model
requests travel over the process protocol to the Python host; the child receives
no AWS, GitHub, gateway or customer credential in its arguments/environment. The
host-child boundary is a protocol boundary, not a sandbox against hostile code
in the shared container; v1 executes only the trusted allowlisted package. Existing agent packages and their
dependency versions are unchanged.

The host calls a task-scoped gateway model adapter using the existing Anthropic
Messages request/response format and platform proxy service. This deliberately
uses the implemented Messages/InvokeModel transport, not an assumed Converse
endpoint. The initial compatibility fixture uses the catalogued canonical model
`global.anthropic.claude-haiku-4-5-20251001-v1:0`. This is the selected pilot model,
not proof that a selected tenant can invoke it. The persona must have a registered
request shape, a permitted model-policy resolution and an invocability result
before admission. A missing eligible model blocks readiness; the worker cannot
choose a fallback or bypass the gateway.

Resolve the canonical service principal's persona mapping, permitted model set,
request shape, current pricing evidence and budget at admission. Save their
versions with the run grant. Revalidate restrictions and reserve a conservative
request cost before every model call; deny when pricing or the upper bound is
unavailable. Do not run the unrelated pricing-refresh schedule as a task side
effect. Model usage names the service principal, task, run and model, preserving
unknown usage when a provider response is lost.

## 4. Authentication and service authority

### External credentials and permissions

Use an existing tenant's Cognito user pool and a confidential app client with
`client_credentials`, 15-minute access tokens, and the Task API resource-server
scopes `adp-tasks/submit`, `adp-tasks/read`, `adp-tasks/input`,
`adp-tasks/cancel` and `adp-tasks/artifacts`. Client registration/rotation follows
the existing authorized service-registration path. Register its exact verified
`client_id` as an active `cognito_m2m` alias of one canonical service principal in
that tenant using the existing human-org-admin
`POST /service-principals/register` route (`alias_source=cognito_m2m`,
`alias_id=client_id`). Its status and alias-revocation routes remain the identity
lifecycle authority. The Cognito approved-client list alone is insufficient.

Validate JWT signature, allowlisted issuer, `token_use=access`, expiry, client ID
and required scope. Do not require an ID-token `aud` on an access token that has
none; if an audience is issued it must match the configured resource audience.
Tenant resolution comes from the trusted authentication/client registration;
then use the exact tenant/source/alias lookup in the canonical principal service.
No fallback to `sub`, a body service name or a legacy EventBridge alias is allowed.

V1 is service-owner-only. The owner can read its tasks and artifacts; same-tenant
services have no implicit access. There is no public delegation, human-admin
override or IAM/SigV4 external submit adapter in v1. Administrative policy and
registration stay in their existing platform surfaces. These restrictions do
not change the legacy API's authentication behavior.

Every operation checks the active alias, canonical principal, task ownership and
current task policy. Streams repeat the check at most 15 seconds apart and close
by token expiry or within 30 seconds of revocation. No positive service-policy
cache may extend that bound. An unavailable authorization dependency denies new
operations and closes streams; it cannot turn into anonymous access.

### Lambda/gateway boundary

The explicit main API Gateway `POST /v1/tasks` integration calls the existing
ingress Lambda's lazy task handler. Lambda applies byte/shape limits and forwards
the request to the new gateway `POST /internal/v1/tasks/admit` adapter over the
existing main API Gateway's internal TLS/SigV4 route. `Authorization` carries the
producer's SigV4 signature; the original bearer token is forwarded separately in
`X-Adp-Task-Caller-Token`. Lambda derives that header only from the public request's
Authorization token and ignores any caller-supplied forwarding header. The gateway
validates the forwarded token with the same canonical-principal validation service
used by task reads and controls, and owns the acceptance transaction. Lambda
coordinates dispatch after that commit.
It does not open a SQL connection or maintain another identity cache.

The internal call also requires the ingress role's existing STS producer-proof
mechanism. Bind the proof to SHA-256 of method, route, original token digest,
idempotency key and exact request bytes using a versioned length-delimited
encoding. Allowlist only the ingress producer role for admission. This extends
the existing body-bound proof pattern in `internal/persona_model_selection.py`;
a worker key or a body-supplied principal cannot substitute. Never log the token,
proof, client secret or unrestricted task body. Missing connectivity or proof
validation is a readiness failure, not a reason to relax authentication.

### Task grants

Introduce a task-specific standing policy under the existing protected authority
store: `pk=TENANT#<tenant>`, `sk=TASK_POLICY#<canonical_principal>`. It references
the existing identity and contains status/version, allowed personas, task scopes,
resource constraints, model-policy version, duration and spend limits. It is
authorization data, not a new service directory. Only an authenticated human
organization administrator with the existing
`ORG_UPDATE` permission and same-tenant canonical-principal validation can write
it through new `GET/PUT /service-principals/{canonical_id}/task-policy` adapters.
PUT requires `expected_version`, validates all values against platform ceilings,
and atomically writes a protected audit entry and policy version. Services cannot
self-grant task authority. These additive adapters are T3 work, following the
existing persona-model admin gate. Existing service principals are denied task
execution until that explicit task policy exists.

Task authority uses a typed `task_service_policy` adapter with canonical service
ID and policy reference; it does not fill `human_id`, repository or installation
fields to satisfy the existing EventBridge grant. The bootstrap and settlement
rows also retain the admitted alias ID/version so alias revocation can stop that
root even while another alias of the canonical principal remains active. One run
grant binds task,
request digest, persona, model snapshot, resource bounds and workload generation.
Admission creates the grant atomically with task metadata. SQL identity is
rechecked immediately before the transaction and again before publication,
bootstrap, commands and model access; no cross-SQL/DynamoDB atomicity is claimed.
Revocation after acceptance stops further work through those checks.

The durable standing policy authorizes execution past the submit token's expiry,
within the task deadline. A queued input command additionally expires with its
authorizing token; it cannot be applied under a later login automatically.
Cancellation already durably accepted continues stopping work even if that token
expires. Ordinary input never grants another resource or approval.

## 5. Public contract and identity

All JSON requests use `schema_version: "1.0"`, reject duplicate/unknown fields,
nonfinite numbers and invalid UTF-8. IDs are opaque: `tsk_<UUIDv4>` for tasks,
UUIDv4 for invocations and commands, `art_<UUIDv4>` for artifacts. Numbers used
for ordering are positive integers; server timestamps are UTC RFC3339.

| Operation | Request and result |
|---|---|
| `POST /v1/tasks` | Required `Idempotency-Key` (1–128 printable ASCII characters), persona and nonempty instructions. Optional `inputs` JSON object, `artifact_ids` array, `external_reference`, `acceptance_criteria` string array. Response `202` contains task/run IDs, current status and status/events URLs. Same key/body returns the same task; different body returns `409 idempotency_conflict`. |
| `GET /v1/tasks/{task_id}` | Strongly consistent snapshot: task/run IDs, status, version, created/updated/deadline times, latest/oldest event cursors, execution generation and runtime attempt where known, execution health, result/error, input request, command receipts and queue-ack status. Unknown evidence is nullable, never zero/success. |
| `GET /v1/tasks/{task_id}/events` | Durable SSE, with `Last-Event-ID` or an `after` cursor. Supplying conflicting cursor forms returns `400`. Section 9 defines replay. |
| `POST /v1/tasks/{task_id}/messages` | `command_id`, nonempty `text`, optional `reply_to` input-request UUID. Returns `202` with receipt. Inputs are queued FIFO for a safe turn boundary; a stale `reply_to` conflicts. At a full queue return `429`; after cancellation/terminal state return `409`. |
| `POST /v1/tasks/{task_id}/cancel` | `command_id`, optional bounded `reason`. Returns `202` while cancellation is pending, or `200` with an existing terminal outcome. Same intent retries keep the command ID. |
| `POST /v1/task-artifacts` | Authenticated binary text/JSON upload, with content type and SHA-256 digest. Gateway derives owner and storage location; returns artifact ID/version/digest. No caller bucket/key/URL. |
| `GET /v1/tasks/{task_id}/artifacts/{artifact_id}` | Authorizes task and exact immutable artifact binding, then streams bytes through the gateway with periodic access checks. No long-lived presigned download URL. |

Artifact uploads require the artifacts scope; access through a task also requires
read and ownership. Inputs may use uploaded artifacts belonging to the same
principal/tenant. The gateway stores them in the existing artifact bucket under
`tasks/<tenant-hash>/<principal-hash>/<artifact-id>/<version>` and pins their
digest/version at admission. Referenced artifacts receive the task's retention;
unclaimed uploads expire after 24 hours. T6 owns these additive artifact routes.

Normalize optional defaults, then use RFC 8785 canonical JSON and SHA-256 for
request/command digests. Idempotency scope is tenant + canonical principal + key;
external references are correlation data only. Public requests cannot specify
tenant, owner, executable, model override, generation, attempt, queue, grant or
transport credentials. A command UUID is unique across input and cancellation within its task; reuse
with another kind or different payload conflicts. A task retry must use the same key even after a lost
response. A terminal task is never reopened by that retry.

Errors contain `code`, safe `message`, `request_id` and optional `retry_after_ms`.
Use `400` invalid request, `401` invalid credential, `403` disallowed scope/persona,
`404` absent or invisible resource, `409` conflict, `410` expired retained history,
`413` oversize, `429` limit and `503` unavailable prerequisite. Clients retry
`429`/`503` with bounded jitter and the original idempotency/command ID; a timeout
is not permission to create a fresh intent.

### Runtime identities

Keep task ID, invocation ID, worker registration `generation`, opaque
`runtime_attempt_id`, command ID and model `turn_id` separate. Pod/Job UIDs are
internal workload-instance bindings. Generation increments on a verified worker
replacement; an in-process attempt change never impersonates a new generation.
The public API does not accept pod names or addresses as command targets.

V1 has one invocation per task. Pre-model worker recovery can replace its
generation with history retained. There is no automatic new invocation to hide
uncertain execution. Runtime adapters replace/invalidate attempt endpoints before
disposal and discard stale callbacks, following the remote-control contract.

### State transitions

| Current state | Permitted next states and evidence |
|---|---|
| `accepted` | `queued` after confirmed publication; `running` if a valid consumer starts before the publisher records its acknowledgement; `cancel_requested`; `failed` on definitive admission recovery exhaustion. |
| `queued` | `running` after protected bootstrap; `cancel_requested`; `failed` on deadline/exhaustion with no active execution. |
| `running` | `waiting_for_input`, `cancel_requested`, `completed`, `failed`. Completion needs committed result and validated process exit. |
| `waiting_for_input` | `running` after a committed next turn; `cancel_requested`; `failed` at the deadline after confirmed stop. |
| `cancel_requested` | `cancelled` only after confirmed stop/no execution; `failed` only for a separately proven terminal failure. Never `completed`. |
| `completed`, `failed`, `cancelled` | Immutable terminal outcome; later queue acknowledgement/cleanup evidence may be appended without changing it. |

Task version is a compare-and-swap fence. If completion commits before a cancel
request, cancellation returns the existing completion. If cancellation commits
first, completion is refused. Heartbeat loss sets `execution_health=unknown` and
`recovery_required=true`; it does not assert exit or change the task to success.

## 6. Storage and integrity

The existing request table keeps physical keys `event_id` and `arrived_at`.
Each new item has `record_type`, schema version and nested `scope` metadata.
It omits the legacy GSI attributes `tenant_id`, `user_id`, `correlation_id`,
`root_human_id` and `engine_command_status`. V1 has no legacy Activity GSI projection. A separate owner list uses an atomic
`TASK_OWNER_BINDING` record in the existing authority table, keyed by tenant and
owner digest plus admission time and Task ID. The owner-only Activity route
`GET /me/agent-invocations/tasks` queries that prefix and reauthorizes every
canonical Task; it does not scan, list foreign runs, or backfill historical Tasks. Owner-only direct-ID Activity detail
and retained-report reads locate the canonical Task through its admission run-grant
binding and reapply current Task authentication, ownership and policy checks.
No task item uses the raw invocation ID as its `event_id` partition.

| Record | `event_id` | `arrived_at` |
|---|---|---|
| Task snapshot/counters | `TASK#<task_id>` | `META` |
| Run history | `TASK_RUN#<task_id>` | `RUN#<invocation_id>#GEN#<10-digit-generation>` |
| Ordered events | `TASK_EVENTS#<task_id>` | `SEQ#<20-digit-sequence>` |
| Commands/receipts | `TASK_COMMANDS#<task_id>` | `CMD#<command_id>` |
| Canonical conversation | `TASK_TURNS#<task_id>` | `TURN#<20-digit-turn-number>` |
| Model operation/receipt | `TASK_OPS#<task_id>` | `MODEL#<turn_id>` |
| Request idempotency | `TASK_IDEMP#<SHA256(tenant,principal,key)>` | `META` |
| Publication/recovery | `TASK_WORK#<task_id>` | `DISPATCH#<dispatch_id>` or `RECONCILE` |
| Producer event deduplication | `TASK_REPORT#<task_id>` | `REPORT#<generation>#<report_id>` |
| Artifact binding | `TASK_ARTIFACT#<artifact_id>` | `META` |

Hashes use versioned length-delimited components, not ambiguous concatenation.
Event numbers are allocated by a conditional transaction updating META and
putting the event. Retry uses a stable report UUID and digest; same UUID/different
content conflicts. Failed transactions do not consume a sequence. The task state
transition and its event commit together. Commands allocate their own monotonic
command sequence, independent of random UUID ordering.

Add one sparse `task-work-index`: partition attribute `task_work_shard` equals
`v1#<00..15>` from the task hash; sort attribute `task_due` is a fixed-width epoch
millisecond time plus task/work ID. Only work records carry these fields. Recovery
queries due items, then reads primary records consistently and claims them by
version/lease. Index lag can delay discovery but never authorize a mutation.
Pagination is bounded and its continuation retained; use no table scan.

The gateway commits acceptance with a DynamoDB transaction across the existing
request and authority tables in the same account/region: idempotency item,
metadata, run record, first event, outbox, task/run grant, protected dispatch
digest, policy condition and task-capacity/budget reservations. Use a stable
transaction token and consistent idempotency read after ambiguous responses.
Protected task bindings use `pk=TENANT#<tenant>`, `sk=TASK#<task_id>` and task-run
grants use `sk=TASK_RUN#<invocation_id>#GEN#<10-digit-generation>`. The existing
`pk=INVOCATION#<invocation_id>, sk=DISPATCH` lookup remains compatible with protected
queue acquisition. Model operations store bounded response/reference data and
usage/reservation status in TASK_OPS; transcripts reference immutable large bodies
rather than exceeding the item limit. Task-capacity reservations live under
`pk=TASK_CAPACITY#<scope-hash>`, `sk=ACTIVE` in the authority store, with per-task
reservation IDs. Reserve nonterminal capacity at acceptance, execution capacity
at bootstrap, and release each reservation once at confirmed terminal settlement.
Budget reservations use the existing platform budget store/services; any separate
budget reservation created before the acceptance transaction is bound by ID and
expiry, and automatically released if acceptance does not commit. An unconfirmed
reservation cannot support acceptance or a model call.
Payload limits keep the transaction below DynamoDB item/transaction limits.
No partial prepared task is publicly accepted. The gateway writer role gets
only the additional required permissions; Lambda does not get authority-table
write permission.

Before admission, deny all worker roles direct writes to every `TASK*` namespace
above and to protected authority records. Include Put/Update/Delete, batch and
transactional write paths; use `ForAnyValue:StringLike` on DynamoDB LeadingKeys
for a deny so a mixed legacy/task batch cannot bypass it. Task hosts report via
the authenticated gateway only. Legacy gateway write adapters must also reject
reserved task prefixes and cannot use their service role as a deputy to bypass
the IAM deny. Deny worker direct access to the task artifact prefix; uploads and
downloads go through run-bound/task-owner routes. Audit the actual deployed roles,
including alternate credential paths; test both permitted legacy writes and
denied task writes against AWS. If that boundary cannot be proven, admission
stays off. A Python helper or record discriminator alone is not the boundary.

## 7. Dispatch, worker assignment and recovery

After acceptance the Lambda asks the gateway for the committed dispatch envelope
and publishes it using the existing queue. The envelope contains
`kind="adp.task"`, `schema_version="1.0"`, `task_id`, `invocation_id`,
`message_id=invocation_id` (the existing protected receive adapter's identifier),
`persona`, `dispatch_id`, `request_digest`, immutable input reference and protected
assignment reference. `message_id` is not an SQS receipt or transport ID.
The producer persists its exact envelope digest before publication.

FIFO `MessageGroupId` is a hash of tenant + task; `MessageDeduplicationId` is the
stable dispatch UUID. The gateway confirms publication by dispatch ID and advances
accepted to queued conditionally; a late acknowledgement cannot regress running
or terminal state. SQS's deduplication window is not the task's idempotency window.

Recovery runs every 60 seconds through a new `task-recovery` alias of the existing
Lambda. The scheduled target has a source-scoped invoke permission; the public
API integration cannot invoke that alias. Verify invoked alias/context before
accepting a recovery event; an HTTP body naming recovery is never sufficient.
The alias calls producer-proof-protected gateway work-claim/settlement routes;
the gateway owns work records and authority. This is a separate task recovery
schedule, with no change to orchestration or pricing schedules.

Protected queue acquisition uses the existing TokenReview-bound receive/
heartbeat/ack mechanism. Add a narrow task discriminator adapter; legacy
envelopes keep their current validation. A pod receives one immutable assignment
and never a queue receipt. Bootstrap additionally compares committed task,
envelope digest, live policy, request digest and persona, then conditionally binds
the run generation to the verified Pod/Job UID. Body fields do not select authority.
The task branch executes before any GitHub preparation or finalization.

| Interruption | Required recovery |
|---|---|
| Before acceptance commit | No `202`, no publication; retry original key. |
| Transaction commits, HTTP response lost | Consistent idempotency read returns the original task; scheduled recovery owns publication. |
| SQS send fails or response is lost | Retry same dispatch envelope/ID. Conditional bootstrap prevents concurrent execution even after FIFO deduplication expires. |
| Send succeeds, queued-state write fails | Consumer may proceed from committed accepted state; reconciliation records delivery without state regression. |
| Duplicate delivery while an owner is live | No second bootstrap; settle only that duplicate receipt, without changing the active owner's grant or acknowledgement. |
| Owner lost before any model send | Revoke old grant, prove workload termination, then allow a bounded new generation under the same invocation. Lease expiry alone is insufficient. |
| Model request may have been sent, no durable result | Preserve `handoff=unknown`; never automatically send it again. Reconcile retained result if available, otherwise stop/fail with `model_outcome_unknown` after confirmed workload exit. |
| Result/terminal state committed, SQS delete uncertain | Retry/reconcile queue acknowledgement only; a redelivery observes terminal state and cannot rerun the task. Record acknowledgement as unknown until confirmed. |
| Recovery bound exhausted | Visible failure if no execution remains; otherwise visible recovery-required state and operator intervention. Never quietly abandon or invent an exit. |

Replacement invalidates old run credentials and attempt endpoints first. Prove
termination from Kubernetes workload-instance evidence, not a reusable pod name
or missing heartbeat. A current owner can append only allowed reports for its
bound task/generation. Gateway model, artifact and control adapters revalidate
the same fence immediately before external operations.

## 8. Process, input and cancellation

The host and child exchange one JSON object per newline, incrementally, with
`protocol_version=1`, `type`, `request_id` and task/run/generation/attempt identity
where applicable. Stdout is protocol-only; bounded sanitized stderr is diagnostic.
Host-to-child types: `start`, `turn`, `model.result`, `report.ack`, `cancel`.
Child-to-host types: `ready`, `progress`, `model.request`, `input.required`,
`result`, `cancelled`, `error`. The host validates each message, owns persisted
receipts and authenticates all gateway calls. It must not buffer output until exit.
Invalid protocol, identity mismatch or a missing valid final result fails the run.

Implement a task-local adapter compatible with the neutral `ControlRuntimeAdapter`
semantics documented in [remote control](../../doc/agent-remote-control/README.md).
Keep SDK types out of it and run the applicable provider-independent contract
fixtures. Reuse narrowly copied protocol/types with source provenance if importing
them would pull in or alter the existing agent. Advertise only implemented input
and cancel capabilities. V1 does not add pause/resume or expose the human-control
gateway routes to service tokens. Remote abort/steering/live-acceptance stories
are not dependencies of Task API delivery. T4/T5/T7 implement and qualify the
required task-local behavior; the adjacent unfinished stories are not assumed
to be delivered.

### Durable input and the meaning of consumption

V3-07 continues to require durable input consumed once by command ID. This design
defines the testable boundary explicitly: **one command is appended once to the
canonical conversation and included once in its assigned logical turn**. It does
not claim exactly-once provider execution or proof that a model obeyed the input.
The owner accepted this explicit consumption boundary in the design review. It is
not permission
for an engine story to weaken that acceptance criterion.

On input acceptance, conditionally insert command UUID, digest, author, authority
version/expiry and command sequence, update META, and append `input.accepted` in
one transaction. Return a receipt with independent fields:

- `status`: `accepted`, `consumed`, `rejected` or `cancelled`;
- `handoff`: `not_started`, `prepared`, `sent`, `confirmed` or `unknown`;
- command/turn IDs, timestamps and safe reason where applicable.

At the next safe boundary the gateway revalidates the author/expiry and current
worker, then atomically changes the eligible FIFO commands to consumed, creates
an immutable turn containing each command ID once, and advances the transcript
head. Expired or revoked queued input is rejected with an event. If a turn already
exists, recovery reads it rather than inserting its commands again. Concurrent
input versus final result uses the same META version: completion cannot bypass
an input already committed to the next turn. Pending input must be consumed or
explicitly settled before terminal completion.

Before the model call, the gateway claims that turn's unique model operation,
persists its request digest and `handoff=sent`, and revalidates authority. The
operation claim and cancellation/input-admission fences use the same META version
transaction, so a cancel that commits first prevents the send claim. The gateway
sends at most one upstream request for that operation (disable automatic
HTTP/SDK replay after a potentially sent request). It stores the result before
returning it to the host. A caller/host retry retrieves the stored operation;
it does not repeat a model call. A crash between claiming and physical send is
conservatively unknown too. Preserve the cost reservation until settled; never
mark unknown usage as zero.

Thus a lost child acknowledgement can be reconciled from the turn and operation
records, while a genuinely ambiguous external handoff remains unknown and stops
automatic progress. Future side-effecting tools would need their own operation
idempotency/reconciliation contract; they are outside this persona.

V3 must inject crashes before and after command insertion, turn commit, model-send
claim, actual send, durable response and child acknowledgement. Assert one
transcript entry/turn assignment per command, no blind provider replay, truthful
receipts and eventual visible settlement. A journal that merely remembers IDs
inside the worker cannot pass.

### Cancellation and finalization

Cancellation acceptance atomically latches `cancel_requested`, blocks new turn/
model/input admission and writes its command/event. It has reserved command
capacity. A queued task can confirm cancellation when protected authority proves
no workload started. For a running task the host delivers typed intentional
cancellation, closes admission, aborts owned model I/O, disposes the adapter and
waits for child exit. At 20 seconds send process-group termination; at 30 seconds
force kill if necessary. Confirm exit before marking cancelled. A disconnected
host cannot claim the deadline succeeded; retain unknown/recovery-required state.

Closing an HTTP connection does not prove a provider stopped computation. Track
provider outcome/cost uncertainty separately from confirmed task-process exit.
Intentional cancellation must never enter the generic retry path or start another
attempt. Late progress/results from fenced generations cannot change the outcome.

Finalization first stores and verifies result artifacts, then commits terminal
task/run state and event, then acknowledges the owned SQS delivery. The gateway
records actual delete confirmation independently. It never claims an SQS ack
from an attempted delete. No task finalizer posts GitHub comments or checks. A stop-only settlement adapter
accepts exit/cleanup evidence from the still-bound workload after owner-policy or
run-token expiry, using live TokenReview and the immutable assignment. It cannot
authorize new model, input, progress or result writes. The gateway reconciler can
also settle from independently verified workload termination. This prevents
revocation from making truthful cleanup impossible without reviving authority.

## 9. Durable progress and SSE

An event contains `schema_version`, `task_id`, `invocation_id`, `generation`,
nullable `runtime_attempt_id`, task-level `sequence`, `event_id`, `type`, server
`timestamp`, optional producer timestamp and an allowlisted data object.
Event ID/cursor is `<task_id>:<sequence>`. Validate task association and bounds;
cursors confer no read authority.

Kinds are `task.accepted`, `task.queued`, `run.started`, `progress.updated`,
`artifact.created`, `input.required`, `input.accepted`, `input.consumed`,
`input.rejected`, `command.updated`, `cancel.requested`, `run.completed`,
`run.failed`, `task.completed`, `task.failed`, `task.cancelled` and
`history.gap`. All are persisted before delivery; state transitions and their
events share a transaction. Diagnostic/tool content is allowlisted and sanitized.

Use strongly consistent DynamoDB event queries every second for active SSE
readers, with 100-event pages. No Redis relay or DynamoDB Streams is required in
v1. The query loop continues from the last emitted sequence, including during
snapshot catch-up; there is no separate ephemeral subscription gap. On a fresh
connection send a snapshot frame containing its version/high-water cursor, then
the requested retained history in order. Snapshot frames do not advance the SSE
Last-Event-ID; only actual durable events do. Clients deduplicate by event ID.

No cursor means replay from the oldest retained event. A future or different-task
cursor returns `400`. An expired cursor returns `410 history_expired` with the
current snapshot, oldest retained cursor and explicit history gap before opening
SSE. A consumer may then reconnect from that cursor deliberately. Close terminal
streams only after all committed terminal events have been emitted. Send a
heartbeat comment every 15 seconds, and reconnect after a deliberate 10-minute
connection window (below the current 15-minute API Gateway limit).

Bound each subscriber buffer independently; close a slow reader with a replay
cursor rather than blocking the agent. The host can buffer 128 nonterminal
reports/256 KiB for at most 10 seconds. If storage recovers, persist a history-gap
record identifying omitted producer reports. If it does not, stop new model work
and enter recovery; never fabricate sequence numbers for unpersisted progress.
Reserve terminal/error event capacity; terminal evidence cannot be dropped.

## 10. Fixed pilot limits and evidence thresholds

These are accepted baseline limits, not observed performance or authorization
to spend. Existing stricter tenant/platform policy wins. Raising a limit requires
a reviewed design/configuration change before new acceptance measurements.

| Dimension | Limit / evidence threshold |
|---|---|
| Submit body | 64 KiB UTF-8 JSON; instructions 16,000 characters; external reference 256; at most 10 criteria of 1,000 characters each. |
| Artifacts | At most 4 input artifacts, 256 KiB each, 1 MiB total; result artifacts at most 1 MiB total. Text/JSON only for investigator v1. |
| Follow-up / cancel | Message 4,000 characters, reason 1,000; whole command 16 KiB; 10 pending inputs and a reserved cancel slot; at most 100 input commands/task. |
| Process/report | Frame 64 KiB; progress event 8 KiB; 1 progress report/second sustained, burst 5; 10,000 events/task with final 100 slots reserved for control/terminal evidence. |
| SSE | 2 streams/task, 10/principal, 32/environment; 100 frames or 256 KiB buffered per stream; disconnect after 10 seconds of blocked writes. |
| Submit rate / capacity | 10 new tasks/minute/principal; 20 nonterminal tasks/tenant; 2 executing tasks/principal, 4/tenant and 4 across the pilot. Idempotent retries do not reserve another slot. |
| Lifetime | Up to 360 minutes (6 hours) from acceptance, configured per principal and including queue/input waits; at most 8 model turns; 120 seconds per provider operation; model output cap 4,096 tokens/turn. |
| Spend | At most USD 1/task, USD 10/tenant/day and USD 25 for the entire qualification run, enforced through existing budget authority and per-call upper-bound reservation. Separate from the engine's code-development budget. |
| Reporting / access | Two distinct authored progress markers arrive externally within 5 seconds each while a controlled healthy fixture is held open; access revocation closes streams within 30 seconds; queued input revalidated immediately before turn/model handoff. |
| Heartbeat / cancellation | Host heartbeat every 30 seconds; SQS visibility 300 seconds with the existing lease safety margin; connected healthy host observes cancel within 5 seconds and confirms child stop within 30 seconds, otherwise explicit uncertainty. |
| Recovery | Scheduled every 60 seconds; healthy pending dispatch rediscovered within 120 seconds; each invocation handles at most 100 work records and 30 seconds; work lease 45 seconds; at most 5 publication tries within 10 minutes and 3 pre-model generations/task, all within the task deadline. |
| Retention | No TTL on active task, grant, idempotency, command or referenced artifact records. Task content/events/results/command receipts retained 30 days after terminal state. Content-free task and idempotency tombstones retained to day 90, returning `410` instead of creating fresh work. After day 90 key reuse may create a new task. |

Keep per-task recovery due records throughout execution, then for cleanup and
retention work. Terminalization schedules idempotent, paginated TTL/S3 cleanup;
API authorization enforces logical expiry even while DynamoDB TTL deletion is
pending. Preserve diagnostic authority/ack state until queue settlement is proved.
Recovery-required tasks retain metadata until explicit settlement; do not let TTL
erase unresolved work. Task reports enforce event/turn budgets and fail visibly
with a reserved error record rather than growing without bound.

V4/V5 use synthetic owned fixtures in an explicitly authorized nonproduction
deployment. Capture at least ten bounded legacy request/run observations before
and during the pilot. Require zero new task-caused legacy routing/auth failures,
no Lambda throttles, and no increase above 20% (or one second, whichever is larger)
in measured legacy p95 admission/table latency. Shared queue age must return to
baseline within five minutes after the burst. Small-sample pilot evidence is not
a production SLO claim. Preserve the two-marker streaming test and every original
V1–V5 criterion regardless of these additional bounds.

## 11. Rollout and what the engine can deploy

Flags are `ADP_TASK_API_ADMISSION_ENABLED=false`,
`ADP_TASK_API_READ_ENABLED=false`, `ADP_TASK_API_WORKER_ENABLED=false`, and
`ADP_TASK_API_RECOVERY_ENABLED=false` initially. A protected pilot allowlist binds
tenant + canonical service + persona; a capability/readiness record binds the
actual image digest, protocol/schema revision, authority/IAM proof and queue
consumer cohort. These flags do not enable legacy controls or either currently
disabled orchestration/pricing schedule.

Order: additive inactive gateway/storage/IAM/routes; compatible worker image;
finish/drain incompatible shared consumers; prove every consumer capable of
receiving from the shared queue is compatible (or excluded); enable reads and
recovery; then enable bounded admission. Gateway/bootstrap must refuse task
assignment to an incompatible consumer. Do not use a mixed image rollout as
proof of same-queue safety. Keep admission off during this compatibility window.

Rollback first stops new admission. Keep read/control/recovery services and
task-capable consumers until accepted tasks are drained, cancelled with evidence
or durably quarantined. Do not purge the shared queue or replace the table.
Retain task-capable code for outstanding task messages even if legacy traffic
returns to its previous image. Any IAM rollback must preserve the task record
boundary while retained task data exists.

The engine has deployment workflow/verification code, but current source has
unresolved gateway/migration entries in its packaged deployment manifest and no
complete Task API coverage for ingress, shared worker and additive infrastructure.
Its runtime controller registers gateway-health and migration-head verification,
and no automatic rollback adapter. The current Task API proposed policy also
contains no `deploy` action or environment connection. Prior observed publishing
failures from the protected-automation cutover remain a separate prerequisite.
Merging code therefore cannot be treated as a completed automated deployment.

T8 prepares exact artifacts, deployment/rollback steps and qualification tooling.
Use the engine for deployment only after supported component adapters, reviewed
workflow/artifact revisions, verified physical targets, working runners and the
accepted deploy/evaluation policy are bound. Otherwise use the documented operator
deployment path and feed its immutable runtime evidence into V4/V5. This design
does not expand the engine platform or its deployment permissions. If complete
engine-managed release is required, that enablement is separate work with its own
reviewed scope; Task API code delivery can proceed without claiming it is done.

Design acceptance selects no live deployment target. Confirm the actual account and
operational scope under the [canonical deployment guide](../adp-platform-deployment/deploy-with-agent.md)
before a deployment. V4/V5 remain blocked until their environment, spend and real
runner bindings exist; no invented evaluator command or skipped live test passes.

## 12. Implementation handoff and conformance

T0's title is **Implement Task API schemas, fixtures and conformance
checks**. Wave 1 is **Contract implementation**: encode this design and verify
agreement across components. It is useful coding work, not an architecture story.

T0 creates `docs/task-api/contracts/v1/` for JSON Schemas and positive/invalid/
legacy fixtures; `docs/task-api/evaluation-manifest.json` for criterion/owner/lane/
fixture/command mappings; and `scripts/task-api/check-contracts.py` for the runnable
validator. These paths are specified here; the files/commands do not exist yet.
Each original T0 acceptance criterion is preserved: its O1–O8 evidence comes from
this accepted design, and fixtures prove its implementation. Missing or conflicting
decisions come back to this session rather than being filled in by T0.

T1–T8 retain their existing boundaries in the architecture. T3 also owns the
producer/authority/model adapters and task recovery trigger; T6 owns artifact
routes and persistent turn/model-operation storage with T1; T4 owns host process
I/O and task-scoped calls; T5 owns the independent investigator and adapter; T7
integrates turn/command/cancellation behavior. Coordinate shared edits by those
owners. The new internal adapters have the fixed surface below. All JSON bodies
reject
unknown fields. Run routes require IAM transport, run credential and live
TokenReview binding; the gateway derives tenant/task/run/generation. Commands
cannot supply another task ID to a run route.

| Route (all POST) | Inputs and result |
|---|---|
| `/internal/v1/tasks/admit` | Original public submit body/key/token plus producer proof; returns durable acceptance receipt. |
| `/internal/v1/tasks/dispatch/claim` | Producer proof and dispatch ID; returns claimed immutable envelope and 45-second lease token only after current policy checks. |
| `/internal/v1/tasks/dispatch/settle` | Producer proof, dispatch ID, lease token, publication outcome and actual SQS message ID if confirmed; conditionally records evidence, not a caller-selected task state. |
| `/internal/v1/tasks/recovery/claim` | Recovery producer proof, shard/cursor and limit at most 100; returns due leased work records. |
| `/internal/v1/tasks/recovery/settle` | Work ID/lease token and observed evidence; gateway validates state/ownership and applies the relevant recovery transition. |
| `/internal/v1/agent/task/bootstrap` | Verified workload and acquired envelope digest; returns task-scoped run credential, immutable input/model/limit bindings and generation. No caller-selected owner. |
| `/internal/v1/agent/task/attempt` | Run binding, runtime attempt UUID, protocol version and supported input/cancel capabilities; atomically replaces current attempt after old endpoint invalidation. |
| `/internal/v1/agent/task/report` | Current attempt, stable report UUID and typed event payload; returns committed sequence/receipt. |
| `/internal/v1/agent/task/turn` | Current attempt, request UUID and expected transcript version; atomically commits eligible input to a turn or returns the existing turn/input-wait state. |
| `/internal/v1/agent/task/model` | Turn ID, request digest and bounded Messages-format request; validates against immutable turn/model/limits and returns a stored result or operation receipt (`pending`, `confirmed`, `unknown`, `rejected`). Confirmed responses include durably stored Messages-format `content` and `stop_reason` alongside the unchanged receipt identity/usage fields. Other outcomes omit these fields or return null. Repeating this same request reads the same stored result/receipt without another provider send. One model operation per turn. |
| `/internal/v1/agent/task/control` | Current attempt and last receipt cursor; returns durable cancel/input state, never arbitrary signed control authority. Host polls every second while active. |
| `/internal/v1/agent/task/artifact` | Existing upload body (run binding, content type, digest and bounded bytes) returns immutable task artifact reference. Closed `operation=read` body accepts only schema version, run binding and input artifact ID; returns the same run/artifact binding, content type, SHA-256, decoded byte length and base64 bytes. |
| `/internal/v1/agent/task/finalize` | Final report UUID, child exit evidence, typed outcome and committed result references; returns terminal receipt after all fences/checks. |
| `/internal/v1/agent/task/settlement` | Live workload token and assignment-bound stop/ack evidence, including after run-credential expiry; stop-only behavior as defined in section 8. |

The existing `/internal/v1/agent/task/acquire`, `/heartbeat` and `/ack` endpoints
retain their old-envelope behavior. Their task-discriminator adapters enforce
these fences and return confirmed acknowledgement only after actual settlement.
Producer operations use versioned request digests, leases and idempotency keys;
retrying a claimed operation returns its existing lease/receipt while valid; a
new recovery owner can claim only after verified lease expiry and state checks.
It never confers another model operation.
Input artifact reads require live run authority and a stored immutable input
artifact bound to this task and approved scope. Output or unrelated artifacts,
caller-selected storage locations/owners and URL-based fetches are rejected.
T6 implements this read variant on the existing artifact route; T4 consumes it.
Internal result uploads are already bound to an active task and return
`expires_at: null`; no active artifact TTL is created. Terminal settlement
establishes their task retention. Public unclaimed uploads retain the required
24-hour expiry timestamp. These are distinct response contracts.
The adapter must validate base64, decoded byte length and SHA-256 before returning
bytes, retaining the 256 KiB per-input and 1 MiB aggregate limits. These bounds
apply to decoded bytes; no URL or credential is returned to the child. T3 owns
the confirmed model response adapter, T6/T1 persist the result atomically with
its receipt, and T4 maps that stored result to the child `model.result` frame.
These adapter corrections do not change the child protocol or frame limits;
large input delivery still requires a separately resolved framing amendment.
T0 encodes these schemas; it does not choose alternate routes or authority models.

V0 verifies schemas against the accepted design and executes the validator.
V1–V3 exercise real storage/IAM, image and distributed failure boundaries in their
specified lanes. V4/V5 qualify the actual deployment and rollback. Every report
cites the design SHA in addition to tested code, fixture, runner and artifact
revisions. V0 does not approve design changes; any contradiction is a blocked
handoff until resolved here. Keep all 84 original acceptance criteria and the
eight V0 criteria; add explicit coverage for separated identities, service
authority, transcript consumption, ambiguous handoff and typed cancellation.

Before publishing/accepting the revised flow, synchronize the epic and affected
child issue bodies to this design, preview the real graph revision with the CLI,
and inspect its version/hash and unchanged acceptance gate. Bind real evaluators
only when delivered. [The publication record](waves.md#plan-publication-status)
identifies the saved live draft separately from design acceptance.

## 13. Source anchors and adjacent-design influence

| Evidence | Consequence |
|---|---|
| [Canonical identity design](../persona-model-mapping-approved-design.md), [self-route principal resolver](../../modules/gateway/src/admin/persona_models/self_routes.py), [Cognito context](../../modules/gateway/src/auth/dependencies.py) | Exact tenant/source/client alias resolves to canonical identity; raw client ID is not the owner. |
| [Producer proof](../../modules/gateway/src/agentauth/work_routes.py), [body-bound model lookup](../../modules/gateway/src/internal/persona_model_selection.py) | Reuse the proven producer-auth pattern; add a task operation binding. |
| [Protected queue acquisition](../../modules/gateway/src/agentauth/task_delivery.py), [bootstrap](../../modules/gateway/src/agentauth/bootstrap.py), [service authority](../../modules/gateway/src/agentauth/service_authority.py) | Preserve workload binding; implement task adapters for fields current repository/human-rooted paths require. |
| [Request table](../../modules/agent-factory/webhook-ingress/infra/dynamodb.tf), [worker IAM](../../modules/agent-factory/webhook-ingress/infra/scaledjob-iam.tf) | Keep table/index semantics and prove a new task namespace write boundary. |
| [Model proxy](../../modules/gateway/src/proxy/routes.py), [request-shape catalog](../../modules/gateway/src/admin/persona_models/request-shape-manifest.json) | Messages/InvokeModel transport exists; task persona policy/shape readiness is new work. |
| [Remote-control design](../../doc/agent-remote-control/README.md), [neutral runtime](../../modules/agent-factory/agent/src/control-runtime.ts) | Separate generation/attempt/command identity, current-attempt invalidation, immediate authority revalidation, intentional cancellation and honest unknown handoff. Durable transcript/receipts are a task extension. |
| [Human control](../../modules/gateway/src/agentauth/human_control.py) | Do not make service tasks impersonate a human control session. |
| [Deployment manifest](../../modules/gateway/src/orchestration/manifests/orchestration-deployments.yaml), [runtime controller](../../modules/gateway/src/orchestration/deployment_controller.py) | Deployment capability and authorization must be verified component by component; no automatic rollback capability is assumed. |

This is the accepted design baseline for engine implementation. Source/design
approval is distinct from flow execution approval and runtime acceptance.

### Host adapter completion correction

The model adapter accepts the same optional `system` string (maximum 16,000 characters) already permitted by the child model-request frame. It participates in the canonical request digest and grants no additional authority. The turn adapter returns `messages` containing the immutable texts of exactly the returned turn’s committed command IDs, in FIFO order; initial turns and waiting responses use an empty list. Stored turn identity and command consumption remain atomic and attempt-fenced. T3 owns the turn/model adapters, T4 their host consumption, and T7 command admission. These fields close transport omissions without changing routes, model selection, authority, artifact/frame limits, or required evidence.
