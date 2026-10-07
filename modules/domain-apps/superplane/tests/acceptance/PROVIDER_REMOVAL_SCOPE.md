# Demo 1 provider removal evidence

The supervised demo uses the maintained **dedicated workspace with owned networking
and a supplied, retained KMS key**. A saved baseline is captured before requesting
removal approval. The baseline is private observational evidence, not deletion
authority. The authenticated domain workflow retains approval, grant, ownership,
and execution checks.

The source for this bounded inventory is the resource declarations in
[`infra/workspaces/main.tf`](../../infra/workspaces/main.tf),
[`iam.tf`](../../infra/workspaces/iam.tf),
[`eks.tf`](../../infra/workspaces/eks.tf),
[`network.tf`](../../infra/workspaces/network.tf),
[`private_sts.tf`](../../infra/workspaces/private_sts.tf), and
[`node_network.tf`](../../infra/workspaces/node_network.tf).

| Maintained resource or relationship | Baseline and post-removal check |
| --- | --- |
| EKS cluster, workspace security groups, VPC and subnets | Original authenticated applied ownership plus exact provider IDs. |
| EC2 instances, EBS volumes, snapshots, ENIs, EIPs, NAT gateways, route tables, internet gateways, VPC endpoints, launch templates and default security group | Complete bounded workspace-tag listings and owned-VPC listings where supported; instance EBS and ENI EIP attachments are followed. Every recorded ID is read again independently after removal. |
| Cluster, node and CNI IAM roles | Exact `${cluster}-cluster-role`, `${cluster}-node-role`, and `${cluster}-vpc-cni-role` relationships from `iam.tf`; baseline role tags and live EKS references must match. Each role is read by exact name and returned ARN. |
| Optional workspace admin role | Exact `${cluster}-admin`; recorded as present or explicitly absent before approval. It must be absent after removal. |
| Inline role policies and managed-policy attachments | Their owning roles must be absent. IAM role deletion requires inline policies and policy attachments to be removed. AWS-managed policies themselves are shared and are not workspace deletion targets. |
| IAM OIDC provider | Exact provider ARN derived from the original cluster's live issuer URL. The returned provider URL must match. |
| CloudWatch cluster log group | Exact `/aws/eks/${cluster}/cluster` from `main.tf` and `eks.tf`; the baseline key must equal the supplied retained key. All prefix-query pages are read and only the exact group name establishes presence. |
| EKS nodegroups and addons | All children listed under the original cluster, including required `${cluster}-default` and `vpc-cni`; provider-assigned ARNs are recorded. Exact post-removal descriptions must establish absence. |
| Node launch template, Auto Scaling groups and instance profiles | Follow live nodegroup template and Auto Scaling references and the node role's instance-profile list. Record the exact identities and check them independently, including after their parent disappears. |
| Route table associations, security group rules and EKS access entries/associations | Parent route tables, security groups and cluster must be absent. These child configurations cannot survive removal of their parent resource. |
| Supplied KMS key and declared peer | Explicit survivor identities; the live cluster encryption configuration and cluster log group must reference the selected KMS key. The key must still exist and be enabled after removal, and the declared peer must still exist. |
| Workspace-created KMS key/alias | Outside this demo mode: a supplied key is required. Pending KMS deletion is not treated as completed removal. |
| `terraform_data` guards | Local Terraform validation/state objects; no independent cloud resource to delete. |

The Resource Groups Tagging API is an additional bounded census. Its unresolved
entries are accepted only when they match independently checked maintained-plan
identities or declared survivors. CloudWatch's trailing `:*` ARN form is normalized
only for that match. Other unresolved resource types still block verification.
The census itself retains `inventory_complete=false`; the removal report's
`inventory_complete=true` applies only to its explicit `inventory_scope`.
`full_inventory_complete` remains false.

All AWS calls use the selected broker label and verify the account and assumed
role before each provider operation. Calls have individual and total runtime
bounds. Listings follow pagination and refuse malformed, repeated or unfinished
tokens. Denied requests, unknown errors and malformed success responses never
prove absence. Exact-ID NAT gateway, VPC endpoint and launch-template APIs can
establish absence with a complete empty result list; IAM, EKS and KMS exact lookup
errors must match the service's typed missing-resource response and operation.
Complete empty exact-name Logs and Auto Scaling listings also establish absence.

Terminated EC2 instances and deleted NAT gateway tombstones are reported
separately from absent resources. Active leftovers fail the removal check even
when tags were removed or attachments detached. New census identities outside
the saved baseline block verification. The baseline is never refreshed to hide a
leftover during recovery. Its SHA-256 detects changes within the protected private
checkpoint; it is not a provider signature.

A successful result requires both the authenticated original removal operation
and the independent scoped absence/survivor checks. Cost remains unknown. This
is not a global account inventory, a proof of zero billing, or a claim about
resources outside the stated workspace and survivor scope.
