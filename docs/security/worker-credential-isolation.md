# Worker credentials and least-privilege migration

The coding worker must not hold platform administrator authority, GitHub App
private keys, shared gateway credentials, or permission to read the tenant vault
directly. Credential renewal belongs to the authenticated gateway. Harnesses use
the same scoped credential contract; their tool adapters consume the current
credential at command time.

This change prepares the runtime and an IAM boundary. It does **not** remove the
live worker's AdministratorAccess attachment or activate protected workers.
The remaining migration conditions below are release blockers, tracked in
[#5195](https://github.com/aws-e/adp/issues/5195).

## Evidence behind the change

The 8–15 September 2026 management-event audit identified 34,513 events for the
exact `adp-dev-agent-scaledjob-role` session issuer. These included 7,728
GetSecretValue calls (2,002 platform GitHub App private-key reads), Cognito user
administration, EventBridge rule changes, and an SSM feature-flag write.
AdministratorAccess originated as a temporary grant for #1619. It is outside the
role's Terraform attachment management; applying its inline policy does not
remove it.

CloudTrail management events omit object/item/message operations and application
requests. Absence from this audit is not evidence that a permission is unused.
KMS events also include service-mediated operations. The audit informs tests and
migration, not an allowlist of every observed action.

## Implemented runtime contract

The registry can mark an IAM identity `requires_run_identity=true`. The existing
`authority-worker` identity always has that requirement, including older registry
rows. Credential brokers require its signed run credential and current pod proof
even when the global authority flag is off; an unavailable authority service
refuses the call. Presenting protected proof also commits a request to protected
authentication. A client cannot downgrade to the shared-secret path by stripping
its run headers or supplying a victim's matching user and invocation IDs.

Legacy internal identities remain available for migration. Their request-body
invocation lookup alone is **not** an isolation boundary. Environment variables
are agent-writable. Never describe the legacy binding test that returns a victim's
synthetic secret as evidence of security.

The protected broker reuses the existing accepted-policy, principal, selected
credential, repository, installation, workload and grant checks. GitHub renewal
revalidates after App-key lookup, after minting, and after the audit write before
delivery. A token withheld after minting is submitted to GitHub's installation
token revocation API. Revocation is best effort; failure is logged without the
token. Tokens already delivered retain their provider lifetime/revocation rules.

Protected App runs always use the broker. Bootstrap carries the provider's actual
expiry to the Node runtime and removes both private-key environment aliases.
Protected bootstrap also removes inherited shared gateway/Door keys and does not
load the shared marker HMAC key. The run-service source below supplies mediation;
deployed compatibility remains required before activation.
Concurrent timer, posting-helper and forced refresh calls share one renewal.
Transient network/429/502/503/504 failures receive at most three attempts; access
refusals are not retried and provider response bodies are not included in errors.
Unknown or expired tokens are not treated as a fresh one-hour credential.

Token publication uses an exclusive temporary file, mode 0600, and atomic rename.
The token file is published before the process environment and cache change.
Failure propagates, so the runtime cannot report successful renewal while a
running harness's tools read the old file. `git-askpass-helper` and `gh-wrapper`
read the file at command time. An arbitrary SDK that captures a token once must
use a refreshable provider or recreate its client; changing a parent's environment
does not update an already-running process. The two-hour regression uses the real
askpass helper with a frozen child environment.

PAT runs never initialize App refresh, including when App settings were inherited.
A PAT cannot generally be reissued by ADP: reconnect/rotate the user's credential
through its existing vault flow. Do not silently change the user's GitHub identity.
Composite shell commands are not automatically replayed after a 401; a caller
must explicitly declare a command safe to retry.

Bootstrap logs now use a provisioned log group without CreateLogGroup. The Node
correlation writer uses UpdateItem, preserving webhook-managed attributes and
matching its intended IAM permission.

## Run-bound marker and Knowledge Door services (#5195)

The source adds four fixed surfaces under `/internal/v1/agent/self`: `POST
/marker`, `POST /knowledge/call`, `POST /knowledge/mcp/`, and `GET
/knowledge/tools`. Each requires the protected SigV4 transport, a current run
credential, and a TokenReview-verified workload. A refreshed proof is checked
after slow reads; a changed grant or execution is refused.

Marker requests have an empty typed body. The gateway derives all marker fields
from the protected execution and grant, including the persisted chain depth. It
signs with gateway-only `ADP_MARKER_SIGNING_KEY`. Protected workers never sign
caller-selected text or fall back to a shared key. A missing key is unavailable.

The Door bridge accepts only its fixed method/path pairs, with no query string.
The gateway resolves the human's current tenant membership, Cognito subject and
linked GitHub login in PostgreSQL. Shared row locks prevent removal or reassignment
while the request is used. Platform teams are not asserted as GitHub teams. A
missing GitHub link leaves code access unresolved while preserving an available
personal identity. Service roots cannot borrow a human's identity.

Only the gateway receives `ADP_DOOR_SERVICE_URL` (an HTTP(S) origin) and
`ADP_DOOR_SERVICE_KEY` (the Door's existing internal key). It builds downstream
headers itself, drops caller credentials, cookies, session IDs and identity
headers, disables redirects and environment proxies, and bounds uploads to 1 MiB,
responses to 4 MiB and service work to 30 seconds. Private responses are withheld
if authority is withdrawn during the Door query. Run-bound Door ACLs enforce the
delegated tenant even when the legacy tenant-scope flag is off, including the
owner-only repository visibility branch.

Native MCP, experience save and recall use the worker's existing loopback proxy
at `/__run/knowledge`. This process holds only the worker's own run/pod proofs and
platform transport identity. It is **not** a privileged supervisor. Static MCP
configuration holds no credentials; the bridge reads rotating proof files on each
request and signs only the fixed gateway paths. Protected callers ignore mutable
identity environment variables and never fall back to direct Door authentication.

These source changes do not mount keys, activate flags, provision IAM or remove
live permissions. Scoped rollout and compatibility verification remain required.

## Run artifact uploads (#5195)

Protected workers archive transcripts, tool spills, failed GitHub comments and git
backups with `POST /internal/v1/agent/self/artifacts/{kind}`. The five fixed kinds
are `transcript`, `spill`, `comment`, `git-changes` and `git-manifest`. The gateway
selects its configured logs/fallback bucket and derives the prefix from hashes of
the verified tenant and invocation plus the current attempt. Content hashes make
identical uploads converge. Neither request bodies nor headers select an object
key, tenant, run, ACL, encryption key or bucket. Responses contain a URI, key and
content digest; no S3 credentials or presigned capability is returned.

Uploads are limited to 8 MiB and 30 seconds, with bounded S3 connection/read timeouts
and no automatic retry. Run and workload proofs are checked after request streaming
and again after the storage call. An upload completed during revocation can leave
an object under its original run prefix, but no receipt is released to an expired
caller. Workers report an explicit best-effort archive failure for oversized or
refused artifacts and do not fall back to shared S3. Workspace spill locators remain
usable independently of archival. The status service accepts transcript pointers
only under the presenting run/attempt's transcript prefix, preventing an own-row
write from becoming a read of another run's private artifact.

The separate legacy Beads/Dolt S3 remote remains an outstanding source dependency:
its shared repository state is not one of these five archive kinds. It must be
mediated or explicitly migrated before the coding role's shared S3 access can be
removed. This checkpoint is not complete artifact isolation or live acceptance.

## Prepared IAM contract

The infrastructure follow-up narrows the existing protected role, with its distinct
service account and permissions boundary. It denies all direct Secrets Manager
access, IAM mutation, direct STS assumption/federation, platform administration,
and direct Bedrock inference. GitHub/vault/task AWS credentials and model requests
cross their authenticated gateway routes. Task AWS sessions retain the accepted
user-credential contract in [the Stage 1 policy contract](../orchestration/stage1-policy-rollout.md); the coding identity itself
does not acquire administrator or customer-deployment permissions.

Operational access is limited to the current environment/account's worker queue,
artifact buckets, correlation-pointer table, provisioned log groups and
`ADP/Provenance` metric namespace. KMS is limited to the correlation table's key
through DynamoDB. The old role's cross-environment wildcards are not inherited.
Queue/artifact access still spans runs in this environment. This is not complete
supervisor isolation or a claim that all worker data is isolated per run.

## Rollout and remaining release conditions

Runtime and infrastructure are separate PRs. Runtime merged in #5196. The IAM
definitions can merge with the checked-in
`.github/deployment-holds/webhook-infra.md` hold: the webhook deployment workflow
then skips infrastructure and mixed releases, including manual dispatches,
before any artifacts, state or infrastructure are changed. Code-only Lambda
pushes remain eligible. The hold persists for later commits and must only be
removed in a reviewed rollout change after the conditions below are satisfied.
Follow the canonical deployment guide and the scoped prerequisite proposal #5176;
merging the definitions does not authorize a broad apply or worker activation.

Before activation:

1. Move the remaining marker-signing and Knowledge Door shared-key functions behind
   authenticated services or a supervisor isolated from agent-authored code. The
   proposed deny-all-secrets boundary deliberately removes these reads; their
   current fallback/degradation is not functional acceptance. A child process or
   different environment variables under the same UID do not provide isolation.
   Move shared queue/artifact housekeeping as part of the supervisor migration.
2. Inventory all platform Kubernetes access for the old principal, including EKS
   access entries, aws-auth and implicit creator access. Remove it and verify
   isolation before enabling the existing customer task-source session mechanism.
   IAM session policies do not constrain Kubernetes authorization. Preserve
   customer destination trust, ExternalIds, tags, duration and role chaining.
3. Prepare compatible immutable gateway/worker images and the protected registry,
   signing, TokenReview/RBAC and work-ownership prerequisites from #5176. Keep
   authority/task-source activation flags off during preparation. Existing active
   orchestration must not be redispatched or have its credentials removed mid-run.
4. Produce a fresh plan restricted to reviewed IAM/registry/service-account
   resources. Reject unrelated RDS or other drift. The historical #5176 plan is
   not evidence for this new boundary. Review the explicit out-of-band removal of
   AdministratorAccess, any old sessions and any remaining alternative grants.
5. Exercise a controlled cohort before migration: ordinary coding, logs, GitHub
   renewal beyond one hour, cancellation/refusal, alternate harness tool access,
   selected raw/proxy/file vault credentials, saved customer-role assumption,
   default SDK refresh and role chaining. Validate actual AWS identities and
   denied victim-user/run/repo substitution. IAM simulation is not a live canary.
6. Drain or complete old workers, migrate the verified cohort and remove the old
   administrator attachment. Recheck effective IAM and Kubernetes access. Do not
   mark #1619 resolved until these live facts are recorded.

Rollback stops new protected admissions and preserves in-flight ownership and
provider-session records. It must not restore AdministratorAccess or turn a
protected identity into a legacy shared-secret caller.


## Validation

The runtime regression selection covers gateway internal/authority endpoints and
user-credential policies, Node token/identity/posting helpers, and Python worker
bootstrap/credential clients. It includes synthetic victim-run substitution,
revocation during provider calls, PAT separation, concurrent refresh, publication
failure and a simulated two-hour run using the real askpass helper. These tests
use controlled provider fixtures, not other users' live credentials.

The infrastructure follow-up renders the actual Terraform policy expressions
with fixture resource ARNs. Local Terraform validation and boundary/manifest
checks are separate from live acceptance. Read-only IAM custom-policy simulation
in account `879318057152` verified 16 action/resource cases against the boundary
even with an additional administrator identity policy. The concrete CloudWatch
log-stream case returned implicitDeny; a standalone logs:PutLogEvents policy with
Resource `*` produced the same result. This remains a simulator limitation or
resource-model question requiring a live scoped logging canary, not grounds to
expand the worker's permissions. No live Terraform plan/apply or IAM mutation is
claimed by this verification.
