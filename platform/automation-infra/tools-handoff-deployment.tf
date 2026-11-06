variable "enable_tools_handoff_deployment" {
  type    = bool
  default = false
}

variable "tools_handoff_rest_api_id" {
  type    = string
  default = ""
  validation {
    condition     = var.tools_handoff_rest_api_id == "" || can(regex("^[a-z0-9]{1,32}$", var.tools_handoff_rest_api_id))
    error_message = "Select one existing private connector API."
  }
}

variable "tools_handoff_stage" {
  type    = string
  default = "dev"
  validation {
    condition     = can(regex("^[A-Za-z0-9_-]{1,128}$", var.tools_handoff_stage))
    error_message = "Select one existing release stage."
  }
}

variable "tools_handoff_connector_target_arns" {
  type    = list(string)
  default = []
  validation {
    condition = alltrue([for arn in var.tools_handoff_connector_target_arns :
      startswith(arn, "${var.tools_gateway_arn}/target/") &&
    can(regex("/target/[A-Za-z0-9_-]+$", arn))])
    error_message = "Select only exact connector targets in the approved gateway."
  }
}

locals {
  tools_handoff_api = "arn:aws:apigateway:${var.aws_region}::/restapis/${var.tools_handoff_rest_api_id}"
  tools_handoff_actions = concat([
    "sts:GetCallerIdentity", "eks:DescribeCluster", "apigateway:GET", "apigateway:PATCH", "bedrock-agentcore:GetGatewayTarget",
    "s3:PutObject",
  ], local.tools_handoff_preparation_actions, local.tools_handoff_target_actions)
}

resource "aws_iam_role" "tools_handoff" {
  count                = var.enable_tools_handoff_deployment ? 1 : 0
  name                 = "${var.name_prefix}-tools-handoff-trusted-deployment"
  max_session_duration = 3600
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity",
    Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn },
    Condition = { StringEquals = {
      "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com",
      "token.actions.githubusercontent.com:sub" = "repo:${var.repository}:environment:adp-tools-handoff-${var.environment}",
    } },
  }] })
}

resource "aws_iam_role_policy" "tools_handoff" {
  count = var.enable_tools_handoff_deployment ? 1 : 0
  name  = "tools-connector-handoff"
  role  = aws_iam_role.tools_handoff[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = "sts:GetCallerIdentity", Resource = "*" },
    { Effect = "Allow", Action = "eks:DescribeCluster", Resource = local.tools_cluster },
    { Effect = "Allow", Action = "apigateway:GET", Resource = local.tools_handoff_reads },
    { Effect = "Allow", Action = "apigateway:PATCH", Resource = local.tools_handoff_patches },
    # AWS authorizes GetGatewayTarget against its parent gateway, not a target ARN.
    # https://docs.aws.amazon.com/service-authorization/latest/reference/list_bedrock-agentcore.html
    { Effect = "Allow", Action = "bedrock-agentcore:GetGatewayTarget", Resource = var.tools_gateway_arn },
    { Effect = "Allow", Action = "s3:PutObject", Resource = concat([local.tools_evidence_objects], var.enable_tools_handoff_preparation ? [local.tools_handoff_journals] : []) },
    { Effect = "Deny", NotAction = local.tools_handoff_actions, Resource = "*" },
    { Effect = "Deny", Action = "eks:DescribeCluster", NotResource = local.tools_cluster },
    { Effect = "Deny", Action = "apigateway:GET", NotResource = local.tools_handoff_reads },
    { Effect = "Deny", Action = "apigateway:PATCH", NotResource = local.tools_handoff_patches },
    { Effect = "Deny", Action = "bedrock-agentcore:GetGatewayTarget", NotResource = var.tools_gateway_arn },
    { Effect = "Deny", Action = "s3:PutObject", NotResource = concat([local.tools_evidence_objects], var.enable_tools_handoff_preparation ? [local.tools_handoff_journals] : []) },
  ] })
  lifecycle {
    precondition {
      condition = (
        var.tools_handoff_rest_api_id != "" && (var.enable_tools_handoff_preparation || length(toset(var.tools_handoff_connector_target_arns)) == 4) &&
        startswith(var.tools_gateway_arn, "arn:aws:bedrock-agentcore:${var.aws_region}:${local.tools_account}:gateway/")
      )
      error_message = "Handoff requires one private API and all four exact, already prepared connector targets in this account."
    }
  }
}

resource "aws_eks_access_entry" "tools_handoff" {
  count             = var.enable_tools_handoff_deployment ? 1 : 0
  cluster_name      = var.cluster_name
  principal_arn     = aws_iam_role.tools_handoff[0].arn
  type              = "STANDARD"
  kubernetes_groups = ["adp:tools-handoff-${var.environment}"]
}

output "tools_handoff_deployment_role_arn" {
  value = var.enable_tools_handoff_deployment ? aws_iam_role.tools_handoff[0].arn : null
}
