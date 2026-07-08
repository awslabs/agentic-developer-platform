#!/bin/bash
# =============================================================================
# GitLab Backup Script
# =============================================================================
# Creates a GitLab backup, uploads to S3, and prunes old local backups.
# Installed by Terraform; runs daily via cron at 02:00 UTC.
# =============================================================================
set -euo pipefail

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BUCKET="${bucket_name}"
LOG_FILE="/var/log/gitlab-backup.log"

exec > >(tee -a "$LOG_FILE") 2>&1
echo "=== GitLab backup started: $TIMESTAMP ==="

# -----------------------------------------------------------------------------
# Create GitLab backup (repos, database, uploads, CI artifacts)
# STRATEGY=copy allows backup without downtime
# -----------------------------------------------------------------------------
gitlab-backup create STRATEGY=copy

# -----------------------------------------------------------------------------
# Find the latest backup file
# -----------------------------------------------------------------------------
BACKUP_FILE=$(ls -t /var/opt/gitlab/backups/*_gitlab_backup.tar 2>/dev/null | head -1)

if [ -z "$BACKUP_FILE" ]; then
  echo "ERROR: No backup file found after gitlab-backup create"
  exit 1
fi

echo "Uploading backup: $BACKUP_FILE"

# -----------------------------------------------------------------------------
# Upload backup archive to S3
# -----------------------------------------------------------------------------
aws s3 cp "$BACKUP_FILE" "s3://$${BUCKET}/daily/$${TIMESTAMP}_gitlab_backup.tar" \
  --sse AES256 \
  --only-show-errors

# -----------------------------------------------------------------------------
# Backup configuration files (not included in gitlab-backup)
# -----------------------------------------------------------------------------
aws s3 cp /etc/gitlab/gitlab.rb \
  "s3://$${BUCKET}/config/$${TIMESTAMP}_gitlab.rb" \
  --sse AES256 \
  --only-show-errors

aws s3 cp /etc/gitlab/gitlab-secrets.json \
  "s3://$${BUCKET}/config/$${TIMESTAMP}_gitlab-secrets.json" \
  --sse AES256 \
  --only-show-errors

# -----------------------------------------------------------------------------
# Prune local backups older than 7 days
# -----------------------------------------------------------------------------
find /var/opt/gitlab/backups -name "*_gitlab_backup.tar" -mtime +7 -delete

echo "=== GitLab backup completed: $(date +%Y%m%d_%H%M%S) ==="
