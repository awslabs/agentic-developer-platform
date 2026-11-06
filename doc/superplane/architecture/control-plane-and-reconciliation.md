# Superplane control plane and reconciliation

Date: 2026-10-15

Status: Proposed contracts and implementation requirements. The
[architecture overview](README.md) describes the complete workspace and training
flow. This document defines how its four layers share desired state, execution
authority and observations. Names below describe proposed interfaces, not APIs
or controllers already available in ADP.

The companion [database design](database-design.md) defines persistence and
transaction boundaries. The [API and ADP CLI design](api-design.md) defines
proposed routes, schemas and commands for this flow, preserving existing
approval, budget and execution owners.

Use provider-neutral descriptions and fictional examples. Deployment identities,
credentials, endpoints and private operational records belong in private stores.

## 1. Four layers behind one API

| Layer | Desired configuration | Execution and observation owner |
| --- | --- | --- |
| Account Factory | New or existing provider environment, baseline version, ownership and retention policy. | Account adapter/controller; native account, subscription, project or tenant APIs and governance services. |
| Networking Factory | Private networks, service access, DNS, routes and regional/cross-cloud connections. | Networking adapter/controller; provider APIs or the governed Terraform execution path. |
| Cluster Factory | Cluster type, location, initial capacity, access, foundational components and lifecycle policy. | Cluster adapter/controller; provider APIs, Terraform or an explicitly owned Kubernetes installation. |
| Workload Management | Platform application versions, approved runtimes, queues and workload policies; separately admitted training runs. | Argo for maintained applications; Superplane execution services, capacity adapters, Trainer, JobSet and Kueue for runs. |

Bootstrap spans the last two layers. Existing accounts, networks and clusters
can be onboarded with recorded ownership; their creation steps are skipped while
validation and observation still apply. A workspace need not own its whole
account or cluster.

Central standard Argo CD is the default target for the multi-cloud design. It
applies factory resources in the management plane and maintained application
resources on workspace clusters. Factory controllers perform cloud operations.
The optional AWS-managed backend serves selected EKS targets under the same
Superplane contract. Each Application and external resource has one active owner.

Superplane is one logical API, implemented with independently scalable services.
UI, CLI and agents use the same authorization, preview, submission and observation
paths. Factory resources are internal execution interfaces; possession of a
Kubernetes object or an Argo Application does not independently authorize spend
or organization changes.

Resolve provider, cluster, dataset, artifact and credential references through
the caller's workspace grants. Restrict application sources, destinations and
runtime permissions to approved profiles. Issue text or a training specification
cannot select an arbitrary privileged service identity or bypass those checks.

## 2. State ownership and approved publication

Several kinds of state must remain distinct:

| State | Authoritative record |
| --- | --- |
| User intent, approved plan and reconciliation policy | Superplane's durable records, bound to authenticated identity, workspace, provider scope and revision. |
| Published desired configuration | Immutable artifact or approved source revision referenced by the Application; linked to the recorded plan and desired revision. |
| Applied factory and application declarations | Kubernetes objects reconciled by Argo, with their resource identity and generation. |
| Actual infrastructure | Provider observations; Terraform remote state retains its owned resource bindings and state history. A state file alone is not a fresh provider observation. |
| Mutation execution | The existing durable execution owner holds operation, attempt, lease/fence and dispatch records; factory adapters reference these contracts. |
| Training progress and results | Workspace job/queue/pod observations, training metrics and verified durable artifacts, correlated in the ADP training-run record. |

The approved publication path is:

1. Authorize and preview the request. Persist the approved plan, desired revision
   and durable publication intent before external dispatch.
2. Publish an immutable desired-state bundle or approved source revision. Bind
   its artifact identity to the plan. Sources can be OCI packages, initially in
   ECR; a repository per workspace is unnecessary.
3. Update the assigned Argo Application's pinned source reference through a
   single publisher. Recover lost responses by checking the intended revision.
4. Argo applies the factory/application declarations. Controllers verify the
   approved scope, revision and current execution or service authority before
   mutating resources.
5. Persist observations and expose progress through Superplane. API acceptance,
   Argo sync and provider readiness are separate events.

The API owns the Application source reference; Argo owns the declarations it
renders; controllers own status and explicitly assigned runtime fields. Define
that field ownership before enabling reconciliation. Alternative writers must
use an approved adoption/change process rather than silently altering desired
configuration. A desired revision is never inferred from a caller-supplied
approval label or digest without checking the trusted authority record.

Publication retries are idempotent. Changing a desired revision requires its own
authorization and comparison with the current generation. Configuration rollback
is a newly evaluated change; it does not automatically reverse cloud mutations
or restore a model checkpoint. Credentials remain external references, never
plaintext values in Git, OCI bundles, issue text or status output.

## 3. Factory controller contract

Each provider adapter exposes the same lifecycle concepts while preserving the
provider's actual capabilities. Resource kind names and schemas must be versioned
and reconciled with existing ADP contracts before implementation.

The desired resource records the workspace and provider binding, create/onboard
mode, approved plan revision, baseline/profile versions, dependency bindings,
ownership and retirement policy. Status records observed generation, external
resource/operation bindings, durable execution references, timestamps and
conditions with actionable reasons. Proposed conditions include Ready,
Reconciling, Drifted, ApprovalRequired, Deleting and Unknown; a published schema
must define their exact semantics and allowed transitions.

Required controller behavior:

- Observe before mutation, using recorded provider identities. Provider lookup
  failure reports Unknown; it is not evidence that an account or cluster is absent.
- Retain dispatch intent and provider request handles across worker restarts.
  Reconcile an ambiguous create response before considering another create call.
- Use the existing admission, budget, credential and execution authority contracts
  when dispatching Terraform or other workers. Preserve execution-owned attempt
  IDs and fencing; do not create an independent retry authority in the factory.
- Serialize operations for each owned resource. Terraform adapters also use
  remote backend locking; expired workers must not race a replacement mutation.
- Gate dependencies on current-generation, sufficiently fresh observations.
  Revalidate completed stages after relevant input or dependency changes.
- Observe terminal provider outcomes and partial cleanup. Retain unresolved
  resource exposure until ownership and deletion/retention are verified.

Argo custom health mappings should interpret those conditions consistently with
Superplane. An applied request or an old Ready condition cannot satisfy a new
generation. Cross-Application dependencies require explicit controller/API gates;
sync waves within one Application are not global workflow ordering.

Long-running provisioning is an asynchronous controller operation. Existing
Terraform workers can remain its executors, preserving reviewed saved plans and
their approval bindings. Argo sync hooks must not become the sole storage of
account-creation progress, attempt identity or recovery state.

## 4. Drift, service authority and retirement

There are two reconciliation loops. Argo compares desired declarations with
Kubernetes objects. Factory controllers compare those declarations with actual
cloud resources. For example, a manually changed firewall rule can leave Argo
Synced while the Networking Factory reports Drifted.

Provider controllers observe through supported watches/events or bounded periodic
polling. A Terraform-backed adapter needs recurring refresh/plan observation and
governed apply; launching Terraform once through Argo does not supply that loop.
Publish observation age and detection intervals. Drift can occur between checks;
the contract is to detect and reconcile it, not to promise it can never occur.

An approved reconciliation policy defines which repairs may run automatically,
which require a new plan/approval, and when observation-only behavior applies.
Replacement, destructive topology changes and policy changes outside the approved
scope follow the existing mutation approval rules. Reusing an initial provisioning
approval must not grant unlimited future change authority.

Controllers need explicit ongoing service permissions for the baseline and
resources they maintain. Those permissions are separate from temporary bootstrap
grants and from each worker's leased credentials. Revocation or unavailable
authority blocks new mutations and remains visible in status. Every correction
records its desired revision, observed difference, authority and outcome.

Adoption or migration between Terraform, Crossplane or another provider controller
requires verified resource bindings and an explicit transfer of ownership. Two
provisioners must never actively manage the same external resource. Kubernetes
field ownership must also accommodate controllers such as Kueue that legitimately
change runtime fields on admitted jobs.

Retirement is a durable, authorized desired lifecycle state. Stop new admission
and creation/repair while continuing cleanup reconciliation. Configure Argo
pruning, resource finalizers and provider deletion/retention policy together.
Removing a manifest cannot be the account-closure authorization. Shared accounts,
subscriptions, projects, networks, clusters and retained data outlive individual
workspace cleanup. A finalizer may complete only after verified deletion or an
authorized, recorded retention handoff.

## 5. Central training contract and provider capabilities

The public contract has common concepts for capability discovery, capacity-aware
planning, option comparison/selection, approval, submission, observation,
logs/results, cancellation and explicit retry.
The [API and ADP CLI design](api-design.md) proposes concrete endpoints and
schemas; implementing them must extend existing ADP contracts rather than
creating another authorization or execution path.

| Specification area | Required information |
| --- | --- |
| Reproducible inputs | Code revision, image digest, model/dataset versions, arguments and approved runtime profile. |
| Compute | Framework/runtime compatibility, CPU and memory, accelerator requirements, worker count and distributed communication requirements. |
| Placement | Workspace or permitted placement constraints, provider/region, locality, quota and allowed data locations; distinguish required constraints from preferences. |
| Data and identity | Dataset/model references, scoped workload access, storage protocol, checkpoint/output locations and retention. |
| Execution policy | Deadline, retry/checkpoint policy, spend envelope, interruption behavior and success criteria. |

The provider capability catalogue exposes validated combinations of factory
operations, baseline controls, network options, cluster types, runtimes and
accelerators. Include version, observation time and validation state. Listing a
provider does not imply that every training or baseline requirement is available.

Planning first queries SkyPilot/provider adapters and existing workspace
inventory without provisioning resources. Combine available capacity observations
with authorization, capability, quota, locality, data-access and runtime checks.
SkyPilot catalogue/price information identifies candidates; provider/account
observations supply availability evidence where supported. Expose unresolved
facts instead of treating an offering as confirmed capacity.

Return 2–3 suitable options when available. Persist the planning request revision
and each option's identity, provider/location, GPU configuration, evidence source
and time, validity window, setup/reuse steps, estimated startup/queue/training
time, cost breakdown and assumptions. Cost includes required infrastructure,
data movement and any explicitly proposed reservation. Rank options by the user's
preferences without relaxing required constraints. If only one or no option is
feasible, return that result with reasons and any proposed constraint changes.
Unavailable estimates remain unknown; include confidence or ranges where timing
and cost depend on assumptions rather than measurements.
Evaluate completion deadlines against setup, data transfer, queueing and training
time together, not just the expected training duration.

The user chooses an option and approves its concrete plan. Bind the selection to
the request revision, provider and region, existing workspace/cluster generation
or proposed creation intent, runtime, spend envelope and permitted fallbacks.
API clients make the same explicit selection and approval through the shared
contract as agents, UI and CLI. Neither generating options nor an agent's
recommendation authorizes account creation, resource provisioning, paid probes
or billable capacity reservations.

On submission, revalidate authority, capacity, quota, pricing and other volatile
requirements. Changed conditions outside the approved bounds produce an updated
plan requiring a new decision. A capacity observation is not a reservation;
new-account quotas can remain conditional until the environment exists. Any
provider-supported reservation must have its own approved acquisition terms,
expiry and cleanup path within the chosen execution plan.

After approval and revalidation, the agent invokes Superplane's durable operation
path to orchestrate Account Factory, Networking Factory, Cluster Factory and
Workload Management through their Argo/controller interfaces. Reuse verified
resources and create only what the selected plan requires. Keep preparation
progress and owned resources bound to the same operation across retries. Check
capacity again at acquisition; report and replan if it is no longer available,
without silently switching provider/location or forgetting partially created
infrastructure.

Superplane allocates a durable training-run identity and records dispatch before
creating a uniquely bound TrainJob. The shared execution service owns underlying
mutation attempt identities. Individual runs are submitted through Superplane;
Argo maintains Trainer, JobSet, Kueue and their configuration. Completed or
cancelled runs must not be recreated by an application sync.

Expose compatible behavior for every provider while retaining meaningful
differences in GPU availability, price, locality and runtime support. Changes to
placement follow the approved constraints. Cross-cloud restart or checkpoint
resume requires explicit orchestration, compatible saved state and authorized
data movement; it is not a live migration guarantee.

Training images and data must be accessible from the chosen execution location
before job admission. Use scoped workload identity and authorized artifact links,
including for metrics, logs and model downloads. Include transfer time, egress,
storage and retained-capacity costs in preview where applicable. MLflow can add
experiment tracking; basic run status and output verification must work without it.

Report estimated, reserved and observed cost separately with timestamps. Provider
billing can arrive late. Budgets and configured deadlines govern admission and
execution, while ongoing allocation observations drive spend monitoring and
cleanup. Stopping a training process is not proof that billable capacity has been
released, and a budget estimate cannot promise an exact billing cutoff.

## 6. Unified status and control-plane recovery

Superplane correlates factory resources, Argo Applications, provider allocations,
Kubernetes jobs, optional MLflow runs and artifact records through durable
workspace/run bindings. Serve one normalized view with source, desired/observed
revision, timestamp, reason and next action. Keep workspace readiness, application
health, queue/capacity state, training outcome, result verification, drift and
observation freshness distinct.

Provide resumable event/log access and bounded retention; large logs and artifacts
belong in their storage systems, with access authorized through ADP. A temporary
observer outage preserves the last fact and marks it stale. It does not invent
success/failure or authorize duplicate compute. Notifications and GitHub updates
use durable, deduplicated delivery independent of an agent's lifetime.

The management API, publication service and observers can restart independently.
Persist publication/job dispatch intent and deduplicate delivery; use watches
with reconnect/relist behavior and provider polling where needed. Controller
replicas and workers require effective leases/fencing around mutations.

During a management outage, already admitted training continues under its recorded
policy. Configure execution deadlines and required local enforcement in the
workspace so they survive loss of central observation. Hold new work when its
authority or budget cannot be verified. Cancellation requested during a partition
stays pending until termination and capacity release can be observed.

Back up and test restoration of the ADP database and execution ledger, desired
artifact references, management Kubernetes factory/Application resources,
Terraform remote state, and references to recoverable secrets. Restore identities
and observation first, then reconcile provider inventory before resuming mutation.
The initial management installer and its recovery path must not depend on an
unavailable workspace factory or on resources deleted by workspace retirement.

Version factory CRDs, controllers, provider adapters and application profiles as
a compatible release set. Test schema migration and upgrades while operations
are pending. Use staged rollout and an evaluated rollback plan; changing a
controller image must preserve resource ownership and durable operation identity.

## 7. Delivery gates

Build incrementally: central Argo and workload applications first, with existing
Terraform workers retained; then add factory declarations, continuous provider
observation and governed correction. Validate each provider and ownership mode
before enabling it for users. Reuse the existing offline account-factory planning
and ownership helpers without treating them as a live provisioning service.

Acceptance must cover lost publication/create responses, duplicate dispatch,
stale Ready conditions, cloud drift while Argo is Synced, a repair requiring new
approval, expired/concurrent workers, credential revocation, controller upgrades,
management restoration, and prune/retirement behavior for retained infrastructure.
Training acceptance also covers unavailable capacity, inaccessible data, stale
metrics/billing, checkpoint resume, cancellation during capacity creation and
verified cleanup. Planning acceptance covers constraint-preserving option ranking,
zero/one-option results, catalogue-only or stale availability, changed capacity or
price after approval, new-account quota uncertainty, duplicate submission and
proof that unselected/unapproved options cause no provisioning. These are required
scenarios, not reported passing tests.

## References

- [Architecture overview and provider references](README.md)
- [Database design](database-design.md)
- [API and ADP CLI design](api-design.md)
- [Existing Superplane integration contracts](../../../modules/domain-apps/superplane/contracts/INTEGRATION-CONTRACT.md)
- [Existing account-factory planning code](../../../modules/domain-apps/superplane/infra/account-factory/account_factory/)
- [Canonical agent deployment guide](../../../docs/adp-platform-deployment/deploy-with-agent.md)
- [Argo automated sync, self-healing and pruning](https://github.com/argoproj/argo-cd/blob/master/docs/user-guide/auto_sync.md)
- [Argo custom resource health](https://github.com/argoproj/argo-cd/blob/master/docs/operator-manual/health.md)
- [Argo OCI source configuration](https://github.com/argoproj/argo-cd/blob/master/docs/user-guide/oci.md)
