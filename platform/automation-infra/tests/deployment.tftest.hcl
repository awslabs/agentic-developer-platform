mock_provider "external" {
  mock_data "external" { defaults = { result = { verified = "true" } } }
}
mock_provider "aws" {
  mock_data "aws_iam_role" { defaults = { arn = "arn:aws:iam::123456789012:role/adp-test-worker", permissions_boundary = "arn:aws:iam::123456789012:policy/adp-test-workload-ceiling" } }
  mock_data "aws_caller_identity" { defaults = { account_id = "123456789012" } }
  mock_data "aws_iam_openid_connect_provider" {
    defaults = { arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com" }
  }
  mock_resource "aws_iam_role" { defaults = { arn = "arn:aws:iam::123456789012:role/test" } }
  mock_resource "aws_iam_policy" { defaults = { arn = "arn:aws:iam::123456789012:policy/test" } }
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
  build_project_names = ["adp-test-gateway-build"]
  deployment_role_boundaries = {
    "arn:aws:iam::123456789012:role/adp-test-worker" = "arn:aws:iam::123456789012:policy/adp-test-workload-ceiling"
  }
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

run "domain_cutover_targets_and_rule_publisher_are_scoped" {
  command = plan
  variables {
    additional_cluster_names = ["adp-test-cyber-eks", "adp-test-eks-cluster"]
    cape_instance_ids        = ["i-0123456789abcdef0"]
  }
  assert {
    condition     = keys(aws_eks_access_entry.domain_deployment) == ["adp-test-cyber-eks"]
    error_message = "Domain access must be explicit and must not duplicate the primary cluster entry."
  }
  assert {
    condition     = toset(jsondecode(aws_iam_role_policy.cape_registration[0].policy).Statement[0].Resource) == toset(["arn:aws:ssm:us-east-1::document/AWS-RunShellScript", "arn:aws:ec2:us-east-1:123456789012:instance/i-0123456789abcdef0"])
    error_message = "CAPE registration must not execute on arbitrary instances or SSM documents."
  }
  assert {
    condition     = jsondecode(aws_iam_role.rules.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:aws-e/adp:environment:adp-rules-test"
    error_message = "Untrusted public-rule parsing must not share deployment or build trust."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.rules.policy).Statement[1].Resource == "arn:aws:s3:::adp-test-cape-assets/yara-rules/public/florian-roth/*" && jsondecode(aws_iam_role_policy.rules.policy).Statement[5].NotResource == "arn:aws:s3:::adp-test-cape-assets/yara-rules/public/florian-roth/*"
    error_message = "Rules publication must explicitly deny writes outside public rules."
  }
  assert {
    condition     = aws_iam_role.deployment.max_session_duration >= 10800
    error_message = "The existing two-hour Windows build must retain credentials for teardown."
  }
}

run "workload_mutation_requires_an_immutable_ceiling" {
  command = apply
  variables {
    deployment_role_boundaries = {
      "arn:aws:iam::123456789012:role/adp-test-worker" = "arn:aws:iam::123456789012:policy/adp-test-workload-ceiling"
    }
    deployment_execution_resources = ["arn:aws:lambda:us-east-1:123456789012:function:adp-test-worker"]
  }
  override_data {
    target = data.aws_iam_role.admitted_workload["arn:aws:iam::123456789012:role/adp-test-worker"]
    values = {
      arn                  = "arn:aws:iam::123456789012:role/adp-test-worker"
      permissions_boundary = "arn:aws:iam::123456789012:policy/adp-test-workload-ceiling"
    }
  }
  assert {
    condition     = jsondecode(aws_iam_policy.deployment_role_lifecycle["0"].policy).Statement[0].Condition.ArnEquals["iam:PermissionsBoundary"] == "arn:aws:iam::123456789012:policy/adp-test-workload-ceiling"
    error_message = "CreateRole and privilege-bearing role mutations must require the reviewed ceiling."
  }
  assert {
    condition     = jsondecode(aws_iam_policy.deployment_role_lifecycle["0"].policy).Statement[1].Condition.ArnNotEquals["iam:PermissionsBoundary"] == "arn:aws:iam::123456789012:policy/adp-test-workload-ceiling"
    error_message = "An omitted/replaced ceiling must be explicitly denied."
  }
  assert {
    condition     = jsondecode(aws_iam_policy.deployment_role_lifecycle["0"].policy).Statement[3].Resource == ["arn:aws:iam::123456789012:role/adp-test-worker"] && !can(jsondecode(aws_iam_policy.deployment_role_lifecycle["0"].policy).Statement[3].Condition.ArnEquals)
    error_message = "PassRole needs an exact admitted role, not an unsupported PermissionsBoundary condition."
  }
  assert {
    condition     = contains(one([for s in jsondecode(aws_iam_role_policy.deployment_identity_management.policy).Statement : s if s.Sid == "ImmutableAutomationAndCeilings"]).Resource, "arn:aws:iam::123456789012:policy/adp-test-workload-ceiling")
    error_message = "Deployment must not be able to change the ceiling policy itself."
  }
  assert {
    condition     = one([for s in jsondecode(aws_iam_role_policy.deployment_identity_management.policy).Statement : s if s.Sid == "NeverRemoveWorkloadCeilings"]).Action == ["iam:DeleteRolePermissionsBoundary"]
    error_message = "Deployment must not remove a workload's ceiling."
  }
  assert {
    condition     = contains(jsondecode(aws_iam_role_policy.deployment_execution_ceiling.policy).Statement[0].NotResource, "arn:aws:lambda:us-east-1:123456789012:function:adp-test-worker") && !contains(jsondecode(aws_iam_role_policy.deployment_execution_ceiling.policy).Statement[0].NotResource, "arn:aws:lambda:us-east-1:123456789012:function:adp-test-unbounded")
    error_message = "An existing unbounded function must not provide a code-update bypass."
  }
}

run "unbounded_existing_role_cannot_be_admitted" {
  command = plan
  variables {
    deployment_role_boundaries = {
      "arn:aws:iam::123456789012:role/adp-test-worker" = "arn:aws:iam::123456789012:policy/adp-test-workload-ceiling"
    }
  }
  override_data {
    target = data.aws_iam_role.admitted_workload["arn:aws:iam::123456789012:role/adp-test-worker"]
    values = { arn = "arn:aws:iam::123456789012:role/adp-test-worker", permissions_boundary = null }
  }
  expect_failures = [data.aws_iam_role.admitted_workload]
}

run "empty_inventory_cannot_gain_cluster_admin_through_kubectl" {
  command = apply
  variables { deployment_role_boundaries = {} }
  assert {
    condition     = length(aws_eks_access_entry.deployment) == 0 && length(aws_eks_access_entry.domain_deployment) == 0
    error_message = "IAM API denies do not contain kubectl; do not grant EKS cluster access before workload admission."
  }
  assert {
    condition     = one([for s in jsondecode(aws_iam_role_policy.deployment_identity_management.policy).Statement : s if s.Sid == "AwaitWorkloadAdmission"]).Action == ["*"]
    error_message = "Empty admission must deny all AWS actions, including indirect event/source mutation paths."
  }
}

run "security_delivery_can_only_emit_its_root_event" {
  command = plan
  assert {
    condition     = aws_iam_role_policy.scan.role == aws_iam_role.scan.id
    error_message = "Delivery permission must belong to the workflow's trusted-scan role."
  }
  assert {
    condition     = one([for s in jsondecode(aws_iam_role_policy.scan.policy).Statement : s if s.Sid == "SecurityDeliveryRoot"]).Resource == "arn:aws:events:us-east-1:123456789012:event-bus/default"
    error_message = "Security delivery must be scoped to this account and region's default bus."
  }
  assert {
    condition     = one([for s in jsondecode(aws_iam_role_policy.scan.policy).Statement : s if s.Sid == "SecurityDeliveryRoot"]).Action == ["events:PutEvents"]
    error_message = "Security delivery must not receive EventBridge rule or target administration."
  }
  assert {
    condition = one([for s in jsondecode(aws_iam_role_policy.scan.policy).Statement : s if s.Sid == "SecurityDeliveryRoot"]).Condition.StringEquals == {
      "events:source"      = "adp.security-agent"
      "events:detail-type" = "ADP Agent Dispatch"
    }
    error_message = "Only the security-agent root event vocabulary may be emitted."
  }
}
