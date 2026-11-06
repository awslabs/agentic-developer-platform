# First enablement is opt-in. AWS authorizes CreateGatewayTarget against its
# parent gateway, not a target ARN, and has no target-name/endpoint condition.
# The protected saved-plan validator binds each Knowledge or Connections target
# separately. Connections reuse this exact private network; no additional IAM
# grants or state ownership changes are needed. Never enable these grants with
# an unqualified release definition.
variable "tools_knowledge_target_creation" {
  description = "Optional reviewed, already provisioned Knowledge network. Enables creation on the selected existing gateway; the protected release binding and saved-plan validator must independently approve each exact private mediator target."
  type = object({
    vpc_id             = string
    subnet_ids         = set(string)
    security_group_ids = set(string)
  })
  default = null
  validation {
    condition = var.tools_knowledge_target_creation == null ? true : (
      (var.enable_tools_deployment || (var.enable_tools_handoff_deployment && var.enable_tools_handoff_preparation)) &&
      can(regex("^vpc-[a-f0-9]{8,17}$", var.tools_knowledge_target_creation.vpc_id)) &&
      length(var.tools_knowledge_target_creation.subnet_ids) >= 2 &&
      length(var.tools_knowledge_target_creation.subnet_ids) <= 16 &&
      alltrue([for id in var.tools_knowledge_target_creation.subnet_ids : can(regex("^subnet-[a-f0-9]{8,17}$", id))]) &&
      length(var.tools_knowledge_target_creation.security_group_ids) >= 1 &&
      length(var.tools_knowledge_target_creation.security_group_ids) <= 5 &&
      alltrue([for id in var.tools_knowledge_target_creation.security_group_ids : can(regex("^sg-[a-f0-9]{8,17}$", id))])
    )
    error_message = "Enable tools deployment or handoff preparation and select an exact existing VPC, at least two private subnets and explicit resource-gateway security groups."
  }
}

locals {
  tools_knowledge_create = var.enable_tools_deployment && var.tools_knowledge_target_creation != null
  tools_private_target_create = (
    var.enable_tools_deployment || (var.enable_tools_handoff_deployment && var.enable_tools_handoff_preparation)
  ) && var.tools_knowledge_target_creation != null
  tools_knowledge_ec2_actions = [
    "ec2:CreateNetworkInterface", "ec2:DescribeVpcs", "ec2:DescribeSubnets", "ec2:DescribeSecurityGroups",
  ]
  tools_private_target_actions = concat(["bedrock-agentcore:CreateGatewayTarget", "iam:CreateServiceLinkedRole"], local.tools_knowledge_ec2_actions)
  tools_knowledge_actions      = local.tools_knowledge_create ? local.tools_private_target_actions : []
  tools_knowledge_network_role = "arn:aws:iam::${local.tools_account}:role/aws-service-role/bedrock-agentcore.amazonaws.com/AWSServiceRoleForBedrockAgentCoreGatewayNetwork"
  tools_knowledge_vpc          = local.tools_private_target_create ? "arn:aws:ec2:${var.aws_region}:${local.tools_account}:vpc/${var.tools_knowledge_target_creation.vpc_id}" : ""
  tools_knowledge_eni          = "arn:aws:ec2:${var.aws_region}:${local.tools_account}:network-interface/*"
  tools_knowledge_network = local.tools_private_target_create ? concat(
    [for id in sort(tolist(var.tools_knowledge_target_creation.subnet_ids)) : "arn:aws:ec2:${var.aws_region}:${local.tools_account}:subnet/${id}"],
    [for id in sort(tolist(var.tools_knowledge_target_creation.security_group_ids)) : "arn:aws:ec2:${var.aws_region}:${local.tools_account}:security-group/${id}"],
  ) : []
}

data "aws_subnet" "tools_knowledge" {
  for_each = local.tools_private_target_create ? var.tools_knowledge_target_creation.subnet_ids : toset([])
  id       = each.value
}

data "aws_security_group" "tools_knowledge" {
  for_each = local.tools_private_target_create ? var.tools_knowledge_target_creation.security_group_ids : toset([])
  id       = each.value
}

# Keep optional grants/denials managed: IAM's 10,240 byte inline quota is
# aggregate across the role. Both existing action ceilings include exactly the
# same optional actions, and still deny every unlisted API.
locals {
  tools_knowledge_policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Sid = "CreateKnowledgeOnSelectedGateway", Effect = "Allow", Action = "bedrock-agentcore:CreateGatewayTarget", Resource = var.tools_gateway_arn },
    # Managed private target creation checks this permission even when the
    # bootstrap-owned service-linked role already exists. Keep its exact grant
    # and explicit fences atomic; no general role creation or IAM administration.
    { Sid = "EnsureGatewayNetworkRole", Effect = "Allow", Action = "iam:CreateServiceLinkedRole", Resource = local.tools_knowledge_network_role,
    Condition = { StringEquals = { "iam:AWSServiceName" = "bedrock-agentcore.amazonaws.com" } } },
    { Sid = "DenyOtherServiceLinkedRoles", Effect = "Deny", Action = "iam:CreateServiceLinkedRole", NotResource = local.tools_knowledge_network_role },
    { Sid = "DenyOtherNetworkRoleServices", Effect = "Deny", Action = "iam:CreateServiceLinkedRole", Resource = local.tools_knowledge_network_role,
    Condition = { StringNotEquals = { "iam:AWSServiceName" = "bedrock-agentcore.amazonaws.com" } } },
    # A new interface ID is allocated by AWS. EC2 independently authorizes the
    # required subnet and optional supplied SG resources against the next grant.
    # ec2:Subnet/ec2:Vpc are NOT supported on the new interface resource for this
    # action; do not invent a condition that makes the documented call unusable.
    { Sid = "CreateKnowledgeInterface", Effect = "Allow", Action = "ec2:CreateNetworkInterface", Resource = local.tools_knowledge_eni },
    { Sid = "UseKnowledgeNetwork", Effect = "Allow", Action = "ec2:CreateNetworkInterface", Resource = local.tools_knowledge_network,
    Condition = { ArnEquals = { "ec2:Vpc" = local.tools_knowledge_vpc } } },
    { Sid = "DescribeKnowledgeNetwork", Effect = "Allow", Action = ["ec2:DescribeVpcs", "ec2:DescribeSubnets", "ec2:DescribeSecurityGroups"], Resource = "*",
    Condition = { StringEquals = { "ec2:Region" = var.aws_region } } },
    { Sid = "DenyOtherKnowledgeNetwork", Effect = "Deny", Action = "ec2:CreateNetworkInterface", NotResource = concat([local.tools_knowledge_eni], local.tools_knowledge_network) },
    { Sid = "DenyOtherKnowledgeVpcs", Effect = "Deny", Action = "ec2:CreateNetworkInterface", Resource = local.tools_knowledge_network,
    Condition = { ArnNotEquals = { "ec2:Vpc" = local.tools_knowledge_vpc } } },
    { Sid = "DenyOtherKnowledgeNetworkRegions", Effect = "Deny", Action = local.tools_knowledge_ec2_actions, Resource = "*",
    Condition = { StringNotEquals = { "ec2:Region" = var.aws_region } } },
  ] })
}

resource "aws_iam_policy" "tools_knowledge_creation" {
  count  = local.tools_private_target_create ? 1 : 0
  name   = "${var.name_prefix}-tools-knowledge-creation"
  policy = local.tools_knowledge_policy
  lifecycle {
    precondition {
      condition     = length(local.tools_knowledge_policy) <= 6144
      error_message = "Exact Knowledge creation grants exceed the managed-policy quota; split the policy without broadening its network resources."
    }
    precondition {
      condition = (
        alltrue([for subnet in data.aws_subnet.tools_knowledge : subnet.vpc_id == var.tools_knowledge_target_creation.vpc_id && !subnet.map_public_ip_on_launch]) &&
        length(toset([for subnet in data.aws_subnet.tools_knowledge : subnet.availability_zone_id])) >= 2 &&
        alltrue([for group in data.aws_security_group.tools_knowledge : group.vpc_id == var.tools_knowledge_target_creation.vpc_id])
      )
      error_message = "Knowledge release networking must use private subnets in distinct AZs and security groups in the exact selected VPC."
    }
  }
}

resource "aws_iam_role_policy_attachment" "tools_knowledge_creation" {
  count      = local.tools_knowledge_create ? 1 : 0
  role       = aws_iam_role.tools_deployment[0].name
  policy_arn = aws_iam_policy.tools_knowledge_creation[0].arn
}
