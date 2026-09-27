mock_provider "external" {}
mock_provider "aws" {
  mock_data "aws_caller_identity" { defaults = { account_id = "123456789012" } }
  mock_data "aws_iam_openid_connect_provider" {
    defaults = { arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com" }
  }
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
  repository          = "aws-e/adp"
  build_project_names = ["adp-test-superplane-executor"]
}

run "skypilot_release_is_bounded" {
  command = plan
  variables { enable_skypilot_deployment = true }
  assert {
    condition     = aws_eks_access_policy_association.skypilot_deployment[0].access_scope[0].type == "namespace" && toset(aws_eks_access_policy_association.skypilot_deployment[0].access_scope[0].namespaces) == toset(["skypilot"])
    error_message = "SkyPilot release must not administer the control plane or cluster."
  }
  assert {
    condition     = jsondecode(aws_iam_role.skypilot_deployment[0].assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:aws-e/adp:environment:adp-skypilot-deploy-test"
    error_message = "Only the dedicated protected environment can obtain the role."
  }
  assert {
    condition     = toset(flatten([for statement in jsondecode(aws_iam_role_policy.skypilot_deployment[0].policy).Statement : statement.Action])) == toset(["eks:DescribeCluster", "ssm:GetParameter"])
    error_message = "The manifest lane must not acquire secrets, IAM or publishing authority."
  }
}
