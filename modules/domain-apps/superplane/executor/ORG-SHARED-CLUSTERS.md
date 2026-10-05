# Organization-scoped workspace cluster sharing

The [authoritative Superplane design](../DESIGN.md) governs architecture and ownership.
This document provides supporting implementation detail or historical evidence;
its availability statements do not imply that pending design requirements are implemented.

Accepted product requirement, 25 September 2026. Parent: #4910. Implementation story: #6048.
Implementation is pending. This document supersedes the management-cluster
exclusion in the original AWS milestone design; it does not claim that the
current workspace lifecycle supports shared clusters.

## Workspace creation

Workspace creation offers **Dedicated cluster** (the existing default) and
**Shared cluster**. Shared placement selects an eligible existing cluster owned
by the caller's ADP organization. Multiple workspaces in that organization may
use the cluster, each with its own namespace and workspace identity. Cluster
sharing across ADP organizations is not supported, even when they use the same
AWS account. An AWS Organizations identifier is not an ADP tenant identifier.

Placement is distinct from infrastructure ownership. The existing `managed`,
`adopt`, and `new-account-managed` modes describe how infrastructure is acquired;
they must not silently decide whether other workspaces can join it. An existing
cluster must explicitly be made eligible for sharing before it is offered.
Creating a dedicated workspace must preserve existing behavior and admissions.

The API should add `cluster_placement` (`dedicated` or `shared`) and an opaque
`shared_cluster_id`. For shared placement, resolve the selected cluster on the
server under the authenticated organization, including its account, region,
ARN, endpoint, CA, sharing policy and generation. Require namespace isolation.
Reject incompatible account, region, cluster-reference and infrastructure-mode
inputs; do not silently overwrite them. Include the resolved cluster identity
and membership intent in preview, approval and execution validation. Retain the
existing default request representation for old clients and reviewed operations.
The CLI should expose the same choice and use the existing preview/approval API.

Cluster discovery returns only clusters the caller may use in their organization.
A foreign cluster identifier must not disclose another organization's inventory.
Eligibility requires a Ready cluster, sharing enabled, sufficient policy/quota
headroom, no retirement in progress, and a supported controller configuration.
The first implementation shares already registered eligible clusters; registration
and explicit eligibility changes are separate authorized operations.

## Supported placements

1. A workspace on its own dedicated EKS cluster.
2. Multiple workspaces in one organization on a separate shared data-plane EKS cluster.
3. Multiple workspaces in one organization on an explicitly eligible ADP management
   EKS cluster. This requires platform authorization in addition to organization
   authorization. Presence on ADP's management cluster must not grant access to
   ADP namespaces, credentials, controller roles or other platform workloads.

The third placement does not permit workloads from multiple tenant organizations
to share that cluster under this feature. Do not infer eligibility from a matching
cluster name or AWS account. Compare verified provider identity and installed
management-cluster configuration. Unsupported placements must be refused at preview.

## Regional fleet composition

Shared placement must work with both a fleet of independent EKS data planes in
different AWS regions and one selected EKS with GPU workers across regions. #6054
covers the regional fleet and its composition with #5925–#5930. One management
control plane may manage all of these clusters; one workspace has one active
cluster membership. Select the cluster explicitly under organization authorization,
then resolve its compute-region alternatives independently. No global region or
cluster default may retarget another workspace. Sharing remains restricted to
one ADP organization per tenant data-plane cluster.

## Ownership and lifecycle

Separate the cluster's organization and infrastructure ownership from workspace
membership. Store per-workspace membership, namespace, namespace UID, bootstrap
registration, scoped credentials and lifecycle state. Enforce one active cluster
membership per workspace and unique namespace membership within a cluster. The
cluster's lifecycle owner is not simply the first or last member workspace.

Serialize admission, membership changes and cluster retirement using the cluster's
canonical identity and generation. Reserve membership before bootstrap mutation;
failed, interrupted or uncertain creation remains represented until owned cleanup
is verified. Recheck eligibility and organization binding during execution, not
only in the list endpoint or preview. Replays must converge to the original
membership and namespace.

Derive a distinct namespace from immutable workspace identity. The current single
namespace in organization runtime configuration cannot be reused for every member.
Preserve namespace RBAC, NetworkPolicies, quotas, Pod Security and scheduler scope.
Workspaces must not read another workspace's secrets or mutate its workloads,
even though both belong to the same organization.

Do not install a competing cluster-wide controller for each workspace. Record and
reuse a compatible controller under cluster-level authority; retain independent
workspace registrations and scoped execution grants. Every GPU allocation belongs
to one workspace, and that workspace's one active data-plane cluster determines
where its GPU nodes join. Sharing a cluster does not create an unassigned GPU pool
or authorize one workspace to use another workspace's allocation. Enforce this
ownership in workload scheduling and accounting, even within one organization.
SkyPilot chooses eligible compute capacity for the already-bound cluster; affinity
to a nearer or in-region cluster must never retarget the allocation. Bind and
revalidate membership generation throughout execution, recovery and cleanup.

Cluster-wide health and inventory observations require a cluster-scoped trusted
principal bound to the owning organization. Workspace principals submit and read
only their authorized namespace/workload observations. Do not replace the current
single-owner check with "any workspace on this cluster"; that would give a member
authority to overwrite other members' fleet state. Project common cluster health
and workspace-specific usage separately, without charging the same cost twice.

Deleting a workspace removes only its recorded owned resources, credentials and
membership after workload drain and verified cleanup. Preserve other namespaces,
controller registrations, node allocations and shared network dependencies. Even
deleting the last member does not implicitly destroy a shared cluster. Cluster
retirement is a separate authorized action, refused while any active, pending or
unresolved membership or allocation still depends on it. Adopted infrastructure
retains its existing preservation contract. Migration of an existing dedicated
cluster into shared use must explicitly transfer lifecycle ownership and invalidate
stale workspace teardown plans before admitting a second member.

Use existing Harness admission, effects, fencing, recovery and accounting interfaces.
All maintained changes must stay under `modules/domain-apps/superplane/`.

## Current implementation gaps

The audit at ADP main `c6f20b354` found:

- `app/models/workspace.py` contains `shared_cluster_id`, but it is not wired into
  the canonical provisioning path.
- `superplane_bootstrap/canonical.py` binds `clusters.workspace_id` and a single
  `workspace_bootstrap` metadata object; it rejects a second workspace binding.
- `workspace_provisioning/bootstrap_result.py` and retirement validation assume
  that single registration and must move to per-workspace membership evidence.
- `app/services/observations.py` intentionally requires one workspace owner and
  excludes ambiguous/shared ownership. Preserve its authorization guarantees.
- Bootstrap refuses an existing controller not accounted for by its own record.
  Shared placement needs a verified reuse contract, not removal of this guard.
- Existing adopted-cluster preservation is useful but is not membership-aware
  managed-cluster retirement.

## Acceptance evidence required before shared placement is enabled

1. Create two workspaces in one organization on one eligible cluster through the
   actual preview, approval, registered worker and bootstrap path. Prove distinct
   namespaces, registrations and scoped credentials, with one compatible controller.
2. Reject a second organization's list, selection, forged direct request, replay,
   execution and observation attempts, including organizations in one AWS account.
3. Reject stale cluster generation, disabled sharing, non-Ready/retiring clusters,
   conflicting namespace ownership and incompatible creation options.
4. Verify dedicated behavior and existing stored admissions still work; selecting
   namespace isolation alone must not silently opt into shared placement.
5. Prove workspace A cannot mutate B's workloads, read B's secrets, or attribute B's
   usage to itself; cluster-level observations remain trusted and correctly scoped.
6. Delete A while B remains active. Verify B's workload, credentials, controller,
   GPU capacity and networking survive. Repeat with creation/retirement races,
   lost replies and partial cleanup. Refuse cluster destruction with unresolved members.
7. Exercise management-cluster placement with explicit platform eligibility and
   separate platform/tenant permissions, scheduling and namespaces. Cover rejection
   when eligibility is absent.
8. Cover the API and CLI choice. Run affected remote code-only domain, API and worker
   tests. Record simulated-provider evidence separately from live EKS evidence.

This is a prerequisite to completing #5926 and #5927 for shared placement. #5928
must preserve membership-aware cleanup. #5929 must carry the selected workspace
binding through the issue workflow. #5930 must exercise both dedicated placement
and two same-organization workspaces sharing an eligible cluster, including member
removal without disruption. Live execution still requires concrete target, budget,
deadline and cleanup inputs through the existing operations gates.
