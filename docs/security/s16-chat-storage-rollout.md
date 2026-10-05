# S16 Chat Storage Ownership — Coordinated Rollout and Acceptance

References: #5615 (parent), PR #5857 (merged source), #5973 (continuation).
Source tests and this procedure do not establish runtime acceptance. Keep #5615
open until the artifact, permission and controlled runtime evidence below passes.

## Verified starting point (2026-09-25 UTC)

The supervising operator inspected the deployed ZIP for
`adp-dev-agent-gateway-ingest` and verified its bytes against CodeSha256
`X9QeKvfPoz9mmuqdG3rpa5bNVc0tVTNlyutfTzWtS1U=`. Its upload-token handler still
calls `_reserve_upload_session`: the merged existing-session requirement has
**not** reached that artifact. LastModified alone cannot establish code identity.

The operator applied the reviewed Terraform storage changes to
`adp-dev-chat-artifacts-000000000101`: versioning is Enabled, current objects
expire after 30 days, and noncurrent versions after 7 days. These protections
are live; repeat readback before rollout, without recreating the bucket.
The inspected live sweeper role still allowed bucket-wide ListBucket and
DeleteObject. It had no DeleteObjectVersion grant in the inspected policies;
its narrower source policy still requires deployment and effective-policy review.

Evidence: [deployed ingest](https://github.com/aws-e/adp/issues/5615#issuecomment-5826609252),
[versioning](https://github.com/aws-e/adp/issues/5615#issuecomment-5826624520),
[retention](https://github.com/aws-e/adp/issues/5615#issuecomment-5826644638).
These are dated observations, not a claim that all current components match.

## Coordinated release and artifact provenance

Coordinate with the operator deploying the gateway/task infrastructure. Record
current digests and pending changes before planning; do not redeploy main over
an unrelated live change. Use the canonical deployment guide and real build
artifacts. Review the saved Terraform plan and stop on unrelated replacements.

1. Pin the reviewed source revision and build ingest, response, agent/worker,
   sweeper and frontend artifacts with the repository build procedures. Record
   revision, dependency lockfiles, build command, build output and artifact hashes.
   Inspect the actual previous artifacts for compatibility; do not assume that
   a previous ingest does or does not support `create-session`.
2. Verify versioning/lifecycle readback. Review the effective permissions below.
   Deploy the reviewed sweeper initially with `SWEEPER_DRY_RUN=true`; inspect
   its environment and artifact to prove dry-run support. Keep deletion paused
   if either cannot be established.
3. Deploy the compatible ingest/response pair and required worker artifacts.
   Compare each built ZIP's base64 SHA256 with Lambda CodeSha256 and its deployed
   version/alias. Inspect the packaged handlers for server-issued sessions,
   existing-session upload checks and owner/generation response conditions.
   For containers, bind source/build evidence to the registry digest and verify
   the running pod's imageID uses that digest. A mutable tag is insufficient.
4. Publish the frontend built from that revision. Compare the local build
   manifest and compiled asset SHA256 hashes with the published S3 objects and
   assets actually served via CloudFront. TypeScript source filenames are not
   expected in the published bucket. Invalidate the relevant distribution and
   wait for completion, then fetch the HTML and referenced assets again.
5. Have operators reload their controlled browser tabs and execute the runtime
   matrix below. Confirm the browser sends `create-session`, receives the
   server-issued ID, and reuses it for messages and all attachments. Record the
   bundle identity, request correlation and response frames with secrets removed.
6. Validate sweeper dry-run using only controlled fixture OldImages. Review
   intended prefixes and catalog rows and confirm zero deletions. Enable normal
   sweeping only after policy checks and fixture acceptance, then demonstrate
   recovery of a controlled deleted object version within retention.

Do not log credentials, presigned URLs, JWTs or user content in issue evidence.
Store raw artifacts securely; publish hashes, timestamps and redacted results.

## Open tabs, compatibility and rollback

CloudFront invalidation cannot replace JavaScript already running in a tab.
An old client naming a new session is refused. The new backend sends a
`session_invalid` frame; old clients may ignore it. Communicate the reload
requirement through the established release channel. Confirm recovery by
capturing the actual WebSocket frame and opening a server-issued session.
The send helper does not necessarily log each payload: searching CloudWatch
for `session_invalid` is not a reliable delivery check.

Existing sessions may continue only when their complete owner identity and
stored generation are compatible with the deployed code. Legacy/ownerless,
expired or foreign rows remain refused; do not promise all old conversations
or in-flight tasks survive a release. Verify owned reconnect and pending-task
response delivery before general rollout.

Record exact previous Lambda versions, container digests and frontend manifest
before deployment. Prefer a forward fix or pause affected intake if acceptance
fails. Roll back only to a verified compatible artifact set that preserves
ownership enforcement; retain versioning, retention and narrowed permissions.
Restoring the observed old ingest would restore the known upload-reservation
gap and is not a security-preserving rollback. Explicitly document that risk
and keep affected uploads disabled if no safe previous artifact exists. Re-run
the same positive and negative fixture checks after any rollback.

## Effective storage permission review

For each actual deployed workload, resolve its role (including pod service
account and assumed role), attached and inline policies, default managed-policy
versions, permissions boundary, bucket/resource policies and relevant SCPs.
Record allowed and denied action/resource/context tuples using IAM simulation
and controlled calls. Simulation alone does not prove all resource-policy,
trust-policy or SCP outcomes. An application prefix check is not an IAM boundary.
The following is the review matrix, not a claim that these checks already pass:

| Consumer | Actions/resources to inspect and exercise | Required negative checks |
|---|---|---|
| Ingest upload/catalog | GetItem/PutItem/UpdateItem on exact session and catalog tables; PutObject/GetObject on the reviewed artifact bucket hierarchy; KMS operations if encryption requires them | Other buckets/tables, unapproved key namespaces and destructive S3 actions denied; foreign sessions refused before signing or catalog writes |
| Response | GetItem/UpdateItem on the exact sessions table; delivery permission on the intended WebSocket API | Other tables/APIs denied; application owner + generation condition rejects stale/foreign writes |
| Agent/worker artifact consumer | GetObject/PutObject on intended artifact paths; exact catalog GetItem/Query/write permissions needed by the deployed implementation | Foreign key/session and missing tenant refused at the application boundary; record whether shared IAM can address other tenants instead of claiming per-tenant IAM isolation |
| Session sweeper | Stream reads on the session stream; reviewed catalog/table cleanup actions; ListBucket with `s3:prefix` restricted to `o/*/t/*/u/*/s/*/`; DeleteObject restricted to that bucket hierarchy | Flat/partial prefixes and other buckets denied; DeleteObjectVersion must be effectively denied so retention recovery remains possible; no unrelated table cleanup |

Source references: `infra/chat-agent-infra.tf`, the gateway module IAM and
actual worker role configuration. Use real resource ARNs obtained by readback;
never substitute fabricated role assumptions as live evidence. Review whether
any wildcard identity grant or resource policy bypasses the intended ceiling.
Record the remaining application-enforced isolation when IAM is shared.

Read back S3 GetBucketVersioning and GetBucketLifecycleConfiguration and check
all overlapping rules. Versioning is not a substitute for permissions: lifecycle
can permanently expire old versions, and DeleteObjectVersion could bypass the
recovery window if allowed. Confirm restoration works on fixture objects.

## Controlled runtime acceptance (pending until executed)

Establish two legitimate test users through supported authentication: A and B
in different tenants, plus C in A's tenant with a different user identity.
Use existing operator-controlled identities where available. Do not invent
JWTs, tenant headers, customer identities or passing results. If those fixtures
are unavailable, record runtime acceptance as pending and provision them through
the supported administrative and organization onboarding flows before executing
dependent probes. Never repurpose customer accounts or rows for these fixtures.

Use A's browser to create an owned session and two harmless attachments. Record
its exact session key, catalog keys and S3 object keys/version IDs in protected
evidence. Before each negative test take consistent DDB reads of those known
fixture keys, catalog queries and object metadata/content hashes. Afterward
compare messages, threads, connection owner, catalog rows and S3 versions; allow
only changes explicitly caused by the positive fixture step. Do not scan or
sample customer rows. DDB Scan is not chronological; S3 key order is not recency.

| Probe | Steps | Required evidence |
|---|---|---|
| Normal creation/message | A requests `create-session`, sends a message using returned ID | Successful server ID response, message and response persisted/delivered with A's ownership |
| Reconnect | Disconnect A, authenticate again and send on that same owned session | Authorized connection rebind; response reaches new connection without owner change |
| Multiple attachments | A requests two upload tokens, uploads harmless bytes and completes each; list/fetch as A | Two distinct server-derived keys and catalog entries; content hashes match; no overwrite of the other attachment |
| Wrong user and wrong tenant | C then B attempt message, upload-token and upload-complete using A's session ID | Message returns `session_invalid` frame with `error: session not found`; upload response is `session not found` and no signed upload capability; no victim session/catalog/object mutation |
| Unknown session | Same authenticated caller repeats each operation on a never-issued random session ID | Same refusal class/payload as foreign session after normalizing echoed ID/correlation; no new reserved session/catalog row |
| Foreign key / missing tenant | Through the real artifact consumer, request A's object as B and with supported missing-tenant negative fixture | Access denied before object bytes returned; list excludes unreadable rows; no victim state change |
| Response race | Deliver a controlled task response after its fixture session owner/generation has changed, using supported worker test tooling | Conditional persistence refused; no writes to replacement session; no redirection to another owner's active connection |
| Legacy/open tab | Old compiled client attempts a new client-named session; new client accesses controlled legacy/expired row | Refusal observed on wire; reload/new session recovery works without adopting ownerless row |
| Sweeper | Feed controlled full-depth owned and legacy/partial-prefix OldImages in dry-run; then perform scoped fixture cleanup | Dry-run lists intended owned rows only and deletes nothing; malformed/legacy prefixes skipped; enforced fixture delete leaves recoverable version and does not affect neighbor fixture |

A WebSocket integration may discard the Lambda HTTP-style return value. Record
the actual client response/frame as well as handler logs, rather than treating
an internal 404 as delivered UI evidence. For upload request/response operations
check the correlated error and absence of a presigned capability. Never publish
captured token or signed-URL contents.

CloudWatch logs/metrics can corroborate these probes, but their absence cannot
prove refusal or success. Count parsed events across all pages (not JSON output
lines), record time bounds and dimensions, and correlate only controlled fixture
identifiers. Silence or a `SessionsSkipped` metric without a fixture correlation
is inconclusive.

## Evidence required for closure

| Contract | Source/test evidence | Live gate |
|---|---|---|
| Server-issued IDs, unknown/foreign refusal | `test_server_issued_session_ids.py`, `test_session_ownership.py` | Creation, negative and open-tab probes |
| Upload and downstream consumer ownership | `test_upload_endpoints.py`, `test_storage_ownership_consumers.py`, `s3-artifact-store.test.ts` | Upload, multiple attachment and foreign-key probes |
| Response and delivery ownership | `test_response_ownership.py` | Reconnect and response-race probes |
| Storage ceiling and safe cleanup | `chat-agent-infra.tf`, sweeper tests | Effective IAM readback, dry-run and version recovery |
| Coordinated rollout | Reviewed build and deployment records | Lambda ZIP hashes, running image digests and served frontend manifest |

Attach the reviewed revision, each artifact identity, effective permissions,
redacted fixture results, unchanged-victim comparisons and rollback reference to
#5615. Mark every unexecuted gate pending. A merged test/runbook PR alone does
not close the parent security package.
