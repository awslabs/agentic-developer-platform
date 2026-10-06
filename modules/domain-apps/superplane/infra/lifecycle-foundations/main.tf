# Separately owned bootstrap actors and retained encryption key. This state must
# never be included in a workspace's saved Terraform destroy plan.
data "aws_caller_identity" "current" {}
data "aws_iam_role" "provider" {
  name = basename(var.provider_role_arn)
}
data "aws_iam_role" "autoscaling" {
  name = "AWSServiceRoleForAutoScaling"
}

locals {
  infrastructure_id = substr(sha256(jsonencode([var.org_id, var.workspace_id])), 0, 32)
  cluster_name      = "adp-${var.environment}-spw-${local.infrastructure_id}"
  cluster_log_arn   = "arn:aws:logs:${var.aws_region}:${var.account_id}:log-group:/aws/eks/${local.cluster_name}/cluster"
  autoscaling_arn   = "arn:aws:iam::${var.account_id}:role/aws-service-role/autoscaling.amazonaws.com/AWSServiceRoleForAutoScaling"
  # These identify the independent owner. OrgId/WorkspaceId identify resources
  # charged to the workspace allocation and are deliberately NOT used here.
  owner_tags = {
    ManagedBy                       = "superplane-lifecycle-foundations"
    Environment                     = var.environment
    SuperplaneFoundationOrgId       = var.org_id
    SuperplaneFoundationWorkspaceId = var.workspace_id
    Lifecycle                       = "retained-independent-owner"
  }
  cryptographic_actions = ["kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:DescribeKey"]
}

resource "terraform_data" "verified_owner" {
  input = {
    account_id       = var.account_id
    provider_role    = var.provider_role_arn
    provider_role_id = var.expected_provider_role_id
    org_id           = var.org_id
    workspace_id     = var.workspace_id
  }
  lifecycle {
    precondition {
      condition = (
        data.aws_caller_identity.current.account_id == var.account_id &&
        data.aws_iam_role.provider.arn == var.provider_role_arn &&
        data.aws_iam_role.provider.unique_id == var.expected_provider_role_id &&
        data.aws_iam_role.autoscaling.arn == local.autoscaling_arn
      )
      error_message = "Live account/provider RoleId/AutoScaling service-linked role differs from the reviewed owner."
    }
  }
}

resource "aws_iam_role" "actor" {
  for_each             = var.actor_role_names
  name                 = each.value
  description          = "Superplane ${each.key}: temporary Kubernetes authority from approved bootstrap journals only"
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyReviewedWorkspaceProvider"
      Effect    = "Allow"
      Principal = { AWS = var.provider_role_arn }
      Action    = "sts:AssumeRole"
    }]
  })
  tags       = merge(local.owner_tags, { BootstrapActor = each.key })
  depends_on = [terraform_data.verified_owner]
  lifecycle {
    prevent_destroy = true
  }
  # No IAM permission policies and no standing EKS access mappings. Actor tokens
  # identify these roles; the operation journal owns their temporary Kubernetes grants.
}

resource "aws_kms_key" "retained" {
  description             = "Independently retained encryption key for ${local.cluster_name}"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "KeyAdministrationAndRecovery"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${var.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid       = "ReviewedProvisionerConfiguresEKSEncryption"
        Effect    = "Allow"
        Principal = { AWS = var.provider_role_arn }
        Action    = ["kms:DescribeKey", "kms:CreateGrant"]
        Resource  = "*"
      },
      {
        Sid       = "ExactWorkspaceClusterLogs"
        Effect    = "Allow"
        Principal = { Service = "logs.${var.aws_region}.amazonaws.com" }
        Action    = local.cryptographic_actions
        Resource  = "*"
        Condition = { ArnEquals = { "kms:EncryptionContext:aws:logs:arn" = local.cluster_log_arn } }
      },
      {
        Sid       = "AutoScalingNodeVolumesThroughRegionalEC2"
        Effect    = "Allow"
        Principal = { AWS = local.autoscaling_arn }
        Action    = local.cryptographic_actions
        Resource  = "*"
        Condition = { StringEquals = { "kms:ViaService" = "ec2.${var.aws_region}.amazonaws.com" } }
      },
      {
        Sid       = "AutoScalingGrantsOnlyForAWSResources"
        Effect    = "Allow"
        Principal = { AWS = local.autoscaling_arn }
        Action    = "kms:CreateGrant"
        Resource  = "*"
        Condition = { Bool = { "kms:GrantIsForAWSResource" = "true" } }
      }
    ]
  })
  tags       = local.owner_tags
  depends_on = [terraform_data.verified_owner]
  lifecycle {
    prevent_destroy = true
  }
}
resource "aws_kms_alias" "retained" {
  name          = "alias/${local.cluster_name}-retained"
  target_key_id = aws_kms_key.retained.key_id
}
