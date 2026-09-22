# ADP-Managed Deploy

> **Status: unavailable for cross-account customer bootstrap.** Do not run a
> platform deployment with a linked AWS credential or a `customer_account`
> block. Use the [self-managed deployment](./self-managed-deploy.md) with
> temporary, customer-controlled bootstrap credentials.

## Why This Track Is Disabled

The gateway currently registers two customer role purposes:

- personal, user-bound read-only account inspection; and
- shared Bedrock model invocation.

Both are steady-state roles. Neither role authorizes platform provisioning.
The credential broker resolves the exact role ARN stored for the selected
`aws_label`; installing another role does not register or select it. The gateway
IRSA policy likewise authorizes only the registered customer-role name contract.

Do not attach `AdministratorAccess` or another provisioning policy to a linked
role. An out-of-stack attachment survives deletion of the connection stack and
permits access that the installed template does not show. Administrator access
also permits role-grant escalation, unconstrained use of pre-existing customer
roles, access to unrelated customer data, and destructive APIs.

The published `aws_role_deploy_v1.yaml` contract addresses the enforceable
foundation subset: it has no administrator policy, denies every IAM mutation,
uses exact control-plane actions, scopes writes by deployment name or ownership
tag, and exposes four default-off capability switches. It cannot create roles
or complete an ADP installation. Publication is evidence for review, not
activation.

See [Customer AWS Role Setup](./customer-aws-setup.md) for the supported operation
inventory and trust contracts.

## Supported Deployment Path

Follow [Deploy With an Agent](./deploy-with-agent.md) and
[Self-Managed Deploy](./self-managed-deploy.md). In that path, the customer
chooses and controls the temporary bootstrap identity instead of converting a
steady-state ADP connection into an administrator role.

Before Phase 1:

1. Confirm the active AWS identity and target account as required by the
   canonical deployment guide.
2. Use credentials created for bootstrap, not an ADP inspection or routing role.
3. Apply customer IAM restrictions appropriate to the deployment inventory.
4. Remove the bootstrap credentials after deployment; steady-state ADP access
   continues through its separately scoped roles.

No customer role changes are applied by this repository change.

## Requirements to Re-enable

The foundation action inventory, trust checks, capability switches, and
fail-closed legacy loader now ship. Hosted execution remains disabled until
these integration pieces ship and are tested together:

- a connect-time deploy tier with tenant-bound credential registration;
- explicit deployment request selection and server-side tier validation;
- gateway `sts:AssumeRole` and `sts:TagSession` authorization scoped to the
  registered deploy role ARN contract;
- an allowlisted role-provisioning mechanism that validates initial trust policies;
- a verified least-privilege inventory for the remaining deployment phases;
- authorization of requested phases against the registered capability set; and
- integration tests proving selection plus positive deployment and teardown
  behavior under the exact generated policy.

The template must not be presented as a runnable hosted path while those
prerequisites remain incomplete. The shared deployment action rejects legacy
customer-account inputs before any plan, apply, or destroy command.
