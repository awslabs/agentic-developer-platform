# Set up and delete evaluator credentials

Run `platform/scripts/setup-evaluator-credentials.py` after deploying the target,
using an infrastructure operator identity. This installs the base identity needed
by deployment snapshots and the CLI login checkpoint. It uses GitHub OIDC, with
no long-lived AWS keys in GitHub.

Before setup, review [platform targeting and supported environments](platform-regression.md#platform-targeting-and-supported-environments)
and prepare the [private target record and operator preflight](platform-regression.md#target-record-and-operator-preflight).
The GitHub target label selects protected configuration; it does not override
every lane's AWS resource environment or region. This script prepares the
snapshot/CLI evaluator, not all live regression lanes.

The script prepares private files by default. With `--apply`, it provisions a
separate Terraform state, applies named gateway-observer RBAC, and configures
three secrets in the selected GitHub environment:

- `AWS_CLI_UPLIFT_EVAL_ROLE_ARN`
- `CLI_UPLIFT_EVAL_BINDINGS_JSON`
- `CLI_UPLIFT_EVAL_GATEWAY_CATALOG`

New and unrestricted GitHub environments are restricted to `main` only. Setup
preserves existing reviewers, wait timers and self-review restrictions. Existing
branch policies must already allow exactly `main`; other policies are refused
rather than overwritten. Conflicting repository or
environment variable overrides fail before provisioning.

## Prerequisites

Install Python 3.10+ with boto3, Terraform 1.10+, AWS CLI, `gh`, and `kubectl`.
Authenticate `gh` with permission to manage repository environments and secrets.
The AWS operator needs permission to manage the resources in
[`platform/evaluator-infra/main.tf`](../../platform/evaluator-infra/main.tf).
Never bootstrap with the evaluator role or from a pull-request job.

Provide:

- The expected AWS account and operator profile, repository, and target environment.
- The AWS resource environment when it differs from the GitHub target name.
  Pass `--resource-environment` to select gateway SSM paths. For example, a
  GitHub target named `staging` can test resources named `dev`; use
  `--environment staging --resource-environment dev`. OIDC trust continues to
  use `staging`. Omitting the option retains the target name as the namespace.
- An existing private Terraform backend bucket owned by the target account, in
  the target region. Setup uses a separate repository/environment-specific key
  and S3 state locking; it does not reuse platform deployment state.
- A private subnet and security group in the target VPC. They must reach SSM,
  S3, Secrets Manager and the gateway; the security group must meet the existing
  evaluator preflight's no-ingress requirement.
- A dedicated native Cognito test identity with its initial password challenge
  completed, and its existing Secrets Manager fixture reference. See
  [fixture requirements](cli-uplift-evaluation.md#configure-the-first-run).
  Setup reads secret metadata, never its password. It does not create users,
  generate fixture credentials, or copy a real user's identity.
- An active EKS cluster with access entries enabled, an immutable gateway ECR
  repository, and, for apply, an operator kubeconfig authorized to install the
  namespace-scoped observer Role and RoleBinding. Setup checks the kubeconfig's
  server against the selected EKS endpoint.

## Private configuration

Start with `tests/e2e/cli_uplift/config.example.json`, outside the checkout.
Replace **every** target identifier with reviewed private configuration. The
script requires all evaluator target fields explicitly; public example values
are not a fallback. Core smoke and login do not require a destination account.
Cross-account fixtures require a distinct destination account and explicit roles.

Add `gateway_deployment`, `credential_secret_name`, and
`instance_security_group_id`. Omit `instance_profile`, `state_bucket`, and
`state_kms_key_id` on first setup: the script generates the profile and a private,
versioned evaluation bucket and a dedicated KMS key for bundles and recovery
state. A customer-managed key on the fixture secret
is supported through a narrowly scoped decrypt grant. Keep any required KMS key
policy permission under the operator's existing management.

Supply a separate gateway catalog JSON object. This fictional structure shows the
shape; the values must be replaced privately:

```json
{
  "staging": {
    "account": "000000000101",
    "region": "us-east-1",
    "gateway_url": "https://gateway.example.com/api",
    "cluster": "adp-dev-eks-cluster",
    "cluster_arn": "arn:aws:eks:us-east-1:000000000101:cluster/adp-dev-eks-cluster",
    "namespace": "adp-gateway",
    "deployment": "bedrockgateway",
    "service": "bedrockgateway",
    "container": "bedrockgateway",
    "selector": {"app": "bedrockgateway"},
    "image_repository": "000000000101.dkr.ecr.us-east-1.amazonaws.com/adp-gateway",
    "images": {}
  }
}
```

The catalog key must match `gateway_deployment` in the private bindings. Setup
uses immutable ECR source tags; it refuses historical image-receipt catalogs that
would need additional source-bucket permissions. Gateway, region, account and
cluster identities must agree. The GitHub target name is independent of AWS
resource naming: the coordinated regression still uses its existing `dev`
resource convention.

## Preview and apply

Set these shell variables from private operator configuration:

```bash
python3 platform/scripts/setup-evaluator-credentials.py \
  --profile "$AWS_PROFILE" --account "$TARGET_ACCOUNT" \
  --repository "$GITHUB_REPOSITORY" --environment "$TARGET_ENVIRONMENT" \
  --resource-environment "$RESOURCE_ENVIRONMENT" \
  --bindings "$PRIVATE_BINDINGS" --catalog "$PRIVATE_CATALOG" \
  --state-bucket "$TERRAFORM_STATE_BUCKET" --output-dir "$PRIVATE_SETUP_DIR"
```

Preview performs read-only metadata checks and writes files with private
permissions. Review the rendered Terraform inputs, module and `observer.json`.
Repeat the same command with `--apply --kubeconfig "$OPERATOR_KUBECONFIG"` to
install. Use a new, empty output directory for each target. Reusing it for a
different account, repository, region, environment or backend is refused.

Apply creates a saved Terraform plan, rejects deletion/replacement, and applies
that exact plan. It then installs observer RBAC and publishes bindings/catalog
before publishing the role ARN. Partial failures are nonzero; rerun the same
command and output directory to complete an interrupted setup. Private plan
output remains in `plan.txt`. Do setup while evaluations for this target are idle.

The evaluator can observe only the named gateway Deployment and Service; it
cannot list pods, exec, read Kubernetes secrets or deploy workloads. Its EC2
launch, SSM and termination permissions are limited to the evaluator profile,
selected network and environment-owned instances. Bundle/recovery storage has
its own bucket and prefix. Only the disposable instance can read the designated
fixture password. Optional destination access is restricted to explicitly supplied
role ARNs; those roles must already trust the relevant evaluator identities.

## Delete credentials and retain evidence

Use the same script with `--delete`. Stop dispatching evaluations for the target,
pause its scheduled evaluations, and let active jobs finish cleanup first. The
script refuses deletion if any non-terminated EC2 instance carries the evaluator
target tag or uses its instance profile, including stopped instances. It never
terminates instances itself. Keep evaluations idle until deletion completes.

Keep the original private setup directory: its target marker, Terraform inputs,
observer manifest and `published-secrets.json` receipt establish the resources
this setup owns. The receipt contains secret names and publication timestamps,
not secret values. If a GitHub secret was replaced since setup, deletion refuses
to remove it. Missing secrets and already-removed credentials are safe on retry.
For an older setup without receipts, rerun setup to publish the reviewed target
configuration and record ownership before using delete.

Preview with the same private arguments used for setup:

```bash
python3 platform/scripts/setup-evaluator-credentials.py \
  --profile "$AWS_PROFILE" --account "$TARGET_ACCOUNT" \
  --repository "$GITHUB_REPOSITORY" --environment "$TARGET_ENVIRONMENT" \
  --resource-environment "$RESOURCE_ENVIRONMENT" \
  --bindings "$PRIVATE_BINDINGS" --catalog "$PRIVATE_CATALOG" \
  --state-bucket "$TERRAFORM_STATE_BUCKET" --output-dir "$PRIVATE_SETUP_DIR" \
  --kubeconfig "$OPERATOR_KUBECONFIG" --delete
```

Delete preview requires access to the existing Terraform state, GitHub secret
metadata and the target cluster. It reads live metadata and writes a private
`delete-plan.txt` and saved plan, without changing remote resources. Review the
plan, then repeat with `--delete --apply` to execute. Apply regenerates and checks
the plan, rejects changes outside the owned credential resources, and applies
that exact saved plan. No blanket `terraform destroy` is used.

| Removed | Retained |
| --- | --- |
| Evaluator OIDC role and inline policy | Shared account GitHub OIDC provider |
| Evaluator EC2 role, policy, SSM attachment and instance profile | Cluster, network, ECR repository and platform deployment |
| Evaluator EKS access entry and named observer Role/RoleBinding | Other Kubernetes access and workloads |
| The three setup-owned GitHub environment secrets | GitHub environment, reviewers, protections and other secrets |
| | Versioned evidence bucket, all objects, KMS key/alias and bucket protections |
| | Terraform backend/state, Cognito test identity, fixture secret and destination roles |

The role ARN secret is removed first, followed by Terraform-managed credentials,
observer RBAC and the remaining two secrets. Failures return nonzero; rerun the
same delete command and directory to finish an interrupted deletion. The cluster
must remain accessible until observer cleanup completes; delete evaluator access
before tearing down the platform.

This removes evaluator access, not retained data. Storage and KMS charges may
continue. There is no evidence-purge option. Keep the private directory and
backend state for recovery and audit. To reinstall credentials, rerun the normal
setup command with `--apply`; it reuses the retained bucket and key.
Keep the same resource-environment selection for setup, deletion and recovery.
An existing private directory cannot be retargeted to another resource namespace.

For commissioning a new environment after setup, use the [new environment E2E smoke pack](new-environment-smoke.md). It groups existing tests and accepts explicit target, resource environment, region and gateway release inputs.

## Verify and complete regression prerequisites

After merging the setup/harness changes, dispatch a snapshot and the CLI `login`
checkpoint from `main`, using the selected protected target and verified deployed
revision. For the standalone checkpoint:

```bash
gh workflow run eval-cli-uplift.yml --repo "$GITHUB_REPOSITORY" --ref main \
  -f environment=dev -f target_environment="$TARGET_ENVIRONMENT" \
  -f expected_revision="$DEPLOYED_REVISION" -f mode=start -f suites=login
```
A successful setup command confirms installation, **not** successful
OIDC assumption, network connectivity, login, cleanup or release acceptance.
Review the checkpoint's results and cleanup evidence.

The full daily pack also needs the distinct checks, browser-checks and deploy
identities and owned scenario fixtures listed in
[platform regression configuration](platform-regression.md#configuration-before-activation).
This script does not repurpose the evaluator as a deployment role or grant all
scenario permissions. Additional fixtures, enabled-chat qualification, destination
trust and other known workflow defects remain explicit prerequisites. Self-hosted
runners and S3 evidence remain the execution path; setup changes no Actions billing.
