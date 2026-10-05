# =============================================================================
# Workspace outputs — the deterministic handover surface
# Issue #5532 (w6-09) design item 3: "Publish deterministic account/cluster/endpoints/
# trust/identity outputs."
# =============================================================================
# WHAT "DETERMINISTIC" MEANS HERE
#
# Every output below is derived from this module's inputs or from a resource this module
# created. None of them is looked up at read time from a mutable external source, so two
# reads of the same state produce the same answer, and the answer a consumer gets is the one
# the plan was reviewed against.
#
# WHO CONSUMES THESE, AND WHY THE SHAPE MATTERS
#
# #5533 (w6-10) registers this cluster as a workspace target and bootstraps it; #5534 wires
# the `provisioning_provider` port. Both read this state remotely, and both need to reach the
# cluster without being told anything out-of-band. That is why the cluster endpoint, the CA
# certificate and the OIDC issuer are all published: those three are exactly what a
# kubeconfig needs, and a consumer that has to ask a human for one of them is a consumer that
# will hardcode it.
#
# NO SECRET IS PUBLISHED HERE, AND NONE IS MARKED SENSITIVE EITHER
#
# `cluster_certificate_authority_data` looks like a credential and is not one: it is the
# PUBLIC certificate a client uses to verify the API server's identity. It authenticates the
# SERVER to the client, not the client to the server. Marking it sensitive would hide it from
# the plan output that consumers need it from, while protecting nothing.
#
# There is deliberately no output carrying a token, a signing key or a password. The only
# credential-adjacent thing a consumer needs is the ability to assume
# `workspace_admin_role_arn`, which is governed by that role's trust policy and MFA
# condition, not by a value copied out of here.
# =============================================================================

# ---------------------------------------------------------------------------
# Account and target identity
# ---------------------------------------------------------------------------
output "account_id" {
  description = "The AWS account this workspace's infrastructure exists in. Equal to var.account_id — the target guard in main.tf fails the plan if the credentials disagree, so this is the verified account rather than a restated claim."
  value       = data.aws_caller_identity.current.account_id
}

output "aws_region" {
  description = "Region this workspace's infrastructure exists in."
  value       = var.aws_region
}

output "workspace_name" {
  description = "The workspace this state belongs to. Published so a consumer reading remote state can verify it read the workspace it intended, rather than trusting the state key it constructed."
  value       = var.workspace_name
}

output "environment" {
  description = "Environment this workspace belongs to."
  value       = var.environment
}

# ---------------------------------------------------------------------------
# Cluster identity and endpoints
# ---------------------------------------------------------------------------
output "cluster_name" {
  description = "EKS cluster name for this workspace. Derived from the name prefix, not a separate input, so it cannot disagree with the other resource names."
  value       = aws_eks_cluster.workspace.name
}

output "cluster_arn" {
  description = "EKS cluster ARN. The identity IAM policies scope to, and what `WorkspaceInfrastructure.status.clusterArn` in ../account-factory/manifests/ reports."
  value       = aws_eks_cluster.workspace.arn
}

output "cluster_version" {
  description = "The exact Kubernetes minor version running. Equal to var.cluster_version: no moving label was resolved at apply time, so what is running is what was reviewed."
  value       = aws_eks_cluster.workspace.version
}

output "cluster_endpoint" {
  description = "HTTPS endpoint for this workspace's Kubernetes API. Reachable from inside the VPC always; from outside only if cluster_endpoint_public_access is true, within the allowlisted CIDRs."
  value       = aws_eks_cluster.workspace.endpoint
}

output "cluster_certificate_authority_data" {
  description = "Base64 PEM certificate authority for the cluster's API server. PUBLIC — it authenticates the server to a client, and is one of the three values a kubeconfig needs. Deliberately not marked sensitive; see outputs.tf."
  value       = aws_eks_cluster.workspace.certificate_authority[0].data
}

output "cluster_endpoint_public_access" {
  description = "Whether this cluster's API is reachable from outside the VPC. Published so an auditor can answer the exposure question from state without reading the tfvars that produced it."
  value       = aws_eks_cluster.workspace.vpc_config[0].endpoint_public_access
}

output "cluster_endpoint_public_access_cidrs" {
  description = "The address ranges permitted to reach the public API endpoint. Empty when public access is off (variables.tf enforces that pairing, so an empty list here is never a populated-but-ineffective allowlist)."
  value       = aws_eks_cluster.workspace.vpc_config[0].public_access_cidrs
}

output "cluster_security_group_id" {
  description = "The workspace's cluster security group (egress only, no ingress from outside the VPC)."
  value       = aws_security_group.cluster.id
}

# ---------------------------------------------------------------------------
# Trust anchors
# ---------------------------------------------------------------------------
output "cluster_oidc_issuer_url" {
  description = "This cluster's OIDC issuer URL. THIS workspace's trust anchor — distinct from the ADP management cluster's, which the control plane's IRSA roles use. A workspace workload must never be able to assume a control-plane role, and separate issuers are what makes that structural."
  value       = aws_eks_cluster.workspace.identity[0].oidc[0].issuer
}

output "cluster_oidc_provider_arn" {
  description = "ARN of the IAM OIDC provider for this cluster. #5533 (w6-10) scopes per-workload IRSA trust policies to this, so a workspace pod gets its own role instead of sharing the node role's permissions."
  value       = aws_iam_openid_connect_provider.cluster.arn
}

# ---------------------------------------------------------------------------
# Identities
# ---------------------------------------------------------------------------
output "cluster_role_arn" {
  description = "Role EKS assumes to manage this workspace's control plane. Assumable only by eks.amazonaws.com."
  value       = aws_iam_role.cluster.arn
}

output "node_role_arn" {
  description = "Role this workspace's nodes assume. Published because #5533's EKS access entry for the node group must reference it."
  value       = aws_iam_role.node.arn
}

output "workspace_admin_role_arn" {
  description = <<-EOT
    Operator role for reaching THIS cluster's API, scoped to this cluster only. Null when
    neither workspace_admin_principal_arns nor workspace_admin_automation_role_arns names a
    principal — which is the correct state for a workspace whose operator has not been
    named, and is distinguishable from an empty string so a consumer cannot treat "not
    configured" as a valid ARN.

    Holding this role grants API REACHABILITY, not Kubernetes authority. The RBAC/access
    entry is #5533's (w6-10).
  EOT
  value       = length(aws_iam_role.workspace_admin) > 0 ? aws_iam_role.workspace_admin[0].arn : null
}

# ---------------------------------------------------------------------------
# WHO may assume the admin role, and under which condition.
#
# Published because the two are trusted differently and a consumer cannot tell from the role
# ARN alone. An automated caller that reads only `workspace_admin_role_arn` and finds
# AccessDenied has no way to know whether its role was never named or was named in the human
# list, where the MFA condition it cannot satisfy applies. This output answers that directly.
# ---------------------------------------------------------------------------
output "workspace_admin_trust" {
  description = "Which principals may assume this workspace's admin role and under what condition: human principals require MFA, named automation roles do not (a role session cannot present MFA — see iam.tf). A principal in neither list is denied."
  value = {
    human_principals_mfa_required = var.workspace_admin_principal_arns
    automation_roles_no_mfa       = var.workspace_admin_automation_role_arns
    mfa_condition                 = "aws:MultiFactorAuthPresent = true, applied ONLY to the human statement"
    role_created                  = local.workspace_admin_enabled
  }
}

# ---------------------------------------------------------------------------
# Networking — including who owns it
# ---------------------------------------------------------------------------
output "vpc_id" {
  description = "VPC this workspace's cluster runs in: the one this module created in owned mode, or the supplied one it merely read."
  value       = local.vpc_id
}

output "private_subnet_ids" {
  description = "Private subnets the cluster and node group are placed in. Nodes are never placed in a public subnet."
  value       = local.private_subnet_ids
}

output "public_subnet_ids" {
  description = "Public subnets, for egress and any internet-facing load balancer the workspace's owner later creates. Empty in supplied mode — this module does not enumerate a supplied network's public subnets, because it makes no claim about a network it does not own."
  value       = aws_subnet.public[*].id
}

# ---------------------------------------------------------------------------
# THE OWNERSHIP OUTPUT — the one a teardown must read before it acts
#
# This is the machine-readable form of design item 3's "without silently adopting its
# lifecycle", and it exists because the destructive question ("may ADP delete this network?")
# must be answerable WITHOUT reconstructing the request that created the workspace. It mirrors
# `ClusterOwnership` in ../account-factory/account_factory/modes.py, whose rule is that a
# cluster ADP adopted is never deleted by ADP, whichever request produced the adoption.
#
# In supplied mode the answer is "no", and that is true structurally as well as informationally:
# there is no network resource in this module's state to destroy (network.tf gates every one
# of them). This output tells an operator that before they run the destroy, rather than after.
# ---------------------------------------------------------------------------
output "network_ownership" {
  description = "\"adp-created\" when this module created the VPC and may destroy it; \"supplied\" when the VPC was lent by its owner, is only read, and is absent from this module's state entirely."
  value       = local.owns_network ? "adp-created" : "supplied"
}

output "nat_gateway_id" {
  description = "The workspace's single shared NAT gateway (see network.tf for why one rather than one per zone, and what fails when its zone does). Empty in supplied mode."
  value       = local.owns_network ? aws_nat_gateway.workspace[0].id : null
}

output "kms_key_arn" {
  description = "Key envelope-encrypting this workspace's Kubernetes Secrets, control-plane logs and node root volumes: the operator's supplied key, or the workspace-scoped key this module created. Node root-volume encryption with this key is established by aws_launch_template.node, which the node group references — not by an account-level EBS default."
  value       = local.kms_key_arn
}

output "kms_key_is_workspace_managed" {
  description = "True when this module created and owns the key (and therefore wrote its policy); false when the operator supplied one, whose policy remains the operator's to maintain. Read this before concluding anything about the key's permissions from this module's source."
  value       = var.kms_key_arn == ""
}

# ---------------------------------------------------------------------------
# What a SUPPLIED key must already permit.
#
# Published for the mandatory live prepare/apply preflight; Terraform alone cannot verify it. A supplied key is not
# in this state, so its policy stays the operator's -- the same non-adoption rule that governs
# supplied networking. But the requirement is real and the failure is late and misleading: a
# key missing the CloudWatch Logs statement makes the log group fail with
# InvalidParameterException, and one missing the Auto Scaling statements makes every node
# launch fail in a way that presents as a networking fault.
#
# So the statements are rendered here with THIS workspace's real log-group ARN, region and
# account -- not described in prose -- so an operator can diff them against
# `aws kms get-key-policy` output before applying, and the Wave 6 operations evaluator has an
# exact precondition to check rather than an instruction to interpret.
#
# Null when this module created the key, in which case it wrote these statements itself.
# ---------------------------------------------------------------------------
output "supplied_kms_key_required_policy" {
  description = "Statements a SUPPLIED KMS key must already carry for this workspace to work, rendered with this workspace's real ARNs. Null when this module created the key. This module never modifies a supplied key's policy; the maintained prepare/apply entry points verify key state, policy, caller IAM and KMS dry-run authority before any resource apply."
  value = var.kms_key_arn == "" ? null : jsonencode({
    key_arn = var.kms_key_arn
    verify  = "aws kms get-key-policy --key-id ${var.kms_key_arn} --policy-name default --query Policy --output text"
    required_statements = [
      {
        Sid    = "AllowCloudWatchLogsToEncryptThisWorkspacesClusterLogGroup"
        Effect = "Allow"
        Principal = {
          Service = "logs.${var.aws_region}.amazonaws.com"
        }
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:DescribeKey",
        ]
        Resource = "*"
        Condition = {
          ArnEquals = {
            "kms:EncryptionContext:aws:logs:arn" = local.cluster_log_group_arn
          }
        }
      },
      {
        Sid    = "AllowAutoScalingToUseThisKeyForNodeRootVolumes"
        Effect = "Allow"
        Principal = {
          AWS = "arn:${local.partition}:iam::${var.account_id}:role/aws-service-role/autoscaling.amazonaws.com/AWSServiceRoleForAutoScaling"
        }
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:DescribeKey",
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "kms:ViaService" = "ec2.${var.aws_region}.amazonaws.com"
          }
        }
      },
      {
        Sid    = "AllowAutoScalingToCreateGrantsForAttachedVolumes"
        Effect = "Allow"
        Principal = {
          AWS = "arn:${local.partition}:iam::${var.account_id}:role/aws-service-role/autoscaling.amazonaws.com/AWSServiceRoleForAutoScaling"
        }
        Action   = "kms:CreateGrant"
        Resource = "*"
        Condition = {
          Bool = {
            "kms:GrantIsForAWSResource" = "true"
          }
        }
      },
      {
        Sid       = "AllowProvisioningCallerToConfigureEKSEncryption"
        Effect    = "Allow"
        Principal = { AWS = data.aws_iam_session_context.provisioner.issuer_arn }
        Action    = ["kms:DescribeKey", "kms:CreateGrant"]
        Resource  = "*"
      },
    ]
  })
}

output "node_root_volume_encryption" {
  description = "How this workspace's node root volumes are encrypted, as planned: the launch template that establishes it, the key used, and the reviewed size. Published so the claim is checkable against the plan rather than read from a description."
  value = {
    launch_template_name = aws_launch_template.node.name
    encrypted            = true
    kms_key_arn          = local.kms_key_arn
    volume_size_gib      = var.node_volume_size
    volume_type          = "gp3"
    established_by       = "aws_launch_template.node, referenced by aws_eks_node_group.default.launch_template — not by an account-level EBS-encryption-by-default setting, and not by the cluster's Secrets encryption_config, which covers a different store."
  }
}

output "cluster_log_group_name" {
  description = "CloudWatch log group receiving this cluster's control-plane logs, with reviewed retention. Declared explicitly so EKS does not create a never-expiring one implicitly."
  value       = aws_cloudwatch_log_group.cluster.name
}

output "cluster_log_group_arn" {
  description = "ARN of this cluster's control-plane log group. Published because it is the exact value the workspace KMS key's encryption-context condition authorises; an operator supplying their own key needs it to scope the equivalent statement."
  value       = local.cluster_log_group_arn
}

# ---------------------------------------------------------------------------
# THE STATE KEY CONVENTION
#
# Published as an output for the reason ../control-plane/outputs.tf publishes its own: the
# backend block in versions.tf is deliberately empty, so the convention has to be recorded
# somewhere a human and a CI lane can both read it.
#
# Here it carries more weight than it does for the control plane. This module is instantiated
# once per workspace, and the workspace segment below is the ONLY thing separating one
# workspace's record of what exists from another's. Two workspaces initialized against the
# same key would each compute that the other's cluster, VPC and node group are unconfigured
# and should be destroyed — see the note in main.tf on what a diff against a shared record
# means.
#
# tests/test_workspace_backend_state_key.py asserts this output interpolates both identities and that
# the backend block stays empty; tests/backend.tftest.hcl asserts the rendered value. The two
# together keep the convention and the block from drifting apart.
# ---------------------------------------------------------------------------
output "state_key_convention" {
  description = "Required S3 state key for this workspace. Per-workspace by construction: the workspace segment is what keeps one workspace's apply from planning a destroy against another's."
  value       = "${var.environment}/modules/superplane-workspaces/v2/${var.org_id}/${var.workspace_id}/terraform.tfstate"
}

output "provisioning_caller_kms_requirements" {
  description = "Pre-existing identity permissions required by the actual CreateCluster caller, in addition to the owned or supplied key policy. This workspace never manages the caller's IAM policy. Verify the caller's effective permissions before applying; an SCP, boundary or explicit deny still overrides these grants."
  value = {
    principal_arn = data.aws_iam_session_context.provisioner.issuer_arn
    key_arn       = local.kms_key_arn
    identity_policy = jsonencode({
      Version = "2012-10-17"
      Statement = [{
        Effect   = "Allow"
        Action   = ["kms:DescribeKey", "kms:CreateGrant"]
        Resource = local.kms_key_arn
      }]
    })
    grant_condition = "CreateCluster must not be conditioned on kms:GrantIsForAWSResource"
    verification    = "Verify both the caller identity policy and the key policy before apply; the key-policy template alone does not establish caller permissions."
  }
}

output "org_id" { value = var.org_id }
output "workspace_id" { value = var.workspace_id }
output "infrastructure_id" { value = local.infrastructure_id }

output "ownership_tags" { value = local.common_tags }

# Kept separate from key-dependent permission outputs, which are partially unknown on create.
output "provisioning_principal_arn" {
  description = "Canonical IAM principal embedded in the saved plan; known before key creation."
  value       = data.aws_iam_session_context.provisioner.issuer_arn
}

output "tenant_scheduling_prerequisites" {
  description = "Bootstrap must prove these controls before removing the pending taint and registering the workspace as usable. This output is a requirement, not observed readiness."
  value = {
    bootstrap_taint_key = "superplane.aws-e/bootstrap"
    node_imds_hop_limit = 1
    cni_role_arn        = aws_iam_role.vpc_cni.arn
    cni_addon_version   = local.node_network_policy.vpc_cni_version
    required_proofs = [
      "Restricted Pod Security admission in every tenant namespace, with tenant identities unable to change namespace policy labels",
      "Normal tenant pod cannot reach IPv4 or IPv6 IMDS or retrieve node credentials",
      "Tenant hostNetwork, hostPID, privileged and hostPath pod requests rejected by admission",
      "aws-node uses its dedicated IRSA role; node role has no CNI or account-wide ECR permissions",
      "Only after these proofs, remove the bootstrap taint through the bounded bootstrap owner"
    ]
  }
}

# Bootstrap consumes the two distinct SGs; EKS attaches its managed cluster SG
# to managed-node-group instances because the launch template supplies no SGs.
output "workspace_api_security_group_id" {
  description = "Supplemental control-plane security group receiving management API ingress."
  value       = aws_security_group.cluster.id
}

output "workspace_node_security_group_id" {
  description = "EKS-managed security group attached to workspace managed nodes; source of private STS ingress."
  value       = aws_eks_cluster.workspace.vpc_config[0].cluster_security_group_id
}

output "workspace_node_group" {
  description = "Exact applied node-group and launch-template identities for lifecycle verification and read-only recovery."
  value = {
    name                    = aws_eks_node_group.default.node_group_name
    arn                     = aws_eks_node_group.default.arn
    launch_template_id      = aws_launch_template.node.id
    launch_template_version = tostring(aws_launch_template.node.latest_version)
  }
}
