# Trusted automation cutover (#5674)

This state separates reviewed infrastructure and publishing workflows from
repository jobs. It is a code/configuration change until an operator performs
the rollout. Do not run an apply, change a live role, rotate secrets, or change
engine transport as part of a PR review.

## Permission inventory

| Caller | Required operations | Deliberately excluded |
|---|---|---|
| Ordinary repository runner | Existing model/gateway transport; ECR pull; own logs; upload source and start/poll the non-publishing gateway smoke project | Lambda mutation/invocation, publishing builds, ECR push, tenant objects/state, IAM, role assumption, EKS administration, KMS policies/grants, SSM |
| Gateway PR smoke build | Read its own source archive; write its own build logs; local Docker build/run | ECR publication, secrets, Terraform state, other project archives, deployment |
| Publishing build dispatcher | Start/poll the exact `build_project_names`; upload their source archives; inspect published image digests | Project mutation, changing service roles, IAM, Kubernetes deployment, secrets |
| On-demand scan dispatcher | Existing Security Agent space and exact service role; nonpublishing Grype/Syft projects; scan source/evidence prefixes | Deployment, tenant/state reads, secrets, publishing builds |
| Public-rule ingestion | Read benign canaries and publish only public YARA rule versions under a separate protected rules identity, away from deployment nodes | Other bucket objects, builds, deployment, tenant data and secrets |
| Each CodeBuild project | Its own source prefix, log group and declared output repositories/artifacts | Sibling projects, identity mutation, service control, secrets/state; its service trust pins the exact project ARN |
| Trusted deployment | Existing infrastructure API inventory in `deployment-services.tf`, bounded ADP role/policy lifecycle, explicit deployment secret reads, EKS deployment | Tenant vault namespaces and unlisted secret reads |

`deployment-workflows.json` and `build-workflows.json` enumerate the workflows
admitted to the trusted group. The ordinary runner's action and resource ceilings
are shared by active and legacy Terraform. Explicit `NotAction`/`NotResource`
denies prevent an attached or resource policy from restoring service escalation.
The smoke project cannot publish even if a caller overrides its buildspec.
Gateway PR image checks run on credential-free GitHub-hosted Docker VMs using
the canonical smoke buildspec. They do not require a pre-existing AWS project,
upload source to platform storage, or publish images. The bounded CodeBuild smoke
project remains available for repository jobs after the authorized cutover.
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
   platform/agent-context outputs. Use a distinct backend key such as
   `dev/trusted-automation/terraform.tfstate`. The GitHub OIDC provider must already
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
6. Apply per-project build roles/source-prefix changes and the new gateway smoke
   project with the trusted deployment/operator identity. Exercise a publishing
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
