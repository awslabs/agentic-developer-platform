# Protected registration delivery

`gitlab-infra-apply.yml` collects the six registration checks through
`scripts/registration_workflow.py`. It runs manually from reviewed main history
using the `adp-gitlab-deploy-<environment>` environment and its dedicated role.
The flow's accepted source, definition and contract digest must match that run.
Successful registration stages paused runners; manager activation, CI jobs and
final qualification remain separate deliveries.

## Prepare the reviewed inputs

Apply and reconcile prerequisite infrastructure in the original module backends
first. This workflow refuses resource creation, deletion, replacement and IAM
updates. Keep the existing infrastructure setup and teardown procedures for
those operations; a label cannot authorize them through registration.

Make a saved plan with Terraform **1.14.6** from the accepted checkout and exact
private variables. The backend is the default workspace of
`<environment>/modules/gitlab/terraform.tfstate` in that account's platform state
bucket, using the existing lock table. Never replay a prerequisite create plan.
The workflow applies that binary and then requires an ordinary full replan with
exit code zero. Refresh-only or targeted plans do not establish this check.

Retain the saved binary, private variables JSON and independently approved root
PEM as separate immutable versions in the existing private deployment evidence
bucket under `gitlab-registration/inputs/`. Review their SHA-256 digests and
versions. The plan also needs the SHA-256 of its `terraform show -json` document,
canonicalized using sorted keys and compact JSON separators.
The workflow restores the provider lock embedded in that reviewed binary and
initializes with `-lockfile=readonly`, so later provider releases cannot silently
change the selected dependencies.

Set `GITLAB_REGISTRATION_CONFIG_JSON` in the protected environment. Its schema is
validated before provider mutations:

| Field | Reviewed value |
| --- | --- |
| `schema_version` | `1` |
| `account_id`, `region`, `environment`, `source_revision`, `correlation` | Exact accepted deployment and operational release |
| `registrar_role_arn`, `exporter_role_arn` | Separate account-local roles; the first equals `ADP_DEPLOY_ROLE_ARN` |
| `instance_id`, `instance_url` | Existing GitLab instance and portal URL; URL must match SSM discovery |
| `targets` | `tenant_parent`, `product_subgroup`, `project`, `qualification_parent`, `approved_root_sha256`, `reviewed_plan_sha256` |
| `backend` | Exact original bucket, key, region, `encrypt: true`, existing `dynamodb_table` |
| `artifacts` | Entries `tfplan`, `variables`, `approved_root`, each with `bucket`, `key`, `version_id`, `sha256` |
| `product` | `tenant_id`, `group_id`, `product_id`, `product_group_id`, `project_id`, `tag_list`, `access_level`, `operation_id`, `token_secret_arn` |
| `qualification` | Separate owned tenant: `tenant_id`, `group_id`, `tag_list`, `access_level`, `operation_id`, `token_secret_arn` |
| `probe_operation_id` | Reserved UUID distinct from both persistent runner operations; retain it for retries |
| `ca` | `certificate_arn`, `runner_url`, `ca_secret_arn`, `ca_version` from the separately verified export |
| `admin_secret_arn`, `admin_user_id` | Existing service administrator; human access is unchanged |
| `secret_policy_sha256` | Exact admin and two token-secret ARNs mapped to independently reviewed canonical resource-policy digests |
| `instance_change_approval` | `null` unless the exact instance update has been explicitly authorized |

An authorized instance record contains `account_id`, `instance_id`,
`source_revision`, the **binary** `plan_sha256`, and a private `approval_ref`.
Configure it only after the existing deployment gate approves that exact plan.
A dispatch parameter cannot grant instance authority. Only `user_data` and
`user_data_base64` updates on that instance are permitted, and only with this
record. Updating launch metadata can stop/start the instance and does not itself
rerun cloud-init; review that operational effect separately.

Keep all actual values, policies, plans and receipts outside this public repo.
Retain the existing `GITLAB_INFRA_DELIVERY_*` protected contract variables for the
signed publisher. Do not change the six accepted check names.

## Operations and evidence

The workflow checks the real registrar identity, saved plan and zero-change
replan before registration. It verifies the three live secret resource policies
against reviewed digests, assumes the separate exporter, and rechecks the exact
exported CA version, chain and private HTTPS liveness. It does not create a new
CA version on every retry.

Both persistent group runners use `RunnerLifecycle` and existing SSM operation
records. An interrupted stage reconciles its original UUID; a different owner or
changed paused/tag/access policy fails. Tokens remain in Secrets Manager with
`AWSPENDING`. No manager is activated and no CI setting is changed.

Revocation uses a separate paused runner under the owned qualification group.
The collector first requires a successful token-authentication response, then
deletes that runner and requires HTTP 403 plus an absent admin/group readback.
Transport errors, redirects and HTTP 404 are not authentication rejection.
Cleanup searches only the exact probe marker and verified group/policy, including
after a lost create response. It never schedules a permanent token container for
deletion. A hard-killed run may leave a paused probe; retry with the same reserved
probe UUID to reconcile it. Do not broaden cleanup to other runners.

Six separate private, encrypted, versioned S3 objects bind the source, account,
region, correlation, workflow run and attempt to redacted observations. Exact
version readback is mandatory before the existing signed delivery publisher can
accept the receipt. Tokens, PEM and provider bodies never enter these objects
or Actions logs. Local private material is removed on success or failure.

The deployment role needs exact input-version reads, private evidence writes and
readbacks, bucket versioning/public-access-block reads, three
`secretsmanager:GetResourcePolicy` reads, and the existing registrar/exporter
delegation. Terraform additionally needs the original state/lock permissions and
read access for its managed resources. Any approved instance mutation requires
separately reviewed permissions. Prepare those policies from actual bindings;
do not grant broad service access or relax resource policies to pass a probe.

The guard also checks live CA, discovery, secret-policy and product/group
bindings before the reviewed instance stop/start. During collection the
protected registrar disables shared runners only on the verified product
project, then independently reads the setting back. It leaves CI activation
and user membership to their dedicated steps. A live project with shared
runners enabled therefore needs no manual Maintainer grant to its human user.
