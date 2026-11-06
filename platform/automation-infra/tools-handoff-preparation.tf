variable "enable_tools_handoff_preparation" {
  type    = bool
  default = false
}

variable "tools_handoff_foundation_state_key" {
  type    = string
  default = ""
  validation {
    condition     = var.tools_handoff_foundation_state_key == "" || can(regex("^[A-Za-z0-9_/.-]+\\.tfstate$", var.tools_handoff_foundation_state_key))
    error_message = "Select the retained adapter foundation state key."
  }
}

variable "tools_handoff_security_group_ids" {
  type    = set(string)
  default = []
  validation {
    condition     = alltrue([for id in var.tools_handoff_security_group_ids : can(regex("^sg-[a-f0-9]{8,17}$", id))])
    error_message = "Only exact adapter and existing VPC-link security groups may be prepared."
  }
}

variable "tools_handoff_vpc_endpoint_id" {
  type    = string
  default = ""
  validation {
    condition     = var.tools_handoff_vpc_endpoint_id == "" || can(regex("^vpce-[a-f0-9]{8,17}$", var.tools_handoff_vpc_endpoint_id))
    error_message = "Select the existing private execute-api endpoint."
  }
}

variable "tools_handoff_vpc_link_id" {
  type    = string
  default = ""
  validation {
    condition     = var.tools_handoff_vpc_link_id == "" || can(regex("^[a-z0-9]+$", var.tools_handoff_vpc_link_id))
    error_message = "Select the existing private API VPC link."
  }
}

variable "tools_handoff_state_kms_key_arn" {
  type    = string
  default = ""
  validation {
    condition     = var.tools_handoff_state_kms_key_arn == "" || can(regex("^arn:aws:kms:[a-z0-9-]+:[0-9]{12}:key/[a-f0-9-]+$", var.tools_handoff_state_kms_key_arn))
    error_message = "Select only the existing foundation state encryption key."
  }
}

locals {
  tools_handoff_journal_prefix = "tools/adapters/handoff/${var.tools_handoff_rest_api_id}/"
  tools_handoff_journals       = "${local.tools_state_bucket}/${local.tools_handoff_journal_prefix}*.json"
  tools_handoff_foundation     = "${local.tools_state_bucket}/${var.tools_handoff_foundation_state_key}"
  tools_handoff_groups         = [for id in var.tools_handoff_security_group_ids : "arn:aws:ec2:${var.aws_region}:${local.tools_account}:security-group/${id}"]
  tools_handoff_endpoint       = "arn:aws:ec2:${var.aws_region}:${local.tools_account}:vpc-endpoint/${var.tools_handoff_vpc_endpoint_id}"
  tools_handoff_link           = "arn:aws:apigateway:${var.aws_region}::/vpclinks/${var.tools_handoff_vpc_link_id}"
  tools_handoff_reads          = concat([local.tools_handoff_api, "${local.tools_handoff_api}/*"], var.enable_tools_handoff_preparation ? [local.tools_handoff_link] : [])
  tools_handoff_patches        = concat(["${local.tools_handoff_api}/stages/${var.tools_handoff_stage}"], var.enable_tools_handoff_preparation ? [local.tools_handoff_api] : [])
  tools_handoff_preparation_actions = var.enable_tools_handoff_preparation ? [
    "apigateway:PUT", "apigateway:POST", "apigateway:UpdateRestApiPolicy", "bedrock-agentcore:GetGateway", "bedrock-agentcore:ListGatewayTargets", "bedrock-agentcore:CreateGatewayTarget",
    "elasticloadbalancing:DescribeLoadBalancers", "ec2:DescribeSecurityGroups", "ec2:DescribeVpcEndpoints",
    "ec2:AuthorizeSecurityGroupIngress", "ec2:AuthorizeSecurityGroupEgress", "ec2:ModifyVpcEndpoint",
    "iam:GetRolePolicy", "iam:PutRolePolicy", "s3:GetObject", "s3:ListBucket", "kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey",
  ] : []
}

resource "aws_iam_role_policy" "tools_handoff_preparation" {
  count = var.enable_tools_handoff_deployment && var.enable_tools_handoff_preparation ? 1 : 0
  name  = "tools-connector-preparation"
  role  = aws_iam_role.tools_handoff[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = concat([
    { Effect = "Allow", Action = ["bedrock-agentcore:GetGateway", "bedrock-agentcore:ListGatewayTargets", "bedrock-agentcore:CreateGatewayTarget"], Resource = var.tools_gateway_arn },
    { Effect = "Allow", Action = "apigateway:PUT", Resource = local.tools_handoff_api },
    # Updating /policy requires this permission in addition to apigateway:PATCH.
    { Effect = "Allow", Action = "apigateway:UpdateRestApiPolicy", Resource = local.tools_handoff_api },
    { Effect = "Allow", Action = "apigateway:POST", Resource = ["${local.tools_handoff_api}/deployments", "${local.tools_handoff_api}/stages"] },
    { Effect = "Allow", Action = ["elasticloadbalancing:DescribeLoadBalancers", "ec2:DescribeSecurityGroups", "ec2:DescribeVpcEndpoints"], Resource = "*" },
    { Effect = "Allow", Action = ["ec2:AuthorizeSecurityGroupIngress", "ec2:AuthorizeSecurityGroupEgress"], Resource = local.tools_handoff_groups },
    { Effect = "Allow", Action = "ec2:ModifyVpcEndpoint", Resource = local.tools_handoff_endpoint },
    { Effect = "Allow", Action = ["iam:GetRolePolicy", "iam:PutRolePolicy"], Resource = local.tools_gateway_role },
    { Effect = "Allow", Action = "s3:GetObject", Resource = [local.tools_handoff_foundation, local.tools_handoff_journals] },
    { Effect = "Allow", Action = "s3:ListBucket", Resource = local.tools_state_bucket,
    Condition = { StringLike = { "s3:prefix" = ["${local.tools_handoff_journal_prefix}*"] } } },
    { Effect = "Deny", Action = "apigateway:PUT", NotResource = local.tools_handoff_api },
    { Effect = "Deny", Action = "apigateway:UpdateRestApiPolicy", NotResource = local.tools_handoff_api },
    { Effect = "Deny", Action = "apigateway:POST", NotResource = ["${local.tools_handoff_api}/deployments", "${local.tools_handoff_api}/stages"] },
    { Effect = "Deny", Action = ["ec2:AuthorizeSecurityGroupIngress", "ec2:AuthorizeSecurityGroupEgress"], NotResource = local.tools_handoff_groups },
    { Effect = "Deny", Action = "ec2:ModifyVpcEndpoint", NotResource = local.tools_handoff_endpoint },
    { Effect = "Deny", Action = ["iam:GetRolePolicy", "iam:PutRolePolicy"], NotResource = local.tools_gateway_role },
    { Effect = "Deny", Action = ["bedrock-agentcore:GetGateway", "bedrock-agentcore:ListGatewayTargets", "bedrock-agentcore:CreateGatewayTarget"], NotResource = var.tools_gateway_arn },
    ], var.tools_handoff_state_kms_key_arn == "" ? [] : [
    { Effect = "Allow", Action = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey"], Resource = var.tools_handoff_state_kms_key_arn,
    Condition = { StringEquals = { "kms:ViaService" = "s3.${var.aws_region}.amazonaws.com", "kms:CallerAccount" = local.tools_account } } },
  ]) })
  lifecycle {
    precondition {
      condition = (
        var.tools_handoff_foundation_state_key != "" && length(var.tools_handoff_security_group_ids) >= 2 &&
        var.tools_handoff_vpc_endpoint_id != "" && var.tools_handoff_vpc_link_id != "" &&
        (var.tools_handoff_state_kms_key_arn == "" || startswith(var.tools_handoff_state_kms_key_arn, "arn:aws:kms:${var.aws_region}:${local.tools_account}:key/"))
      )
      error_message = "Preparation requires retained state and exact reviewed networking resources in the selected account."
    }
  }
}
