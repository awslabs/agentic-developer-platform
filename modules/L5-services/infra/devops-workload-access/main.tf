# Operator-owned prerequisites for an existing workload. No function, log group,
# connection trust or existing policy is changed. ADP retains run authority.
locals {
  prefix     = "arn:aws:aidevops:${var.aws_region}:${var.aws_account_id}:agentspace/"
  space_arn  = "${local.prefix}${var.native_space_id}"
  reader_arn = "arn:aws:iam::${var.aws_account_id}:role/${var.name}-reader"
  region     = { StringEquals = { "aws:RequestedRegion" = var.aws_region } }
  setup_statements = [for statement in [
    {
      Effect = "Allow", Action = ["aidevops:CreateAgentSpace", "aidevops:TagResource"], Resource = "${local.prefix}*"
      Condition = {
        StringEquals                = { "aws:RequestedRegion" = var.aws_region, "aws:RequestTag/adp:scope" = var.setup_fingerprint }
        "ForAllValues:StringEquals" = { "aws:TagKeys" = ["adp:scope"] }
      }
    },
    {
      Effect    = "Allow", Action = ["aidevops:GetAgentSpace", "aidevops:ListGoals", "aidevops:UpdateGoal"], Resource = "${local.prefix}*"
      Condition = { StringEquals = { "aws:RequestedRegion" = var.aws_region, "aws:ResourceTag/adp:scope" = var.setup_fingerprint } }
    },
    { Effect = "Allow", Action = ["aidevops:ListAgentSpaces"], Resource = "*", Condition = local.region }
  ] : statement if var.setup_fingerprint != "" && var.native_space_id == ""]
  workload_statements = [for statement in [
    {
      Effect = "Allow", Action = [for action in [
        "GetAgentSpace", "ListGoals", "ListAssociations", "GetAssociation",
        "CreateBacklogTask", "UpdateBacklogTask", "GetBacklogTask", "ListBacklogTasks",
        "ListExecutions", "ListJournalRecords", "ListRecommendations", "GetRecommendation"
      ] : "aidevops:${action}"], Resource = local.space_arn, Condition = local.region
    },
    {
      Effect    = "Allow", Action = ["aidevops:AssociateService"], Resource = local.space_arn,
      Condition = { StringEquals = { "aws:RequestedRegion" = var.aws_region, "aidevops:ServiceType" = "aws" } }
    },
    {
      Effect   = "Allow", Action = ["iam:GetRole", "iam:ListRolePolicies", "iam:GetRolePolicy", "iam:ListAttachedRolePolicies", "iam:SimulatePrincipalPolicy"],
      Resource = local.reader_arn
    },
    {
      Effect    = "Allow", Action = ["iam:PassRole"], Resource = local.reader_arn,
      Condition = { StringEquals = { "iam:PassedToService" = "aidevops.amazonaws.com" } }
    },
    { Effect = "Allow", Action = ["lambda:GetFunction"], Resource = var.lambda_arn, Condition = local.region },
    { Effect = "Allow", Action = ["logs:FilterLogEvents"], Resource = var.log_group_arn, Condition = local.region },
    # GetAssociation requires the parent space and this second resource type.
    { Effect = "Allow", Action = ["aidevops:GetAssociation"], Resource = "${local.space_arn}/association/*", Condition = local.region }
  ] : statement if var.native_space_id != ""]
}
resource "aws_iam_role_policy" "connection" {
  count  = length(local.setup_statements) + length(local.workload_statements) > 0 ? 1 : 0
  name   = var.name
  role   = var.connection_role_name
  policy = jsonencode({ Version = "2012-10-17", Statement = concat(local.setup_statements, local.workload_statements) })
  lifecycle {
    precondition {
      condition     = can(regex("^arn:aws:lambda:${var.aws_region}:${var.aws_account_id}:function:[A-Za-z0-9_-]+$", var.lambda_arn)) && can(regex("^arn:aws:logs:${var.aws_region}:${var.aws_account_id}:log-group:[^*]+:\\*$", var.log_group_arn))
      error_message = "The exact Lambda and log group must belong to the selected account and region."
    }
  }
}
resource "aws_iam_role" "reader" {
  count = var.native_space_id != "" ? 1 : 0
  name  = "${var.name}-reader"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = "sts:AssumeRole", Principal = { Service = "aidevops.amazonaws.com" },
    Condition = { StringEquals = { "aws:SourceAccount" = var.aws_account_id }, ArnEquals = { "aws:SourceArn" = local.space_arn } }
  }] })
}
resource "aws_iam_role_policy" "reader" {
  count = var.native_space_id != "" ? 1 : 0
  name  = "selected-workload-reads"
  role  = aws_iam_role.reader[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = "lambda:GetFunction", Resource = var.lambda_arn },
    { Effect = "Allow", Action = "logs:FilterLogEvents", Resource = var.log_group_arn }
  ] })
}
output "resource_role_arn" { value = var.native_space_id != "" ? local.reader_arn : null }
