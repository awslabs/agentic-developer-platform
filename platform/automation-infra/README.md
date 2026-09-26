# Trusted automation cutover (#5674)

This state separates reviewed infrastructure and publishing workflows from
repository jobs. It is a code/configuration change until an operator performs
the rollout. Do not run an apply, change a live role, rotate secrets, or change
engine transport as part of a PR review.

## Permission inventory

| Caller | Required operations | Deliberately excluded |
|---|---|---|
| Ordinary repository runner | Existing model/gateway transport; ECR pull; own logs; upload PR source and start/poll the existing gateway build project with its exact nonpublishing PR role | Lambda mutation/invocation, publishing builds, ECR push, tenant objects/state, IAM mutation, role assumption, EKS administration, KMS policies/grants, SSM |
| Gateway PR smoke build | Read its own source archive; write its own build logs; local Docker build/run | ECR publication, secrets, Terraform state, other project archives, deployment |
| Publishing build dispatcher | Start/poll the exact `build_project_names`; upload their source archives; inspect published image digests | Project mutation, changing service roles, IAM, Kubernetes deployment, secrets |
| On-demand scan dispatcher | Existing Security Agent space and exact service role; nonpublishing Grype/Syft projects; scan source/evidence prefixes | Deployment, tenant/state reads, secrets, publishing builds |
| Public-rule ingestion | Read benign canaries and publish only public YARA rule versions under a separate protected rules identity, away from deployment nodes | Other bucket objects, builds, deployment, tenant data and secrets |
| Each CodeBuild project | Its own source prefix, log group and declared output repositories/artifacts | Sibling projects, identity mutation, service control, secrets/state; its service trust pins the exact project ARN |
| Trusted deployment | Infrastructure APIs, exact admitted workload roles and executable targets, immutable operator-owned ceilings, explicit deployment secret reads, admitted EKS clusters | Unbounded roles, mutation of automation identities/ceilings/operator state, tenant vaults, role chaining |
| Gateway smoke/live checks | Exact smoke-token ARN and three environment-specific gateway configuration parameters | Deployment, arbitrary secrets and parameters |

`deployment-workflows.json` and `build-workflows.json` enumerate the workflows
admitted to the trusted group. The ordinary runner's action and resource ceilings
are shared by active and legacy Terraform. Explicit `NotAction`/`NotResource`
denies prevent an attached or resource policy from restoring service escalation.
All GitHub Actions jobs run on ARC. Container smoke checks reuse the existing
`adp-<environment>-gateway-build` CodeBuild project and smoke buildspec. No extra
CodeBuild project is created. Release builds retain the project's publishing role
and existing source path. PR runs explicitly select
`adp-<environment>-codebuild-gateway-pr`, which can read only
`codebuild/src/adp-<environment>-gateway-build-pr/*` and write build logs.
This prefix is separate from the release source prefix; PR callers cannot replace
release archives, and the PR role explicitly denies publication and other APIs.
The runner grants `iam:PassRole` only for that exact role to CodeBuild. Its
boundary also denies `StartBuild` when the `codebuild:serviceRole` condition is
missing or names another identity. Removing the workflow override therefore
cannot inherit publishing credentials. AWS documents these controls in the
[CodeBuild action and condition reference](https://docs.aws.amazon.com/service-authorization/latest/reference/list_codebuild.html).
Infrastructure PR workflows validate with the backend disabled. Their real plans
remain available by manual dispatch from main under the protected deployment
environment. One-shot maintenance, diagnostics, ingestion and teardown workflows
use that same reviewed identity; none rely on the repository runner boundary.

Deployment/build jobs verify their source is an ancestor of the reviewed main
workflow commit before obtaining credentials. Scan jobs intentionally inspect
arbitrary accepted source revisions, so their credential action is loaded from
protected main independently of the source checkout; they receive only the scan
identity and do not use deployment nodes.

Existing GitHub engine transport is a separate rollout. Preserve its *existing*
secret inputs by putting their complete ARNs in `runner_transport_secret_arns`
(active factory) / `transport_secret_arns` (legacy). The runtime module accepts
only exact GitHub runner/app inputs, including the six-character secret suffix;
it rejects wildcard prefixes, tenant vaults and deployment credentials. This does
not rotate keys or switch the engine transport. Do not omit required existing
transport inputs during cutover.

## Scope a build dispatcher to one component

Existing installations retain their current ECR read scope and Cyber worker
image-tag publication when the new inputs are omitted. For an executor-only
installation, supply both restrictions alongside the exact project inventory:

```hcl
build_project_names             = ["adp-dev-superplane-executor"]
build_ecr_repository_names      = ["adp-superplane-executor"]
build_publish_worker_image_tag = false
```

`build_ecr_repository_names` accepts exact repository names, not ARNs or
wildcards. Its default `null` retains the previous `adp-*` read scope for existing
multi-image consumers. `build_publish_worker_image_tag` defaults to `true` for
existing Cyber worker publication; set it to `false` to remove the entire SSM
write statement. Project dispatch and source staging remain limited to
`build_project_names`. These inputs do not create a CodeBuild project or ECR
repository; verify those separately in their canonical owning states.

## Ordered rollout

This is an upgrade procedure after GitHub has already been connected. It does
not add upfront GitHub setup to the canonical fresh-deployment guide.

1. Using the operator's existing deployment profile, verify the AWS account and
   obtain the rollout authorization required by the canonical deployment guide.
   Inventory the existing engine transport's exact secret ARNs and all custom
   repository IAM/RBAC bindings. Retain the operator identity throughout.
2. Bootstrap this **independent Terraform state** with the operator identity.
   Supply `name_prefix`, `environment`, `aws_region`, `cluster_name`, `repository`,
   exact `deployment_secret_arns`, optional existing `security_agent_space_id`,
   exact `deployment_db_user_arns` for retained database diagnostics/evaluations,
   `additional_cluster_names` for the Cyber/domain clusters, `cape_instance_ids` for
   the existing image-registration host, and `build_project_names` from the reviewed
   platform/agent-context outputs. Use the protected backend key
   `<environment>/trusted-automation/terraform.tfstate` (the deployment identity explicitly denies this prefix). The GitHub OIDC provider must already
   exist; this state does not silently replace or broaden it.
3. Create GitHub environments `adp-deploy-<environment>` and
   `adp-build-<environment>` and `adp-scan-<environment>` and `adp-rules-<environment>`. Require an independent reviewer, prevent self-review,
   disable admin bypass, and allow **only the main branch**, including rejecting
   tags. Set `ADP_DEPLOY_ROLE_ARN`, `ADP_BUILD_ROLE_ARN`, `ADP_SCAN_ROLE_ARN` or `ADP_RULES_ROLE_ARN` from this state's outputs,
   and `ADP_DEPLOY_REGION`, in their respective protected environments. Set `ADP_DB_USER` to the reviewed
   database username for diagnostic workflows; no master secret is fetched.
4. Configure the `adp-deployment` runner group for only the deployment repository
   and the exact `repository/.github/workflows/<name>@refs/heads/main` entries from
   both workflow inventories. Enable `restricted_to_workflows`. A runner label
   alone is not access control. Install `runner-isolation.yaml` and the ARC scale
   set with `runner-values.yaml`, replacing the org and **reviewed runner image
   digest**. Build `platform/automation-infra/Dockerfile` from a reviewed ARC image digest
   to add the PostgreSQL client and PyYAML. Use the `platform/arc-runner/Dockerfile` tool inventory (AWS CLI,
   Terraform, kubectl, Helm, Python, Node and archive tools), pinned by digest.
   Workflow checks fail if a required tool is absent; jobs cannot use sudo. Keep its GitHub App installation secret in
   `arc-deployment`; never mount it in the job pod. The pod has no IRSA, Pod
   Identity association, Kubernetes token or service-account deploy binding.
   Its separate tainted node pool prevents kernel sharing with repository jobs.
5. Prove each protected identity can be obtained on the trusted runner. With an
   operator GitHub token able to read environment/group settings, run
   `verify-cutover.py --account <id> --repository <owner/repo> --environment <env>
   --group-id <id> --purpose deployment`, and repeat with `--purpose build`.
   Repeat with `--purpose rules` for the public-rule publishing identity.
   Repeat with `--purpose scan` for the scan identity (no deployment runner group
   is used by scanning). The deployment role supports a three-hour session for the existing two-hour
   Windows image build and its teardown. Other deployment jobs request one hour.
   The scan role supports the existing six-hour job
   ceiling; its service role remains separately scoped and service-only.
   This checker reads configuration; it does not grant missing access. Save
   successful identity checks and reviewed Terraform plans as rollout evidence.
6. Apply per-project build roles/source-prefix changes, including the restricted
   gateway PR role, with the trusted deployment/operator identity. Reuse the
   existing gateway build project; do not create a second project. Exercise a publishing
   build and a gateway PR smoke build. Then update active/legacy runner policies
   and remove their EKS edit policies and service-account RBAC bindings **together**.
   Include webhook `scaledjob-rbac.tf` in this cutover. Set the retired
   `manage_ci_runner_cluster_admin` input to false; true is now rejected.
   Platform state excludes the trusted deployment access entry because this
   independent state owns it, avoiding duplicate-entry conflicts. Confirm custom/onboarded
   repository roles use the narrowed boundary and have no independent Kubernetes
   deploy binding. A stale binding can bypass the AWS restrictions. Remove ordinary runner ARNs
   from `cyber_cluster_admin_principal_arns` and other domain admin inventories.
   Do not add the independently managed trusted role to those inventories.
7. Run an ordinary repository job using the unchanged engine transport; verify
   approved inference/GitHub operations succeed, and Lambda mutation, a publishing
   `StartBuild`, tenant-object reads and Kubernetes deployment are denied. Verify
   gateway `StartBuild` fails with the role override omitted or changed to the
   publishing role, and succeeds with the exact PR role. Confirm PR source cannot
   overwrite a release archive and the PR role cannot publish to ECR.
   Verify
   a second tenant's secret is denied, including when another policy allows it.
   Confirm the trusted identities can still plan/apply after the boundary update.

Do not apply the new runner boundary first. Do not restore broad runner grants
to repair a failed deployment; use the retained operator/trusted identity. Stop
cutover if any existing operational workflow has not been moved to an appropriate
reviewed identity. Security scans remain on demand and need their existing scoped
scan identity preserved; never give scanning jobs the deployment identity.

## Local validation

`terraform test` in `modules/agent-factory/infra/modules/runner-iam` and
`platform/infra/modules/codebuild` renders policies with mocked AWS providers.
`terraform validate` in this directory checks the standalone bootstrap state.
Workflow/cutover tests exercise the GitHub trust requirements without contacting
GitHub or AWS. These are code-level checks, not evidence that a live installation
has completed the migration.

## Workload admission and operational identity repairs

The deployment role no longer gets a prefix-wide role factory. Supply
`deployment_role_boundaries` (exact role ARN to exact ceiling ARN),
`deployment_managed_policy_arns`, `deployment_instance_profile_arns`, and
`deployment_execution_resources` (exact
Lambda, CodeBuild, and EC2 targets). Role creation, inline-policy writes,
managed-policy attachment, trust changes and boundary assignment require the
registered ceiling. Boundary removal is denied. Boundary policies and automation
identities/policies are immutable to deployment; their metadata stays readable
for Terraform refresh. `PassRole` is confined to the admitted role list and AWS
services; it deliberately does **not** use `iam:PermissionsBoundary`, which AWS
does not provide for that action. Existing executable mutation is also scoped,
because updating Lambda code can inherit a role without a new PassRole check.

Admission runs automatically in this independent state's plan, under the
operator identity. `verify-workload-inventory.py` checks actual role boundaries,
rejects IAM mutation, role chaining, open-ended workload API ceilings and
unbounded executable capabilities. It verifies each executable's current service
role, every local role trusting the cluster's IRSA provider, managed/self-managed node roles, Fargate roles
and Pod Identity associations. The empty inventory denies all deployment AWS actions and grants no Kubernetes access;
an IAM API deny alone would not prevent kubectl authentication. Unknown node identities or cross-account workload targets fail admission.
`iam-write-actions.json` is the write-action inventory from AWS's IAM service
reference (retrieved 2026-09-23); review it when changing supported IAM APIs.

Prepare approved workload ceilings with the operator, then attach them using
`automation_permissions_boundary_arn` in the platform, gateway, agent-factory,
legacy runner and cyber Terraform roots. The input reaches their local role
modules; existing specialized runner/build boundaries stay in place. Null is
only for operator bootstrap. Keep the approved value in each root's deployment
configuration so Terraform never attempts to remove it. New roles are admitted
by the operator after bootstrap; subsequent automation can reconcile or recreate
them only with the same required ceiling. Up to eight grouped lifecycle policies
are available under IAM's attachment quota. Plans fail if the reviewed inventory
exceeds that quota. Complete admission **before** enabling the protected GitHub
environments or removing the old runner's permissions.

Configure `runner_gateway_execution_arns` in the active root (or
`gateway_execution_arns` in the legacy root) with the reviewed API ID, stage,
HTTP method and required `/agent/` or `/internal/` route. An example is
`arn:aws:execute-api:us-east-1:123456789012:abc123def4/dev/POST/internal/bedrock/invoke`.
Explicit `[]` disables gateway invocation; no API, stage or method wildcard is accepted. In a fresh active-factory installation, null derives the API and stage from the already-deployed environment's operator-owned SSM endpoint and enumerates POST agent/internal and GET internal methods, preserving the canonical gateway-before-factory deployment order.
The environment now determines shared CodeBuild/SSM resource names independently
of the runner role's `-agent` suffix. For scan triage's optional gateway mode,
supply `scan_gateway_execution_arns`; direct Bedrock inference remains available.
All four nightly ledger jobs use the existing scan identity. Ordinary runners
have no ledger object/list grants.

Create `adp-checks-<environment>` with the same main-only independent review
protection. Set `ADP_CHECKS_ROLE_ARN` from `checks_role_arn`, and set
`ADP_SMOKE_REFRESH_TOKEN_ARN` to the exact value of `smoke_refresh_token_arn`.
The reusable smoke call explicitly forwards `id-token: write`. The scheduled
live check gets its own credentials as well. If no smoke token is configured,
smoke reports that it is skipped; IAM errors with a configured token still fail.
Cognito `InitiateAuth` is authenticated by that refresh token, not an IAM grant.
Stage 1 Terraform/Kubernetes diagnostics remain a separate protected deployment
job. The existing E2E role is obtained before its first configuration read.

GitHub issue agent workflows now select their existing repository-scoped GitHub
task tracking (`BEADS_ENABLED=false`) and do not initialize/sync the shared
cross-account Beads database. This changes workflow authority, not stored data.
Export any Beads-only tasks into the repository's GitHub tracker as a separately
reviewed data migration before relying on them in those workflows. Local Beads
and explicitly configured standalone integrations remain available. Repository
agents also no longer automatically acquire platform Kubernetes or SkyPilot
credentials; infrastructure work uses the protected deployment workflows or the
existing separately bound customer-task credential path.

Cyber workflow wrappers retain app-owned composite implementations. Worker
manifest extraction executes inside its existing CodeBuild job with Docker
networking disabled, and only that project's declared artifact prefix is
writable. The publishing dispatcher updates only the exact worker build-tag
parameter; no Docker daemon is mounted into the protected ARC runner.

The GitLab required PR check executes the real handler/helper unit contracts
without live credentials. Deployed webhook-contract and fleet checks run on
protected main with the checks identity. Configure their exact
`gitlab_checks_secret_arns` and `gitlab_checks_queue_arn`; SSM reads enumerate
only the existing GitLab/webhook test configuration keys. The browser image
build now uses the same protected publishing lane and Terraform-owned project
contract as the worker image build.

The admission checker accepts only the finite API catalog in
`workload-actions.json`. An unconditional `Deny`/`NotAction` must close all other
APIs, including future service operations. Execution is currently verified only
for Lambda, CodeBuild and EC2/SSM targets; other execution services require an
explicit extension and tests before admission. Customer credential brokers that
legitimately chain roles are not compatible with this deployment ceiling and
must remain operator-managed until a separately reviewed broker ceiling exists.
Do not enable deployment access for a cluster containing such an unadmitted role.
The operator must also review data paths: mutable image repositories, source
archives, event inputs and secrets must not feed an unadmitted privileged service.
API admission is not a proof of arbitrary application dataflow safety.

Mutable managed policies are checked for every existing role/user/group and
boundary attachment. Policies attached outside the admitted bounded roles, or
used as any identity's boundary, are rejected. New mutable policies may be
created only at their exact admitted ARNs and attached only to admitted roles.
Re-run admission after operator changes to these attachments.

### Gateway deployment on the existing ARC pool

Gateway deploy, migrations, smoke-test deployment helpers, pricing finalization,
and gateway infra apply select `arc-runner-org` in the `Default` group. This
uses the existing runner with its reduced ambient role. Use the ARC scale-set
label directly (`runs-on: arc-runner-org`), matching working CI jobs; an explicit
`group: Default` selector left release jobs queued even while this pool was idle. These jobs retain their
protected environments, reviewed main-source checks, and explicit trusted OIDC
credential exchange with no ambient credential fallback. No runner IAM policy is
expanded by this routing change. This pool does not provide the dedicated node
and tokenless service-account isolation described for `arc-runner-deployment`.
Other deployment workflows retain their dedicated runner selection.
