mock_provider "aws" {
  mock_data "aws_iam_openid_connect_provider" {
    defaults = { arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com" }
  }
}
variables {
  account_id  = "123456789012"
  aws_region  = "us-west-2"
  environment = "test"
  repository  = "example/platform"
  runner_activation = {
    group_id              = 37
    cluster_name          = "adp-test-eks-cluster"
    access_entry_arn      = "arn:aws:eks:us-west-2:123456789012:access-entry/adp-test-eks-cluster/role/123456789012/adp-test-gitlab-trusted-deployment/11111111-2222-3333-4444-555555555555"
    eks_oidc_provider_arn = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-west-2.amazonaws.com/id/ABC123"
    vpc_id                = "vpc-0123456789abcdef0"
  }
}
run "no_implicit_connector_update" {
  command = plan
  assert {
    condition     = length(aws_iam_role_policy.runner_connector_update) == 0
    error_message = "Activation must not implicitly grant network mutation."
  }
}
run "only_the_bound_existing_rule" {
  command = plan
  variables {
    runner_connector_update = {
      manager_security_group_id = "sg-0123456789abcdef4"
      cache_security_group_id   = "sg-0123456789abcdef3"
      cache_manager_rule_id     = "sgr-0123456789abcdef3"
      group_id                  = 37
      security_group_id         = "sg-0123456789abcdef0"
      security_group_rule_id    = "sgr-0123456789abcdef0"
    }
  }
  assert {
    condition = jsondecode(aws_iam_role_policy.runner_connector_update[0].policy) == jsondecode(jsonencode({
      Version = "2012-10-17"
      Statement = [{
        Sid       = "CorrectBoundJobConnector", Effect = "Allow", Action = "ec2:ModifySecurityGroupRules"
        Resource  = ["arn:aws:ec2:us-west-2:123456789012:security-group/sg-0123456789abcdef0", "arn:aws:ec2:us-west-2:123456789012:security-group-rule/sgr-0123456789abcdef0", "arn:aws:ec2:us-west-2:123456789012:security-group/sg-0123456789abcdef3", "arn:aws:ec2:us-west-2:123456789012:security-group-rule/sgr-0123456789abcdef3", "arn:aws:ec2:us-west-2:123456789012:security-group/sg-0123456789abcdef4"]
        Condition = { StringEquals = { "aws:RequestedRegion" = "us-west-2" } }
      }]
    }))
    error_message = "Only the existing rule and job group may change; no new ingress or instance control."
  }
}
run "reject_foreign_group" {
  command = plan
  variables {
    runner_connector_update = {
      manager_security_group_id = "sg-0123456789abcdef4"
      cache_security_group_id   = "sg-0123456789abcdef3"
      cache_manager_rule_id     = "sgr-0123456789abcdef3"
      group_id                  = 99
      security_group_id         = "sg-0123456789abcdef0"
      security_group_rule_id    = "sgr-0123456789abcdef0"
    }
  }
  expect_failures = [var.runner_connector_update]
}
run "reject_rule_wildcard" {
  command = plan
  variables {
    runner_connector_update = {
      manager_security_group_id = "sg-0123456789abcdef4"
      cache_security_group_id   = "sg-0123456789abcdef3"
      cache_manager_rule_id     = "sgr-0123456789abcdef3"
      group_id                  = 37
      security_group_id         = "sg-0123456789abcdef0"
      security_group_rule_id    = "*"
    }
  }
  expect_failures = [var.runner_connector_update]
}

run "reject_manager_source_wildcard" {
  command = plan
  variables {
    runner_connector_update = {
      group_id                  = 37
      security_group_id         = "sg-0123456789abcdef0"
      security_group_rule_id    = "sgr-0123456789abcdef0"
      cache_security_group_id   = "sg-0123456789abcdef3"
      cache_manager_rule_id     = "sgr-0123456789abcdef3"
      manager_security_group_id = "*"
    }
  }
  expect_failures = [var.runner_connector_update]
}

run "reject_manager_source_as_destination" {
  command = plan
  variables {
    runner_connector_update = {
      group_id                  = 37
      security_group_id         = "sg-0123456789abcdef0"
      security_group_rule_id    = "sgr-0123456789abcdef0"
      cache_security_group_id   = "sg-0123456789abcdef3"
      cache_manager_rule_id     = "sgr-0123456789abcdef3"
      manager_security_group_id = "sg-0123456789abcdef0"
    }
  }
  expect_failures = [var.runner_connector_update]
}
