# Dedicated native AMI build lane

Supervisor-approved source design, including the refinements below. The lane is
required to deliver the offline producer; the existing executor OCI project and
shared permissions boundary remain unchanged. All maintained Terraform, dispatcher,
policies, buildspec and documentation belong under `modules/domain-apps/superplane/`.
Deployment/builds require the selected account, region, existing network and budget
scope to be approved separately from this source design.

## Resources and inputs

An app-owned Terraform module creates one dedicated CodeBuild project, service role
and boundary, helper EC2 role/profile, private input/output buckets with public
access blocked and KMS encryption, and log group with explicit retention. Existing
VPC, private subnets, CodeBuild/helper security groups and selected KMS key are
explicit inputs; no VPC, NAT gateway, EKS cluster or account bootstrap is implicit.
Project VPC attachment uses the approved subnets/security group. The helper group
permits TCP/22 only from the project group. Egress must support approved artifact
registries and AWS EC2/STS/KMS/S3 APIs through the installation's private endpoints
or approved outbound route. The helper receives no public address.

Project configuration pins the source buildspec path and reviewed build environment
image. Docker is needed only for pinned upstream compilation/extraction; privileged
CodeBuild execution must be enabled explicitly and considered part of this dedicated
trust boundary. Python, AWS CLI, Docker and trusted Packer/plugin executables are
provided by reviewed inputs/environment; helper tooling includes readelf, ldconfig,
lsblk, mount, sync, sudo and AWS CLI. These are verified prerequisites, not assumed
features of arbitrary AMIs. Build timeout and project concurrency (one) bound active
work; they are not monetary spend guarantees.

## Trusted dispatch and immutable inputs

A separate trusted dispatcher obtains an exact reviewed commit and verifies clean
source. It runs `source_provenance.py` with Python bytecode disabled, creates the
external whole-tree attestation, and archives that same revision. The plan includes
the attestation digest, exact maintained wrapper hashes, upstream/tool digests,
account/region, helper/target image identities, existing network/profile/key,
runtime bounds and budget approval reference. Dispatcher publishes a unique input
prefix and exact S3 object version IDs, then starts the dedicated project with those
identities. Versioned source/input bucket writes belong only to dispatch authority;
build and helper roles cannot write source, plan or attestation.

Before execution the project fetches exact versions into separate source/input
locations, verifies the archive digest from the dispatch envelope, extracts source
with traversal/symlink-parent protection, and verifies the full attestation before
running product build commands. Python runs with `-B`. Source archive creation must
refuse export-ignore/export-subst attributes that would alter attested bytes, or
fail by comparing its extracted tree during dispatch. Never fabricate `.git`.

Untrusted PR code does not receive native cloud-build authority. Remote fixture CI
can exercise the producer separately; a reviewed merged revision and independently
approved plan are required for this lane. The dispatcher controls project name,
buildspec and service role; caller-supplied overrides cannot switch them. The service
role cannot StartBuild or modify CodeBuild, IAM, bucket policies, project configuration,
source objects or Terraform state.

## IAM separation

The CodeBuild role/boundary permits its own logs; exact versioned native input reads;
write-only build evidence under the dedicated output prefix; required private VPC
ENI operations constrained to approved subnets/security groups; regional readonly
EC2 discovery and STS identity; and the producer's bounded Packer workflow. The
helper profile can be passed only to EC2 via its exact role ARN. No role assumption,
EKS access, SSM registration/SendCommand, cluster credentials or workspace authority
is available to either role.

The helper needs regional DescribeVolumes and STS identity only, plus its EC2
metadata identity. Packer uploads all tools/source; helper S3 writes are unnecessary.
The helper has no AMI/snapshot mutation or cleanup authority.

Packer mutations require a reviewed action/resource matrix from the pinned plugin:
RunInstances with approved helper image, instance profile, subnets/security groups,
instance types and encrypted target mappings; temporary key creation/deletion;
CreateVolume/AttachVolume/DetachVolume/DeleteVolume; instance stop/terminate;
CreateSnapshot/DeleteSnapshot; RegisterImage/DeregisterImage and build-resource
CreateTags. Actions supporting resource/request tags must require the unique native
build tag and source revision, restrict existing resources by those tags, and deny
shared infrastructure changes. KMS grants/encrypt/data-key use are limited to the
selected key and required EC2 service context. No AMI sharing or arbitrary region
copy API is granted. The implementation must account for actions/resources lacking
condition-key support instead of emitting conditions AWS ignores.

## RegisterImage gap and reconciliation authority

Pinned Packer RegisterImage does not tag the image atomically. A tags-only policy
cannot both authorize registration and safely recover every failure before tagging.
This gap is explicit: image registration may need bounded regional/account wildcard
resource permission, and successful name lookup is evidence, not deletion authority.
A unique build name, persisted before Packer launch, owned image identity and original
target-volume/snapshot binding form the reconciliation record. The build receipt
must retain any ambiguous handle and remain failed/unknown.

Routine cleanup of tagged helper instances/volumes/keypairs follows Packer's bounded
cleanup path. Destructive AMI/snapshot cleanup for an untagged registration is not
inferred from a name or granted account-wide to the build role. A separate operator
reconciler, invoked under approved installation authority after reviewing original
snapshot/volume/image evidence, can delete the exact observed handles. If Packer's
normal error cleanup demands broader image permission, the implementation must
surface and resolve that concrete incompatibility before deployment; it must not
silently broaden the boundary. EventBridge/CloudTrail evidence may aid identification
but cannot make an otherwise ambiguous resource safe to delete.

## Artifacts, interruptions and acceptance

Private output objects include dispatch envelope/plan/source attestation, upstream
provenance, base metadata, recipe hash, Packer log/manifest, durable state, root
evidence, descriptor, result and cleanup inventory. Upload runs in a finalization
path even after producer failure. S3 versioning and configured retention protect
review evidence; no source/input write permission is shared with artifact writers.
Native AMIs and snapshots are intentionally retained as explicit cost obligations
until acceptance or reviewed cleanup. Output lifecycle must not imply AMI deletion.

Producer timeout interrupts Packer, allows bounded cleanup and inventories actual
resources. A killed CodeBuild host can prevent its own finalization, so the dispatch
receipt records build identity before launch and an operator reconciliation command
uses it to perform readonly inventory without relying on completed output. Missing
inventory is unknown, never proof of absence. No workload profile promotion occurs
in this lane. Actual private SSM/EKS networking, node join, GPU workload and recovery
acceptance remain subsequent governed operations on the produced exact artifact.

## Implementation validation

Review Terraform formatting/schema and IAM action/condition support remotely;
exercise source packaging, exact version retrieval, archive tampering, failed build
artifact retention and lost-worker reconciliation in fixtures. Inspect generated
plans for exact helper PassRole, no shared role changes, no source writes and no
public network/artifact access. After source approval and explicit account/budget
approval, deploy the dedicated lane, run one bounded image build and reconcile every
created resource before any promotion. Source validation alone cannot establish
that AWS IAM conditions, pinned Packer or helper ABI work in the target account.

## Approved implementation refinements

Reuse the existing shared CodeBuild CLI/action; the app owns input verification,
receipts and its invocation wrapper. Alternate-module tests run from the existing
control-plane Terraform suite without shared workflow edits. Pinned-plugin request
facts and official AWS action/resource/key support are recorded in
`infra/native-image/IAM-MATRIX.md`. Atomically tagged native resources bind the
authenticated STS UserId to IAM `aws:userid`. AMI tags are empty, which the pinned
plugin supports without implicit defaults; no image CreateTags or DeregisterImage
is granted. Caller-tagged source snapshots constrain RegisterImage. Success retains
the untagged image with exact provenance; post-register failure explicitly retains
a cleanup obligation for separately authorized operator review. This supersedes the
earlier tentative post-create tagging/cleanup discussion above. Source receipt
publication precedes any helper launch. CodeBuild ENIs and artifact writes retain
the documented subnet/lane scope rather than claiming per-build IAM isolation.
