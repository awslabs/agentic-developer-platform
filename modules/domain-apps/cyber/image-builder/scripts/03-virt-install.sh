#!/usr/bin/env bash
# =============================================================================
# 03-virt-install.sh — Launch Windows VM via KVM for unattended install
# =============================================================================
# Launches a KVM virtual machine that performs the Windows 10 unattended
# installation. The VM shuts down automatically when install.ps1 completes.
#
# This script blocks until the VM shuts down (~25-40 min) or times out.
# =============================================================================
set -euo pipefail

ISO_PATH="/var/lib/libvirt/images/Win10_Eval.iso"
UNATTEND_ISO="/var/lib/libvirt/images/unattend.iso"
QCOW2_PATH="/var/lib/libvirt/images/winbuild.qcow2"
VM_NAME="winbuild"
VM_RAM=4096
VM_VCPUS=4
DISK_SIZE=40
WAIT_TIMEOUT=90  # minutes

echo "=== Phase 3: Launch Windows VM via virt-install ==="

# Verify prerequisites
for f in "${ISO_PATH}" "${UNATTEND_ISO}"; do
  if [[ ! -f "$f" ]]; then
    echo "ERROR: Required file missing: $f"
    echo "Run the previous build scripts first."
    exit 1
  fi
done

# Check KVM is available
if [[ ! -e /dev/kvm ]]; then
  echo "ERROR: /dev/kvm not found. Nested virtualization is not available."
  echo "Ensure the build host is c8i.4xlarge (or similar) with KVM support."
  exit 1
fi

# Remove any existing VM with the same name
virsh destroy "${VM_NAME}" 2>/dev/null || true
virsh undefine "${VM_NAME}" --nvram 2>/dev/null || true

# Remove existing disk if present (fresh build)
rm -f "${QCOW2_PATH}"

echo "Starting virt-install..."
echo "  VM: ${VM_NAME}"
echo "  RAM: ${VM_RAM} MB"
echo "  vCPUs: ${VM_VCPUS}"
echo "  Disk: ${DISK_SIZE} GB (qcow2)"
echo "  Timeout: ${WAIT_TIMEOUT} min"
echo ""
echo "The unattended install will take ~25-40 minutes."
echo "The VM will shut down automatically when install.ps1 completes."

virt-install \
  --name "${VM_NAME}" \
  --ram "${VM_RAM}" \
  --vcpus "${VM_VCPUS}" \
  --disk "path=${QCOW2_PATH},size=${DISK_SIZE},format=qcow2,bus=virtio" \
  --cdrom "${ISO_PATH}" \
  --disk "path=${UNATTEND_ISO},device=cdrom" \
  --os-variant win10 \
  --network network=default,model=virtio \
  --graphics none \
  --noautoconsole \
  --boot uefi \
  --wait "${WAIT_TIMEOUT}"

# Check VM state
VM_STATE=$(virsh domstate "${VM_NAME}" 2>/dev/null || echo "unknown")
echo "VM state after virt-install: ${VM_STATE}"

if [[ "${VM_STATE}" == "shut off" ]]; then
  echo "VM shut down cleanly. Windows installation likely completed."
elif [[ "${VM_STATE}" == "running" ]]; then
  echo "WARNING: VM is still running after ${WAIT_TIMEOUT} min timeout."
  echo "The installation may not have completed. Check the VM."
  echo "Forcing shutdown..."
  virsh destroy "${VM_NAME}" 2>/dev/null || true
  echo "VM forcefully shut down. The qcow2 may be incomplete."
else
  echo "VM state: ${VM_STATE}. Proceeding with caution."
fi

# Verify qcow2 was created
if [[ ! -f "${QCOW2_PATH}" ]]; then
  echo "ERROR: qcow2 not found at ${QCOW2_PATH}"
  exit 1
fi

echo "Raw qcow2 size: $(du -h "${QCOW2_PATH}" | awk '{print $1}')"

# Clean up the VM definition (keep the disk)
virsh undefine "${VM_NAME}" --nvram 2>/dev/null || true

echo "=== virt-install complete ==="
