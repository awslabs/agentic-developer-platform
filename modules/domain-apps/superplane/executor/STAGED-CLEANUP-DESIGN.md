# Staged cleanup: implementable admission and snapshot wiring

Approved design based on b920bae89. The first implementation slice adds immutable
source snapshot capture, schema and pure graph/recipe compilation. Staged preview,
admission and provider effects remain pending review of that slice.
This document includes the approved snapshot, graph and continuation boundaries.

## Approved boundary

Existing source finalization/recovery captures an immutable cleanup snapshot only
after complete original inventory and terminal source creation are established.
Preview remains read-only. It selects existing evidence; it cannot capture new
Node UIDs, call cloud APIs, fence the source, or create a snapshot. No new public
prepare endpoint, shared Gateway/Harness changes, second teardown UUID, or new
retry authority. All maintained changes stay within the Superplane app.

New successful allocations gain this snapshot. Historical sources without it,
and unsealed/incomplete cancelled sources, retain the aggregate path or an
explicit refusal when staged cleanup is requested. No automatic fallback changes
the approval requested by the caller. This is deliberately incomplete coverage.

## 1. Capture point and durable evidence

Add executor-local `cleanup_snapshot.py` and a domain migration/model for
`controller_cleanup_snapshots`. Capture from `Finalizer.__call__` after
`assessment.inventory.complete` is verified, after seal/report publication and
accounting persistence, before returning the successful after-step hook. The
same hook runs for `_ClaimFinalizer` through `Finalizer.__call__`, using that
subclass's verified original recovery operation and claim-bound observations.
No recovery API route is added; capture only writes domain evidence and does not
perform a provider mutation or acquire a fabricated execution grant.

Capture only for a governed dedicated AWS `provision` source. Re-read shared
`confirmed_plan_progress` and actual calls under its original live lease lock:
require COMPLETE, every expected call present and SUCCEEDED, and no intended or
unresolved creation. This initial implementation does not capture a successful
prefix from a cancelled source: existing recovery deliberately skips finalization
for PREFIX. Do not weaken that shared behavior to manufacture evidence.

Use the existing shared seal, complete attested inventory and enumeration binding
as evidence, with the same exact resource membership checked by the finalizer.
Read all source `harness_allocation_resource` identities and original creating
step keys. Require nonempty original compute. Decode Nodes with
`node_inventory.decode`, verify original source creating keys and cluster. For
workload roots retain only original successful POST UIDs, original source row and
deploy step provenance; no label-based adoption. Read exact original network
member/resource rows, including resource and membership generations, descriptors,
native references, source operation/plan and owned/adopted status. Unknown network
creation references cannot yield a staged snapshot.

Snapshot document (canonical JSON, strict keys, finite bounds):

- v1, source operation/plan digest, org/workspace/deployment/allocation/cluster;
- sealed membership revision and sorted original resource identities/kinds/keys;
- sorted exact compute identities, Node references, and workload root references;
- ordered network release recipes, including key/generation/membership generation,
  exact native reference/descriptor/source and immutable owned/adopted attributes;
- source-approved graph contract version; no caller-supplied commands or callbacks.

Store capture proof separately: original attempt/fence/holder, original report
and enumeration binding, capture timestamp. The document digest excludes mutable
observed presence and capture timestamps/fences so repeated source finalization
can compare identical identity content and reuse the same immutable row. Never
replace an existing snapshot with changed identities. Unique source operation
and allocation scope; retain source plan digest and body SHA256. UPDATE/DELETE
are refused; downgrade refuses while evidence remains. Register ORM metadata.

Capture uses only bounded SQL while the source lease is locked: authorize before
the transaction, lock/recheck original shared lease and evidence, write the domain
snapshot, and recheck source claim before completing. Do not call a reentrant
authorization callback while holding its own lease row lock. If separate domain
and execution connections are used, hold the execution fence across the domain
commit and validate again; no cloud I/O or borrowed grants inside either lock.
This follows existing fenced domain-journal composition rather than adding shared
tables or allowing a preview writer to impersonate the source.

Historical complete sources whose lease is already closed are not retroactively
captured by preview. An ordinary authorized recovery finalization may capture
when it actually has a live valid source recovery claim and the full conditions
above; no code fabricates such a claim merely to unlock staged preview.

## 2. Compact approved graph and pure reconstruction

Ordinary parameters are limited to 2000 characters. Shared execution descriptors
permit at most 64 steps/16384 UTF-8 bytes; total operation parameter limits also
apply. A full 16-Node/network identity graph cannot safely fit one normal field.

Add optional canonical `controller_cleanup_graph` header:
`{version:1,snapshot_id:<id>,snapshot_sha256:<hex>,nodes:<n>,roots:<r>,network:<m>}`.
It contains bounded counts and an immutable snapshot commitment, not caller
resource identities. The expanded review displays the snapshot identities and
step mapping so the approved digest has a concrete reviewable meaning.

Pure `teardown_request(source,...,cleanup_graph=None)` retains the exact old branch
when absent. With v1 it validates the closed header and emits fixed descriptors:
one `cordon:<index>` per Node, one `root:<index>` per root, `drain`, `down`, one
`node:<index>` per Node, one `network:<index>` per reverse-ordered network recipe,
and `inventory`. Every descriptor uses existing provider `aws`, operation kind
`delete_cluster`, original capacity target. Stage identity comes from the approved
step ID, never from additional caller arguments. The fixed order and counts allow
Plan.read to validate exact execution_steps without database I/O or extra target
fields. Minimum step count is `2*n + r + m + 3` (drain/down/inventory).

The snapshot loader validates counts against the canonical full document before
admission and before effects. A caller cannot change counts to drop obligations
while preserving the original snapshot hash. The graph/header is accepted only
for teardown of a governed dedicated source; provision requests cannot contain it.
Reject graphs exceeding existing shared bounds; do not truncate or aggregate
several mutations into one step to hide a large graph. Cross-region networking
can exhaust the 64-step cap alongside many Nodes; report this supported limit.

Add `cleanup_graph` to Plan with default None so existing positional construction
and legacy byte fixtures remain intact. `deployment_plan.validate_request` permits
the extra parameter only in the opt-in teardown branch. Node bootstrap action
selection stays unchanged for original provision and old teardown requests.

## 3. Read-only preview, admission and registration

Extend `DeleteDeploymentRequest` with optional `cleanup_mode` enum
`aggregate|staged-v1`, default aggregate. Absence preserves legacy behavior. Both
serving and batch teardown preview/delete use this existing body and route; no
public preparation operation. Request UUID/approval/revision requirements remain.

`deployment_operations.preview_delete`:

1. Keep current tenant/workspace/deployment target and original source lookup.
2. Check existing cleanup eligibility and original paid source registration.
3. For staged-v1, read the already captured snapshot for exact source/scope. Verify
   immutable body digest, source plan/cluster, retained original seal/report revision,
   original UID/creating-step provenance and network identity generations. Derive
   counts/header and planned request using the pure builder. Compare the retained
   original attestation, not a current provider enumeration/query generation:
   legitimate cleanup advances those generations and publishes its own reports.
4. Return the exact planned request/revision plus expanded graph for review. Remove
   the current duplicate final `teardown_request` rebuild or pass the same header
   through it; otherwise it would silently discard staged preview metadata.
5. Missing/ineligible snapshot refuses staged-v1. It does not silently approve
   aggregate. Repeated preview reads the same immutable snapshot, never refreshes
   provider state or changes a stored snapshot.

The delete route already reruns preview. Preserve that behavior and the original
approval/revision comparison. Thread the same header through all independent
original-request reconstructions:

- `cleanup_binding.require_original_request`, `validate`, `bind`;
- `controller_deployments.admit_controller_deployment` expected request gate;
- `deployment_registry.registration_values` and require registration gates;
- `deployment_operations.preview_delete` (both current builder call sites).

Pure reconstruction proves the exact immutable original source inputs plus graph
header; separate async validation proves that header names the trusted snapshot.
Do not rely only on a header extracted from the request to validate itself.
Validation must load the snapshot and compare its source evidence before invoking
the pure equality check. Existing cancellation takeover/payment requirements and
cleanup binding dedup remain; the snapshot supplies identity evidence only.

Normal source eligibility remains current settled-source behavior, but staged-v1
additionally requires its captured snapshot. Cancelled-source eligibility retains
current authenticated cancellation/recovery fencing/payment proof. A complete
snapshot does not override a missing source takeover. Source snapshot identity
does not need changing when a later valid observation fence advances.

No existing second teardown request is admitted: admission's prior action query
and cleanup binding uniqueness remain. If later new identities make a graph
unusable, exposure remains retained; a new teardown UUID is NOT an available
automatic escape. This corrects the earlier design's suggestion of a later new
cleanup approval for discovered obligations.

## 4. Pure network reconstruction and execution mapping

Current `NetworkRuntime.cleanup` reconstructs callbacks by traversing `compose`
with `cleaning=True`; it reads journal parent references and skips a branch when
parents are missing. That is insufficient as an implicit authoritative graph.
Extract an explicit pure recipe compiler from original approved network plan plus
captured parent/native journal descriptors. It returns ordered immutable recipe
descriptors only; no SDK, journal.ensure/member/release, or opaque callbacks during
snapshot/preview/reconstruction. Require set equality with all original member
keys, deduplicate shared parent recipes once, preserve reverse dependency order,
and refuse missing parents/unknown generations or unrepresented keys.

At execution, select stage by recomputing the original shared step_key and matching
the actual intent's operation/provider/kind/target. Load snapshot by exact scope
and approved digest, revalidate count/index and immutable source identities. Bind
the chosen pure recipe to existing read/delete transport methods only then. Never
reconstruct by calling compose in ordinary creating mode.

Keep current authorization immediately before each mutation. For network release,
use exact per-key journal locking and current membership/refcount checks: another
allocation can join/leave, so current peer count is not an immutable graph input.
Reject resource/native descriptor or generation replacement. Existing release
may return early on missing row/member; staged execution must refuse such missing
evidence rather than treating that silent return as success.

Stage postconditions:

- Cordon: exact retained Node name/UID/providerID and unschedulable. A replacement
  or denied read stays UNKNOWN; missing Node needs positive original termination.
- Root DELETE: original successful-POST UID absent through exact GET404; a
  same-name replacement stays UNKNOWN. DeletionTimestamp is not absence.
- Drain: all original roots absent, complete bounded Pod listings of original
  namespace and every original Node, no original workload or foreign Pod there.
  Preserve verified system DaemonSets. Lost ReplicaSet owner evidence cannot be
  recovered from labels; remain UNKNOWN until actual absence is established.
- Down: original durable request ID succeeded and every exact original EC2
  identity/account/region positively reports terminated. NotFound is UNKNOWN.
- Node DELETE: exact original Node UID absent, original compute terminated.
- Network: original resource/generation/native descriptor matches and this
  allocation's membership is durably released. Preserve adopted/peer resources;
  last-owned native resources require exact provider absence.
- Inventory: fresh complete owned-resource enumeration, current seal/report and
  independent cleanup accounting; historical snapshot proof cannot release money.

Fresh discovery outside approved snapshot identities refuses before further
mutations; it does not add a new graph stage. Read-only recovery may confirm
fenced journal evidence after an exact completed provider effect but cannot call
mutation-capable network release. If an exact immutable original native network
identity is freshly observed absent, recovery may atomically record that absence
and release only its original membership under the current fence, even when no
delete was dispatched. It never creates a delete effect to explain preexisting
absence. Unknown native identities remain UNKNOWN. Prior non-successful shared intents are never
redispatched. Prefix continuation uses the existing paid dispatch path unchanged.

## 5. Explicit remaining obstacles / coverage limits

- Historical sources without snapshots and cancelled partial prefixes cannot use
  staged-v1. Existing aggregate handling stays available with its documented gaps.
- The snapshot is complete evidence at source finalization, not a guarantee that
  no outside actor or late native registration will appear later. Fresh checks
  reject anything outside it, and no new teardown UUID is implicitly authorized.
- Individual intent-before-send, lost SkyPilot handle, revoked credentials,
  inconclusive ownership, exhausted attempts/deadline remain UNKNOWN. Splitting
  effects solves completed-effect lost replies and later unrecorded steps only.
- The 64-step/parameter limits exclude some large Node/network combinations.
- A repeated unknown network delete intent remains observation-only; no fabricated
  domain retry or new shared action vocabulary is introduced.

## 6. Implementation and review sequence

First implement snapshot schema/model + source finalizer capture + pure recipe/
header compiler with remote PostgreSQL tests. Then wire opt-in read-only preview
and all three admission/registration gates; test legacy byte stability and forged
snapshot/count/source rejection. Finally add stage dispatch/observers and real
paid recovery-prefix continuation tests at each completed-effect boundary.
Test cancellation and before-send gaps as refusals, not fake successful recovery.
Run static checks locally only; runtime tests in isolated CI. No cloud changes.
