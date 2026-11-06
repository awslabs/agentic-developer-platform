mock_provider "external" {
  mock_data "external" { defaults = { result = { verified = "true" } } }
}
mock_provider "aws" {
  mock_data "aws_caller_identity" { defaults = { account_id = "123456789012" } }
  mock_data "aws_iam_openid_connect_provider" { defaults = { arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com" } }
  mock_data "aws_kms_key" { defaults = { arn = "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000000" } }
  mock_data "aws_ssm_parameter" { defaults = { value = "mock" } }
  mock_resource "aws_iam_role" { defaults = { arn = "arn:aws:iam::123456789012:role/test" } }
  mock_resource "aws_iam_policy" { defaults = { arn = "arn:aws:iam::123456789012:policy/test" } }
  mock_data "aws_subnet" { defaults = { vpc_id = "vpc-00000000000000001", map_public_ip_on_launch = false, availability_zone_id = "use1-az1" } }
  mock_data "aws_security_group" { defaults = { vpc_id = "vpc-00000000000000001" } }
}

variables {
  name_prefix               = "adp-dev"
  environment               = "dev"
  aws_region                = "us-east-1"
  cluster_name              = "adp-dev-eks-cluster"
  repository                = "aws-e/adp"
  build_project_names       = ["adp-dev-gateway-build"]
  enable_tools_deployment   = true
  tools_gateway_arn         = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/adp-tools-example"
  tools_gateway_target_arns = ["arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/adp-tools-example/target/existing"]
}

override_data {
  target = data.aws_ssm_parameter.frontend_bucket
  values = { value = "adp-dev-frontend" }
}
override_data {
  target = data.aws_ssm_parameter.frontend_cloudfront_id
  values = { value = "E1234567890" }
}
override_data {
  target = data.aws_subnet.tools_knowledge["subnet-00000000000000002"]
  values = { vpc_id = "vpc-00000000000000001", map_public_ip_on_launch = false, availability_zone_id = "use1-az2" }
}

run "creation_is_default_off_in_both_action_ceilings" {
  command = plan
  assert {
    condition = alltrue([for policy in [aws_iam_role_policy.tools_deployment[0].policy, aws_iam_role_policy.tools_deployment_ceiling[0].policy] :
      toset([for action in one([for s in jsondecode(policy).Statement : s if s.Sid == "DenyOtherAPIs"]).NotAction : action if startswith(action, "lambda:")]) == toset([
        "lambda:GetFunction", "lambda:GetFunctionConfiguration", "lambda:UpdateFunctionCode", "lambda:PublishVersion",
        "lambda:GetAlias", "lambda:CreateAlias", "lambda:UpdateAlias", "lambda:ListVersionsByFunction",
        "lambda:ListAliases", "lambda:GetPolicy", "lambda:ListTags",
      ])
    ])
    error_message = "Both default ceilings must admit exactly the existing interceptor release/read APIs, including first-release CreateAlias."
  }
  assert {
    condition = (
      length(aws_iam_policy.tools_knowledge_creation) == 0 &&
      length(aws_iam_role_policy_attachment.tools_knowledge_creation) == 0 &&
      length(data.aws_subnet.tools_knowledge) == 0 &&
      length(data.aws_security_group.tools_knowledge) == 0 &&
      alltrue([for policy in [aws_iam_role_policy.tools_deployment[0].policy, aws_iam_role_policy.tools_deployment_ceiling[0].policy] :
        alltrue([for action in concat(["bedrock-agentcore:CreateGatewayTarget", "iam:CreateServiceLinkedRole"], local.tools_knowledge_ec2_actions) :
          !contains(one([for statement in jsondecode(policy).Statement : statement if statement.Sid == "DenyOtherAPIs"]).NotAction, action)
        ])
      ])
    )
    error_message = "Without explicit network opt-in both action ceilings must still deny target and network creation."
  }
}

run "knowledge_create_has_exact_gateway_network_and_preserved_denials" {
  command = plan
  variables {
    tools_gateway_arn = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/adp-tools-0000000000"
    tools_gateway_target_arns = [
      "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/adp-tools-0000000000/target/000000000001",
      "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/adp-tools-0000000000/target/000000000002",
      "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/adp-tools-0000000000/target/000000000003",
    ]
    tools_inputs_secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/dev/tools/inputs-000000"
    tools_knowledge_target_creation = {
      vpc_id             = "vpc-00000000000000001"
      subnet_ids         = ["subnet-00000000000000001", "subnet-00000000000000002"]
      security_group_ids = ["sg-00000000000000001"]
    }
  }
  assert {
    condition = alltrue([for policy in [aws_iam_role_policy.tools_deployment[0].policy, aws_iam_role_policy.tools_deployment_ceiling[0].policy] :
      toset([for action in one([for s in jsondecode(policy).Statement : s if s.Sid == "DenyOtherAPIs"]).NotAction : action if startswith(action, "lambda:")]) == toset([
        "lambda:GetFunction", "lambda:GetFunctionConfiguration", "lambda:UpdateFunctionCode", "lambda:PublishVersion",
        "lambda:GetAlias", "lambda:CreateAlias", "lambda:UpdateAlias", "lambda:ListVersionsByFunction",
        "lambda:ListAliases", "lambda:GetPolicy", "lambda:ListTags",
      ])
    ])
    error_message = "Knowledge opt-in must neither remove interceptor release APIs nor admit extra Lambda actions in either ceiling."
  }
  assert {
    condition = (
      one([for s in jsondecode(aws_iam_policy.tools_knowledge_creation[0].policy).Statement : s if s.Sid == "CreateKnowledgeOnSelectedGateway"]).Resource == var.tools_gateway_arn &&
      one([for s in jsondecode(aws_iam_policy.tools_knowledge_creation[0].policy).Statement : s if s.Sid == "CreateKnowledgeOnSelectedGateway"]).Action == "bedrock-agentcore:CreateGatewayTarget" &&
      one([for s in jsondecode(aws_iam_policy.tools_knowledge_creation[0].policy).Statement : s if s.Sid == "CreateKnowledgeInterface"]).Resource == "arn:aws:ec2:us-east-1:123456789012:network-interface/*" &&
      toset(one([for s in jsondecode(aws_iam_policy.tools_knowledge_creation[0].policy).Statement : s if s.Sid == "UseKnowledgeNetwork"]).Resource) == toset([
        "arn:aws:ec2:us-east-1:123456789012:subnet/subnet-00000000000000001",
        "arn:aws:ec2:us-east-1:123456789012:subnet/subnet-00000000000000002",
        "arn:aws:ec2:us-east-1:123456789012:security-group/sg-00000000000000001",
      ]) &&
      one([for s in jsondecode(aws_iam_policy.tools_knowledge_creation[0].policy).Statement : s if s.Sid == "UseKnowledgeNetwork"]).Condition.ArnEquals["ec2:Vpc"] == "arn:aws:ec2:us-east-1:123456789012:vpc/vpc-00000000000000001" &&
      toset(one([for s in jsondecode(aws_iam_policy.tools_knowledge_creation[0].policy).Statement : s if s.Sid == "DenyOtherKnowledgeNetwork"]).NotResource) == toset(concat([local.tools_knowledge_eni], local.tools_knowledge_network)) &&
      one([for s in jsondecode(aws_iam_policy.tools_knowledge_creation[0].policy).Statement : s if s.Sid == "DenyOtherKnowledgeVpcs"]).Condition.ArnNotEquals["ec2:Vpc"] == local.tools_knowledge_vpc &&
      one([for s in jsondecode(aws_iam_policy.tools_knowledge_creation[0].policy).Statement : s if s.Sid == "DenyOtherKnowledgeNetworkRegions"]).Condition.StringNotEquals["ec2:Region"] == "us-east-1"
    )
    error_message = "Creation must be bounded by the exact parent gateway and subnet/SG resources; new interface IDs are scoped to account and region."
  }
  assert {
    condition = (
      one([for s in jsondecode(aws_iam_policy.tools_knowledge_creation[0].policy).Statement : s if s.Sid == "DescribeKnowledgeNetwork"]).Resource == "*" &&
      one([for s in jsondecode(aws_iam_policy.tools_knowledge_creation[0].policy).Statement : s if s.Sid == "DescribeKnowledgeNetwork"]).Condition.StringEquals["ec2:Region"] == "us-east-1" &&
      toset(one([for s in jsondecode(aws_iam_policy.tools_knowledge_creation[0].policy).Statement : s if s.Sid == "DescribeKnowledgeNetwork"]).Action) == toset(["ec2:DescribeVpcs", "ec2:DescribeSubnets", "ec2:DescribeSecurityGroups"]) &&
      alltrue([for policy in [aws_iam_role_policy.tools_deployment[0].policy, aws_iam_role_policy.tools_deployment_ceiling[0].policy] :
        toset(one([for s in jsondecode(policy).Statement : s if s.Sid == "DenyOtherAPIs"]).NotAction) == toset(local.tools_actions)
      ]) &&
      alltrue([for action in local.tools_knowledge_actions : contains(local.tools_actions, action)]) &&
      toset([for action in local.tools_actions : action if startswith(action, "iam:")]) == toset(["iam:PassRole", "iam:CreateServiceLinkedRole"]) && !contains(local.tools_knowledge_actions, "iam:PassRole") &&
      !contains(local.tools_actions, "ec2:DeleteNetworkInterface") && !contains(local.tools_actions, "vpc-lattice:CreateResourceGateway") &&
      !contains(local.tools_actions, "bedrock-agentcore:CreateGateway") && !contains(local.tools_actions, "bedrock-agentcore:DeleteGatewayTarget") &&
      one([for s in jsondecode(aws_iam_role_policy.tools_deployment[0].policy).Statement : s if s.Sid == "DenyOtherPassedRoles"]).NotResource == local.tools_gateway_role &&
      toset(one([for s in jsondecode(aws_iam_policy.tools_deployment_resource_ceiling[0].policy).Statement : s if s.Sid == "DenyOtherGatewayResources"]).NotResource) == toset(local.tools_gateway_resources) &&
      length(aws_iam_role_policy.tools_deployment[0].policy) + length(aws_iam_role_policy.tools_deployment_ceiling[0].policy) <= 10240 &&
      length(aws_iam_policy.tools_knowledge_creation[0].policy) <= 6144
    )
    error_message = "Optional actions must update both ceilings, preserve IAM/deletion/other-gateway denies and fit policy quotas."
  }
  assert {
    condition = (
      one([for s in jsondecode(aws_iam_policy.tools_knowledge_creation[0].policy).Statement : s if s.Sid == "EnsureGatewayNetworkRole"]) == {
        Sid       = "EnsureGatewayNetworkRole", Effect = "Allow", Action = "iam:CreateServiceLinkedRole",
        Resource  = "arn:aws:iam::123456789012:role/aws-service-role/bedrock-agentcore.amazonaws.com/AWSServiceRoleForBedrockAgentCoreGatewayNetwork",
        Condition = { StringEquals = { "iam:AWSServiceName" = "bedrock-agentcore.amazonaws.com" } }
      } &&
      one([for s in jsondecode(aws_iam_policy.tools_knowledge_creation[0].policy).Statement : s if s.Sid == "DenyOtherServiceLinkedRoles"]).NotResource == local.tools_knowledge_network_role &&
      one([for s in jsondecode(aws_iam_policy.tools_knowledge_creation[0].policy).Statement : s if s.Sid == "DenyOtherNetworkRoleServices"]) == {
        Sid       = "DenyOtherNetworkRoleServices", Effect = "Deny", Action = "iam:CreateServiceLinkedRole", Resource = local.tools_knowledge_network_role,
        Condition = { StringNotEquals = { "iam:AWSServiceName" = "bedrock-agentcore.amazonaws.com" } }
      }
    )
    error_message = "Only AgentCore's exact network service-linked role may be ensured; other roles and missing/foreign services must be explicitly denied."
  }
}

run "creation_refuses_disabled_release_identity" {
  command = plan
  variables {
    enable_tools_deployment = false
    tools_knowledge_target_creation = {
      vpc_id = "vpc-00000000000000001", subnet_ids = ["subnet-00000000000000001", "subnet-00000000000000002"], security_group_ids = ["sg-00000000000000001"]
    }
  }
  expect_failures = [var.tools_knowledge_target_creation]
}

run "creation_refuses_implicit_security_group" {
  command = plan
  variables {
    tools_knowledge_target_creation = {
      vpc_id = "vpc-00000000000000001", subnet_ids = ["subnet-00000000000000001", "subnet-00000000000000002"], security_group_ids = []
    }
  }
  expect_failures = [var.tools_knowledge_target_creation]
}

run "creation_refuses_foreign_vpc_group" {
  command = plan
  variables {
    tools_knowledge_target_creation = {
      vpc_id = "vpc-00000000000000001", subnet_ids = ["subnet-00000000000000001", "subnet-00000000000000002"], security_group_ids = ["sg-00000000000000001"]
    }
  }
  override_data {
    target = data.aws_security_group.tools_knowledge["sg-00000000000000001"]
    values = { vpc_id = "vpc-00000000000000002" }
  }
  expect_failures = [aws_iam_policy.tools_knowledge_creation]
}

run "creation_refuses_public_subnet" {
  command = plan
  variables {
    tools_knowledge_target_creation = {
      vpc_id = "vpc-00000000000000001", subnet_ids = ["subnet-00000000000000001", "subnet-00000000000000002"], security_group_ids = ["sg-00000000000000001"]
    }
  }
  override_data {
    target = data.aws_subnet.tools_knowledge["subnet-00000000000000001"]
    values = { vpc_id = "vpc-00000000000000001", map_public_ip_on_launch = true, availability_zone_id = "use1-az1" }
  }
  expect_failures = [aws_iam_policy.tools_knowledge_creation]
}

run "creation_refuses_one_az" {
  command = plan
  variables {
    tools_knowledge_target_creation = {
      vpc_id = "vpc-00000000000000001", subnet_ids = ["subnet-00000000000000001", "subnet-00000000000000002"], security_group_ids = ["sg-00000000000000001"]
    }
  }
  override_data {
    target = data.aws_subnet.tools_knowledge["subnet-00000000000000002"]
    values = { vpc_id = "vpc-00000000000000001", map_public_ip_on_launch = false, availability_zone_id = "use1-az1" }
  }
  expect_failures = [aws_iam_policy.tools_knowledge_creation]
}
