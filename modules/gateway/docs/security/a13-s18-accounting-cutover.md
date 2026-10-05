# A13 / S18 accounting cutover

This change establishes a server-generated request UUID before authentication and admission. Caller X-Request-ID is a bounded diagnostic correlation value only. Reservations, pricing decisions, SQL settlement, and immutable transcript object names share the server UUID.

Gateway UsageService writes a `(org_id, request_id)` settlement receipt and every budget hierarchy debit in one database transaction, independently of optional transcript logging. The tracker uses the same receipt and compares the owner, six-decimal cost, and measured tokens and a digest of the UTC request-start date and canonical hierarchy before accepting a replay. Gateway and transcript use the same private server request-start timestamp. Concurrent replays debit once. Tracker bridge updates are scoped to tenant and owner and share that transaction, so bridge or fanout failure rolls back the receipt. S3 notifications read their exact VersionId and validate the object path against the event owner/request. New writes use If-None-Match. Failed Lambda batches raise for asynchronous retries; completed records are safe to replay.

SQL commits before Redis reconciliation; failed SQL or missing provider usage retains reservations rather than implying a zero charge. A bounded finalizer timeout marks accounting incomplete without starting an unbounded second Redis attempt. Exact admission keys survive requests crossing midnight. Downstream rejection before provider dispatch releases the reservation. Local count_tokens remains authenticated/rate-limited but does not reserve spend. Non-Claude compatibility calls now use the measured pricing path, including streaming and logging-disabled requests.

If SQL fails and optional transcript logging is disabled, the request has **not** completed durable settlement. Its reservation is retained, but this patch does not add an outbox or infinite retention. Operators must reconcile the request before the ordinary hold expires if the service cannot recover; do not report such a request as durably debited.

## Required rollout order

Merge remains gated on A11's verified identity contract. No live migration or rollout is included in this PR.

1. Pause model admission and drain in-flight gateway calls. Drain the old transcript consumer and record unresolved historical events for explicit reconciliation. Preserve transcript version evidence and existing aggregate totals.
2. Stop the old additive tracker. Apply gateway migration 074 after 073; it creates `budget_settlement_receipts` without guessing/backfilling historical per-request debits.
3. Deploy the new tracker and gateway together, then resume admission and the consumer. **Never run the new gateway SQL writer beside the old additive tracker.** That combination would debit the same request twice.
4. Verify a measured request with transcript logging disabled, and one with logging enabled plus duplicate S3 delivery. Confirm exactly one receipt and one debit per hierarchy/period, with the same tenant/request/cost/token evidence.
5. Verify repeated caller X-Request-ID values receive distinct server UUIDs; exercise rate-limit/validation rejection and an ambiguous provider outcome. Rejections before dispatch release reservations; ambiguity retains them.

New transcripts carry `settlement_version: 1`. Historical objects without that marker are rejected for explicit reconciliation; replaying them cannot manufacture a fresh debit against already-populated aggregates. Do not add the marker to historical objects. Permanent failures must be investigated using Lambda failure destinations/logs and retained S3 versions; never acknowledge them by inventing zero usage.

Rollback must preserve receipts. Migration downgrade deliberately refuses to remove them because doing so would reopen duplicate debit. To roll back binaries, pause/drain admission and consumers first and reconcile the settlement boundary; do not reactivate the old additive tracker against new transcripts.

## Validation scope

Focused tests exercise real PostgreSQL concurrency, rollback/redelivery, tenant/owner conflicts, gateway/tracker shared receipt and the race where the tracker arrives before the gateway usage row. Other tests cover middleware ordering and pre-provider rejections, exact-version S3 reads, invalid object owners, asynchronous retry behavior, non-Claude compatibility stream/nonstream pricing with transcripts disabled, and conditional transcript writes. These are source/runtime tests, not evidence of deployed enforcement or historical balance reconciliation.
