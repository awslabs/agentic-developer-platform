# Historical context before live URL investigation

The hosted cyber agent uses `prepare → hypothesize → browse`, followed by its
existing evidence/review/action loop. Preparation queries Common Crawl's Parquet
index through Athena without starting a browser. The model reads the recorded
metadata, states an initial hypothesis and limitations, then tests that hypothesis
against the current site. No classification labels are imported from the index.

## Evidence and coverage

Each lookup records its Athena query ID, workgroup, database, selected crawl
partitions, scan bytes, and up to 30 newest matching index records. Records include
URL, hostname, fetch time/status, content type/language, content digest, and WARC
coordinates. Page bodies are not downloaded. The model can infer useful leads
from paths and metadata, but cannot claim historical page text or ownership from
these fields. A digest match is a lead about indexed content, not attribution.

`available` with `found: false` means no rows matched the configured scope and
crawls. `unavailable` means the query failed or exhausted its budget. `skipped`
means configuration is absent. Unsupported inputs do not start a query. None establishes
safety, maliciousness, a registration date, or absence from the full archive.
Queries constrain the hostname and its subdomains, with ancestor-registration
filters for Parquet pruning; a public suffix is not a supported domain-wide census.
The latest 30 rows are a sample, not prevalence statistics or a first-seen date.

The initial hypothesis and source IDs are retained in case JSON and reports.
Archive context remains separate from browser observations and can inform the
existing `context_assessment` even when live browsing fails. Unavailable sources
cannot support reported facts. Query output remains in S3; the dedicated query
bucket expires its intermediate results after seven days. Normal case artifacts
follow their own retention policy.

## Runtime changes

Each production broker investigation owns an isolated Python/Playwright process
group. Startup waits at most 60 seconds; actions wait at most 45 seconds. A
supervisor terminates the process group and independently attempts to stop its
recorded AgentCore session. Cleanup is `unknown` if it cannot be confirmed;
the agent must not claim success in that case. The managed browser's own timeout
remains the backstop if its creation returned no session identifier.

DOM and screenshot checkpoints cross the process boundary before subsequent
capture work. A deadline or abrupt worker exit can therefore return preserved
partial evidence; destination-policy refusals still propagate as refusals.
Screenshot failure no longer prevents DOM evidence from being
retained, and frame capture happens after the main screenshot. Both normal and
checkpoint observations contain the evidence-item inventory required by the
assessment validator. A skipped corroboration lookup preserves saved findings.

With session-owner routing enabled, new starts use the Kubernetes service without
client-IP affinity. The returned private capability includes its owner pod IP;
the worker sends subsequent actions only to that pod's fixed broker port. Existing
worker NetworkPolicy restricts this traffic to broker pods. The owner is never a
target-page URL, and capabilities remain outside artifact directories. Lost
sessions fail explicitly and are never replayed on another replica.

`/readyz` reflects admission capacity; `/healthz` checks service liveness. Capacity
errors are HTTP 503 with retry metadata, and startup/action deadlines are HTTP
504. The agent retains failed cases and reports infrastructure limitations instead
of deleting cases and repeatedly sleeping for a lease to expire.

## Configuration and release

The app Terraform module creates an Athena engine-v3 workgroup, private encrypted
S3 results bucket, projected Glue table and scoped worker policy when explicit
crawl partitions are configured. The workgroup enforces a 1 GiB scan cutoff per
query. The client verifies that cutoff before starting a query and requests
cancellation if its 45-second polling budget expires. IAM grants index-object
reads under `commoncrawl/cc-index/table/cc-main/warc/`, not WARC payload downloads.

Pass application settings through webhook-stack composition:

```hcl
domain_app_settings = {
  cyber = {
    common_crawl_partitions = "CC-MAIN-2026-39,CC-MAIN-2026-34,CC-MAIN-2026-30"
    session_owner_routing   = "true"
  }
}
```

These partition names were listed by Common Crawl on 2026-09-24; the September
Parquet partition was independently checked. Refresh this explicit list when
releasing, and verify index-table availability as well as the crawl's announcement.
The official schema is maintained at
<https://github.com/commoncrawl/cc-index-table/blob/main/src/sql/athena/cc-index-create-table-flat.sql>.

The app supplies `CYBER_CC_DATABASE`, `CYBER_CC_TABLE`, `CYBER_CC_WORKGROUP`,
`CYBER_CC_CRAWLS`, and `CYBER_CC_REGION` to hosted workers. Existing deployments
remain disabled until partitions are configured. Existing Athena installations
can use the same environment contract, provided their workgroup enforces the
required output location and scan cap and their table follows the index schema.

Release matching worker and broker images before enabling owner routing. The
compatibility image pin remains unchanged in source: a Terraform apply using an
old broker image must not be mistaken for release of the new process supervisor.
Use the repository's normal image and saved-plan deployment procedures. Drain
active investigations before changing the routing mode. This change does not
grant reasoning workers direct AgentCore browser credentials.

Validate the release with an archive hit and miss, an unavailable archive query,
a live multi-page controlled case, and a deliberately stalled worker. Check query
IDs and S3 results, hypothesis provenance, browser-state continuity, preserved
evidence, terminal AWS session status, reclaimed capacity and artifact access.
Historical targets and differing models are not a detection-accuracy benchmark.

## Validation on 2026-09-24

Real Athena engine-v3 queries against `CC-MAIN-2026-39` exercised this client and
the projected table schema in account `879318057152`, region `us-east-1`:

| Control | Result | Bytes scanned | Query ID |
| --- | --- | ---: | --- |
| `example.com` | 30 sampled records, limit reached | 6,701,835 | `d7cbc176-e85e-41c5-a478-23d672465a45` |
| Unique subdomain under `example.com` | No matching records | 922,337 | `37e33ea5-56b2-49b1-a6f5-c94d00f068ca` |

Both executions succeeded. Temporary Glue database/table, Athena workgroup and
private S3 results bucket were deleted after checking the results. No archived
page bodies were fetched or saved locally. The first acceptance attempt used the
existing evidence bucket, whose policy rejected the operator's write; the successful
run used the separate private results bucket specified by this design.

These checks used the operator identity. Worker IAM configuration is covered by
Terraform tests; the deployed hosted worker still needs release acceptance with
its own role and the configured catalog. Process tests include real offline
Chromium navigation with session state, screenshots and evidence inventories,
plus hung startup/action, process crash, cleanup races and checkpoint recovery.
