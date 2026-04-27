#!/bin/bash
set -euo pipefail

# =============================================================================
# ADP — Lightweight Install Setup Script
# =============================================================================
# Sets up ADP agent workflows on your own AWS + EKS + ARC infrastructure.
# This script does NOT provision EKS, ARC, or Bedrock access — those are
# prerequisites you must have ready before running this.
#
# Usage:
#   ./platform/scripts/lightweight-setup.sh
#
# What it does:
#   1. Validates prerequisites (aws, gh, kubectl)
#   2. Collects configuration interactively
#   3. Creates IAM role for runner IRSA (Bedrock + Secrets Manager access)
#   4. Stores GitHub App credentials in AWS Secrets Manager
#   5. Annotates the ARC runner service account for IRSA
#   6. Sets GitHub repo variables for parameterised workflows
#   7. Creates issue labels for agent triggers
#   8. Runs a smoke test
# =============================================================================

# --- Error handling ---
trap 'echo ""; echo "ERROR on line $LINENO. Command: $BASH_COMMAND"; echo "Run with: bash -x $0 to debug"; exit 1' ERR

# --- Colors ---
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
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
  printf -v "$varname" '%s' "$input"
}

# =============================================================================
# Phase 1: Prerequisites
# =============================================================================
header "Checking prerequisites"

# AWS CLI
if ! command -v aws &>/dev/null; then
  fail "AWS CLI not installed. Install: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
fi
ok "AWS CLI installed ($(aws --version 2>&1 | head -1))"

# AWS authentication
if ! aws sts get-caller-identity &>/dev/null; then
  fail "AWS CLI not authenticated. Run: aws configure"
fi
CALLER_IDENTITY=$(aws sts get-caller-identity --output json)
ACCOUNT_ID=$(echo "$CALLER_IDENTITY" | grep -o '"Account": "[^"]*"' | cut -d'"' -f4)
CALLER_ARN=$(echo "$CALLER_IDENTITY" | grep -o '"Arn": "[^"]*"' | cut -d'"' -f4)
ok "AWS authenticated — Account: ${ACCOUNT_ID}, Identity: ${CALLER_ARN}"

# gh CLI
if ! command -v gh &>/dev/null; then
  fail "GitHub CLI not installed. Install: https://cli.github.com/"
fi
ok "GitHub CLI installed"

if ! gh auth status &>/dev/null 2>&1; then
  fail "GitHub CLI not authenticated. Run: gh auth login"
fi
ok "GitHub CLI authenticated"

# kubectl
if ! command -v kubectl &>/dev/null; then
  fail "kubectl not installed. Install: https://kubernetes.io/docs/tasks/tools/"
fi
ok "kubectl installed"

if ! kubectl cluster-info &>/dev/null 2>&1; then
  fail "kubectl cannot reach a cluster. Configure with: aws eks update-kubeconfig --name <cluster>"
fi
ok "kubectl connected to cluster"

# Bedrock access check (warning only)
AWS_REGION_CHECK="${AWS_REGION:-us-east-1}"
if aws bedrock list-inference-profiles --region "$AWS_REGION_CHECK" --output text 2>/dev/null | grep -q "claude"; then
  ok "Bedrock Claude models accessible in ${AWS_REGION_CHECK}"
else
  warn "Could not verify Bedrock Claude model access in ${AWS_REGION_CHECK}."
  warn "Ensure model access is enabled: https://console.aws.amazon.com/bedrock/home#/modelaccess"
fi

# =============================================================================
# Phase 2: Interactive configuration
# =============================================================================
header "Configuration"

prompt GITHUB_ORG "GitHub organization name"
prompt REPO_NAME "Repository name" "adp"
prompt AWS_REGION "AWS region" "us-east-1"
prompt EKS_CLUSTER "EKS cluster name"
prompt ARC_RUNNER_NS "ARC runner K8s namespace" "arc-runners"
prompt ARC_RUNNER_SA "ARC runner service account name" "github-runner"
prompt ARC_RUNNER_LABEL "ARC runner label (used in runs-on:)" "arc-runner-org"
prompt SECRET_PREFIX "Secrets Manager prefix" "adp/${GITHUB_ORG}"

echo ""
info "GitHub App credentials"
info "You need a GitHub App with these permissions: Issues R/W, Pull Requests R/W, Contents R/W, Metadata R"
info "Create one at: https://github.com/organizations/${GITHUB_ORG}/settings/apps/new"
echo ""

prompt GH_APP_DEV_ID "GitHub App ID for dev persona (numeric)"
prompt GH_APP_DEV_KEY_PATH "Path to GitHub App private key (.pem file)"

# Validate key file
if [ ! -f "$GH_APP_DEV_KEY_PATH" ]; then
  fail "File not found: ${GH_APP_DEV_KEY_PATH}"
fi
ok "Private key file found"

# Ask about additional personas
echo ""
info "The agent workflows support 3 personas (dev, pm, ops) with separate rate limits."
info "You can use the same App for all 3 or provide separate App credentials."
prompt MULTI_PERSONA "Use separate Apps for pm and ops? (y/N)" "N"

if [[ "$MULTI_PERSONA" =~ ^[Yy]$ ]]; then
  prompt GH_APP_PM_ID "GitHub App ID for pm persona"
  prompt GH_APP_PM_KEY_PATH "Path to pm private key (.pem file)"
  [ -f "$GH_APP_PM_KEY_PATH" ] || fail "File not found: ${GH_APP_PM_KEY_PATH}"

  prompt GH_APP_OPS_ID "GitHub App ID for ops persona"
  prompt GH_APP_OPS_KEY_PATH "Path to ops private key (.pem file)"
  [ -f "$GH_APP_OPS_KEY_PATH" ] || fail "File not found: ${GH_APP_OPS_KEY_PATH}"
else
  GH_APP_PM_ID="$GH_APP_DEV_ID"
  GH_APP_PM_KEY_PATH="$GH_APP_DEV_KEY_PATH"
  GH_APP_OPS_ID="$GH_APP_DEV_ID"
  GH_APP_OPS_KEY_PATH="$GH_APP_DEV_KEY_PATH"
  warn "Using same App for all 3 personas. Rate limits will be shared (5000 req/hr total)."
fi

echo ""
header "Configuration summary"
echo "  GitHub org:      ${GITHUB_ORG}"
echo "  Repo:            ${GITHUB_ORG}/${REPO_NAME}"
echo "  AWS account:     ${ACCOUNT_ID}"
echo "  AWS region:      ${AWS_REGION}"
echo "  EKS cluster:     ${EKS_CLUSTER}"
echo "  ARC namespace:   ${ARC_RUNNER_NS}"
echo "  ARC SA:          ${ARC_RUNNER_SA}"
echo "  ARC label:       ${ARC_RUNNER_LABEL}"
echo "  Secret prefix:   ${SECRET_PREFIX}"
echo "  Dev App ID:      ${GH_APP_DEV_ID}"
echo "  PM App ID:       ${GH_APP_PM_ID}"
echo "  Ops App ID:      ${GH_APP_OPS_ID}"
echo ""
prompt CONFIRM "Proceed? (y/N)" "y"
[[ "$CONFIRM" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }

# =============================================================================
# Phase 3: Create IAM role for IRSA
# =============================================================================
header "Creating IAM role for IRSA"

ROLE_NAME="adp-agent-runner-role"

# Get OIDC issuer
OIDC_ISSUER=$(aws eks describe-cluster --name "$EKS_CLUSTER" --region "$AWS_REGION" \
  --query 'cluster.identity.oidc.issuer' --output text)
OIDC_PROVIDER="${OIDC_ISSUER#https://}"
ok "OIDC issuer: ${OIDC_PROVIDER}"

# Check if role exists
if aws iam get-role --role-name "$ROLE_NAME" &>/dev/null 2>&1; then
  warn "IAM role '${ROLE_NAME}' already exists."
  prompt REUSE_ROLE "Reuse existing role? (Y/n)" "Y"
  if [[ ! "$REUSE_ROLE" =~ ^[Yy]$ ]]; then
    echo "Exiting. Delete the role manually and re-run."
    exit 1
  fi
  ok "Reusing existing IAM role"
  ROLE_ARN=$(aws iam get-role --role-name "$ROLE_NAME" --query 'Role.Arn' --output text)
else
  info "Creating IAM role: ${ROLE_NAME}"

  # Trust policy
  TRUST_POLICY=$(cat <<TRUSTEOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::${ACCOUNT_ID}:oidc-provider/${OIDC_PROVIDER}"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "${OIDC_PROVIDER}:sub": "system:serviceaccount:${ARC_RUNNER_NS}:${ARC_RUNNER_SA}",
          "${OIDC_PROVIDER}:aud": "sts.amazonaws.com"
        }
      }
    }
  ]
}
TRUSTEOF
)

  aws iam create-role \
    --role-name "$ROLE_NAME" \
    --assume-role-policy-document "$TRUST_POLICY" \
    --description "ADP agent runner role for IRSA — lightweight install" \
    --output text --query 'Role.Arn'

  ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"
  ok "IAM role created: ${ROLE_ARN}"

  # Inline policy: Bedrock invoke + Secrets Manager read
  POLICY_DOC=$(cat <<POLICYEOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "BedrockInvoke",
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
        "bedrock:ListInferenceProfiles"
      ],
      "Resource": "*"
    },
    {
      "Sid": "SecretsManagerRead",
      "Effect": "Allow",
      "Action": [
        "secretsmanager:GetSecretValue",
        "secretsmanager:DescribeSecret"
      ],
      "Resource": "arn:aws:secretsmanager:${AWS_REGION}:${ACCOUNT_ID}:secret:${SECRET_PREFIX}/gh-app-*"
    }
  ]
}
POLICYEOF
)

  aws iam put-role-policy \
    --role-name "$ROLE_NAME" \
    --policy-name "adp-agent-runner-policy" \
    --policy-document "$POLICY_DOC"

  ok "Inline policy attached (Bedrock + Secrets Manager)"
fi

ROLE_ARN=$(aws iam get-role --role-name "$ROLE_NAME" --query 'Role.Arn' --output text)

# =============================================================================
# Phase 4: Store GitHub App secrets in Secrets Manager
# =============================================================================
header "Storing GitHub App secrets"

store_secret() {
  local secret_id="$1" secret_value="$2"

  if aws secretsmanager describe-secret --secret-id "$secret_id" --region "$AWS_REGION" &>/dev/null 2>&1; then
    warn "Secret '${secret_id}' already exists."
    prompt OVERWRITE "Overwrite? (y/N)" "N"
    if [[ "$OVERWRITE" =~ ^[Yy]$ ]]; then
      aws secretsmanager put-secret-value \
        --secret-id "$secret_id" \
        --secret-string "$secret_value" \
        --region "$AWS_REGION" >/dev/null
      ok "Secret updated: ${secret_id}"
    else
      ok "Keeping existing secret: ${secret_id}"
    fi
  else
    aws secretsmanager create-secret \
      --name "$secret_id" \
      --secret-string "$secret_value" \
      --region "$AWS_REGION" \
      --description "ADP agent GitHub App credential — lightweight install" >/dev/null
    ok "Secret created: ${secret_id}"
  fi
}

GH_APP_DEV_KEY=$(cat "$GH_APP_DEV_KEY_PATH")
GH_APP_PM_KEY=$(cat "$GH_APP_PM_KEY_PATH")
GH_APP_OPS_KEY=$(cat "$GH_APP_OPS_KEY_PATH")

store_secret "${SECRET_PREFIX}/gh-app-dev-id" "$GH_APP_DEV_ID"
store_secret "${SECRET_PREFIX}/gh-app-dev-key" "$GH_APP_DEV_KEY"
store_secret "${SECRET_PREFIX}/gh-app-pm-id" "$GH_APP_PM_ID"
store_secret "${SECRET_PREFIX}/gh-app-pm-key" "$GH_APP_PM_KEY"
store_secret "${SECRET_PREFIX}/gh-app-ops-id" "$GH_APP_OPS_ID"
store_secret "${SECRET_PREFIX}/gh-app-ops-key" "$GH_APP_OPS_KEY"

# =============================================================================
# Phase 5: Annotate runner service account for IRSA
# =============================================================================
header "Configuring IRSA on runner service account"

# Check if SA exists
if kubectl get sa "$ARC_RUNNER_SA" -n "$ARC_RUNNER_NS" &>/dev/null 2>&1; then
  kubectl annotate sa "$ARC_RUNNER_SA" -n "$ARC_RUNNER_NS" \
    "eks.amazonaws.com/role-arn=${ROLE_ARN}" \
    --overwrite
  ok "Service account annotated: ${ARC_RUNNER_SA} in ${ARC_RUNNER_NS}"
else
  warn "Service account '${ARC_RUNNER_SA}' not found in namespace '${ARC_RUNNER_NS}'."
  info "Available service accounts in ${ARC_RUNNER_NS}:"
  kubectl get sa -n "$ARC_RUNNER_NS" --no-headers 2>/dev/null || echo "  (namespace may not exist)"
  echo ""
  prompt ACTUAL_SA "Enter the correct service account name (or 'skip' to skip)"
  if [ "$ACTUAL_SA" != "skip" ]; then
    kubectl annotate sa "$ACTUAL_SA" -n "$ARC_RUNNER_NS" \
      "eks.amazonaws.com/role-arn=${ROLE_ARN}" \
      --overwrite
    ok "Service account annotated: ${ACTUAL_SA}"
  else
    warn "Skipping SA annotation. You'll need to annotate manually:"
    warn "  kubectl annotate sa <sa-name> -n ${ARC_RUNNER_NS} eks.amazonaws.com/role-arn=${ROLE_ARN}"
  fi
fi

# Restart ONLY pods that use the annotated runner SA to pick up the new IRSA
# annotation. Scope this tightly — `kubectl delete pods --all` would kill
# every pod in the namespace including the ARC controller and listener pods,
# and would terminate any in-flight workflow jobs mid-run.
#
# Determine the SA we actually annotated: either ARC_RUNNER_SA (the user's
# default answer that existed), or ACTUAL_SA (the replacement they entered
# when the default didn't exist). If the user typed "skip", we didn't
# annotate anything — tell them how to restart manually and move on.
if [ "${ACTUAL_SA:-}" = "skip" ]; then
  info "SA annotation was skipped; no pods to restart. After you annotate the SA manually, restart matching pods with:"
  info "  kubectl delete pods -n ${ARC_RUNNER_NS} --field-selector spec.serviceAccountName=<sa-name>"
else
  RESTART_SA="${ACTUAL_SA:-$ARC_RUNNER_SA}"
  info "Restarting pods in ${ARC_RUNNER_NS} that use SA '${RESTART_SA}' (scoped — other pods in the namespace are untouched)..."
  MATCHING_PODS=$(kubectl get pods -n "$ARC_RUNNER_NS" \
    --field-selector "spec.serviceAccountName=${RESTART_SA}" \
    -o name 2>/dev/null || echo "")
  if [ -n "$MATCHING_PODS" ]; then
    echo "$MATCHING_PODS" | while read -r pod; do
      kubectl delete "$pod" -n "$ARC_RUNNER_NS" --wait=false 2>/dev/null || true
    done
    ok "Restarted runner pods using SA '${RESTART_SA}'"
  else
    info "No existing pods found using SA '${RESTART_SA}'. New pods created by ARC will pick up the annotation automatically."
  fi
fi

# =============================================================================
# Phase 6: Set GitHub repo variables
# =============================================================================
header "Setting GitHub repo variables"

FULL_REPO="${GITHUB_ORG}/${REPO_NAME}"

set_var() {
  local name="$1" value="$2"
  if gh variable set "$name" --body "$value" --repo "$FULL_REPO" 2>/dev/null; then
    ok "Set ${name}=${value}"
  else
    warn "Failed to set ${name}. You may need to re-run 'gh auth login' with 'repo' scope."
    warn "  Command: gh variable set ${name} --body '${value}' --repo ${FULL_REPO}"
  fi
}

set_var "SECRET_PREFIX" "$SECRET_PREFIX"
set_var "ARC_RUNNER_LABEL" "$ARC_RUNNER_LABEL"
set_var "EKS_CLUSTER" "$EKS_CLUSTER"
set_var "BEADS_ENABLED" "false"

# Only set AWS_REGION if non-default
if [ "$AWS_REGION" != "us-east-1" ]; then
  set_var "AWS_REGION" "$AWS_REGION"
fi

# =============================================================================
# Phase 7: Create issue labels
# =============================================================================
header "Creating issue labels"

LABELS=("agent-developer" "agent-pm" "agent-operations" "agent-architect" "agent-product" "agent-reviewer" "agent-pt-superpower" "skill-agent" "agent-failed")
COLORS=("0E8A16" "5319E7" "D93F0B" "1D76DB" "FBCA04" "BFD4F2" "C5DEF5" "006B75" "B60205")

for i in "${!LABELS[@]}"; do
  label="${LABELS[$i]}"
  color="${COLORS[$i]}"
  if gh label create "$label" --color "$color" --repo "$FULL_REPO" 2>/dev/null; then
    ok "Created label: ${label}"
  else
    ok "Label already exists: ${label}"
  fi
done

# =============================================================================
# Phase 8: Smoke test
# =============================================================================
header "Smoke test"

prompt RUN_SMOKE "Create a smoke-test issue to verify the setup? (Y/n)" "Y"

if [[ "$RUN_SMOKE" =~ ^[Yy]$ ]]; then
  info "Creating smoke-test issue..."
  ISSUE_URL=$(gh issue create \
    --title "chore: lightweight install smoke test" \
    --body "Automated smoke test from \`lightweight-setup.sh\`. If this issue gets a comment from @agent-developer, the setup is working correctly. Safe to close after verification." \
    --label "agent-developer" \
    --repo "$FULL_REPO")

  ok "Smoke-test issue created: ${ISSUE_URL}"
  info "The agent-developer workflow should trigger within 30 seconds."
  info "Watch it with:"
  echo "  gh run list --repo ${FULL_REPO} --workflow agent-developer.yml --limit 1"
  echo "  gh run watch --repo ${FULL_REPO} \$(gh run list --repo ${FULL_REPO} --workflow agent-developer.yml --limit 1 --json databaseId --jq '.[0].databaseId')"

  # Wait briefly and check if workflow triggered
  echo ""
  info "Waiting 15 seconds for workflow to trigger..."
  sleep 15

  LATEST_RUN=$(gh run list --repo "$FULL_REPO" --workflow agent-developer.yml --limit 1 --json status,databaseId,url --jq '.[0]' 2>/dev/null || echo "")
  if [ -n "$LATEST_RUN" ]; then
    RUN_STATUS=$(echo "$LATEST_RUN" | grep -o '"status":"[^"]*"' | cut -d'"' -f4)
    RUN_URL=$(echo "$LATEST_RUN" | grep -o '"url":"[^"]*"' | cut -d'"' -f4)
    ok "Workflow triggered! Status: ${RUN_STATUS}"
    info "Watch: ${RUN_URL}"
  else
    warn "Could not detect a workflow run yet. Check manually:"
    warn "  gh run list --repo ${FULL_REPO} --workflow agent-developer.yml --limit 1"
  fi
else
  info "Skipping smoke test."
fi

# =============================================================================
# Done
# =============================================================================
header "Setup complete!"
echo ""
echo "Your ADP lightweight install is configured:"
echo ""
echo "  Repo:           ${FULL_REPO}"
echo "  IAM role:       ${ROLE_ARN}"
echo "  Secrets:        ${SECRET_PREFIX}/gh-app-{dev,pm,ops}-{id,key}"
echo "  Runner IRSA:    ${ARC_RUNNER_SA}@${ARC_RUNNER_NS} → ${ROLE_ARN}"
echo "  Repo variables: SECRET_PREFIX, ARC_RUNNER_LABEL, EKS_CLUSTER, BEADS_ENABLED"
echo ""
echo "To use agents:"
echo "  1. Label any issue with 'agent-developer' to trigger the developer agent"
echo "  2. Watch: gh run list --repo ${FULL_REPO} --workflow agent-developer.yml"
echo "  3. The agent will post a comment and open a PR"
echo ""
echo "To tear down:"
echo "  ./platform/scripts/lightweight-teardown.sh ${GITHUB_ORG} ${REPO_NAME} ${SECRET_PREFIX} ${AWS_REGION}"
echo ""
