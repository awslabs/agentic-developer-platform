# Dedicated native image lane

This app-owned Terraform root supplies the dedicated CodeBuild project, readonly
helper role/profile, scoped build role/boundary, private versioned input/evidence
buckets and logs for the offline #5927 producer. It changes no shared executor role
or workflow. See [IAM-MATRIX.md](IAM-MATRIX.md) for verified permission limits and
[the approved lane design](../../images/native-node/LANE-DESIGN.md).

Supply the complete `lane` object from `variables.tf`: exact account/region,
dedicated existing private build subnets/SG, helper subnet/SG/image/type, all reviewed
source snapshots used by the helper/target launch mappings, existing KMS key,
private bucket names, digest-pinned CodeBuild environment, bounded runtime and
retention, and existing trusted dispatcher identity. Terraform has no account,
network or backend defaults; installation must supply isolated state and approved
credentials. The environment must contain Python, AWS CLI and Docker. Its image
registry must authorize the CodeBuild pull identity. The helper must provide the
offline inspection/mount tools documented in the producer README.

Network prerequisites are explicit: helper SSH ingress from only the build SG;
private EKS/SSM enrollment authority is unnecessary for an image helper. Build/helper
routes must reach reviewed registries and required AWS APIs through approved private
endpoints or outbound routes. This module does not create shared VPC routes, NAT,
security-group rules or KMS key policies. Existing KMS policy and any dispatcher
permissions boundary must allow the exact generated grants. The output
`dispatcher_policy_arn` must be reviewed and attached under installation authority;
this module does not mutate the existing dispatcher role.

## Invoking the lane

After source review and separately approved installation/account/budget scope, use
the app-owned entry point `images/native-node/lane/dispatch.py`. It invokes the
existing `platform/scripts/codebuild-run.sh` in release-archive mode. No new engine
or shared workflow is required by the CLI. The shared composite action uses a
selected-directory ZIP, so this lane uses the CLI release mode to guarantee Git
archive source transport without following local filesystem symlinks.

Run Python with `-B` and provide all flags shown by the source parser: `--checkout`,
`--account-id`, `--region`, `--project`, `--dispatcher-role`, `--plan`,
`--plan-sha256`, `--inputs`, `--packer`, `--amazon-plugin`, `--output`, `--bucket`,
`--output-bucket` and a unique `--dispatch-id`. All paths identify reviewed actual
inputs; no executable placeholder plan is provided. The checkout must be clean at
the plan revision, which must appear in the locally fetched reviewed main history.
The current identity must be the exact selected dispatcher role session.

The dispatcher verifies the plan digest and whole-tree attestation, verifies the
actual archive by extraction, stages pinned source/tool/upstream inputs with exact
S3 version IDs, and stores a START_PENDING receipt before invoking the shared CLI.
The build downloads immutable versions, verifies all hashes and the Git tree, checks
its fixed project constraints, and runs the producer. No release lock changes or
profile promotion occurs. Digest approvals and installation authorization are
external review gates, not inferred from the presence of files.

## Interruption, retention and reconciliation

The shared CLI does not provide an idempotency token or external receipt itself;
the app wrapper records its emitted build ID immediately. A lost StartBuild reply
before that line is **unknown**, never automatically retried. Locate the actual
build using CodeBuild project history and its exact `NATIVE_ENVELOPE_B64` environment
value; the dedicated dispatcher policy includes readonly history access. Inspect
versioned `dispatch/<dispatch-id>/child.json` and local dispatcher log. Stopping
local polling does not stop or clean the cloud build.

Before any helper launch, the producer publishes `builds/<CodeBuild UUID>/native-start.json`
with original native build/account/region identity. If that upload fails, it refuses
to launch. This survives a killed CodeBuild worker that cannot execute finalization.
Normal finalization uploads logs, state and provenance even after a failed producer;
a failed upload remains an error. Recover the approved plan and native-start receipt,
then invoke `images/native-node/lane/reconcile.py --plan ... --native-start ... --output ...`
under authorized readonly identity. Missing inventory remains unknown. The reconciler
never deletes resources or promotes an image.

Successful images/snapshots are retained cost obligations. A post-registration
failure may retain an untagged AMI because the build role intentionally has no image
tagging/deletion permission. Review the exact image/snapshot/volume evidence before
separately authorized operator cleanup. Bucket lifecycle retention removes evidence
only on the configured schedule; it does not delete AMIs/snapshots or establish their
absence. Use a retention period sufficient for outstanding cleanup obligations.

## Verification

`infra/control-plane/tests/native_image_lane.tftest.hcl` runs this sibling root as
an alternate module in the existing credential-free Terraform CI job. The normal
control-plane test count guard includes it; no shared YAML edits are required.
Executor fixtures cover source archive traversal/symlink/mode boundaries, immutable
input drift and the no-AMI-tags caller-bound recipe. Local verification is limited to
Ruff and digest-verified Terraform formatting. Terraform init/validate/mock plans,
transport runtime fixtures and actual cloud/ABI/GPU acceptance run remotely and must
be reported separately. Source tests cannot establish effective AWS policy behavior.
