# Tools dev release: owner handoff

This is a **post-review procedure**, not evidence of a deployment. The source
changes for #7513 do not create a GitHub environment, migrate Terraform state,
or deploy to an AWS account. Only the platform/automation state owner may approve
and execute bootstrap and state moves. The repository environment owner binds
the selected GitHub identity; the tools owner checks the actual client spike.
Do not use these steps on prod: prod remains dispatch-only with a human reviewer.

## Before enabling the workflows

1. Select a reviewed commit on `main` and reconcile the actual AWS account, role,
   region, cluster, namespace, gateway and current Terraform states with the
   platform owner. Confirm the release authority currently in effect: a
   checked-in opt-in or a passing mock test is not evidence that a role is safe
   or has been applied. Keep `enable_tools_deployment = false` until the scoped
   OIDC role, exact access entry/group, named-object Role/RoleBinding, state
   split and source gates have passed review. Never use the release role to
   create IAM resources, change key policies or fix an unexpected plan.
2. Follow the [bootstrap state migration](../../modules/tools/platform/bootstrap/README.md)
   under exclusive approved authority. Preserve private snapshots, state
   lineage/serial and resource inventory; move existing foundation ownership
   exactly once to `tools/bootstrap/dev.tfstate` and leave only AgentCore
   gateway, targets and policy in `tools/platform/dev.tfstate`. Reconcile a
   fresh plan for **both** roots before apply. Do not apply the new root to an
   old combined state, import the same resource into two states, delete a
   state object to hide drift, or run a destructive foundation plan.
3. Through the approved bootstrap process, set `enable_interceptor = true`
   with the façade OIDC identity enabled. Confirm the bootstrap outputs include
   `interceptor_function_name`, `interceptor_function_arn`,
   `interceptor_execution_role_arn` and `assertion_signing_key_arn`. The
   function starts with a **403-only placeholder** at `$LATEST`, not a `live`
   alias; it must not be wired into a working gateway or counted as a security
   interceptor. Configure its façade role and verification key from these
   reviewed outputs. Confirm the execution role can verify only the assertion
   key and write only its own logs. Verify the gateway role's interceptor invoke
   permission and any other bootstrap-owned target wiring before release,
   without granting IAM/KMS mutation to the ordinary release identity. The
   managed bootstrap role initially grants invoke **only on the future `:live`
   alias**, which does not yet exist; this is not a gateway attachment.
4. Under platform ownership, pre-create the `tools-facade` Deployment and
   Service, `tools-facade` and `tools-facade-reconciler` ServiceAccounts, and
   `tools-facade-reconciler` CronJob in `adp-gateway` on the selected cluster.
   Reconcile the service identity/IRSA annotation and scheduled worker before
   unsuspending it. The release Role can read/patch only these named objects;
   it cannot create anything or edit another workload, Role, Secret or token.

The cleanup CronJob selects up to 50 due records per minute. Its 45-second
processing window leaves five seconds before Kubernetes terminates the job.
Scheduled DynamoDB, KMS and Lambda clients use explicit connect/read timeouts
and one SDK attempt; socket settings do not bound DNS, credential discovery or
the complete lifetime of a streaming response. The job deadline remains the
final process bound. Before claiming a provider cleanup, the worker reserves
time for signing, invocation and durable outcome storage. It can still finish
cheap records when another provider call no longer fits.

Deferred work retains its due record without adding a cleanup attempt. A lost
provider response retains the original execution, unknown outcome and scheduled
retry, including when the remaining window prevents another database write.
The summary reports selected, processed and deferred records plus deadline
exhaustion; processed does not mean provider termination was confirmed. Verify
the actual backlog, successful scheduled runs and provider termination evidence
after deployment. A larger batch improves throughput but does not guarantee a
600-second reconciliation deadline under arbitrary backlog or provider outages.

## Protected GitHub configuration

The repository environment owner creates or verifies `adp-tools-deploy-dev` in
repository Settings → Environments with **only `main`** allowed to deploy. Dev
push delivery may proceed without a human reviewer once owner approval and the
bootstrap gates above are complete. Keep prod's dispatch-only required reviewer
and existing environment unchanged. Trust the three named workflows only:
`tools-facade-deploy.yml`, `tools-platform-deploy.yml` and
`tools-interceptor-deploy.yml`. The selected role must trust only the
`repo:aws-e/adp:environment:adp-tools-deploy-dev` OIDC subject; inspect the
applied role, access group and policy before opting in. No IAM role chaining.

Set environment variables from the **approved private target configuration**:
`ADP_DEPLOY_ROLE_ARN`, `ADP_TOOLS_ACCOUNT_ID`, `ADP_TOOLS_STATE_BUCKET`,
`ADP_TOOLS_REGION`, `ADP_TOOLS_AWS_REGION`, `ADP_DEPLOY_REGION`,
`ADP_TOOLS_EKS_CLUSTER_NAME`, `ADP_TOOLS_NAMESPACE` (`adp-gateway`) and
`ADP_TOOLS_INTERCEPTOR_FUNCTION_NAME`. Verify all three workflows resolve the
same region/account/role and the named function; unset is a stop. Store the
private `ADP_TOOLS_DEPLOY_TFVARS_JSON` overlay as an environment **secret**, not
in source. It must select reviewed bootstrap `gateway_role_arn`,
`gateway_kms_key_arn`, `assertion_signing_key_arn`,
`interceptor_execution_role_arn`, `interceptor_lambda_arn` (the reviewed
function's `:live` alias) and `gateway_internal_api_execution_arn` (the ADP
gateway's API Gateway stage execution ARN, so the façade role may invoke
`POST /internal/v1/tools/authorize`); keep any other private target identifiers
there.
For a private Knowledge target, the protected job can add only the five reviewed
Knowledge settings using the durable `ADP_TOOLS_KNOWLEDGE_CONFIG_JSON` variable.
The original secret stays private and is not exported or replaced. Follow
[Knowledge first enablement](knowledge-first-enable.md) and
[Connections target activation](provider-onboarding-automation.md#protected-tool-activation) for its exact binding,
conflict checks and separate temporary creation authorization.
The merge with checked-in portable defaults is preserved. Never copy actual
account numbers, function ARNs, endpoints, secrets or run logs into public docs
or the PR. Confirm the selected gateway/target ARNs in the deployment role
policy against reviewed private outputs; the source ARN shape alone has not
been checked against live resources here.

## Ordered dev verification after merge and approved apply

Use a reviewed revision and reconcile prior effects before each dispatch. The
interceptor and platform workflows share a per-environment concurrency group,
but independent pushes are **not** an ordering guarantee: a newer queued run
may replace a pending one, or platform release may fail before interceptor
publication. Inspect run history and deliberately rerun the reviewed platform
revision if needed; never blindly rerun a historical workflow against changing
source or private inputs.

1. Publish the **real** Python interceptor using
   `tools-interceptor-deploy.yml` on the reviewed main revision. The release
   script first checks the configured façade role/key, publishes a numbered
   version, verifies its package hash and moves `live` using the alias
   revision. Keep the private `release.json` and deployment-context artifact.
   Read back the qualified version, `CodeSha256`, handler, role, key/role
   environment and unweighted `live` alias. If any disagree, stop; do not
   activate protected targets. The placeholder is not a pass.
2. Before platform/target activation, the state owner reviews and applies a
   second bootstrap-only plan enabling the shared tool consumer to verify the
   same assertion key. Use the already-published managed `:live` alias and
   the bootstrap-owned `interceptor_execution_role_arn` as bootstrap inputs;
   reject another function or role. This IAM/KMS policy step is privileged:
   do not run it through the ordinary release identity. Verify the selected
   shared Lambda identity exists and has only the approved KMS verify grant.
3. Run `tools-platform-deploy.yml` at a reviewed source containing the **same
   interceptor package** as the live numbered version. Its release role must
   touch only reviewed AgentCore gateway/target/policy resources in the release
   state. The script independently checks version, exact package hash, active
   handler/verification configuration and alias before plan, before/after
   apply and before receipt. Confirm the gateway interceptor ARN points to
   `:live` and only then are protected targets enabled. Read back gateway
   configuration, registered target IDs and release receipt; match its
   `interceptor_version`, `interceptor_code_sha256` and
   `interceptor_lambda_arn` to the interceptor receipt and live Lambda. A
   green Terraform apply alone is not a working interceptor or gateway.
4. Run `tools-facade-deploy.yml` at the reviewed main revision after target
   wiring. It must use the selected identity and roll the existing named
   Deployment in `adp-gateway` to an immutable ECR digest. Check its rollout
   status and read back the container image digest, ECR image tags, release
   receipt and deployment-context artifact against that revision. Only then
   perform an authenticated façade `tools/list` smoke with the authorized
   private client; do not publish the gateway URL or token. A receipt without
   a ready workload is not AC-03 acceptance.
5. The tools owner resumes #7253 against the **deployed façade** and retains
   that story's own five spike results and story PR evidence. A local test,
   placeholder Lambda or green pipeline is not AC-05 live acceptance.

For handoff, record privately: reviewed source/workflow SHAs, selected account
and role identity, two state lineages/plan summaries, relevant workflow runs,
observed named Deployment and digest, interceptor numbered version/hash/alias,
AgentCore gateway/target readback and the spike evidence. AC-01 / proposed
SCN-01 cover scoped trust and negative policy/RBAC tests; AC-02 / proposed
SCN-02 cover the three-workflow inventory and rejection of unlisted identities.
These are pre-review source checks, not observed live permissions. AC-03 /
proposed SCN-03 require rollout/context; AC-04 / proposed SCN-04 require real
Lambda/gateway readback; AC-05 / proposed SCN-05 require the client spike.
The proposed SCN-06 source-only placeholder denial is not live interceptor
acceptance. Leave every live ID **not run**, not passed, until its owner has
retained that evidence.

## Recovery and stop conditions

If a plan asks to create, mutate or destroy bootstrap-owned IAM/KMS/ECR/build
resources from the release state, stop and reconcile ownership from private
snapshots. If the alias is missing, weighted, still at the placeholder, has a
mismatched hash/configuration or changes during apply, stop target activation;
retain state and receipts for the owner to assess any partial effect. Never
broaden a permission, use a wildcard ARN, rerun an old deploy blindly or
replace an identity merely to turn a check green.

For a verified interceptor rollback, dispatch
`tools-interceptor-deploy.yml` with `rollback_to_version` set to the **prior
verified numbered version** (never `$LATEST`); read back the `live` alias and
hash and retain its new receipt. The platform gate matches the alias hash to
the **selected source package**: use a reviewed main-ancestor revision matching
that prior version when reconciling or reverting gateway targets through a
reviewed state plan. For a façade rollback, dispatch
`tools-facade-deploy.yml` with `rollback_to_digest` set to the prior verified
ECR digest, then confirm ready workload and receipt. Preserve the previous
gateway configuration/state and restore it only through its reviewed plan;
never destroy keys or state for recovery. Resume smoke and #7253 only after
all three readbacks agree.
