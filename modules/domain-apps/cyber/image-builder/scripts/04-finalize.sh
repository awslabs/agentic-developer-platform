#!/usr/bin/env bash
# =============================================================================
# 04-finalize.sh — Compact qcow2, upload to S3, clean up
# =============================================================================
# After the Windows VM shuts down, this script:
#   1. Compacts the qcow2 image (raw → compressed qcow2)
#   2. Verifies size sanity (expect 8-12 GB compressed)
#   3. Computes SHA256 checksum
#   4. Uploads both to S3
#   5. Cleans up the SSM password parameter
# =============================================================================
set -euo pipefail

RAW_QCOW2="/var/lib/libvirt/images/winbuild.qcow2"
FINAL_QCOW2="/tmp/win10-cape-v1.qcow2"
FINAL_SHA256="/tmp/win10-cape-v1.qcow2.sha256"
BUCKET="${ASSETS_BUCKET:-adp-dev-cape-assets}"
REGION="${AWS_REGION:-us-east-1}"
ENVIRONMENT="${ENVIRONMENT:-dev}"

echo "=== Phase 5: Finalize qcow2 + upload to S3 ==="

# -----------------------------------------------------------------
# 1. Verify raw qcow2 exists
# -----------------------------------------------------------------
if [[ ! -f "${RAW_QCOW2}" ]]; then
  echo "ERROR: Raw qcow2 not found at ${RAW_QCOW2}"
  echo "Run 03-virt-install.sh first."
  exit 1
fi

echo "Raw qcow2 info:"
qemu-img info "${RAW_QCOW2}"
echo ""

# -----------------------------------------------------------------
# 2. Compact the image
# -----------------------------------------------------------------
echo "Compacting qcow2 (this may take a few minutes)..."
rm -f "${FINAL_QCOW2}"
qemu-img convert -O qcow2 -c "${RAW_QCOW2}" "${FINAL_QCOW2}"

FINAL_SIZE=$(stat -c%s "${FINAL_QCOW2}")
FINAL_SIZE_GB=$(echo "scale=2; ${FINAL_SIZE} / 1073741824" | bc)
echo "Compressed qcow2 size: ${FINAL_SIZE_GB} GB ($(du -h "${FINAL_QCOW2}" | awk '{print $1}'))"

# Sanity check: expect 8-20 GB
MIN_SIZE=$((8 * 1073741824))
MAX_SIZE=$((20 * 1073741824))
if [[ ${FINAL_SIZE} -lt ${MIN_SIZE} ]]; then
  echo "WARNING: Image is smaller than expected (< 8 GB). Installation may have failed."
fi
if [[ ${FINAL_SIZE} -gt ${MAX_SIZE} ]]; then
  echo "WARNING: Image is larger than expected (> 20 GB). Consider investigating."
fi

# -----------------------------------------------------------------
# 3. Verify qcow2 metadata
# -----------------------------------------------------------------
echo ""
echo "Final qcow2 info:"
qemu-img info "${FINAL_QCOW2}"

# -----------------------------------------------------------------
# 4. Compute SHA256
# -----------------------------------------------------------------
echo ""
echo "Computing SHA256 checksum..."
sha256sum "${FINAL_QCOW2}" > "${FINAL_SHA256}"
cat "${FINAL_SHA256}"

# -----------------------------------------------------------------
# 5. Upload to S3
# -----------------------------------------------------------------
echo ""
echo "Uploading qcow2 to s3://${BUCKET}/win10-cape-v1.qcow2 ..."
aws s3 cp "${FINAL_QCOW2}" "s3://${BUCKET}/win10-cape-v1.qcow2" \
  --region "${REGION}" \
  --expected-size "${FINAL_SIZE}"

echo "Uploading SHA256 to s3://${BUCKET}/win10-cape-v1.qcow2.sha256 ..."
aws s3 cp "${FINAL_SHA256}" "s3://${BUCKET}/win10-cape-v1.qcow2.sha256" \
  --region "${REGION}"

echo "Upload complete."

# -----------------------------------------------------------------
# 6. Verify upload
# -----------------------------------------------------------------
echo ""
echo "Verifying S3 objects..."
aws s3 ls "s3://${BUCKET}/win10-cape-v1.qcow2" --region "${REGION}"
aws s3 ls "s3://${BUCKET}/win10-cape-v1.qcow2.sha256" --region "${REGION}"

# -----------------------------------------------------------------
# 7. Clean up SSM password parameter
# -----------------------------------------------------------------
echo ""
echo "Cleaning up SSM password parameter..."
aws ssm delete-parameter \
  --name "/adp/${ENVIRONMENT}/cape/builder-windows-password" \
  --region "${REGION}" 2>/dev/null || true
echo "SSM parameter deleted."

# -----------------------------------------------------------------
# 8. Clean up local files
# -----------------------------------------------------------------
echo "Cleaning up local build artifacts..."
rm -f "${RAW_QCOW2}"
rm -f "${FINAL_QCOW2}"
rm -f "${FINAL_SHA256}"
rm -f /var/lib/libvirt/images/Win10_Eval.iso
rm -f /var/lib/libvirt/images/unattend.iso

echo ""
echo "=== Finalize complete ==="
echo "Image uploaded to: s3://${BUCKET}/win10-cape-v1.qcow2"
echo "Checksum uploaded to: s3://${BUCKET}/win10-cape-v1.qcow2.sha256"
echo ""
echo "Next: terminate this build host (or set build_host_enabled=false in Terraform)."
