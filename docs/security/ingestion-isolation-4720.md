# Ingestion permission isolation (#4720)

This patch confines content processing to one registered asset and ingestion
attempt. It supplements the existing repository-execution restrictions; the
production structural parser remains fail-closed without an isolated backend.
MFA configuration is unchanged.

## Authority boundary

The `ingestion-receiver` init container receives one SQS message using a
queue-only IAM role with a permissions boundary. It sends only the signed
callback grant and receipt to the gateway. The gateway checks the persisted
asset, tenant, current attempt, grant digest and lifecycle state, then rebuilds
the source and scope from that row. Queue-supplied ownership, installation,
source and step fields confer no authority.

The content container has no service-account token, IRSA credentials, database
login, secret bundle or shared PVC. The EKS IRSA webhook must skip `worker`.
Scratch volumes are bounded. A network policy permits DNS, the gateway,
enrichment services and public HTTPS; it excludes private networks and metadata.
Public HTTPS is necessary for admitted GitHub and document sources and does not
constitute a hostname allowlist. Enforcement must be verified with the cluster's
CNI.

The worker can use only the following gateway operations:

- S3 GET/HEAD and exact-key POST capabilities for its admitted input/output.
  POST policies constrain file length and content type. Capabilities expire
  after five minutes; an already issued URL can remain usable for that interval
  after reindex/removal. No S3 credentials or bucket-wide listing are returned.
- Named catalog commands bound to deterministic asset/attempt run and stage IDs.
  Arbitrary SQL, ownership changes and worker-supplied ACL changes are refused.
- Vector writes with authoritative ownership metadata, asset/attempt keys and
  count-only read-back. Preparation prunes earlier generations of the same asset;
  retry clearing cannot delete another attempt. Other assets are never returned.
- DynamoDB state/history derived from the asset identity, and parameter-bound
  graph writes whose entity IDs are namespaced by asset. Workers cannot submit
  graph queries or select another asset's vertices.
- Queue acknowledgment with an HMAC receipt ticket bound to asset and attempt.

Repository clone tokens have only contents/metadata read permissions and are
restricted by GitHub repository ID. The broader installation token used for
server-side ACL resolution never leaves the gateway. Removed, completed or
superseded attempts cannot obtain further processing authority (the current
terminal attempt may still acknowledge its queue receipt).

## Compatibility

The gateway retains privileged backend access; it is the trusted enforcement
point, separate from content parsing. Legacy shared services keep their own
permissions. The ingestion role cannot assume that gateway role or read its
credentials.

Existing repository S3 layouts remain readable. Structured code-index JSON is
also written to S3, with a local scratch copy for the current job. Document and
URL outputs use asset/attempt directories. Personal and Product repository
registrations retain their existing restricted embedding path. Conflicting
legacy flattened repository filenames fail admission instead of granting access
to the other repository's artifacts.

`ingestion-refresh` now submits a purpose-specific projected Kubernetes JWT to
the gateway, which dispatches only persisted registrations. It refreshes
complete/partial assets older than 20 hours, in pages of 100. It does not use the
old clone-based SHA filter or ingest entries solely because they appear in a
text file. Each new attempt re-verifies its own output; operators should account
for the resulting daily indexing/embedding cost. Failed or uncertain dispatches
need explicit reindex through the registry.

Infrastructure discovery, correlation and knowledge maintenance retain their
existing trusted implementation in `knowledge-maintenance`. This workload does
not invoke repository ingestion, repository refresh, or the legacy queue
publisher. New code-index files are no longer placed on the shared PVC: the
Door reads S3, and the shared maintenance readers prefer published S3 indexes
while retaining a fallback for older PVC-only artifacts.
Legacy vectors without asset identity are not deleted by worker capabilities;
cleanup of those records belongs to trusted maintenance.

External S3 sources still require the existing source allowlist and separately
reviewed read permissions on the gateway. The default infrastructure grants
access only to the platform bucket. Private AWS endpoints may require narrowly
scoped network-policy rules. Do not broaden worker IAM to accommodate either.

## Rollout and acceptance

These are deployment requirements, not evidence of a completed deployment.
Follow the canonical deployment guide for target-account confirmation and any
live apply. Keep #4720 open until the runtime checks below pass.

1. Build both gateway and ingestion images. Review and apply the agent-context
   Terraform changes: receiver role/boundary, gateway backend permissions and
   `adp-ingestion-control` ConfigMap. This is additive to existing workloads.
2. Deploy/restart the gateway so it loads that ConfigMap and the two new routes.
   The CLI/workflow cutover check requires the new module and
   `INGESTION_ISOLATION_VERSION=1` in a running gateway container.
3. Suspend the old refresh CronJob and stop other unsigned publishers. Let
   existing jobs finish. Inspect/drain old unsigned queue entries; reindex the
   corresponding registered assets through the registry. Do not purge the queue
   indiscriminately. Keep a private record of jobs requiring reindex.
4. Apply the service accounts, ingestion network policy, new refresh/maintenance
   CronJobs and ScaledJob. Gradual rollout leaves existing pods intact; the
   network policy targets only the new isolation-version label.
5. Inspect an **admission-mutated pod**, not just the source manifest. Verify
   that the IRSA token and role environment are present only in the init
   container; the worker has no projected tokens, credential environment,
   secret injection, shared PVC or host mounts. Confirm the actual role policy
   and permissions boundary allow only the four queue actions on this queue.
6. In a fixture worker, confirm SDK credential discovery yields no credentials
   and that direct S3, Secrets Manager, SQS and database access is unavailable.
   Confirm connections to metadata, RDS and unrelated cluster services fail,
   while admitted downloads and gateway capabilities work. Test IPv4 and IPv6
   where enabled. Review other additive NetworkPolicies for accidental bypasses.
7. Ingest a harmless repository containing inert build-file placeholders, plus
   a private repository and registered document. Check successful clone,
   lexical/vector/artifact output, stage records and terminal callback, and
   SQS acknowledgement. No repository-supplied executable may run. Check
   optional GraphRAG when enabled, and Door search/understand read-back.
8. With two fixture assets in different scopes, attempt cross-asset object,
   catalog, vector, graph and receipt operations. Expect refusal and unchanged
   foreign data. Reindex/remove one asset and verify the previous grant is
   refused. Test the scheduled refresh identity with wrong audience/subject.
9. Record the deployed image digests, mutated-pod evidence, IAM boundary,
   network checks and fixture results privately. Close the issue only after
   these checks succeed in every applicable environment.

If acceptance fails, suspend new ingestion and retain its isolated identity
while investigating. Do not restore shared credentials as an automatic fallback.
