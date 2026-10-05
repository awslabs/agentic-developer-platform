# S15 Mountpoint ownership acceptance and rollout plan

Status: source preparation only. No IAM grants, Kubernetes objects, production
mounts or S3 objects were created by preparing this plan. Nonroot image PR6043
and scratch PR6060 are source progress; full S15 isolation remains open.

## Why explicit mount ownership is needed

Filtered live inventory on 2026-09-25 shows `s3-csi-node` using
`aws-s3-csi-driver:v1.15.0`, systemd host mounts, no `MOUNTER_KIND` setting and no
Mountpoint Pods. Upstream [env.go](https://github.com/awslabs/mountpoint-s3-csi-driver/blob/v1.15.0/pkg/util/env.go)
selects the pod mounter only when `MOUNTER_KIND=pod`.
[node.go](https://github.com/awslabs/mountpoint-s3-csi-driver/blob/v1.15.0/pkg/driver/node/node.go)
advertises `VOLUME_MOUNT_GROUP` only for that pod mounter and only then expands
fsGroup into gid/allow-other/mode flags. Thus the observed systemd driver does
not derive ownership from ingestion's fsGroup1001. Its live RWX PV currently has
only `allow-delete` and `allow-overwrite`.

The source PV now explicitly requests `allow-other`, `uid=1001`, `gid=1001`,
`file-mode=0640`, `dir-mode=0750`. These are synthetic FUSE ownership/modes, not
S3 object ACLs. UID1001 gets owner writes; GID1001 gets read/traverse only; other
UIDs have no access. `allow-other` enables the FUSE mount's kernel permission
checks for the non-root process; it does not grant world filesystem access.
Never run recursive chmod/chown on S3.

Zoekt's prepare-index and shard-sync containers are additional shared readers,
not covered by the five ingestion templates. Their source mounts already use
`readOnly:true` and `subPath:zoekt-shards`. The pod gains supplementalGroups1001
so its root readers can read group-owned data even without DAC override. This
is process group membership, not fsGroup and not a recursive volume operation.
The main Zoekt server reads its separate scratch index. Inventory actual reader
UIDs/groups before rollout; do not assume all images run as root.

## Prepared isolated probe

`scripts/prepare-mountpoint-probe.py` renders JSON only. It does not invoke AWS,
Kubernetes, a shell or deployment workflows. Supply a fresh 12hex run ID,
reviewed account879318057152, bucket and exact EKS OIDC issuer. Example command
(the issuer must come from a current DescribeCluster receipt):

```
python modules/agent-context/scripts/prepare-mountpoint-probe.py \
  --run 012345abcdef --account 879318057152 \
  --bucket agent-context-platform-data-879318057152 \
  --oidc oidc.eks.us-east-1.amazonaws.com/id/REPLACE_WITH_REVIEWED_ISSUER \
  --output new-probe-plan.json
```

The output separates role policies, setup objects, individual Jobs and a denied
prefix stage. It is a review bundle, not one bulk `kubectl apply` manifest.

- Namespace, two SAs, role names, PV handles and PVCs are unique to the run.
  The mount prefix is `security-validation/s15/<run>/`; no production PVC or
  tenant object is mounted. PVs use `Retain` and explicit claim refs. The1Gi
  capacity is not an S3 byte quota; the trusted script writes only two tiny
  fixed files and Job deadlines bound execution.
- Two prepared IRSA role policies have exact issuer/audience/SA trust. Writer
  permits only own-prefix List/Get/Put/AbortMultipart; reader only List/Get.
  No global S3, SecretsManager, EKS, IAM mutation or provider credentials.
  No IAM role or policy is actually created by rendering. Read-only metadata
  shows default SSE-KMS using the AWS-managed aws/s3 key; its reviewed policy
  allows same-account use via s3.us-east-1.amazonaws.com for authorized S3
  principals. No direct KMS permission is added. Reconfirm encryption/key
  policy and stop rather than widen permission if the live mount fails.
- PV `authenticationSource:pod` prevents driver-wide credential fallback. This
  works with the v1.15 systemd mounter independently of delegated fsGroup:
  [provider_pod.go](https://github.com/awslabs/mountpoint-s3-csi-driver/blob/v1.15.0/pkg/driver/node/credentialprovider/provider_pod.go)
  explicitly handles systemd IRSA and refuses missing IRSA role annotation.
  Live CSIDriver has `podInfoOnMount:true` and STS tokenRequests, but a real
  mount must verify this path. Do not switch to driver authentication on failure.
- The app has `automountServiceAccountToken:false`, no envFrom, projected secret
  or host mount, and `eks.amazonaws.com/skip-containers:probe` suppresses IRSA
  application token/env injection. CSI consumes its requested token node-side.
  Each Pod starts with a run-owned schedulingGate; inspect its admitted spec
  and pinned image reference before releasing it. The actual imageID exists
  only after start and is checked then. Unknown admission injections or
  credentials fail gate release. The script checks token paths/env.
- Fixed promoted image digest
  `879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-dev-agent-context-ingestion@sha256:0de0c4e65ce0810fd00bd0ed2dbb6fa85311490bc52eea115f27d34bd636368d`
  is used only with the explicit synthetic Python probe command. It has no
  repository/model/database workload. Read-only root, dropped capabilities,
  no escalation, RuntimeDefault seccomp,32Mi tmp, strict supplemental groups,
  bounded resources, no retries and180second deadline apply.
- Namespace baseline PodSecurity allows the intentional root reader while
  prohibiting privileged forms. Writer is UID/GID1001. Reader is UID0/GID1001
  with all caps dropped and read-only volume. Outsider is UID/GID2002 without
  group1001; `supplementalGroupsPolicy:Strict` prevents image group leakage.
  Verify node support for Strict before use. There is no fsGroup/chown probe.
- Default-deny NetworkPolicy is prepared, but its presence is not proof of CNI
  enforcement on EKS Auto Mode. The synthetic command makes no socket/provider
  calls. CSI's node-side S3/STS traffic is necessary and separate. This probe
  does not accept parser network isolation or per-asset production authority.

## Staged acceptance, with no production change

1. Supervisor re-reads cluster/account/OIDC, driver image/mounter mode,
   CSIDriver tokenRequests, supported node group policy, IRSA webhook behavior,
   bucket encryption and relevant admission. Verify exact image manifest and
   that run namespace/roles/PVs/claims/prefix do not exist. Archive reviewed
   render hash, IAM policies and expected objects. Existing objects are not
   reused or overwritten. Obtain any final live-action approval outside this
   source-only task.
2. Create only the reviewed run-owned roles/SAs/setup. No shared-driver policy,
   deployment group membership or cluster-admin grants are needed for the app.
   The supervisor's narrowly scoped operator access creates these objects.
   Verify actual role policies/trust and identity wiring; no credential dumps.
3. Create only writer Job, whose Pod remains SchedulingGated on
   `security.adp.dev/s15-<run>`. Confirm namespace, controller owner UID, Pod UID,
   resourceVersion and exactly that gate. Compare the admitted spec against the
   reviewed render: exactly one named container, no init/ephemeral/sidecar
   additions, the exact digest image reference, live-csi command arguments,
   service account, automount=false, skip-containers annotation, explicit
   security/resources/groups, exact env list and only the three declared
   volumes/mounts. Unknown injected env/volumes/sidecars or credential paths
   fail release; never print their values. Kubernetes defaults must be explicitly
   accounted for by the review, not broadly ignored.

   Store the reviewed admitted spec privately. Release only that Pod using a
   JSON Patch that tests `/metadata/uid`, `/metadata/resourceVersion` and the
   entire `/spec` against the reviewed values, then removes
   `/spec/schedulingGates`. Any changed precondition aborts for fresh review;
   never patch the Job template to bypass the gate or remove arbitrary gates.
   Use the same process for reader, outsider and denied-prefix Pods. Gate review
   time counts toward the180second Job deadline: prepare validation before Job
   creation, and leave expired/failed-review Jobs gated and stop that attempt.
   Do not extend an expired attempt or remove a gate just to avoid timeout.

   After start, verify actual container imageID against the reviewed immutable
   OCI index/linux-amd64 manifest chain. Require UID/GID, real CSI mount,
   token-free app and `result:pass` with `acceptance_scope:live-csi-mount`.
   The live command rejects non-FUSE filesystems and requires source
   `mountpoint-s3` with type `fuse` or `fuse.mountpoint-s3`; generic FUSE alone
   does not pass. The local Docker harness explicitly selects
   `local-posix-fixture`, which emits `local-fixture-only`; this argument is
   never present in rendered live Jobs and must fail admission review. It creates/fully overwrites/reads
   `roundtrip.json` and creates `zoekt-shards/reader-fixture.txt` (tiny inert
   bytes). Verify exact keys under own prefix; preserve a filtered receipt.
4. After writer completes, launch reader and outsider Jobs. Reader must read
   both objects and fail write; outsider must receive permission denial on
   read. A reader parse/error/missing-file failure is not a valid denial test.
   Record UID/GID/groups, actual mount identity/options, image ID and results.
   No chmod/chown or object metadata changes are attempted.
5. Optionally stage the prepared denied-prefix PV/PVC/Job. It targets the new
   synthetic sibling `security-validation/s15/<run>-denied/`, outside the role
   policy, using the same reader role and pod authentication. Require a
   CSI/S3 AccessDenied explicitly attributable to that prefix before container
   start. A scheduling/token/network error is inconclusive. Any container start
   is failure, even if its command then exits nonzero. No unrelated tenant key
   or preexisting sentinel is queried.
6. Stop/delete only the run-owned Jobs/Pods by fresh UID checks; wait for node
   unpublish before deleting claims/PVs. Remove only the two recorded synthetic
   keys and their exact run-created version IDs (and only run-owned incomplete
   multipart uploads if any) using reviewed operator cleanup authority; the
   bucket is versioned, so deleting current keys alone leaves old versions and
   delete markers. Record at most the three expected writes: two roundtrip
   versions and one reader fixture. Unexpected keys/versions stop cleanup for
   review; never sweep a bucket. Probe roles deliberately have no DeleteObject.
   Verify exact run prefix has no current objects, versions, delete markers or
   multipart uploads. Remove only created role policies/roles/SAs and
   namespace after confirming ownership. No bucket deletion, global prefix
   sweep, wildcard namespace deletion or production-volume finalizer removal.
   Preserve sanitized receipts before removing objects.

## Production rollout and rollback gates

A passing synthetic CSI probe is necessary, not sufficient. The promoted6043
image predates6060 scratch/retry repairs: it is valid for the isolated command,
**not** the final refresh rollout candidate. Build/test/promote a new immutable
candidate containing6060 and reviewed source integration before production
rollout. Do not invoke the broad build/deploy workflow just to validate; it
updates latest and auto-deploys. Parser continuation6059 has separate acceptance.

Fresh snapshot all five ingestion consumers and the Zoekt readers, exact UIDs,
images, PVC/PV options, controller templates, resource versions and owners.
Current filtered ScaledJob `agent-context/ingestion-worker` uses the old
70c8d6... image and has no pod securityContext in the snapshot. Refresh and
retained workers resolve to rollback digest
`sha256:6e3800817dfe1ef8c4e6c97fee9f3b704a7888d8ad4fc2c4f282ac0e019795a2`.
Revalidate it remains pullable. Vuln-scan's old bf6cea... tag is missing from
ECR; no rollout of that consumer until a known compatible immutable rollback
exists. Synthesis is absent and no migration Job is retained: define creation
and migration lifecycle separately, never infer a migration did not run or
re-run it to test filesystem ownership.

Quiesce only approved ingestion writers using the installed controllers'
verified suspension mechanism and record original values. CronJob.suspend does
not suspend KEDA. Confirm no new worker Jobs and wait active writers terminal;
do not kill work or discard messages to speed rollout. Deploy reader group
compatibility first, then use fresh UID/resourceVersion guarded PV mountOptions
change and bounded consumer replacement. Existing mounts may retain old flags;
prove new mounts actually use the reviewed options. Never manually unmount
shared node paths or remove PV finalizers. Mixed old/new mounts are not full
acceptance. Preserve Zoekt availability expectations during its Recreate cycle.

Canary each existing consumer on the immutable candidate with authorized
synthetic workload and exact mounted-state read/write checks before resuming
its controller. Verify positive refresh/index/state, nonroot UID, protected
root, scratch bounds/cleanup and Zoekt read/search health; module tests alone
are not live proof. Synthesis/migration need explicit separate disposition.
No live tenant data probing or model/provider calls are implied by this plan.

Rollback is a concrete reviewed snapshot: original PV options (currently
allow-delete/allow-overwrite), each exact controller template/securityContext,
reader groups and pullable image digest, plus original controller pause state.
If a canary fails, stop new attempts, preserve receipts and restore only
changed owned fields with fresh guarded patches; re-create bounded affected
Pods so mount flags actually revert. Restoration of synthetic mount ownership
changes no S3 object owner/ACL and does not require data chmod/chown. Resume
writers only after old-image/old-mount health is verified. Never roll back to
missing tags, blindly overwrite concurrent changes or recreate absent Jobs.

## Current evidence limits

Source rendering/IAM scope tests and an owned disposable Docker POSIX fixture
exercise the actual writer/root-reader/outsider script successfully. That
fixture was prepared with local ownership/modes and removed afterwards; it
cannot establish CSI/IRSA/CNI/mounted-prefix behavior. Full live acceptance,
post-rollout health and rollback remain unexecuted. S15 remains open.
