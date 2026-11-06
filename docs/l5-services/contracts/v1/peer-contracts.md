# Peer and worker interfaces

These are **proposed integration contracts**, not available endpoints. SA02 owns
the broker/Product verifier with the identity owner; SA11 and #8149 own Context
admission, reads and invalidation. Peer agreement and implementation remain named
integration prerequisites. Domain schemas are in [schemas.json](schemas.json).

## Credential transport

`POST /internal/v1/l5/credential-session` accepts `CredentialSessionRequest` only
over the existing identity-verified internal route. Authenticate the registered
Lambda service independently of the submitted delegation/operation references.
Check the active `Delegation`, tenant/Product/Project, connection ownership and
workspace delegation, allowed capability/action/source, current run/setup attempt,
lease fence and binding generation. A stale/revoked grant never refreshes a session.

Success is `200` with transient `{access_key_id, secret_access_key, session_token,
expires_at, provider_identity, delegation_revision, operation_id, attempt_id}`;
all strings except the revision integer and typed `ProviderIdentity`. Only the
authenticated worker receives this; no logging, persistence in L5 rows/queues,
public client response or artifact export. Reuse `assume_bound_role` after verified
binding, and verify selected account/role via STS before target effects. Errors
use `Error`: 403 delegation denied, 409 stale attempt/fence, 503 selected connection
unavailable. Retry refresh only through this path, never ambient credentials.
The machine schema is `CredentialSession`; it has no example credential values.

Product access uses existing current/revision/source/change routes through the
same verified service identity plus stored delegation reference. Headers can carry
an opaque reference, not authority; the server retrieves and verifies it. Preserve
current Product source filtering and 410 revision expiry. No Profile SQL in Lambda.
These routes supply Profile snapshots and source-selection metadata, not repository
archives or document bytes. AWS credential sessions do not grant GitHub/document
connector access. Content uses the following separate Product-owned proposal.

## Pinned source content

SA02 owns the proposed Product route/connector authorization integration and L5
`product_client` protocol below, with the Product/source owner. SA06 consumes the
protocol and persists its receipts; SA08 owns repository archive verification and
SA07 consumes document bytes. This is a new interface, not an existing capability.
Enable a source type only after its connector implements and qualifies it.

Prefix: `/internal/v1/products/{product_id}/projects/{project_id}/source-snapshots`.
Every route authenticates the registered platform service through the existing
verified internal transport. IDs in requests select stored grants; they never
grant authority. Derive tenant from that grant, then check current Product/Project
membership, source connection and source access. Connector credentials stay with
the connector owner (GitHub installation/document credential); never return them
to L5 or use the worker's AWS session to read repository content.

| Operation | Request → response | Contract |
|---|---|---|
| `POST {prefix}` | `SourceSnapshotRequest` → `201 SourceSnapshot` (200 replay) | L5 admission calls `pin_source(request, idempotency_key)`. Require delegated `source:pin`, exact source identity/revision and current source rights. Capture bytes into versioned private storage before returning. Same scoped key/request returns the same receipt; changed request is 409. |
| `POST {prefix}/{snapshot_id}/read` | `SourceReadRequest` → `200 SourceSnapshot` | Worker `read_source(snapshot_id, request)` rechecks current grant, live operation/attempt/fence, admitted snapshot membership and expiry. Return the original receipt; never refresh the revision. |
| `POST {prefix}/{snapshot_id}/content` | `SourceReadRequest` → bounded byte stream | Worker `stream_source(...)` uses the same checks. Return exactly `content.media_type` and `content.bytes`, with the SHA-256 in the immutable receipt. No redirects or bearer download URL. |
| `POST {prefix}/{snapshot_id}/manifest` | `SourceReadRequest` → `SourceFileManifest` JSON bytes | Worker `read_source_manifest(...)` uses the same checks; exact serialized bytes, size and hash match `file_manifest`. Each file binds the same source revision, safe relative path, size and hash. |

`SourceSnapshotRequest` contains one exact six-field `source_ref`, exclusions,
delegation ID/revision, `max_bytes`, `max_files` and `retain_until`. The request
limits are also capped by connector and service policy (at most 1 GiB uncompressed
content and 100,000 files); exceeding a bound returns 422, never silent truncation.
Manifest serialization is bounded to 64 MiB and streamed, not embedded in a queue
or admission JSON. Repository content is a canonical ZIP without unsafe paths,
duplicate names or implicit symlink/submodule/LFS expansion. Document content is
the exact original bytes with one manifest entry. `max_bytes` bounds both the
content artifact and total expanded file bytes; SA06 enforces the bound again.
Exclusions are explicit and echoed unchanged; unsupported selection is 422.

The connector proves that the bytes belong to `source_ref.record_revision` using
its native immutable revision, then hashes the captured content and file manifest.
The response must echo the exact requested source ref and exclusions. Mutable
aliases and connectors unable to prove this association return
`422 immutable_source_required`. UI/source selection resolves branch/document
aliases before this call. `expires_at` must cover requested retention and the
admission deadline; policy cannot silently shorten it. Unsupported retention is
422. Snapshot IDs are scoped, unguessable references, not authorization tokens.

Admission persists the complete `SourceSnapshot` receipts in
`Admission.source_snapshots`; source refs must match `RunInput.sources` in order.
Additional consumed PR base/head snapshots are explicit source entries. Orphaned
captures after failed admission expire through Product-owned retention cleanup.
Worker reads use the same delegation and admitted operation, with current attempt
and fence verified by the injected L5 work-authorizer. Reconcile delegation revision
changes through admission policy; never treat an old receipt as current permission.
Check receipt equality, content/manifest digests, per-file sizes/hashes and exact
revision association before sealing the L5 manifest. Hash mismatch is
`409 source_integrity_mismatch`, blocks provider effects and cannot repin.

All failures use `Error`: non-enumerating 404 for inaccessible sources/snapshots,
403 for denied actions on authorized scope, 409 stale delegation/attempt/fence or
idempotency conflict, 410 expired snapshot/revision, 422 unsupported immutability,
selection/retention/size and 503 connector unavailable. No fallback to local git,
default branch, ambient credentials or a newer document. Revocation also fences
already staged L5 artifacts. SA02/SA06/SA08 must test exact and mismatched bytes,
expiry, excessive expansion, connector outage and revocation between pin/read.

## Durable setup intent

The public `Operation` is only a status projection. SA04 stores a separate strict
`SetupIntent` in the setup-operation row in the same transaction as the receipt
and work intent. Its `request` is the original authorized `BindingInput`, including
the selected `native_resource_ref`, configuration revision and cleanup confirmation.
It also retains scope, configuration ID, binding generation for existing bindings,
server-verified `SetupAuthorization` and the original owned-resource receipt for
cleanup. The intent hash is SHA-256 of RFC 8785 canonical intent JSON excluding
`intent_hash`. No credentials are stored; client-supplied authority is rejected.

SA06 loads this immutable intent, the exact configuration version and optional
binding snapshot, verifies the intent hash and current binding generation, and
passes them with the current `WorkerAttempt` and transient session. Lifecycle
admission records the effective generation after any suspend/unlink fence in the
same transaction. `request.binding_id`, configuration revision and selected native
resource must match the resolved snapshots. For create/link, binding may be absent;
once created, the separately persisted native/ownership receipt supplies recovery
identity without rewriting intent. For other actions the binding is required.

The stored authorization is audit evidence; workers recheck current delegation,
action, scope, attempt fence and ownership before effects. Cleanup requires all of
the explicit confirmation, matching creation receipt, ADP ownership and current
cleanup grant. Customer-linked resources cannot be deleted. Retries and setup
reconciliation use the same intent and original attempt identity; they cannot
substitute another space or infer the original request from `Operation`.

## Context contribution and read proposal

FinOps cost results use the same L5 publication outbox and receipt recovery, but
are **not** Security `Contribution` claims. The proposed cost contribution has
`kind: finops_cost_result`, `schema_version: 1.0`, a scoped `contribution_id`,
`result_ref`, `run_id` and the immutable `FinopsCostResult` under `cost` (including
original query, Estimated, freshness and native/billing provenance). The
`evidence_refs` array separately identifies the versioned native answer, scoped
snapshot and each independent billing page so later authorized tasks can cite
the original evidence without rerunning a paid inquiry. An intent is
keyed independently by tenant/Product/project/result/version/destination; its
idempotency key includes tenant/result/version/destination/schema. Context accepts
it at `POST /internal/v1/context/l5-cost-contributions`; Insights accepts it at
`POST /internal/v1/insights/l5-cost-contributions`. Each destination exposes
`GET .../{contribution_id}` for its durable receipt and
`GET .../{contribution_id}/projection` for the readable projection. A receipt
must identify exactly the original `result_ref`; readback must match scope,
result, cost, evidence references and full contribution hash. A timed-out write
first reads the same
receipt and projection before retrying the same idempotent contribution. A
missing projection remains owed, rather than becoming a published result.

These cost routes are a **proposed consumer contract**, not evidence that either
consumer already implements them. Delivery requires separate currently scoped
delegation and qualified private-read fencing for each destination. Without those
bindings, SQL intents remain pending and the existing Security publisher never
sends cost payloads. Consumer retrieval must enforce current Product and source
visibility, including indexed and artifact copies, before FO07 may enable or
claim live publication. Neither receipt recovery nor repair may resubmit native
provider work. Consumer owners must confirm these routes and the revocation
behavior before deployment.

For an authorized result, the L5 host reports each consumer independently at
`GET /v1/products/{product_id}/projects/{project_id}/services/finops/results/{result_id}/publication-status`.
Each entry identifies the destination, SQL delivery state, publication ID and any
retry error. A published, non-invalidated entry also names its durable receipt and
sets `projection_verified_on_ack` only after receipt and projection readback
succeeded. This records verification at acknowledgment, **not** a live check of
current consumer access or availability. Pending, unavailable, missing or
invalidated entries do not claim readable projections. The host rechecks the
same current Product and source authority as result reads and sends no native
request when reporting status.

The L5 host also offers
`GET /v1/products/{product_id}/projects/{project_id}/services/finops/results/{result_id}/cost-contributions/{destination}`
for a later entitled reader. It returns the original frozen cost and evidence
references only for an acknowledged, non-invalidated contribution. The
`citation` bundle repeats the pinned `result_ref` and versioned `evidence_refs`
from the contribution so a later entitled task can cite the original evidence;
`contribution_hash` covers the stored contribution, not that derived bundle.
The L5 read also checks the stored cost, scope, lineage and hash against the
original result; a mismatched copy is denied rather than attributed to a new
result. On **each** read, the Gateway rechecks current Product membership and
relationship revision, the original ready FinOps binding/generation, current
access to its AWS connection and the original active scoped worker delegation. Revoking any of those denies
the read as 404 even when a copy remains in SQL; failures never turn missing
amounts into zero. This L5 check does not substitute for equivalent fencing in
Context/Insights indexes and artifact endpoints. Service-account retrieval stays
denied pending a separately qualified consumer delegation path.

Transform uses the same SQL publication/receipt repair pattern but carries a separate
`kind: transform_result` envelope: frozen `scope`, versioned `result_ref`, `run_id`,
`result` (including source trees, native attribution, original patch, tests,
verification status, limitations, freshness and retention), pinned `goal` and
`source_refs`, and versioned `evidence_refs`. Its proposed peer paths are
`/internal/v1/context/l5-transform-contributions` and
`/internal/v1/insights/l5-transform-contributions`; each supports `POST`,
`GET /{contribution_id}`, `GET /{contribution_id}/projection`, and
`POST /{contribution_id}/invalidate` with the same scoped idempotency, receipt
and watermark semantics as the cost paths. The receipt must cover exactly the
original result reference; readable projection must reproduce the frozen fields
and full payload hash. These paths are **proposed**, not installed-consumer
proof. A qualified per-destination delegation and private read fencing must be
configured before the producer sends anything; consumer owners must confirm the
routes, current entitlement checks and invalidation behavior before rollout.
An acknowledgement does not prove that a later task used the result.
The existing human cost/Transform contribution readers revalidate Product, source,
binding and delegation authority in a fresh host database session before releasing
the frozen copy. They also re-read its publication state, watermark, receipt and
payload, so an earlier result selection or ORM identity map cannot retain revoked
access. This host read check does not enable consumer admission or service-account
retrieval, and does not establish later-task ownership or consumer index fencing.
An authorized human later task may retrieve
`GET /v1/products/{product_id}/projects/{project_id}/services/transform/results/{result_id}/transform-contributions/{destination}`
to obtain the frozen result and its versioned source/evidence citation. Each read
requires current Product/run/source rights; unacknowledged, invalidated, expired,
foreign and mismatched records return 404. This metadata route does not enable
Transform's independently gated raw result/artifact reader or qualify a
service-account delegation. Installed consumer indexing and later task use
still require TF07's live checks.

| Operation | Request → response | Semantics |
|---|---|---|
| `POST /internal/v1/context/l5-contributions` | `Contribution` → `{receipt_id, contribution_id, admitted_schema, consumer_revision, record_refs}` | Verified producer + current delegation, `Idempotency-Key` = result/version/destination/schema. Return 201 only after durable admission; replay returns same receipt. Changed payload under same key is 409. |
| `GET /internal/v1/context/l5-contributions/{contribution_id}` | authenticated ID → same durable receipt | Recover lost acknowledgment; do not infer admission from a transport timeout or unrelated Context record. |
| `POST /internal/v1/context/l5-contributions/{contribution_id}/invalidate` | `{source_refs, invalidation_watermark, reason}` → `{receipt_id, applied_watermark, record_refs}` | Monotonic watermark, current verified revocation authority. Duplicates idempotent; older watermark cannot restore derived visibility. |
| `POST /v1/products/{product_id}/projects/{project_id}/context/query` | `{topic, source_refs, max_records, required_freshness_seconds}` → `{snapshot_ref, record_refs, evidence_refs, gaps, observed_at}` | Proposed #8149 shared query, not an L5-owned endpoint. Current reader/source visibility, bounded response (1–100 records), attributable Security claim/evidence refs, no private-count leakage. |

Reference arrays use the six-field `RecordRef`; evidence uses `ArtifactRef`;
timestamps use `Instant`; watermark/revision values are positive integers (initial
invalidation watermark may be zero). IDs are bounded strings as in `Id`; reasons
and topics are at most 1,000 characters. All objects reject additional fields.
Producer `invalidation_pending` records a fenced, owed consumer invalidation;
`invalidated` requires acknowledged removal or verified absence, never just a sent request.
The L5 worker accepts a revocation only after a current Product verification callback
confirms the exact source, scope and watermark. Publication of sourced private
material remains disabled by default until the consumer's read fencing is qualified.
Use non-enumerating 404, 410 expired revision and 503 unavailable consumer/schema.
No silent fallback to an ungoverned knowledge write.
Machine definitions are `ContextReceipt`, `ContextInvalidation`,
`InvalidationReceipt`, `ContextQuery` and `ContextAnswer` respectively.

Scheduled producer sweeps require a client bound to the currently verified
delegation's tenant, Product and project. Filter owed SQL work to that scope before
the batch limit and lease; another scope's publications and invalidations remain
untouched for its own authorized worker. A client without a verified scope cannot
run a sweep. The transport still checks current delegation on every request.
Revocation queues invalidation even for an `unavailable` publication: a rejected
receipt lookup does not prove that a previous timed-out POST was not admitted.

Large results use bounded claim pages, not truncation. The producer freezes all
claims in SQL and sends at most 100 claims and 100 evidence references per
`Contribution` envelope. Single-page envelopes and receipts remain unchanged.
Multi-page envelopes share the contribution ID, scope and result lineage, with
`claim_page` containing zero-based `index`, page `count`, `total_claims` and
`contribution_hash`. The hash is SHA-256 of the original complete producer payload
(sorted JSON keys, compact separators, ASCII escaping), before paging. Reassemble
claims and evidence in page-index order and remove `claim_page` to verify it.
Each page's idempotency key appends `/pages/{index}` to the existing contribution
key; retries send the same immutable pages. A conflicting page or inconsistent
lineage, count or hash is rejected, never combined with another result.

The consumer returns 202 while pages are incomplete, without a publication
receipt or query-visible partial assessment. Only admission of every page with a
matching full-payload hash and claim count creates the durable `ContextReceipt`.
For multi-page admission it additionally requires `contribution_hash` and
`claim_count`; producer validation rejects a partial or mismatched receipt.
`record_refs` remains bounded and identifies the complete assessment's consumer
records, not one record per claim. Receipt lookup recovers a lost final
acknowledgement; otherwise retry replays pages without another Security run.
Invalidation at the shared contribution ID fences both pending pages and admitted
records, including late page replays. Compatible page admission/read fencing is
part of the proposed #8149/SA15 consumer integration, not deployed proof.

Context's existing snapshot v1 **business contribution** type is not an assessment
record. The producer envelope retains Security evidence while #8149 implements a
versioned admitted representation. Record that representation in `admitted_schema`;
unsupported versions fail closed. Do not claim publication or EC-07 until the
consumer receipt and a later authorized query are observed. Revocable private
publication stays disabled until invalidation/read fencing is implemented.

## Worker adapter signatures

All methods return bounded `AdapterOutcome` plus operation-specific immutable
references; errors are structured and contain no credentials or raw provider logs.

| Method | Typed input | Required output / side-effect boundary |
|---|---|---|
| `capabilities()` | locked SDK/service-model revision | Supported actions, Region constraints, source/size/cancellation limits; unsupported is explicit. |
| `check_binding(binding, configuration, session)` | `Binding`, `Configuration`, verified transient session | Source/role/eligibility readback; no provider create. |
| `prepare_binding(intent, configuration, binding, attempt, session)` | `SetupIntent`, pinned `Configuration`, optional pinned `Binding`, `WorkerAttempt`, session | Native setup references and ownership receipt; persist attempt before effects and use the original link target. |
| `reconcile_setup(intent, attempt, session)` | Same `SetupIntent`, original durable `WorkerAttempt` and persisted native receipt | Unique native match or outcome_unknown; no second create. |
| `materialize_inputs(run, attempt, source_client)` | `Run` with admitted `SourceSnapshot` receipts, `WorkerAttempt` and the pinned source client above | Verified source bytes and sealed `Manifest`/artifact refs; no repinning or provider start. |
| `submit(run, manifest, attempt, session)` | `Run`, `Manifest`, durable attempt/fence | Native job ref or definitive_failure/outcome_unknown; static-mode constants enforced. |
| `reconcile_submit(run, attempt, session)` | Original attempt/source hash/token | Unique native identity or unknown; never new start. |
| `observe(run, session)` | Persisted native identity, pinned monitoring policy and scoped session | `AdapterOutcome.observation: ProviderObservation` on a successful job read, available phase/task progress, redacted error and bounded diagnostics. Missing/unknown native status is explicit unknown; failed reads never fabricate progress or provider failure. |
| `collect(run, manifest, session)` | Exact completed native job | `Result`, paginated `Finding` artifacts and completeness/validation/cost. |
| `cancel(run, session)` | Cancel intent and current authorization | Native stopped proof or unsupported/unknown; local cancellation is not proof. |
| `cleanup_owned(binding, intent, attempt, session)` | Pinned `Binding`, `SetupIntent` with owned-resource receipt, `WorkerAttempt` and current cleanup grant | Native absence or retained failed work; linked/customer resources untouched. |

Work queue envelope is exactly `{schema_version:"1.0", work_id, generation}`.
The SQL work row contains the authoritative entity/step/scope/attempt reference;
worker re-reads it and acquires a lease fence. No credential or source bytes in
the queue. Additive envelope changes require backward-compatible consumers;
breaking changes require a new queue schema and a drain/dual-reader rollout.

## Dispatch and observation handoff

SA06 supplies `dispatch_committed_work(work_id, generation)` as the common internal
dispatcher used by SA05 admission, SA04 setup/control and worker transitions.
Call only after committing run/receipt/work. Send immediately for due work and use
an SQS standard-queue message delay for future work (600 seconds for normal
Security polling). A bounded send failure leaves SQL work recoverable and preserves
the API's durable 202 receipt. Delivery is not completion. The one-minute
EventBridge relay repairs owed work; it does not poll every native job each minute.
SA15 grants the gateway/worker/relay identities queue-specific send permissions.

`MonitoringPolicy` is user configuration; `EffectiveMonitoringPolicy` has defaults
resolved by SA03 and is pinned at admission. `ProviderObservation` is the bounded
adapter result; `RunMonitoring` is SA06's persisted client projection. Generic
health/outcome types stay in L5; SA07/SA08 map the AWS statuses documented in
[the monitoring architecture](../../architecture.md#polling-and-assessment-monitoring).
The job's `error` and a failed read's `last_poll_error` are separate facts.

Complete observation snapshots compare stable native job/phase/task identities
and status changes. Persist large paginated task details and diagnostic references
in the authorized `details_ref` artifact. A partial/absent snapshot cannot establish
inactivity. Native timestamps are evidence with no guaranteed update cadence;
`progress_fingerprint` excludes the local read timestamp and unrelated metadata.
SDK response optionality and unknown future statuses must survive normalization.
Increment the poll-attempt counter before the API call; record read errors when
known and observation/next-work together after the call. Expired leases recover
the same work, never a new assessment; a crashed read is not provider failure.
