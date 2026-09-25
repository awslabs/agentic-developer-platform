# Installed native node transport

These are fixed SSM Command documents for the opt-in native bootstrap contract.
`bootstrap-document.json` is SuperplaneNativeBootstrapV1, version 1 (SHA256
`02e65ce48a37bd18b64550493fcbeb1cdbcab7f57e667469e60779989417e7a2`).
`probe-document.json` is SuperplaneNodeProbeV1, version 1 (SHA256
`41756cc3ea53232d1f22bff56ab5de5e07e9e22fd3a9b8ad6ca372723e0842ef`).
Preserve their exact bytes. No generic shell document or supplied executable is
accepted. The bootstrap command permits three preflight attempts inside 60 seconds,
then one nodeadm invocation with AWS_MAX_ATTEMPTS=3, all within the original
runtime deadline and the 290-second wrapper limit. The probe has a 40-second limit.

The image build must install the fixed launchers in `/opt/superplane/bin/`, copy
`node_runner.py`, `node_bootstrap_runner.py`, and `node_probe_runner.py` from the
executor package into `/opt/superplane/node-runtime/`, and install a complete
private Python standard-library/runtime closure there, including `bin/python3`.
Launchers clear the environment and run that interpreter with `-I -B -S`. The
runtime tree must contain regular files/directories (no symlinks), owned by root
and not writable by group/other. The system binaries, CNI binaries and all dynamic
loader/library dependencies must also be recorded by the reviewed image manifest.
All installed paths and their ancestors are checked. This intentionally requires
a prepared image; arbitrary AMIs cannot opt in by supplying a hash.

The canonical root-owned `/opt/superplane/node-runtime/manifest.json` has exactly
`version: 1`, `runtime`, `files`, and `trees`. `runtime` is the approved descriptor's
`runtime_manifest` without `artifact_sha256`. `files` maps absolute regular-file
paths to SHA256, including all required system executables and both launchers.
`trees` maps absolute directories to the SHA256 of canonical JSON mapping every
relative regular-file path to its SHA256, excluding only this manifest file.
It must include the entire private runtime and `/opt/cni/bin`. Record the complete
executable dependency closure; the independently reviewed build manifest establishes
its completeness. There are no sample hashes or publishable AMI IDs in this tree.

Compute `artifact_sha256` over the exact canonical manifest bytes. Compute each
wrapper hash over the corresponding installed runner module bytes. The external
approval binds those hashes, image ID, nodeadm commit, architecture and exact
runtime/CNI/GPU versions. Actual installed bytes are independently checked before
node-side effects. A matching EC2 tag alone is not execution provenance. A missing
manifest, mutable file, unsupported version or unresolved digest refuses execution.
This source implementation is not live image or GPU compatibility acceptance.

Before the first boot, the image must mask `nodeadm-config.service` and
`nodeadm-run.service` and ensure their processes/config caches are absent. Preserve
separately required native networking boot hooks. Prepare root-owned `/var/lib/superplane`
and `/etc/eks`; the wrapper creates only its fixed private state/config children.
It refuses an existing kubelet bootstrap identity/config, upstream run-start marker,
extra NodeConfig drop-ins, or a running nodeadm. It never stops competing services,
erases state, enrolls SSM, installs CNI, changes IAM, or repairs a foreign runtime.

`/var/lib/superplane/node-bootstrap/<instance-id>.started` is created exclusively,
fsynced before init, and never removed. Even an empty or interrupted latch blocks
re-entry. It survives reboot; `/run/nodeadm/init` is only an upstream start marker.
Successful nodeadm exit is a bootstrap execution receipt, not Node/GPU readiness.
The executor separately verifies readiness and packet/workload evidence.

A killed nodeadm may already have queued a systemd restart. Timeout, lost response,
or partial execution therefore retains uncertainty and original allocation exposure.
Only bounded preflight retry is supported; repeated full init requires separately
reviewed interruption/re-entry evidence for the same prepared image. The wrapper
emits bounded success receipts only, never NodeConfig, native output, IMDS tokens,
credentials, or provider-selected error text.
