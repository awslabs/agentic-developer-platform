# L5 Services module

The installable `l5_services` package provides `ProductClient` for Product
Profile, revision, change, binding and source reads and for pinned source content.
The caller supplies an authenticated `httpx.AsyncClient`; the library neither
acquires ambient credentials nor uses Profile SQL. Current/historical metadata
reads use the internal IAM gateway path rather than public human/JWT routes,
where the edge deliberately strips IAM identity headers. The server validates
the stored delegation on each read; work attempts are checked separately on
pinned source and broker requests. Source capture and broker routes are
separate gateway work and are not supplied by this package alone.

From `modules/L5-services/`, install dependencies and run its transport tests:

```sh
uv run python -m unittest discover -s tests -p test_product_client.py
```

All architecture, contracts, backlog and verification documentation is maintained
in [docs/l5-services](../../docs/l5-services/README.md).

The SQS handler is `l5_services.workers.execution.lambda_handler` with partial
batch-failure reporting. Deployment provides `L5_WORKER_FACTORY` as an importable
`module:factory` returning an `ExecutionService`. The factory supplies the SQL
session maker, authenticated pinned-source client, capability adapter, queue
publisher, current-authority callback and (for multiple sources) private archive
store. Only opaque work ID and generation travel in queue messages; queue
publication happens after the SQL transition commits. Deployment of the factory,
queue mapping and IAM transport is owned by SA15; this package does not infer
provider credentials or a public caller from the queue payload.

For Well-Architected later-task reuse, the gateway accepts an optional trusted
`app.state.l5_wa_consumer_resolver(scope)` returning qualified
`WellArchitectedConsumerClient` instances keyed by `product-context` and
`engineering-insights`. The scope contains tenant, Product and project IDs;
the gateway passes clients to result reads only when their admitted destination,
scope and task-fencing paths match. An absent or unqualified peer cannot enable
reuse. Worker-side publication requires separately configured, peer-admitted
clients; the gateway resolver does not create a second publication path.

For WA07 handoff, keep the revision-pinned acceptance/cost/cleanup ledger in a
private operator store **outside this repository**. From `modules/L5-services/`,
run `uv run --no-sync python scripts/wa07-acceptance.py --private-ledger PRIVATE_LEDGER`
where `PRIVATE_LEDGER` is the authorized private JSON file. The checker requires
every epic, story and scenario row, separate known/unknown ADP, provider and
ancillary cost categories, and terminal/readback proof before marking owned
resources removed. It prints only allowlisted acceptance, cost and resource
states, owner aliases and blocker codes; it does not print private resource
identities or receipt locations.
A validated ledger is not itself installed acceptance: required blocked and
not-run rows keep the issue incomplete until their named owners verify them.

The gateway admits DevOps investigations only when a linked, ready Product
configuration selects the exact ready binding, pinned source references and
current execution delegation. The deployment must supply `L5_DEVOPS_WORK_QUEUE_URL`
for the shared SQS queue; without it, new submissions fail before admission.
The unauthenticated incident ingress verifies the configured HMAC secret reference
and stores a pending receipt. Provider work additionally requires the gateway
work-authority and recovery callbacks and a configured investigation adapter;
an acknowledged receipt alone does not mean an investigation has started.

Provider submission has a separate SQL `WorkerAttempt` committed before the native
call. Its idempotency token and input hash remain stable if the worker dies or
loses the response; redelivery calls the adapter's scoped reconciliation method,
never another submit. An ambiguous readback leaves the run `outcome_unknown` and
commits another bounded reconcile intent rather than claiming provider success.

The separate scheduled handler `l5_services.workers.recovery.lambda_handler`
uses the same runtime factory. SA15 wires its one-minute trigger; no sweep is
needed to start an admitted run. Each sweep fairly considers at most 50 owed
work rows across tenants, retains SQL due times and execution leases, and
retries failed sends on the next tick. After ten unsuccessful or unacknowledged
redrive attempts, a work row is marked blocked for operator inspection rather
than retried without a bound.

The `cancel_and_dispatch` API helper commits an active cancellation intent and
immediately tries to publish its work. The worker revalidates delegated authority
before each provider action, separates local expiry/cancellation from last known
native status, and records readback counts before provider checks. A remote
running or unknown result is not reported as a confirmed stop; provider and
poll/reconcile attempts remain bounded by the admitted limits.

Worker status checks persist attempt counts before each read. A failed or unknown
read records a redacted monitoring error and schedules bounded follow-up without
replacing the last successful observation or treating the native job as failed.
Fresh complete observations with unchanged progress can flag an explicitly
configured suspected stall; partial observations reset that comparison. Native
job failure and stop are separate terminal provider outcomes. A late check is
marked overdue until a successful read restores monitoring freshness.
