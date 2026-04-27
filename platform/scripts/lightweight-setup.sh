#!/bin/bash
set -euo pipefail

# =============================================================================
# ADP — Lightweight Install (repo-config only)
# =============================================================================
# Configures a forked ADP repo so its agent workflows read from YOUR identifiers
# instead of the upstream `aws-e` hardcoded defaults. Assumes you already have:
#   - AWS account + working ARC runners with credentials to invoke Bedrock and
#     read from Secrets Manager (via IRSA or Pod Identity — we don't care which).
#   - A GitHub App created in your org, installed on the fork.
#
# What this script does NOT do:
#   - Create IAM roles or attach policies.
#   - Annotate service accounts or restart pods.
#   - Install or configure ARC.
#   - Enable Bedrock model access.
#   - Create GitHub Apps.
#
# If your ARC runners can't yet call AWS APIs, stop here. Set up IRSA or Pod
# Identity on your cluster first — that's a one-time cluster admin task, not
# something a per-repo install script should try to do.
#
# Usage:
#   ./platform/scripts/lightweight-setup.sh
# =============================================================================

trap 'echo ""; echo "ERROR on line $LINENO. Command: $BASH_COMMAND"; exit 1' ERR

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; RED='\033[0;31m'; BOLD='\033[1m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $1"; }
warn() { echo -e "${YELLOW}⚠${NC} $1"; }
fail() { echo -e "${RED}✗${NC} $1"; exit 1; }
info() { echo -e "${BLUE}→${NC} $1"; }
header() { echo ""; echo -e "${BOLD}=== $1 ===${NC}"; }
prompt() {
  local varname="$1" message="$2" default="${3:-}"
  if [ -n "$default" ]; then
    echo -en "${BLUE}?${NC} ${message} [${default}]: "
  else
    echo -en "${BLUE}?${NC} ${message}: "
  fi
  read -r input
  input="${input:-$default}"
  eval "$varname=\"$input\""
}

# --- Check gh CLI ---
command -v gh &>/dev/null || fail "GitHub CLI not installed. Install: https://cli.github.com/"
gh auth status &>/dev/null || fail "GitHub CLI not authenticated. Run: gh auth login (make sure 'repo' scope is granted)"
ok "gh CLI authenticated"

# --- Config ---
header "Configuration"
prompt GITHUB_ORG    "GitHub organization name"
prompt REPO_NAME     "Repository name"                       "adp"
prompt ARC_LABEL     "ARC runner label (used in runs-on:)"   "arc-runner-org"
prompt SECRET_PREFIX "Secrets Manager prefix where you'll store GitHub App credentials" "adp/${GITHUB_ORG}"
prompt AWS_REGION    "AWS region"                            "us-east-1"

FULL_REPO="${GITHUB_ORG}/${REPO_NAME}"

echo ""
echo -e "${BOLD}Summary${NC}"
echo "  Repo:            ${FULL_REPO}"
echo "  ARC label:       ${ARC_LABEL}"
echo "  Secret prefix:   ${SECRET_PREFIX}"
echo "  AWS region:      ${AWS_REGION}"
echo ""
prompt CONFIRM "Set these repo variables + create agent labels? (y/N)" "y"
[[ "$CONFIRM" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }

# --- Set repo variables ---
header "Setting repo variables"

set_var() {
  local name="$1" value="$2"
  if gh variable set "$name" --body "$value" --repo "$FULL_REPO" 2>/dev/null; then
    ok "${name} = ${value}"
  else
    warn "Failed to set ${name}. Is 'repo' scope granted on your gh auth? Try: gh auth refresh -h github.com -s repo"
  fi
}

set_var "SECRET_PREFIX"    "$SECRET_PREFIX"
set_var "ARC_RUNNER_LABEL" "$ARC_LABEL"
set_var "BEADS_ENABLED"    "false"
if [ "$AWS_REGION" != "us-east-1" ]; then
  set_var "AWS_REGION" "$AWS_REGION"
fi

# --- Create labels ---
header "Creating agent labels"

LABELS=("agent-developer" "agent-pm" "agent-operations" "agent-architect" "agent-product" "agent-reviewer" "agent-pt-superpower" "skill-agent")
for label in "${LABELS[@]}"; do
  if gh label create "$label" --color "1d76db" --repo "$FULL_REPO" 2>/dev/null; then
    ok "Created ${label}"
  else
    ok "${label} (already exists)"
  fi
done

# --- Next steps for the user ---
header "Next steps (do these yourself)"

cat <<NEXTSTEPS

The repo is configured. Two things remain — they require values only you have,
so the script doesn't try to do them for you.

1. Store your GitHub App credentials in AWS Secrets Manager at these paths:

     ${SECRET_PREFIX}/gh-app-dev-id   → the App ID (numeric)
     ${SECRET_PREFIX}/gh-app-dev-key  → the private key (full PEM contents)
     ${SECRET_PREFIX}/gh-app-pm-id    + -key
     ${SECRET_PREFIX}/gh-app-ops-id   + -key

   If you have one GitHub App for everything, store the SAME values under all
   three personas (dev / pm / ops). If you want independent rate limits, use
   three separate Apps.

   Example using the AWS CLI:

     aws secretsmanager create-secret \\
       --region ${AWS_REGION} \\
       --name "${SECRET_PREFIX}/gh-app-dev-id" \\
       --secret-string '<YOUR-APP-ID>'

     aws secretsmanager create-secret \\
       --region ${AWS_REGION} \\
       --name "${SECRET_PREFIX}/gh-app-dev-key" \\
       --secret-string "\$(cat /path/to/your-app.private-key.pem)"

   Repeat with pm / ops if you want separate apps, otherwise just reuse the
   dev values under the other persona names.

2. Confirm your ARC runners have AWS credentials with:

     bedrock:InvokeModel                  (on the Claude inference profiles)
     secretsmanager:GetSecretValue        (on arn:aws:secretsmanager:${AWS_REGION}:<acct>:secret:${SECRET_PREFIX}/gh-app-*)

   These come from the IAM role your runners already assume — either via IRSA
   (service-account annotation) or via EKS Pod Identity (pod-identity-association).
   The script doesn't create or modify this role; it's a cluster-admin concern.

   If you're not sure whether your runners have these permissions, run a smoke
   test (next step) — the workflow will fail with a clear AccessDenied if they
   don't, and you'll know exactly which permission is missing.

Smoke test — label any issue with 'agent-developer' and watch:

     gh issue create --repo ${FULL_REPO} \\
       --title 'chore: agent smoke test' \\
       --body 'Verifying agent pipeline.' \\
       --label agent-developer

     gh run list --repo ${FULL_REPO} --workflow agent-developer.yml --limit 1

NEXTSTEPS

ok "Done."
