#!/bin/bash
# =============================================================================
# 01-install-deps.sh — Install system dependencies for CAPE guest agent
# =============================================================================
# Runs inside the Ubuntu guest VM during Packer provisioning.
# Installs Python 3, network tools, and utilities needed by the CAPE agent.
# =============================================================================
set -euxo pipefail

export DEBIAN_FRONTEND=noninteractive

# Wait for cloud-init to fully finish (dpkg lock release)
cloud-init status --wait || true

sudo apt-get update
sudo apt-get install -y \
  python3 \
  python3-pip \
  git \
  tcpdump \
  curl \
  strace \
  ltrace \
  net-tools \
  iproute2

# Clean apt cache to reduce image size
sudo apt-get clean
sudo rm -rf /var/lib/apt/lists/*
