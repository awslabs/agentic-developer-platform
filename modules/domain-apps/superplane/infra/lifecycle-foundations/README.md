# Independently owned lifecycle foundations

This root owns three bootstrap actor roles and a retained encryption key for one
immutable workspace draft. It owns no EKS access entries and gives actor roles no
AWS identity policies. The original, approved bootstrap establishes temporary
Kubernetes grants; retirement has separately approved cleanup grants.

The actor trust names the exact selected provider IAM role. Before any resource
change, live IAM reads must match its independently observed RoleId, the selected
AWS account, and the existing AutoScaling service-linked role. A missing service
role is a real prerequisite; this root does not silently adopt or replace it.

The key policy follows `../workspaces/eks.tf` and its
`supplied_kms_key_required_policy` output: exact EKS provisioning caller,
workspace-specific Logs encryption context, and the distinct EC2/AutoScaling
conditions. Key administration remains delegated to the selected account's IAM.
The key and roles have `prevent_destroy`; retiring a workspace preserves them.
Their tags identify this separate foundation owner, and `retained_owner_inventory`
reports the resources that must remain visible after workspace retirement.

Create the workspace draft first. All variables are required: `account_id`,
`aws_region`, `environment`, actual immutable `org_id` and `workspace_id`,
`provider_role_arn`, independently read `expected_provider_role_id`, and three
explicit, distinct `actor_role_names` (`registrar`, `installer`, `supervisor`).
Do not copy example identities into an executable deployment. Review that the
selected provider already has the exact permissions printed by
`provider_identity_requirements`; this module does not edit that provider's policy.

Use the canonical ADP deployment account confirmation and saved-plan review flow
from `docs/adp-platform-deployment/deploy-with-agent.md`. This is a separate state
root. Its S3 backend key must be:

```
<environment>/modules/superplane-lifecycle-foundations/v1/<org_id>/<workspace_id>/terraform.tfstate
```

Initialize with the existing reviewed encrypted backend configuration. Save the
plan with `terraform plan -input=false -var-file=<reviewed-inputs> -out=<private-plan>`;
render that exact plan using `terraform show -json <private-plan>` for review.
Record the source revision and SHA-256 of the saved plan alongside the verified
account/role identities. Apply only that reviewed saved plan. Keep plan/state
files private; do not commit them. Before apply, recheck the active account and
provider RoleId against the review. No actual role or key ARN exists merely
because this source or a plan has been generated.

After apply, use real Terraform outputs for runtime `actor_role_names` and
workspace `workspace_variables.kms_key_arn`. Read back IAM RoleIds/trust policies,
KMS key state/policy, and the alias target under the actual selected credentials.
The original lifecycle code rechecks actor identity before every use. Following
workspace retirement, verify this root's exact role/key/alias identities still
exist and report them as retained independently owned resources, never as zero
cloud resources. A future foundation teardown needs its own reviewed lifecycle;
KMS PendingDeletion remains a real resource throughout the AWS deletion window.

Offline verification (no AWS operations):

```
terraform init -backend=false -input=false
terraform validate
terraform test
```
