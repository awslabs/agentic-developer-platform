#!/bin/bash
set -euo pipefail

# =============================================================================
# ADP — Lightweight Install Teardown
# =============================================================================
# Removes everything created by lightweight-setup.sh:
#   - IAM role and inline policy
#   - Secrets Manager secrets
#   - GitHub repo variables
#   - Issue labels (optional)
#
# Usage:
#   ./platform/scripts/lightweight-teardown.sh <github-org> <repo-name> <secret-prefix> [aws-region]
#
# Example:
#   ./platform/scripts/lightweight-teardown.sh acmecorp adp adp/acmecorp us-east-1
# =============================================================================

trap 'echo ""; echo "ERROR on line $LINENO. Command: $BASH_COMMAND"; exit 1' ERR

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $1"; }
warn() { echo -e "${YELLOW}⚠${NC} $1"; }
fail() { echo -e "${RED}✗${NC} $1"; exit 1; }
info() { echo -e "${BLUE}→${NC} $1"; }
header() { echo ""; echo -e "${BOLD}=== $1 ===${NC}"; }

GITHUB_ORG="${1:-}"
REPO_NAME="${2:-adp}"
SECRET_PREFIX="${3:-}"
AWS_REGION="${4:-us-east-1}"

if [ -z "$GITHUB_ORG" ] || [ -z "$SECRET_PREFIX" ]; then
  echo "Usage: $0 <github-org> <repo-name> <secret-prefix> [aws-region]"
  echo ""
  echo "Example: $0 acmecorp adp adp/acmecorp us-east-1"
  exit 1
fi

FULL_REPO="${GITHUB_ORG}/${REPO_NAME}"
ROLE_NAME="adp-agent-runner-role"

echo ""
echo -e "${BOLD}This will remove:${NC}"
echo "  - IAM role: ${ROLE_NAME}"
echo "  - Secrets: ${SECRET_PREFIX}/gh-app-{dev,pm,ops}-{id,key}"
echo "  - Repo variables: SECRET_PREFIX, ARC_RUNNER_LABEL, EKS_CLUSTER, BEADS_ENABLED, AWS_REGION"
echo "  - From repo: ${FULL_REPO}"
echo ""
echo -n "Type 'yes' to confirm: "
read -r CONFIRM
[ "$CONFIRM" = "yes" ] || { echo "Aborted."; exit 0; }

# --- Delete Secrets ---
header "Deleting Secrets Manager secrets"
for suffix in gh-app-dev-id gh-app-dev-key gh-app-pm-id gh-app-pm-key gh-app-ops-id gh-app-ops-key; do
  secret_id="${SECRET_PREFIX}/${suffix}"
  if aws secretsmanager delete-secret --secret-id "$secret_id" --force-delete-without-recovery --region "$AWS_REGION" 2>/dev/null; then
    ok "Deleted: ${secret_id}"
  else
    warn "Not found or already deleted: ${secret_id}"
  fi
done

# --- Delete IAM role ---
header "Deleting IAM role"
if aws iam get-role --role-name "$ROLE_NAME" &>/dev/null 2>&1; then
  # Delete inline policies first
  POLICIES=$(aws iam list-role-policies --role-name "$ROLE_NAME" --query 'PolicyNames' --output text 2>/dev/null || echo "")
  for policy in $POLICIES; do
    aws iam delete-role-policy --role-name "$ROLE_NAME" --policy-name "$policy"
    ok "Deleted inline policy: ${policy}"
  done
  # Delete attached managed policies
  ATTACHED=$(aws iam list-attached-role-policies --role-name "$ROLE_NAME" --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null || echo "")
  for arn in $ATTACHED; do
    aws iam detach-role-policy --role-name "$ROLE_NAME" --policy-arn "$arn"
    ok "Detached policy: ${arn}"
  done
  aws iam delete-role --role-name "$ROLE_NAME"
  ok "Deleted IAM role: ${ROLE_NAME}"
else
  warn "IAM role not found: ${ROLE_NAME}"
fi

# --- Delete repo variables ---
header "Deleting GitHub repo variables"
for var in SECRET_PREFIX ARC_RUNNER_LABEL EKS_CLUSTER BEADS_ENABLED AWS_REGION; do
  if gh variable delete "$var" --repo "$FULL_REPO" 2>/dev/null; then
    ok "Deleted variable: ${var}"
  else
    warn "Variable not found or already deleted: ${var}"
  fi
done

# --- Labels (optional) ---
echo ""
echo -n "Delete agent labels from the repo? (y/N): "
read -r DEL_LABELS
if [[ "$DEL_LABELS" =~ ^[Yy]$ ]]; then
  header "Deleting issue labels"
  for label in agent-developer agent-pm agent-operations agent-architect agent-product agent-reviewer agent-pt-superpower skill-agent agent-failed; do
    if gh label delete "$label" --repo "$FULL_REPO" --yes 2>/dev/null; then
      ok "Deleted label: ${label}"
    else
      warn "Label not found: ${label}"
    fi
  done
fi

header "Teardown complete"
echo ""
echo "Removed: IAM role, secrets, repo variables."
echo "The GitHub App itself was NOT deleted — remove it from:"
echo "  https://github.com/organizations/${GITHUB_ORG}/settings/apps"
echo ""
