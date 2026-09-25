# AWS capacity milestone: maintained architecture and implementation boundaries

The [authoritative Superplane design](../DESIGN.md) governs architecture and ownership.
This document provides supporting implementation detail or historical evidence;
its availability statements do not imply that pending design requirements are implemented.

Source review for #5925–#5930, 24 September 2026. Baseline: ADP
`0d47dae09`; SkyPilot parser/backend source: `v0.12.0`. This is source evidence,
not an installation or live capability assessment.

## Request and execution path

```text
GitHub issue/tag
  → existing ADP ingress, principal and agent run
  → installed Superplane skill and workspace API
  → server-owned profile + canonical workspace binding
  → immutable preview, approval and bounded admission
  → shared Harness Jobs operation + transactional dispatch
  → protected paid worker and trusted executor RPC
  → SkyPilot allocation
  → private workspace EKS connectivity and verified node membership
  → Kubernetes Job
  → retained result, originating issue and resource/cost observations
```

The management EKS hosts ADP and the Superplane control plane. GPU machines
belong to the cluster selected for the workspace. The accepted 25 September
requirement adds explicit shared placement: multiple workspaces may share a cluster
only within the same ADP organization, using separate namespaces and scoped access.
An eligible cluster may be a separate data-plane EKS or the management EKS when
platform policy explicitly permits that placement. Dedicated placement remains the
default. This is a required extension, not a claim of current runtime support; see
[organization-scoped shared clusters](ORG-SHARED-CLUSTERS.md) for the creation,
ownership, monitoring, migration and deletion contract.

### Required regional topologies

Both topologies are required under one ADP/Superplane management control plane:

| Topology | Cluster and workspace binding | Delivery and evidence |
| --- | --- | --- |
| Multiple regional data planes | Distinct workspace EKS clusters in two or more approved AWS regions, each with its own Kubernetes control plane and workspace membership | #6054 covers concurrent operation, isolation, recovery and independent cleanup. |
| Cross-region workers on one data plane | One selected EKS cluster with GPU allocations in two or more approved regions; its EKS control plane remains in its home region | #5925–#5930 retain their exact single-cluster/two-region acceptance criteria. |

These must compose: a fleet of regional EKS clusters may contain clusters with
cross-region GPU workers. The compute region never chooses or overrides the
workspace's cluster identity. For example, a GPU in us-west-2 allocated to the
us-east-1 EKS must join that EKS even when another managed EKS exists in us-west-2.
There must be no process-global current data-plane target. Workspaces retain one
active cluster membership; workload federation, automatic regional failover and
transparent migration are outside this contract.

### Workspace-owned GPU placement invariant

The binding is `GPU allocation → workspace → one data-plane cluster`. Every GPU
allocation belongs to one workspace; every provisioned workspace has exactly one
active data-plane cluster membership. Multiple same-organization workspaces may
share a cluster, but that does not merge their GPU ownership or reservations.

Resolve and approve the workspace's cluster identity and membership generation
before asking SkyPilot for capacity. Eligible compute regions must satisfy that
cluster's approved network, image, identity and budget constraints. SkyPilot may
choose among those eligible regions and machines; proximity or affinity to another
in-region cluster never changes the destination. A GPU in us-west-2 allocated to
workspace W bound to cluster A in us-east-1 joins A, not cluster B in us-west-2.

Bind workspace, cluster, membership generation and allocation through admission,
credential delivery, launch, networking, node join, scheduling, observation and
cleanup. Revalidate the binding before mutation and on recovery. If capacity or
connectivity to the bound cluster is unavailable, report failure or retain uncertain
ownership; do not reassign the GPU or workspace to a nearer cluster. Workload
scheduling must enforce the owning workspace's GPU allocation. Cluster migration
or reassignment requires a separate explicit operation with drain and ownership
checks; neither regional fallback nor retries may perform it implicitly.

Acceptance must include a same-organization cluster B in the GPU's region and
prove that W's GPU still joins A. Also reject a stale membership generation, a
wrong-cluster join and use of W's GPU by another workspace sharing A. #6048 and
#6054 must test these boundaries; #5926/#5927 must consume the binding, and #5928
must preserve it during recovery and cleanup.

#6048's explicit same-organization sharing applies to either topology. Each
cluster keeps independent identity, credentials, namespace memberships, inventory,
cost attribution and lifecycle. Losing or retiring one must not mutate another.
#6051 extends the provider-local data-plane model to Azure, GCP and a supported
neocloud while management stays on AWS; those provider paths are not implemented
by the AWS regional stories.

Transit Gateway is the AWS cross-region worker network path; native VPC connectivity
is used where appropriate. A regional cluster fleet does not itself require an
inter-cluster mesh. WireGuard and Nebius node-join parity are separate follow-up work.

ADP owns identity, vault credentials, the existing agent runtime and ingress.
Shared Harness Jobs owns admission, operation/attempt identity, effects,
fencing, cancellation and recovery. Superplane owns profiles, domain projections,
provider adapters and evidence. SkyPilot owns machine selection, launch and down;
Kubernetes owns workload execution. A second allocator, queue or provider loop
would bypass these boundaries.

The maintained API/controller/monitor sources are now inside this repository.
The module skeleton README and early integration design predate that transfer.
The historical upstream Go provisioning loop and Python migration protocol tests
are not substitutes for the protected paid-worker integration.

## Current runtime and concrete gaps

| Boundary | Maintained implementation | AWS milestone extension |
| --- | --- | --- |
| Approval plan | `superplane_executor/deployment_plan.py`, `plan.py`; v1/v2 fixed machines, v3 GPU alternatives | Bind a bounded regional configuration set without changing old admissions or weakening the 2,000-character parameter limits. |
| SkyPilot selection | `Plan.task()` sends GPU `any_of`, CPU/memory minimums; `provider.py` submits one launch | Let SkyPilot evaluate region/GPU alternatives, each retaining its regional image and network constraints. No executor-side ranking or fallback. |
| Backend authorization | `installation/skypilot_runtime.py` guards RunInstances, physical GPUs and allocation tags; bootstrap attests the backend role | Enforce approved regional account/image/network/resource bindings before each create, including fallback attempts. |
| Provider prerequisites | `Provider.cloud()` currently requires compute region/account/VPC to match EKS and an `EC2_LINUX` access entry | Separate EKS identity from compute location; preserve same-region native behavior. Remote bootstrap compatibility must be established by #5927. |
| Ownership | Shared effect intent precedes I/O; domain capacity row prevents repeat launch; SkyPilot request handle is journalled before waiting | Discover interrupted launches across every approved region and persist region-qualified instance/volume identities under the original operation. |
| Network | Workspace Terraform configures EKS remote node/pod ranges | Add owned/reused Transit Gateway attachments, peering, DNS, node/pod routes, security rules and reference-safe teardown. RemoteNetworkConfig alone does not connect regions. |
| Join | Current native task invokes nodeadm with public EKS discovery data | Add the production post-launch transport, expected node identity, scoped certificate handling and Ready/GPU/CNI evidence. Migration Protocols alone perform no transport. |
| Inventory/recovery | `inventory.py` and protected recovery API enumerate AWS attachments and original Kubernetes UIDs | Query actual regional identities; denied or partial discovery retains exposure. Network/bootstrap effects must share durable ownership and recovery semantics. |
| Workload/results | Allocation selectors, bounded batch Jobs, UID-bound observations and retained results already exist | Connect the existing issue ingress and skill to this lifecycle, including duplicate delivery and result-delivery failures. |
| Acceptance | Existing acceptance/release tooling and CUDA probe source | Executable two-region issue driver with required evidence; code CI and live acceptance remain distinct. |

## Pinned SkyPilot compatibility findings

SkyPilot 0.12.0 accepts resource `any_of`, regional `image_id`, and resource-local
`_cluster_config_overrides`. Its AWS `vpc_name` configuration is a string;
`vpc_names` is a string/list, not a region map. Its effective region configuration
helper documents region-specific lookup for Kubernetes, not AWS. Therefore an
invented `aws.region_configs` map is not a valid implementation of #5925.

The real parser check lives in `tests/_skypilot_task_schema_check.py`, run by
the remote domain workflow in its own pinned SkyPilot environment. Extend that
check alongside the registered-worker/PostgreSQL integration tests. Fixtures
must identify simulated SkyPilot/AWS responses and cannot establish live capacity.

## Delivery order and completion evidence

1. **#5925:** regional admission, SkyPilot alternatives, pre-create enforcement,
   durable regional discovery and down/inventory. Exercise ambiguous replies,
   capacity exhaustion, fallback and unapproved selections.
   Before #5926/#5927 are completed for shared placement, implement and validate
   #6048, the [organization-scoped workspace cluster-sharing prerequisite](ORG-SHARED-CLUSTERS.md).
2. **#5926:** private Transit Gateway connectivity, both route directions,
   DNS and shared-resource ownership. Test API, kubelet and ordinary pod/Service
   traffic separately.
3. **#5927:** post-launch join and verified GPU readiness. Validate the deployed
   EKS/nodeadm identity mode; historical EC2-as-Hybrid behavior is not an AWS
   support claim. Never blanket-approve certificates.
4. **#5928:** interruptions across the complete lifecycle, cancellation,
   dependency-ordered teardown and preserved shared resources. Record compute,
   storage, network and transfer cost evidence separately.
5. **#5929:** actual issue/tag ingress through the existing worker to a Kubernetes
   CUDA Job and durable issue result. Replays retain allocation, Job and operation
   identities.
6. **#5930:** runnable acceptance driver and configured release inputs. Live proof
   requires two distinct AWS regions, at least one remote from EKS, both nodes
   Ready simultaneously, and one CUDA Job per node returning checksum 33,554,432.
   Replay/recovery, removal of one member without breaking the other, and full
   provider-verified cleanup are mandatory evidence.

#6054 adds separate acceptance for multiple regional data-plane clusters and the
composed topology above. Neither a two-cluster demonstration nor the existing
single-cluster demonstration substitutes for the other. Shared-placement acceptance
also requires #6048; do not mark the combined product requirement complete from
#5930's original single-cluster scenario alone.

Each successor story explicitly requires its predecessor's passing code checks
and reviewed merge. The code-only validation path is remote Superplane Domain CI,
plus affected Harness/Gateway/infrastructure checks. The live demonstration
requires separately selected current account/workspace, regional network/images,
release and workload digests, access, finite budget/deadline and cleanup owner.
Historical deployment-state files, upstream targets and budgets supply none of
that authorization.

## Approved implementation contract

Status: approved for source implementation under the user's delegated design,
ADP-agent dispatch, PR review and merge authorization in this conversation.
This records the supervisor's design under that authorization; it is not a claim
of a separate human review of this document or of verified live AWS behavior.

### Scope and ownership

Every changed or added repository file must be beneath
`modules/domain-apps/superplane/`. This includes code, tests, infrastructure,
designs, agent instructions and acceptance tooling. Reuse existing Gateway,
Harness, Agent Factory and CI interfaces without modifying their files. Do not
relocate shared logic or duplicate an execution/approval engine in the app to
work around this constraint. If an existing shared interface is insufficient,
report the exact missing interface and continue independent in-scope work; the
supervisor must resolve that conflict before the dependent implementation ships.
Do not create bookkeeping, transcripts or plan files outside this prefix in a PR.

Use one story per implementation PR, based on current main after the predecessor
merges. No parallel story dispatch. The existing accepted AI-DLC execution graph
is not modified by these direct issue assignments. Agents implement and open a
ready PR after pre-submit checks; the supervising assistant reviews the actual
final diff, acceptance evidence and current checks and owns the merge decision.
Do not enable auto-merge or dispatch the next story yourself. Publish checkpoints
on the branch during long work; checkpoints do not satisfy story completion.

### Regional allocation contract — #5925

Separate immutable workspace cluster identity (ARN, endpoint, CA, namespace and
organization-scoped membership and explicit placement eligibility) from eligible
compute locations. The first milestone
uses one account and the existing exact credential binding. Each regional entry
must bind region, compatible regional image, VPC/subnets, node identity/profile,
security rules and network prerequisites; shared GPU/CPU/RAM, physical GPU,
storage, runtime and cost limits apply to every alternative and any fallback.

Introduce a versioned plan/profile representation, retaining historical fixed
instance and single-region GPU requests and their original teardown semantics.
All regional choices must be covered by the approved request digest. If additional
parameters are needed for bounded regional metadata, bind them by digest and obey
the existing parameter count/length limits; do not expand shared Harness limits.
Validate closed schemas, duplicate regions, account/role consistency and regional
image bindings before admission. Keep regional selections within the installed
policy; the worker supplies admitted step IDs, not arbitrary targets or YAML.

Produce one SkyPilot task carrying eligible GPU/region alternatives and their
resource-specific configuration. Verify the actual pinned parser and configuration
consumption, not just a hand-built dict. Do not rank candidates, pick an instance,
or implement fallback in the executor. Before RunInstances, enforce the actual
selected account/region/image/subnet/security-group/instance-profile and physical
resource limits; post-launch validation alone is insufficient. Any extension to
backend attestation must be checked by the trusted executor.

Record original operation, allocation, stable cluster name and request handle
before waiting on asynchronous work. Persist provider resource identities with
account and region so a read in the wrong region cannot establish absence. After
an ambiguous launch, reconcile all approved regions under the original operation;
do not allocate again. A failed/denied/incomplete listing is unresolved. Retain
partial fallback allocations and their storage/network attachments for cleanup.
SkyPilot owns down; trusted provider inventory establishes actual absence.

Remote allocation code does not itself establish working connectivity or GPU
membership. Keep readiness and workload admission fail-closed until the network
and join criteria are observed. #5926 and #5927 extend those runtime stages.

### Private network contract — #5926

Derive a bidirectional network plan from the admitted allocation and the workspace
EKS registration. Bind exact VPC, node and pod ranges, DNS resolution paths,
Transit Gateway attachments/peering, route-table destinations and scoped security
rules. Reject overlaps, default/overbroad routes and changes that steal local
provider routing. Preserve the native same-region path.

Represent owned versus pre-existing/shared resources explicitly. Reuse only
verified approved infrastructure. Persist intent and returned identity through
existing operation/effect/inventory interfaces before dependent mutations.
Peering acceptance and readiness may be delayed: observe and resume the same
resource rather than creating a duplicate. Serialize shared membership/removal
through durable existing facilities; a process-local reference count is inadequate.
Reserve bounded network exposure alongside compute. Remove exclusively owned
resources in dependency order, preserving routes/attachments used by other members.

Provide separate executable observations for node-to-private-EKS API,
EKS-to-kubelet, and ordinary pod-to-pod/Service traffic and DNS. A created route,
logs call, or host-network-only pod does not satisfy all connectivity criteria.

### Node join contract — #5927

Use a trusted post-launch transport bound to the observed instance/account/region
and immutable workspace target. Confirm the deployed EKS/nodeadm identity mode,
image/runtime/CNI/device-plugin compatibility and supported limitations from actual
interfaces. Never silently convert EC2 to an asserted supported Hybrid Nodes mode.
Retain the working same-region path and historical migration compatibility.

Keep sensitive bootstrap material out of SkyPilot task YAML/history, ordinary logs,
results and GitHub comments. Use existing credential delivery and bounded renewal;
restrict certificate approval to the expected node identity. A retry operates on
the same allocation. Admission requires matching provider/node identities, Node
Ready, sufficient allocatable GPUs, approved image pull, cluster DNS and ordinary
pod networking. Deadline, stale credentials or missing GPUs retain a cleanable
allocation and cannot report successful readiness.

### Recovery and accounting contract — #5928

Use the existing shared operation/effects ledger and fenced execution, with app-owned
projections/adapters only. Network and bootstrap effects require real durable
behavior through supported shared interfaces. Reject unsupported effects rather
than disguising a mutation as a status read. Exercise lost replies/crashes after
launch, networking, join and Kubernetes submission, including restart and concurrent
cleanup. Resume original identities without duplicate machines, nodes or Jobs.

Cancellation retires demand, drains/removes owned workloads and nodes, invokes
SkyPilot down, revokes owned bootstrap material and releases exclusively owned
network resources. Query original Kubernetes UIDs and exact provider identities.
Incomplete observations retain obligations. Compute, storage, network and transfer
cost evidence remain distinct; unavailable provider billing remains unknown, even
when all resources are absent.

### Issue workflow and acceptance contracts — #5929 and #5930

Integrate through the existing GitHub ingress/run identity, scoped APIs and result
facilities. Implement app-owned adapters/skill changes only. Missing workspace,
regional configuration, credentials or paid bounds must produce an actionable
request, never defaults inferred from historical state. Duplicate deliveries and
agent restart reuse operations and workload identities. A CUDA Job runs through
Kubernetes on the verified allocation, never as the SkyPilot task's workload.

Return region/instance, EKS node, Job, operation IDs, retained CUDA result and
cleanup/cost state to the originating issue. Result-delivery failure preserves the
durable result for retry. Include unauthorized issue/workspace negative tests and
an actual ingress-to-registered-worker integration with declared provider fixtures.

The #5930 driver uses existing acceptance and installation/release interfaces.
Require explicit current target, allowed regions, images/digests, network bindings,
finite aggregate compute/network/runtime/deadline/spend bounds and cleanup owner.
Exclude the first selected region from the second allocation without bypassing
SkyPilot. Require simultaneous Ready nodes, each CUDA Job's all-elements-512 check
and checksum 33,554,432, replay/recovery without duplication, surviving-member
usability after first removal, and provider-verified final cleanup. Evidence is
criterion-specific; missing evidence is BLOCKED/FAIL rather than a skipped pass.

### Verification and closure

Run existing remote code-only CI at the final PR revision. Extend app-owned tests
that existing workflows already collect, including real SkyPilot parser,
registered-worker/RPC and PostgreSQL coverage where relevant. Do not claim fixture
responses, mocks or compiled manifests establish live AWS behavior. Record commands,
run URLs, exact tested commit and per-criterion results in the PR.

The supervisor reviews ownership, authority, bounded spending, interruption behavior,
compatibility and acceptance coverage, requests repairs for material findings,
checks that every changed path is in the app, and merges only the reviewed revision
with passing applicable checks and resolved findings. No administrative bypass or
blind merge based only on an agent's completion message.

This authorization covers source work, agent invocation and its ordinary code-only
CI, review and normal merges. It does not authorize deployment, infrastructure
mutation, migration execution, release/image promotion, live GPU workloads or an
issue trigger that launches the demonstration. Code for the demonstration can be
completed; live acceptance stays explicitly pending until concrete live inputs and
operations authorization are provided.
