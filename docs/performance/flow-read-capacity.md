# Flow read capacity

Flow list and detail reads reuse structural summaries across gateway replicas
through the existing configured Redis service. Authorization still runs for
every request. This reduces repeated aggregation; it does not establish a
particular concurrent-user capacity without a deployed load measurement.

## Connections and request logging

Platform-admin authentication releases its owned database connection before
waiting for Cognito, then checks for a committed revocation in a fresh
transaction. A caller-owned transaction remains intact. Flow HTTP reads also
release their completed permission-read transaction before opening a separate
read-only display snapshot, avoiding two pool slots for one request.

`BG_REQUEST_LOG_QUEUE_ENABLED=true` enables request analytics batching when
Redis is configured. Each record receives a stable UUID before enqueueing. A
Redis Stream holds at most 10,000 records, with a 16 KiB record limit. Workers
commit batches of up to 100 to PostgreSQL before acknowledging and deleting
them. Replayed UUIDs are idempotent; another worker can reclaim entries from a
crashed consumer after 30 seconds. Database writes have a five-second deadline.
Enqueueing has a 250 ms deadline, and individual Redis operations use a 200 ms
socket deadline. A full, unavailable or disabled queue uses the existing
synchronous logging path, so an outage can increase response latency.

This queue serves best-effort **request analytics**. Redis eviction or complete
Redis loss can lose records that have not reached PostgreSQL; the existing
Redis configuration uses `allkeys-lru`. It is not a durable audit journal.
Transactional administrative audit writes and metering retain their existing
paths. Do not use this queue for those records. Shutdown leaves pending stream
entries for another consumer instead of discarding them in process memory.

Migration `144_request_logs` creates the request analytics table for installations
created exclusively through Alembic; it preserves existing tables and retains
records on rollback. The queue can drain previously retained entries after this
schema repair.

Monitor `bedrock_gateway_request_log_queue_total` outcomes and
`bedrock_gateway_request_log_batch_records`. Rising `full`, `unavailable` or
`batch_deferred` counts indicate fallback or a blocked writer. Inspect stream
length and pending entries alongside Redis memory/evictions and database
latency. Queue keys are hashed by database namespace; do not delete them as
ordinary cache entries. The platform-admin-only
`GET /api/admin/diagnostics/export-health` endpoint exports process-local queue
outcomes, batch count/size totals and cache outcomes without tenant identities.
Collect it from each replica when diagnosing shared-cache or queue behavior.

## Structural summaries and invalidation

`BG_FLOW_READ_CACHE_ENABLED=true` enables structural sharing when Redis is
configured and migration `143_flow_read_revisions` is installed. Only explicit
PostgreSQL read-only display snapshots can publish. Metadata-only local setup,
SQLite, mutation transactions and a missing migration bypass this cache.

Cache identities include database namespace, organization, committed revision,
operation and filter/page parameters. List summaries, detail node display
states and display decisions are eligible. The complete graph, product access,
permissions and provider history are not stored here. Costs are overlaid on
every list response using the independent display-cost reader, preserving its
existing ten-second freshness limit and unknown-cost semantics.

Migration 143 installs statement triggers on existing `orchestration_*` tables
with an `org_id`. Inserts, updates, deletes and truncates invalidate affected
summaries in the same transaction as the data change, including raw SQL and
background-engine writes. Rollbacks also roll back invalidation. Revisions are
per organization and flow so unrelated flow writers do not share a revision-row
lock. Tables without a flow identity use an organization-wide fallback. List
reads fingerprint all organization revisions; detail reads fingerprint their
flow and the fallback revision. Old read snapshots remain internally consistent.

When adding a table that contributes to these projections, register its
invalidation triggers in that migration and test raw SQL updates, rollback and
tenant isolation. An application callback alone does not cover background
writers. The revision table is database-maintained, not an application-editable
counter.

Entries expire within five seconds of computation start and are limited to
1 MiB. Token-fenced leases share a refresh across replicas and prevent an old
owner from overwriting a newer result. Refresh waits are capped at two seconds,
computation at three seconds and local concurrent computations at four. Busy
refreshes return 503 with `Retry-After: 1`; they do not invent a result. Redis
outages use bounded local reads. Monitor
`bedrock_gateway_flow_projection_cache_total` together with pool wait time,
database CPU, request errors and latency.

## Browser traffic

List, graph, execution, Timeline and feature queries pause polling while the
tab is hidden. Focus refresh and initial reads remain immediate. Stable jitter
spreads recurring polls between 90% and 100% of the existing 30-second or
15-second maximum interval. List requests carry cancellation signals. Activity
reads omit pending/ready stories with zero attempts and no run; the authorized
graph already establishes that they have not started. A started attempt becomes
eligible for activity enrichment on the next graph observation.

## Rollout, rollback and validation

Use the normal protected gateway update procedure: migrate from the candidate
image before serving it, preserve target configuration, verify the backend and
frontend source, and confirm health and authorization. Both flags default on
but require configured Redis. Disable structural sharing independently with
`BG_FLOW_READ_CACHE_ENABLED=false`. The additive revision schema can remain
during application rollback. To stop queue producers, disable its flag on all
HTTP workers while retaining a consumer until stream length and pending count
reach zero; immediately disabling every consumer leaves records awaiting a
later compatible worker. Never flush Redis to disable this feature.

Regression checks cover one-slot authentication/revocation and snapshot
handoff, shared reads across simulated replicas, revision/tenant isolation,
expiry, cancellation, lease fencing, PostgreSQL write/rollback/truncate and
migration removal, fresh cost overlays, logging replay and real Redis queue
operations. Browser checks cover delayed enrichment, retries, cancellation,
layout, navigation and progress direction.

For capacity validation, compare the same authorized polling workload first,
then measure the changed browser request mix. Record ready replicas at each
stage, cache outcomes, queue backlog, database pool waits and CPU, identity
provider throttling, all failures and failure-inclusive p95. Preserve the
existing stop thresholds. Distinguish short polling-session ramps from sustained
load, independent user identities, populated history and multi-tenant traffic.
Increase database or pod capacity only after measuring the remaining limit;
larger connection pools alone can overload the database.
