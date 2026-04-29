#!/bin/bash
# =============================================================================
# build-pipeline.sh — Ubuntu 22.04 CAPE qcow2 Build Orchestrator
# =============================================================================
# Runs on the Linux build host (via SSM send-command).
# Downloads inputs from S3, fetches CAPE agent, runs Packer, uploads output.
#
# Prerequisites:
#   - Build host user-data has installed qemu, packer, libvirt, etc.
#   - /var/run/builder-ready marker file exists
#   - AWS credentials available via instance profile
#   - /dev/kvm exists (nested virtualization enabled)
#
# Usage:
#   sudo bash /opt/linux-build/build-pipeline.sh
# =============================================================================
set -euxo pipefail

export HOME=/root
export AWS_REGION="${AWS_REGION:-us-east-1}"
export ENVIRONMENT="${ENVIRONMENT:-dev}"
export ASSETS_BUCKET="${ASSETS_BUCKET:-adp-dev-cape-assets}"

WORKDIR=/opt/linux-build
mkdir -p "$WORKDIR"/{packer,cloud-init,scripts,payload}
# Packer requires output_dir to NOT exist — it creates it during build.
# Remove it here so reruns work cleanly.
rm -rf "$WORKDIR/output"

echo "========================================"
echo "Ubuntu 22.04 CAPE qcow2 Build Pipeline"
echo "========================================"
echo "Started: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "Region:  ${AWS_REGION}"
echo "Bucket:  ${ASSETS_BUCKET}"
echo ""

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------
if [[ ! -f /var/run/builder-ready ]]; then
  echo "ERROR: Builder not ready. Waiting for user-data to complete..."
  echo "Check /var/log/builder-userdata.log for status."
  exit 1
fi

if [[ ! -e /dev/kvm ]]; then
  echo "ERROR: /dev/kvm not found. Nested virtualization not available."
  exit 1
fi

echo "Builder ready. KVM available."
echo ""

# ---------------------------------------------------------------------------
# Stage build inputs from S3
# ---------------------------------------------------------------------------
echo "========== Stage 1/4: Download build inputs =========="

aws s3 sync "s3://${ASSETS_BUCKET}/linux-build-inputs/packer/"     "$WORKDIR/packer/"
aws s3 sync "s3://${ASSETS_BUCKET}/linux-build-inputs/cloud-init/" "$WORKDIR/cloud-init/"
aws s3 sync "s3://${ASSETS_BUCKET}/linux-build-inputs/scripts/"    "$WORKDIR/scripts/"

echo ""

# ---------------------------------------------------------------------------
# Fetch CAPE agent from upstream
# ---------------------------------------------------------------------------
echo "========== Stage 2/4: Fetch CAPE agent =========="

curl -sL https://raw.githubusercontent.com/kevoreilly/CAPEv2/master/agent/agent.py \
  -o "$WORKDIR/payload/agent.py"

# Verify it's valid Python
python3 -c "import ast; ast.parse(open('$WORKDIR/payload/agent.py').read())"
echo "CAPE agent fetched and validated."
echo ""

# ---------------------------------------------------------------------------
# Run Packer build
# ---------------------------------------------------------------------------
echo "========== Stage 3/4: Packer build =========="

cd "$WORKDIR/packer"

# Initialize Packer plugins
packer init ubuntu-cape.pkr.hcl

# Build the image
PACKER_LOG=1 packer build \
  -on-error=cleanup \
  -var "output_dir=$WORKDIR/output" \
  ubuntu-cape.pkr.hcl 2>&1 | tee "$WORKDIR/packer.log"

echo ""

# ---------------------------------------------------------------------------
# Upload to S3
# ---------------------------------------------------------------------------
echo "========== Stage 4/4: Upload to S3 =========="

OUT=$(find "$WORKDIR/output" -name 'ubuntu-cape-v1.qcow2' | head -1)
if [[ -z "$OUT" ]]; then
  echo "ERROR: qcow2 output not found!"
  ls -la "$WORKDIR/output/"
  exit 1
fi

# Generate checksum
sha256sum "$OUT" > "${OUT}.sha256"
SHA256=$(awk '{print $1}' "${OUT}.sha256")
SIZE=$(stat -c %s "$OUT")

echo "Image:    $OUT"
echo "Size:     $SIZE bytes ($(numfmt --to=iec $SIZE))"
echo "SHA256:   $SHA256"
echo ""

# Upload
aws s3 cp "$OUT"          "s3://${ASSETS_BUCKET}/ubuntu-cape-v1.qcow2"
aws s3 cp "${OUT}.sha256" "s3://${ASSETS_BUCKET}/ubuntu-cape-v1.qcow2.sha256"

echo ""
echo "========================================"
echo "UBUNTU BUILD COMPLETE"
echo "========================================"
echo "Finished: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "Image:    s3://${ASSETS_BUCKET}/ubuntu-cape-v1.qcow2"
echo "SHA256:   ${SHA256:0:16}..."
echo "Size:     $(numfmt --to=iec $SIZE)"
echo ""
echo "This build host can now be terminated."
