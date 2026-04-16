#!/bin/bash
set -euo pipefail

# =============================================================================
# ADP — Install Prerequisites
# =============================================================================
# Checks for required tools and installs them if missing.
# Supports macOS (brew) and Linux (apt/yum).
#
# Usage:
#   ./platform/scripts/install-prereqs.sh           # Install minimum (aws, gh)
#   ./platform/scripts/install-prereqs.sh --all     # Install everything (for --local deploys)
# =============================================================================

INSTALL_ALL=false
[ "${1:-}" = "--all" ] && INSTALL_ALL=true

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓ $1${NC}"; }
warn() { echo -e "${YELLOW}⚠ $1${NC}"; }
fail() { echo -e "${RED}✗ $1${NC}"; }

# Detect OS and package manager
OS="$(uname -s)"
ARCH="$(uname -m)"
PKG=""
if [ "$OS" = "Darwin" ]; then
  command -v brew &>/dev/null && PKG="brew"
elif [ -f /etc/debian_version ]; then
  PKG="apt"
elif [ -f /etc/redhat-release ] || [ -f /etc/amazon-linux-release ]; then
  PKG="yum"
fi

install_tool() {
  local name="$1"
  local brew_pkg="${2:-$name}"
  local apt_pkg="${3:-$name}"
  local yum_pkg="${4:-$name}"
  local manual_url="$5"

  if command -v "$name" &>/dev/null; then
    ok "$name already installed"
    return 0
  fi

  echo -e "${BLUE}Installing $name...${NC}"
  case "$PKG" in
    brew) brew install "$brew_pkg" 2>/dev/null && ok "$name installed via brew" && return 0 ;;
    apt)  sudo apt-get update -qq && sudo apt-get install -y -qq "$apt_pkg" 2>/dev/null && ok "$name installed via apt" && return 0 ;;
    yum)  sudo yum install -y "$yum_pkg" 2>/dev/null && ok "$name installed via yum" && return 0 ;;
  esac

  fail "$name could not be auto-installed. Install manually: $manual_url"
  return 1
}

echo ""
echo "ADP Prerequisites Installer"
echo "============================"
echo "OS: $OS ($ARCH), Package manager: ${PKG:-none detected}"
echo ""

FAILED=0

# =============================================================================
# Required for all deploy modes
# =============================================================================
echo -e "${BLUE}── Required tools ──${NC}"

# AWS CLI
if command -v aws &>/dev/null; then
  ok "AWS CLI: $(aws --version 2>&1 | head -1)"
else
  echo -e "${BLUE}Installing AWS CLI...${NC}"
  if [ "$OS" = "Darwin" ]; then
    if [ "$PKG" = "brew" ]; then
      brew install awscli 2>/dev/null && ok "AWS CLI installed" || { fail "AWS CLI install failed"; FAILED=$((FAILED+1)); }
    else
      curl -s "https://awscli.amazonaws.com/AWSCLIV2.pkg" -o "/tmp/AWSCLIV2.pkg"
      sudo installer -pkg /tmp/AWSCLIV2.pkg -target / && ok "AWS CLI installed" || { fail "AWS CLI install failed"; FAILED=$((FAILED+1)); }
      rm -f /tmp/AWSCLIV2.pkg
    fi
  elif [ "$OS" = "Linux" ]; then
    curl -s "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o "/tmp/awscliv2.zip"
    unzip -q /tmp/awscliv2.zip -d /tmp && sudo /tmp/aws/install && ok "AWS CLI installed" || { fail "AWS CLI install failed"; FAILED=$((FAILED+1)); }
    rm -rf /tmp/awscliv2.zip /tmp/aws
  else
    fail "AWS CLI: install manually — https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
    FAILED=$((FAILED+1))
  fi
fi

# GitHub CLI
if command -v gh &>/dev/null; then
  ok "GitHub CLI: $(gh --version 2>&1 | head -1)"
else
  echo -e "${BLUE}Installing GitHub CLI...${NC}"
  case "$PKG" in
    brew) brew install gh 2>/dev/null && ok "GitHub CLI installed" || { fail "gh install failed"; FAILED=$((FAILED+1)); } ;;
    apt)
      curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg 2>/dev/null
      echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null
      sudo apt-get update -qq && sudo apt-get install -y -qq gh && ok "GitHub CLI installed" || { fail "gh install failed"; FAILED=$((FAILED+1)); }
      ;;
    yum)
      sudo yum-config-manager --add-repo https://cli.github.com/packages/rpm/gh-cli.repo 2>/dev/null
      sudo yum install -y gh && ok "GitHub CLI installed" || { fail "gh install failed"; FAILED=$((FAILED+1)); }
      ;;
    *) fail "GitHub CLI: install manually — https://cli.github.com/"; FAILED=$((FAILED+1)) ;;
  esac
fi

# zip (needed for CodeBuild source packaging)
install_tool "zip" "zip" "zip" "zip" "https://linux.die.net/man/1/zip" || FAILED=$((FAILED+1))

# kubectl (needed for post-deploy validation)
if command -v kubectl &>/dev/null; then
  ok "kubectl already installed"
else
  echo -e "${BLUE}Installing kubectl...${NC}"
  if [ "$OS" = "Darwin" ]; then
    [ "$PKG" = "brew" ] && brew install kubectl 2>/dev/null && ok "kubectl installed" || {
      curl -sLO "https://dl.k8s.io/release/$(curl -sL https://dl.k8s.io/release/stable.txt)/bin/darwin/$ARCH/kubectl"
      chmod +x kubectl && sudo mv kubectl /usr/local/bin/ && ok "kubectl installed" || { fail "kubectl install failed"; FAILED=$((FAILED+1)); }
    }
  elif [ "$OS" = "Linux" ]; then
    curl -sLO "https://dl.k8s.io/release/$(curl -sL https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
    chmod +x kubectl && sudo mv kubectl /usr/local/bin/ && ok "kubectl installed" || { fail "kubectl install failed"; FAILED=$((FAILED+1)); }
  fi
fi

# =============================================================================
# Additional tools (for --local or --all)
# =============================================================================
if [ "$INSTALL_ALL" = true ]; then
  echo ""
  echo -e "${BLUE}── Additional tools (--all) ──${NC}"

  # Terraform
  if command -v terraform &>/dev/null; then
    ok "Terraform already installed"
  else
    echo -e "${BLUE}Installing Terraform...${NC}"
    case "$PKG" in
      brew) brew tap hashicorp/tap 2>/dev/null; brew install hashicorp/tap/terraform 2>/dev/null && ok "Terraform installed" || { fail "Terraform install failed"; FAILED=$((FAILED+1)); } ;;
      yum)
        sudo yum-config-manager --add-repo https://rpm.releases.hashicorp.com/AmazonLinux/hashicorp.repo 2>/dev/null
        sudo yum install -y terraform && ok "Terraform installed" || { fail "Terraform install failed"; FAILED=$((FAILED+1)); }
        ;;
      apt)
        wget -qO- https://apt.releases.hashicorp.com/gpg | sudo gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg 2>/dev/null
        echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/hashicorp.list > /dev/null
        sudo apt-get update -qq && sudo apt-get install -y -qq terraform && ok "Terraform installed" || { fail "Terraform install failed"; FAILED=$((FAILED+1)); }
        ;;
      *) fail "Terraform: install manually — https://developer.hashicorp.com/terraform/install"; FAILED=$((FAILED+1)) ;;
    esac
  fi

  # Docker
  if command -v docker &>/dev/null; then
    ok "Docker already installed"
  else
    warn "Docker not installed. Install Docker Desktop: https://docs.docker.com/get-docker/"
    FAILED=$((FAILED+1))
  fi

  # Node.js
  if command -v node &>/dev/null; then
    ok "Node.js: $(node --version)"
  else
    install_tool "node" "node@22" "nodejs" "nodejs" "https://nodejs.org/" || FAILED=$((FAILED+1))
  fi

  # Helm
  install_tool "helm" "helm" "helm" "helm" "https://helm.sh/docs/intro/install/" || true
fi

# =============================================================================
# Auth checks
# =============================================================================
echo ""
echo -e "${BLUE}── Authentication ──${NC}"

if aws sts get-caller-identity &>/dev/null; then
  ok "AWS CLI authenticated: $(aws sts get-caller-identity --query Arn --output text)"
else
  warn "AWS CLI not configured. Run: aws configure"
fi

if gh auth status &>/dev/null; then
  ok "GitHub CLI authenticated: $(gh auth status 2>&1 | grep 'Logged in' | head -1)"
else
  warn "GitHub CLI not authenticated. Run: gh auth login"
fi

# =============================================================================
# Summary
# =============================================================================
echo ""
if [ "$FAILED" -gt 0 ]; then
  echo -e "${RED}$FAILED tool(s) could not be installed. Fix manually and re-run.${NC}"
  exit 1
else
  echo -e "${GREEN}All prerequisites installed. Ready to deploy.${NC}"
  echo ""
  echo "Next steps:"
  echo "  1. Configure AWS:    aws configure"
  echo "  2. Login to GitHub:  gh auth login"
  echo "  3. Deploy:           ./platform/scripts/deploy-all.sh"
fi
