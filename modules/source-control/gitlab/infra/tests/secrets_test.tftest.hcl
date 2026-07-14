# =============================================================================
# GitLab Secrets — Terraform Test
# =============================================================================
# Validates that the gitlab-root-password secret is created with the correct
# name pattern and tags.
# =============================================================================

variables {
  environment          = "dev"
  aws_region           = "us-east-1"
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

run "gitlab_root_password_version_placeholder" {
  command = plan

  assert {
    condition     = aws_secretsmanager_secret_version.gitlab_root_password.secret_string == "ROTATE-ME-BEFORE-EXPOSURE"
    error_message = "Secret version must contain the ROTATE-ME placeholder"
  }
}
