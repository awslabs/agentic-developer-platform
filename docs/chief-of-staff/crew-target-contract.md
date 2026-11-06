# Crew target and executor generation contract

Registration creates a `provisioning` inventory record and never starts a
workload or grants execution approval. The owner is the canonical
`(tenant_id, user_id)` pair, not a pod name, endpoint, caller-supplied tag or
Dev Box reference. A partial database index permits only one non-deleted row
for that pair. When a deleted executor is replaced, the new row retains its
owner and receives the next generation. `dev_box_id` remains nullable until
the separate Dev Box schema can supply an owner-preserving foreign key.

The per-user DynamoDB record uses `PK=crew#<tenant_id>#<user_id>, SK=executor`.
Its `tenantId` and `ownerUserId` must match the key; `crewLease` contains
`tenant_id`, `user_id`, `target_kind`, `target_ref`, `generation`, `expires_at`
and `endpoint`. The Crew run binding carries the same owner, generation and
`target_kind`. Existing **per-session** chat leases are not part of this key
space and are not changed. The default allowed target is `eks-pod`; the
downstream tenant-policy integration must read `crew.allowed_targets` from
trusted tenant settings, defaulting to `['eks-pod']`. This contract does not
enable EC2 provisioning. A ready EKS inventory row records the pod UID only
after trusted admission has written a matching, unexpired per-user lease.

Endpoint resolution makes a strongly consistent lease read and compares the
lease owner, generation, target kind, target reference and expiry with the ready
inventory row. No inventory row or missing/mismatched/expired lease is a fence;
the authenticated gateway target route returns `409 lease_fenced`, never an
unowned legacy pod endpoint. An unavailable lease store is an error, not an
invitation to fall back to an unverified target.

## Publication and replacement ordering

The control database is the authoritative **publication fence**. `record_ready`
mirrors the trusted lease's generation and expiry into the inventory row.
The lease writer implemented by #7203 must update/fence that row **before**
changing or deleting the DynamoDB lease; no other writer may independently
advance its generation. An EC2 host lease is published once per machine
generation when the controller verifies the host, with the expiry of the box's
own lease (hours), and is ended by stop, replace or release; there is no
heartbeat-driven renewal of a verified host lease (#8223 simplification). A pod
lease that is renewed must update the mirrored expiry through the same
owner/generation guard; an unmirrored renewal fails closed when the control-store
expiry elapses. `end` first changes the control row to `ending`
and clears the mirrored lease generation and expiry. Only after that commit
may a worker clean up the old DynamoDB lease or pod. Do not treat DynamoDB
TTL as revocation.

For a checkpoint or any other generation-sensitive write in this control DB,
#7202 must open a transaction, call
`CrewExecutorInventory.lock_current_generation(session, binding, now=...)`,
write the pointer for that exact owner within **that same transaction**, then
commit. The conditional update locks the row until commit, verifies tenant,
user, target kind, ready state, current generation and unexpired mirrored lease,
and excludes a concurrent end/replacement. A separate lease read followed by
an unrelated pointer transaction does **not** provide this guarantee.
The inventory migration is independent of and precedes the checkpoint schema.

## Verified checkpoint handoff

The authenticated owner read `GET /v1/crew/checkpoints/selection` returns the
selected verified checkpoint ID and sequence, its source executor generation and
target kind, the current executor generation and target kind, and the Crew and
schema versions. Replacement registration carries the selected checkpoint
pointer from the previous owner generation within the control database
transaction; a failed or pending upload never replaces that pointer. An
incompatible or incomplete recorded receipt returns no selectable checkpoint.
The response does not expose S3 locations, version IDs, owner keys or grants.
The separate owner read `GET /v1/crew/checkpoints/coverage` identifies the last
verified capture time and does not promise durability for unsynced writes.

A `verified` selector describes objects accepted at publication time, **not** an
activated local home, a paused writer, permission to read S3 objects, or a
completed live recovery. #7209 and #7222 consume this identity when they
coordinate their respective executors; #7223 checks the repository handoff
contract and #7217 owns the approved live cross-executor recovery. Neither
owner read provisions EC2 nor substitutes for the restore helper's separately
verified authority.

Operator rollout applies the gateway migration before enabling new Crew target
admission. Live matching of a ready row and pod is a post-deployment operator
check, not proof supplied by the local fixtures.

## Checkpoint helper integration

Both capture and restore helpers use the dedicated gateway TLS listener for
the four checkpoint POST operations: `upload-grant`, `complete`, `restore-grant`
and `restore-release`. The listener does not admit owner-facing checkpoint
administration or bypass helper token verification.

Helper pod producers must put full 64-character hexadecimal `adp.io/session-hash`
and `adp.io/checkpoint-hash` values in **annotations**, not labels (Kubernetes
label values allow at most 63 characters). The owner key and helper-class selector
remain labels. The gateway verifies the checkpoint annotation and compares the
full digest with the requested checkpoint ID; it does not use a truncated hash.

Helpers use `ClusterFirst` DNS without custom DNS configuration. Their network
policies allow TCP/UDP port 53 only to `k8s-app: kube-dns` pods in `kube-system`;
the operator must verify that the target's cluster DNS uses this path. S3 names
must resolve to the approved interface endpoint IP permitted by the separate
TCP/443 policy. DNS access does not grant general HTTPS egress, and helpers retain
TLS hostname and certificate verification. Applying these policies is not a live
DNS or S3 connectivity check.

The managed root `token_signing.key` link created by dashboard startup is excluded
without reading its target. Other unexpected symlinks still fail capture.

## Checkpoint retention configuration

An authorized operator must set `ADP_CREW_CHECKPOINT_RETENTION_SECONDS` to a
positive integer in private gateway configuration before new checkpoint
admission and publication. There is no default duration. Missing configuration
blocks new admission/publication but leaves reads and previously eligible cleanup
available. Invalid configuration returns an unavailable error.

Successful verification records the expiry as verification time plus the configured
duration in the same transaction that publishes the latest pointer. Retrying an
accepted completion or changing the setting does not rewrite an existing expiry.
Deletion still requires expiry, a newer selected checkpoint and no active restore
pin. Existing records without an expiry remain protected; configuration does not
retroactively authorize their deletion. #7217's operator owns target-specific
retention selection and live validation.
