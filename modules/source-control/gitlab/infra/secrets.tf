# =============================================================================
# GitLab CE Infrastructure — Secrets Manager (Break-Glass Access)
# =============================================================================
# Stores the GitLab root password in Secrets Manager for break-glass access.
# No default password is installed. Store the actual instance-generated root
# password through the controlled bootstrap procedure before break-glass use.
#
# Post-apply ops step:
#   1. SSM into the GitLab instance
#   2. gitlab-rails runner "User.find(1).update!(password: '<new>')"
#   3. Update the secret version in Secrets Manager to match
# =============================================================================

resource "aws_secretsmanager_secret" "gitlab_root_password" {
  name                    = "adp/${var.environment}/gitlab-root-password"
  description             = "Break-glass root password for GitLab instance. Rotate before internet exposure."
  recovery_window_in_days = 30

  tags = merge(local.common_tags, {
    Component = "gitlab"
    Purpose   = "break-glass"
  })
}

removed {
  from = aws_secretsmanager_secret_version.gitlab_root_password
  lifecycle {
    destroy = false
  }
}
