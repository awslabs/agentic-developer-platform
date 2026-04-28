#!/bin/bash
# =============================================================================
# Phase 5: Windows Analysis VM + CAPE First Boot
# =============================================================================
# Creates a Windows 10 Enterprise evaluation VM inside the sandbox network,
# installs the CAPE guest agent, and takes a clean snapshot.
#
# NOTE: Windows installation requires ~30-60 minutes and is partially attended
# (OS install wizard via VNC/SPICE). This script automates everything it can
# and documents the manual steps clearly.
#
# Alternative: Use a pre-built CAPE-ready Windows qcow2 image from the
# community. See: https://github.com/kevoreilly/CAPEv2/wiki/Preparing-the-Guest
#
# Run via SSM send-command after Phase 4 completes.
# =============================================================================
set -euo pipefail

echo "=== Phase 5: Windows analysis VM setup ==="

CAPE_USER="cape"
CAPE_HOME="/home/$CAPE_USER"
DATA_DIR="/opt/cape-data"
ISO_DIR="$DATA_DIR/isos"
VM_NAME="win10-sandbox"
VM_DISK="$DATA_DIR/vms/${VM_NAME}.qcow2"
DISK_SIZE="40G"
SANDBOX_NET="sandbox"

mkdir -p "$DATA_DIR/vms" "$ISO_DIR"

# ---------------------------------------------------------------------------
# Step 1: Download Windows 10 Enterprise Evaluation ISO
# ---------------------------------------------------------------------------
# Microsoft provides 90-day evaluation ISOs. The URL changes periodically.
# If the direct download fails, the operator should manually download the ISO
# to $ISO_DIR/win10-eval.iso via SSM session.
#
# As of 2024, Microsoft requires a form submission for the ISO. For automated
# setups, a community qcow2 is more practical.
# ---------------------------------------------------------------------------

WIN_ISO="$ISO_DIR/win10-eval.iso"
VIRTIO_ISO="$ISO_DIR/virtio-win.iso"

# Download VirtIO drivers (needed for Windows to see the virtio disk)
if [ ! -f "$VIRTIO_ISO" ]; then
  echo "Downloading VirtIO drivers ISO..."
  wget -q -O "$VIRTIO_ISO" \
    "https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/virtio-win.iso" || {
    echo "WARNING: Failed to download VirtIO ISO. Download manually to $VIRTIO_ISO"
  }
fi

if [ ! -f "$WIN_ISO" ]; then
  echo ""
  echo "================================================================"
  echo "MANUAL STEP REQUIRED: Windows 10 Enterprise Evaluation ISO"
  echo "================================================================"
  echo "Download the Windows 10 Enterprise evaluation ISO from:"
  echo "  https://www.microsoft.com/en-us/evalcenter/evaluate-windows-10-enterprise"
  echo ""
  echo "Then upload it to this instance:"
  echo "  scp win10-eval.iso ssm-user@<instance>:$WIN_ISO"
  echo "  # or via SSM + S3:"
  echo "  aws s3 cp s3://your-bucket/win10-eval.iso $WIN_ISO"
  echo ""
  echo "After the ISO is in place, re-run this script."
  echo "================================================================"
  echo ""

  # Check if ISO exists after message
  if [ ! -f "$WIN_ISO" ]; then
    echo "Skipping VM creation — ISO not found. Continuing with CAPE config."
  fi
fi

# ---------------------------------------------------------------------------
# Step 2: Create VM disk
# ---------------------------------------------------------------------------
if [ ! -f "$VM_DISK" ]; then
  echo "Creating ${DISK_SIZE} qcow2 disk at $VM_DISK..."
  qemu-img create -f qcow2 "$VM_DISK" "$DISK_SIZE"
  chown "$CAPE_USER:$CAPE_USER" "$VM_DISK"
fi

# ---------------------------------------------------------------------------
# Step 3: Define and install VM (if ISO is available)
# ---------------------------------------------------------------------------
if [ -f "$WIN_ISO" ]; then
  echo "Creating Windows VM '$VM_NAME' on sandbox network..."

  # Destroy existing VM if present
  virsh destroy "$VM_NAME" 2>/dev/null || true
  virsh undefine "$VM_NAME" --snapshots-metadata 2>/dev/null || true

  virt-install \
    --name "$VM_NAME" \
    --ram 4096 \
    --vcpus 2 \
    --os-variant win10 \
    --disk path="$VM_DISK",format=qcow2,bus=virtio \
    --cdrom "$WIN_ISO" \
    --disk path="$VIRTIO_ISO",device=cdrom \
    --network network="$SANDBOX_NET",model=virtio \
    --graphics spice,listen=127.0.0.1 \
    --noautoconsole \
    --boot hd,cdrom

  echo ""
  echo "================================================================"
  echo "MANUAL STEP: Complete Windows installation via VNC/SPICE"
  echo "================================================================"
  echo "Connect to the VM's display:"
  echo "  # From an SSM session, forward SPICE port:"
  echo "  virsh domdisplay $VM_NAME"
  echo ""
  echo "During install:"
  echo "  1. Load VirtIO storage driver from the CD (virtio-win)"
  echo "  2. Complete Windows installation"
  echo "  3. Install VirtIO network driver from Device Manager"
  echo "  4. Inside Windows, run these commands (PowerShell as Admin):"
  echo ""
  echo '  # Disable Windows Defender'
  echo '  Set-MpPreference -DisableRealtimeMonitoring $true'
  echo '  Set-MpPreference -DisableBehaviorMonitoring $true'
  echo '  Set-MpPreference -DisableBlockAtFirstSeen $true'
  echo '  Set-MpPreference -DisableIOAVProtection $true'
  echo '  Set-MpPreference -DisablePrivacyMode $true'
  echo '  Set-MpPreference -DisableScriptScanning $true'
  echo ''
  echo '  # Disable Windows Update'
  echo '  Stop-Service wuauserv'
  echo '  Set-Service wuauserv -StartupType Disabled'
  echo ''
  echo '  # Disable UAC'
  echo '  Set-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" -Name "EnableLUA" -Value 0'
  echo ''
  echo '  # Install Python 3'
  echo '  # Download from https://www.python.org/downloads/ (via INetSim it will be faked)'
  echo '  # Or pre-stage python installer on the virtio CD'
  echo ''
  echo '  # Install CAPE agent'
  echo '  # Copy agent.py from /home/cape/CAPEv2/agent/ to C:\Users\<user>\agent.py'
  echo '  # Create a startup task:'
  echo '  schtasks /create /tn "CapeAgent" /tr "python C:\Users\<user>\agent.py" /sc onlogon /rl highest'
  echo ""
  echo "After setup, shut down the VM and run:"
  echo "  virsh snapshot-create-as $VM_NAME clean --description 'Clean snapshot for CAPE'"
  echo "================================================================"

else
  echo "No Windows ISO found — VM not created."
  echo "Define VM manually after placing ISO at $WIN_ISO"
fi

# ---------------------------------------------------------------------------
# Step 4: Configure CAPE to use the VM
# ---------------------------------------------------------------------------
CAPE_KVM_CONF="$CAPE_HOME/CAPEv2/conf/kvm.conf"

if [ -f "$CAPE_KVM_CONF" ]; then
  echo "Updating CAPE KVM configuration..."
  cat > "$CAPE_KVM_CONF" <<KVMCONF
[kvm]
# KVM machinery module configuration
machines = $VM_NAME
interface = virbr-sandbox

[$VM_NAME]
label = $VM_NAME
platform = windows
ip = 192.168.100.100
snapshot = clean
interface = virbr-sandbox
resultserver_ip = 192.168.100.1
resultserver_port = 2042
tags = win10,x64
options = noagent=0
KVMCONF
  chown "$CAPE_USER:$CAPE_USER" "$CAPE_KVM_CONF"
else
  echo "WARNING: $CAPE_KVM_CONF not found — CAPE may not be fully installed"
fi

# ---------------------------------------------------------------------------
# Step 5: Configure CAPE web API
# ---------------------------------------------------------------------------
CAPE_WEB_CONF="$CAPE_HOME/CAPEv2/conf/web.conf"

if [ -f "$CAPE_WEB_CONF" ]; then
  # Enable API token authentication
  sed -i 's/^api_token =.*/api_token = yes/' "$CAPE_WEB_CONF" 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
# Tag instance
# ---------------------------------------------------------------------------
# IMDSv2 token required (instance enforces http_tokens=required)
IMDS_TOKEN=$(curl -sX PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")
INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" http://169.254.169.254/latest/meta-data/instance-id)
REGION=$(curl -s -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" http://169.254.169.254/latest/meta-data/placement/region)
aws ec2 create-tags --resources "$INSTANCE_ID" \
  --tags "Key=cape-setup,Value=phase5-complete" --region "$REGION" || true

echo "=== Phase 5 complete ==="
echo ""
echo "Next steps:"
echo "  1. Complete Windows install if not done"
echo "  2. Install CAPE guest agent in Windows"
echo "  3. Shut down VM and take snapshot: virsh snapshot-create-as $VM_NAME clean"
echo "  4. Test: su - cape -c 'cd CAPEv2 && source venv/bin/activate && python3 cuckoo.py'"
