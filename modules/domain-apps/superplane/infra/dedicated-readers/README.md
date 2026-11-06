# Dedicated workspace reader authority

This app-owned root owns one installation IRSA role, its read-only EKS metadata
policy, and one explicit access entry per selected existing workspace cluster.
It owns no cluster, namespace, worker role or platform resource. Its state key is
`<environment>/modules/superplane-dedicated-readers/terraform.tfstate` in the
existing ADP Terraform backend.

Trust binds the management cluster OIDC provider and exactly
`system:serviceaccount:<namespace>:superplane-dedicated-readers`. There is no
operator assumption, PassRole, IAM mutation, service policy attachment or EKS
admin policy in the runtime authority. Access entries map only to each
workspace's native `superplane:reader:<workspace-id>` group. Actual
Kubernetes delegation belongs to the native bootstrap journal.

Plan with the authorized installation account and review the saved plan before
apply. Where an access coordinator owns IAM, hand this root, exact inputs and
saved plan to that owner. Consume its real outputs; never invent a role ID or
access-entry ARN. Existing journals and cluster infrastructure remain untouched.
See [reader installation](../../workspace_provisioning/dedicated_readers/README.md).
