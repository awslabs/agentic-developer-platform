#!/usr/bin/env bash
# =============================================================================
# run-build.sh — Orchestrator: runs the full Windows qcow2 build pipeline
# =============================================================================
# Run this on the build host (via SSM Session Manager) after Terraform creates
# the instance with build_host_enabled=true.
#
# Usage:
#   sudo /path/to/run-build.sh
#
# Prerequisites:
#   - Build host user-data has already installed qemu, libvirt, etc.
#   - /var/run/builder-ready marker file exists
#   - AWS credentials available via instance profile
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="/var/log/image-builder"
mkdir -p "${LOG_DIR}"

export AWS_REGION="${AWS_REGION:-us-east-1}"
export ENVIRONMENT="${ENVIRONMENT:-dev}"
export ASSETS_BUCKET="${ASSETS_BUCKET:-adp-dev-cape-assets}"

echo "========================================"
echo "Windows 10 CAPE qcow2 Build Pipeline"
echo "========================================"
echo "Started: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "Region:  ${AWS_REGION}"
echo "Bucket:  ${ASSETS_BUCKET}"
echo ""

# Check builder is ready
if [[ ! -f /var/run/builder-ready ]]; then
  echo "ERROR: Builder not ready. Waiting for user-data to complete..."
  echo "Check /var/log/builder-userdata.log for status."
  exit 1
fi

# Check KVM
if [[ ! -e /dev/kvm ]]; then
  echo "ERROR: /dev/kvm not found. This instance does not support nested virtualization."
  exit 1
fi

echo "Builder ready. KVM available. Starting build pipeline."
echo ""

# Phase 1: Fetch Windows ISO
echo "========== Step 1/4: Fetch Windows ISO =========="
bash "${SCRIPT_DIR}/01-fetch-iso.sh" 2>&1 | tee "${LOG_DIR}/01-fetch-iso.log"
echo ""

# Phase 2: Build unattend ISO
echo "========== Step 2/4: Build unattend ISO =========="
bash "${SCRIPT_DIR}/02-build-iso.sh" 2>&1 | tee "${LOG_DIR}/02-build-iso.log"
echo ""

# Phase 3: Run virt-install (Windows unattended install, ~25-40 min)
echo "========== Step 3/4: Windows unattended install =========="
echo "This will take approximately 25-40 minutes..."
bash "${SCRIPT_DIR}/03-virt-install.sh" 2>&1 | tee "${LOG_DIR}/03-virt-install.log"
echo ""

# Phase 4: Finalize + upload to S3
echo "========== Step 4/4: Finalize + upload =========="
bash "${SCRIPT_DIR}/04-finalize.sh" 2>&1 | tee "${LOG_DIR}/04-finalize.log"
echo ""

echo "========================================"
echo "Build pipeline complete!"
echo "Finished: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "========================================"
echo ""
echo "Image:    s3://${ASSETS_BUCKET}/win10-cape-v1.qcow2"
echo "Checksum: s3://${ASSETS_BUCKET}/win10-cape-v1.qcow2.sha256"
echo ""
echo "This build host can now be terminated:"
echo "  terraform apply -var build_host_enabled=false"
echo "  # or: TOKEN=\$(curl -s -X PUT 'http://169.254.169.254/latest/api/token' -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')"
echo "  #     aws ec2 terminate-instances --instance-ids \$(curl -s -H \"X-aws-ec2-metadata-token: \$TOKEN\" http://169.254.169.254/latest/meta-data/instance-id)"
