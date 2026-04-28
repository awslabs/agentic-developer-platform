#!/usr/bin/env bash
# =============================================================================
# 02-build-iso.sh — Create secondary ISO with autounattend.xml + firstboot/
# =============================================================================
# Builds an ISO containing:
#   - autounattend.xml (with password injected from SSM)
#   - firstboot/install.ps1
#   - firstboot/cape-agent/ (CAPE guest agent from GitHub release)
#   - firstboot/python-3.11.9-amd64.exe
#   - drivers/ (VirtIO drivers for Windows)
#
# The ISO is mounted as a second CD-ROM during virt-install.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="/tmp/unattend-iso"
ISO_PATH="/var/lib/libvirt/images/unattend.iso"
REGION="${AWS_REGION:-us-east-1}"
ENVIRONMENT="${ENVIRONMENT:-dev}"

# VirtIO drivers ISO URL (stable release)
VIRTIO_ISO_URL="${VIRTIO_ISO_URL:-https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/virtio-win.iso}"

# Python 3.11 installer URL
PYTHON_URL="https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe"

# CAPE agent source — clone from GitHub
CAPE_AGENT_REPO="https://github.com/kevoreilly/CAPEv2.git"
CAPE_AGENT_BRANCH="master"

echo "=== Phase 2: Build unattend ISO ==="

# Clean previous build
rm -rf "${WORK_DIR}"
mkdir -p "${WORK_DIR}/firstboot/cape-agent"
mkdir -p "${WORK_DIR}/drivers"

# -----------------------------------------------------------------
# 1. Inject password into autounattend.xml
# -----------------------------------------------------------------
echo "Fetching Windows password from SSM..."
WIN_PASSWORD=$(aws ssm get-parameter \
  --name "/adp/${ENVIRONMENT}/cape/builder-windows-password" \
  --with-decryption \
  --query "Parameter.Value" \
  --output text \
  --region "${REGION}" 2>/dev/null || true)

if [[ -z "${WIN_PASSWORD}" ]]; then
  echo "SSM parameter not found. Generating random password and storing it..."
  WIN_PASSWORD=$(openssl rand -base64 16 | tr -dc 'A-Za-z0-9!@#' | head -c 16)
  aws ssm put-parameter \
    --name "/adp/${ENVIRONMENT}/cape/builder-windows-password" \
    --value "${WIN_PASSWORD}" \
    --type SecureString \
    --overwrite \
    --region "${REGION}"
  echo "Password stored in SSM."
fi

echo "Injecting password into autounattend.xml..."
sed "s/PASSWORD_PLACEHOLDER/${WIN_PASSWORD}/g" \
  "${SCRIPT_DIR}/autounattend.xml" > "${WORK_DIR}/autounattend.xml"

# -----------------------------------------------------------------
# 2. Copy firstboot scripts
# -----------------------------------------------------------------
echo "Copying firstboot scripts..."
cp "${SCRIPT_DIR}/install.ps1" "${WORK_DIR}/firstboot/"

# -----------------------------------------------------------------
# 3. Download Python installer
# -----------------------------------------------------------------
echo "Downloading Python 3.11 installer..."
curl -fSL --retry 3 -o "${WORK_DIR}/firstboot/python-3.11.9-amd64.exe" "${PYTHON_URL}"
echo "Python installer downloaded."

# -----------------------------------------------------------------
# 4. Clone CAPE guest agent (sparse checkout — agent/ directory only)
# -----------------------------------------------------------------
echo "Cloning CAPE guest agent..."
CAPE_TMP="/tmp/cape-clone"
rm -rf "${CAPE_TMP}"
git clone --depth 1 --filter=blob:none --sparse \
  "${CAPE_AGENT_REPO}" "${CAPE_TMP}"
cd "${CAPE_TMP}"
git sparse-checkout set agent
cp -r agent/* "${WORK_DIR}/firstboot/cape-agent/"
cd /
rm -rf "${CAPE_TMP}"
echo "CAPE agent files copied."

# -----------------------------------------------------------------
# 5. Download VirtIO drivers
# -----------------------------------------------------------------
echo "Downloading VirtIO drivers ISO..."
VIRTIO_ISO="/tmp/virtio-win.iso"
if [[ ! -f "${VIRTIO_ISO}" ]]; then
  curl -fSL --retry 3 -o "${VIRTIO_ISO}" "${VIRTIO_ISO_URL}"
fi

echo "Extracting VirtIO drivers for Windows 10..."
VIRTIO_MNT="/mnt/virtio"
mkdir -p "${VIRTIO_MNT}"
mount -o loop,ro "${VIRTIO_ISO}" "${VIRTIO_MNT}"

# Copy relevant driver directories
for driver in viostor NetKVM vioscsi qxldod Balloon; do
  if [[ -d "${VIRTIO_MNT}/${driver}" ]]; then
    mkdir -p "${WORK_DIR}/drivers/${driver}"
    cp -r "${VIRTIO_MNT}/${driver}/w10" "${WORK_DIR}/drivers/${driver}/" 2>/dev/null || true
    # Fallback: some ISOs use 2k22 or amd64 directly
    cp -r "${VIRTIO_MNT}/${driver}/2k19" "${WORK_DIR}/drivers/${driver}/" 2>/dev/null || true
  fi
done

umount "${VIRTIO_MNT}"
echo "VirtIO drivers extracted."

# -----------------------------------------------------------------
# 6. Build the ISO
# -----------------------------------------------------------------
echo "Building unattend ISO..."
genisoimage \
  -o "${ISO_PATH}" \
  -V "UNATTEND" \
  -J -r \
  -input-charset utf-8 \
  "${WORK_DIR}"

echo "Unattend ISO created at ${ISO_PATH}"
echo "Size: $(du -h "${ISO_PATH}" | awk '{print $1}')"

# Clean up work directory (keep the ISO)
rm -rf "${WORK_DIR}"

echo "=== Unattend ISO ready ==="
