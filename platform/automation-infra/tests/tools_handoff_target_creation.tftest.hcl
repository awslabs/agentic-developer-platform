mock_provider "external" {
  mock_data "external" { defaults = { result = { verified = "true" } } }
}
mock_provider "aws" {
  mock_data "aws_caller_identity" { defaults = { account_id = "123456789012" } }
  mock_data "aws_iam_openid_connect_provider" { defaults = { arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com" } }
  mock_data "aws_ssm_parameter" { defaults = { value = "mock" } }
  mock_data "aws_subnet" { defaults = { vpc_id = "vpc-00000000000000001", map_public_ip_on_launch = false, availability_zone_id = "use1-az1" } }
  mock_data "aws_security_group" { defaults = { vpc_id = "vpc-00000000000000001" } }
}
override_data {
  target = data.aws_ssm_parameter.frontend_bucket
  values = { value = "adp-test-frontend" }
}
override_data {
  target = data.aws_ssm_parameter.frontend_cloudfront_id
  values = { value = "E1234567890" }
}
override_data {
  target = data.aws_subnet.tools_knowledge["subnet-00000000000000002"]
  values = { vpc_id = "vpc-00000000000000001", map_public_ip_on_launch = false, availability_zone_id = "use1-az2" }
}

variables {
  name_prefix                        = "adp-test"
  environment                        = "test"
  aws_region                         = "us-east-1"
  cluster_name                       = "adp-test-eks-cluster"
  repository                         = "example/repo"
  build_project_names                = ["adp-test-gateway-build"]
  tools_gateway_arn                  = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/example"
  enable_tools_deployment            = false
  enable_tools_handoff_deployment    = true
  enable_tools_handoff_preparation   = true
  tools_handoff_rest_api_id          = "api123"
  tools_handoff_foundation_state_key = "tools/adapters/test.tfstate"
  tools_handoff_security_group_ids   = ["sg-00000000000000001", "sg-00000000000000002"]
  tools_handoff_vpc_endpoint_id      = "vpce-00000000000000001"
  tools_handoff_vpc_link_id          = "link123"
  tools_handoff_state_kms_key_arn    = "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000002"
  tools_gateway_kms_key_arn          = "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000001"
  tools_knowledge_target_creation = {
    vpc_id             = "vpc-00000000000000001"
    subnet_ids         = ["subnet-00000000000000001", "subnet-00000000000000002"]
    security_group_ids = ["sg-00000000000000001"]
  }
}

run "handoff_reuses_private_target_permissions_without_legacy_role" {
  command = plan
  assert {
    condition = (
      length(aws_iam_role.tools_deployment) == 0 &&
      length(aws_iam_role_policy_attachment.tools_knowledge_creation) == 0 &&
      length(aws_iam_role_policy_attachment.tools_gateway_key) == 0 &&
      length(aws_iam_policy.tools_knowledge_creation) == 1 &&
      length(aws_iam_role_policy_attachment.tools_handoff_private_target) == 1 &&
      length(aws_iam_role_policy_attachment.tools_handoff_gateway_key) == 1 &&
      alltrue([for action in concat(local.tools_private_target_actions, ["kms:Decrypt", "kms:GenerateDataKey", "kms:ListGrants"]) :
        contains(local.tools_handoff_actions, action)
      ]) &&
      alltrue([for action in ["iam:PassRole", "iam:CreateRole", "kms:CreateGrant", "kms:PutKeyPolicy", "ec2:DeleteNetworkInterface"] :
        !contains(local.tools_handoff_actions, action)
      ])
    )
    error_message = "Handoff must reuse scoped target-creation capabilities without enabling the legacy release role or granting administration."
  }
  assert {
    condition = (
      toset(one([for s in jsondecode(aws_iam_policy.tools_knowledge_creation[0].policy).Statement : s if s.Sid == "UseKnowledgeNetwork"]).Resource) == toset([
        "arn:aws:ec2:us-east-1:123456789012:subnet/subnet-00000000000000001",
        "arn:aws:ec2:us-east-1:123456789012:subnet/subnet-00000000000000002",
        "arn:aws:ec2:us-east-1:123456789012:security-group/sg-00000000000000001",
      ]) &&
      one([for s in jsondecode(aws_iam_policy.tools_knowledge_creation[0].policy).Statement : s if s.Sid == "DenyOtherKnowledgeVpcs"]).Condition.ArnNotEquals["ec2:Vpc"] == "arn:aws:ec2:us-east-1:123456789012:vpc/vpc-00000000000000001"
    )
    error_message = "The shared private-target policy must retain the exact subnet, security-group and VPC boundaries for handoff."
  }
  assert {
    condition = (
      one([for s in jsondecode(aws_iam_policy.tools_handoff_gateway_key[0].policy).Statement : s if s.Sid == "GatewayKeyValidation"]).Condition == {
        StringEquals = { "kms:ViaService" = "bedrock-agentcore.us-east-1.amazonaws.com", "kms:CallerAccount" = "123456789012" }
        Null         = { "kms:EncryptionContextKeys" = "true" }
      } &&
      toset(one([for s in jsondecode(aws_iam_policy.tools_handoff_gateway_key[0].policy).Statement : s if s.Sid == "DenyOtherHandoffDataKeys"]).NotResource) == toset([var.tools_gateway_kms_key_arn, var.tools_handoff_state_kms_key_arn]) &&
      one([for s in jsondecode(aws_iam_policy.tools_handoff_gateway_key[0].policy).Statement : s if s.Sid == "DenyOtherGatewayGrantResources"]).NotResource == var.tools_gateway_kms_key_arn &&
      alltrue([for s in jsondecode(aws_iam_policy.tools_handoff_gateway_key[0].policy).Statement :
        s.Effect != "Allow" || s.Resource == var.tools_gateway_kms_key_arn
      ]) &&
      anytrue([for s in jsondecode(aws_iam_role_policy.tools_handoff_preparation[0].policy).Statement :
        s.Effect == "Allow" && try(s.Resource == var.tools_handoff_state_kms_key_arn && s.Condition.StringEquals["kms:ViaService"] == "s3.us-east-1.amazonaws.com", false)
      ])
    )
    error_message = "AgentCore validation must stay on its exact key without blocking the separate S3-only journal key."
  }
}

run "unconfigured_handoff_does_not_gain_optional_capabilities" {
  command = plan
  variables {
    tools_gateway_kms_key_arn       = ""
    tools_knowledge_target_creation = null
  }
  assert {
    condition = (
      length(aws_iam_role_policy_attachment.tools_handoff_private_target) == 0 &&
      length(aws_iam_role_policy_attachment.tools_handoff_gateway_key) == 0 &&
      alltrue([for action in ["kms:ListGrants", "iam:CreateServiceLinkedRole", "ec2:CreateNetworkInterface"] :
        !contains(local.tools_handoff_actions, action)
      ])
    )
    error_message = "Optional key and network capabilities require explicitly selected existing resources."
  }
}

run "gateway_and_journal_keys_must_be_separate" {
  command = plan
  variables {
    tools_gateway_kms_key_arn = "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000002"
  }
  expect_failures = [aws_iam_policy.tools_handoff_gateway_key]
}
