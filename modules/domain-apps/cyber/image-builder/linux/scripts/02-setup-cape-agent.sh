#!/bin/bash
# =============================================================================
# 02-setup-cape-agent.sh — Install CAPE guest agent as systemd service
# =============================================================================
# Runs inside the Ubuntu guest VM during Packer provisioning.
# Moves the pre-staged agent.py to its permanent location, creates a systemd
# unit, and applies anti-evasion baseline (hostname, fake user files).
# =============================================================================
set -euxo pipefail

# ---------------------------------------------------------------------------
# Install CAPE agent
# ---------------------------------------------------------------------------

sudo mkdir -p /opt/cape-agent
sudo mv /tmp/agent.py /opt/cape-agent/agent.py
sudo chmod +x /opt/cape-agent/agent.py

# Systemd unit — starts agent at boot, listens on port 8000
sudo tee /etc/systemd/system/cape-agent.service <<'UNIT'
[Unit]
Description=CAPE guest agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/cape-agent/agent.py
Restart=on-failure
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable cape-agent.service

# ---------------------------------------------------------------------------
# Anti-evasion baseline
# ---------------------------------------------------------------------------
# Rename hostname to something that doesn't scream "sandbox" or "vm"
sudo hostnamectl set-hostname web-dev-01

# Create fake user artifacts (desktop files, browser history placeholder)
sudo mkdir -p /home/cape/Documents /home/cape/Downloads /home/cape/.config
sudo touch /home/cape/Documents/readme.txt
sudo touch /home/cape/Documents/project-notes.txt
sudo touch /home/cape/Downloads/report-q4.pdf
sudo chown -R cape:cape /home/cape/

# Set timezone to something common (not UTC which is a sandbox indicator)
sudo timedatectl set-timezone America/New_York 2>/dev/null || true

# ---------------------------------------------------------------------------
# Clean cloud-init artifacts for pristine next-boot
# Keep /etc/netplan/ intact so DHCP works when booted on CAPE host.
# ---------------------------------------------------------------------------
sudo cloud-init clean --logs
sudo rm -rf /var/log/cloud-init*

# Clear bash history and logs
sudo truncate -s 0 /var/log/syslog 2>/dev/null || true
sudo truncate -s 0 /var/log/auth.log 2>/dev/null || true
history -c 2>/dev/null || true

echo "CAPE agent installed and enabled. Anti-evasion baseline applied."
