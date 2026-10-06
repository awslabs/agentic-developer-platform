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

## Maintained installer entrypoint

Use the same domain entrypoint for explicit paid-build preparation. This stage
accepts an unresolved paid image in the release lock, because the repository and
builder must exist before that image can be built. It does not deploy a runtime,
start a build, register an identity, or modify dispatcher permissions.

```bash
modules/domain-apps/superplane/deploy.sh \
  --environment /private/environment.yaml --release-lock /private/release.yaml \
  --output /private/paid-build-preparation --prepare-paid-build

modules/domain-apps/superplane/deploy.sh \
  --environment /private/environment.yaml --release-lock /private/release.yaml \
  --output /private/paid-build-preparation --prepare-paid-build --resume --preflight

# Inspect both plan.json documents, backend keys, and the returned plan hash first.
modules/domain-apps/superplane/deploy.sh \
  --environment /private/environment.yaml --release-lock /private/release.yaml \
  --output /private/paid-build-preparation --prepare-paid-build --resume \
  --execute --approved-plan-sha256 <reviewed-plan-hash>
```

The private environment uses the same account, region, environment, management
cluster, namespaces, origin, database schema and secret references as the domain
installation. Live preparation requires `deployment_identity` from the selected
connection, including its independently resolved exact IAM role ARN and RoleId.
The release lock needs the exact clean checkout's `source_revision` and the
`superplane-paid-worker.ecr_repository` entry in either `pending_images` or
`image_sources`. No resolved paid image, database connection or workspace is
required for this infrastructure stage. Database fields are configuration
references only; no database or Kubernetes operation runs.

Preparation uses the existing domain backend only for a targeted **additive**
plan of the paid ECR repository, its lifecycle policy and inventory guard. It
rejects changes to other resources, updates, replacement, deletion or imports.
This targeted stage does not claim that the full domain root is reconciled:
the subsequent ordinary installer must still inspect its complete release plan.
The five builder resources use the distinct
`<environment>/modules/superplane/paid-worker-build/terraform.tfstate` backend.
Both plans retain the canonical DynamoDB state lock and run under the domain's
conditional installation lock. The installer snapshots all local Terraform
module/manifest inputs into the private output directory and binds the saved
plans, source snapshots and selected environment to the approved hash.

Existing objects outside the selected state are refused before apply, including
log groups and inline policies whose provider could otherwise overwrite them.
Existing image lanes remain with their current owners: ordinary installation
defaults to `manage_image_builds=false`. An environment that already enrolled
those lanes uses the explicit preservation intent described below; the
installer refuses their deletion or implicit adoption.
Never enable the switch as a shortcut for importing existing platform resources.

A lost or failed apply retains its receipt and installation lock. First establish
that the recorded preparation process and Terraform child processes have stopped,
then use this same mode with `--resume --recover-lock --confirm-stopped <run-id>`.
Recovery releases only the matching conditional lock and requires a fresh
preflight, reconciliation of existing state/resources, and a newly reviewed plan.
It never deletes resources or automatically retries an uncertain apply. A
`build-infrastructure-prepared` receipt means only these infrastructure plans
completed; image acceptance and native lifecycle activation remain separate.

For an installation whose **domain state already owns all four existing image
lanes**, set `image_build_ownership: preserve-domain` in its private environment.
The installer then keeps `manage_image_builds=true`, but requires the exact four
projects, roles and inline policies in the plan's refreshed prior state with
matching account/region identities and no-op actions. It refuses missing/partial
ownership, creation, import, updates or drift of these resources. Their unchanged
records remain in the approved plan hash; only those verified records bypass the
generic runtime resource-type guard. Configuration changes to the builders or an
ownership transfer still require their separate reviewed procedure. Omitted or
`external` intent keeps the current external-owner default. A direct Terraform
caller with already enrolled state must continue passing
`manage_image_builds=true` explicitly; the root's default does not infer ownership.
