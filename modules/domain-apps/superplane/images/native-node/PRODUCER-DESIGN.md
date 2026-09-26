# Native image producer

Supervisor-approved source design, 26 September 2026. Builds and cloud mutations
remain unauthorized until the selected account, target and budget are approved.

The original `build.py` is a prepared-input consumer. The producer removes that
prerequisite: it accepts published upstream artifacts, an explicit AWS NVIDIA root
snapshot and a separate ordinary Linux helper. Target Python is not required. No
target image boots before its automatic nodeadm units are masked offline. The
published snapshot never contains a validation instance's SSM or kubelet identity.

`producer.py` verifies a closed input plan, source revision, supplied artifact/tool
digests and account/image/snapshot identities. `upstream.py` assembles private
Python from a pinned relocatable CPython archive, materializing only validated
internal file links. It builds both nodeadm binaries from the runtime's exact source
commit and vendored modules in a pinned Go container with networking disabled. It
extracts pinned crictl and CNI artifacts; a CNI container is created for file copying
but never started. No human assembles a final root or supplies guessed closure hashes.

A pinned `amazon-ebssurrogate` helper attaches an encrypted clone of the target
snapshot at API device `/dev/sdf`. The target mapping has delete-on-termination and
is the sole AMI root mapping. The helper's root is excluded. Target HVM, x86_64,
boot mode and ENA properties are copied explicitly. No top-level encryption/copy
operation creates an unnecessary intermediate image. Source schema evidence is
HashiCorp Amazon plugin v1.8.2, commit
`3896533621ce21e5d8277b7e86e9bf02b577045a`; a real pinned binary still needs validation.

`offline_root.py` resolves the target volume from this helper's authenticated IMDS
identity and EC2 attachment/snapshot/build-tag/KMS facts. Linux block identity must
match that EBS volume; an API device name is never assumed to be a Nitro NVMe path.
It probes only that volume's XFS/ext4 partitions read-only and requires one target
root. Mounts use a fixed private directory. Path resolution remains inside the
mounted root even for absolute target symlinks. Original enrollment indicators are
refused; no enrollment state is erased, no target init is started and no helper
system unit is modified.

The offline installer adds generated artifacts and exact maintained source,
materializes required artifact/loader aliases as regular files/directories while
preserving their contents, and masks target nodeadm config/run units. It inventories
ELF interpreters, DT_NEEDED libraries and RPATH/RUNPATH against the target, together
with complete private Python/CNI trees and explicitly reviewed runtime/dlopen/config
dependencies. It never runs ldd on a target binary. Alias transformations, package
and input provenance are retained for review. Unresolved dependencies, cyclic links,
unsupported filesystems/layouts or manifest limits refuse publication rather than
weakening the node runtime.

Actual closure hashes and canonical manifest/descriptor are generated from the
completed root. The existing node runner verifies the installation inside a bounded
chroot using the newly installed private interpreter, without calling bootstrap,
IMDS, SSM, Kubernetes or GPU entry points. This is file/import validation; driver,
CNI, plugin and GPU compatibility remain pending a governed disposable-instance run.
Any use of chroot must retain the no-target-init boundary.

Build state is durable before mutation and records uncertain cleanup on interruption.
Packer cleanup remains enabled. Reconciliation checks the unique AMI name as well
as build tags because RegisterImage can succeed before tags are applied. Snapshot
sets must equal the intended AMI root snapshot exactly; extra intermediate snapshots
remain unresolved. Original helper/volume/image/snapshot identities and generated
provenance are retained for operator reconciliation. Automatic profile promotion,
region copying, image sharing and arbitrary resource deletion are excluded.

Tool/plugin/ABI compatibility and the selected candidate's root layout are unverified
until a real build. The source recipe plus generated hashes is not runtime approval.
Only a reviewed artifact and actual Node/GPU/workload/cleanup evidence can complete
the live #5927 acceptance.

## Verified upstream source and source transport

The pinned nodeadm source is `awslabs/amazon-eks-ami` commit
`ffc658f85bb8732e130898850802b100aee507e9`. Its `nodeadm/Makefile` SHA256 is
`59e7ff23d97bd5826ef4e7d1d281cf18e5b6764ee201b28b6257d9d9375ddbb7`;
`nodeadm/vendor/modules.txt` SHA256 is
`122ca3da0090962828234c1b00a724d493c4947f5f757abdd4bb64cbedef8686`.
The real release target builds `./cmd/...` into `_bin`; the producer retains
`nodeadm` and `nodeadm-internal`. Upstream enables nodeadm-config before cloud-init,
which rules out relying on later cloud-init masking for the never-started boundary.

Source transport is an extracted Git archive, not a checkout with fabricated Git
metadata. A trusted dispatcher measures every tracked blob, mode and full tree,
then supplies an external attestation whose SHA256 is approved in the plan. The
builder verifies the exact extracted inventory and reconstructed tree. Dispatcher
source and attestation delivery must remain immutable to the build role. The new
app-owned dedicated lane is separate work; the executor OCI project and its shared
boundary explicitly do not authorize the required helper PassRole/EC2 workflow.

## Version-2 CNI source extension

Source-approved follow-up: the full AWS CNI installer output can depend on two
independently pinned images. Version 2 replaces the single CNI input assumption with
a bounded `cni_sources` list (one or two image/file maps, 1–64 entries each).
Destinations are globally unique; version-2 source paths are canonical absolute
paths. Archived version-1 input validation remains unchanged and normalization never
mutates the approved plan. Each source reuses the non-starting create/cp/remove
flow and produces per-source provenance before the merged CNI staging tree reaches
the existing offline installer/verifier. Real cluster-image equality, installer
behavior and full postjoin tree stability are still acceptance evidence.
