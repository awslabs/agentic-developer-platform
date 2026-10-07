# Budget recovery before subsequent stories

A shared delivery has two separate reservation records: a per-node admission hold
for permission to start a worker, and a model meter containing actual charges plus
unresolved provider-call estimates. Ending a story makes its admission hold
redundant; it does not make its model calls free.

Before admitting the next shared story or reserving a shared continuation, the
engine now performs these repairs:

1. Retry model settlement from committed `budget_settlement_receipts`. Claude and
   Codex proxy logging attach the exact private reservation scope keys only when
   provider usage passes the same trust checks used for inline settlement. Scope
   evidence and budget debits commit in one SQL transaction. A gateway interruption
   after commit therefore leaves enough evidence for the next admission to replace
   the estimate with actual cost. Replays do not debit the SQL ledger again.
2. Release terminal stories' admission holds using either the existing authenticated
   terminal reports or a concluded current execution and its released work claim.
   The latter must match tenant, flow, issue and claim generation, have an evidenced
   completed/failed/abandoned release, and have no active run or outstanding handoff.
   Claim and flow locks fence admission against ownership changes.

Receipt recovery also runs on the existing policy admission path for continuing
nodes. Model authorization and observation remain read-only. This is recovery on
admission, not a periodic billing sweep or provider receipt retrieval service.

## Limits

- Failure alone is not proof of worker exit. A failed node with a live or
  unverified execution retains its admission hold.
- Calls without trusted usage receipts retain their original estimate. Existing
  receipts without scope evidence are not retroactively trusted. The change does
  not infer zero cost, refund historical unknown calls, or raise the delivery cap.
- Redis updates are atomic and preserve the original request deadline. They do not
  recreate missing meters, renew accounting lifetimes, or remove expired/unbounded
  accounting. Actual cost above the estimate still counts in full and can block
  admission.
- The receipt lookup uses tenant and request identity and checks the exact flow
  scope before applying a cost. Legacy receipts cannot starve later recoverable
  requests behind a fixed result limit.

## Rollout and verification

Apply Alembic revision `084_reservation_receipt_scopes` before deploying the gateway
and orchestration code. The new JSON column is nullable and old code can ignore it.
Downgrade retains this additive column and its durable recovery evidence;
re-upgrade reuses it without resetting receipts. No historical backfill is required.

For a stopped story, compare the node admission key (`orchnode:<node-id>`) separately
from the flow's `:models` meter. A successful repair sets only the obsolete admission
hold to zero. A repaired provider request retains its amount field at receipt cost
and loses its `pending:`/`bounded:` marker. The delivery outcome stays failed if it
was failed. Repeating admission must not change settled spend again.

Regression tests cover the next story after an interrupted SQL-to-Redis settlement,
Claude/Codex receipt attestation, real PostgreSQL receipt atomicity and migration,
missing/untrusted/foreign receipts, outstanding successors, expiry, meter loss,
replay, and actual spend above the original estimate.
