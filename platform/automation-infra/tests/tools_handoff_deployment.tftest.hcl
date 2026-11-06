mock_provider "external" {
  mock_data "external" { defaults = { result = { verified = "true" } } }
}
mock_provider "aws" {
  mock_data "aws_caller_identity" { defaults = { account_id = "123456789012" } }
  mock_data "aws_iam_openid_connect_provider" {
    defaults = { arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com" }
  }
  mock_data "aws_ssm_parameter" { defaults = { value = "mock" } }
}

override_data {
  target = data.aws_ssm_parameter.frontend_bucket
  values = { value = "adp-test-frontend" }
}

override_data {
  target = data.aws_ssm_parameter.frontend_cloudfront_id
  values = { value = "E1234567890" }
}

variables {
  name_prefix         = "adp-test"
  environment         = "test"
  aws_region          = "us-east-1"
  cluster_name        = "adp-test-eks-cluster"
  repository          = "example/repo"
  build_project_names = ["adp-test-gateway-build"]
  tools_gateway_arn   = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/example"
}

run "disabled_by_default" {
  command = plan
  assert {
    condition     = length(aws_iam_role.tools_handoff) == 0 && length(aws_eks_access_entry.tools_handoff) == 0
    error_message = "Normal evaluation must not acquire handoff authority."
  }
}

run "preparation_requires_exact_network_and_gateway_boundaries" {
  command = plan
  variables {
    enable_tools_handoff_deployment    = true
    enable_tools_handoff_preparation   = true
    tools_handoff_rest_api_id          = "api123"
    tools_handoff_foundation_state_key = "tools/adapters/test.tfstate"
    tools_handoff_security_group_ids   = ["sg-00000000000000001", "sg-00000000000000002"]
    tools_handoff_vpc_endpoint_id      = "vpce-00000000000000001"
    tools_handoff_vpc_link_id          = "link123"
  }
  assert {
    condition = (
      contains(local.tools_handoff_actions, "apigateway:UpdateRestApiPolicy") &&
      length([for statement in jsondecode(aws_iam_role_policy.tools_handoff_preparation[0].policy).Statement : statement
        if statement.Effect == "Allow" && try(statement.Action == "apigateway:UpdateRestApiPolicy" && statement.Resource == "arn:aws:apigateway:us-east-1::/restapis/api123", false)
      ]) == 1 &&
      anytrue([for statement in jsondecode(aws_iam_role_policy.tools_handoff_preparation[0].policy).Statement :
        statement.Effect == "Deny" && try(statement.Action == "apigateway:UpdateRestApiPolicy" && statement.NotResource == "arn:aws:apigateway:us-east-1::/restapis/api123", false)
      ])
    )
    error_message = "Preparing the API policy requires UpdateRestApiPolicy within the action ceiling and only on the selected API."
  }
  assert {
    condition = (
      length(aws_iam_role_policy.tools_handoff_preparation) == 1 &&
      alltrue([for statement in jsondecode(aws_iam_role_policy.tools_handoff_preparation[0].policy).Statement :
        statement.Effect != "Allow" || !contains(try(tolist(statement.Action), [statement.Action]), "iam:PutRolePolicy") ||
        statement.Resource == "arn:aws:iam::123456789012:role/adp-tools-test-gateway"
      ]) &&
      alltrue([for statement in jsondecode(aws_iam_role_policy.tools_handoff_preparation[0].policy).Statement :
        statement.Effect != "Allow" || statement.Action != "apigateway:PUT" || statement.Resource == "arn:aws:apigateway:us-east-1::/restapis/api123"
      ]) &&
      alltrue([for statement in jsondecode(aws_iam_role_policy.tools_handoff_preparation[0].policy).Statement :
        statement.Effect != "Allow" || statement.Action != "ec2:ModifyVpcEndpoint" ||
        statement.Resource == "arn:aws:ec2:us-east-1:123456789012:vpc-endpoint/vpce-00000000000000001"
      ])
    )
    error_message = "Preparation grants must remain on the dedicated role and exact existing gateway, API and private network."
  }
  assert {
    condition = (
      toset(local.tools_handoff_groups) == toset(["arn:aws:ec2:us-east-1:123456789012:security-group/sg-00000000000000001", "arn:aws:ec2:us-east-1:123456789012:security-group/sg-00000000000000002"]) &&
      !contains(local.tools_handoff_actions, "bedrock-agentcore:DeleteGatewayTarget") &&
      !contains(local.tools_handoff_actions, "iam:PassRole")
    )
    error_message = "Preparation may create targets only in the selected gateway, never delete targets, pass roles or open unrelated network groups."
  }
  assert {
    condition = (
      anytrue([for statement in jsondecode(aws_iam_role_policy.tools_handoff[0].policy).Statement :
        statement.Effect == "Allow" && try(statement.Action == "bedrock-agentcore:GetGatewayTarget" && statement.Resource == var.tools_gateway_arn, false)
      ]) &&
      anytrue([for statement in jsondecode(aws_iam_role_policy.tools_handoff[0].policy).Statement :
        statement.Effect == "Deny" && try(statement.Action == "bedrock-agentcore:GetGatewayTarget" && statement.NotResource == var.tools_gateway_arn, false)
      ])
    )
    error_message = "Preparation must read targets through AWS's parent-gateway resource and deny target reads in other gateways."
  }
}

run "missing_preparation_network_refused" {
  command = plan
  variables {
    enable_tools_handoff_deployment  = true
    enable_tools_handoff_preparation = true
    tools_handoff_rest_api_id        = "api123"
  }
  expect_failures = [aws_iam_role_policy.tools_handoff_preparation]
}

run "dedicated_environment_and_exact_resources" {
  command = plan
  variables {
    enable_tools_handoff_deployment = true
    tools_handoff_rest_api_id       = "api123"
    tools_handoff_stage             = "test"
    tools_handoff_connector_target_arns = [
      "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/example/target/atlassian",
      "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/example/target/github",
      "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/example/target/jira",
      "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/example/target/slack",
    ]
  }
  assert {
    condition = (
      !contains(local.tools_handoff_actions, "apigateway:UpdateRestApiPolicy") &&
      length(aws_iam_role_policy.tools_handoff_preparation) == 0
    )
    error_message = "Switching already prepared routes must not grant API resource-policy editing authority."
  }
  assert {
    condition = (
      jsondecode(aws_iam_role.tools_handoff[0].assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:example/repo:environment:adp-tools-handoff-test" &&
      aws_eks_access_entry.tools_handoff[0].kubernetes_groups == toset(["adp:tools-handoff-test"])
    )
    error_message = "Evaluation's protected environment and Kubernetes group cannot assume handoff authority."
  }
  assert {
    condition = alltrue([for statement in jsondecode(aws_iam_role_policy.tools_handoff[0].policy).Statement :
      statement.Effect != "Allow" || statement.Action != "apigateway:PATCH" || statement.Resource == ["arn:aws:apigateway:us-east-1::/restapis/api123/stages/test"]
    ])
    error_message = "Only the exact release stage may be switched."
  }
  assert {
    condition = (
      anytrue([for statement in jsondecode(aws_iam_role_policy.tools_handoff[0].policy).Statement :
        statement.Effect == "Allow" && try(statement.Action == "bedrock-agentcore:GetGatewayTarget" && statement.Resource == var.tools_gateway_arn, false)
      ]) &&
      anytrue([for statement in jsondecode(aws_iam_role_policy.tools_handoff[0].policy).Statement :
        statement.Effect == "Deny" && try(statement.Action == "bedrock-agentcore:GetGatewayTarget" && statement.NotResource == var.tools_gateway_arn, false)
      ])
    )
    error_message = "Prepared-target handoff must use the same exact parent-gateway read boundary as preparation."
  }
}
