mock_provider "aws" {
  mock_data "aws_caller_identity" { defaults = { account_id = "123456789012" } }
  mock_data "aws_iam_openid_connect_provider" {
    defaults = { arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com" }
  }
  mock_resource "aws_iam_role" { defaults = { arn = "arn:aws:iam::123456789012:role/test" } }
  mock_resource "aws_iam_policy" { defaults = { arn = "arn:aws:iam::123456789012:policy/test" } }
}
variables {
  name_prefix         = "adp-test"
  environment         = "test"
  aws_region          = "us-east-1"
  cluster_name        = "adp-test-eks-cluster"
  repository          = "aws-e/adp"
  build_project_names = ["adp-test-gateway-build"]
}
run "deployment_and_build_trust_are_separate" {
  command = plan
  assert {
    condition = jsondecode(aws_iam_role.deployment.assume_role_policy).Statement[0].Condition.StringEquals == {
      "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
      "token.actions.githubusercontent.com:sub" = "repo:aws-e/adp:environment:adp-deploy-test"
    }
    error_message = "Deployment must require the exact protected environment and AWS audience."
  }
  assert {
    condition     = jsondecode(aws_iam_role.build.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:aws-e/adp:environment:adp-build-test"
    error_message = "Build publication must not share the deployment environment."
  }
  assert {
    condition     = alltrue([for policy in [aws_iam_policy.deployment_base.policy, aws_iam_policy.deployment_services.policy] : length(policy) <= 6144])
    error_message = "Deployment managed policies exceed IAM quota."
  }
  assert {
    condition     = !can(regex("iam:|lambda:|eks:|secretsmanager:|ecr:PutImage|codebuild:UpdateProject", aws_iam_role_policy.build_dispatch.policy))
    error_message = "The build dispatcher gained deployment, project-mutation or credential authority."
  }
}

run "scan_identity_is_separate_and_service_role_is_exact" {
  command = plan
  variables { security_agent_space_id = "as-984b0721-375d-44f2-9e64-e0ff4c8111f8" }
  assert {
    condition     = jsondecode(aws_iam_role.scan.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:aws-e/adp:environment:adp-scan-test" && aws_iam_role.scan.max_session_duration == 21600
    error_message = "On-demand scans need their own protected environment and the existing long-run credential lifetime."
  }
  assert {
    condition     = one([for s in jsondecode(aws_iam_role_policy.scan.policy).Statement : s if s.Sid == "OnlyScannerServiceRole"]).Resource == "arn:aws:iam::123456789012:role/adp-test-securityagent-nightly"
    error_message = "Scanning can pass a role other than the existing Security Agent service role."
  }
  assert {
    condition     = !can(regex("lambda:|eks:|secretsmanager:|ecr:|iam:Create|iam:Put|codebuild:Update", aws_iam_role_policy.scan.policy))
    error_message = "Scanning has gained deployment or publication authority."
  }
}
