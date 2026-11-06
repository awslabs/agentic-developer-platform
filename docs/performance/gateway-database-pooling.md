# Gateway database connection pooling

The Kubernetes gateway enables the existing IAM-authenticated PostgreSQL pool
for its long-lived HTTP workers. Without pooling, the approval, budget and rate
limit session providers reset their database engine, and each session establishes
a new connection. Reusing the engine and connections avoids that setup on the
Delivery Flows page and its startup requests. Authorization and accounting still
run for every request; this is not a response or permission cache.

The settings live on the gateway Deployment, not its shared ConfigMap. The
orchestration tick Lambda keeps its default `NullPool` behavior. The pool retains
eight connections and permits four overflow connections in the default worker, with the existing
10-second acquisition timeout, 600-second recycle interval, connection health
check, TLS verification and fresh IAM password callback for new connections.
Authenticated connections need not be closed when their original IAM token
expires; replacement connections receive a current token.

## Capacity and rollout

The default one-CPU image runs one asynchronous worker, so the maximum remains 12 connections per pod
and 144 at the default 12-pod HPA ceiling. The Deployment rolls with
`maxSurge: 50%` (6 extra pods at the ceiling, 216 connections) and
`maxUnavailable: 0`. Include terminating pods when budgeting a rollout: 30
simultaneously live pods (12 ready + 6 surge + 12 draining) would permit 360
connections, under the ~450 `max_connections` of the dev `db.t3.medium` with
headroom for the tick, migrations and operators. That is a planning example,
not a guarantee that overlapping rollouts cannot create more.
Inspect live and terminating pods, database `max_connections`, reserved slots,
other applications and observed usage before deployment. Leave headroom for
migrations, operators and the tick. Recalculate if worker counts, HPA ceilings,
pool settings or deployment concurrency change. The optional LLM proxy profile
has its own worker and pool settings and needs its own capacity calculation.

The single worker avoids Uvicorn 0.34's five-second subprocess health deadline
during injected Python instrumentation startup. Kubernetes startup, readiness and
liveness probes still supervise the serving process. The two-CPU LLM proxy profile
continues to use two workers and 20 database connections per pod; legacy manifests
without explicit worker arguments retain their released four-worker interpretation.

Preserving the database budget does not establish equal throughput. Thread
executors, metered HTTP client pools and optional stream-admission limits are per
process. The default has no stream-admission cap; explicitly configured per-worker
limits keep their values and now apply to one worker per default pod. No client or
executor limit is multiplied automatically. Requalify traffic latency, concurrent
streams and HPA behavior before making a capacity claim.

### Default-off isolation canary and connection worksheet (#7031)

The HTTP Deployment declares `BG_RDS_SPLIT_POOL_ENABLED=false`. Its selected
candidate is six request plus four accounting connections **per worker**, with
zero overflow and finite connect (5 s), checkout (10 s), lock (1 s) and
statement (8 s) waits. These settings have no effect while the split is off:
the combined pool remains eight retained plus four overflow in the default worker. They are
not in the shared ConfigMap; the tick Lambda still uses its default `NullPool`.
Enabling the split requires an operator-owned fleet budget; adding replicas
does not make the database limit larger.

| HTTP pool mode | Per worker | One-worker pod | 12 HPA pods | 12 pods + 6 surge | 12 pods + 6 surge + 12 *example* terminating pods |
| --- | ---: | ---: | ---: | ---: | ---: |
| Existing combined / rollback | 12 | 12 | 144 | 216 | 360 |
| Candidate request + accounting | 10 | 10 | 120 | 180 | 300 |

The combined budget also covers a rollout from the former four-worker default:
each old pod retained eight and allowed four overflow connections in aggregate.
For split-mode or other mixed revisions, calculate each revision separately.

The last column is only an overlap example, **not** an upper bound: terminating
pods can overlap a rollout beyond the configured `maxSurge: 50%`. For #7034's
fleet worksheet, record the selected revision and worker count, effective
request/accounting/overflow limits, HPA peak, surge, *measured* terminating
pods and connection lifetimes. Compute `HTTP maximum = workers × (ready pods +
surge pods + terminating pods) × (combined size + overflow)` with split off;
with split on use `(request size + accounting size)` instead. Separately record
database `max_connections`, reserved slots, diagnostics, migrations, tick and
callback concurrency (`NullPool` is not zero connections), other services and
the agreed headroom policy. Do not activate until that budget covers the
worst plausible overlap and the authorized operator confirms it. Numbers in
this example are source settings, not live connection measurements or approval.

After rollout, verify the serving source, worker settings and gateway/tick image
parity. Check connection counts, pool acquisition failures, authorization errors,
pod health and the original API/browser latency gates using the
[Delivery Flow validation runbook](delivery-flow-read-validation.md). Repository
tests and isolated connection diagnostics do not establish live page acceptance.

For a split-mode canary, the named operator first verifies the approved private
target, fleet headroom and running revision; enables only the owned HTTP
Deployment, observes pool pressure and classified 401/403/429/503 failures,
and retains accounting and audit evidence. No automated run is authorized by
this documentation. For rollback, stop new canary work, allow in-flight
accounting to drain and check no accounting worker owns an open transaction.
Restore `BG_RDS_SPLIT_POOL_ENABLED=false` while retaining
`BG_RDS_POOL_ENABLED=true`, `BG_RDS_POOL_SIZE=8` and
`BG_RDS_POOL_MAX_OVERFLOW=4` on the same single-worker HTTP Deployment; verify the compatible
combined 12-connection-per-pod ceiling and healthy traffic. Do not use
`BG_RDS_POOL_ENABLED=false` as a substitute for reverting the split: that
changes the current pooled baseline into `NullPool`. Record rollback in the
deployment source so the next automated deployment preserves it. Never delete
accounting holds or bypass authorization or #7032 active gates; preserve TLS
and wait for the named operator's deployment and cleanup evidence.
