# Complete domain installation (U23 / #5327)

The [authoritative Superplane design](../DESIGN.md) governs architecture and ownership.
This document provides supporting implementation detail or historical evidence;
its availability statements do not imply that pending design requirements are implemented.

`modules/domain-apps/superplane/deploy.sh` plans, checks and executes an installation of the API, controller, platform monitor and pinned SkyPilot server on an **existing** ADP installation. It never calls a platform deployment script. Actual installation, database mutation, feature activation and workload operation remain subject to the accepted installation authorization.

The explicit `control_plane_only: true` mode installs organization administration,
the controller registration manager, platform monitor and pinned SkyPilot service
with zero registered workspaces. It verifies authenticated public reads and state
after restarting the API/controller. Workspace execution remains unavailable: the
full installation still refuses absent production credential/operation adapters.
Management health does not imply governed provisioning or workload readiness.

Each singleton service has an installation-owned PodDisruptionBudget requiring
one available replica. This blocks voluntary node consolidation/drain from
evicting the only instance. Its selector excludes bootstrap/migration Job pods.
Explicit Deployment replacement still works; deliberate node maintenance must
coordinate availability or a reviewed temporary budget change. These budgets
do not provide multiple-replica high availability or prevent involuntary failure.

SkyPilot reserves its full bounded server allocation (1 CPU and 2 GiB), in
addition to its authenticated sidecar. The scheduler must account for that
allocation when placing it alongside ADP services. Lower requests can pack the
server onto small nodes where memory reclaim stalls cause health-check failures
and repeated restarts even before Kubernetes reports node memory pressure.

## Inputs and prerequisites

Use Python 3.12 with `PyYAML`, `httpx` and `boto3`, Terraform matching the maintained module,
AWS CLI v2 with conditional S3 PUT/DELETE support, and kubectl. Set
`image_execution: cluster` for private RDS or machines without Docker. This path
verifies ECR manifest/config digests and OCI source labels, tests actual
NetworkPolicy traffic, and executes pinned image checks in a temporary restricted
namespace. Probe pods have no AWS/Kubernetes identity; database probes allow only
DNS and the selected RDS addresses/port. Cleanup checks the namespace UID and
records completion. The default `docker` path requires local Docker and database
reachability. Keep output directories private and outside the checkout.
Temporary probe pods block voluntary Karpenter/Auto Mode disruption for their
bounded lifetime, so node consolidation cannot erase an in-progress check.

Supply an environment YAML based on `environment.example.yaml` and an immutable release lock based on `releases/superplane.lock.yaml`. The example deliberately leaves unresolved environment decisions blank; it is not an execution configuration. No credentials belong in either file.

Full installation also requires `controller_profiles`, the explicit version-1
policy consumed by the deployment API. Use the structure in
[`controller-profiles.example.json`](controller-profiles.example.json) as a mapping
under that YAML key. Replace its organization/workspace keys and every unresolved
value with reviewed inputs; the template intentionally cannot pass validation.
The policy names exactly this installation's domain organization and its actual
`adp_org_id`, and includes the selected workspace. Additional workspace entries
must each contain their own explicit profiles. Control-plane-only installation
may omit the field; deployment admission then remains unavailable.
The serialized installer policy is limited to 64 KiB so both supported image
probe transports fit the operating system's per-string limit. The API's separate
256 KiB policy limit does not enlarge the installer transport limit.

Each profile pins the serving image by SHA-256 digest, its model options and exact
invocation, compatible AMI/instance/network/cluster/public-CA inputs, registered
opaque credential reference, physical GPU capacity, finite runtime and cost
limits. No image, AMI, resource allocation or credential fallback is generated.
The current controller supports one serving replica. Its serving image must
implement `superplane-token-file-header-v1`: read the token from
`SUPERPLANE_AUTH_TOKEN_FILE` and require `X-Superplane-Token` on `/healthz`.
`workload.auth_secret` names an **existing Secret in the target workspace
namespace** with a `token` key containing at least 32 characters. Create that
Secret through the separately authorized workspace credential process before
running a workload. The installer neither reads nor writes its token, and the
profile contains only its name.

The installer serializes the reviewed policy into an immutable, content-named
ConfigMap, mounted read-only in the API at
`/etc/superplane/controller-profiles/profiles.json`, and sets
`SUPERPLANE_CONTROLLER_PROFILES_FILE`. Missing ConfigMap content prevents the API
pod from starting. Policy changes select a new ConfigMap and replace the API pod;
resume and rollback retain the policy in the exact environment/receipt identity.
Regional profiles with an approved `network` may also include the closed
`node_bootstrap` descriptor documented in
[`executor/node-command/README.md`](../executor/node-command/README.md). Local
planning checks its shape; preflight and verification use the pinned API image's
canonical native validator for the complete runtime manifest and digest contract.
This supports native batch profiles without installing executor dependencies in
the operator's Python environment. It does not prepare an AMI, install SSM
documents, or grant native execution authority.
Preflight runs the maintained `build_deployment_preview`/plan validator in the
pinned API image, and private verification checks the mounted policy and digest
again. In cluster mode these checks run in the isolated preflight pod; execute CLI
and runtime regression tests only on the authorized EC2 harness.
Every profile for the selected workspace must match the installation's explicit
cluster UUID, ARN, namespace, AWS account and region. Before installation writes,
the existing EKS preflight also compares its endpoint and public CA with the
independently queried selected cluster. A self-consistent profile pointing at a
different destination is refused.

`controller_profiles_validated` proves configuration compatibility, not canonical
database registration or a running model. The actual API preview independently
checks current organization, workspace, cluster, account and credential bindings.
Installation receipts explicitly leave `serving_workload_ready: false`; a model
becomes ready only after its separately reviewed approval, paid admission and
authenticated provider/runtime checks succeed. API/controller management health
and a valid profile cannot establish that result.

The release lock must remove the three built images from `pending_images`, supply their observed ECR digests in `images`, and record `registry`, `repository` and `source_revision` under each `image_sources` entry. Set the root `source_revision` to the exact clean ADP checkout used by the maintained build lanes. Registry tags and OCI revision labels must match each image’s recorded build revision. An image from an earlier commit is reusable only when Git proves its complete component build context (including its Dockerfile) is identical to the installation revision. Both commits must be available locally; changed or unavailable source is refused. The receipt records reused build revisions and Git tree IDs. Preserve the reviewed SkyPilot 0.12.0 digest. This command consumes completed immutable builds; it does not treat a workflow dispatch as a completed build.

Both managed VPC CNI and EKS Auto Mode are supported when NetworkPolicy enforcement
is verified. Set `gateway_namespace` to the existing Gateway's actual namespace.
Native EKS Auto Mode DNS is discovered automatically during target preflight
from the cluster's service network. No DNS address is required in the environment
file. The installer derives the resolver (service CIDR network address plus 10),
refreshes the rendered manifests and records `management_dns_configuration`.
Workload and database-probe policies then allow UDP/TCP 53 to that exact address,
in addition to traditional `kube-system` DNS peers. Auto Mode runs CoreDNS as a
node system service, which cannot be selected by a namespace selector. It does
not require Superplane to install another CoreDNS Deployment. The cluster image
execution path checks Kubernetes service and private database DNS over both UDP
and TCP inside the restricted probe boundary and records `management_dns`.
The discovered configuration is included in the reviewed plan hash and is read
again on execution/resume and rollback. User inputs remain unchanged. The default
offline plan performs no AWS calls; native DNS rules appear in `manifests.yaml`
after online preflight. Missing or invalid Auto Mode service-network data refuses
preflight rather than silently producing the old namespace-only rule.
Standard clusters retain pod-based DNS. An older explicit `cluster_dns_ip` remains
accepted as an assertion against the discovered address; it is never required.
Mixed clusters must retain traditional CoreDNS for their non-Auto Mode nodes,
including managed node groups currently scaled to zero and Fargate profiles.
Removing an existing add-on is a separate platform operation: inventory both
current nodes and configured non-Auto Mode capacity, verify native DNS with the
replicas stopped, preserve recovery configuration, then verify DNS and application
health after removal. A cluster with only Auto Mode nodes today can still require
the add-on when a dormant node group starts.
See [AWS's CoreDNS considerations](https://docs.aws.amazon.com/eks/latest/userguide/auto-networking.html).

Full workspace activation requires a distinct cluster, approved CRDs/namespace,
scoped identity and controller handover. Management-only installation omits these
workspace inputs and mounts no workspace credential. It does not attach AWS roles
to service accounts; the installer supplies only the named runtime secrets.
SkyPilot binds to pod loopback behind its authenticated sidecar.
The four services explicitly opt out of injected OpenTelemetry language agents.
CloudWatch auto-monitoring can prepend incompatible Python packages to the pinned
API and SkyPilot images; per-workload opt-outs preserve their tested dependencies.
This does not change cluster-wide monitoring or the domain's health endpoints.
Service selectors include the installation ownership label used by the network
policies. AWS policy resolution requires this match to allow Service ClusterIPs
before destination translation, as well as the selected pod IPs.
The rendered SkyPilot config omits the legacy `db: {backend: postgres}` mapping:
SkyPilot 0.12 expects `db` to be a URL string. Its connection comes exclusively
from the required `SKYPILOT_DB_CONNECTION_URI` Secret reference.
`IS_SKYPILOT_SERVER=true` is required to select PostgreSQL rather than SQLite.
The pinned server expects its configuration in PostgreSQL as well, so a bounded
startup script seeds the installation's provider settings through SkyPilot's
configuration API before starting the server. Its initial file config is empty.
Private verification reads the actual engine dialect, database, schema, TLS,
initialized state tables and effective configuration; a healthy SQLite server
cannot pass installation verification.
It also supplies `USER=skypilot`: the pinned image has no password-database entry
for UID 1000, and SkyPilot calls `getpass.getuser()` while importing its modules.
Exec probes allow time for Python startup and the bounded health request, rather
than using Kubernetes' one-second default.

Install Gateway transport version 2 and its scoped route-read IAM policy through the normal reviewed platform release before running the domain installer. `/api/superplane/installation-support` must report configured `s3-conditional-domain-registration`; older or unconfigured Gateways fail preflight. The deployment ConfigMap derives `BG_SUPERPLANE_ROUTE_BUCKET` from the existing platform account placeholder. The Gateway may read only `s3://adp-terraform-state-<account>/domain-routes/<environment>/superplane/public-route.json`, not Terraform state. Each installer route write durably records its complete payload, unique revision and prior ETag before using S3 `If-None-Match` or `If-Match`; concurrent creation, replacement or stale writers refuse enable/disable, rollback and cleanup without overwriting another registration.

Version 2 no longer reads the legacy SSM route parameter. Preserve it for rollback; do not copy it over an existing S3 registration. A controlled Gateway upgrade disables the legacy route until an authorized installer resume/reinstallation publishes the verified S3 route. Perform this cutover under the existing installation gate. Subsequent activation/removal takes at most five seconds and needs no Gateway restart. An explicit `FEATURE_SUPERPLANE_ENABLED=false` overrides registration. The public proxy forwards only inventoried domain methods/paths and the original ADP bearer token; the domain API performs token and workspace authorization. It does not publish local login/token minting, internal callbacks, OpenAPI or service diagnostics.

The selected existing RDS instance, database and **two isolated schemas** (API and SkyPilot) need named migration, backup and restore owners and an available matching snapshot. Runtime and migration roles must have the correct schema search path and no rights to mutate other schemas, no role memberships that can elevate authority, and no database-creation/superuser authority. Configure migration-role default privileges so the runtime role can use migrated domain tables and sequences. The API and migration use an explicit verifying SSL context from `ca-pem`; the pinned SkyPilot package uses both psycopg2 and asyncpg, so it receives `PGSSLMODE=verify-full` and a mounted `PGSSLROOTCERT` bundle with a driver-neutral URL. Runtime API and migration sessions explicitly configure asyncpg's `search_path`; they do not rely on the ignored libpq `PGOPTIONS` variable. The maintained Alembic chain runs from the API image and must reach `033_retained_batch_results`. An image rollback does not reverse a database migration.

Three environment-scoped Secrets Manager references contain these exact JSON string fields:

| Reference | Fields |
|---|---|
| `secrets.database` | `runtime-url`, `migration-url`, `skypilot-url`: distinct scoped `postgresql://` role URLs with no query parameters, all bound to the selected RDS endpoint/database; `ca-pem`: trusted PEM certificate bundle for that RDS endpoint |
| `secrets.observation` | `submitters` (serialized JSON array), `monitor-credential`, `monitor-signing-key`, `controller-credential`, `controller-signing-key`, `skypilot-token` (at least 32 characters), `jwt-signing-key` (at least 32 characters) |
| `secrets.workspace_access` | `kubeconfig`: one workspace cluster and one static bearer/certificate identity; no exec plugins, local file references, impersonation or proxy/TLS overrides |

The submitter array has exactly two entries with distinct `submitter_id` and credentials, matching the two named credential/key pairs and the exact immutable workspace UUID. Only the monitor has `lease_scopes: ["budget_monitor/global"]`; the controller has no global lease scope. The installer reads values into memory, supplies Kubernetes Secret objects through stdin, and records only secret **version IDs**. Version changes restart the consuming pods. Output does not contain credentials.

`jwt-signing-key` signs and verifies the org-scoped tokens the API's `/auth/login`
issues. It is **required, and an installation refuses to start without it** (issue
#5683): the API previously fell back to a placeholder default committed to this
repository, so a deployment that never supplied a key ran on a value any reader of
the source could forge tokens with. The field check is set equality, so an
**existing** `secrets.observation` secret that predates this requirement is refused
until the field is added — add it before the next installer run. Generate at least
32 characters of random material; a short HS256 key can be recovered offline from a
single captured token, which would leave the tokens forgeable while appearing
fixed. Replacing this value invalidates tokens issued under the previous one, so
for an environment already running on the removed placeholder follow
[the rotation and cutover runbook](../../../../docs/runbooks/superplane-jwt-and-db-credential-rotation.md).

For a fresh installation, `adp_org_id` names the actual ADP organization (for example `aws-e`); the three UUIDs identify the new domain organization, workspace and cluster. After migration, a one-shot bootstrap Job verifies the signed ADP access token and current `org_admin` membership through `/api/auth/workspaces`, checks both again before commit, and stores an explicit organization binding and one initial `administer` workspace grant for that human subject. The policy defines the permissions implied by this grant. Resume is idempotent and never restores a revoked grant, rebinds an existing organization, or adopts an unbound legacy organization. The temporary token Secret is deleted with UID preconditions after terminal bootstrap success; an interrupted or failed Job requires recovery inspection. U21's historical identity mapping, migration and cutover remain separate.

## Organization bootstrap before the first workspace

The API bootstrap command accepts `control_plane_only: true` in
`SUPERPLANE_BOOTSTRAP_CONFIG`, with only `adp_org_id`, `org_id` and `origin`.
It verifies the ADP access token and current organization-administrator membership,
then records an explicit organization binding and `organization:administer` grant
in one transaction. It creates no workspace, cluster or workspace grant. Both
membership and token validity are checked again before commit. Repeating bootstrap
preserves existing identities and refuses revoked or restricted grants.

This grant permits organization-level administration, including listing an empty
workspace collection. Access to an individual workspace still requires that
workspace's own grant. A later full bootstrap can add the selected workspace and
its grant under the same organization. Bound ADP organizations cannot substitute
workspace grants for organization authority; unbound legacy organizations retain
their existing policy pending explicit migration.

The management controller reads this durable registry using a dedicated credential
with `controller_management/<org UUID>` scope and a finite PostgreSQL lease. An
empty registry is healthy. The API management mode blocks workspace mutations and
does not start legacy credential/bootstrap loops. Activation through the governed
provider/operation integrations remains a separate delivery requirement.

## Database and credential preparation

`--prepare-database` emits reviewable, transactional SQL without contacting AWS.
It accepts either a full environment or a management-only environment. After the
preparation is authorized, supply the selected database administrator's URL in
`SUPERPLANE_DATABASE_ADMIN_URL` for the one-shot process and use the same entry:

```sh
modules/domain-apps/superplane/deploy.sh \
  --environment /secure/environment.yaml --release-lock /secure/release.yaml \
  --output /secure/superplane-preparation --prepare-database --apply-preparation
```

This requires `image_execution: cluster`. It verifies target/source/image/backup,
creates or reuses installation-owned Secrets Manager values, and creates separate
migration/runtime/SkyPilot roles and schemas in one transaction. A fresh observation
secret includes independently generated service credentials, service signing keys, a
SkyPilot token and an API JWT signing key; none has a deployed fallback. Existing unowned
roles, schemas or secrets are refused. All three roles must authenticate over TLS
with their stored passwords and pass the schema boundary checks before preparation
succeeds. Credentials never enter argv, rendered manifests or receipt files. The
administrator credential is only in a temporary Secret removed with its namespace.
Preparation does not run migrations or claim application installation.

## Run

Use absolute paths for clarity. With complete inputs, the default command renders every action and manifest without contacting AWS:

```sh
modules/domain-apps/superplane/deploy.sh \
  --environment /secure/environment.yaml --release-lock /secure/release.yaml \
  --output /secure/superplane-run
```

Preflight verifies the relevant cluster identities and credential scope, image
capabilities, database privileges and backup identity, observes Gateway support,
and produces a checked domain-only Terraform plan. Cluster execution additionally
creates and removes the bounded probe resources described above:

```sh
modules/domain-apps/superplane/deploy.sh \
  --environment /secure/environment.yaml --release-lock /secure/release.yaml \
  --output /secure/superplane-run --resume --preflight
```

Review the saved Terraform plan and its `plan_sha256`, then obtain the retained installation authorization for the exact environment/release, database, permitted operations/spend and recovery owners. Supply an authorized ADP access token through `SUPERPLANE_VERIFICATION_TOKEN` in the process environment, never argv or a file committed to Git.

```sh
modules/domain-apps/superplane/deploy.sh \
  --environment /secure/environment.yaml --release-lock /secure/release.yaml \
  --output /secure/superplane-run --resume --execute \
  --approved-plan-sha256 <reviewed-plan-sha256>
```

Preflight is repeated, and a changed plan is refused. An atomic conditional S3 lock serializes installers across machines. Terraform applies only its saved, reviewed domain plan. The command installs foundations, runs and waits for the exact migration and fresh-tenancy bootstrap Jobs, waits for all four deployments, verifies runtime auth/schema and authenticated SkyPilot health, and waits for fresh authenticated **observations from both the controller and monitor** for the selected workspace. The controller must report healthy; a monitor result of `not_checked` is retained explicitly because an empty fleet and unavailable cost/vault probes cannot establish workload health. Degraded, unknown or unreachable reports refuse completion. Only the independently configured controller submitter can advance controller liveness; a monitor cannot refresh it. Only then does it register the public route and verify an authorized request through the existing ADP origin, invalid/missing credential denial, private-route denial and existing ADP availability. A dispatch, successful `apply`, or a ready pod alone cannot produce `installed-and-verified`.

Kubernetes writes create absent objects and update owned objects using the observed UID and resource version. Concurrent replacement or ownership changes refuse the write; they cannot adopt another installation's object. Each successful write records its returned UID before the next write.

The private JSON receipt records the source, release, environment, plan, run ID, migration Job, object UIDs, secret versions, authenticated observations and public endpoint. This is installation evidence; GPU/workload parity and the five live evaluations remain separate.

## Interruption, resume, rollback and cleanup

Terraform uses the platform's canonical `adp-terraform-locks` DynamoDB table as
well as the separate installation lock. This preserves its S3 state checksum and
coordinates plans/applies with platform CI. A checksum mismatch requires inspecting
the S3 version history; never discard state or disable locking to get past it.

Every phase records its state before starting. `--resume` requires the identical environment/release and uses the same migration and bootstrap Job identities. A nonterminal Job is waited on, never duplicated. Failed/interrupted mutations retain the shared lock with status `recovery-required`; time passing never steals it.

After the named operator verifies that the prior installer and child processes have stopped, inspect the recorded migration Job and Terraform backend lock. A still-running migration or bootstrap Job prevents lock recovery. Recovery first deletes any recorded temporary preflight namespace with UID/resource-version preconditions and verifies its absence, then releases only the recorded lock version. Lock acquisition persists a unique owner/run/nonce identity before PUT. If the response is lost, readback must match that complete identity before recovery adopts its ETag; a lost DELETE response is accepted only after verified absence. Recovery also reconciles any pending route operation and disables an unverified publication while still holding the installation lock. A changed owner or failed cleanup retains the recovery marker and lock. The same command recovers interrupted database preparation even when it never acquired an installation lock:

```sh
modules/domain-apps/superplane/deploy.sh \
  --environment /secure/environment.yaml --release-lock /secure/release.yaml \
  --output /secure/superplane-run --resume --recover-lock \
  --confirm-stopped <recorded-run-id>
```

Then rerun preflight and resume with the newly reviewed plan hash. A terminal failed migration Job is not silently deleted/retried: diagnose its schema state and follow the named restore owner's recovery decision. A new installation attempt requires the failure to be resolved first. Publication or verification failure attempts to disable the public route. A committed PUT with a lost response is recognized only by its complete recorded payload and unique revision; if the prior version remains, a requested publication retries with its original condition. Compensation instead writes a disabled operation directly, never publishing an uncommitted enable. Its receipt retains the superseded enable identity to fence a late commit. Unavailable or conflicting readback retains the pending operation and installation lock, reports route state as unknown, and requires same-receipt recovery. Before returning a compensated failure, changing rollback workloads, removing cleanup workloads, or releasing a recovery lock, the installer waits out the Gateway-advertised cache lifetime and polls the inventoried public workspace path for the Gateway feature-off response while verifying unrelated ADP health. Failed disable observation retains the recovery marker and lock. It never silently claims the previous release remains available or overwrites a foreign registration.

Rollback records and fences the current route before preparation, separately from the receipt selecting the release to restore. Both rollback and cleanup require the configured version-2 Gateway transport before mutation.

For image/configuration rollback, pass a prior **successful receipt from this same environment** using `--rollback /secure/previous/receipt.json` and a new output directory. The command verifies schema compatibility, restores pinned secret versions and the prior four-service release, and repeats private/public verification. It refuses cross-schema rollback and performs no downgrade or restore automatically.

`--cleanup /secure/failed-or-current/receipt.json` requires that receipt's exact route observation, including an explicitly recorded absence, and carries forward any pending publication evidence for reconciliation. A newer route or a legacy receipt without this evidence is refused before mutation, even when Kubernetes UIDs are unchanged. With a new output directory, cleanup disables routing and removes only recorded, still-owned Kubernetes object UIDs, using API-server UID/resource-version preconditions. Workloads are removed before network policies. It preserves namespaces, Kubernetes Secrets, databases/backups, domain Terraform/ECR resources and external workspace/provider resources for recovery, and reports those retained resources explicitly. It does not destroy the core platform or pretend to reclaim billable external capacity.

## Code validation

The offline integration harness uses instrumented tool processes and HTTP responses. It exercises the real installer sequence, missing prerequisites, account/source/image/ownership boundaries, migration and rollout failure, public verification failure, lock retention, schema-incompatible rollback, and secret-free receipts. Separate API tests exercise schema confinement against disposable PostgreSQL. Gateway tests compare the public route list with the maintained API inventory and exercise authentication forwarding and private-route denial. Go tests validate signed controller observations and SkyPilot token/redirect handling. None is presented as live installation acceptance.
