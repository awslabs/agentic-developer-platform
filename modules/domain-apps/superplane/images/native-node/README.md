# Preparing native GPU node images

This source-only recipe prepares the fixed #5927 node command capability from an
explicitly reviewed base, tool binaries and runtime bundle. It supplies no AMI ID,
default version lock or fabricated digest. It does not establish that an arbitrary
AWS EKS NVIDIA AMI is compatible. Building an image costs money and creates AWS
resources; use the selected account's approved installation/build authority and
budget before invoking `build.py`. A source merge does not authorize execution.

The only maintained source is under `modules/domain-apps/superplane/`. The recipe
reuses Packer's Amazon EBS builder and existing CodeBuild project/source delivery
patterns; it creates no project, role, cluster or SSM Command document. It does not
change the live release lock or workspace profile. The OCI `releases/build-image.sh`
lane cannot produce this AMI.

## Inputs

Supply a canonical JSON lock accepted by `native_image.validate_lock`. Every field
is required, and unknown fields refuse. Its exact shape is:

| Field | Required evidence |
| --- | --- |
| `version` | Integer 1. |
| `source_revision`, `source_files` | Full ADP commit; SHA256 for each of the two maintained launchers and three runner modules listed in `SOURCE_FILES`. A clean checkout of that exact commit is required. |
| `account_id`, `region` | Explicit build account and region; STS must agree before Packer executes. |
| `base` | Explicit `ami_id`, `owner_id`, `architecture: x86_64`. DescribeImages must confirm owner, available state, EBS root and HVM. Review base snapshot/package provenance independently. |
| `builder` | Explicit `instance_type`, private `subnet_id`, existing `security_group_id`, existing `instance_profile`, `ssh_username`, and a KMS key ARN in the selected account/region. |
| `tools` | `packer` and `amazon_plugin`, each with exact `version` and executable `sha256`. Supply already verified binaries; the recipe never downloads a latest plugin. |
| `bundle_sha256` | Hash of the supplied runtime overlay tar bytes. |
| `dependency_review_sha256` | Hash of the separately supplied review/SBOM document describing complete executable, loader, library, driver/kernel and configuration provenance and compatibility. A hash does not establish that a review occurred. |
| `runtime` | The actual `node_runner.MANIFEST_FIELDS` except `artifact_sha256`. It must pass the maintained runtime validator, including the fixed nodeadm commit and x86_64/AWS VPC CNI restrictions. |
| `closure` | `files` and `trees` maps of canonical absolute paths to reviewed SHA256 values, describing the expected final installation, not merely the downloaded overlay. Required files and both private runtime/CNI trees must be present. |
| `build_timeout_minutes` | Explicit bound from 10 to 120 minutes. Packer receives an interrupt on timeout and up to two further minutes for cleanup. |
| `budget_approval_reference` | Reference to the actual approved build scope/budget. This is evidence, not a budget API or a guarantee that AWS billing stops at a monetary ceiling. |

Use `native_image.py validate --lock <reviewed-lock>` for source/input validation
without AWS. Exact keys and path/version constraints live in the validator. The
test-only fixture lock in `executor/tests/test_native_image_recipe.py` is not a
publishable example and must never become an execution profile.

The candidate base must already contain an independently reviewed provisioning
interpreter at `/usr/bin/python3.12`, sudo and the required OS/native prerequisites.
Provisioning does not install packages or fetch dependencies. It executes only on
an isolated builder, never on an existing allocated GPU node. A stock AL2023 image
with only another Python version is not an accepted prepared base. The base review
must include this provisioner interpreter and the selected pinned nodeadm commit's
compatibility with kubelet, containerd, NVIDIA driver/runtime, CNI and cluster minor.
SkyPilot version compatibility comes from the actual locked OCI digest and its
package/build metadata, not a possibly stale README version string.

The input tar contains regular files only, with canonical relative names such as
`opt/superplane/node-runtime/bin/python3`; no directory entries, symlinks, hardlinks,
devices, duplicate paths, traversal or group/other writes are accepted. Entries
must belong to the reviewed closure. Do not include the maintained runner modules,
launchers or manifest: the recipe installs those from source or generates them.
This bundle supplies the private Python standard library/extensions and native/CNI
artifacts or dependencies missing from the reviewed base. An ordinary venv is
insufficient. Absolute manifest paths and all ancestors must be root-owned regular
files/directories with no symlinks or group/other writes. Existing incompatible OS
symlink layouts refuse; the recipe does not rewrite loader/OS directory layouts.

The expected tree hashes must include the exact maintained modules added during
preparation. Use the runtime's canonical relative-file/hash mapping, excluding only
`/opt/superplane/node-runtime/manifest.json`. System binaries, their real ELF loader
and dependency paths, private Python extensions, driver/kernel modules and runtime
configuration require an independent completeness review. The generator verifies
every reviewed file/tree hash; its required-file minimum is not an automatic ELF
dependency resolver or proof of completeness. Kernel/config artifacts outside the
runtime manifest's allowed closure paths remain pinned base/SBOM evidence and need
live compatibility verification.

## Builder and enrollment boundaries

The recipe refuses an original kubelet identity/config, upstream nodeadm start/cache,
ambient NodeConfig, an existing durable application bootstrap latch or per-instance
SSM registration/history. It never deletes these to make a builder pass. Active or
ambiguous native bootstrap refuses before changes. It then masks both automatic
nodeadm units and calls the actual maintained exclusivity guard to check masked,
inactive units, queued competing jobs and running nodeadm processes. It checks again
before publication. Required native network boot hooks remain intact.

This permits a candidate that demonstrably remained never-enrolled during its
builder boot. It does not mandate offline root-volume customization. If the chosen
base starts nodeadm or registers SSM before preparation, choose a reviewed base with
those builder-time behaviors prevented; do not erase the resulting state. The
builder profile/network must not permit enrollment or bake a native SSM identity.
The produced AMI still needs native SSM Agent startup/permissions on the later real
instance. No registered identity, secrets, cluster credentials or per-instance latch
belong in an AMI. A failed guard leaves the candidate unpublishable.

## Remote build and retained evidence

Use `releases/buildspecs/native-node.yml` in an existing approved build project, with
a clean Git checkout (including its Git metadata), Python 3.12, AWS CLI and the
following explicit environment inputs: `NATIVE_IMAGE_LOCK`, `NATIVE_RUNTIME_BUNDLE`,
`NATIVE_DEPENDENCY_REVIEW`, `NATIVE_PACKER_BINARY`, `NATIVE_AMAZON_PLUGIN_BINARY`, and
`NATIVE_IMAGE_OUTPUT`. Inputs are pre-staged reviewed files; the output directory
must not already exist and must live outside the checkout. A source ZIP without Git
metadata is insufficient for the exact-source check; adjust approved source delivery
rather than bypassing it. Existing CodeBuild source packaging, artifact retention
and role scope must be reviewed for this AMI task, not assumed from an OCI lane.

The build host requires private SSH reachability to the selected builder subnet and
permission for Packer's temporary key pair/EC2/EBS operations, existing instance-profile
pass-role and the selected KMS key. Packer creates no public IP or new security group;
it cannot alter IAM or share/publicize the AMI. Effective IAM/SCP/boundary permissions
are installation prerequisites. Reviewed builder, snapshot and cleanup costs must
fit the approved budget independently of the configured time limit.

Packer prepares and verifies both installed wrapper manifests using the actual
`node_runner.verify_installation`, downloads the generated descriptor and removes
only its isolated upload directory before image creation. It records unique build
tags, encrypted image/snapshot provenance and a Packer manifest. Preparation and
final verification refuse incomplete closure or changed source. Live execution still
performs all its normal runtime/instance/grant checks.

`-on-error=cleanup` remains enabled. On a build timeout the process receives SIGINT
so Packer can clean its temporary instance, EBS volumes and key pair. A final
read-only, exact-build-tag inventory always records observed temporary resources,
produced images and retained snapshots. `result.json` is written only when no live
builder, volume or temporary key remains and exactly one owned available image is
found. An unavailable inventory, interrupted cleanup or leftover resource is a
failure, not a “clean” receipt. Preserve `state.json`, logs and
`cleanup-inventory.json`; the authorized operator reconciles exact remaining handles.
The script never deletes an AMI/snapshot or guesses absence after a lost reply.
Produced snapshots intentionally retained for review are explicitly recorded as cost
obligations. No automatic region copy or profile promotion occurs.

Retain the input lock, dependency review, source commit, tool/bundle hashes, base
DescribeImages evidence, Packer logs/manifest, generated descriptor/manifest hashes,
recipe hash, resulting AMI/snapshot identities and cleanup inventory in approved
private build artifact storage. Configure that storage in the existing build project;
this source does not invent a bucket or upload artifacts to a public service.

## Acceptance and promotion

`executor/tests/test_native_image_recipe.py` supplies remote, non-cloud checks using
the real manifest/descriptor validators. It covers hostile bundle members, unresolved
locks, enrollment refusal before mutation, generated manifest consumption and drift,
and refusal to report leftover temporary resources as clean. Do not run these against
an operator environment or interpret mocked artifact bytes as AMI compatibility.

After an authorized build, install the exact maintained native Command documents and
supplementary scoped permissions via `executor/node-command/INSTALLATION.md`. Verify
private SSM/ssmmessages, EKS API DNS/TLS, registry/CNI traffic, original Node join,
GPU/device-plugin provenance, a governed GPU workload and recovery/cleanup under the
same tenant/workspace/cluster scope. Bind actual image IDs and generated descriptor
bytes in a reviewed execution-profile update. `result.json` deliberately records
`live_gpu_acceptance: pending`: build success cannot close #5927 live acceptance.
