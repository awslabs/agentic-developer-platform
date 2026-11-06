# Original planning start recovery

An interrupted initial plan can record AWS credential issuance without ever
reaching Terraform. The `unstarted-planning` recovery scope requires independent
installation-owner evidence; an issuance audit remains intact. It never treats
an HTTP timeout, empty cloud listing, or missing artifact alone as an absence
proof. Apply, adoption, bootstrap, shared placement and account creation remain
ineligible.

The maintained `installation.planning_probe` builds an inert Job using the
qualified worker image, no workload/database/provider credentials, and the
original PVC mounted read-only in the container. CSI staging retains normal
ownership and security-label handling. The process independently verifies the
mount's read-only flag and inspects only the exact original operation directory.
It does not create, remove, rename or repair retained state.
The bounded job requests 256 MiB and permits 512 MiB for the collector,
Terraform and native provider startup. An OOM-killed probe supplies no evidence;
retain its failure and collect a fresh completed probe before recovery.

Under the maintained installation lock, `installation.planning_recovery.record`
collects evidence itself. Select the original verified installation receipt,
owner enrollment, exact original operation/fence/principal/pod selectors and
the completed probe Job. The collector verifies the operator ARN and immutable
RoleId, selected cluster, paused dispatch/worker, original PVC/PV UIDs, no live
volume user, exact probe manifest and live probe output. It reads protected EKS
control-plane audit records for the original server-assigned pod UID, its
creation and terminal state. Missing audit provenance, command overrides,
additional containers, exec/attach/ephemeral-container access or changed image
refuses evidence. It reads the original image by digest without network access
and compares lifecycle runtime sources against the maintained start-barrier
implementation. That implementation obtains its session before exclusively
creating its private operation directory, and creates the directory before any
planning process. The lifecycle runtime does not remove that directory.

The qualified source, original pod/volume provenance, quiescence and retained
directory observation together establish an unstarted planning phase. If the
directory contains unknown output or provenance is uncertain, this owner refuses.
The separately bounded retained-preparation cases below preserve their exact
files; they do not permit replay of a started Terraform plan. The evidence is written once
under a distinct `sppr1:` key in the existing protected provider-authority table.
Gateway and app runtimes cannot write that table. Evidence is valid for one hour;
publish it only after the repaired runtime is installed and ready for recovery.
Keep the review/installation receipt and original audit records.

The original requester calls `/workspaces/{workspace_id}/planning-recovery/review`
with the current operation version and a fresh idempotency key. The selected
original human approver decides the unchanged plan through `/decision`.
`/resume` invokes the protected producer route `/recover-unstarted-planning`.
All three domain routes retain their normal workspace permission checks. The
fresh decision authorizes only the original initial planning step; it does not
approve a subsequent infrastructure apply.

Gateway joins the owner record to the original operation, admitted call, plan,
pod binding, acquisition time, provider and installation. It independently
proves all protected execution grants expired, waits a full 900-second session
window after their deadlines, and verifies every retained issuance audit has
an expired session from the same reserved broker. Missing expiry, active session,
withdrawn authority, artifact, provider result, resource, cancellation, settlement
or exhausted attempt budget prevents recovery. Current human authority and
confirmed original budget are checked again around proof collection.

Harness schema 13 appends an immutable receipt for each separately proven
restart. It reconciles only the exact unstarted intent, links a new key to that
intent, advances fencing and preserves attempts and their ceiling. The complete
chain is validated before execution or acknowledgement of expired deliveries.
Old recovery response replay remains idempotent and cannot change the latest
authority. Domain migration 048 adds the distinct decision scope. Coordinate
Gateway/tick, API and worker consumers with these schemas under the installer
maintenance lock; old consumers cannot safely read receipt chains. Downgrade
refuses after a start-barrier recovery has been recorded.

An initial preparation that stopped before Terraform initialization can also
establish the start barrier. The inert inspection must find the exact maintained
module sources, one variables file and an empty review directory, with no backend
metadata, state, plan or unexpected output. Only the known AWS CLI session-cache
and Terraform checkpoint paths may accompany these files. Every retained file is
hashed; symlinks, hard links, foreign ownership and oversized trees refuse.
Original pod/image provenance and expired session checks remain mandatory.
The worker consumes the immutable receipt, verifies the inspected bytes again,
and preserves the directory under a receipt-derived archive name before creating
a fresh private preparation directory. It never deletes or overwrites the archive.

An explicitly authorized attempt extension may accompany that protected owner
record. It names the actual authorizing requester, the authorization, exact prior
ceiling and recorded count, and one to five additional attempts (total at most ten).
The original requester must still make the fresh recovery decision. Harness checks
those bindings under the original operation lock, appends the extension in the
immutable recovery proof and audit, and changes the ceiling atomically with
recovery. Counts, admission, approvals, reservation and prior receipts remain
unchanged. Replaying a decision cannot add attempts again. No extension is inferred
from a worker restart or an ordinary recovery request.

Planning probes use supplemental groups rather than fsGroup so inspection cannot
recursively change retained file permissions. Worker and initial storage mounts use
OnRootMismatch. Recovery tightens its own private directory to0700; it never
relaxes permissions or changes the shared volume owner. A source-image review may
select the original qualified source checkout explicitly; its Git revision must
match the original image's OCI label and every source hash must match the image.
Deploy compatible Gateway/Harness and worker behavior together before publishing
new owner evidence. These optional proof fields use the existing immutable
Harness13 receipts; no existing receipt or approval is rewritten.

### Interrupted provider installation

The `incomplete-provider-install-v1` boundary is distinct from pre-init recovery.
It accepts only the maintained Linux amd64 recipe with AWS 6.65.0 and TLS 4.4.1,
exact original module/lock bytes, an empty review directory and initialization-only
S3 backend metadata. The AWS provider directory must be absent; only the known
TLS package files may accompany the metadata. The qualified source runs checked
initialization before planning, creates the operation directory exclusively and
never removes installed providers. Together with original pod/image provenance,
this establishes that the Terraform plan command was not reached. A complete
provider installation, any plan/state output, an unexpected file, modified source,
or uncertain provenance refuses. This is not general initialized-state recovery.

The inert probe emits bounded backend metadata only after rejecting credentials,
endpoint overrides and state content. The owner binds its exact bytes and selected
workspace identity to the original immutable lifecycle policy, verifies the source
pod's policy mount and hash, then reads the exact versioned backend key and both
DynamoDB lock/checksum records. Existing versions, delete markers, locks, a disabled
version history, wrong account or any read error refuse. These reads corroborate
the source barrier; they are not a general proof that AWS resources are absent.

The recovery receipt retains the full file inventory, source and backend evidence.
The compatible worker verifies every retained byte before archiving the original
directory under that receipt. It never executes or deletes the archived providers.
All existing grant/session expiry barriers, fresh human decision, attempt ceiling,
original budget and original-operation checks remain required. Install the
compatible worker before publishing this boundary; no budget increase or attempt
extension is implied by incomplete provider installation.

### Historical provider checksum refusal

The `provider-lock-refusal-v1` boundary covers only the historical archive-only
lock for AWS 6.65.0 and TLS 4.4.1, with the exact reviewed read-only initialization
recipe and complete Linux amd64 provider cache. Terraform 1.9.8 accepts the signed
ZIP identities during installation but rejects the unpacked cache before loading
provider schemas when no package hashes are recorded. The inert read-only probe
runs credential-free validation and requires the exact checksum refusal for both
packages. A repaired lock, arbitrary validation error, successful validation,
altered recipe, saved plan or unexpected file is ineligible.

Original source/image/pod and policy provenance, empty versioned backend and
lock observations, all session expiry barriers and a fresh recovery decision
remain mandatory. The owner records the native binary and diagnostic hashes.
Inspection streams the one known large AWS binary with a one-GiB bound; other
files retain the existing aggregate bound. Recovery preserves every inspected
byte under the immutable receipt before any new preparation. Compatible workers
use the authenticated package hashes in the maintained lock and validate the
native fixture after read-only initialization. This boundary neither approves
apply nor adds attempts. Any unchanged daily-cap confirmation is explicit in the
fresh recovery review; see [budget confirmations](planning-budget-amendments.md).

### Historical provider socket refusal

The separate `provider-socket-refusal-v1` boundary requires the exact historical
process runner, Terraform adapter, preparation recipe and repaired package lock.
That runner sets `TMPDIR` beneath the hashed operation directory. Its 122-byte
path exceeds Linux's Unix socket address limit before the provider appends its
socket name. Neither pinned provider can start its schema service there.

The owner verifies original pod/source/volume/policy provenance and the exact
complete provider cache with an empty review directory. A credential-free probe
mounts retained state read-only and reproduces both providers' explicit socket
bind failures using an equally long path in separate private tmpfs. It runs
`terraform validate`, never plan, apply or a cloud operation. Other diagnostics,
different source, successful schema loading, any saved plan or unexpected file
refuse. The owner also requires the empty versioned backend and lock observations,
expired source grants/sessions and a fresh unchanged-plan recovery decision.
Every retained byte is archived under the immutable receipt; attempts remain
counted. This is not general recovery of failed Terraform plans.

The corrected runner creates disk-backed scratch under the private operation
directory and exposes it through a short symlink inside a separate mode-0700
`/tmp` directory. Downloads remain on the operation volume; provider socket
addresses remain short. Both directories are removed after child processes stop.
The native fixture uses the real hashed directory layout, a 64-MiB `/tmp`,
read-only provider initialization and successful schema validation.
Failed checked commands retain their bounded output in separate mode-0700
directories with mode-0600 files on the private operation volume. Public errors
and worker logs remain generic. These diagnostics are for owner investigation;
their presence does not authorize another attempt or fit an older boundary.

### Created registrar grants before namespace intent

Use `bootstrap-pre-namespace-revoked-v1` only for a qualified stopped worker with
exactly the registrar entry/policy prefix and no Kubernetes intent. Preserve all
previous journals and consumed retry receipts. The owner collector in
`installation/bootstrap_revoked.py` verifies stopped source, expired authority,
protected journal continuity and positive AWS absence after cleanup. No empty
listing or old owner publication substitutes for this fresh evidence.

`workspace_bootstrap/superplane_bootstrap/registrar_recovery.py` composes native
revocation and claim release under the original registration lock. Supply
`EksGrants` with deletion credentials narrowed to the exact immutable entry ARN.
The optional `infra/bootstrap-registrar-cleanup` root creates a time-limited
policy on the existing installer role; apply a reviewed saved plan and remove it
with a reviewed destroy plan after cleanup. Never directly repair journal rows,
reset attempts, replay Terraform, or relabel created grants as unstarted.

Publish fresh owner evidence only after native cleanup and positive absence,
record a current authenticated requester decision, and resume the original
bootstrap through the existing recovery endpoint. The Gateway and Harness
independently validate retained history, attempt accounting and the unchanged
apply artifact. Verify actual workspace readiness through the ordinary client and
browser after the next attempt.
