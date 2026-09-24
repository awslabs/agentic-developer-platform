mock_provider "aws" {
  mock_data "aws_caller_identity" { defaults = { account_id = "123456789012" } }
}
variables {
  name_prefix            = "adp-test"
  environment            = "test"
  aws_region             = "us-east-1"
  oidc_provider_arn      = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/TEST"
  oidc_issuer            = "https://oidc.eks.us-east-1.amazonaws.com/id/TEST"
  github_org             = "example"
  gateway_execution_arns = ["arn:aws:execute-api:us-east-1:123456789012:abc123/test/POST/agent/control"]
}
override_data {
  target = data.aws_secretsmanager_secret.github_dev["id"]
  values = { arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/example/gh-app-dev-id-ABC123" }
}
override_data {
  target = data.aws_secretsmanager_secret.github_dev["key"]
  values = { arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/example/gh-app-dev-key-DEF456" }
}
override_resource {
  target          = aws_iam_policy.boundary
  override_during = plan
  values          = { arn = "arn:aws:iam::123456789012:policy/adp-test-agent-workflow-boundary" }
}
run "exact_identity_and_ceiling" {
  command = plan
  assert {
    condition = jsondecode(aws_iam_role.agent.assume_role_policy).Statement[0].Condition == {
      StringEquals = {
        "oidc.eks.us-east-1.amazonaws.com/id/TEST:aud" = "sts.amazonaws.com"
        "oidc.eks.us-east-1.amazonaws.com/id/TEST:sub" = "system:serviceaccount:arc-runners:agent-workflow-sa"
      }
    }
    error_message = "The agent identity must trust only its own service account and STS audience."
  }
  assert {
    condition     = aws_iam_role.agent.permissions_boundary == aws_iam_policy.boundary.arn && aws_iam_role_policy.runtime.role == aws_iam_role.agent.name
    error_message = "Runtime grants and the explicit ceiling must bind the same separate role."
  }
  assert {
    condition     = aws_iam_role.agent.name == "adp-test-agent-workflow" && length(aws_iam_policy.boundary.policy) <= 6144
    error_message = "The dedicated role must be distinct and its managed ceiling must fit IAM limits."
  }
}
run "legitimate_work_without_deployment_authority" {
  command = plan
  assert {
    condition = toset(local.actions) == toset([
      "sts:GetCallerIdentity", "bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream",
      "ssm:GetParameter", "execute-api:Invoke", "secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"
    ])
    error_message = "Developer work needs model, exact gateway and dev-app access; no deployment/build/PassRole APIs."
  }
  assert {
    condition     = toset(one([for s in jsondecode(aws_iam_policy.boundary.policy).Statement : s if s.Sid == "DenyOutsideAgentApis"]).NotAction) == toset(local.actions)
    error_message = "Other attached/resource policies must not restore APIs outside the agent ceiling."
  }
  assert {
    condition = toset(one([for s in local.grants : s if s.Sid == "LegacyEngineTransport"]).Resource) == toset([
      "arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/example/gh-app-dev-id-ABC123",
      "arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/example/gh-app-dev-key-DEF456"
    ])
    error_message = "Only the two exact developer App secrets may be read, never ops/PM or tenant vaults."
  }
  assert {
    condition = alltrue([for grant in local.grants : length([
      for deny in local.boundary : deny if try(deny.Action == grant.Action && deny.NotResource == grant.Resource, false)
    ]) == 1 if grant.Resource != ["*"]])
    error_message = "Every resource-scoped capability needs a matching explicit deny outside that scope."
  }
}
run "no_gateway_grant_without_reviewed_routes" {
  command = plan
  variables { gateway_execution_arns = [] }
  assert {
    condition     = !contains(local.actions, "execute-api:Invoke")
    error_message = "Missing route configuration cannot become broad gateway access."
  }
}
run "namespace_patterns_refused" {
  command = plan
  variables { runner_namespace = "arc-runners*" }
  expect_failures = [var.runner_namespace]
}
