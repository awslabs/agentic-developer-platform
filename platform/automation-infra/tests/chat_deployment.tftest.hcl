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


run "chat_release_joins_only_the_read_and_publish_rbac_groups" {
  command = plan
  variables {
    enable_chat_deployment  = true
    runtime_check_model_ids = ["global.anthropic.claude-sonnet-5"]
  }
  assert {
    condition     = toset(aws_eks_access_entry.chat_deployment[0].kubernetes_groups) == toset(["adp:chat-release-read", "adp:chat-release-publish"])
    error_message = "Chat release uses exactly the read-only prerequisite group and the gateway converge group (chat-release-publish-rbac.yaml, #8086)."
  }
}

run "chat_release_publishes_only_the_three_digest_parameters" {
  command = plan
  variables {
    enable_chat_deployment  = true
    runtime_check_model_ids = ["global.anthropic.claude-sonnet-5"]
  }
  assert {
    condition = toset(one([for s in jsondecode(aws_iam_role_policy.chat_deployment[0].policy).Statement : s if s.Action == ["ssm:PutParameter"]]).Resource) == toset([
      "arn:aws:ssm:us-east-1:123456789012:parameter/adp/test/gateway/chat-authority-worker-images",
      "arn:aws:ssm:us-east-1:123456789012:parameter/adp/test/gateway/chat-sandbox-image-digests",
      "arn:aws:ssm:us-east-1:123456789012:parameter/adp/test/gateway/chat-crew-image-digests",
    ])
    error_message = "The chat release identity may put only the three chat digest parameters (#8086); the activation switches stay with deploy-all.sh."
  }
  assert {
    condition = toset(one([for s in jsondecode(aws_iam_role_policy.chat_deployment[0].policy).Statement : s if s.Action == ["ssm:GetParameter"]]).Resource) == toset([
      "arn:aws:ssm:us-east-1:123456789012:parameter/adp/test/gateway/apigw-invoke-url",
      "arn:aws:ssm:us-east-1:123456789012:parameter/adp/test/gateway/crew-dashboard-origin",
      "arn:aws:ssm:us-east-1:123456789012:parameter/adp/test/gateway/crew-s3-interface-ip",
      # Read-only: the persistent-executor default is rendered from SSM on this path but only
      # published by deploy-all.sh --chat delegated / the operator (#8115).
      "arn:aws:ssm:us-east-1:123456789012:parameter/adp/test/gateway/chat-persistent-executor",
      "arn:aws:ssm:us-east-1:123456789012:parameter/adp/test/gateway/chat-authority-worker-images",
      "arn:aws:ssm:us-east-1:123456789012:parameter/adp/test/gateway/chat-sandbox-image-digests",
      "arn:aws:ssm:us-east-1:123456789012:parameter/adp/test/gateway/chat-crew-image-digests",
      "arn:aws:ssm:us-east-1:123456789012:parameter/adp/test/gateway/chat-worker-service-account",
      "arn:aws:ssm:us-east-1:123456789012:parameter/adp/test/gateway/chat-data-enabled",
    ])
    error_message = "The chat release identity reads exactly the Crew values, the persistent-executor default and the published chat workload identities (#8058, #8086, #8115)."
  }
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.chat_deployment[0].policy).Statement : s if contains(flatten([s.Action]), "ssm:PutParameter") && s.Action != ["ssm:PutParameter"]]) == 0
    error_message = "ssm:PutParameter must appear only in its own statement."
  }
}

run "gitlab_fixture_decrypt_is_secret_and_service_scoped" {
  command = plan
  variables {
    gitlab_checks_secret_arns  = ["arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/test/gitlab-secret-ABC123"]
    gitlab_checks_kms_key_arns = ["arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111"]
  }
  assert {
    condition = one([for s in jsondecode(aws_iam_role_policy.gitlab_checks.policy).Statement : s if s.Action == ["kms:Decrypt"]]).Condition.StringEquals == {
      "kms:ViaService"                  = "secretsmanager.us-east-1.amazonaws.com"
      "kms:EncryptionContext:SecretARN" = ["arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/test/gitlab-secret-ABC123"]
    }
    error_message = "Fixture decrypt must be restricted to Secrets Manager and the exact secret encryption contexts."
  }
  assert {
    condition     = one([for s in jsondecode(aws_iam_role_policy.gitlab_checks.policy).Statement : s if s.Action == ["kms:Decrypt"]]).Resource == ["arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111"]
    error_message = "Fixture decrypt must name only the admitted keys."
  }
}
