# Native GPU node image producer

`producer.py` builds the #5927 native node runtime from a published NVIDIA EKS root
snapshot and pinned upstream artifacts. Packer boots a separate ordinary helper;
the target root never boots during preparation. The helper attaches an encrypted
clone, refuses existing enrollment state, masks automatic nodeadm startup offline,
installs the maintained wrappers and generated runtime, and produces hashes from
the completed filesystem. See [PRODUCER-DESIGN.md](PRODUCER-DESIGN.md).

This is source implementation pending remote fixture and actual AMI acceptance.
No published AMI, validated ABI combination, dedicated deployed build project or
live GPU acceptance is supplied here. Source merge does not authorize cloud builds.
`build.py` retains the earlier prepared-input consumer and shared lifecycle helpers;
it is not the new buildspec entry point.

## Reviewed inputs

The closed version-1 plan schema is enforced by `producer.validate_plan`:

| Field | Required value/evidence |
| --- | --- |
| `version` | Integer 1. |
| `source_revision`, `source_attestation_sha256`, `source_files` | Full Git revision, external source attestation SHA256, and maintained launcher/runner SHA256 map. |
| `account_id`, `region` | Explicit approved build scope; live STS must agree. |
| `helper` | Pinned ordinary helper `ami_id` and `owner_id`. |
| `target` | Published `ami_id`, `owner_id`, `snapshot_id`, `root_device_name`, explicit `boot_mode` and `ena_support`. |
| `builder` | Existing private subnet, security group, helper instance profile, SSH username, instance type, regional/account KMS ARN and target volume size. |
| `tools` | Packer and Amazon plugin versions and executable SHA256; plugin is fixed to 1.8.2. |
| `upstream` | Digest-verified Python, nodeadm source and crictl archives with explicit prefixes; digest-pinned Go and CNI images; explicit CNI source/destination file mapping. |
| `runtime` | Actual maintained runtime manifest fields except generated artifact hash. |
| `extra_runtime_files` | Explicit runtime/dlopen/config closure paths beyond mechanically discovered ELF dependencies. |
| `build_timeout_minutes`, `budget_approval_reference` | Approved bounded runtime (10–120 minutes) and actual budget authorization reference. |

The test fixture is not an execution plan. Upstream versions and all final ABI,
GPU driver/kernel, CNI and cluster compatibility still require real review/build
acceptance. ELF discovery cannot infer every dynamically constructed dlopen path
or all runtime configuration; the explicit extra paths and base provenance matter.

## Source archive delivery

On the trusted dispatcher, invoke `python3 -B images/native-node/source_provenance.py`
from the appropriate app path with `--checkout`, `--revision` and an `--output`
outside the checkout. It requires a clean exact Git checkout, measures each tracked
blob and mode, and reconstructs the Git tree. Approve the emitted attestation digest
in the plan. Deliver that exact revision's archive and attestation independently.
The builder verifies all extracted file bytes, executable/symlink modes, exact file
inventory and reconstructed tree without `.git`; run Python with `-B` to avoid cache
files contaminating the source inventory. Source, plan and attestation integrity
ultimately depend on the trusted dispatcher and immutable input IAM scopes.

## Remote execution and evidence

`releases/buildspecs/native-node.yml` expects `NATIVE_IMAGE_PLAN`,
`NATIVE_SOURCE_ATTESTATION`, `NATIVE_UPSTREAM_INPUTS`, `NATIVE_PACKER_BINARY`,
`NATIVE_AMAZON_PLUGIN_BINARY`, and `NATIVE_IMAGE_OUTPUT`. Inputs are staged reviewed
files; output must be new and outside source. The build host needs Python, AWS CLI,
Docker and private SSH reachability. The pinned helper needs the reviewed offline
inspection/mount tools and AWS API reachability. A dedicated native project/role/
network/artifact lane is required; the existing executor OCI role is insufficient
and must not be widened as a shortcut.

The producer validates archive hashes before extraction/build commands; expands
internal links with emitted-byte/file limits; compiles vendored nodeadm without
network access; and extracts CNI files from an unstarted pinned container. It binds
the target volume to source snapshot, encryption key, original build tags, helper
instance attachment and Linux EBS serial. Initial filesystem probes disable journal
replay. It never starts the target, erases enrollment state or executes target init.

The completed root is validated by the actual maintained installation verifier via
an isolated import-only private Python invocation. Generated manifest/descriptor,
upstream/source provenance, base metadata, state, Packer logs and cleanup inventory
are retained. Atomic state writes precede launch. Timeout interrupts Packer, permits
bounded cleanup and then reconciles resources. Unavailable inventory or leftover
resources cannot produce success. Registration-before-tagging is also reconciled by
unique owned AMI name. Final retained snapshots must match exactly and identify the
observed target volume. Images/snapshots remain explicit cost obligations; there is
no automatic promotion, cross-region copy or artifact deletion.

## Acceptance

Remote executor fixtures exercise real runner validators and hostile input/failure
boundaries. They do not prove a bootable image. After an authorized real build,
verify native SSM, private EKS API networking, original node join, GPU device plugin,
a governed workload and recovery under the tenant/workspace/cluster scope. Promote
only the exact resulting AMI and descriptor after review. `result.json` deliberately
keeps live GPU acceptance pending.
