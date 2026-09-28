# Paid-worker build infrastructure

This Superplane-owned Terraform root manages only the dedicated paid-worker
CodeBuild project, build role, narrow permissions boundary/policy and log group.
It is not called by platform deployment or automatically discovered by the
platform CodeBuild module. `enabled` defaults to `false`; the disabled plan has
no resources. Explicitly supply `account_id`, `region`, `environment` and, after
review, `enabled=true`. No target is inherited from historical test evidence.

Use a separate S3 backend key owned by Superplane, for example the selected
environment's `modules/superplane/paid-worker-build/terraform.tfstate`. Supply
the actual backend bucket, region and locking settings through a private backend
configuration. Do not initialize this root against platform or control-plane
state. Save and review the plan before applying; confirm the target account and
deployment authorization first. This root is optional even when Superplane is
installed. Do not invoke it from the ordinary platform deploy path.

The app control-plane root separately owns `adp-superplane-paid-worker` ECR.
Provision that repository before dispatch. The build root does not adopt or
create ECR repositories, edit shared IAM roles, provision compute/networking,
start builds, or deploy workers. The existing platform source bucket must already
exist with the release dispatcher's required versioning and evidence retention.
Encrypted source objects require compatible bucket/key authorization; this root
does not broaden KMS permissions or edit key policies. Public registry access and
the selected CodeBuild environment remain installation prerequisites.

The project uses the existing exact-commit release dispatcher and paid buildspec.
It has a 60-minute build timeout and explicit 480-minute queue timeout. Its role
can read only its own source prefix, push only to its app ECR repository, and
write only its log group. The same allow-list is its permissions boundary. Only
the exact selected-account CodeBuild project may assume it. Agent worker roles
are unchanged. Dispatcher grants remain separately reviewed under their owner.

Before apply, verify the project, role, boundary and log group names are absent
or already owned by this exact state. Do not overwrite or import unrelated
resources. If an earlier version was deployed using the platform manifest, use
the [ownership migration procedure](../BUILD-OWNERSHIP-MIGRATION.md) instead of
creating competing owners. Turning `enabled` off after creation plans deletion;
that is an explicit teardown requiring review, not a way to skip maintenance.

Validation runs remotely through the control-plane Terraform test suite's
`paid_worker_build.tftest.hcl`, using a mocked AWS provider. It checks disabled
zero-resource plans and enabled project, source, trust, timeout and IAM scope.
The release tests additionally check the actual platform manifest discovery
glob excludes this project. Fixture plans do not prove a live apply or build.
