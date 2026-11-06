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
  environment                      = "test"
  gitlab_domain                    = "gitlab.example.test"
  route53_zone_name                = "example.test"
  cognito_user_pool_id             = "us-east-1_TestPool"
  cognito_domain                   = "adp-test"
  gateway_role_name                = "example-gateway"
  portal_operator_role_arns        = ["arn:aws:iam::123456789012:role/example-operator"]
  eks_oidc_provider_arn            = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE"
  reconciler_database_resource_arn = "arn:aws:rds-db:us-east-1:123456789012:dbuser:db-EXAMPLE/adp"
}

run "separate_reconciler_and_gateway_credentials" {
  command = apply

  variables {
    secretsmanager_endpoint_security_group_ids = ["sg-00000000000000001"]
  }

  assert {
    condition = (
      aws_security_group_rule.secretsmanager_endpoint_from_gitlab["sg-00000000000000001"].source_security_group_id == aws_security_group.instance.id &&
      aws_security_group_rule.secretsmanager_endpoint_from_gitlab["sg-00000000000000001"].protocol == "tcp" &&
      aws_security_group_rule.secretsmanager_endpoint_from_gitlab["sg-00000000000000001"].from_port == 443 &&
      aws_security_group_rule.secretsmanager_endpoint_from_gitlab["sg-00000000000000001"].to_port == 443
    )
    error_message = "Private endpoint bootstrap access must admit only HTTPS from the GitLab instance security group."
  }

  assert {
    condition = (
      jsondecode(aws_secretsmanager_secret_policy.gateway_signing_key[0].policy).Statement[0].Effect == "Deny" &&
      toset(jsondecode(aws_secretsmanager_secret_policy.gateway_signing_key[0].policy).Statement[0].Condition.ArnNotEquals["aws:PrincipalArn"]) ==
      toset(["arn:aws:iam::123456789012:role/example-gateway", "arn:aws:iam::123456789012:role/example-operator"]) &&
      !contains(jsondecode(aws_secretsmanager_secret_policy.reconciler_admin[0].policy).Statement[0].Condition.ArnNotEquals["aws:PrincipalArn"], "arn:aws:iam::123456789012:role/example-gateway")
    )
    error_message = "Secret policies must deny legacy broad readers and keep the gateway outside administrator access."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.gateway_signing_key[0].policy).Statement[1].Effect == "Deny" && jsondecode(aws_iam_role_policy.gateway_signing_key[0].policy).Statement[1].Resource == aws_secretsmanager_secret.reconciler_admin.arn
    error_message = "Gateway must explicitly deny the GitLab administrator credential, even with legacy broader policies."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.home_reconciler[0].policy).Statement[0].Resource == aws_secretsmanager_secret.reconciler_admin.arn
    error_message = "The dedicated reconciler must read only its own administrator credential."
  }
  assert {
    condition     = aws_instance.gitlab.root_block_device[0].delete_on_termination == false
    error_message = "GitLab repository storage must survive instance termination."
  }
  assert {
    condition     = strcontains(aws_iam_role.home_reconciler[0].assume_role_policy, "system:serviceaccount:adp-gateway:gitlab-home-reconciler")
    error_message = "Only the dedicated reconciler service account may assume its role."
  }
}
