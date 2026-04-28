#!/bin/bash
# =============================================================================
# Phase 4: Sandbox Network + INetSim
# =============================================================================
# Creates the libvirt sandbox network (192.168.100.0/24), configures INetSim
# as the fake internet, and sets iptables rules to default-deny outbound from
# the sandbox bridge.
#
# Hard invariant #2: Analysis VMs have ZERO internet egress. All outbound goes
# to INetSim on the host (192.168.100.1).
#
# Run via SSM send-command after Phase 3 completes.
# =============================================================================
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

echo "=== Phase 4: Configuring sandbox network ==="

# ---------------------------------------------------------------------------
# Install iptables-persistent for rule persistence
# ---------------------------------------------------------------------------
apt-get install -y iptables-persistent

# ---------------------------------------------------------------------------
# Define libvirt sandbox network
# ---------------------------------------------------------------------------
SANDBOX_NET_XML="/tmp/sandbox-network.xml"
cat > "$SANDBOX_NET_XML" <<'NETXML'
<network>
  <name>sandbox</name>
  <bridge name="virbr-sandbox" stp="on" delay="0"/>
  <ip address="192.168.100.1" netmask="255.255.255.0">
    <dhcp>
      <range start="192.168.100.100" end="192.168.100.200"/>
    </dhcp>
  </ip>
</network>
NETXML

# Remove existing sandbox network if present, then recreate
virsh net-destroy sandbox 2>/dev/null || true
virsh net-undefine sandbox 2>/dev/null || true
virsh net-define "$SANDBOX_NET_XML"
virsh net-start sandbox
virsh net-autostart sandbox

echo "Sandbox network created: 192.168.100.0/24 on virbr-sandbox"

# ---------------------------------------------------------------------------
# Configure INetSim
# ---------------------------------------------------------------------------
INETSIM_CONF="/etc/inetsim/inetsim.conf"

# Back up original config
cp -f "$INETSIM_CONF" "${INETSIM_CONF}.bak" 2>/dev/null || true

cat > "$INETSIM_CONF" <<'INETSIMCONF'
# INetSim configuration for CAPE sandbox
# Binds to the sandbox bridge IP only

service_bind_address    192.168.100.1
dns_bind_port           53
dns_default_ip          192.168.100.1

# Enable core services
start_service dns
start_service http
start_service https
start_service smtp
start_service ftp
start_service pop3
start_service imap
start_service ntp

# HTTP/HTTPS settings
http_bind_port          80
https_bind_port         443
http_fakemode           1
https_fakemode          1

# Logging
logdir                  /var/log/inetsim
report_language         en
INETSIMCONF

mkdir -p /var/log/inetsim
chown -R inetsim:inetsim /var/log/inetsim 2>/dev/null || true

# Enable and start INetSim
systemctl enable inetsim
systemctl restart inetsim

echo "INetSim configured on 192.168.100.1"

# ---------------------------------------------------------------------------
# iptables rules — default-deny outbound from sandbox bridge
# ---------------------------------------------------------------------------
# Hard invariant #2: Analysis VMs have ZERO internet egress.
#
# Strategy:
# 1. Allow sandbox -> 192.168.100.1 (INetSim on the host) — this is the ONLY
#    allowed destination from the sandbox bridge.
# 2. Drop sandbox -> eth0 (public internet via NAT)
# 3. Drop sandbox -> 169.254.169.254 (EC2 IMDS — prevent credential theft)
# 4. Drop sandbox -> all private networks (prevent lateral movement)

SANDBOX_BRIDGE="virbr-sandbox"

# Flush existing sandbox-specific rules (idempotent re-run)
iptables -D FORWARD -i "$SANDBOX_BRIDGE" -d 192.168.100.1 -j ACCEPT 2>/dev/null || true
iptables -D FORWARD -i "$SANDBOX_BRIDGE" -d 169.254.169.254 -j DROP 2>/dev/null || true
iptables -D FORWARD -i "$SANDBOX_BRIDGE" -d 10.0.0.0/8 -j DROP 2>/dev/null || true
iptables -D FORWARD -i "$SANDBOX_BRIDGE" -d 172.16.0.0/12 -j DROP 2>/dev/null || true
iptables -D FORWARD -i "$SANDBOX_BRIDGE" -d 192.168.0.0/16 -j DROP 2>/dev/null || true
iptables -D FORWARD -i "$SANDBOX_BRIDGE" -o eth0 -j DROP 2>/dev/null || true
iptables -D FORWARD -i "$SANDBOX_BRIDGE" -j DROP 2>/dev/null || true

# Insert rules in order (first match wins)
# Rule 1: Allow sandbox -> INetSim host
iptables -I FORWARD 1 -i "$SANDBOX_BRIDGE" -d 192.168.100.1 -j ACCEPT

# Rule 2: Drop sandbox -> IMDS (credential theft prevention)
iptables -I FORWARD 2 -i "$SANDBOX_BRIDGE" -d 169.254.169.254 -j DROP

# Rule 3-5: Drop sandbox -> all private networks
iptables -I FORWARD 3 -i "$SANDBOX_BRIDGE" -d 10.0.0.0/8 -j DROP
iptables -I FORWARD 4 -i "$SANDBOX_BRIDGE" -d 172.16.0.0/12 -j DROP
iptables -I FORWARD 5 -i "$SANDBOX_BRIDGE" -d 192.168.0.0/16 -j DROP

# Rule 6: Drop sandbox -> internet (via any interface)
iptables -I FORWARD 6 -i "$SANDBOX_BRIDGE" -o eth0 -j DROP

# Rule 7: Catch-all drop for sandbox bridge (belts and suspenders)
iptables -A FORWARD -i "$SANDBOX_BRIDGE" -j DROP

# Persist rules
mkdir -p /etc/iptables
iptables-save > /etc/iptables/rules.v4

# Enable netfilter-persistent
systemctl enable netfilter-persistent

echo "iptables rules configured — sandbox bridge is fully isolated"
echo ""
echo "Rule verification:"
iptables -L FORWARD -v -n --line-numbers | grep -i sandbox

# ---------------------------------------------------------------------------
# Tag instance
# ---------------------------------------------------------------------------
# IMDSv2 token required (instance enforces http_tokens=required)
IMDS_TOKEN=$(curl -sX PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")
INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" http://169.254.169.254/latest/meta-data/instance-id)
REGION=$(curl -s -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" http://169.254.169.254/latest/meta-data/placement/region)
aws ec2 create-tags --resources "$INSTANCE_ID" \
  --tags "Key=cape-setup,Value=phase4-complete" --region "$REGION" || true

echo "=== Phase 4 complete: Sandbox network + INetSim configured ==="
echo "Verify with:"
echo "  virsh net-list --all"
echo "  iptables -L FORWARD -v -n"
echo "  systemctl is-active inetsim"
