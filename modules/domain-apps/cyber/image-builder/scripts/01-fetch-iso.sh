#!/usr/bin/env bash
# =============================================================================
# 01-fetch-iso.sh — Download Windows 10 Enterprise Evaluation ISO
# =============================================================================
# Downloads the Windows 10 Enterprise evaluation ISO from Microsoft's public
# eval center. Verifies SHA256 checksum. Fails loud on mismatch.
#
# The ISO is stored at /var/lib/libvirt/images/Win10_Eval.iso
# =============================================================================
set -euo pipefail

ISO_DIR="/var/lib/libvirt/images"
ISO_PATH="${ISO_DIR}/Win10_Eval.iso"

# Microsoft Windows 10 Enterprise Evaluation download URL.
# This is the publicly available evaluation edition (90-day license).
# URL may change — update if Microsoft rotates the download link.
WIN10_EVAL_URL="${WIN10_EVAL_URL:-https://software-static.download.prss.microsoft.com/dbazure/888969d5-f34g-4e03-ac9d-1f9786c66749/19045.2006.220908-0225.22h2_release_svc_refresh_CLIENTENTERPRISEEVAL_OEMRET_x64FRE_en-us.iso}"

# SHA256 of the known-good ISO. Update when changing the URL.
# Set to empty string to skip verification (not recommended).
EXPECTED_SHA256="${WIN10_EVAL_SHA256:-ef7312733a9f5d7d51571b3de0d2b0e6a5cfbb478570760acb7a07e4945614b2}"

echo "=== Phase 1: Fetch Windows 10 Enterprise Evaluation ISO ==="

mkdir -p "${ISO_DIR}"

if [[ -f "${ISO_PATH}" ]]; then
  echo "ISO already exists at ${ISO_PATH}, verifying checksum..."
  ACTUAL_SHA256=$(sha256sum "${ISO_PATH}" | awk '{print $1}')
  if [[ -n "${EXPECTED_SHA256}" && "${ACTUAL_SHA256}" == "${EXPECTED_SHA256}" ]]; then
    echo "Checksum matches. Skipping download."
    exit 0
  else
    echo "Checksum mismatch or verification skipped. Re-downloading."
    rm -f "${ISO_PATH}"
  fi
fi

echo "Downloading Windows 10 Enterprise Evaluation ISO..."
echo "URL: ${WIN10_EVAL_URL}"
echo "This may take 10-20 minutes depending on bandwidth."

curl -fSL --retry 3 --retry-delay 10 \
  -o "${ISO_PATH}" \
  "${WIN10_EVAL_URL}"

echo "Download complete. Size: $(du -h "${ISO_PATH}" | awk '{print $1}')"

if [[ -n "${EXPECTED_SHA256}" ]]; then
  echo "Verifying SHA256 checksum..."
  ACTUAL_SHA256=$(sha256sum "${ISO_PATH}" | awk '{print $1}')
  if [[ "${ACTUAL_SHA256}" != "${EXPECTED_SHA256}" ]]; then
    echo "ERROR: SHA256 mismatch!"
    echo "  Expected: ${EXPECTED_SHA256}"
    echo "  Actual:   ${ACTUAL_SHA256}"
    echo "The ISO may be corrupted or Microsoft updated the file."
    echo "Update WIN10_EVAL_SHA256 if this is a new valid ISO."
    rm -f "${ISO_PATH}"
    exit 1
  fi
  echo "Checksum verified OK."
else
  echo "WARNING: No expected SHA256 set — skipping verification."
fi

echo "=== ISO ready at ${ISO_PATH} ==="
