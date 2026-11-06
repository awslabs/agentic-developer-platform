# Operator-owned prerequisite. Worker/release roles cannot expand connection IAM.
# This root changes neither the role's trust policy nor its other policies.
locals {
  regional         = "${var.aws_region}:${var.aws_account_id}"
  region_condition = { StringEquals = { "aws:RequestedRegion" = var.aws_region } }
  security_statements = [for statement in concat([
    {
      Sid       = "SecurityNativeDiscoveryAndCreation"
      Effect    = "Allow"
      Action    = ["securityagent:CreateAgentSpace", "securityagent:ListAgentSpaces"]
      Resource  = ["*"]
      Condition = local.region_condition
    },
    {
      Sid    = "SecurityNativeLifecycle"
      Effect = "Allow"
      Action = [for operation in [
        "BatchGetAgentSpaces", "UpdateAgentSpace", "DeleteAgentSpace",
        "CreateCodeReview", "StartCodeReviewJob", "BatchGetCodeReviewJobs", "ListCodeReviewJobsForCodeReview",
        "ListCodeReviews", "BatchGetCodeReviews", "ListCodeReviewJobTasks", "ListFindings", "BatchGetFindings", "StopCodeReviewJob",
        "CreateThreatModel", "ListThreatModels", "BatchGetThreatModels", "UpdateThreatModel",
        "AddArtifact", "ListArtifacts", "StartThreatModelJob", "BatchGetThreatModelJobs", "ListThreatModelJobs",
        "ListThreatModelJobTasks", "ListThreats", "BatchGetThreats", "StopThreatModelJob"
      ] : "securityagent:${operation}"]
      Resource  = ["arn:aws:securityagent:${local.regional}:agent-space/*"]
      Condition = local.region_condition
    },
    {
      Sid      = "SecuritySetupIntentTagging"
      Effect   = "Allow"
      Action   = ["securityagent:TagResource"]
      Resource = ["arn:aws:securityagent:${local.regional}:agent-space/*"]
      Condition = {
        StringEquals                = { "aws:RequestedRegion" = var.aws_region }
        "ForAllValues:StringEquals" = { "aws:TagKeys" = ["adp-setup-operation"] }
        Null                        = { "aws:RequestTag/adp-setup-operation" = "false" }
      }
    }
    ], length(var.security.source_bucket_names) == 0 ? [] : [{
      Sid      = "SecurityRegisteredSourceBytes"
      Effect   = "Allow"
      Action   = ["s3:GetObject", "s3:PutObject"]
      Resource = [for bucket in sort(tolist(var.security.source_bucket_names)) : "arn:aws:s3:::${bucket}/security-agent/source/*"]
      }], length(var.security.execution_role_arns) == 0 ? [] : [{
      Sid       = "SecuritySelectedExecutionRoles"
      Effect    = "Allow"
      Action    = ["iam:PassRole"]
      Resource  = sort(tolist(var.security.execution_role_arns))
      Condition = { StringEquals = { "iam:PassedToService" = "securityagent.amazonaws.com" } }
  }]) : statement if var.security.enabled]

  finops_statements = [for statement in [
    {
      Sid       = "FinOpsNativeDiscovery"
      Effect    = "Allow"
      Action    = ["finops-agent:ListAgentSpaces", "finops-agent:ListConnections", "finops-agent:GetConnection", "finops-agent:ListIntegrations"]
      Resource  = ["*"]
      Condition = local.region_condition
    },
    {
      Sid       = "FinOpsNativeWorkspaceAccess"
      Effect    = "Allow"
      Action    = ["finops-agent:GetAgentSpace", "finops-agent:CreateOneTimeLoginSession"]
      Resource  = ["*"]
      Condition = local.region_condition
    },
    {
      Sid      = "FinOpsSelectedBillingViews"
      Effect   = "Allow"
      Action   = ["ce:GetCostAndUsage", "billing:GetBillingView", "billing:GetBillingViewData"]
      Resource = sort(tolist(var.finops.billing_view_arns))
    },
    {
      Sid      = "FinOpsSelectedDataRoleReadback"
      Effect   = "Allow"
      Action   = ["iam:GetRole", "iam:ListRolePolicies", "iam:GetRolePolicy", "iam:ListAttachedRolePolicies", "iam:SimulatePrincipalPolicy"]
      Resource = sort(tolist(var.finops.data_role_arns))
    },
    {
      Sid      = "FinOpsPolicyMetadataReadback"
      Effect   = "Allow"
      Action   = ["iam:GetPolicy", "iam:GetPolicyVersion"]
      Resource = ["arn:aws:iam::${var.aws_account_id}:policy/*", "arn:aws:iam::aws:policy/*"]
    }
  ] : statement if length(var.finops.agent_arns) > 0]
  devops_statements = [for statement in [
    {
      Sid    = "DevOpsSelectedNativeSpaces"
      Effect = "Allow"
      Action = [for operation in [
        "GetAgentSpace", "ListGoals", "GetBacklogTask", "ListBacklogTasks", "ListExecutions",
        "ListJournalRecords", "ListRecommendations", "GetRecommendation", "GetAssociation",
        "CreateBacklogTask", "UpdateBacklogTask", "CreateOneTimeLoginSession"
      ] : "aidevops:${operation}"]
      Resource  = sort(tolist(var.devops.agent_space_arns))
      Condition = local.region_condition
    },
    {
      Sid       = "DevOpsSelectedAssociations"
      Effect    = "Allow"
      Action    = ["aidevops:GetAssociation"]
      Resource  = [for arn in sort(tolist(var.devops.agent_space_arns)) : "${arn}/association/*"]
      Condition = local.region_condition
    }
  ] : statement if length(var.devops.agent_space_arns) > 0]
  devops_knowledge_statements = [for statement in [
    {
      Sid       = "DevOpsSelectedKnowledgeAssets"
      Effect    = "Allow"
      Action    = [for operation in ["CreateAsset", "UpdateAsset", "GetAsset", "GetAssetContent", "ListAssets"] : "aidevops:${operation}"]
      Resource  = sort(tolist(var.devops.knowledge_agent_space_arns))
      Condition = local.region_condition
    }
  ] : statement if length(var.devops.knowledge_agent_space_arns) > 0]
  statements = concat(local.security_statements, local.finops_statements, local.devops_statements, local.devops_knowledge_statements)
}

resource "aws_iam_role_policy" "native_services" {
  count  = length(local.statements) == 0 ? 0 : 1
  name   = "adp-${var.environment}-l5-native-services"
  role   = var.connection_role_name
  policy = jsonencode({ Version = "2012-10-17", Statement = local.statements })
}

output "connection_native_policy" {
  value = one(aws_iam_role_policy.native_services[*].name)
}
