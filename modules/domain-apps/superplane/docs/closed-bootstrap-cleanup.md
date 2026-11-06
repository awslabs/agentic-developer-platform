# Service-held cleanup of a closed bootstrap

A stopped bootstrap can retain its registrar and original RBAC grants after its
paid lease closes. Normal provider-session delivery correctly refuses that lease.
This recovery uses `recover_components` in the Gateway process under a separate,
owner-issued cleanup permit. It does not reopen the operation or renew provider
enrollment. The original budget, attempts, namespace, components and immutable
source/provenance requirements remain in force.

This is a source capability, not evidence of a deployed or recovered workspace.

## Authority and scope

`installation.closed_cleanup.prepare` reuses `qualify_cleanup` with the existing
protected source, journal and expiry readers. It produces an inert permit for one
closed operation, original enrollment revision, registrar RoleId, object roster,
artifact and running service source digest. A permit lasts at most one hour.
`publish` writes only the exact reviewed bytes, conditionally, to the existing
owner-only provider authority table. Lost writes are read back; timestamps are
never regenerated during publication. Bounded lossless encoding keeps large
original provenance within DynamoDB limits; the revision hashes the full decoded
document. The Gateway cannot write this table.

At use, the service checks the current original human membership, installation,
provider role/policy/secret identities, closed operation, lease and original
session expiry again. It obtains fresh STS sessions held only in process memory.
Session identity, expiry and scope digests are recorded separately from cleanup
progress in the evidence table. No credentials are returned, persisted in files,
or transported through operator stdin. The normal paid broker retains its expiry
checks and cannot consume this permit.

The original registrar's existing Kubernetes administrator mapping supplies the
native deletions. This is privileged **service-held** authority. It gives the
operator neither administrator credentials nor an arbitrary Kubernetes API.
The transport permits named GETs and original RBAC DELETEs, plus the canonical
dormant native retirement policy/binding pair from the sealed original grant plan.
Both generated names, complete documents, generation, original allocation and
registrar ownership must match. Active, foreign or modified admission objects
are refused. Before deleting the binding, the service independently reads its
original policy again and refuses activation or identity drift. Native reverse
revocation order and the registration/operation locks remain in force.
Every deletion requires original
UID/digest, current resourceVersion, pinned endpoint/CA and native admission.
It rejects finalizers, redirects, discovery, list, secrets, exec, impersonation,
and changed objects. It never creates Kubernetes objects or deletes the namespace
or retained components.

Final verification requires a reader that survives registrar removal. The service
creates a group-free EKS mapping for the original provider role and associates
`AmazonEKSAdminViewPolicy`. **This AWS-managed policy grants cluster-wide
get/list/watch, including Secrets. It is not exact-object RBAC.** The native
transport restricts actual use to its sealed named roster and exports no keys.
This underlying read scope must be reviewed explicitly; if it is disallowed,
this path is unavailable. `AmazonEKSViewPolicy` omits the RBAC reads needed here.
See [AWS access-policy permissions](https://docs.aws.amazon.com/eks/latest/userguide/access-policy-permissions.html).

## Installation and execution order

1. Review, test, build and install the selected Gateway source through the normal
   installation owner and deployment gates. `stage-contracts.sh` packages the
   canonical native code, and the image build runs its import check. In the exact
   selected pod, read the source digest with
   `python -m src.internal.domain_closed_cleanup_source`. Keep the original
   workers/producers paused. No enrollment renewal is useful before this path is
   installed and reachable.
2. Under the existing installation/operation qualification locks, refresh original
   source/provenance approval, expiry and journal evidence. Use `prepare` to bind
   those observations to the selected service digest and a fresh permit attempt.
   Preserve and review the returned document. The publisher's live requalification
   callback must reproduce its stable binding, including protected operation,
   lease, artifact and original object identities.
3. Prepare private Terraform input `{"permit": <reviewed document>}` for
   `infra/closed-cleanup-access`. Its saved plan may create only the app-owned
   managed policy and its attachment to the existing registrar. Review its exact
   target, tags, dates and service-held read scope before applying. It owns no role,
   trust, provider policy, cluster or platform resource. Never reuse an expired plan.
4. Publish the exact permit through the owner command. Invoke only the maintained
   module in the reviewed Gateway pod:
   `python -m src.internal.domain_closed_cleanup cleanup --permit-id <protected-id>`.
   The dispatch/operation locks exclude paid successors. Native registration locks,
   claim comparison, admission and journal checks remain in `recover_components`.
5. The independent provider reader verifies original RBAC absence and retained
   namespace/components after original EKS grants are removed. Native SQL commits
   before the service writes its completion receipt. A lost receipt re-observes
   committed state without replaying deletions or obtaining another reservation.
6. The service removes the verifier policy and entry using fresh sessions narrowed
   to its observed immutable EKS ARN. Independent provider AWS reads prove absence.
   The owner then prepares and reviews a fresh Terraform destroy plan limited to
   this temporary policy/attachment, applies it, and conditionally revokes the exact
   cleanup permit. Do not restore removed bootstrap grants.
7. Wait until every recorded cleanup session expires. `verify_teardown` independently
   requires original/verifier EKS absence, temporary IAM policy absence, a revoked
   exact permit, completed service receipt and expired sessions. Pass
   `cleanup_permit` to `collect_components` before the normal immutable owner proof
   publication. That collector also refuses any surviving provider verifier entry.
8. Later recovery/admission still requires the original human's normal sign-in and
   fresh native decision. Cleanup does not admit or resume paid work, change its
   budget, or replay a prior plan/admission. Enrollment renewal, if later needed,
   remains a separate native step performed only when it can be used.

## Failure and teardown

Every uncertain create is recorded before the AWS call. A retry reconciles an
observed, exactly tagged entry; it never blindly repeats a create. An absent
entry with an unresolved create outcome remains unresolved rather than producing
a false teardown receipt. Reconcile that intent with owner observations before
proceeding. Permission failures, foreign tags, replaced entries, changed UIDs and
non-native effects all refuse cleanup.

The service attempts reader teardown after a failure and retains all evidence.
The maintained `teardown --permit-id <protected-id>` mode performs only reader
removal; it does not require the domain database or Kubernetes endpoint. It still
requires the current owner permit, original closed operation and current identity
checks. If these checks or AWS access are unavailable, access removal remains
pending and must not be reported complete. A permit can be renewed through an
exact reviewed conditional successor only while the supported service path exists;
this never renews provider enrollment. A fully abandoned attempt is terminal;
a separately qualified owner permit with a new attempt ID is required to restart.

The underlying temporary IAM policy expires with the permit. Expiry alone does
not remove its EKS mapping, IAM attachment, or journal. Record and verify their
removal through the steps above. An independent owner observation is required
before publishing recovery proof.


## Retained partial reader generations

A closed reader attempt may follow more than one completed component retry. The
reader contract verifies every immutable predecessor receipt, its retained native
settlement and every authority journal, bounded to four predecessors and the
unchanged original attempt cap. The oldest original 25-grant journal remains the
complete five-object ownership anchor regardless of journal sorting order.

Only the separately catalogued c8f44a0b/12be5519 stopped image can use the partial
reader boundary. Its current 29-grant journal must contain exactly the nonempty
`components` subset and `prerequisite_inventory` markers, alongside ordinary grant
revocation state. Each recorded component must exactly match the oldest roster.
The proof seals that roster separately as `inherited_components`; it adds no
completion, management-observation or system-baseline markers to stored progress.
The permit's named read scope includes all five originals. Native cleanup and
lost-response verification read all five identities, preserve the actual partial
journal, revoke only recorded grants, and release the native registration claim.

Use fresh installed-source qualification and permit/IAM plans after actual native
unknown/retain closure and expiration of original authority. Earlier cleanup plans
cannot be reused. The verifier still requires independent teardown and absence
proof; source qualification does not authorize paid execution or replace the
original human's fresh recovery decision. No IAM actions or Kubernetes admin
credentials are added by this lineage extension.

Complete retained closure bytes are encoded losslessly for DynamoDB. Permit and
owner-proof decoding are capped at four MiB expanded; new encoded items remain
below 380,000 bytes. Small historical owner items retain their original encoding,
record IDs and revisions; larger new owner proofs use canonical gzip/base64 with
revision hashes over the original expanded document. Deploy compatible Gateway
readers before publishing a compressed proof. No historical item is rewritten.

## Bounded verification reads

The sealed Kubernetes transport can verify one exact roster as a read-only batch.
It validates the full source, owner, operation, enrollment and expiry before
obtaining the reader and immediately before and after the batch. A batch lasts
no longer than 30 seconds to qualify; later results are refused. It selects
only original named objects and refuses nested
batches, writes, undeclared targets, redirects and failed responses. Reader
material stays in service memory and is discarded when the batch exits.

Native component recovery still performs both complete absence checks and all
original namespace/component UID, desired-document and creation checks. Each
pass obtains fresh authority; no result is cached for another pass or mutation.
Per-object deletion still uses its existing fences, separate registrar session
and exact UID/resourceVersion preconditions. A failed post-read authority check
prevents successful cleanup and claim release. Transports without this optional
capability retain their original per-request verification behavior.

This avoids multiplying full cloud-identity validation by every read in both
final verification passes. Source/CI/image deployment and a fresh exact native
permit remain required before using changed runtime bytes; the optimization
does not extend an active permit or make an incomplete cleanup successful.
