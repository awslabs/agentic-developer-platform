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
  name_prefix               = "adp-test"
  environment               = "test"
  aws_region                = "us-east-1"
  cluster_name              = "adp-test-eks-cluster"
  repository                = "aws-e/adp"
  build_project_names       = ["adp-test-superplane-executor"]
  enable_context_deployment = true
}

run "context_reads_exact_prerequisites_without_managing_receiver_identity" {
  command = plan
  assert {
    condition = toset(one([for s in jsondecode(aws_iam_role_policy.context_deployment[0].policy).Statement : s if s.Action == ["ssm:GetParameter"]]).Resource) == toset([
      "arn:aws:ssm:us-east-1:123456789012:parameter/adp/test/agent-context/ingestion-queue-url",
      "arn:aws:ssm:us-east-1:123456789012:parameter/adp/test/agent-context/ingestion-receiver-role-arn",
      "arn:aws:ssm:us-east-1:123456789012:parameter/adp/test/agent-context/neptune-endpoint",
      "arn:aws:ssm:us-east-1:123456789012:parameter/adp/test/agent-context/neptune-port",
      "arn:aws:ssm:us-east-1:123456789012:parameter/adp/test/agent-context/neptune-bulk-load-role-arn",
      "arn:aws:ssm:us-east-1:123456789012:parameter/adp/test/rds/endpoint",
    ])
    error_message = "Context deployment reads only the exact existing prerequisite parameters."
  }
  assert {
    condition = toset(flatten([for s in jsondecode(aws_iam_role_policy.context_deployment[0].policy).Statement : s.Action])) == toset([
      "eks:DescribeCluster", "ssm:GetParameter", "secretsmanager:GetSecretValue", "ecr:DescribeImages"
    ])
    error_message = "Receiver lookup must not grant IAM, SSM write, assume-role or provider execution actions."
  }
  assert {
    condition     = jsondecode(aws_iam_role.context_deployment[0].assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:aws-e/adp:environment:adp-context-deploy-test"
    error_message = "The existing protected Context workflow trust must remain exact."
  }
}
