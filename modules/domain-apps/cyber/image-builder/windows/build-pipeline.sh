#!/bin/bash
# =============================================================================
# build-pipeline.sh — Windows 11 CAPE qcow2 Build Orchestrator
# =============================================================================
# Runs on the build host (self-contained via user-data).
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

aws s3 sync "s3://${ASSETS_BUCKET}/windows-build-inputs/packer/"       "$WORKDIR/packer/"       --region "$AWS_REGION"
aws s3 sync "s3://${ASSETS_BUCKET}/windows-build-inputs/answer_files/" "$WORKDIR/answer_files/" --region "$AWS_REGION"
aws s3 sync "s3://${ASSETS_BUCKET}/windows-build-inputs/scripts/"      "$WORKDIR/scripts/"      --region "$AWS_REGION"

echo ""

# ---------------------------------------------------------------------------
# Download VirtIO drivers ISO
# ---------------------------------------------------------------------------
echo "========== Stage 2/5: Download VirtIO drivers =========="

VIRTIO_ISO="$WORKDIR/virtio-win.iso"
if [[ ! -f "$VIRTIO_ISO" ]]; then
  # Try S3 cache first
  if aws s3 cp "s3://${ASSETS_BUCKET}/isos/virtio-win.iso" "$VIRTIO_ISO" --region "$AWS_REGION" 2>/dev/null; then
    echo "VirtIO ISO loaded from S3 cache."
  else
    echo "Downloading VirtIO drivers ISO from Fedora..."
    curl -fsSL "https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/virtio-win.iso" \
      -o "$VIRTIO_ISO"
    # Cache to S3 for future builds
    aws s3 cp "$VIRTIO_ISO" "s3://${ASSETS_BUCKET}/isos/virtio-win.iso" --region "$AWS_REGION" || true
  fi
fi
echo "VirtIO ISO: $(ls -lh "$VIRTIO_ISO" | awk '{print $5}')"
echo ""

# ---------------------------------------------------------------------------
# Download Windows 11 ISO (try S3 cache first, then Microsoft)
# ---------------------------------------------------------------------------
echo "========== Stage 3/5: Download Windows 11 ISO =========="

WIN_ISO="$WORKDIR/win11-enterprise.iso"
if [[ ! -f "$WIN_ISO" ]]; then
  # Try S3 cache first (much faster — same region, ~30s vs ~5 min)
  if aws s3 cp "s3://${ASSETS_BUCKET}/isos/win11-enterprise.iso" "$WIN_ISO" --region "$AWS_REGION" 2>/dev/null; then
    echo "Windows ISO loaded from S3 cache."
  else
    echo "Downloading Windows 11 Enterprise Evaluation ISO (~5 GB)..."
    # Try multiple URLs — Microsoft periodically rotates evaluation ISOs.
    # Order: newest first (24H2), then 23H2, then fwlink redirect.
    WIN_ISO_URLS=(
      "https://software-static.download.prss.microsoft.com/dbazure/888969d5-f34g-4e03-ac9d-1f9786c66749/26100.1742.240906-0331.ge_release_svc_refresh_CLIENTENTERPRISEEVAL_OEMRET_x64FRE_en-us.iso"
      "https://software-static.download.prss.microsoft.com/dbazure/888969d5-f34g-4e03-ac9d-1f9786c66749/22631.2861.231204-0540.23h2_release_svc_refresh_CLIENTENTERPRISEEVAL_OEMRET_x64FRE_en-us.iso"
      "https://software-download.microsoft.com/download/sg/22631.2861.231204-0540.23h2_release_svc_refresh_CLIENTENTERPRISEEVAL_OEMRET_x64FRE_en-us.iso"
    )

    DOWNLOADED=false
    for url in "${WIN_ISO_URLS[@]}"; do
      echo "Trying: ${url:0:80}..."
      if curl -fSL --retry 3 --retry-delay 10 "$url" -o "$WIN_ISO" 2>&1; then
        # Verify it's a real ISO (> 4 GB)
        ISO_SIZE=$(stat -c %s "$WIN_ISO" 2>/dev/null || echo "0")
        if (( ISO_SIZE > 4000000000 )); then
          echo "Download successful. Size: $(numfmt --to=iec "$ISO_SIZE")"
          DOWNLOADED=true
          break
        else
          echo "Downloaded file too small ($ISO_SIZE bytes), trying next URL..."
          rm -f "$WIN_ISO"
        fi
      else
        echo "Download failed, trying next URL..."
        rm -f "$WIN_ISO"
      fi
    done

    if [[ "$DOWNLOADED" != "true" ]]; then
      echo "ERROR: Could not download Windows 11 ISO from any source."
      echo "Consider manually uploading to s3://${ASSETS_BUCKET}/isos/win11-enterprise.iso"
      exit 1
    fi

    # Cache to S3 for future builds (avoid re-downloading next time)
    echo "Caching ISO to S3 for future builds..."
    aws s3 cp "$WIN_ISO" "s3://${ASSETS_BUCKET}/isos/win11-enterprise.iso" \
      --region "$AWS_REGION" --storage-class INTELLIGENT_TIERING || true
  fi
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
  --region "$AWS_REGION" --metadata "sha256=${SHA256},build_date=${BUILD_DATE}"
aws s3 cp "${OUT}.sha256" "s3://${ASSETS_BUCKET}/win11-cape-${BUILD_DATE}.qcow2.sha256" \
  --region "$AWS_REGION"

# Update latest pointer
aws s3 cp "$OUT" "s3://${ASSETS_BUCKET}/win11-cape-latest.qcow2" \
  --region "$AWS_REGION" --metadata "sha256=${SHA256},build_date=${BUILD_DATE}"
aws s3 cp "${OUT}.sha256" "s3://${ASSETS_BUCKET}/win11-cape-latest.qcow2.sha256" \
  --region "$AWS_REGION"

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
