# Recover an original bootstrap before mutations

This path retries one stopped `bootstrap-workspace` operation for an existing-account
managed dedicated workspace with owned networking and public management access.
It preserves the completed infrastructure and all original admission history.
It is limited to the independently qualified worker sources and images in
`modules/gateway/src/shared/domain_bootstrap_boundary.py`. Additional images require
independent source review and qualification; missing files are not a start proof.

When the full historical installer receipt is unavailable, the owner may supply
the genuine sanitized verified statement through the collector's separate archived
history path. This is not a reconstructed full receipt. Pass the current locked
`installer` and `historical_archives` to `installation.bootstrap_recovery.collect`
(or through the revoked/acquisition collector's source options). Keep `original`
equal to the archived verified statement, without adding environment fields.

`historical_archives` contains exactly `verified_statement` and
`recovery_preparation`. Each reference contains `key`, `version_id`,
`archive_sha256`, `member`, and `member_sha256`. The maintained reader retrieves
the exact S3 versions only from the selected account's protected
`domain-evidence/{environment}/superplane/` prefix, verifies archive and member
bytes, and bounds archive expansion. It never extracts archive paths to disk.
An optional `archive_read_session` may supply only those versioned S3 reads when
the installation role lacks archive access. Its current same-account STS identity
is retained with the archive provenance. It cannot replace the installer session
for authority, enrollment, cluster, lock or recovery checks; no new IAM grant is
required. Both identities and the resulting context are rechecked across collection.
The verified statement must say `installed-and-verified`; the independent
preparation receipt must say `recovery-credentials-prepared`, and both must carry
the same installation ID as the current deterministic owner and enrollment.

The preparation environment must match the independently current account, region,
environment, cluster, namespace, domain organization and ADP organization. Current
operator identity, EKS endpoint/CA, native installation lock and owned
Namespace/API/ScaledJob identities/configuration are checked before and after
collection. Any drift or lost lock refuses. The private owner receipt records the
historical archive/member provenance separately from current observations and
explicitly states that the full original receipt was not recovered. All ordinary
enrollment, source/pod, expiry, journal, cleanup and human-authorization checks
remain required; the Gateway proof schema is unchanged. The existing full-original
receipt path remains available without this optional context.

For the pinned bootstrap diagnostic source whose environment ConfigMap changed
after the source stopped, the collector also supports `historical_environment`.
Supply the genuine original receipt and current locked Installer, plus the
retained `snapshot` and ordered `maintenance` entries containing each saved
`plan` and completed `receipt`. The maintained fingerprint file authenticates
these exact private documents and the original protected owner proof; no raw
configuration is committed. This option cannot be combined with
`historical_archives`.

The qualifier proves the original ConfigMap UID, resource version and data from
the retained snapshot, positively observes its original audit read, and rejects
mutation through the source terminal event. Later mutations must match the
reviewed maintenance sequence. It separately verifies the current configuration,
paused runtime, rollout, schema and real owner lock, then repeats protected and
current checks after collection. Historical observations are recorded under
`source_audit.historical_environment`; they do not pretend the current ConfigMap
still contains the original data. Missing evidence or unexplained drift refuses.

Only this verified archived-history context permits a stopped source acquired
within the preceding 30 days. Ordinary planning and full-original collection retain
their 24-hour window. The original acquisition time is unchanged. The owner checks
that the exact audit log group predates the queried window and its current retention
covers that window, then reads from five minutes before acquisition through the
actual current time. Exact creation and terminal pod provenance, the event bound,
and refusal of exec, attach or ephemeral-container access remain mandatory. Retention
metadata alone proves no absence. Current context/enrollment are rechecked afterward;
published owner evidence still expires within one hour.

Keep API dispatch and KEDA paused only after useful execution stops. Under the
maintained Installer lock, `installation.bootstrap_recovery.collect` verifies the
owner identity, enrolled provider, original installation, original Kubernetes pod
audit, exact image source hashes and its immutable workspace policy. `record` defaults
to check-only. Publication uses a conditional create in protected authority storage;
it cannot overwrite a previous record. Evidence expires in one hour. Publish only
when compatible runtimes and fresh recovery decision are ready, after all original
execution deadlines plus the 900-second session guard. Retain the receipt.

If that owner evidence expires before activation, start a new native Installer run
and collect the same stopped attempt again under its real lock. The maintained
publisher reads the original record and its deterministic successors with
consistent point reads. It appends one successor only after the predecessor
expires, preserving the original document and all prior records. Its closed
`renewal` metadata binds the predecessor record ID, exact document digest and
next generation (1 through 8). This counter does not change operation attempts,
lease fences, human decisions or runtime terms.

The successor must retain every source, admission, closure, policy, cleanup and
extension field. Only the fresh observation window and source audit may change;
the audit must preserve the same log group and pod creation event and retain all
previous terminal events. Gateway validates every ancestor's complete boundary,
requires the terminal record to be fresh, and re-reads the whole chain during
resume. Invalid children, competing changes and chains beyond the bound refuse.
Deploy the compatible Gateway reader before publishing any successor. Existing
fresh original records keep their original IDs and behavior.

Before publication, the private native receipt saves the exact intended DynamoDB
item. If a Put response is lost, only observation of that identical item counts as
success. A different winner refuses. A repeated invocation with an existing
intent is observe-only: it neither recollects evidence nor resends an uncertain
write. An absent or changed item requires investigation. A completed legacy
receipt cannot publish a successor; retain it and use a new Installer run.
Check-only collection does not save a publication intent or write remote evidence.
No expired proof or historical timestamp becomes fresh authority.

Deploy Harness schema 16 and domain revision `055_sso_provider_ownership` with compatible
Gateway/scheduler, API and paid worker in coordinated maintenance. Match packaged
sources and installed worker bindings, rotate the workspace provider binding through
the supported owner path, validate it, then verify every serving process before
activation. Downgrades refuse while bootstrap retry history exists. Never replay
an already applied infrastructure plan to reload configuration.

The original requester uses the authenticated workspace API:

1. `POST /workspaces/{id}/bootstrap-recovery/review` with the current operation
   version and a new idempotency key.
2. The original selected human makes `bootstrap-recovery/decision` for that exact
   ticket and plan digest.
3. Before resume, keep dispatch fenced at the producer IAM boundary. Verify the
   exact dispatch denial, then enable the configured dispatcher and KEDA behind
   that fence so the API can verify an executable installation. An inert probe Job
   must remain unready if it shares the API Service selector.
4. `bootstrap-recovery/resume` submits the decision to Gateway. Gateway reads protected
   source evidence, grant mappings, session issuance audits and domain journals itself.
   The absent-journal proof refuses any bootstrap authority, including revoked
   records. The distinct denied-first-grant proof below has its own exact journal
   checks. Both refuse a remaining reservation, membership, network effect or
   bootstrap output artifact.
5. Remove only the reviewed dispatch fence through maintained installation maintenance
   after recovery is recorded, then verify ordinary dispatch and workers.
   The replacement call consumes an immutable linked receipt and separate private
   scratch. The original operation, approval, attempt count/ceiling and files remain;
   normal acquisition consumes its next existing attempt. No Terraform apply runs.

A later pre-mutation failure can receive a separate recovery only after fresh
source/pod qualification, expired grants and sessions, current journal checks, and
a new decision for its exact call and fence. Each receipt links to its predecessor
and must bind the same completed apply artifact. Old proof cannot be reused, and
the original attempt ceiling applies unless the explicit bounded extension below
is independently verified. A reservation released by the
source worker before authority was begun does not require manual cleanup.

The receipt binds the exact completed apply artifact. Its age does not invalidate
this freshly authorized retry of the original bootstrap, but artifact identity,
source admission, current execution authority and all bootstrap prerequisites are
still checked. No artifact timestamp or original approval is changed.

The failed source's underlying cause may be unknown. This recovery authorizes a
bounded retry, not a diagnosis. Bounded exception-chain diagnostics expose types
and frames without messages or locals. Failed command output stays only in private
worker files. Inspect genuine failures before any further recovery. Completion still
requires real bootstrap registration, current provider validation, and browser
readiness across load, reload and reentry.

## A longer bootstrap runtime

The original requester can explicitly request a longer runtime on
`POST /workspaces/{id}/bootstrap-recovery/review` by including
`"max_runtime_seconds": 10800` alongside the expected operation version and a new
idempotency key. The value must be an integer greater than both 900 seconds and
the original runtime, and at most 10800 seconds (three hours). Omitting it retains
the original recovery behavior. Other recovery scopes do not accept this field.

The review exposes the original envelope, a separate `runtime_amendment`, and
`effective_max_runtime_seconds`. The original selected human must approve that
exact review. Its deadline starts when the review is created and covers dispatch,
setup and cleanup; approval or resume does not restart its clock. A changed
runtime requires a new review key. The original approval, request, reservation,
resource/cost limits, completed apply artifact and attempt count remain intact.

Gateway rechecks the persisted decision and records its exact runtime terms and
deadline in the immutable recovery receipt. Lease acquisition and execution
dispatch use that receipt, bounded by its deadline. Provider authority can impose
an earlier deadline. AWS actor credentials still renew in 900-second sessions;
the worker cannot extend its operation authorization by renewing credentials.
Every later recovery needs a fresh decision; omitting the amendment on a later
review does not inherit a previous extended runtime.

Before using this option, deploy compatible API and Gateway/Harness readers and
use the maintained installer to set the native lifecycle worker's
`active_deadline_seconds` to 10800. The default remains 3600; the native controller
worker retains its existing 3600-second maximum. Verify the actual ScaledJob and
ensure current provider enrollment covers the intended execution window. Keep
compatible readers while an extended runtime receipt remains in active history.
This requires no database migration or workspace Terraform replay. All stopped
source qualification, expiry, acquisition cleanup and fresh owner evidence
requirements still apply.

## A positively denied first grant

`bootstrap-denied-first-grant-v1` covers a qualified worker whose very first
`CreateAccessEntry` received AWS `AccessDenied`, and whose ordinary cleanup
recorded that registrar entry absent but failed its final verification. It records
`bootstrap_mutations_started=true` and `bootstrap_grants_created=false`. It does
not relabel an attempted mutation as unstarted. The existing recovery ticket and
receipt scope retain their legacy `unstarted-bootstrap` name for compatibility;
their evidence kind distinguishes these boundaries.

The installation owner uses `installation.bootstrap_denial.collect_denied` with
live protected readers. Gateway's read-only `cleanup_authority` verifies the
original call/fence and every grant/session deadline plus 900 seconds;
`cleanup_snapshot` verifies the original provisioning workspace, unique journal,
unchanged apply target and absence of memberships, effects and bootstrap outputs.
The collector checks the stopped pod and additional pinned grant-plan/EKS source
files, enumerates CloudTrail for the source session window, requires exactly one
denied create with the journal's precise client token, actor, tags and target,
and refuses every other mutation or ambiguous outcome. It requires explicit EKS
NotFound responses for all three planned actors and a complete access-entry
inventory without their groups. Missing logs or a failed read do not prove absence.

Under the Installer lock and with dispatch/workers paused, the trusted owner
composition supplies that revalidated proof to
`superplane_bootstrap.denied_grant.recover_denied_grant`. This backend has no cloud
mutation methods. It calls native `TemporaryAuthority.revoke` and
`SqlRegistrationStore.recover_claim` under the original registration lock. It
retains the immutable grant plan and positively revoked journal, releases only
the matching claim, and refuses extra progress or any observed grant identity.
An uncertain check rolls back this cleanup transaction. Original scratch and
shared operation history remain untouched.

After cleanup, collect fresh evidence with `complete=True` and publish it through
`bootstrap_recovery.record` using the denied collector. Gateway independently
requires the exact retained revoked journal, absent reservation and identical
apply artifact before consuming the fresh human decision. Deploy compatible
Gateway/Harness, API and worker readers before activation. This proof authorizes
only the next existing attempt; it never extends the original ceiling. No new
schema or workspace Terraform replay is required.

## Intervening recovery observers

An automatic recovery observer may advance the lease after the failed worker
stops. Bootstrap retry accepts that transition only after the observer expires
and the protected claim bindings and audit prove an uninterrupted observation
chain from the original call. Any intervening execution, missing claim, changed
workspace or mismatched holder refuses recovery. The original call and producer
remain the evidence source; all execution grants and issued sessions must still
pass their expiry checks. Observer claims do not consume or reset paid attempts.

## A later attempt after retained revocation

`bootstrap-retained-revocation-barrier-v1` identifies a later qualified attempt
that never created a new authority journal. The earlier denied attempt's journal
must remain positively revoked and byte-equivalent to the identity in the exact
consumed predecessor receipt. The new proof binds that receipt's ID, fence and
proof digest to the current failed call's replacement key. A journal from another
operation, a changed plan, any extra progress, or a new journal refuses recovery.
The original absent-journal boundary continues to reject retained journals.

The installation owner uses `installation.bootstrap_retained.collect_retained`
with live expiry and predecessor readers. Gateway independently reads the prior
receipt and current journals, requires absent reservations/effects/memberships
and an unchanged apply artifact, and authenticates the new source pod and image.
Harness checks the predecessor again when recording and consuming the new receipt.
Fresh expiry checks and a new decision still apply; expired predecessor evidence
is historical provenance only. No native cleanup is repeated, no journal is
removed, and no old authorization is extended. A retained predecessor may itself
be a retained-revocation receipt: every link must bind the exact prior proof, call,
fence and unchanged journal back to the denied-first-grant receipt. Gateway and
Harness validate the entire chain, including the reconciled source calls.

Provider preflight validates the operation before and after provider reads. It
checks the provider enrollment before and after its evidence read, while issuing
no credentials. Independent installation, membership, policy and secret-version
reads run concurrently and must all finish successfully; the final consistent
enrollment read still rejects revocation during I/O. Credential issuance retains
its separate checks around STS, audit publication and response delivery.


## Explicit extension of an exhausted retained bootstrap

For a freshly qualified retained-revocation boundary only, the installation owner
may include `attempt_extension` with exactly `authorized_by`, `authorization`,
`previous_limit`, `recorded_attempts`, and `additional_attempts`. Record the actual
human authorization; do not infer an extension from a request to inspect or retry.
The actor must match the enrolled subject, original requester, and current human
recovery principal. Counts must be strict integers matching the protected live
lease, with attempts equal to the exhausted ceiling. The addition must be one to
five attempts and the resulting ceiling at most ten.

The owner collector reads the protected expiry and attempt state before and after
collection. Gateway independently binds the evidence to the actual lease and
requester; Harness repeats the checks under its transaction and operation lock.
The native recovery transaction appends one `execution-attempt-limit-extended`
audit, advances the fence, retains every consumed attempt, and records the linked
receipt. Replaying the same decision returns that receipt without extending again.
A later fresh recovery inherits the recorded ceiling and requires no repeated
extension. No SQL repair, attempt reset, Terraform replay or new paid admission is
part of this path. Deploy compatible Gateway, API, Harness and worker readers
before publishing fresh evidence and resuming.

Read-only `/authority` and `/provider-preflight` calls retry a transient HTTP
502, 503 or 504 once, refreshing and signing the workload identity again after a
bounded delay within the existing total deadline. Authorization refusals,
credential issuance and mutation requests remain non-retrying.


## A retained namespace after temporary authority was revoked

`bootstrap-retained-namespace-v1` accounts for a durable namespace creation intent
whose acknowledgement failed validation. It is distinct from the pre-namespace
boundary. The exact source image must be qualified through the namespace grant;
all earlier temporary registrar grants must be positively revoked and all later
grant/component progress must be absent. The resource-lifetime namespace intent,
full plan hash and full progress hash remain in the immutable recovery receipt.

The collector uses `collect_revoked(..., retained_namespace=True)` and Gateway
independently reconstructs the same snapshot. Harness records that bootstrap
components started. The original artifact, operation, approval, attempt count and
fences are preserved. No namespace is deleted or relabeled. On retry the normal
bootstrap acquisition reads any existing namespace and verifies its workspace and
admission labels before adopting it; retained resources are never claimed to be
absent. A mismatched namespace refuses the bootstrap. This path does not cover
later RBAC grants, CRDs, workloads or registration.

## Interrupted temporary grant acquisition

`bootstrap-acquisition-revoked-v1` covers a qualified worker that stopped while
acquiring the initial registrar, supervisor and installer grants, before any
component installation. It requires a contiguous, generation-bound grant prefix,
a verified retained namespace, no later grant intent, no component progress, and
the original exclusive registration claim. An active installation is outside this
boundary. The stopped image's barrier files are independently qualified offline.

The owner uses `qualify_cleanup(..., acquisition=True)` with fresh protected
snapshot and expiry readers. Under the Installer and native registration locks,
`recover_acquisition` calls `TemporaryAuthority.revoke`; it has no create method.
Kubernetes cleanup verifies generation, UID and pinned permissions, then deletes
with UID/resource-version preconditions. EKS cleanup uses credentials restricted
to each immutable access-entry ARN. A lost acknowledgement requires a positive
observation of the exact grant or its absence. Any changed identity, uncertainty,
new progress or resolver change retains the claim and fails recovery.

Managed workspaces can reach all 25 grants before the authority becomes active:
the original 17 grants, the dormant retirement policy and binding, and six cleanup
RBAC objects. Qualification verifies that exact order, the original allocation,
generation, namespace and policy identities. Unknown grants, an activated fence,
component progress or a changed object refuse this boundary. Native revocation
handles these pinned admission objects through the same UID and resource-version
preconditions as RBAC; it does not remove the retained namespace. The exact
stopped image must also qualify the retirement-fence compiler.

Native revocation preserves the namespace and every journal. Once the matching
claim is released, `collect_revoked(..., acquisition=True)` independently verifies
actor absence and publishes fresh owner evidence. Gateway reconstructs the full
protected journal set and original apply artifact; Harness verifies the consumed
predecessor again. The ordinary recovery decision and next existing attempt are
still required. Neither the attempt ceiling nor infrastructure history changes.

### Bounded extension after complete acquisition cleanup

A complete, positively revoked 25-grant acquisition has one separate bounded
continuation: an exhausted operation at exactly ten of ten may receive one
additional allowance of one to ten attempts, with a resulting ceiling at most
twenty. The owner supplies `attempt_extension` with the existing five fields,
`previous_limit=10`, `recorded_attempts=10`, the explicitly authorized
`additional_attempts`, and the actual original human's authorization. Inspection,
a retry request or this documented capability does not supply that authorization.

The current generation must be present, fully revoked, and retain the original
namespace identity and complete grant prefix. The source qualification must cover
the retirement-fence compiler. The initial exhausted-ten proof can contain at
most ten historical journals. A partial prefix, predecessor-only boundary,
missing cleanup proof, changed live counts, blank or different human authorization,
or any further extension from a raised ceiling refuses. Owner collection checks the protected snapshot
and expiry before and after qualification and proves the disposable cleanup
principal absent. Gateway repeats the boundary, requester and live-lease checks;
Harness repeats the exhausted-ten and bounded-addition checks under the
operation/lease locks. Even an exhausted ceiling between eleven and twenty
cannot receive another allowance through this exception.

The ordinary fresh recovery decision is still required. Its immutable receipt
retains the same paid admission, completed apply artifact, budget, ten consumed
attempts, and every prior receipt. Recovery advances the fence and records one
extension audit; replay returns the same receipt and cannot add capacity. The next
ordinary lease consumes attempt eleven. Later recoveries inherit the recorded
ceiling and still require fresh source, cleanup and expiry evidence and the
ordinary recovery decision; they do not extend the allowance again. No provider
credential, expired owner proof or old human decision is reused. No other recovery
scope gains this exception.

The journal readers retain up to twenty distinct generations so later attempts
can preserve their complete history. This larger history bound grants no attempt
capacity; the locked lease ceiling and original authorization still govern it.

Deploy compatible Gateway, API, Harness and worker readers together before
recording this evidence. Keep compatible readers while the receipt remains in
active operation history; older readers reject the extended acquisition proof.
This requires no schema migration or Terraform replay.

For owner Kubernetes access, the separately planned
`infra/bootstrap-acquisition-cleanup` root creates a disposable role and an EKS
admin entry on this one cluster. Its trust accepts only the existing installer,
an exact session name and a deadline within one hour. A separate installer policy
permits deletion of the three original immutable EKS entries. This root never
changes original actor trust or workspace infrastructure. Review the four-resource
saved plan before applying it under the Installer lock; remove the four temporary
resources after native revocation, including on failure. Publication independently
requires the cleanup role and its access entry to be positively absent. The
successor uses different actor principals, so cleanup sessions cannot authenticate
through its new entries.

During ordinary bootstrap, independent IAM and STS actor-identity reads run in
parallel and share fresh operation checks before and after the read batch. No
identity or authorization result is cached. Immutable role IDs are still compared
for all three actors, and the operation binding is checked again after EKS target
verification. This reduces repeated authority round trips within each grant.

## A bootstrap already settled as unknown

### Completed management components before admission

`bootstrap-management-components-revoked-v1` is a separate qualification for the
original dedicated management bootstrap at source `0025522e4a21c5373aa24aa3f0473ccbef4d5f66`
and worker digest `sha256:845ce1dc5c4d3a1379c548e48d8fd269b1977118793738fc02a6883413fd6117`.
Its complete bootstrap/provisioning source was independently hashed in an offline,
read-only container; the app-owned manifest is hash-pinned and staged into Gateway.
This does not qualify that image for other recovery boundaries.

The boundary retains exactly one original 25-grant plan and its complete revoked
progress, including the namespace UID, five owned reader components and their
desired documents, creation markers, immutable UIDs and specification digests.
Complete management component/prerequisite inventories and the original system
workload baseline are required. All 24 revocable grants must be positively
revoked; the resource-lifetime namespace remains owned. The qualified source
persists `retain_workspace` before taint removal or final registration and never
removes that marker. Its absence therefore establishes the pre-admission boundary.
An adopted or incomplete component, additional component, surviving grant,
admission marker, current registration reservation, membership, lifecycle effect
or completed bootstrap artifact refuses recovery.

After the ordinary unknown/retain settlement is acknowledged, use the same fresh
original-human settled-bootstrap review and explicit runtime amendment described
below. Under the existing Installer lock with workers/dispatch paused, collect
the snapshot with `registrar_snapshot(..., components=True)` and the evidence with
`installation.bootstrap_components.collect_components`. The collector performs
no cleanup: it rechecks source/session expiry, the full protected journal and
original retained settlement, verifies EKS actor and disposable cleanup-principal
absence, and rereads snapshot/expiry before returning evidence for the existing
immutable publication path. Do not use acquisition cleanup on this journal.

Gateway independently reconstructs the full snapshot and original apply artifact.
The immutable receipt explicitly records that components started and remain;
the old no-effects predicate does not accept it. The compatible internal bootstrap
receipt scope is retained. No prior retry receipt or attempt extension is accepted.
The replacement receives a fresh grant generation and private execution directory.
Native `ComponentJournal` re-observes each original UID, creation marker and full
specification before reuse; a changed object refuses. The namespace, old journal,
original apply, closure, budget and attempt accounting remain unchanged. No
Terraform apply or journal reset occurs.

Reader-enabled future acquisition plans have 21 or 29 grants. Their four issuer
grants are checked exactly, and their evidence carries `reader_renewal: true`.
Their stopped image must separately qualify `reader_renewal_acquisition`; existing
17/25-grant source qualifications do not acquire this capability. The initial
component-bearing qualification above remains limited to its original 25 grants.

### Review and reopen the retained settlement

Use `/workspaces/{id}/settled-bootstrap-recovery/review`, `/decision`, and `/resume`
for an original bootstrap whose authenticated recovery closed as `unknown` with an
unresolved provider intent. Include the current version, a new review key and
`max_runtime_seconds: 10800`. The returned ticket names `settled-bootstrap-v1`,
its closure digest and the exact runtime amendment. The selected human's decision
reconfirms only the unchanged retained budget; it does not edit the old approval.

If the original admission already allowed 10800 seconds, this review uses the
distinct `bootstrap-runtime-reauthorization-v1` term for a fresh 10800-second
window. It requires the same settled closure and a new current human decision;
Gateway and the immutable runtime receipt both validate that boundary. It does
not enlarge the original envelope or attempt ceiling. Ordinary unstarted
bootstrap extensions still require a strictly larger cap and cannot use this
term; all bootstrap runtime authority remains bounded by three hours.

Owner preflight uses `domain_bootstrap_recovery.cleanup_authority` on the actual
closed source. It returns `settled_bootstrap` (the immutable source snapshot) and
the original acquisition audit timestamp. Add these exact fields to the descriptor.
Do not manufacture open-lease fields. Native `qualify_cleanup` and
`recover_acquisition` still run under the Installer lock with paused dispatch and
workers. They bind the original retained settlement to the same journal generation,
claim and byte-identical plan. After native cleanup, `collect_revoked(acquisition=True)`
retains the closure in the owner evidence and verifies positive revocation. Remove
the disposable cleanup authority before publishing. The original namespace remains.

Gateway re-reads the closed provenance, every expired execution mapping and issued
session, current domain journals and completed apply artifact. It compares the
owner evidence to the exact human-reviewed closure. Only then does the fenced
Harness transaction preserve closure/source history in an immutable receipt,
reconcile the original unknown intent and reopen the live projection at a greater
version and fence. No original timestamp, deadline, admission or artifact is edited.
The next native worker gets its own authority and the original completed apply.

This boundary requires compatible Harness schema 17 and domain revision
`056_recovery_settlement_claims`. Shared and domain settlement receipts are per
claim, so a later failed execution can be settled without overwriting the original
receipt. Old acknowledged deliveries are idempotent and cannot reapply accounting
to the new reservation. Keep compatible readers installed while this history exists.
The original source must have no prior retry receipts; this is not a generic reset
for arbitrary terminal operations. A missing/stale closure, withdrawn decision,
changed journal/apply, pending settlement or unrevoked authority refuses.

The exact worker at revision `61fcfc098e6e110411402342fcfc3a3531130d21` is qualified
by the app-owned `installation/evidence/bootstrap-worker-61fcfc.json` manifest.
Its 19 barrier files were independently read from the image with networking disabled
and a read-only filesystem, and match the preceding qualified image. Gateway stages
this exact manifest into its image and pins its hash; arbitrary images remain refused.
