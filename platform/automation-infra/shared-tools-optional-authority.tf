variable "shared_tools_vpc_id" {
  description = "Existing approved VPC for the optional shared Lambda security group."
  type        = string
  default     = ""
  validation {
    condition     = var.shared_tools_vpc_id == "" || can(regex("^vpc-[a-f0-9]{8,17}$", var.shared_tools_vpc_id))
    error_message = "Supply an existing VPC ID, never a wildcard."
  }
}

variable "shared_tools_websearch_gateway_arn" {
  description = "Existing approved gateway for optional web search target changes; creating a gateway requires separate approval."
  type        = string
  default     = ""
  validation {
    condition     = var.shared_tools_websearch_gateway_arn == "" || can(regex("^arn:aws:bedrock-agentcore:${var.aws_region}:[0-9]{12}:gateway/[A-Za-z0-9_-]+$", var.shared_tools_websearch_gateway_arn))
    error_message = "Select an exact same-region gateway ARN."
  }
}

resource "aws_iam_role_policy" "shared_tools_network" {
  count = var.enable_shared_tools_deployment && var.shared_tools_vpc_id != "" ? 1 : 0
  name  = "shared-tools-selected-vpc"
  role  = aws_iam_role.shared_tools_deployment[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = ["ec2:CreateSecurityGroup", "ec2:DeleteSecurityGroup", "ec2:AuthorizeSecurityGroupEgress", "ec2:RevokeSecurityGroupEgress", "ec2:DescribeSecurityGroups"],
    Resource = ["arn:aws:ec2:${var.aws_region}:${local.shared_account}:vpc/${var.shared_tools_vpc_id}",
    "arn:aws:ec2:${var.aws_region}:${local.shared_account}:security-group/*"],
    Condition = { StringEquals = { "ec2:Vpc" = "arn:aws:ec2:${var.aws_region}:${local.shared_account}:vpc/${var.shared_tools_vpc_id}" } },
  }] })
}

resource "aws_iam_role_policy" "shared_tools_gateway" {
  count = var.enable_shared_tools_deployment && var.shared_tools_websearch_gateway_arn != "" ? 1 : 0
  name  = "shared-tools-selected-gateway"
  role  = aws_iam_role.shared_tools_deployment[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect   = "Allow", Action = ["bedrock-agentcore:GetGateway", "bedrock-agentcore:ListGatewayTargets", "bedrock-agentcore:GetGatewayTarget", "bedrock-agentcore:UpdateGatewayTarget"],
    Resource = [var.shared_tools_websearch_gateway_arn, "${var.shared_tools_websearch_gateway_arn}/target/*"],
    }, {
    # Gateway refresh reads tags on the gateway itself, never its targets.
    Effect   = "Allow", Action = "bedrock-agentcore:ListTagsForResource",
    Resource = var.shared_tools_websearch_gateway_arn,
  }] })
}
