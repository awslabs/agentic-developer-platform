# PMM-08 usage evidence and rollout

Usage rows retain the actual provider model and the issued launch decision as
separate facts. The gateway conditionally records the fresh signed-response
content in protected execution storage; the SDK carries its random launch nonce
through the proxy. Accounting reads only that tenant/run/attempt-bound record,
never resolves policy again after inference. A missing record retains protected
snapshot attribution with a NULL proposal. The original pricing decision,
confidence, estimate reasons, provider request ID and destination region are
retained where captured. The aggregate model policy revision identifies the
frozen preference/default state; catalogue and harness revisions stay separate.

Costs are scoped to the canonical preference owner and billing tenant. They are
usage-ledger totals, not complete spend or provider invoice reconciliation.
Unknown, estimated, partial and measured zero remain distinct. Legacy rows and
paths without protected identity (including unenrolled chat/ARC calls) cannot be
assigned an owner by inference. Empty responses still include class defaults
and preference source. Approving humans do not replace service preference owners.

Owner warnings use the same catalogue and live allowlist validation as runtime.
They remain visible when the separate model-catalogue UI request fails. Operator
notifications use a committed, token-fenced outbox claim before publish. Delivery
is at least once: a crash after publish and before marking delivered can repeat a
notification. Increment catalogue `lifecycle_version` on every lifecycle change,
including reactivation and subsequent retirement, to create a new transition.

Migration 061 adds nullable columns without backfill using a one-second lock
wait and 30-second statement bound. Migration 062 builds the usage indexes
concurrently in separate autocommit transactions, repairs invalid interrupted
indexes and bounds each statement to five minutes. Gateway Deploy runs migrations
in a release-image Job before changing serving gateway or scheduled-engine code.
The Job has no gateway Service labels, no HTTP server, no retries, a 15-minute
deadline and a one-hour cleanup TTL. Failure stops serving rollout; operators can
inspect the Job and retry after contention clears. Post-rollout pricing validation
remains in place. Destructive schema downgrade requires rolling code back first.

Local PostgreSQL tests cover upgrade/downgrade/re-upgrade, lock timeout with a
queued reader, a 10,000-row concurrent build with ongoing reads/writes, invalid
index recovery and 12-way retirement-claim/lease races. These are local behavior
checks, not measurements of production volume or a live acceptance certificate.
The code merge does not change report-only posture, enable Agent Models/probes,
authorize paid calls, or release the existing webhook deployment hold.
