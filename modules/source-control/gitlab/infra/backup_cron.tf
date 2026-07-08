# =============================================================================
# GitLab CE Infrastructure — Backup Cron
# =============================================================================
# Installs the backup script on the instance and configures a cron job
# to run daily at 02:00 UTC via SSM Association.
# =============================================================================

# -----------------------------------------------------------------------------
# SSM Document — Run backup script
# -----------------------------------------------------------------------------

resource "aws_ssm_document" "gitlab_backup" {
  count = var.backup_enabled ? 1 : 0

  name            = "${local.name_prefix}-backup"
  document_type   = "Command"
  document_format = "YAML"

  content = yamlencode({
    schemaVersion = "2.2"
    description   = "Run GitLab backup and upload to S3"
    mainSteps = [
      {
        action = "aws:runShellScript"
        name   = "runBackup"
        inputs = {
          runCommand     = ["/opt/gitlab-backup/backup.sh"]
          timeoutSeconds = "3600"
        }
      }
    ]
  })

  tags = merge(local.common_tags, {
    Name    = "${local.name_prefix}-backup"
    Service = "ssm"
  })
}

# -----------------------------------------------------------------------------
# SSM Maintenance Window — Daily at 02:00 UTC
# -----------------------------------------------------------------------------

resource "aws_ssm_maintenance_window" "gitlab_backup" {
  count = var.backup_enabled ? 1 : 0

  name                       = "${local.name_prefix}-backup"
  schedule                   = var.backup_schedule
  duration                   = 2
  cutoff                     = 1
  allow_unassociated_targets = false

  tags = merge(local.common_tags, {
    Name    = "${local.name_prefix}-backup-window"
    Service = "ssm"
  })
}

resource "aws_ssm_maintenance_window_target" "gitlab_backup" {
  count = var.backup_enabled ? 1 : 0

  window_id     = aws_ssm_maintenance_window.gitlab_backup[0].id
  name          = "${local.name_prefix}-instance"
  resource_type = "INSTANCE"

  targets {
    key    = "InstanceIds"
    values = [aws_instance.gitlab.id]
  }
}

resource "aws_ssm_maintenance_window_task" "gitlab_backup" {
  count = var.backup_enabled ? 1 : 0

  window_id       = aws_ssm_maintenance_window.gitlab_backup[0].id
  task_type       = "RUN_COMMAND"
  task_arn        = aws_ssm_document.gitlab_backup[0].arn
  priority        = 1
  max_concurrency = "1"
  max_errors      = "0"

  targets {
    key    = "WindowTargetIds"
    values = [aws_ssm_maintenance_window_target.gitlab_backup[0].id]
  }

  task_invocation_parameters {
    run_command_parameters {
      timeout_seconds = 3600
    }
  }
}
