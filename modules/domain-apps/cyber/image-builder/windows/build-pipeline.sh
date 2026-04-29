#!/bin/bash
# =============================================================================
# build-pipeline.sh — Windows 11 CAPE qcow2 Build Orchestrator
# =============================================================================
# Runs on the build host (via SSM send-command).
# Downloads Packer inputs from S3, fetches ISOs, runs Packer, uploads output.
#
# Prerequisites:
#   - Build host user-data has installed qemu, packer, libvirt, etc.
#   - /var/run/builder-ready marker file exists
#   - AWS credentials available via instance profile
#   - /dev/kvm exists (nested virtualization enabled)
#
# Usage:
#   sudo bash /opt/windows-build/build-pipeline.sh
# =============================================================================
set -euxo pipefail

export HOME=/root
export AWS_REGION="${AWS_REGION:-us-east-1}"
export ENVIRONMENT="${ENVIRONMENT:-dev}"
export ASSETS_BUCKET="${ASSETS_BUCKET:-adp-dev-cape-assets}"

WORKDIR=/opt/windows-build
BUILD_DATE=$(date -u +%Y-%m-%d)
mkdir -p "$WORKDIR"/{packer,answer_files,scripts}
# Packer requires output_dir to NOT exist — it creates it during build.
rm -rf "$WORKDIR/output"

echo "========================================"
echo "Windows 11 CAPE qcow2 Build Pipeline"
echo "========================================"
echo "Started: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "Region:  ${AWS_REGION}"
echo "Bucket:  ${ASSETS_BUCKET}"
echo "Date:    ${BUILD_DATE}"
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
# Install Packer if not present
# ---------------------------------------------------------------------------
if ! command -v packer &>/dev/null; then
  echo "Installing Packer..."
  curl -fsSL https://releases.hashicorp.com/packer/1.11.2/packer_1.11.2_linux_amd64.zip \
    -o /tmp/packer.zip
  unzip -o /tmp/packer.zip -d /usr/local/bin/
  rm -f /tmp/packer.zip
  packer --version
fi

# ---------------------------------------------------------------------------
# Stage build inputs from S3
# ---------------------------------------------------------------------------
echo "========== Stage 1/5: Download build inputs =========="

aws s3 sync "s3://${ASSETS_BUCKET}/windows-build-inputs/packer/"       "$WORKDIR/packer/"
aws s3 sync "s3://${ASSETS_BUCKET}/windows-build-inputs/answer_files/" "$WORKDIR/answer_files/"
aws s3 sync "s3://${ASSETS_BUCKET}/windows-build-inputs/scripts/"      "$WORKDIR/scripts/"

echo ""

# ---------------------------------------------------------------------------
# Download VirtIO drivers ISO
# ---------------------------------------------------------------------------
echo "========== Stage 2/5: Download VirtIO drivers =========="

VIRTIO_ISO="$WORKDIR/virtio-win.iso"
if [[ ! -f "$VIRTIO_ISO" ]]; then
  echo "Downloading VirtIO drivers ISO..."
  curl -fsSL "https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/virtio-win.iso" \
    -o "$VIRTIO_ISO"
fi
echo "VirtIO ISO: $(ls -lh "$VIRTIO_ISO" | awk '{print $5}')"
echo ""

# ---------------------------------------------------------------------------
# Download Windows 11 ISO (if not cached)
# ---------------------------------------------------------------------------
echo "========== Stage 3/5: Download Windows 11 ISO =========="

WIN_ISO="$WORKDIR/win11-enterprise.iso"
if [[ ! -f "$WIN_ISO" ]]; then
  echo "Downloading Windows 11 Enterprise Evaluation ISO (~5 GB)..."
  # 23H2 Enterprise Evaluation
  curl -fsSL \
    "https://software-download.microsoft.com/download/sg/22631.2861.231204-0540.23h2_release_svc_refresh_CLIENTENTERPRISEEVAL_OEMRET_x64FRE_en-us.iso" \
    -o "$WIN_ISO"
fi
echo "Windows ISO: $(ls -lh "$WIN_ISO" | awk '{print $5}')"
echo ""

# ---------------------------------------------------------------------------
# Run Packer build
# ---------------------------------------------------------------------------
echo "========== Stage 4/5: Packer build =========="

cd "$WORKDIR/packer"

# Initialize Packer plugins
packer init windows-11.pkr.hcl

# Override ISO paths to use local files + VirtIO
PACKER_LOG=1 packer build \
  -on-error=cleanup \
  -var "output_dir=$WORKDIR/output" \
  -var "iso_url=$WIN_ISO" \
  -var "iso_checksum=none" \
  -var "virtio_iso_url=$VIRTIO_ISO" \
  windows-11.pkr.hcl 2>&1 | tee "$WORKDIR/packer.log"

echo ""

# ---------------------------------------------------------------------------
# Upload to S3
# ---------------------------------------------------------------------------
echo "========== Stage 5/5: Upload to S3 =========="

OUT=$(find "$WORKDIR/output" -name 'win11-cape.qcow2' | head -1)
if [[ -z "$OUT" ]]; then
  echo "ERROR: qcow2 output not found!"
  ls -la "$WORKDIR/output/" 2>/dev/null || echo "Output dir doesn't exist"
  exit 1
fi

# Generate checksum
sha256sum "$OUT" > "${OUT}.sha256"
SHA256=$(awk '{print $1}' "${OUT}.sha256")
SIZE=$(stat -c %s "$OUT")

echo "Image:    $OUT"
echo "Size:     $SIZE bytes ($(numfmt --to=iec "$SIZE"))"
echo "SHA256:   $SHA256"
echo ""

# Upload dated version
aws s3 cp "$OUT" "s3://${ASSETS_BUCKET}/win11-cape-${BUILD_DATE}.qcow2" \
  --metadata "sha256=${SHA256},build_date=${BUILD_DATE}"
aws s3 cp "${OUT}.sha256" "s3://${ASSETS_BUCKET}/win11-cape-${BUILD_DATE}.qcow2.sha256"

# Update latest pointer
aws s3 cp "$OUT" "s3://${ASSETS_BUCKET}/win11-cape-latest.qcow2" \
  --metadata "sha256=${SHA256},build_date=${BUILD_DATE}"
aws s3 cp "${OUT}.sha256" "s3://${ASSETS_BUCKET}/win11-cape-latest.qcow2.sha256"

echo ""
echo "========================================"
echo "WINDOWS BUILD COMPLETE"
echo "========================================"
echo "Finished: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "Image:    s3://${ASSETS_BUCKET}/win11-cape-${BUILD_DATE}.qcow2"
echo "Latest:   s3://${ASSETS_BUCKET}/win11-cape-latest.qcow2"
echo "SHA256:   ${SHA256:0:16}..."
echo "Size:     $(numfmt --to=iec "$SIZE")"
echo ""
echo "This build host can now be terminated."
