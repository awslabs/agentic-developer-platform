data "aws_caller_identity" "current" {}
data "aws_iam_role" "gateway" { name = basename(var.gateway_role_arn) }

locals {
  infrastructure_id = substr(sha256(jsonencode([var.org_id, var.workspace_id])), 0, 32)
  workspace_name    = "adp-${var.environment}-spw-${local.infrastructure_id}"
  provider_name     = "adp-${var.environment}-spp-${local.infrastructure_id}"
  child_roles       = [for suffix in ["cluster-role", "node-role", "vpc-cni-role", "admin"] : "arn:aws:iam::${var.account_id}:role/${local.workspace_name}-${suffix}"]
  ec2_prefix        = "arn:aws:ec2:${var.aws_region}:${var.account_id}"
  eks_prefix        = "arn:aws:eks:${var.aws_region}:${var.account_id}"
  cluster_arn       = "${local.eks_prefix}:cluster/${local.workspace_name}"
  owned_tags        = { OrgId = var.org_id, WorkspaceId = var.workspace_id }
  owner_tags = {
    ManagedBy              = "superplane-domain-provider"
    SuperplaneInstallation = var.installation_id
    ProviderOrgId          = var.org_id
    ProviderWorkspaceId    = var.workspace_id
  }
  regional       = { "aws:RequestedRegion" = var.aws_region }
  owned_resource = { "aws:ResourceTag/OrgId" = var.org_id, "aws:ResourceTag/WorkspaceId" = var.workspace_id }
  owned_request  = { "aws:RequestTag/OrgId" = var.org_id, "aws:RequestTag/WorkspaceId" = var.workspace_id }
  operation      = { "aws:PrincipalTag/adp:agent_id" = "superplane-operation" }
}
resource "terraform_data" "verified_gateway" {
  input = { arn = var.gateway_role_arn, role_id = var.gateway_role_id }
  lifecycle {
    precondition {
      condition     = data.aws_caller_identity.current.account_id == var.account_id && data.aws_iam_role.gateway.arn == var.gateway_role_arn && data.aws_iam_role.gateway.unique_id == var.gateway_role_id
      error_message = "Live account or Gateway immutable identity differs from the reviewed target."
    }
  }
}
resource "aws_iam_role" "provider" {
  name                 = local.provider_name
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = var.gateway_role_arn }
      Action    = ["sts:AssumeRole", "sts:TagSession"]
      Condition = {
        StringEquals = {
          "sts:ExternalId"              = var.external_id
          "aws:RequestTag/adp:user_id"  = var.beneficiary_user_id
          "aws:RequestTag/adp:agent_id" = ["superplane-operation", "superplane-provider-validation"]
          "aws:RequestTag/adp:persona"  = ["superplane-operation", "superplane-provider-validation"]
        }
        StringLike                  = { "aws:RequestTag/adp:task_id" = "?*" }
        "ForAllValues:StringEquals" = { "aws:TagKeys" = ["adp:user_id", "adp:agent_id", "adp:task_id", "adp:persona"] }
      }
    }]
  })
  depends_on = [terraform_data.verified_gateway]
}
resource "aws_secretsmanager_secret" "external_id" {
  name                    = "/adp/${var.environment}/superplane/provider/${local.infrastructure_id}"
  recovery_window_in_days = 30
}
resource "aws_secretsmanager_secret_version" "external_id" {
  secret_id     = aws_secretsmanager_secret.external_id.id
  secret_string = jsonencode({ role_arn = aws_iam_role.provider.arn, account_id = var.account_id, external_id = var.external_id })
}
resource "aws_iam_policy" "child_boundary" {
  name        = "${local.provider_name}-child-boundary"
  description = "Maximum workspace service permissions; no IAM, STS chaining, secrets, state or platform-resource writes."
  policy      = jsonencode(local.child_boundary)
}
# Four exact policies avoid IAM's 10,240-byte aggregate inline-role limit.
# Their full documents and current DefaultVersionIds are pinned by the protected
# authority owner; a new attachment or version invalidates current delegation.
resource "aws_iam_policy" "execution" {
  for_each = var.execution == null ? {} : local.policy_shard_indexes
  name     = "${local.provider_name}-${each.key}"
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [for index in each.value : local.execution_policy.Statement[index]]
  })
  lifecycle {
    precondition {
      condition     = length(jsonencode({ Version = "2012-10-17", Statement = [for index in each.value : local.execution_policy.Statement[index]] })) <= 6144
      error_message = "Provider policy shard exceeds AWS's managed-policy quota."
    }
  }
}
resource "aws_iam_role_policy_attachment" "execution" {
  for_each   = aws_iam_policy.execution
  role       = aws_iam_role.provider.name
  policy_arn = each.value.arn
}
