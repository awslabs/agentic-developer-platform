# Adapter image publication module

The tools release bootstrap calls this Terraform child module to provision a
separate immutable ECR repository and CodeBuild qualifier for each environment.
It inherits the caller's AWS provider and verifies the selected account/region.
It creates no runtime role, namespace permissions, provider secrets or deployment
trigger. The release stage and its caller permissions are integrated separately.

Inputs are `aws_account_id`, `aws_region`, `environment` (`dev` or `prod`), and
the existing `state_bucket`. Supply `source_kms_key_arn` only when source archives
use SSE-KMS. The optional decrypt/data-key permissions are restricted to that key
through S3.

The caller uploads an exact reviewed Git archive beneath the returned
`source_prefix`, starts `build_project_name` with that S3 location and sets
`ADP_SOURCE_SHA` to the full source revision. Do not override the build role or
buildspec. The build obtains its account and environment from Terraform, checks
the AWS caller identity, builds and self-checks the reviewed source without ECR
access. Its role explicitly denies registry mutations. The protected release
driver reads the exact image archive version and checksum, then invokes the
daemonless ECR uploader with a fresh native lease check before each request.
It does not require Docker on the Actions runner. Grouped production dispatch,
IAM access for that runner and durable in-flight settlement are not wired; do
not enable the stage based on this local driver. The planned tag is
`adp-tools-<environment>-adapters:<source-sha>`; there are no mutable aliases.
Qualification has no registry credentials or calls. It verifies the source label
and isolated imports, saves the image under a 1 GiB limit, and puts it once into
its own project prefix in the private versioned bucket. The log reports the
object key, version, SHA-256 and checked image config digest; the deployment
reader checks the exact version and S3 checksum independently. The temporary
Docker configuration is removed on exit. This archive is not a published image
or accepted runtime evidence.

The bounded `ADP_ADAPTER_PUBLICATION_V1` CodeBuild log reports source revision
and qualification success, not an image digest. The driver independently reads
the ECR digest after a successful guarded upload and deploys by digest. This check
does not replace the standalone-image CI evidence, runtime verification or live
connector acceptance. Published tagged images are retained for rollback.

Run `terraform init -backend=false` and `terraform test` in this directory.
The mocked-provider tests verify the rendered permissions, trust, repository
retention, environment isolation and rejection of a mismatched provider target.
