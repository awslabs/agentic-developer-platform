# Dedicated developer-workflow identity (S14)

The label-triggered `agent-developer.yml` previously selected the general ARC
pool and inherited its IRSA role. The new `arc-runner-agent` pool uses
`agent-workflow-sa` and a separate `*-agent-workflow` IAM role. Its trust requires
the exact service-account subject, cluster OIDC provider and STS audience.

The role reuses the reviewed runtime-policy resource scopes, selecting only
identity lookup, model inference, gateway endpoint discovery, configured gateway
routes and the two exact developer-App secrets. Its permissions boundary
explicitly denies other APIs and resources. It has no build, PassRole, S3,
ECR, EKS, IAM mutation or role-assumption grant. There is no Kubernetes RoleBinding
or EKS access entry for this identity. Shared runner and privileged deployment
identities are unchanged.

The workflow uses the dedicated label without a shared-pool fallback and checks
its actual STS identity against repository variable `ADP_AGENT_WORKFLOW_ROLE_ARN`
before reading App credentials. Its unused GitHub OIDC permission is removed.
The gateway model preflight now runs from the directory actually checked out.

## Ordered rollout

1. Build the real factory Lambda inputs with
   `platform/scripts/build-agent-factory-lambdas.sh` before planning the factory
   root. Even a targeted plan parses the existing sweeper's `file()` input.
2. For the active factory, enable `enable_agent_workflow_runner` and review a
   saved plan for `module.agent_workflow_iam` and `module.agent_workflow_pool`.
   These modules deliberately avoid the shared pool's module-level IAM dependency. Refuse a plan that replaces
   the shared controller, general runner pool or other resources. The dev tfvars
   retains the enabled setting so later applies do not remove the new pool.
3. Apply the reviewed additive plan. Verify the dedicated service-account
   annotation, exact IAM trust, boundary, pool registration and controller
   readiness. Check the role's effective allowed/denied operations and verify
   an actual runner reports the expected STS identity before routing work.
4. Set `ADP_AGENT_WORKFLOW_ROLE_ARN` to the verified role ARN, then merge the
   workflow-routing change. The dedicated label must be available first.
5. Verify a developer workflow starts on the dedicated pool. Keep webhook
   dispatch unchanged. Record runtime evidence separately from source tests.

For the legacy standalone runner stack, the same IAM module is instantiated
with `enable_agent_workflow_runner=true`. After its plan/apply, provide the
reviewed `AGENT_WORKFLOW_RUNNER_IMAGE` and run
`runner-infra/scripts/deploy-agent-workflow-runner.sh`. This uses the stack's
output contract and existing `github-arc-secret` in `arc-runners`; it does not
copy a general runner policy or invent credentials. The ARC controller and
registration secret must already exist. Configure the repository role variable
for that installation before switching its workflow.

Rollback stops new label-triggered work, drains the dedicated jobs, then removes
only the dedicated Helm release/service account and IAM resources using a
reviewed Terraform plan. Do not silently route repository-authored work back to
the general runner. Webhook dispatch remains available throughout.

## Evidence boundaries

Mock-provider tests check the rendered IAM trust, grants and explicit denies;
ARC tests check the service-account binding, routing label and refusal of a
shared-role configuration. Shell tests execute the workflow's actual identity
guard with valid, missing, foreign-account and shared-role identities. These
are source tests, not proof of live IAM or Kubernetes behavior.

S14 and older finding #4725 also require S13's separately owned admin-audit
acceptance. Installing this pool does not complete that requirement.
