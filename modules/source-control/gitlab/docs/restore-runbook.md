# GitLab Restore Runbook

Restore a GitLab CE instance from an S3 backup. Target: RTO 4 hours.

## Prerequisites

- AWS CLI configured with access to the backup bucket
- SSH/SSM access to the target GitLab instance
- The target instance has GitLab CE installed (same major version as backup)

## Identify Available Backups

```bash
# List recent backups
aws s3 ls s3://adp-dev-gitlab-backups/daily/ --recursive | sort | tail -10

# List config backups
aws s3 ls s3://adp-dev-gitlab-backups/config/ --recursive | sort | tail -10
```

## Restore Procedure

### Step 1: Stop GitLab services (keep PostgreSQL and Redis running)

```bash
sudo gitlab-ctl stop puma
sudo gitlab-ctl stop sidekiq
sudo gitlab-ctl status
```

### Step 2: Download backup from S3

```bash
# Replace TIMESTAMP with the desired backup timestamp
TIMESTAMP="20260708_020000"
BUCKET="adp-dev-gitlab-backups"

# Download the backup archive
aws s3 cp "s3://${BUCKET}/daily/${TIMESTAMP}_gitlab_backup.tar" \
  /var/opt/gitlab/backups/ --sse AES256

# Download configuration files
aws s3 cp "s3://${BUCKET}/config/${TIMESTAMP}_gitlab.rb" /tmp/gitlab.rb.restore
aws s3 cp "s3://${BUCKET}/config/${TIMESTAMP}_gitlab-secrets.json" /tmp/gitlab-secrets.json.restore
```

### Step 3: Restore configuration (if instance is new/replaced)

```bash
# Only needed if this is a fresh instance or config was lost
sudo cp /tmp/gitlab.rb.restore /etc/gitlab/gitlab.rb
sudo cp /tmp/gitlab-secrets.json.restore /etc/gitlab/gitlab-secrets.json
sudo gitlab-ctl reconfigure
```

### Step 4: Restore the backup

```bash
# The backup file name format is: EPOCH_YYYY_MM_DD_VERSION_gitlab_backup.tar
# GitLab expects just the prefix before _gitlab_backup.tar
BACKUP_NAME=$(basename /var/opt/gitlab/backups/${TIMESTAMP}_gitlab_backup.tar _gitlab_backup.tar)

sudo gitlab-backup restore BACKUP=${BACKUP_NAME}
```

When prompted, confirm with `yes` to proceed with the restore.

### Step 5: Reconfigure and restart

```bash
sudo gitlab-ctl reconfigure
sudo gitlab-ctl restart
```

### Step 6: Verify the restore

```bash
# Check GitLab is running
sudo gitlab-ctl status

# Check readiness endpoint
curl -sk https://gitlab.dev.adp.internal/-/readiness | jq .status

# Check database migrations are current
sudo gitlab-rake db:migrate:status | tail -5

# Verify a known repository exists
sudo gitlab-rake gitlab:check SANITIZE=true
```

## Restoring from Glacier

Backups older than 30 days are transitioned to Glacier. To restore:

```bash
# Initiate restore from Glacier (takes 3-5 hours for Standard retrieval)
aws s3api restore-object \
  --bucket adp-dev-gitlab-backups \
  --key "daily/${TIMESTAMP}_gitlab_backup.tar" \
  --restore-request '{"Days":7,"GlacierJobParameters":{"Tier":"Standard"}}'

# Check restore status
aws s3api head-object \
  --bucket adp-dev-gitlab-backups \
  --key "daily/${TIMESTAMP}_gitlab_backup.tar" \
  --query 'Restore'

# Once restore is complete (Restore header shows ongoing-request="false"),
# download as in Step 2
```

## Troubleshooting

| Issue | Resolution |
|-------|-----------|
| `gitlab-backup restore` fails with version mismatch | Install the same GitLab version as the backup: `apt-get install gitlab-ce=VERSION` |
| Permission denied on backup file | `chown git:git /var/opt/gitlab/backups/*.tar` |
| Database restore fails | Ensure no active connections: `sudo gitlab-ctl stop puma && sudo gitlab-ctl stop sidekiq` |
| Secrets mismatch after restore | Restore `gitlab-secrets.json` from the same timestamp as the backup, then reconfigure |

## Important Notes

- `gitlab-backup` does NOT include `/etc/gitlab/gitlab.rb` or `/etc/gitlab/gitlab-secrets.json` — these are backed up separately to the `config/` prefix.
- `gitlab-secrets.json` contains encryption keys for CI/CD variables and 2FA. Without it, encrypted data in the database cannot be decrypted.
- Always restore configuration files from the same timestamp as the database backup.
