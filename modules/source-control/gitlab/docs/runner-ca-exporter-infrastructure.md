# Set up the separate CA-exporter role

The maintained identity definition is
[`runners/ca-exporter/infra/`](../runners/ca-exporter/infra/). It creates one
role with trust limited to explicitly selected, existing operator roles in
the same account. The new role has no identity permissions until the GitLab
instance root attaches its resource-specific exporter policy.

Keep this root's state separate from the instance and runner states. Select an
unused key such as `<environment>/modules/gitlab-runner-ca-exporter/terraform.tfstate`
in the deployment's approved encrypted backend. Use private backend and
variable files outside the checkout. Required variables are `account_id`,
`aws_region`, `environment`, and `trusted_operator_role_arns`.

Using the approved setup identity, verify the account and role, initialize the
selected backend and save a plan:

```bash
CA_EXPORTER_ROOT=modules/source-control/gitlab/runners/ca-exporter/infra
aws sts get-caller-identity
terraform -chdir="$CA_EXPORTER_ROOT" init -input=false \
  -backend-config="$CA_EXPORTER_BACKEND"
terraform -chdir="$CA_EXPORTER_ROOT" plan -input=false \
  -var-file="$CA_EXPORTER_VARIABLES" -out="$CA_EXPORTER_PLAN"
```

For initial setup, the plan must contain exactly one role creation, the
reviewed account/environment and the reviewed operator trust. It must not
attach administrator access or grant access to GitLab tokens. The access
owner applies the same saved plan under the existing IAM approval procedure.
Adding this source or preparing a plan does not authorize an apply.

Pass the `runner_ca_exporter_role_name` output to the instance root's input of
the same name. The instance root continues to own its `runner_ca_exporter`
inline policy because it owns the certificate/secret bindings. The exporter
role must stay separate from the registrar and runner-manager identities.
Then follow [the CA export procedure](runner-registration-live.md#export-and-verify-the-ca).

If a role or a local bootstrap state already exists, retain its state and
review a state migration or import before changing ownership. Do not apply a
new empty state against an existing role or apply two saved plans for the
same identity. Unapplied local bootstrap plans should be replaced by a
reviewed plan from this maintained root and the selected backend; preserve
the earlier plan as history.
