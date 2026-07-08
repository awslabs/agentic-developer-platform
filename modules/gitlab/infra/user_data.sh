#!/bin/bash
# =============================================================================
# GitLab CE Omnibus Installation — Cloud-Init User Data
# =============================================================================
# Installs GitLab CE 17.x via the official Omnibus package on Ubuntu 22.04 LTS.
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
EXTERNAL_URL="${gitlab_external_url}" apt-get install -y gitlab-ce

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
GITLAB_CONFIG

# -----------------------------------------------------------------------------
# Reconfigure GitLab with updated settings
# -----------------------------------------------------------------------------
gitlab-ctl reconfigure

# -----------------------------------------------------------------------------
# Signal completion
# -----------------------------------------------------------------------------
echo "GitLab CE installation complete" > /var/log/gitlab-install.log
