# GitLab deployment identity

The `infra` root owns the GitLab module's GitHub OIDC deployment role. Use a
separate backend key such as `<environment>/modules/gitlab-deployment/terraform.tfstate`.
Provide the exact `account_id`, `aws_region`, `environment`, and `repository`.
It reuses the account's existing GitHub OIDC provider and creates one role with
**no service permissions**. Review and apply its saved plan through the account's
IAM coordinator.

Before use, create the GitHub environment returned by `protected_environment`
with a custom deployment branch rule permitting only `main`. Set its
`ADP_DEPLOY_ROLE_ARN` to `deployment_role_arn` and `ADP_DEPLOY_REGION` to the
reviewed region. The three GitLab delivery workflows use this environment. The
environment rule is essential: the OIDC environment subject does not encode the
branch. Preserve any separately required deployment approval gates.

The instance root owns registrar permissions and token resource policies. Set
its `runner_registrar_role_name` to `deployment_role_name` and include the exact
ARN in its reviewed `runner_registrar_admin_role_arns`. Keep portal operators
unchanged: registrar authority must not exempt the deployment role from the
gateway signing-key denial. Use real secret ARNs produced by that root; never
guess suffixes. The CA exporter remains a separate identity. Update
its existing root's `trusted_operator_role_arns` only after this role exists, and
review the exact exporter AssumeRole grant separately. Set the optional
`ca_exporter_role_name` to the existing exporter name to prepare that one-policy
plan; the default empty value keeps the initial setup permissionless. Both sides
of this delegation need review: the exporter root owns trust, and this root owns
the exact `sts:AssumeRole` grant.

This identity setup does not grant IAM writes, PassRole, Kubernetes access or
permission to deploy infrastructure. Those need reviewed plans with actual
resource bindings. Retain the original instance, exporter, builder and runner
state keys. Do not replay their earlier creation plans. Configuring this role
alone is not proof of registration, working CI or live qualification.

## Registration workflow permissions

The optional registration inputs below let this root own the protected
workflow's remaining permissions. They default off. Populate them from the
reviewed repository, live resource inventory and immutable input receipt:

| Input | Scope |
| --- | --- |
| `registration_readback` | Resource-policy reads for the admin and two group secrets; six per-run registration evidence objects and the repository's signed delivery object. |
| `registration_state_access` | Read/write the original GitLab state, read its platform remote state, and use only the existing GitLab DynamoDB lock and checksum rows. |
| `registration_input_versions` | Read exactly one saved plan, variables file and approved CA root, each restricted to its S3 version ID. |
| `registration_refresh` | Read existing GitLab resources for an ordinary Terraform refresh. A separate managed policy avoids the role's cumulative inline-policy limit. |
| `registration_context_repository_id` | Publish the registration workflow's signed, immutable deployment context so the controller can identify its run and target. |

Registration evidence writes require literal `If-None-Match: *`; the six
registration objects also require `AES256`. Input object keys are
`gitlab-registration/inputs/<sha256>/<filename>` in the environment's private,
versioned deployment evidence bucket. Supported filenames are `tfplan`,
`variables.tfvars.json` and `approved-root.pem`. Upload with create-only
preconditions and verify the exact returned version and bytes before binding.
Keep plans, variables and state private: Terraform can include credentials.

The refresh inventory contains exact role/profile, instance, secret and SSM
parameter ARNs. Backup, DNS and Cognito bindings are optional when absent from
that environment. Refresh reads the existing OIDC SecureString and Cognito
client secret because those values are part of the module's Terraform state;
it does not add Secrets Manager value reads. Enumerated EC2, ELB, Cognito and
SSM discovery calls require `Resource: *` and are region constrained. Route 53
zone discovery is global; subsequent reads bind the exact zone. This read set
matches the current module with AWS provider 5.100.0 and an on-demand M6i
instance; review it if the resource inventory or provider changes.

When the coordinator has already applied a policy, import it into this original
deployment backend before an ordinary replan. Inline policy import IDs are
`<deployment-role-name>:<policy-name>` at
`aws_iam_role_policy.registration_readback[0]`,
`aws_iam_role_policy.registration_state[0]` and
`aws_iam_role_policy.registration_inputs[0]`. Import the refresh managed policy
ARN at `aws_iam_policy.registration_refresh[0]`, then its
`<deployment-role-name>/<policy-arn>` attachment at
`aws_iam_role_policy_attachment.registration_refresh[0]`. Never replay a saved
create plan after an external apply. Verify a zero-change replan and preserve
existing role trust and policies. Actual workflow verification must use the
protected environment; policy simulation and devbox reads are not runtime proof.

These inputs do not grant instance changes or remove the saved-plan approval
gate. Keep an unapproved instance plan marked as gated even when its input
objects and IAM prerequisites are ready.

The separate `reviewed_instance_update` input remains null until the owner
approves the exact instance, source revision and saved-plan digest. Its policy
permits stop/start of that instance and `ModifyInstanceAttribute` only when
`ec2:Attribute` is `userData`. The protected registration configuration must
carry the same approval before the workflow can apply. Updating user data may
stop and start the instance; it does not rerun cloud-init automatically. Review
service health, retained volumes and recent backups before enabling it.
An explicit RFC3339 `expires_at` bounds every statement with `aws:CurrentTime`.
After the operation is verified, set this input back to null and have the IAM
coordinator remove only this temporary inline policy, then reconcile state.
Retain the reviewed pre-change metadata and service-restoration handoff until
the instance is running and its HTTPS health checks pass.
