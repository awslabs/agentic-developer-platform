# Metering incident reconciliation

`POST /admin/metering/attempts/{request_id}/reconcile` is a protected recovery
path, not a budget override or a way to estimate missing usage. It requires a
current authenticated human platform administrator with `BUDGET_UPDATE`, a live
workspace membership, and a separately issued incident grant. It is inactive
without an operator-controlled `BG_METERING_INCIDENT_GRANTS` configuration and a
versioned immutable `BG_METERING_JOURNAL_BUCKET`. No endpoint issues grants.

An incident grant is a private JSON-list entry binding an opaque `incident_id`,
canonical `actor_id`, exact `tenant_id`, `request_id`, `action`,
`expected_revision`, `evidence_ref`, `evidence_version`, `evidence_sha256` and
finite timezone-aware `expires_at`. The operator stores and protects this
configuration outside the repository. Neither a request body nor a worker grant
can supply or extend it. The gateway revalidates after evidence retrieval and
before writing the accounting event; a changed or expired entry at either check
refuses the mutation. The gateway logs access attempts with
an evidence-reference digest rather than a private object key or document.

The strict request names `tenant_id`, `expected_revision`, `idempotency_key`,
`action`, `reason`, `evidence_ref` and `evidence_sha256`. For
`apply_trusted_receipt`, it also names the exact `receipt_key`,
`receipt_version` and `receipt_hash` of the gateway's immutable final-usage
object. That object must contain verified provider usage and an admitted
historical pricing decision; the existing settlement authority atomically
records one debit, diagnostic usage and administrator audit event while clearing
the attempt's hold. For `confirm_not_dispatched`, the evidence must be the exact
versioned admission object and the SQL attempt must still be prepared. Armed
attempts cannot be cancelled by an administrator; durable physical-dispatch
cancellation proof is a separate transport integration. No estimated charge,
manual release or financial-exception action exists.

Successful responses contain `disposition`, `revision`, `spend_blocked` and
`exact_receipt`. `spend_blocked` stays true if *any* intersecting scope still has
unknown, quarantined or overdue exposure; it is not a promise that every other
budget or authorization rule permits another request. Identical authorized
idempotency retries return the persisted response without rereading journal
objects. A conflicting key or stale revision returns `409`; invalid evidence or
unsupported action returns `422`; missing authority returns `403`; unavailable
journal or database returns `503`. Missing and invalid bearer tokens return
`401`. An unknown attempt without trusted evidence remains held even when
ordinary budget enforcement is off. A later trusted receipt contradicting a
proven-unsubmitted disposition quarantines and reblocks rather than silently
charging or releasing it.

Schema migration `116_metering_admin_result` adds a nullable persisted-response
snapshot to reconciliation events. Apply migrations before serving the route;
rollback must retain attempts, holds, receipts and events. This implementation
neither activates metering gates nor supplies a production incident grant.
