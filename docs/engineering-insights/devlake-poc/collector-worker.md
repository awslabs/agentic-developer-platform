# Run an admitted two-source collection

The Insights collector accepts a job admitted by the Product gateway. It does
not accept credentials, source identities or authorization assertions in its job
configuration. Profile selection alone still grants no collection. Deployment,
native-source reconciliation and live acceptance are separate authorized work.

## Prerequisites and admission

Apply gateway migration head `169_merge_collection` (which retains both the
installation-delegation and service-work migration histories) and the cell's current
schema-v2 migration and grants before starting the worker. Both
`FEATURE_DEVLAKE_CONNECTORS_ENABLED` and `DEVLAKE_COLLECTION_ENABLED` must be
`true` in the gateway and worker. The gateway module must also be enabled.

A current human Product member requests
`POST /v1/products/{product_id}/projects/{project_id}/collection-jobs`, with the
existing `Idempotency-Key` and `If-Profile-Revision` headers. The JSON body contains
two `grant_ids`, a timezone-qualified `cutoff` within the past 30 days, and optional
`page_size` (1–100, default 100) and `page_limit` (1–5, default 5). The page limit
applies across entity kinds for each source, including repository/board reads;
insufficient coverage fails rather than advancing the checkpoint.

**Current implementation limitation:** the supported pair is a verified GitHub repository bound to an owned GitHub App
installation and a verified Jira Cloud board bound to an accessible `jira`
`basic_auth` vault credential. Jira's existing secret contains `url`, `email`
and `token`; only a root HTTPS Jira Cloud site is supported. The broker never
returns these credentials to the worker. Unsupported connection kinds deny.
This is not the required user setup: ADP already has Jira connections. The
[connection-reuse refinement](existing-connection-reuse.md) requires the missing
bridge to existing ADP adapters and current source authority before live launch.
Do not register a second basic-auth credential to work around that gap.
Both explicit, unexpired collection grants must select the same registered
cell and consumer. One active job per cell prevents competing campaigns.

Admission derives two child source operations and delegations from the stored
bindings. GitHub delegation references the actual installation, not a fabricated
credential. The response supplies `job_id`, `tenant_id` and `state`.

## Assigned worker configuration

The workload launcher supplies a private, read-only JSON file. Its `Cells` list
maps opaque gateway database/role references to previously registered targets;
it is trusted deployment configuration, not user-supplied job input. A mapping
must match both source contexts exactly. The following values are fictional
placeholders, not a usable deployment configuration:

```json
{
  "GatewayURL": "https://gateway.example.test",
  "Region": "us-east-1",
  "TenantID": "assigned-tenant",
  "JobID": "returned-job-id",
  "ClaimKey": "stable-workload-claim-key",
  "Cells": [{
    "DatabaseRef": "registered-database-ref",
    "RoleRef": "registered-role-ref",
    "ResourceID": "cluster-00000000000000000000000000",
    "Host": "registered-cell.example.test",
    "Database": "registered_database",
    "Writer": "registered_writer",
    "Reader": "registered_reader",
    "Region": "us-east-1",
    "CAPath": "/run/private/rds-ca.pem"
  }]
}
```

In the authorized worker image, run:

```sh
/usr/local/bin/collector-worker --config /run/private/collection-job.json
```

The image supplies its schema and source manifests. The gateway uses HTTPS and
IAM-signed requests from the renewable workload identity. `celldb.Open` opens
separate writer and reader pools, renewing IAM tokens on physical connections
and verifying TLS. No static database password or credential fallback exists.
The image and private target configuration must be supplied by the authorized
deployment process; this command does not provision or start a deployment.

## Lease, collection and replay

The internal IAM-only job routes are `receipt`, `claim`, `renew` and `finish` beneath
`/internal/v1/collection-jobs/{tenant_id}/{job_id}`. Claim accepts `claim_key`;
renew and finish require the returned `attempt_id` and `lease_fence`. A live
claim is idempotent only for the same key. An expired lease receives a new
attempt/fence, invalidating prior source contexts. Leases last at most 120 seconds,
jobs at most 30 minutes, and jobs permit at most ten attempts. Expired work is
denied immediately and terminalized when its job is next accessed. Failure or
cancellation retires the child delegations; module disable also cancels pending
and running operations. Terminal records are retained.

Before claiming work, the worker posts its stable `claim_key` to `receipt`.
The gateway restricts this metadata read to the job's registered IAM consumer;
terminal recovery also requires the original claim key. Pending/running jobs
continue through ordinary claim admission. Successful finish stores
`content_digest`, `batch_id` and `items` atomically with terminal state and source
revocation. If that response is lost, a restart returns the stored result without
claiming, renewing, opening cell pools or fetching source records. Receipt reads
do not extend leases, recreate source contexts or change the original attempt.
They remain available after the completed job's lease expires. Failed, cancelled
or expired jobs cannot recover as success. Older digest-only receipts remain
readable metadata, but the worker rejects them rather than inventing an item count.

The worker rechecks both source contexts around bounded broker reads and before
the shared commit. The broker restricts reads to the selected repository/board,
accounts for page budget durably, and rejects redirects and oversized responses.
Native decoders preserve exact record bytes; private records and out-of-window
issues/PRs are excluded before persistence. Missing Jira security classification
or incomplete pagination denies the batch. Native GitHub issue-to-PR references
are linked only when both independently decoded records identify the same URL;
no production, incident or heuristic links are inferred.

Workspace updates, exact-byte batch sealing and checkpoints share one fenced SQL
transaction. Removal envelopes contain no newly private native payload. Immutable
earlier batches remain readable. A repeated sealed attempt loads and verifies its
stored bytes without refetching; a lost completion response does not turn a
durably sealed batch into a failure. A successful run emits only batch ID,
content digest and changed-item count. The shared normalized query is checked
against sealed history before completion; raw/native and normalized data remain
inside the registered cell.

## Local checks and their limits

From `modules/gateway`, after `uv sync --frozen --extra dev`, run:

```sh
uv run --frozen --extra dev pytest tests/products/test_collection_jobs.py tests/products/test_collection_broker.py
```

The maintained collection-contract CI stages the locked DevLake backend with Go
1.26.0, builds the worker test executable, then runs
`modules.insights.tests.devlake_poc.test_collection_schema_postgres` against
disposable PostgreSQL writer/reader roles. Its entry-point case exercises
first/new scopes, exact native bytes, private exclusions, native links, immutable
B1/B2, lost-response replay, incomplete pages, missing privacy, revocation,
throttling and receipt mismatch. HTTP providers and workload signing are injected
only by tests; the production executable has no bypass flag. These checks do not
establish deployed IAM/TLS, native API coverage, scheduling or live acceptance.

The cross-language recovery regression connects the Go runtime to the real
gateway routes with a synthetic sealed-result receipt. Its local HTTP bridge
drops the finish response only after the gateway has committed success and
revoked both sources. Two runtime restarts must return the identical receipt,
without any claim or source requests. To run it, first build the test binary in
the staged, locked DevLake backend using Go 1.26.0:

```sh
go test -mod=readonly -c -o /tmp/s03-recovery.test ./cmd/adp-collector-worker/internal/worker
```

Then, from `modules/gateway` with its test dependencies installed:

```sh
DEVLAKE_S03_RECOVERY_TEST_BINARY=/tmp/s03-recovery.test uv run --frozen --extra dev pytest tests/products/test_collection_jobs.py -k go_worker_recovers
```

The test explicitly skips when the binary is not supplied. It tests the gateway
lifecycle with an in-memory database; the separate PostgreSQL entry-point test
exercises actual collection and sealing with a terminal-aware gateway fixture.
