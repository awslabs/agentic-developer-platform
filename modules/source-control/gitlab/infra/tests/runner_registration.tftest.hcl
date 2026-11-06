mock_provider "aws" {
  mock_data "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/example-gateway" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/example-reconciler" }
  }
  mock_resource "aws_secretsmanager_secret" {
    defaults = { arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:example-000000" }
  }
  mock_resource "aws_lb" {
    defaults = {
      arn = "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/gitlab/0000000000000000"
    }
  }
  mock_resource "aws_lb_target_group" {
    defaults = {
      arn = "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/gitlab/0000000000000000"
    }
  }
  mock_resource "aws_lb_listener" {
    defaults = {
      arn = "arn:aws:elasticloadbalancing:us-east-1:123456789012:listener/app/gitlab/0000000000000000/0000000000000000"
    }
  }
}

override_data {
  target = data.terraform_remote_state.platform
  values = {
    outputs = {
      vpc_id             = "vpc-00000000000000000"
      vpc_cidr_block     = "10.0.0.0/16"
      private_subnet_ids = ["subnet-00000000000000001"]
    }
  }
}

variables {
  environment                  = "test"
  aws_region                   = "us-east-1"
  certificate_arn              = "arn:aws:acm:us-east-1:123456789012:certificate/example"
  gitlab_domain                = "gitlab.example.test"
  route53_zone_name            = "example.test"
  cognito_user_pool_id         = "us-east-1_TestPool"
  cognito_domain               = "adp-test"
  runner_group_ids             = [42]
  runner_registrar_role_name   = "example-registrar"
  runner_ca_exporter_role_name = "example-exporter"
  portal_operator_role_arns    = ["arn:aws:iam::123456789012:role/example-registrar"]
  runner_manager_role_arns     = { "42" = "arn:aws:iam::123456789012:role/manager-42" }
}

run "group_secret_container_only" {
  command = plan

  assert {
    condition     = length(aws_secretsmanager_secret.runner_group) == 1 && aws_secretsmanager_secret.runner_group["42"].name == "adp/test/gitlab-runner/42"
    error_message = "Only the configured numeric group gets a secret container."
  }

  assert {
    condition     = aws_ssm_parameter.runner_operation["42"].name == "/adp/test/gitlab-runner-operations/42" && aws_ssm_parameter.runner_operation["42"].value == "{}"
    error_message = "The operation parameter starts non-secret and carries no token."
  }

  assert {
    condition     = !strcontains(aws_ssm_parameter.runner_operation["42"].value, "glrt-synthetic-canary")
    error_message = "Runner token canary must never be stored in Terraform-managed operation state."
  }
}

run "default_ca_metadata_refresh" {
  command = plan
  variables {
    runner_group_ids             = []
    runner_registrar_role_name   = ""
    runner_ca_exporter_role_name = ""
    runner_manager_role_arns     = {}
    portal_operator_role_arns    = []
  }
  assert {
    condition = (
      alltrue([for statement in jsondecode(aws_secretsmanager_secret_policy.runner_ca.policy).Statement :
        !contains(statement.Action, "secretsmanager:DescribeSecret")
      ]) &&
      jsondecode(aws_secretsmanager_secret_policy.runner_ca.policy).Statement[0].Action == ["secretsmanager:GetSecretValue"] &&
      jsondecode(aws_secretsmanager_secret_policy.runner_ca.policy).Statement[0].Effect == "Deny" &&
      jsondecode(aws_secretsmanager_secret_policy.runner_ca.policy).Statement[0].Condition.ArnNotEquals["aws:PrincipalArn"] == ["arn:aws:iam::000000000000:role/adp-unconfigured"]
    )
    error_message = "Default CA policy must preserve identity-authorized metadata refresh while denying all real principals secret-value reads."
  }
}

run "configured_secret_metadata_refresh" {
  command = plan
  assert {
    condition = alltrue([for policy in concat(
      [aws_secretsmanager_secret_policy.runner_ca.policy],
      [for policy in aws_secretsmanager_secret_policy.runner_group : policy.policy]
      ) : (
      alltrue([for statement in jsondecode(policy).Statement : !contains(statement.Action, "secretsmanager:DescribeSecret")]) &&
      jsondecode(policy).Statement[0].Action == ["secretsmanager:GetSecretValue"] &&
      jsondecode(policy).Statement[0].Effect == "Deny" &&
      !contains(jsondecode(policy).Statement[0].Condition.ArnNotEquals["aws:PrincipalArn"], "arn:aws:iam::123456789012:role/example-deployer")
    )])
    error_message = "CA and group policies must permit identity-authorized metadata refresh without exempting the deployer from value-read denials."
  }
}

override_data {
  target = data.aws_iam_role.runner_registrar[0]
  values = { arn = "arn:aws:iam::123456789012:role/example-registrar", name = "example-registrar" }
}

override_data {
  target = data.aws_iam_role.runner_ca_exporter[0]
  values = { arn = "arn:aws:iam::123456789012:role/example-exporter", name = "example-exporter" }
}

run "reject_manager_without_group" {
  command = plan
  variables {
    runner_group_ids         = []
    runner_manager_role_arns = { "42" = "arn:aws:iam::123456789012:role/manager-42" }
  }
  expect_failures = [aws_secretsmanager_secret_policy.runner_ca]
}

run "reject_hostname_outside_private_zone" {
  command = plan
  variables {
    gitlab_domain = "gitlab.other.test"
  }
  expect_failures = [aws_ssm_parameter.gitlab_runner_url[0]]
}

run "reject_credential_bearing_hostname" {
  command = plan
  variables {
    gitlab_domain = "attacker@gitlab.example.test"
  }
  expect_failures = [aws_ssm_parameter.gitlab_runner_url[0]]
}

run "reject_ca_export_without_https_listener" {
  command = plan
  variables {
    certificate_arn = ""
  }
  expect_failures = [aws_iam_role_policy.runner_ca_exporter[0]]
  assert {
    condition     = length(aws_ssm_parameter.gitlab_runner_url) == 0 && length(aws_ssm_parameter.gitlab_runner_ca_secret_arn) == 0 && output.gitlab_runner_url == null
    error_message = "Without a certificate, do not advertise an unusable private HTTPS endpoint."
  }
}

run "exact_iam_authorities" {
  command = apply

  assert {
    condition = jsondecode(aws_secretsmanager_secret_policy.runner_group["42"].policy).Statement[0].Condition.ArnNotEquals["aws:PrincipalArn"] == [
      "arn:aws:iam::123456789012:role/example-registrar", "arn:aws:iam::123456789012:role/manager-42"
    ]
    error_message = "Only registrar and matching group manager may read group token."
  }
  assert {
    condition = jsondecode(aws_secretsmanager_secret_policy.runner_group["42"].policy).Statement[1].Condition.ArnNotEquals["aws:PrincipalArn"] == [
      "arn:aws:iam::123456789012:role/example-registrar"
    ]
    error_message = "Only registrar may write group tokens."
  }
  assert {
    condition = jsondecode(aws_secretsmanager_secret_policy.runner_ca.policy).Statement[0].Condition.ArnNotEquals["aws:PrincipalArn"] == [
      "arn:aws:iam::000000000000:role/adp-unconfigured", "arn:aws:iam::123456789012:role/manager-42", "arn:aws:iam::123456789012:role/example-registrar", "arn:aws:iam::123456789012:role/example-exporter"
    ]
    error_message = "CA reads must exclude gateway and unrelated identities."
  }
  assert {
    condition = jsondecode(aws_secretsmanager_secret_policy.runner_ca.policy).Statement[1].Condition.ArnNotEquals["aws:PrincipalArn"] == [
      "arn:aws:iam::000000000000:role/adp-unconfigured", "arn:aws:iam::123456789012:role/example-exporter"
    ]
    error_message = "Only the CA exporter may write CA material."
  }
  assert {
    condition     = length([for statement in jsondecode(aws_iam_role_policy.runner_ca_exporter[0].policy).Statement : statement if statement.Effect == "Deny" && contains(statement.Action, "secretsmanager:GetSecretValue") && contains(statement.Resource, aws_secretsmanager_secret.runner_group["42"].arn)]) == 1
    error_message = "Exporter must explicitly deny reads of group credentials."
  }
}

run "reference_only_discovery" {
  command = apply

  assert {
    condition     = aws_ssm_parameter.gitlab_runner_url[0].name == "/adp/test/gitlab/runner-url" && aws_ssm_parameter.gitlab_runner_url[0].value == "https://gitlab.example.test"
    error_message = "Runners must discover the private DNS hostname over HTTPS."
  }
  assert {
    condition     = aws_ssm_parameter.gitlab_runner_ca_secret_arn[0].name == "/adp/test/gitlab/ca-chain-secret-arn" && aws_ssm_parameter.gitlab_runner_ca_secret_arn[0].value == aws_secretsmanager_secret.runner_ca.arn
    error_message = "CA discovery must contain only the existing Secrets Manager ARN."
  }
  assert {
    condition     = output.gitlab_runner_ca_secret_arn == aws_secretsmanager_secret.runner_ca.arn && output.gitlab_runner_url == aws_ssm_parameter.gitlab_runner_url[0].value
    error_message = "Terraform outputs must contain references, not CA or runner secret values."
  }
  assert {
    condition     = aws_ssm_parameter.gitlab_url.value == "http://gitlab.test.adp.internal"
    error_message = "Keep the existing public discovery behavior unchanged."
  }
  assert {
    condition     = !strcontains(aws_ssm_parameter.gitlab_runner_ca_secret_arn[0].value, "-----BEGIN CERTIFICATE-----") && !strcontains(output.gitlab_runner_ca_secret_arn, "glrt-synthetic-canary")
    error_message = "Neither CA PEM nor runner token may enter Terraform discovery."
  }
}

run "private_rooted_url_preserves_public_url" {
  command = plan
  variables {
    cloudfront_domain = "public.example.test"
  }
  assert {
    condition = (
      aws_ssm_parameter.gitlab_runner_url[0].value == "https://gitlab.example.test/gitlab" &&
      aws_ssm_parameter.gitlab_url.value == "https://public.example.test/gitlab" &&
      output.gitlab_runner_url == "https://gitlab.example.test/gitlab"
    )
    error_message = "Private HTTPS must retain the installation path without replacing public discovery."
  }
}

run "cross_group_isolation" {
  command = plan
  variables {
    runner_group_ids = [42, 43]
    runner_manager_role_arns = {
      "42" = "arn:aws:iam::123456789012:role/manager-42"
      "43" = "arn:aws:iam::123456789012:role/manager-43"
    }
  }
  assert {
    condition = (!contains(jsondecode(aws_secretsmanager_secret_policy.runner_group["42"].policy).Statement[0].Condition.ArnNotEquals["aws:PrincipalArn"], "arn:aws:iam::123456789012:role/manager-43") &&
    !contains(jsondecode(aws_secretsmanager_secret_policy.runner_group["43"].policy).Statement[0].Condition.ArnNotEquals["aws:PrincipalArn"], "arn:aws:iam::123456789012:role/manager-42"))
    error_message = "No manager may read another group's token."
  }
}

run "reject_manager_aliasing_operator" {
  command = plan
  variables {
    runner_manager_role_arns = { "42" = "arn:aws:iam::123456789012:role/example-registrar" }
  }
  expect_failures = [aws_secretsmanager_secret_policy.runner_group["42"]]
}

run "reject_unapproved_registrar" {
  command = plan
  variables {
    portal_operator_role_arns = []
  }
  expect_failures = [aws_iam_role_policy.runner_registrar[0]]
}

run "registrar_admin_access_does_not_grant_signing_key_access" {
  command = plan
  variables {
    gateway_role_name                = "example-gateway"
    portal_operator_role_arns        = ["arn:aws:iam::123456789012:role/example-operator"]
    runner_registrar_admin_role_arns = ["arn:aws:iam::123456789012:role/example-registrar"]
  }
  assert {
    condition = (
      contains(jsondecode(aws_secretsmanager_secret_policy.reconciler_admin[0].policy).Statement[0].Condition.ArnNotEquals["aws:PrincipalArn"], "arn:aws:iam::123456789012:role/example-registrar") &&
      !contains(jsondecode(aws_secretsmanager_secret_policy.gateway_signing_key[0].policy).Statement[0].Condition.ArnNotEquals["aws:PrincipalArn"], "arn:aws:iam::123456789012:role/example-registrar")
    )
    error_message = "Dedicated registrar must stay denied access to the portal signing key."
  }
}

run "reject_unrelated_registrar_admin_role" {
  command = plan
  variables {
    runner_registrar_admin_role_arns = ["arn:aws:iam::123456789012:role/unrelated"]
  }
  expect_failures = [aws_secretsmanager_secret_policy.reconciler_admin[0]]
}

run "reject_foreign_manager" {
  command = plan
  variables {
    runner_manager_role_arns = { "42" = "arn:aws:iam::999999999999:role/manager-42" }
  }
  expect_failures = [aws_secretsmanager_secret_policy.runner_group["42"]]
}

run "reject_unmapped_manager" {
  command = plan
  variables {
    runner_manager_role_arns = { "43" = "arn:aws:iam::123456789012:role/manager-43" }
  }
  expect_failures = [aws_secretsmanager_secret_policy.runner_group["42"]]
}

override_data {
  target = data.aws_caller_identity.current
  values = { account_id = "123456789012" }
}

run "registration_alone_protects_admin_secret" {
  command = plan
  variables {
    gateway_role_name     = ""
    eks_oidc_provider_arn = ""
  }
  assert {
    condition = (length(aws_secretsmanager_secret_policy.reconciler_admin) == 1 &&
    !contains(jsondecode(aws_secretsmanager_secret_policy.reconciler_admin[0].policy).Statement[0].Condition.ArnNotEquals["aws:PrincipalArn"], "arn:aws:iam::123456789012:role/example-exporter"))
    error_message = "Registrar must not leave the admin secret open to the exporter or unrelated roles."
  }
}
