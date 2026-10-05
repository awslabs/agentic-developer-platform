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

run "chat_custom_resources_remain_namespace_scoped" {
  command = plan
  variables {
    enable_chat_deployment  = true
    runtime_check_model_ids = ["global.anthropic.claude-sonnet-5"]
  }
  assert {
    condition     = aws_eks_access_policy_association.chat_deployment[0].access_scope[0].type == "namespace" && toset(aws_eks_access_policy_association.chat_deployment[0].access_scope[0].namespaces) == toset(["adp-gateway-agents"])
    error_message = "Chat release authority must never include another namespace or the cluster."
  }
  assert {
    condition     = aws_eks_access_policy_association.chat_deployment[0].policy_arn == "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
    error_message = "The namespace-scoped association must include custom KEDA resources."
  }
}
