# Crew working volumes

Persistent Crew uses a separate EBS-backed PVC, not the sandbox scratch volume. The
reviewed `chat-crew-ebs-storage.yaml` class uses `WaitForFirstConsumer` so the
first Crew pod selects the volume's availability zone; `Retain` keeps the EBS
volume when the claim is removed. The 20Gi claim template in
`chat-crew-owner-pvc.yaml` is **not applied automatically**. An authorized owner
provisioner must resolve the authenticated organization/team/user identity,
produce its stable 52-character lowercase hexadecimal owner key, create exactly
one claim named `crew-home-<owner-key>`, and bind its lifecycle to that owner.
Do not derive the key from a message, session ID, pod label or caller-provided
string. The current supervisor notification has no authenticated owner key;
until the trusted owner binding is supplied, persistent pod construction refuses
to mount any PVC. Integrate that binding with the gateway run/owner work before
allowing persistent workloads.

The Crew pod mounts its own claim as `/tmp/workspace`; its bounded 2Gi scratch
remains a separate `emptyDir` at `/tmp`. The checkpoint helper may mount the same
claim read-only and must run on the owner Crew pod's node, because an EBS
`ReadWriteOnce` volume cannot be mounted for writing across nodes. A replacement
Crew pod using the retained claim must be scheduled in the volume's original
zone. The helper implementation must coordinate a consistent capture; a
read-only mount alone is not a snapshot. Restore to a new volume uses the
separate checkpoint protocol, not another writer to the same EBS claim.

`Retain` is a data-safety choice, **not** automatic cleanup: orphaned volumes
continue to incur storage charges until an authorized owner reconciles and
deletes them. Verify the EBS CSI driver, provisioner, actual reclaim status,
zone scheduling, retained data after pod deletion and volume-loss recovery on
the selected deployment target before qualifying the workload. This repository
does not provision a cluster or a volume merely for tests.
