#!/bin/bash
# =============================================================================
# Phase 3: CAPE Dependencies + Base Install
# =============================================================================
# Run via SSM send-command (NOT in Terraform user-data) to keep TF idempotent.
#
# Usage:
#   INSTANCE_ID=$(terraform -chdir=../infra output -raw cape_host_instance_id)
#   aws ssm send-command \
#     --instance-ids "$INSTANCE_ID" \
#     --document-name "AWS-RunShellScript" \
#     --parameters "commands=[\"bash /tmp/01-cape-deps.sh\"]" \
#     --timeout-seconds 1800
#
# Prerequisites: Instance must be running with SSM agent active.
# =============================================================================
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

echo "=== Phase 3: Installing CAPE dependencies ==="

# ---------------------------------------------------------------------------
# System packages
# ---------------------------------------------------------------------------
apt-get update -y
apt-get upgrade -y
apt-get install -y \
  qemu-kvm libvirt-daemon-system libvirt-clients bridge-utils \
  virtinst virt-manager \
  python3 python3-pip python3-venv python3-dev \
  mongodb-org postgresql postgresql-client \
  tcpdump libcap2-bin apparmor-utils \
  inetsim \
  git curl wget jq unzip \
  libffi-dev libssl-dev libjpeg-dev zlib1g-dev \
  awscli

# If mongodb-org is not available from default repos, install from MongoDB repo
if ! command -v mongod &>/dev/null; then
  echo "=== Installing MongoDB from official repo ==="
  curl -fsSL https://www.mongodb.org/static/pgp/server-7.0.asc | \
    gpg --dearmor -o /usr/share/keyrings/mongodb-server-7.0.gpg
  echo "deb [ signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg ] https://repo.mongodb.org/apt/ubuntu jammy/mongodb-org/7.0 multiverse" \
    > /etc/apt/sources.list.d/mongodb-org-7.0.list
  apt-get update -y
  apt-get install -y mongodb-org
fi

# ---------------------------------------------------------------------------
# Verify KVM — critical for CAPE
# ---------------------------------------------------------------------------
if ! kvm-ok 2>/dev/null; then
  # kvm-ok might not be installed; check manually
  if ! grep -cE '(vmx|svm)' /proc/cpuinfo > /dev/null; then
    echo "ERROR: KVM not available — nested virtualization not enabled on this instance"
    echo "Check that the instance type (c8i) supports nested virt and it is enabled."
    exit 1
  fi
  echo "WARNING: kvm-ok not found but CPU flags indicate VMX support"
fi

# Ensure /dev/kvm exists
if [ ! -e /dev/kvm ]; then
  modprobe kvm
  modprobe kvm_intel || modprobe kvm_amd || true
fi

echo "KVM check passed: $(grep -c vmx /proc/cpuinfo 2>/dev/null || echo 0) vCPUs with VMX"

# ---------------------------------------------------------------------------
# Start services
# ---------------------------------------------------------------------------
systemctl enable --now libvirtd
systemctl enable --now mongod || systemctl enable --now mongodb || true
systemctl enable --now postgresql

# ---------------------------------------------------------------------------
# Create cape user
# ---------------------------------------------------------------------------
if ! id cape &>/dev/null; then
  useradd -m -s /bin/bash cape
fi
usermod -aG kvm,libvirt cape

# ---------------------------------------------------------------------------
# Set up PostgreSQL database for CAPE
# ---------------------------------------------------------------------------
sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='cape'" | grep -q 1 || \
  sudo -u postgres psql -c "CREATE USER cape WITH PASSWORD 'cape_sandbox_dev';"

sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='cape'" | grep -q 1 || \
  sudo -u postgres psql -c "CREATE DATABASE cape OWNER cape;"

# ---------------------------------------------------------------------------
# Clone and install CAPEv2
# ---------------------------------------------------------------------------
if [ ! -d /home/cape/CAPEv2 ]; then
  su - cape -c "git clone https://github.com/kevoreilly/CAPEv2.git"
fi

su - cape -c "cd CAPEv2 && python3 -m venv venv"
su - cape -c "cd CAPEv2 && source venv/bin/activate && pip install --upgrade pip setuptools wheel"
su - cape -c "cd CAPEv2 && source venv/bin/activate && pip install poetry"
su - cape -c "cd CAPEv2 && source venv/bin/activate && poetry install --no-interaction" || \
  su - cape -c "cd CAPEv2 && source venv/bin/activate && pip install -r requirements.txt" || true

# ---------------------------------------------------------------------------
# Configure CAPE — basic settings
# ---------------------------------------------------------------------------
CAPE_CONF="/home/cape/CAPEv2/conf"

# Set database connection
if [ -f "$CAPE_CONF/reporting.conf" ]; then
  sed -i 's/^connection =.*/connection = postgresql:\/\/cape:cape_sandbox_dev@localhost:5432\/cape/' \
    "$CAPE_CONF/reporting.conf"
fi

# Set result server to the sandbox gateway IP
if [ -f "$CAPE_CONF/cuckoo.conf" ]; then
  sed -i 's/^ip = 0.0.0.0/ip = 192.168.100.1/' "$CAPE_CONF/cuckoo.conf"
fi

# Ensure data directory permissions
chown -R cape:cape /opt/cape-data
mkdir -p /opt/cape-data/storage /opt/cape-data/isos
ln -sf /opt/cape-data/storage /home/cape/CAPEv2/storage 2>/dev/null || true

# ---------------------------------------------------------------------------
# Allow tcpdump for cape user (needed for network capture)
# ---------------------------------------------------------------------------
setcap cap_net_raw,cap_net_admin=eip "$(which tcpdump)"

# ---------------------------------------------------------------------------
# Tag instance
# ---------------------------------------------------------------------------
# IMDSv2 token required (instance enforces http_tokens=required)
IMDS_TOKEN=$(curl -sX PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")
INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" http://169.254.169.254/latest/meta-data/instance-id)
REGION=$(curl -s -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" http://169.254.169.254/latest/meta-data/placement/region)
aws ec2 create-tags --resources "$INSTANCE_ID" \
  --tags "Key=cape-setup,Value=phase3-complete" --region "$REGION" || true

echo "=== Phase 3 complete: CAPE dependencies installed ==="
echo "Verify with:"
echo "  systemctl is-active mongod postgresql libvirtd"
echo "  test -d /home/cape/CAPEv2 && echo CAPE_INSTALLED"
