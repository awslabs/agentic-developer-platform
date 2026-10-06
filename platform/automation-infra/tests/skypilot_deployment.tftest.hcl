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
    condition     = module.superplane_skypilot_deployment.access_associations[0].access_scope[0].type == "namespace" && toset(module.superplane_skypilot_deployment.access_associations[0].access_scope[0].namespaces) == toset(["skypilot"])
    error_message = "SkyPilot release must not administer the control plane or cluster."
  }
  assert {
    condition     = module.superplane_skypilot_deployment.access_associations[0].policy_arn == "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
    error_message = "The namespace-scoped association must permit its own Namespace security labels."
  }
  assert {
    condition     = jsondecode(module.superplane_skypilot_deployment.deployment_roles[0].assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:aws-e/adp:environment:adp-skypilot-deploy-test"
    error_message = "Only the dedicated protected environment can obtain the role."
  }
  assert {
    condition     = toset(flatten([for statement in jsondecode(module.superplane_skypilot_deployment.deployment_policies[0].policy).Statement : statement.Action])) == toset(["eks:DescribeCluster", "ssm:GetParameter"])
    error_message = "The manifest lane must not acquire secrets, IAM or publishing authority."
  }
}

run "skypilot_is_absent_by_default" {
  command = plan
  assert {
    condition     = length(module.superplane_skypilot_deployment.deployment_roles) == 0 && length(module.superplane_skypilot_deployment.deployment_policies) == 0 && length(module.superplane_skypilot_deployment.access_associations) == 0
    error_message = "Moving app definitions must not activate the deployment identity."
  }
}

run "skypilot_preserves_existing_identity_and_parameter_scope" {
  command = plan
  variables { enable_skypilot_deployment = true }
  assert {
    condition     = module.superplane_skypilot_deployment.deployment_roles[0].name == "adp-test-skypilot-trusted-deployment" && module.superplane_skypilot_deployment.deployment_policies[0].name == "release-existing-skypilot"
    error_message = "A source ownership move must retain existing IAM identities."
  }
  assert {
    condition = toset(one([for s in jsondecode(module.superplane_skypilot_deployment.deployment_policies[0].policy).Statement : s if contains(s.Action, "ssm:GetParameter")]).Resource) == toset([for name in [
      "control-plane-role-arn", "skypilot-role-arn", "namespace", "skypilot-namespace", "skypilot-image",
      "database-secret-name", "jwt-secret-name", "workspace-cluster-context", "aws-region"
    ] : "arn:aws:ssm:us-east-1:123456789012:parameter/adp/test/superplane/${name}"])
    error_message = "The extracted role must retain exact environment-scoped SSM references."
  }
}
