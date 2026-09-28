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
#   4. Create a running snapshot, then power off
#   5. Update conf/kvm.conf
#   6. Restart CAPE services
#
# Usage:
#   sudo bash /opt/cape-registration/register-cape-vm.sh <build-date>
# =============================================================================
set -euo pipefail

BUILD_DATE="${1:?Usage: $0 <YYYY-MM-DD>}"
[[ "$BUILD_DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || { echo "Invalid build date" >&2; exit 1; }
export AWS_REGION="${AWS_REGION:-us-east-1}"
export ASSETS_BUCKET="${ASSETS_BUCKET:-adp-dev-cape-assets}"

VM_NAME="win11-cape-${BUILD_DATE}"
IMAGE_DIR="${IMAGE_DIR:-/opt/cape-data/images}"
KVM_CONF="${KVM_CONF:-/home/cape/CAPEv2/conf/kvm.conf}"
DOMAIN_XML_DIR="${DOMAIN_XML_DIR:-/opt/cape-data/domain-xml}"
CAPE_AGENT_PORT=8000

[[ -f "$KVM_CONF" ]] || { echo "Missing CAPE configuration: $KVM_CONF" >&2; exit 1; }
if virsh dominfo "$VM_NAME" >/dev/null 2>&1; then
  echo "Refusing to overwrite existing VM $VM_NAME" >&2
  exit 1
fi

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
      <source network='sandbox'/>
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
MAX_WAIT=300

while [[ $WAITED -lt $MAX_WAIT ]]; do
  VM_IP=$(virsh domifaddr "${VM_NAME}" 2>/dev/null | grep -oP '(\d+\.){3}\d+' | head -1 || true)
  if [[ -n "$VM_IP" ]]; then
    if curl -sf --connect-timeout 2 --max-time 5 "http://${VM_IP}:${CAPE_AGENT_PORT}/status" &>/dev/null; then
      echo "CAPE agent responding at ${VM_IP}:${CAPE_AGENT_PORT}"
      break
    fi
  fi
  sleep 5
  WAITED=$((WAITED + 5))
  echo "  Waiting for agent... (${WAITED}s / ${MAX_WAIT}s)"
done

if [[ $WAITED -ge $MAX_WAIT ]]; then
  echo "ERROR: Agent did not respond within ${MAX_WAIT}s." >&2
  virsh destroy "$VM_NAME" || true
  exit 1
fi

# ---------------------------------------------------------------------------
# 4. Snapshot the responsive running guest. CAPE restores this memory state.
# ---------------------------------------------------------------------------
echo "=== Step 4/6: Create running clean snapshot ==="
virsh snapshot-create-as --domain "$VM_NAME" --name clean \
  --description "Agent-ready state for CAPE analysis - ${BUILD_DATE}"
virsh snapshot-dumpxml "$VM_NAME" clean | grep '<state>running</state>' > /dev/null

# ---------------------------------------------------------------------------
# 5. Power off the new guest before CAPE takes ownership.
# ---------------------------------------------------------------------------
echo "=== Step 5/6: Power off snapshotted guest ==="
virsh destroy "$VM_NAME"
[[ "$(virsh domstate "$VM_NAME")" == "shut off" ]]

# ---------------------------------------------------------------------------
# 6. Update kvm.conf and restart services
# ---------------------------------------------------------------------------
echo "=== Step 6/6: Update kvm.conf ==="

# Use the libvirt domain name as the machinery label. Preserve existing machines.
python3 - "$KVM_CONF" "$VM_NAME" "$VM_IP" <<'PYCONF'
import configparser
import sys
from pathlib import Path
path, name, ip = sys.argv[1:]
config = configparser.ConfigParser(interpolation=None)
config.read(path)
machines = [value.strip() for value in config["kvm"]["machines"].split(",") if value.strip()]
if name not in machines:
    machines.append(name)
config["kvm"]["machines"] = ", ".join(machines)
config[name] = dict(label=name, platform="windows", arch="x64", ip=ip,
                    snapshot="clean", interface="virbr-sandbox",
                    resultserver_ip="192.168.100.1", resultserver_port="2042",
                    tags="win11,x64,windows")
with Path(path).open("w") as stream:
    config.write(stream)
PYCONF

# Restart CAPE services
systemctl restart cape cape-web cape-processor
systemctl is-active --quiet cape cape-web cape-processor

echo ""
echo "========================================"
echo "REGISTRATION COMPLETE"
echo "========================================"
echo "VM:       ${VM_NAME}"
echo "IP:       ${VM_IP:-unknown}"
echo "Snapshot: clean"
echo "Config:   ${KVM_CONF}"
echo ""
