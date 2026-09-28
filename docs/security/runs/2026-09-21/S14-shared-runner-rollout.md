# Shared-runner bounded ceiling rollout (S14, #5983)

This is a deployment procedure, not evidence that a live role has been narrowed.
The dev audit found the older broad boundary and AdministratorAccess/ReadOnlyAccess
attached to `adp-dev-agent-runner-role`. Re-read current policy versions and
attachments before acting. The dedicated agent-workflow identity is separate.

## Compatibility gate before any live policy update

Inventory every job using `arc-runner-org`, including reusable workflows and
scripts it invokes. Record event/ref restrictions, source checkout, AWS calls,
current role and intended replacement role. Confirm actual service-account/IRSA
and GitHub runner-group bindings; a source `runs-on` declaration alone is not
live isolation evidence. Preserve ongoing jobs and coordinate with other deploys.

Trace credential acquisition before classifying AWS calls as shared-role needs.
Security scan publication and gateway smoke/live checks already select separate
trusted GitHub OIDC identities in reviewed source, unset ambient credentials and
use `role-chaining: false`; their S3/Cognito/SSM calls are not automatically shared
IRSA requirements merely because they run on `arc-runner-org`. Verify that these
OIDC paths and their trust restrictions work in the deployed environment.

The compatibility review must also resolve ambient executor-build calls, plus conditional role-chaining fallbacks such as eval-cli-uplift.
Each actual shared-role capability outside the ceiling needs a reviewed migration
or an explicit retirement decision. Do not expand the shared ceiling to recover
privileges. Read-only test credentials can still confer application administrator
authority and must not be exposed to untrusted PR code.

Protected deployments already select group `adp-deployment`, label
`arc-runner-deployment`, and a protected environment in relevant source workflows.
Verify their actual identity and branch/environment protections. Jobs that execute
untrusted PR content must never inherit that deployment identity. Security scans,
publication and live tests may require their own scoped trusted identity instead.

A successful lint job does not demonstrate all ordinary capabilities. Before
cutover, prepare a bounded same-IRSA fixture job for the capabilities actually
retained: exact transport secret decryption, endpoint SSM read, configured gateway
route, ECR pull, own log stream, model invocation and the non-publishing CodeBuild
PR project where used. Capture the assumed-role ARN and specific successful calls,
not secret values. Keep these jobs distinct from paid security scans and releases.

## Resolve inputs from metadata

Use the active account/region and reviewed exact secret ARNs, including suffixes.
For each secret, call Secrets Manager DescribeSecret. Resolve its KmsKeyId with
KMS DescribeKey; an absent key identifier means `alias/aws/secretsmanager`.
Custom aliases also need resolution. Supply the returned key ARN (UUID or MRK),
not the alias ARN. Never retrieve or print secret contents merely to resolve keys.

The active root inputs are `runner_transport_secret_arns`,
`runner_transport_secret_kms_arns` and `runner_gateway_execution_arns`. The legacy
root uses `transport_secret_arns`, `transport_secret_kms_arns` and its reviewed
gateway inputs. Empty lists disable those capabilities; do not accidentally
replace live transport inputs with empty defaults. Keep the metadata-only input
file protected and preserve its values in the deployment configuration.

## State and plan gate

Read applicable deployment instructions. Confirm account, backend bucket/key,
workspace, source revision, existing role name/trust and all three policy ARNs.
Inspect policy attachment and permissions-boundary consumers: updating a managed
policy affects every consumer, not just the expected runner. Confirm the policies
and their attachments are owned by this installation; do not claim a policy is
outside all Terraform state merely because this root does not declare it.

For the existing active dev maintenance root, use its real initialized backend
and `terraform.tfvars`; there is no assumed relative environments/dev tfvars file.
An example with a separately prepared metadata-only input file is:

```bash
cd modules/agent-factory/infra
terraform state list
terraform plan -input=false -lock-timeout=60s \
  -var-file=terraform.tfvars -var=gateway_deployed=true \
  -var-file=/secure/runner-transport.tfvars.json \
  -target=module.runner_iam.aws_iam_policy.runner_boundary \
  -target=module.runner_iam.aws_iam_policy.runner_base \
  -target=module.runner_iam.aws_iam_policy.runner_services \
  -out=/secure/runner-boundary.tfplan
terraform show -no-color /secure/runner-boundary.tfplan
```

The legacy root is `modules/agent-factory/runner-infra/infrastructure`; use that
installation's existing backend/complete variable file and unprefixed targets
`aws_iam_policy.runner_boundary`, `aws_iam_policy.runner_base` and
`aws_iam_policy.runner_services`. Never initialize it against the active factory
backend or apply both ownership paths to the same resources.

Targeting includes dependencies and is not a guarantee of isolation. Accept only
reviewed in-place policy-document changes, with no role replacement, controller,
Helm, EKS-access, RBAC, secret or other infrastructure changes. Resolve moved-state
errors deliberately; do not widen to a full apply or force-unlock another writer.
Protect plan files because unrelated root values can be sensitive.

Only after compatibility, source checks and plan review pass:

```bash
terraform apply -input=false /secure/runner-boundary.tfplan
```

Re-read the live boundary association, default policy versions and documents.
Record hashes and exact intended statement differences. A successful apply alone
is not effective-permission evidence. Do not restore the old broad boundary.

## Effective-permission verification

Use `iam.simulate_principal_policy` on the actual role so all current identity
policies and its permissions boundary participate. Simulating only a boundary as
an ordinary policy does not establish the role's effective grants. Supply concrete
resource ARNs of the correct type: self-boundary mutation targets the policy ARN,
not a role ARN. Supply all required context and fail on unexpected missing values.

Maintain a reviewed JSON case manifest with fields `name`, `action`, `resource`,
`expected`, and optional AWS `ContextEntries`. This runnable verifier uses that
manifest without retrieving credentials or dumping policies:

```python
import boto3, json, os
client = boto3.client("iam")
role = os.environ["REVIEWED_RUNNER_ROLE_ARN"]
with open(os.environ["REVIEWED_IAM_CASES"]) as stream:
    cases = json.load(stream)
for case in cases:
    result = client.simulate_principal_policy(
        PolicySourceArn=role,
        ActionNames=[case["action"]],
        ResourceArns=[case["resource"]],
        ContextEntries=case.get("ContextEntries", []),
    )["EvaluationResults"][0]
    assert not result.get("MissingContextValues"), case["name"]
    assert result["EvalDecision"] == case["expected"], case["name"]
    print(case["name"], result["EvalDecision"])
```

Required cases include:

- Allowed retained calls on exact Bedrock/ECR/log/SSM/gateway/CodeBuild resources.
- Exact transport GetSecretValue and KMS Decrypt with matching ViaService and
  SecretARN encryption context; deny direct decrypt, wrong service/context/key.
- Denied own-boundary CreatePolicyVersion/SetDefaultPolicyVersion; IAM role/policy
  mutation and boundary removal; STS role assumption; tenant-vault secret access;
  Lambda/service mutation; unrelated resources outside the runtime grants.
- PassRole only for the reviewed service-only PR validation role with the correct
  `iam:PassedToService`; other roles/services refused.

Check live calls as well: simulation does not prove trust, resource policies, SCPs
or service behavior. Test only authorized fixtures; never execute an escalation
merely because its simulation must fail. Record exact expected decision classes
from the reviewed policy instead of accepting arbitrary failure or a 500.

After the bounded configuration is live, remove the specifically audited stale
AdministratorAccess/ReadOnlyAccess attachments if they still exist and no other
owner is racing their management. Re-run effective-permission and normal-job
checks after removal. The boundary must deny escalation even before detachment;
identity policy removal provides additional protection against future drift.

## Failure handling and closure

Pause affected dispatch and correct a specific missing capability through reviewed
source and a fresh narrow plan. An inline Allow cannot override a boundary Deny
or grant an API absent from the boundary. If both identity permission and ceiling
need adjustment, review both and repeat negative checks. Do not restore broad
administrator access, edit the live boundary outside its owner, or move untrusted
jobs to a privileged pool to make them pass.

Attach the compatibility disposition for every shared-pool job, policy hashes,
simulation manifest/results, real ordinary-job receipts and stale-attachment
readback. Keep S14 #5613 and #4725 open until applied-policy and S13 admin-audit
acceptance are complete. A source-only PR or one successful workflow cannot close
that composite scope.
