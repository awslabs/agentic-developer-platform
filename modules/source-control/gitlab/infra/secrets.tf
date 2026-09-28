# =============================================================================
# GitLab CE Infrastructure — Secrets Manager (Break-Glass Access)
# =============================================================================
# Stores the GitLab root password in Secrets Manager for break-glass access.
# The placeholder value MUST be rotated before the instance is exposed to the
# internet (i.e., before /gitlab/* CloudFront behavior goes live).
#
# Post-apply ops step:
#   1. SSM into the GitLab instance
#   2. gitlab-rails runner "User.find(1).update!(password: '<new>')"
#   3. Update the secret version in Secrets Manager to match
# =============================================================================

resource "aws_secretsmanager_secret" "gitlab_root_password" {
  name        = "adp/${var.environment}/gitlab-root-password"
  description = "Break-glass root password for GitLab instance. Rotate before internet exposure."

  tags = merge(local.common_tags, {
    Component = "gitlab"
    Purpose   = "break-glass"
  })
}

resource "aws_secretsmanager_secret_version" "gitlab_root_password" {
  secret_id     = aws_secretsmanager_secret.gitlab_root_password.id
  secret_string = "ROTATE-ME-BEFORE-EXPOSURE"

  lifecycle {
    ignore_changes = [secret_string]
  }
}
