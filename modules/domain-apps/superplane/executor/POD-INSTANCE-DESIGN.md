# #5927 successor: bind completed batch Pods to allocated AWS instances

Status: approved successor design; implementation and remote validation tracked
under #5927. This document supplies no live acceptance or image-release evidence.
Base: PR #6213. All tracked changes belong under
`modules/domain-apps/superplane/`. Azure/GCP remain last.

## Problem and bounded outcome

The authoritative [Superplane design](../DESIGN.md) assigns a workspace to one data-plane cluster and
allocates GPU capacity to that workspace. Its identity/isolation rules separate
workspace execution from cluster observation. The completed Pod must therefore
have run on the actual AWS capacity of the approved allocation, not merely carry
its labels and selector.

PR #6213 improves `Workspace.ready_nodes(operation, target, plan, instances)`:
it matches full Kubernetes provider IDs against EC2 instance ID, actual observed
region and availability zone, and verifies workspace/capacity/topology labels,
Ready state and allocatable GPUs. Its boolean result discards Node name and UID.
`workload_observation.completed_batch(...)` separately checks the original Job
and Pod, ordinary networking, exact invocation/resources, selector, Pod IP and
nonempty `spec.nodeName`. It does not join that actual node name to the allocated
Node. An admission component could inject a foreign nodeName while leaving the
approved selector unchanged. Network probe collection currently compares the
nodeName supplied by that same Pod, so it cannot close this gap.

This successor gates governed dedicated batch completion and result publication
on fresh Pod-to-Node-to-EC2 identity. It adds no resources, spend, workload API,
database table or Kubernetes permission. It does not complete shared execution,
CUDA acceptance, node-side EKS packet probes, image release or the live AWS demo.

## Baseline authority and shared behavior

Existing dedicated provider composition supplies:

- The original `VerifiedOperation`, approved `Plan`, canonical workspace target,
  exact original Job reference, operation lease and `authorize()` callback.
- `Provider.instances(operation, plan, include_terminated=False)`, which uses the
  operation-selected AWS session, enumerates every approved region, filters by
  the allocation's derived SkyPilot cluster tag and attaches `SuperplaneRegion`.
  Failed regional enumeration refuses certainty. Its default list includes
  pending/stopping/stopped states, so the new completion proof must explicitly
  require its selected instance to be running.
- A pinned Kubernetes endpoint/CA/token transport. The existing dedicated
  readiness path already uses it to read Nodes.
- `capture(provider, operation, target, plan, known_references, authorize)`,
  original Job/Pod validation, bounded result parsing, exact final rereads and a
  lease-locked immutable result insert.

Before this successor there was **no explicit shared-missing-observer guard** in
the readiness/result methods. The shared branch disables public shared onboarding;
its normal member credentials omit Node access. A Node listing through those
credentials should fail RBAC and readiness, but the method still attempts that
listing. Standalone completion/result validation does not itself require any
Node observer. Do not describe those paths as already possessing a typed shared
observer refusal. This successor adds that refusal before Node/provider
placement reads and must not rely only on incidental Kubernetes RBAC failure.

Shared target detection must use trusted target metadata, including the existing
`membership_credential` marker and any maintained explicit shared placement flag.
Never classify a target as dedicated because a shared observer is absent. The
credential reader already rejects shared credential extensions without current
membership metadata; preserve that refusal. Do not widen shared SA RBAC or add
an ambient kubeconfig/management credential fallback.

## Minimal implementation and data flow

### Preserve `ready_nodes` compatibility

Keep `ready_nodes(operation, target, plan, instances) -> bool` and its existing
callers intact. Extract its provider/Node validation into a private reusable
helper returning either no proof or a bounded map keyed by Node name. A verified
entry contains only Node UID, full provider ID, AWS instance ID, region and AZ.
Require nonempty unique Node names/UIDs, no deletionTimestamp, and no duplicate
provider identity. Refuse incomplete/paginated Node responses rather than
silently treating a returned page as the entire allocation. Preserve the
existing region, zone, workspace, capacity, Ready and GPU checks.

The wrapper converts a successful map to `True`; malformed/missing inventory
keeps its existing false/refusal behavior. Do not change it to return a dict or
accept a set of bare instance-ID suffixes. Node identity must be collected by the
trusted Kubernetes transport, never supplied as a request flag or inferred from
labels. Explicit shared targets refuse without making a Node request.

### Trusted provider binding primitive

Add a service-internal method such as:

`Provider.verify_pod_allocation(operation, target, plan, pod, authorize)`

It returns an immutable comparison tuple, not a new authority or public receipt:

`(cluster_arn, pod_uid, node_name, node_uid, provider_id, account_id, region, az, instance_id)`.

The method must:

1. Refuse shared targets pending the separate observer contract below.
2. Check current operation authority before reads.
3. Obtain fresh AWS instance records through `instances()`; preserve the existing
   complete approved-region enumeration and account/role checks. Reconcile to
   the approved allocation/node count using the maintained validator. Require the
   selected record's state to be running; never accept raw caller instance data.
4. Obtain a fresh verified Node map through the extracted dedicated helper.
5. Require the exact original Pod `spec.nodeName` to identify exactly one entry;
   join by full provider ID including AZ to exactly one current allocation EC2
   instance. Include the observed Node UID and original Pod UID in the result.
6. Check current authority again and return only the comparison tuple.

Use the existing cloud/session fencing, request bounds and failure handling.
Do not introduce a global cache or reuse step-2 readiness as current evidence.
This is bounded additional read-only evidence under the existing paid status
operation, not another provider mutation or separately admitted job.

### Completion gate

`Provider.invoke` already obtains instances in the status loop, and calls
`Workspace.workload_ready(..., known_references=..., authorize=...)` for the
post-deployment status. Add an optional keyword-only service-owned placement
verifier to `workload_ready` and pass it through to `completed_batch`. Require it
for governed batch completion; preserve serving and non-governed legacy behavior.
The callback is constructed by Provider over the already admitted operation,
target and plan; public input cannot supply it.

After `completed_batch` identifies its single original successful Pod, obtain
placement proof, retain existing Pod/Job rereads and check the binding again.
Compare stable identities, not Node resourceVersion or an entire Node document:
routine heartbeat updates must not turn an unchanged Node into a replacement.
An absent verifier or changed tuple refuses successful completion. A missing
Pod remains incomplete; a foreign/replaced/ambiguous identity is a refusal.

### Independent result-publication gate

Do not trust only `workload_ready`'s preceding boolean. `results.capture` must
independently call the same trusted Provider method after selecting and
validating its original Pod, before accepting its termination result or network
probe evidence. Check placement before the optional-output early return for an
existing completed Pod, so lack of output cannot bypass placement verification.

After any network Service/log verification and the existing exact Pod/Job
rereads, obtain placement again and require the stable tuple to match. Then keep
the final authorize call and existing lease-locked result insert. No new request
arguments, result schema fields or database migration are needed. Existing text
and digest replay semantics stay unchanged. The comparison tuple is internal
evidence observed during this attempt; this PR does not claim a durable signed
node-placement receipt or perfect atomicity across Kubernetes, EC2 and PostgreSQL.

## Freshness, failure and recovery

Recheck authority around all slow provider/API calls and immediately before
publication. The before/after placement reads detect Node recreation, providerID
change, foreign placement, disappearing or stopped instances, and changed Pod
binding during result/log collection. Revocation or expired lease always prevents
publication even if earlier reads succeeded.

Unavailable regional/provider/Node evidence must not become an empty-success
inventory or an approval to release capacity. A status failure retains the
existing unknown/incomplete work and budget/cleanup governance. Recovery must use
the original operation, plan, allocation and Job/Pod UIDs, and repeat live proof
before newly publishing output. Never reconstruct a successful result from labels,
an issue comment, a prior readiness boolean or a new Pod. If Pods/Nodes have been
garbage-collected, preserve uncertainty; this feature grants no cleanup authority.

## Shared observer successor: separate prerequisites

The shared branch already defines explicit cluster-use/administer/observe scopes
with tenant composite foreign keys and parent/child revocation. The Go shared
workspace manager deliberately disables native Node inventory. Existing observation
ingestion authenticates submitters, verifies signatures, sequence and receipts,
but its cluster-ownership resolver deliberately excludes shared clusters.

A future shared observer must compose all of the following before replacing the
explicit refusal:

- Current ADP service identity and selected-organization mapping plus authenticated
  operation/run delegation; no conversion of an `agent` string or requester grant
  into observer authority.
- Its own live `cluster:observe` scope and revocation/generation checks. Workspace
  membership, organization administration and cluster-use are insufficient.
- Separately installed, endpoint/CA/cluster-pinned read-only Node transport. Member
  reader/mutator credentials retain namespace-only permissions.
- An authenticated, fresh, replay-bound response carrying the requested org,
  cluster, workspace, original operation, allocation and Pod UID, plus exact Node
  name/UID/providerID and AWS account/region/AZ/instance identity. Reuse maintained
  submitter/signature/sequence/receipt machinery after defining its shared scope;
  do not pretend an existing compatible endpoint already exists.
- A worker-side consumer verifying that response against its original operation,
  lease and current AWS allocation, with before/after revocation checks.

Implementing just a permissive NodeGetter callback would omit these boundaries.
Keep shared execution and this observer unavailable in the dedicated successor.

## Meaningful remote test matrix

No local runtime/CLI tests. Use existing remote API/domain/controller CI and the
executor's real PostgreSQL worker composition with fake AWS/Kubernetes transports.

- Positive completion and retained output from the original Pod on the exact
  running allocation instance, including an approved non-home AWS region.
- Correct selectors/labels but admission-injected foreign nodeName: no successful
  status and no inserted result. Exercise the real `capture` entry independently
  so a preceding readiness success cannot hide this failure.
- Right instance suffix in wrong AZ/region; wrong account/session; duplicate Node
  provider IDs or names; blank UID; deleting Node; incomplete/paginated inventory.
- Node UID/providerID replacement between reads, Pod replacement/nodeName change,
  instance termination or missing approved-region response during log collection.
- Missing/not-running selected instance, non-Ready Node or insufficient allocatable
  GPU evidence. Avoid tests that merely copy helper implementation.
- Ordinary heartbeat/resourceVersion change with unchanged identity remains valid.
- Revocation/fence takeover between first proof, log read, final proof and insert:
  no new result or successful settlement; existing original evidence stays intact.
- Shared target with matching fake Nodes still refuses before any Node/EC2 proof
  call; missing observer cannot trigger dedicated transport fallback. Shared
  projected credentials acquire no extra permissions.
- Stable repeated capture keeps immutable result replay; changed original Pod or
  result cannot overwrite retained content. Missing historical provider evidence
  after interruption remains unresolved without cleanup/release claims.
- Compatibility tests retain boolean `ready_nodes`, existing serving behavior,
  admitted invocation/digests, namespace/result schema and no new provider effects.

Expected implementation ownership: executor `workspace.py`,
`workload_observation.py`, `provider.py`, `results.py`, a small private identity
helper if needed, and focused executor/worker tests. Document the remaining shared
observer and live acceptance gaps without closing #5927 prematurely.
