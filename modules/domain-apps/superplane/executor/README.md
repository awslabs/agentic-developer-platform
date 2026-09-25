# Trusted controller executor

This service runs beside the Go registration manager in a separate container/UID.
Only this service has the execution/domain database connections, ADP run and
workload credentials, and SkyPilot transport credential. The Go process receives
only the execution socket, per-operation tokens, read-only workspace credentials,
and signed-observation credentials. No fallback invokes the legacy Go provider.

Current capacity support is AWS native EKS in the cluster's account, region and
VPC. Nebius/Lambda adapter source does not make hybrid capacity reachable through
this executor. [Hybrid capacity requirements](HYBRID-CAPACITY.md) map the original
upstream scenarios to the missing provider, network, join and cleanup composition.

Assignments originate from already admitted operations and existing leased ADP
runs. `SUPERPLANE_EXECUTION_OPERATION_FILE` is a JSON array of operation IDs from
that run; it is only a selector. Gateway verifies the live run/pod, IAM scope,
current operation holder, attempt, fence, approved digest and reservation on every
RPC. The service does not create runs, admission records, approvals or leases.

`SUPERPLANE_RUN_HANDOFF_FILE` is the separate document that carries authority, and
without a live grant in it the service stays idle: no pool, socket or provider
client is opened. It is `{"version": 1, "grants": [...]}`, each grant naming exactly
`operation_id`, `attempt_id`, `job_id` and an offset-qualified `not_after`. A bare
JSON array — the selector's shape — is refused rather than read as a handoff,
because a static Secret projection binds no attempt, job or expiry and so cannot
show this pod was granted anything. Each grant is re-checked against the attempt and
job Gateway independently reports, so a copied projection cannot authorize another
attempt, and neither value is ever derived from the operation ID. The document is
re-read every cycle, so a withdrawn or rotated grant revokes without a restart.
Producing this document belongs to #5535; this service only consumes it.

A pod-private shared volume contains the socket and operation tokens, mounted
read-only in Go. A second volume contains the random controller instance ID,
written by Go and mounted read-only in the executor. The executor compares it
against `observation_leases` and the explicitly configured registry submitter ID.
Registry credentials and metadata never contain an execution token.

## Configuration

Required executor environment variables (file values are projected into this
container only):

- `SUPERPLANE_DOMAIN_DSN_FILE`, `SUPERPLANE_EXECUTION_DSN_FILE`: scoped database
  connections. Apply domain migration `018_controller_executions` and the reviewed
  shared harness schema through the existing release migration gate.
- `ADP_EXECUTION_AUTHORITY_ENDPOINT`, `AWS_REGION`, `ADP_RUN_CREDENTIAL_FILE`,
  `ADP_WORKLOAD_TOKEN_FILE`: existing ADP run authority using SigV4, the current
  run credential and TokenReview-bound workload token. This uses the already
  defined `credential:operation-delivery` IAM registry scope; code does not grant it.
- `SUPERPLANE_EXECUTION_SOCKET`, `SUPERPLANE_EXECUTION_CREDENTIALS_DIR`,
  `SUPERPLANE_CONTROLLER_INSTANCE_FILE`, `SUPERPLANE_REGISTRY_SUBMITTER_ID`,
  `SUPERPLANE_WORKER_GID`, `SUPERPLANE_EXECUTION_OPERATION_FILE`.
- `SKYPILOT_URL`, `SKYPILOT_SERVICE_TOKEN_FILE`: the authenticated 0.12.0 proxy.
- `SUPERPLANE_WORKSPACE_CREDENTIALS_DIR`, `SUPERPLANE_MANAGEMENT_API_SERVER`:
  exact workspace kubeconfig projections and the management endpoint exclusion.

Go also requires its existing API/organization/registry settings and
`SUPERPLANE_WORKSPACE_OBSERVATIONS_DIR`, containing `<workspace>.credential` and
`<workspace>.signing-key`. Its `/readyz` means registration management is healthy,
including zero targets. `/statusz` separately reports compiled execution support,
current assignment capability, target verification and operation state.
Signed native-node health reads use the admitted workspace label on EKS nodes;
legacy SuperplaneNode counts are not used to report these nodes healthy.

## Governed deployment requests

The API requires `SUPERPLANE_CONTROLLER_PROFILES_FILE` to name a readable installed
JSON policy. There are no implicit image, AMI, credentials, namespace or resource
defaults. The document has this shape:

```json
{"version":1,"tenants":{"<domain-org-uuid>":{"adp_org_id":"<bound-adp-org>","workspaces":{"<workspace-uuid>":{"<profile-id>":{}}}}}}
```

Each profile must supply all fields validated by
[`deployment_plan.py`](superplane_executor/deployment_plan.py): canonical cluster,
account and namespace bindings; prepared node image/type/profile and network
settings; real public CA; bounded GPUs/runtime/cost; an existing opaque credential
reference; exact model options; and a digest-pinned serving image implementing
`superplane-token-file-header-v1`. The `workload` carries command, arguments,
resource requests, port and the name of an existing authentication Secret. That
Secret is a prerequisite, not an object this controller creates or deletes.
Unsupported replicas, model option changes and unbound targets are refused
before admission. [Batch profiles](BATCH-API.md) use the separate batch producer
and share quota with serving workloads.

`POST /workspaces/{workspace}/deployments/preview` returns the exact approval
request and revision for a caller-supplied operation UUID/profile. The existing
operation-approval service records the independent human decision. Deployment
creation requires that same operation UUID, revision and approval ID. The API
reserves model quota, admits the request through the shared facade/ledger/outbox,
and commits a separate deployment-to-operation registration. It does not mutate
Kubernetes or replace workspace bootstrap pointers. GET deployments reports
scoped durable progress and original provider UIDs without cluster credentials.

Teardown requires a separate request UUID, preview and human approval, and the
original provision operation must be terminal with a closed lease. It preserves
the original allocation and asks for zero additional resource/cost allowance, with
a bounded runtime. A successful stop operation alone does not free model quota:
only the trusted complete owned-absence assessment projects `Deleted`. Original
financial reservation settlement remains with the shared/domain ledger owners.
Replays retain the saved original request even if installed profiles later change.

## Approved plans

The `controller_plan` and ordered `execution_steps` parameters must be part of the
immutable request before approval. The API emits version 2, splitting the public
certificate-authority data into a separate approved parameter and retaining its
SHA-256 digest in the plan. The shared per-parameter limit remains 2,000 characters;
invalid or oversized certificates are refused. `Plan.read` also accepts historical
version 1 admissions without rewriting their payloads. Both versions refuse extra
fields, unknown providers and unbounded resources. The initial
provider path is AWS native EKS nodes using a prepared, reviewed nodeadm AMI and
an existing instance profile with an EKS `EC2_LINUX` access entry. No grant, VPC,
cluster, namespace, CRD or instance profile is created as a side effect.

SkyPilot receives serialized YAML-compatible JSON, a stable allocation-scoped
name/user hash, encrypted disks, an explicit VPC/security group/profile and a
nodeadm configuration containing public EKS discovery data. No activation secret
is put in a task, command or durable plan. The provider's request ID is journalled
before waiting. REST responses are checked without deserializing Python pickles.

The provision sequence is launch, verify the intended EKS node join, create the
workspace workload, verify workload execution. The production API producers support
one GPU serving replica and bounded batch Jobs using explicitly installed profiles.
Batch Jobs have no retry and a finite deadline. Serving uses a Deployment
and private ClusterIP Service. Serving
images must read `SUPERPLANE_AUTH_TOKEN_FILE` and enforce `X-Superplane-Token` on
`/healthz`; readiness requires 401/403 without the token and 200 with it. Both
workloads are pinned by image digest and scheduled only to this allocation's nodes.

For governed API deployments, each successful Kubernetes POST response UID is
committed through the shared fenced inventory before the next object is created.
Later discovery cannot establish an original UID from a name or capacity label.
A lost response or failed UID commit therefore remains unresolved; replacement
objects cannot be adopted during recovery.

Retirement verifies each owned object's observed UID against the original
provision operation's durable allocation membership before the first deletion,
then deletes with UID preconditions and calls SkyPilot down without purge. The
capacity journal prevents the allocation being created again. An uncertain
provider reply is retained for shared recovery, never retried as a new launch.
After each recorded step, the trusted finalizer persists the exact shared per-call
budget dispositions. On completion it enumerates instances, attached/tagged EBS
volumes and network interfaces, EIPs and Kubernetes root UIDs; obtains a fresh
provider listing; seals membership; and publishes fresh attested observations.
The resulting per-resource dispositions and exposure are stored for the owning
domain. EC2 absence alone never releases an allocation. Financial reservation
balances remain with C; this service does no billing arithmetic.

The pinned backend guard adds the allocation tag to instances, volumes and network
interfaces in the same RunInstances call. It refuses independent CreateVolume,
CreateNetworkInterface and AllocateAddress requests. The proxy refuses credential
uploads and server configuration changes. The backend attests its own constrained
web-identity role; the executor compares that role with the current `aws_role`
credential delivered by Gateway and uses that role for cloud reads. The proxy and
Go container receive no AWS identity token.

## Verification and release

Run `superplane-domain-ci.yml`, `harness-jobs-ci.yml` and `gateway-ci.yml` on the
same source head. The controller execution job installs this package in its own
venv, runs the real Go/shared RPC/PostgreSQL tests, and then the trusted executor
tests. Its JUnit guards reject skipped database coverage. Do not run product CLI
or regression tests against a developer's operator environment.

Build the separate image remotely using this Dockerfile, repository-root context,
and a reviewed digest-pinned Python 3.12 `PYTHON_IMAGE`. Image approval, credential
scope activation and deployment are separate live gates. No source merge alone
installs this service or activates spending. Rollback removes assignment publication
and worker access first; retain shared intents, request handles and reservations
for recovery. Never restore direct provider loops or delete uncertain capacity rows.


## Supported installation contract

Use the existing `deploy.sh` environment file and installation identity. Both modes
render the registration manager; workspace credential/observation volumes are
optional so an empty installation can become registered through projected updates.
Full mode requires an `execution` mapping containing:

- `authority_endpoint`: the existing HTTPS Gateway authority origin.
- `role_arn`: the selected existing trusted executor role, scoped to the domain
  database and Gateway operation-delivery boundary.
- `provider_role_arn`: the selected existing SkyPilot backend role. It must equal
  the AWS role in each admitted provider credential; neither field grants IAM access.
- `run_projection_secret`: an existing ADP-owned projection with `run-credential`,
  `workload-token`, and `operations.json` (the actual admitted operation IDs).
- `database_secret`: an existing restricted projection with `domain-dsn` and
  `execution-dsn`. Use TLS-verified connections and the reviewed schema search paths.
- `workspace_credentials_secret`: a separate projection with `<workspace>.kubeconfig`
  credentials scoped for executor workload creation/removal. It is mounted only in
  the trusted executor. The manager's `superplane-workspace-access` projection must
  allow scoped reads and self permission reviews, with no workload mutation or
  Secret reads; runtime refuses incomplete or excessive permission rules.

The source lock keeps `superplane-executor` pending until an authorized remote build
produces a real digest. The manual `superplane-executor-build.yml` lane requires an
existing CodeBuild project using `releases/buildspecs/executor.yml`, source bucket,
account/region and reviewed Python 3.12 base digest. It does not create a project,
change the release lock or deploy. The executor image must match the exact release
revision because it includes both the domain and shared execution packages.

Missing/empty run projections leave the service idle without opening databases or
publishing assignments. A supplied run must already own the real paid lease;
renewal retains the shared maximum runtime and attempt ceiling. A projection is
not permission to create a run, copy another pod's workload token, or impersonate
an execution holder. Production publication must use the existing ADP run owner.
The composed API owns approval and budget admission, and the shared outbox
dispatcher publishes the paid operation's run handoff. Installation consumes
that handoff; it never fabricates execution authority. Missing configured
adapters keep submission unavailable.

## Recovery and live evaluator inputs

On authority loss or an unknown outcome, stop local work, retain the shared intent,
capacity row and any SkyPilot request ID, and use the shared recovery owner's
finite fenced observation path. This tenant executor does not run an unscoped
recovery sweep. A completed plan with interrupted bookkeeping must obtain a fresh
listing/seal/report under valid authority before any allocation release. Persisted
reports are timestamped observations, never replacement execution authority.

The shared dispatch lock now spans provider I/O, finalization and settlement.
Cancellation during I/O still invokes bookkeeping for committed effects while the
lease is live, and retrying interrupted bookkeeping never repeats the provider
call.

### Scoped recovery after lease expiry

`superplane_executor.recovery` is this domain's side of recovery after the
execution lease has lapsed. It is a **caller of shared recovery, not a recovery
engine.** `harness_jobs.recovery` already does the hard part correctly — durable
reconciliation attempts with backoff reserved *before* observer I/O, deferral
instead of terminalization, `lock_lease` re-checked inside every write
transaction, attributed audit, finalization, claim closure. An earlier version of
this module reimplemented all of that and got it wrong in ways that cost money: a
transient provider failure became a permanent `unresolved` row every later pass
skipped, and the two engines could disagree about when budget may be released,
which is the one thing they must not.

What the shared engine deliberately lacks is a tenant scope (an operator sweeping
the harness's own obligations wants every tenant) and a provider credential (it
holds none by design). This module supplies exactly those two things.

- `sweep_scoped_expired_leases(connection, principal=, candidates=, on_settled=)`
  in `harness_jobs.recovery` is the shared engine restricted to one tenant. Scope
  comes from a `ResolvedPrincipal`, constructible only from an authenticated
  context, and is applied in SQL before `LIMIT`. `candidates` can only *reduce*
  the selection — naming a foreign operation does not select it, and an empty set
  selects nothing rather than everything.
- `recovery_scope(registry, operation_ids)` returns the principal Gateway itself
  resolved for a live grant this pod holds, or `None`. Scope is never taken from
  configuration, the handoff document, or a tenant named by a caller, so a pod
  with no live grant recovers nothing.
- `journalled_operations(domain_pool, principal, limit=)` bounds candidates to
  operations this domain recorded in `controller_provider_requests` for that
  tenant. `authorized_operation(...)` refuses an operation outside the scope
  *before* anything is claimed, so a foreign ID cannot advance another tenant's
  fence token merely by being passed in.
- `ScopedRecovery(provider, principal=, observe=, ledger=).run()` runs one
  bounded pass and delivers the exact per-call dispositions to their **own**
  ledger obligation — `settle`, `release` or `retain` are different operations,
  not one call with a label. A release is delivered only when every call
  established absence; anything else retains. The executor computes no amount.
- **Delivery is a durable outbox, not a best-effort call.** Accounting is written
  `pending` inside recovery's own fenced transaction via `on_settled`, so it
  commits under the claim that authorized it; a write that fails aborts the
  settlement rather than leaving a settled operation with no record of its
  disposition. Delivery happens after the sweep and marks the row delivered only
  once the ledger has accepted, so a crash in between is resumed exactly once by
  the next pass. Because accounting and settlement live in different databases and
  cannot share one transaction, delivery re-confirms in the execution database
  that the operation really is settled and closed first.
- `skypilot_observer(sky, domain_pool)` reads the pinned status API against an
  already journalled handle, matching the shared `ProviderObserver` signature.
  Only `SUCCEEDED` settles. A `FAILED` or `CANCELLED` request is **not** reported
  as `CallOutcome.FAILED`, because a SkyPilot launch can create instances and then
  fail partway; treating it as absence would release the reservation for capacity
  still running. Establishing absence requires the trusted finalizer's cloud
  listing, not a request status — so `release_permitted` is always `false` here.
- Recovery performs no mutation: it never relaunches, deletes, purges or retries,
  and never recreates a retired capacity row. An unreadable provider keeps the
  durable handles for a later pass.
- `service.recover(...)` runs one pass per assignment cycle, so this exists in the
  running system rather than only in tests. Failure is non-fatal and silent:
  recovery is cleanup running beside real execution and must not take down the
  assignment loop, and nothing is lost by waiting.

Real PostgreSQL coverage is `tests/test_scoped_recovery_postgres.py`: foreign
scope rejected with the other tenant's lease and fence token asserted untouched,
a deferred call retried by a *later* pass after the 60-second claim expires, the
crash window between accounting and delivery resumed exactly once, an unwritable
accounting row refusing to settle, an unsettled row never delivered, `FAILED`
status not releasing, live lease never swept, and the service-level wiring.
`modules/harness/jobs/tests/test_scoped_recovery_postgres.py` covers the shared
scoped sweep and the settlement hook, including that a failing hook rolls the
settlement back.

**Two unresolved interfaces, owned by #5535.** Both are genuinely open; neither is
worked around here.

1. *An observation-only authority read for a recovery-claim holder.* Recovery runs
   *after* the lease expires, which is exactly when `GatewayAuthority.resolve`'s
   live-grant read is refused by design. That read is therefore unusable here and
   this module does not fabricate a substitute: `observe` is an injected,
   observation-only dependency, and with none supplied every call reconciles to
   `UNKNOWN` and every reservation is retained. What #5535 must publish
   authenticates the *fenced recovery claim* rather than a live execution grant,
   and returns the provider credential for status reads only. The consuming shape
   is the shared `ProviderObserver`:

   ```python
   async def observe(
       idempotency_key: str, provider: str, operation_kind: str, target: str
   ) -> tuple[CallOutcome, str | None, str | None]:  # outcome, detail, provider_ref
   ```

   Keeping the shared signature is deliberate — the shared engine's durable
   attempt, backoff and deadline handling then applies unchanged. Meanwhile
   `recovery_scope` derives scope from a grant that *does* still resolve, so
   scoping does not depend on this interface landing.

2. *The ledger interface for delivering budget dispositions.* `ScopedRecovery`
   consumes `settle`/`release`/`retain`, each taking `operation_id`,
   `dispositions` (the exact `(idempotency_key, BudgetDisposition)` pairs the
   shared engine derived) and `reason`. Until #5535 publishes it, `service.recover`
   passes no ledger and each pass still reconciles the provider and writes this
   domain's accounting under the claim — leaving a durable `pending` outbox row
   that `deliver_pending()` completes once the interface exists. Nothing is lost
   in the meantime and nothing is delivered on a guess.

Full activation additionally requires fresh inventory/accounting through the
trusted finalizer for any release. These are required code integrations, not
live-test-only prerequisites.

For the live gate, provide the reviewed release lock, existing ADP account/cluster,
organization/workspace registration, exact workspace kubeconfig/observation
projections, actual current run projections, approved immutable controller plan,
finite paid limits, prepared nodeadm AMI and existing node instance profile/access
entry. Through `deploy.sh`, first verify authenticated zero-target administration,
401/403 without credentials and state after restart. Register the workspace using
the same installation identity, then verify one admitted launch, native EKS node
provider IDs, batch completion or authenticated private serving, cancellation,
credential revocation, and retirement with no instances, disks, interfaces, EIPs
or dependent pods remaining. Check shared per-resource dispositions against provider
truth; deliberately retain a leaked volume case. These live criteria are not
satisfied by the offline simulated cloud transport tests.

For focused source development, dispatch `superplane-domain-ci.yml` with
`controller_only=true`; that run intentionally skips the domain job and cannot
satisfy final acceptance. Final acceptance requires the default full domain run,
Harness Jobs CI and Gateway CI at the same head, followed by review and normal merge.

### Protected paid task composition

`superplane-paid-worker` is the deterministic paid-operation image entry point.
The deployment source is `deploy/paid-worker.yaml`; release tooling must resolve
its reviewed image and queue placeholders. It does not install schemas or enable
provisioning. Gateway is configured with `ADP_DOMAIN_OPERATION_BINDINGS`, a JSON
list of exact deployment-owned domain bindings:

```
domain, org_id, adp_org_id, producer_registry_id, worker_registry_id,
database_secret_id, database_schema, queue_url, worker_namespace,
worker_service_account, worker_container, worker_image_digests, repo,
observation_url, observation_credential_secret_id
```

`org_id` is the original Superplane organization UUID. `adp_org_id` is the separate
Gateway tenant. The dedicated database secret contains `{"dsn":"postgresql://..."}`;
Gateway verifies TLS using `RDS_CA_BUNDLE` and checks the installed shared schema.
The producer role holds `domain:operation-producer`. The worker registry holds
`domain:operation-executor`, `domain:operation-recovery`, and the existing executor
vault-delivery capability where provider delivery is required. These are trusted
registry capabilities; request headers do not create them.

The Gateway `/internal/v1/controller-execution/dispatch` endpoint reads the actual
paid admission before creating an immutable original-ID mapping, protected pending
run and SQS envelope. Ordinary retries reuse the invocation. Expired bootstrap
attempts that never held a lease can select at most four persisted successors;
a held lease requires shared recovery. Execution after recovery uses the next
actual fence generation and keeps the original operation/job/admission-attempt.

The pod acquires its task using IAM plus a projected TokenReview token, binds the
exact protected envelope, writes its short-lived run credential to a private mount,
and acquires the actual shared lease. The standalone Go sidecar receives only a
scoped socket token and admitted step IDs. Each effect checks the live Gateway run,
current shared lease, handoff and canonical manager registration. The task neither
claims nor changes the registration manager's observation lease. Terminal task
acknowledgement requires the shared operation's durable terminal state.

The worker ConfigMap supplies `ADP_EXECUTION_AUTHORITY_ENDPOINT`, `AWS_REGION`,
`SUPERPLANE_OPERATION_SCHEMA`, `SUPERPLANE_MANAGEMENT_API_SERVER` and `SKYPILOT_URL`.
The manifest identifies private DSN/CA, workload, provider and policy mounts.
Lifecycle operations bind `runtime_config_sha256` in the paid request and run the staged
`workspace_provisioning.runtime.run_lifecycle` composition. Its Terraform artifacts
and phase journal retain the original authority; returning an approval proposal
never registers a ready workspace.

Recovery pods use the protected recovery scope and claim routes, with observation
credentials retained by Gateway/domain API. Domain recovery submission is scoped
as `controller_recovery/<domain-org-uuid>`. The API requires
`SUPERPLANE_RECOVERY_OBSERVATION_ROLE_ARN`,
`SUPERPLANE_RECOVERY_WORKSPACE_CREDENTIALS_DIR`, the management origin and the
SkyPilot URL/token file. AWS observations use an explicit Describe-only STS session
policy; Kubernetes observations refuse mutations, secrets, exec and proxy paths.
Immutable settlement receipts are delivered to the domain ledger using their
original reservation identity.

Workload UI/API reads expose [status and bounded logs](WORKLOAD-OBSERVATIONS.md) and
[paid reservation accounting](WORKLOAD-ACCOUNTING.md). [Cancellation](CANCELLATION-API.md)
retains uncertain resources and accounting. These surfaces do not establish
provider-billed cost or live workload acceptance. Bounded batch text retention is documented in [BATCH-RESULTS.md](BATCH-RESULTS.md).

## GPU requirements instead of a fixed machine

An installed workload profile may replace `instance_type` with `accelerators`,
`max_gpus_per_node`, `cpus` and `memory_gb`, for example:

```json
{"accelerators": ["A10G:1", "L4:1"], "max_gpus_per_node": 1, "cpus": 4, "memory_gb": 32}
```

This produces a version 3 approved plan. SkyPilot receives the GPU alternatives
without an instance type and chooses a machine within the profile's AWS
account, region, image and network constraints. `physical_gpus` must equal
`node_count * max_gpus_per_node`. Each alternative must cover the workload's GPU
request and fit that physical upper bound. CPU/memory minimums must exceed
the workload requests; the profile owner sizes this headroom for node services. For example, AWS H100 capacity may require
approving eight physical GPUs even when the workload uses one.

The installed SkyPilot backend must attest `physical_gpu_limit` support. Its
RunInstances hook reads the selected type's GPU count from EC2 before creating
the instance and refuses missing metadata or excess GPUs. The bound is a
capacity limit, not a provider bill cap. Original fixed-instance profiles and
their teardown requests keep their existing format. Version 3 teardown retains
the original allocation and requests no additional GPU/cost reservation.

This extends the existing AWS launch/join/workload lifecycle. It does not enable
cross-provider credentials or WireGuard hybrid joins; those remain required for
the mixed-provider demo. No deployment or live acceptance follows from code CI.

## Approved regional capacity (#5925)

Version 4 GPU profiles replace the five flat location fields (`region`,
`image_id`, `vpc_name`, `security_group`, `instance_profile`) with `regions`.
Each of its 1–4 entries contains those fields plus exact `vpc_id`,
`security_group_id` and `subnet_ids` (1–4 distinct subnet IDs). All entries use
one approved provider account/credential. The existing workload and aggregate
GPU/runtime/cost bounds remain required. The approved subnet set must cover the
subnets eligible for the prepared SkyPilot VPC; a different selected subnet is
refused, never silently adopted. Remote private connectivity and post-launch
bootstrap completion are delivered by #5926/#5927.

The producer stores compact regional metadata in `controller_regions` and binds
its SHA-256 in the version-4 controller plan. Both parameters remain subject to
the shared 2,000-character limit. SkyPilot receives a single region × GPU choice
set; each candidate carries its own network/profile configuration and binding
labels. The backend verifies the actual account, region/image, subnet/VPC,
security-group ID, node profile, encrypted disk bound and instance count before
RunInstances, in addition to the existing physical GPU guard. V4 requires
`regional_binding_guard: 1` in backend attestation. Old backends cannot execute
new regional admissions. V1–V3 retain their original format.

Regional inventory uses EC2 ARNs with account, region, kind and resource ID,
including instances, volumes, network interfaces and addresses. Discovery scans
all approved regions to find partial/fallback allocations; subsequent observations
query each original resource in its recorded region. Denied/incomplete discovery
retains the obligation. A lost launch reply may leave the optional request-level
`region` null, but discovered resource ARNs persist through the original fenced
inventory and do not rely on that successful-reply projection. Bare IDs in a v4
allocation are unresolved rather than guessed or adopted. Legacy v1–V3 IDs remain
supported.

Validation uses the pinned parser, installed pre-create guard, real registered
worker/PostgreSQL lifecycle, recovery-provider boundary and regional inventory
observation tests. AWS/SkyPilot/Kubernetes responses in these tests are simulated;
passing them does not establish live multi-region GPU execution or billed cost.
