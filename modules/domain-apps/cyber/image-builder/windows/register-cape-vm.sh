#!/bin/bash
# =============================================================================
# register-cape-vm.sh — Register new Windows qcow2 on the CAPE host
# =============================================================================
# Runs on the CAPE host (via SSM send-command) after a successful Windows build.
#
# Steps:
#   1. Download qcow2 from S3
#   2. Define libvirt domain
#   3. Start VM, wait for CAPE agent handshake
#   4. Shutdown + create clean snapshot
#   5. Update conf/kvm.conf
#   6. Restart CAPE services
#
# Usage:
#   sudo bash /opt/cape-registration/register-cape-vm.sh <build-date>
# =============================================================================
set -euo pipefail

BUILD_DATE="${1:?Usage: $0 <YYYY-MM-DD>}"
export AWS_REGION="${AWS_REGION:-us-east-1}"
export ASSETS_BUCKET="${ASSETS_BUCKET:-adp-dev-cape-assets}"

VM_NAME="win11-cape-${BUILD_DATE}"
IMAGE_DIR="/opt/cape-data/images"
KVM_CONF="/opt/CAPEv2/conf/kvm.conf"
DOMAIN_XML_DIR="/opt/cape-data/domain-xml"
CAPE_AGENT_PORT=8000

echo "========================================"
echo "CAPE VM Registration: ${VM_NAME}"
echo "========================================"
echo "Started: $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# ---------------------------------------------------------------------------
# 1. Download qcow2
# ---------------------------------------------------------------------------
echo "=== Step 1/6: Download qcow2 ==="
mkdir -p "$IMAGE_DIR" "$DOMAIN_XML_DIR"

aws s3 cp "s3://${ASSETS_BUCKET}/win11-cape-${BUILD_DATE}.qcow2" \
  "${IMAGE_DIR}/${VM_NAME}.qcow2"

echo "Downloaded: ${IMAGE_DIR}/${VM_NAME}.qcow2 ($(du -h "${IMAGE_DIR}/${VM_NAME}.qcow2" | cut -f1))"

# ---------------------------------------------------------------------------
# 2. Define libvirt domain
# ---------------------------------------------------------------------------
echo "=== Step 2/6: Define libvirt domain ==="

cat > "${DOMAIN_XML_DIR}/${VM_NAME}.xml" << DOMXML
<domain type='kvm'>
  <name>${VM_NAME}</name>
  <memory unit='MiB'>4096</memory>
  <vcpu placement='static'>2</vcpu>
  <os>
    <type arch='x86_64' machine='pc'>hvm</type>
    <boot dev='hd'/>
  </os>
  <features>
    <acpi/>
    <apic/>
    <hyperv>
      <relaxed state='on'/>
      <vapic state='on'/>
      <spinlocks state='on' retries='8191'/>
    </hyperv>
  </features>
  <cpu mode='host-passthrough'/>
  <clock offset='localtime'>
    <timer name='hypervclock' present='yes'/>
  </clock>
  <devices>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='${IMAGE_DIR}/${VM_NAME}.qcow2'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <interface type='network'>
      <source network='default'/>
      <model type='virtio'/>
    </interface>
    <graphics type='vnc' port='-1' autoport='yes'/>
    <serial type='pty'>
      <target port='0'/>
    </serial>
    <console type='pty'>
      <target type='serial' port='0'/>
    </console>
  </devices>
</domain>
DOMXML

virsh define "${DOMAIN_XML_DIR}/${VM_NAME}.xml"
echo "Domain defined: ${VM_NAME}"

# ---------------------------------------------------------------------------
# 3. Start VM and wait for agent handshake
# ---------------------------------------------------------------------------
echo "=== Step 3/6: Start VM, wait for agent ==="
virsh start "${VM_NAME}"

# Wait for CAPE agent to come up (polls port 8000)
VM_IP=""
WAITED=0
MAX_WAIT=180

while [[ $WAITED -lt $MAX_WAIT ]]; do
  VM_IP=$(virsh domifaddr "${VM_NAME}" 2>/dev/null | grep -oP '(\d+\.){3}\d+' | head -1 || true)
  if [[ -n "$VM_IP" ]]; then
    if curl -sf --connect-timeout 2 "http://${VM_IP}:${CAPE_AGENT_PORT}/status" &>/dev/null; then
      echo "CAPE agent responding at ${VM_IP}:${CAPE_AGENT_PORT}"
      break
    fi
  fi
  sleep 5
  WAITED=$((WAITED + 5))
  echo "  Waiting for agent... (${WAITED}s / ${MAX_WAIT}s)"
done

if [[ $WAITED -ge $MAX_WAIT ]]; then
  echo "WARNING: Agent did not respond within ${MAX_WAIT}s. Proceeding anyway."
fi

# ---------------------------------------------------------------------------
# 4. Shutdown + snapshot
# ---------------------------------------------------------------------------
echo "=== Step 4/6: Shutdown and create snapshot ==="
virsh shutdown "${VM_NAME}"

# Wait for shutdown
for i in $(seq 1 30); do
  STATE=$(virsh domstate "${VM_NAME}" 2>/dev/null || echo "unknown")
  [[ "$STATE" == "shut off" ]] && break
  sleep 5
done

# Force off if still running
STATE=$(virsh domstate "${VM_NAME}" 2>/dev/null || echo "unknown")
if [[ "$STATE" != "shut off" ]]; then
  virsh destroy "${VM_NAME}" 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
# 5. Create clean snapshot
# ---------------------------------------------------------------------------
echo "=== Step 5/6: Create clean snapshot ==="
virsh snapshot-create-as --domain "${VM_NAME}" --name "clean" \
  --description "Clean state for CAPE analysis - ${BUILD_DATE}"
echo "Snapshot 'clean' created."

# ---------------------------------------------------------------------------
# 6. Update kvm.conf and restart services
# ---------------------------------------------------------------------------
echo "=== Step 6/6: Update kvm.conf ==="

# Add machine to machines list if not already there
if ! grep -q "${VM_NAME}" "$KVM_CONF" 2>/dev/null; then
  # Append to machines= line
  sed -i "s/^machines = .*/&, ${VM_NAME}/" "$KVM_CONF"

  # Append machine config block
  cat >> "$KVM_CONF" << CONF

[${VM_NAME}]
label = Windows 11 CAPE (${BUILD_DATE})
platform = windows
ip = ${VM_IP:-192.168.122.0}
snapshot = clean
interface = virbr0
resultserver_ip = 192.168.122.1
resultserver_port = 2042
tags = win11,x64,windows
CONF

  echo "Added ${VM_NAME} to kvm.conf"
else
  echo "${VM_NAME} already in kvm.conf — skipping"
fi

# Restart CAPE services
systemctl restart cape cape-web cape-processor 2>/dev/null || true

echo ""
echo "========================================"
echo "REGISTRATION COMPLETE"
echo "========================================"
echo "VM:       ${VM_NAME}"
echo "IP:       ${VM_IP:-unknown}"
echo "Snapshot: clean"
echo "Config:   ${KVM_CONF}"
echo ""
