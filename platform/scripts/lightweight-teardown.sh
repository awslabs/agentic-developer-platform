#!/bin/bash
set -euo pipefail

# =============================================================================
# ADP — Lightweight Install Teardown
# =============================================================================
# Removes what lightweight-setup.sh created on the repo side:
#   - GitHub repo variables (SECRET_PREFIX, ARC_RUNNER_LABEL, BEADS_ENABLED,
#     optionally AWS_REGION)
#   - Agent issue labels (optional — prompts)
#
# Does NOT remove:
#   - AWS resources — this script never created any. Your GitHub App secrets in
#     Secrets Manager were stored by you, not by the setup script. Remove them
#     yourself if you want:
#
#       aws secretsmanager delete-secret --secret-id <prefix>/gh-app-dev-id \
#         --force-delete-without-recovery
#       # ...repeat for dev-key, pm-id, pm-key, ops-id, ops-key
#
#   - IAM roles, SA annotations, or anything else on your cluster. The setup
#     script didn't touch those.
#
# Usage:
#   ./platform/scripts/lightweight-teardown.sh <github-org> <repo-name>
#
# Example:
#   ./platform/scripts/lightweight-teardown.sh acmecorp adp
# =============================================================================

trap 'echo ""; echo "ERROR on line $LINENO. Command: $BASH_COMMAND"; exit 1' ERR

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; RED='\033[0;31m'; BOLD='\033[1m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $1"; }
warn() { echo -e "${YELLOW}⚠${NC} $1"; }
fail() { echo -e "${RED}✗${NC} $1"; exit 1; }
info() { echo -e "${BLUE}→${NC} $1"; }

GITHUB_ORG="${1:-}"
REPO_NAME="${2:-adp}"

if [ -z "$GITHUB_ORG" ]; then
  echo "Usage: $0 <github-org> [repo-name]"
  echo ""
  echo "Example: $0 acmecorp adp"
  exit 1
fi

FULL_REPO="${GITHUB_ORG}/${REPO_NAME}"

echo ""
echo -e "${BOLD}This will remove repo-side config for: ${FULL_REPO}${NC}"
echo "  - Repo variables: SECRET_PREFIX, ARC_RUNNER_LABEL, BEADS_ENABLED, AWS_REGION"
echo ""
echo -e "${YELLOW}Note${NC}: Secrets in AWS Secrets Manager and IAM roles on your cluster are NOT touched."
echo "      See the script comments for how to remove those manually."
echo ""
echo -n "Type 'yes' to confirm: "
read -r CONFIRM
[ "$CONFIRM" = "yes" ] || { echo "Aborted."; exit 0; }

# --- Repo variables ---
info "Deleting repo variables..."
for var in SECRET_PREFIX ARC_RUNNER_LABEL BEADS_ENABLED AWS_REGION; do
  if gh variable delete "$var" --repo "$FULL_REPO" 2>/dev/null; then
    ok "Deleted ${var}"
  else
    warn "${var} not found (already deleted)"
  fi
done

# --- Labels (optional) ---
echo ""
echo -n "Also delete agent issue labels (agent-developer, agent-pm, etc.)? (y/N): "
read -r DELETE_LABELS
if [[ "$DELETE_LABELS" =~ ^[Yy]$ ]]; then
  for label in agent-developer agent-pm agent-operations agent-architect agent-product agent-reviewer agent-pt-superpower skill-agent; do
    if gh label delete "$label" --repo "$FULL_REPO" --yes 2>/dev/null; then
      ok "Deleted label: ${label}"
    else
      warn "${label} not found"
    fi
  done
fi

ok "Done."
