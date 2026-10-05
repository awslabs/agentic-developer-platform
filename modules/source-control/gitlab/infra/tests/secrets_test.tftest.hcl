mock_provider "aws" {}

override_data {
  target = data.terraform_remote_state.platform
  values = {
    outputs = {
      vpc_id             = "vpc-00000000000000000"
      vpc_cidr_block     = "10.0.0.0/16"
      private_subnet_ids = ["subnet-00000000000000001", "subnet-00000000000000002"]
    }
  }
}

# =============================================================================
# GitLab Secrets — Terraform Test
# =============================================================================
# Validates that the gitlab-root-password secret is created with the correct
# name pattern and tags.
# =============================================================================

variables {
  environment          = "dev"
  aws_region           = "us-east-1"
  certificate_arn      = "arn:aws:acm:us-east-1:123456789012:certificate/test-cert-id"
  gitlab_domain        = "gitlab.dev.adp.internal"
  route53_zone_name    = "dev.adp.internal"
  cognito_user_pool_id = "us-east-1_test123"
  cognito_domain       = "adp-dev"
}

run "gitlab_root_password_secret_name" {
  command = plan

  assert {
    condition     = aws_secretsmanager_secret.gitlab_root_password.name == "adp/dev/gitlab-root-password"
    error_message = "Secret name must follow pattern adp/{environment}/gitlab-root-password"
  }
}

run "gitlab_root_password_secret_description" {
  command = plan

  assert {
    condition     = aws_secretsmanager_secret.gitlab_root_password.description == "Break-glass root password for GitLab instance. Rotate before internet exposure."
    error_message = "Secret must have the correct description"
  }
}

run "gitlab_root_password_secret_tags" {
  command = plan

  assert {
    condition     = aws_secretsmanager_secret.gitlab_root_password.tags["Component"] == "gitlab"
    error_message = "Secret must have Component=gitlab tag"
  }

  assert {
    condition     = aws_secretsmanager_secret.gitlab_root_password.tags["Purpose"] == "break-glass"
    error_message = "Secret must have Purpose=break-glass tag"
  }
}

run "gitlab_root_password_recovery" {
  command = plan
  assert {
    condition     = aws_secretsmanager_secret.gitlab_root_password.recovery_window_in_days == 30
    error_message = "Break-glass secret must retain a recovery window"
  }
}
