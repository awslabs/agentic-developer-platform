#!/bin/bash
# =============================================================================
# GitLab CE Omnibus Installation — Cloud-Init User Data
# =============================================================================
# Installs GitLab CE 19.1.x (pin policy: vendor's current maintained minor at spec time)
# via the official Omnibus package on Ubuntu 22.04 LTS.
# ALB terminates TLS, so GitLab listens on HTTP:80 only.
# =============================================================================
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

# -----------------------------------------------------------------------------
# System updates and dependencies
# -----------------------------------------------------------------------------
apt-get update -y
apt-get install -y curl openssh-server ca-certificates tzdata perl postfix

# -----------------------------------------------------------------------------
# Install GitLab CE Omnibus repository
# -----------------------------------------------------------------------------
curl -fsSL https://packages.gitlab.com/install/repositories/gitlab/gitlab-ce/script.deb.sh | bash

# -----------------------------------------------------------------------------
# Install GitLab CE
# -----------------------------------------------------------------------------
EXTERNAL_URL="${gitlab_external_url}" apt-get install -y gitlab-ce=19.1.*

# -----------------------------------------------------------------------------
# Configure GitLab — ALB terminates TLS, GitLab listens on HTTP:80
# -----------------------------------------------------------------------------
cat >> /etc/gitlab/gitlab.rb <<'GITLAB_CONFIG'

# ALB handles TLS termination; GitLab serves HTTP only
nginx['listen_port'] = 80
nginx['listen_https'] = false

# Trust the ALB's X-Forwarded headers
nginx['proxy_set_headers'] = {
  "X-Forwarded-Proto" => "https",
  "X-Forwarded-Ssl" => "on"
}

# Health check endpoint (used by ALB target group)
gitlab_rails['monitoring_whitelist'] = ['0.0.0.0/0']

# Custom nginx health endpoint for ALB health checks.
# Returns 200 directly from nginx regardless of Host header,
# avoiding Rails 404 when ALB probes via private IP.
nginx['custom_gitlab_server_config'] = "location /-/health {\n  access_log off;\n  return 200 'OK';\n}\n"
GITLAB_CONFIG

# -----------------------------------------------------------------------------
# OIDC Configuration — Fetch credentials from SSM and configure OmniAuth
# -----------------------------------------------------------------------------

# Install AWS CLI for SSM parameter retrieval
apt-get install -y awscli

OIDC_CLIENT_ID=$(aws ssm get-parameter \
  --name "/adp/${environment}/gitlab/oidc-client-id" \
  --region "${aws_region}" \
  --query "Parameter.Value" --output text)

OIDC_CLIENT_SECRET=$(aws ssm get-parameter \
  --name "/adp/${environment}/gitlab/oidc-client-secret" \
  --region "${aws_region}" \
  --with-decryption \
  --query "Parameter.Value" --output text)

OIDC_ISSUER=$(aws ssm get-parameter \
  --name "/adp/${environment}/gitlab/oidc-issuer" \
  --region "${aws_region}" \
  --query "Parameter.Value" --output text)

cat >> /etc/gitlab/gitlab.rb <<OIDC_CONFIG

# =============================================================================
# OmniAuth OIDC — Cognito Integration (Issue #3323)
# =============================================================================
gitlab_rails['omniauth_enabled'] = true
gitlab_rails['omniauth_allow_single_sign_on'] = ['openid_connect']
gitlab_rails['omniauth_auto_sign_in_with_provider'] = :openid_connect
gitlab_rails['omniauth_block_auto_created_users'] = false
gitlab_rails['omniauth_providers'] = [
  {
    name: "openid_connect",
    label: "ADP (Cognito)",
    args: {
      name: "openid_connect",
      scope: ["openid", "email", "profile"],
      response_type: "code",
      issuer: "$OIDC_ISSUER",
      discovery: true,
      client_auth_method: "basic",
      uid_field: "sub",
      client_options: {
        identifier: "$OIDC_CLIENT_ID",
        secret: "$OIDC_CLIENT_SECRET",
        redirect_uri: "https://${gitlab_domain}/users/auth/openid_connect/callback"
      }
    }
  }
]
OIDC_CONFIG

# -----------------------------------------------------------------------------
# Reconfigure GitLab with updated settings (includes OIDC)
# -----------------------------------------------------------------------------
gitlab-ctl reconfigure

# -----------------------------------------------------------------------------
# Install backup script (if backup bucket is configured)
# -----------------------------------------------------------------------------
%{ if backup_bucket_name != "" ~}
mkdir -p /opt/gitlab-backup
cat > /opt/gitlab-backup/backup.sh <<'BACKUP_SCRIPT'
${backup_script}
BACKUP_SCRIPT
chmod +x /opt/gitlab-backup/backup.sh

# Schedule daily backup at 02:00 UTC via cron
echo "0 2 * * * root /opt/gitlab-backup/backup.sh >> /var/log/gitlab-backup.log 2>&1" > /etc/cron.d/gitlab-backup
chmod 644 /etc/cron.d/gitlab-backup
%{ endif ~}

# -----------------------------------------------------------------------------
# Signal completion
# -----------------------------------------------------------------------------
echo "GitLab CE $(dpkg -l gitlab-ce | awk '/gitlab-ce/{print $3}') installed with OIDC" > /var/log/gitlab-install.log
