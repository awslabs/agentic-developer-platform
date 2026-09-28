#!/bin/bash
set -euo pipefail

# =============================================================================
# ADP — Deploy Everything
# =============================================================================
# Deploys the entire platform end-to-end (11 steps). Requires: AWS CLI,
# Terraform, Node.js, kubectl. Docker builds use CodeBuild (4 Terraform-managed
# projects in platform/infra/modules/codebuild/). Everything else runs directly.
#
# See docs/adp-platform-deployment/deploy-quickstart.md for the phase-by-phase
# guide and troubleshooting.
#
# Usage:
#   ./platform/scripts/deploy-all.sh                            # Deploy all modules
#   ./platform/scripts/deploy-all.sh --gateway-only             # Platform + gateway only
#   ./platform/scripts/deploy-all.sh --agent-context-only       # Platform + agent-context only
#   ./platform/scripts/deploy-all.sh --destroy                  # Tear down everything
#   ./platform/scripts/deploy-all.sh --local                    # Run everything locally (needs Terraform, Docker, Node, kubectl)
#   ./platform/scripts/deploy-all.sh --ci                       # CI mode: validate outputs exist without re-applying
#   ./platform/scripts/deploy-all.sh --skip-broker              # Skip broker Lambda deploy (step 7)
#   ./platform/scripts/deploy-all.sh --skip-admin-bootstrap     # Skip first-admin DB seeding (step 8)
#   ./platform/scripts/deploy-all.sh --skip-webhook-ingress     # Skip webhook-ingress stack (step 9)
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Load deployment config — populates ADP_ACCOUNT_ID, ADP_REGION,
# ADP_ENVIRONMENT, ADP_GITHUB_ORG, etc. Falls back to runtime defaults
# (aws sts get-caller-identity, AWS_REGION env, etc.) when no config
# file is present, so existing self-managed deploys keep working.
# shellcheck source=load-deploy-config.sh
# Retain only an explicit override for a missing factory. The config loader's
# repository-origin fallback is not evidence of a customer's GitHub org.
FACTORY_GITHUB_ORG_OVERRIDE="${ADP_GITHUB_ORG:-}"
source "${SCRIPT_DIR}/load-deploy-config.sh"

AWS_REGION="$ADP_REGION"
export AWS_REGION AWS_DEFAULT_REGION="$AWS_REGION"
ENVIRONMENT="$ADP_ENVIRONMENT"
GATEWAY_ONLY=false
AGENT_FACTORY_ONLY=false
AGENT_CONTEXT_ONLY=false
SKIP_AGENT_CONTEXT=false
AGENT_CONTEXT_ENABLED="${AGENT_CONTEXT_ENABLED:-false}"
SUPERPLANE_ONLY=false
SKIP_SUPERPLANE=false
# Default false, like AGENT_CONTEXT_ENABLED above. The Superplane domain app must not
# deploy unless somebody asks for it: its Terraform is not this unit's (U3 owns it), so
# an environment that has not opted in has nothing here to stand up (Issue #5037).
SUPERPLANE_ENABLED="${SUPERPLANE_ENABLED:-false}"
DESTROY=false
SKIP_FRONTEND=false
SKIP_BROKER=false
SKIP_ADMIN_BOOTSTRAP=false
SKIP_WEBHOOK_INGRESS=false
LOCAL_MODE=false
CI_MODE=false
UPDATE_MODE=false
CONFIRM_DESTRUCTIVE=false

while [ "$#" -gt 0 ]; do
  case "$1" in
    --env) ENVIRONMENT="${2:?--env requires a value}"; shift ;;
    --region) AWS_REGION="${2:?--region requires a value}"; shift ;;
    --gateway-only) GATEWAY_ONLY=true ;;
    --agent-factory-only) AGENT_FACTORY_ONLY=true ;;
    --agent-context-only) AGENT_CONTEXT_ONLY=true ;;
    --skip-agent-context) SKIP_AGENT_CONTEXT=true ;;
    --superplane-only) SUPERPLANE_ONLY=true ;;
    --skip-superplane) SKIP_SUPERPLANE=true ;;
    --destroy) DESTROY=true ;;
    --skip-frontend) SKIP_FRONTEND=true ;;
    --skip-broker) SKIP_BROKER=true ;;
    --skip-admin-bootstrap) SKIP_ADMIN_BOOTSTRAP=true ;;
    --skip-webhook-ingress) SKIP_WEBHOOK_INGRESS=true ;;
    --local) LOCAL_MODE=true ;;
    --ci) CI_MODE=true ;;
    --update) UPDATE_MODE=true ;;
    --confirm-destructive) CONFIRM_DESTRUCTIVE=true ;;
    --help)
      echo "Usage: $0 [OPTIONS]"
      echo ""
      echo "Modes:"
      echo "  (default)              Fresh deploy — stand up the platform from scratch"
      echo "  --update               Update mode — converge an existing deployment to newer code"
      echo "  --destroy              Tear down all infrastructure (LEGACY — prefer undeploy.sh)"
      echo "  --ci                   CI mode: validate outputs exist without re-applying"
      echo ""
      echo "Update-mode flags (only with --update):"
      echo "  --confirm-destructive  Authorize terraform applies that include resource destroys"
      echo ""
      echo "Target: --env <dev|staging|prod> --region <aws-region> (AWS_PROFILE selects account)"
      echo ""
      echo "Scope:"
      echo "  --gateway-only         Platform + gateway only"
      echo "  --agent-factory-only   Platform + agent-factory only"
      echo "  --agent-context-only   Platform + agent-context only"
      echo "  --superplane-only      Platform + superplane domain app only (skips gateway,"
      echo "                         broker, admin bootstrap, webhook-ingress, agent-factory,"
      echo "                         agent-context; implies SUPERPLANE_ENABLED=true)"
      echo ""
      echo "Skip:"
      echo "  --skip-frontend        Skip frontend build and deploy"
      echo "  --skip-broker          Skip broker Lambda deploy"
      echo "  --skip-admin-bootstrap Skip first-admin DB seeding"
      echo "  --skip-webhook-ingress Skip webhook-ingress stack"
      echo "  --skip-agent-context   Skip agent-context even if AGENT_CONTEXT_ENABLED=true"
      echo "  --skip-superplane      Skip superplane even if SUPERPLANE_ENABLED=true"
      echo "  Domain image build jobs are installed from their own module Terraform roots"
      echo ""
      echo "Build:"
      echo "  --local                Use local Docker for image builds (instead of CodeBuild)"
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
export AWS_DEFAULT_REGION="$AWS_REGION" ADP_REGION="$AWS_REGION"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
step() { echo -e "\n${BLUE}━━━ $1 ━━━${NC}\n"; }
ok()   { echo -e "${GREEN}✓ $1${NC}"; }
warn() { echo -e "${YELLOW}⚠ $1${NC}"; }
fail() { echo -e "${RED}✗ $1${NC}"; exit 1; }

if [ "${ADP_ENABLED_DOMAIN_APPS+x}" = x ]; then
  fail "ADP_ENABLED_DOMAIN_APPS is retired; deploy optional build jobs from their domain modules"
fi

# =============================================================================
# Mutual exclusion checks
# =============================================================================
if [ "$UPDATE_MODE" = true ] && [ "$DESTROY" = true ]; then
  fail "--update and --destroy are mutually exclusive"
fi
if [ "$UPDATE_MODE" = true ] && [ "$CI_MODE" = true ]; then
  fail "--update and --ci are mutually exclusive"
fi
_SCOPE_COUNT=0
for _scope in "$GATEWAY_ONLY" "$AGENT_FACTORY_ONLY" "$AGENT_CONTEXT_ONLY"; do
  [ "$_scope" != true ] || _SCOPE_COUNT=$((_SCOPE_COUNT + 1))
done
[ "$_SCOPE_COUNT" -le 1 ] || fail "Choose only one scope flag"

# Prepared releases only enter through release/upgrade.py, which checks the
# source, account, manifest and every artifact before any infrastructure apply.
if [ -n "${ADP_RELEASE_DIR:-}" ]; then
  [ "$UPDATE_MODE" = true ] && [ "$ENVIRONMENT" = dev ] && [ "$AWS_REGION" = us-east-1 ] || fail "Prepared releases require --update --env dev --region us-east-1"
  for flag in "$DESTROY" "$CI_MODE" "$LOCAL_MODE" "$CONFIRM_DESTRUCTIVE" "$GATEWAY_ONLY" "$AGENT_FACTORY_ONLY" "$AGENT_CONTEXT_ONLY" "$SUPERPLANE_ONLY" "$SKIP_FRONTEND" "$SKIP_BROKER" "$SKIP_ADMIN_BOOTSTRAP" "$SKIP_WEBHOOK_INGRESS" "$SKIP_AGENT_CONTEXT" "$SKIP_SUPERPLANE"; do
    [ "$flag" = false ] || fail "Release upgrades require the full deployment and safety gates"
  done
  python3 "$SCRIPT_DIR/release/artifacts.py" verify-prepared --directory "$ADP_RELEASE_DIR"
fi

# =============================================================================
# Helper: terraform_update_apply — plan-gated apply for update mode (§4)
# =============================================================================
# In update mode, every terraform apply is replaced with a plan-first gate that
# refuses to apply if the plan includes resource destroys (unless
# --confirm-destructive was passed). This prevents silent destruction of live
# resources from TF drift.
source "$SCRIPT_DIR/terraform-update.sh"
source "$SCRIPT_DIR/upgrade-scope.sh"
source "$SCRIPT_DIR/gateway-alb-vars.sh"

# =============================================================================
# Preflight
# =============================================================================
step "Preflight checks"

if [ "$UPDATE_MODE" = true ]; then
  # Update mode: partial preflight — skip tool-install checks; keep AWS auth + cluster-reachable
  echo "Running partial preflight (update mode)..."
  command -v aws >/dev/null 2>&1 || fail "aws CLI not found"
  command -v kubectl >/dev/null 2>&1 || fail "kubectl not found"
  command -v terraform >/dev/null 2>&1 || fail "terraform not found"
else
  echo "Running preflight validation..."
  LOCAL_FLAG=""
  [ "$LOCAL_MODE" = true ] && LOCAL_FLAG="--local"
  if [ -f "$SCRIPT_DIR/preflight-check.sh" ]; then
    bash "$SCRIPT_DIR/preflight-check.sh" $LOCAL_FLAG || fail "Preflight checks failed. Fix the issues above and retry."
  else
    warn "preflight-check.sh not found, skipping validation"
  fi
fi

command -v aws >/dev/null 2>&1 || fail "aws CLI not found"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) || fail "AWS CLI not configured"

# When config/deployment.yml pins a specific account_id, fail-fast if the
# operator's creds resolve elsewhere — mirrors preflight's safety check.
if [ -n "$ADP_ACCOUNT_ID" ] && [ "$ADP_ACCOUNT_ID" != "$ACCOUNT_ID" ]; then
  fail "config/deployment.yml says account_id=$ADP_ACCOUNT_ID but caller resolves to $ACCOUNT_ID. Either fix config/deployment.yml or switch AWS_PROFILE."
fi

ok "AWS Account: $ACCOUNT_ID | Region: $AWS_REGION | Env: $ENVIRONMENT"

# ---------------------------------------------------------------------------
# Accept Bedrock marketplace agreements for the Claude models the platform
# invokes. Fresh accounts have none; without them every model call fails with
# AccessDeniedException and the agent-worker misreports it as "no changes
# needed". Idempotent — skips models already enabled.
# ---------------------------------------------------------------------------
# Upgrades may introduce a new runtime default too. Readiness must not be
# skipped merely because an earlier version was already deployed.
if [ "$CI_MODE" = false ] && [ "$DESTROY" = false ]; then
  step "Bedrock model access and first-use registration"
  bash "$SCRIPT_DIR/enable-bedrock-models.sh" --prepare-and-verify || fail "Required Bedrock model access is not ready; runtime deployment has not started."
fi

# ---------------------------------------------------------------------------
# Detect operator's public IP and lock EKS public API to /32 (portable)
# ---------------------------------------------------------------------------
# Anyone cloning this repo can run the script without editing tfvars. The
# detected IP is exported as TF_VAR_eks_public_access_cidrs so Terraform picks
# it up. If the caller already set the env var, respect it.
if [ "$UPDATE_MODE" = false ] && [ -z "${TF_VAR_eks_public_access_cidrs:-}" ]; then
  MY_IP=""
  for url in https://checkip.amazonaws.com https://api.ipify.org https://ifconfig.me; do
    MY_IP=$(curl -fsS --max-time 5 "$url" 2>/dev/null | tr -d '[:space:]' || true)
    [ -n "$MY_IP" ] && break
  done
  if [ -z "$MY_IP" ]; then
    fail "Could not detect your public IP. Set TF_VAR_eks_public_access_cidrs='[\"<ip>/32\"]' manually."
  fi
  export TF_VAR_eks_public_access_cidrs="[\"${MY_IP}/32\"]"
  ok "EKS public API will allow: ${MY_IP}/32 (your current public IP)"
else
  ok "EKS access: preserving existing configuration during upgrades"
fi

REGISTRY="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
STATE_BUCKET="adp-terraform-state-${ACCOUNT_ID}"
LOCK_TABLE="adp-terraform-locks"
EKS_CLUSTER="adp-${ENVIRONMENT}-eks-cluster"

# =============================================================================
# Update mode: precondition checks (§1)
# =============================================================================
if [ "$UPDATE_MODE" = true ]; then
  step "Update mode: precondition checks"

  export UPGRADE_RUN_DIR="${UPGRADE_RUN_DIR:-$(mktemp -d "${TMPDIR:-/tmp}/adp-upgrade-${ACCOUNT_ID}.XXXXXX")}"
  python3 "$SCRIPT_DIR/upgrade-state.py" prepare --directory "$UPGRADE_RUN_DIR" \
    --account "$ACCOUNT_ID" --environment "$ENVIRONMENT" --region "$AWS_REGION" \
    || fail "Cannot safely discover the existing deployment"
  source "$UPGRADE_RUN_DIR/context.env"
  resolve_deploy_scope
  if [ -n "${ADP_RELEASE_DIR:-}" ]; then
    [ "$DEPLOY_AGENT_CONTEXT" = false ] && [ "$SUPERPLANE_ENABLED" = false ] || fail "This release contract does not cover agent-context or superplane"
    [ "$DEPLOY_GATEWAY" = true ] && [ "$DEPLOY_FACTORY" = true ] && [ "$DEPLOY_WEBHOOK" = true ] || fail "Release upgrade requires gateway, factory and webhook ingress"
  fi
  if [ "$DEPLOY_FACTORY" = true ]; then
    python3 "$SCRIPT_DIR/upgrade-state.py" prepare-factory --directory "$UPGRADE_RUN_DIR" \
      --region "$AWS_REGION" --github-org "$FACTORY_GITHUB_ORG_OVERRIDE" \
      || fail "Cannot prepare the required agent-factory module"
  fi
  if [ "$DEPLOY_AGENT_CONTEXT" = true ]; then
    CONTEXT_CONFIG="$ROOT_DIR/modules/agent-context/config.local.env"
    [ -f "$CONTEXT_CONFIG" ] || fail "Existing agent-context requires its original config.local.env (or use --skip-agent-context)"
    ( source "$ROOT_DIR/modules/agent-context/config.env"; source "$CONTEXT_CONFIG";
      [ "$CLUSTER_NAME" = "$EKS_CLUSTER" ] && [ "$AWS_REGION" = "$ADP_REGION" ] ) \
      || fail "Agent-context config does not match the upgrade target"
  fi
  if [ "$UPGRADE_NEEDS_EKS_ACCESS" = true ]; then
    python3 "$SCRIPT_DIR/upgrade-state.py" open-access --directory "$UPGRADE_RUN_DIR" --region "$AWS_REGION"
  fi
  ok "Upgrade evidence and preserved configuration: $UPGRADE_RUN_DIR"

  # Bind all checks and subsequent kubectl calls to the verified target.
  export KUBECONFIG="${KUBECONFIG:-$(mktemp "${TMPDIR:-/tmp}/adp-${ACCOUNT_ID}-kubeconfig.XXXXXX")}"
  aws eks update-kubeconfig --name "$EKS_CLUSTER" --region "$AWS_REGION" \
    --kubeconfig "$KUBECONFIG" >/dev/null || fail "Cannot configure target cluster"

  # 3. Gateway namespace must exist (indicates prior deploy).
  if [ "$DEPLOY_GATEWAY" = true ]; then
    kubectl get namespace adp-gateway --request-timeout=30s &>/dev/null \
      || fail "Cannot reach the existing gateway namespace"
  fi

  NETWORK_WAS_ENABLED=$(python3 "$SCRIPT_DIR/upgrade-network.py" enabled)
  if [ "$NETWORK_WAS_ENABLED" = true ]; then
    # Updating code must not turn off enforcement already active in the target.
    python3 - "$UPGRADE_RUN_DIR/platform.tfvars.json" <<'PY'
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
data = json.loads(path.read_text())
data["enable_network_policy_controller"] = True
path.write_text(json.dumps(data))
PY
  fi
  ok "Preconditions met; existing module scope resolved"
else
  resolve_deploy_scope
fi
# Pin fresh installs and updates to the source being deployed. Reusing :latest
# can leave an old Ready pod serving while migration verification reports success.
SOURCE_SHA=$(git -C "$ROOT_DIR" rev-parse HEAD) || fail "Cannot pin deployment to a source commit"
export IMAGE_TAG="$SOURCE_SHA"
ok "Image tag for this deployment: $IMAGE_TAG"
GATEWAY_IMAGE="${ADP_RELEASE_GATEWAY_IMAGE:-${REGISTRY}/adp-gateway:${IMAGE_TAG}}"
GATEWAY_UPDATE_VAR_FILE="$ROOT_DIR/environments/$ENVIRONMENT/modules/gateway.tfvars"
if [ "$UPDATE_MODE" = true ] && [ "$DEPLOY_GATEWAY" = true ]; then
  GATEWAY_UPDATE_VAR_FILE=$(terraform_update_var_file \
    "$GATEWAY_UPDATE_VAR_FILE" \
    "${ADP_GATEWAY_UPDATE_TFVARS:-}" "$ACCOUNT_ID") \
    || fail "Gateway update needs target-specific tfvars"
  ok "Gateway update tfvars: $GATEWAY_UPDATE_VAR_FILE"
fi

# =============================================================================
# Helper: refresh AWS credentials (cross-account / short-lived sessions)
# =============================================================================
# Issue #3424: In ADP-managed mode the gateway vault may issue short-lived STS
# credentials. This helper re-runs the assume to get fresh creds. Called before
# each step to avoid mid-operation expiry. No-op in self-managed mode (direct
# creds don't expire within a single deploy run).
_ASSUME_SCRIPT="${SCRIPT_DIR}/assume-customer-creds.py"
_CRED_REFRESH_INTERVAL=300  # seconds — refresh if creds are older than this
_CRED_LAST_REFRESH=${EPOCHSECONDS:-$(date +%s)}
refresh_credentials() {
  # Skip if not in cross-account mode or assume script missing
  if [ -z "${ADP_CUSTOMER_ACCOUNT_ID:-}" ] || [ ! -x "$_ASSUME_SCRIPT" ]; then
    return 0
  fi
  local NOW=${EPOCHSECONDS:-$(date +%s)}
  local ELAPSED=$((NOW - _CRED_LAST_REFRESH))
  if [ "$ELAPSED" -lt "$_CRED_REFRESH_INTERVAL" ] && [ "${1:-}" != "--force" ]; then
    return 0
  fi
  local _OUTPUT
  _OUTPUT=$("$_ASSUME_SCRIPT" 2>&1 >/tmp/.deploy-creds.$$) || {
    warn "Credential refresh failed: $_OUTPUT"
    rm -f /tmp/.deploy-creds.$$
    return 1
  }
  if [ -s /tmp/.deploy-creds.$$ ]; then
    # shellcheck disable=SC1090
    . /tmp/.deploy-creds.$$
  fi
  rm -f /tmp/.deploy-creds.$$
  _CRED_LAST_REFRESH=${EPOCHSECONDS:-$(date +%s)}
}

# =============================================================================
# (Removed) Helper: check if the webhook-secrets KMS alias exists
# =============================================================================
# Issue #3789: webhook_kms_grant_value() and enable_webhook_secrets_kms_grant
# are deleted. The webhook-secrets CMK now lives in platform infra (Step 2),
# so it always exists before gateway applies (Step 3). The gateway grant is
# unconditional — no flag dance needed.

# =============================================================================
# Helper: run a CodeBuild job (project must already exist via Terraform)
# =============================================================================
# Uses codebuild-run.sh which uploads source to a per-build-unique S3 key
# and passes --source-location-override, eliminating the shared-key race.
run_codebuild() {
  if [ -n "${ADP_RELEASE_DIR:-}" ]; then
    ok "Using verified release image; no build required for $1"
    return 0
  fi
  local PROJECT_NAME="$1"
  local BUILDSPEC_FILE="$2"  # unused — buildspec is baked into the project

  # Verify the project exists
  local PROJECT_EXISTS
  PROJECT_EXISTS=$(aws codebuild batch-get-projects --names "$PROJECT_NAME" --region "$AWS_REGION" \
    --query 'projects | length(@)' --output text 2>/dev/null || echo "0")
  if [ "$PROJECT_EXISTS" -eq 0 ]; then
    fail "CodeBuild project '$PROJECT_NAME' not found. Run 'terraform apply' in platform/infra/ first."
  fi

  # Delegate to codebuild-run.sh for per-build isolated source upload + start + poll
  # Forward the full source SHA expected by the immutable publisher.
  local _CB_IMAGE_TAG_OVERRIDE=""
  if [ -n "${IMAGE_TAG:-}" ]; then
    _CB_IMAGE_TAG_OVERRIDE="name=IMAGE_TAG,value=${IMAGE_TAG},type=PLAINTEXT"
  fi
  # shellcheck disable=SC2086
  STATE_BUCKET="$STATE_BUCKET" AWS_REGION="$AWS_REGION" SOURCE_SHA="$(git -C "$ROOT_DIR" rev-parse HEAD 2>/dev/null || echo unknown)" \
    bash "$SCRIPT_DIR/codebuild-run.sh" "$PROJECT_NAME" \
      "name=AWS_REGION,value=$AWS_REGION" \
      "name=ENVIRONMENT,value=$ENVIRONMENT" \
      "name=ACCOUNT_ID,value=$ACCOUNT_ID" \
      "name=REGISTRY,value=$REGISTRY" \
      "name=STATE_BUCKET,value=$STATE_BUCKET" \
      "name=EKS_CLUSTER,value=$EKS_CLUSTER" \
      ${_CB_IMAGE_TAG_OVERRIDE:+"$_CB_IMAGE_TAG_OVERRIDE"} \
  || fail "Build failed: $PROJECT_NAME"
  ok "Build succeeded: $PROJECT_NAME"
}

# =============================================================================
# DESTROY — Tear down all infrastructure in reverse deploy order
# =============================================================================
# Uses the same shared scripts as the per-module destroy workflows:
#   - delete-ingress-and-wait.sh  (ALB cleanup before gateway destroy)
#   - empty-s3-buckets.sh         (non-empty buckets block terraform destroy)
#   - force-delete-secrets.sh     (avoid 7-day collision on re-deploy)
#
# Order: superplane → agent-context → webhook-ingress → agent-factory → gateway → platform
# State backend (S3 + DynamoDB) is NOT destroyed — use bootstrap-destroy.sh.
# GitHub App secrets (adp/gh-app-*) are NOT touched — survive by design.
# =============================================================================
if [ "$DESTROY" = true ]; then
  step "Destroying all infrastructure"
  echo "This will destroy ALL ADP infrastructure in $ENVIRONMENT."
  echo ""
  echo "Destroy order: superplane → agent-context → webhook-ingress → agent-factory → gateway → platform"
  echo "State backend and GitHub App secrets will NOT be deleted."
  echo ""
  echo "Type 'yes' to proceed:"
  read -r confirm
  [ "$confirm" = "yes" ] || { echo "Aborted."; exit 0; }

  # Configure kubectl for K8s cleanup steps
  export KUBECONFIG="${KUBECONFIG:-$(mktemp "${TMPDIR:-/tmp}/adp-${ACCOUNT_ID}-kubeconfig.XXXXXX")}"
  if command -v kubectl >/dev/null 2>&1; then
    aws eks update-kubeconfig --name "$EKS_CLUSTER" --region "$AWS_REGION" --kubeconfig "$KUBECONFIG" 2>/dev/null || true
  fi

  # Use the same Superplane teardown as undeploy.sh and the undeploy workflow.
  # A failed domain teardown must stop before removing its platform dependencies.
  step "Destroy 1/6: Superplane"
  source "$SCRIPT_DIR/undeploy-phases.sh"
  phase_superplane || fail "Superplane teardown failed; leaving its dependencies intact"

  # -------------------------------------------------------------------------
  # 2. Agent Context
  # -------------------------------------------------------------------------
  step "Destroy 2/6: Agent Context"
  if [ -d "$ROOT_DIR/modules/agent-context/terraform" ]; then
    kubectl delete namespace agent-context --wait=true --timeout=120s 2>/dev/null || true

    # Empty S3 buckets (versioned — terraform cannot delete non-empty)
    AC_BUCKETS=""
    for pattern in "agent-context-platform-data-"; do
      FOUND=$(aws s3api list-buckets --query "Buckets[?starts_with(Name,'${pattern}')].Name" --output text 2>/dev/null || echo "")
      [ -n "$FOUND" ] && [ "$FOUND" != "None" ] && AC_BUCKETS="$AC_BUCKETS $FOUND"
    done
    [ -n "$AC_BUCKETS" ] && bash "$SCRIPT_DIR/empty-s3-buckets.sh" $AC_BUCKETS

    cd "$ROOT_DIR/modules/agent-context/terraform"
    terraform init -backend-config="../../../environments/$ENVIRONMENT/modules/agent-context-backend.tfvars" -input=false 2>/dev/null || true
    terraform destroy -var-file="../../../environments/$ENVIRONMENT/modules/agent-context.tfvars" -auto-approve || true
    ok "Agent Context destroyed"
  else
    ok "Agent Context: not present, skipping"
  fi

  # -------------------------------------------------------------------------
  # 3. Webhook Ingress (KEDA + Lambda + SQS + API GW + DynamoDB + WAFv2 + KMS)
  # -------------------------------------------------------------------------
  step "Destroy 3/6: Webhook Ingress"
  if [ -f "$ROOT_DIR/modules/agent-factory/webhook-ingress/infra/terraform.tfvars" ]; then
    # Clean up K8s KEDA resources before TF destroy
    kubectl delete scaledjobs --all -n adp-agents 2>/dev/null || true
    kubectl delete namespace adp-agents --wait=true --timeout=120s 2>/dev/null || true

    # Clean up Lambda artifacts from S3
    aws s3 rm "s3://${STATE_BUCKET}/lambda-artifacts/webhook-ingress/" --recursive 2>/dev/null || true

    cd "$ROOT_DIR/modules/agent-factory/webhook-ingress/infra"
    terraform init -backend-config="../../../../environments/$ENVIRONMENT/modules/webhook-ingress-backend.tfvars" -input=false 2>/dev/null || true
    terraform destroy -var-file=terraform.tfvars -auto-approve || true
    ok "Webhook Ingress destroyed"
  else
    ok "Webhook Ingress: not configured, skipping"
  fi

  # -------------------------------------------------------------------------
  # 4. Agent Factory
  # -------------------------------------------------------------------------
  step "Destroy 4/6: Agent Factory"
  if [ -f "$ROOT_DIR/modules/agent-factory/infra/terraform.tfvars" ]; then
    # Clean up K8s resources
    kubectl delete scaledjobs --all -n adp-gateway-agents 2>/dev/null || true
    kubectl delete namespace adp-gateway-agents --wait=true --timeout=120s 2>/dev/null || true
    kubectl delete namespace arc-runners --wait=true --timeout=120s 2>/dev/null || true

    # Empty S3 buckets (beads state, chat artifacts)
    FACTORY_BUCKETS=""
    for pattern in "adp-${ENVIRONMENT}-agent-beads-state-" "adp-${ENVIRONMENT}-chat-artifacts-"; do
      FOUND=$(aws s3api list-buckets --query "Buckets[?starts_with(Name,'${pattern}')].Name" --output text 2>/dev/null || echo "")
      [ -n "$FOUND" ] && [ "$FOUND" != "None" ] && FACTORY_BUCKETS="$FACTORY_BUCKETS $FOUND"
    done
    [ -n "$FACTORY_BUCKETS" ] && bash "$SCRIPT_DIR/empty-s3-buckets.sh" $FACTORY_BUCKETS

    cd "$ROOT_DIR/modules/agent-factory/infra"
    terraform init -backend-config="../../../environments/$ENVIRONMENT/modules/agent-factory-backend.tfvars" -input=false 2>/dev/null || true
    terraform destroy -var-file=terraform.tfvars -auto-approve || true
    ok "Agent Factory destroyed"
  else
    ok "Agent Factory: not configured, skipping"
  fi

  # -------------------------------------------------------------------------
  # 5. Gateway (most complex — ALB, S3, Secrets, CloudFront cleanup first)
  # -------------------------------------------------------------------------
  step "Destroy 5/6: Gateway"

  # 4a. Delete Ingress and wait for ALB to be removed by the controller
  echo "Cleaning up Ingress resources and ALBs..."
  bash "$SCRIPT_DIR/delete-ingress-and-wait.sh" || true

  # 4b. Delete remaining K8s gateway resources
  kubectl delete namespace adp-gateway --wait=true --timeout=120s 2>/dev/null || true

  # 4c. Empty S3 buckets (frontend, etc.)
  GW_BUCKETS=""
  FRONTEND_BUCKET=$(aws ssm get-parameter --name "/adp/$ENVIRONMENT/gateway/frontend-bucket" \
    --query "Parameter.Value" --output text --region "$AWS_REGION" 2>/dev/null || echo "")
  [ -n "$FRONTEND_BUCKET" ] && [ "$FRONTEND_BUCKET" != "None" ] && GW_BUCKETS="$FRONTEND_BUCKET"
  for pattern in "bedrockgw-${ENVIRONMENT}-frontend-"; do
    FOUND=$(aws s3api list-buckets --query "Buckets[?starts_with(Name,'${pattern}')].Name" --output text 2>/dev/null || echo "")
    [ -n "$FOUND" ] && [ "$FOUND" != "None" ] && GW_BUCKETS="$GW_BUCKETS $FOUND"
  done
  [ -n "$GW_BUCKETS" ] && bash "$SCRIPT_DIR/empty-s3-buckets.sh" $GW_BUCKETS

  # 4d. Force-delete Secrets Manager secrets (avoid 7-day collision)
  bash "$SCRIPT_DIR/force-delete-secrets.sh" \
    "bedrockgw-${ENVIRONMENT}-" \
    "adp/${ENVIRONMENT}/gateway/test-" || true

  # 4e. Disable CloudFront distribution (two-phase delete)
  DIST_ID=$(aws ssm get-parameter --name "/adp/$ENVIRONMENT/gateway/cloudfront-id" \
    --query "Parameter.Value" --output text --region "$AWS_REGION" 2>/dev/null || echo "")
  if [ -n "$DIST_ID" ] && [ "$DIST_ID" != "None" ]; then
    echo "Disabling CloudFront distribution $DIST_ID..."
    DIST_CONFIG=$(aws cloudfront get-distribution-config --id "$DIST_ID" 2>/dev/null || echo "")
    if [ -n "$DIST_CONFIG" ]; then
      ENABLED=$(echo "$DIST_CONFIG" | python3 -c "import sys,json; d=json.load(sys.stdin); print(str(d['DistributionConfig']['Enabled']).lower())" 2>/dev/null || echo "false")
      if [ "$ENABLED" = "true" ]; then
        ETAG=$(echo "$DIST_CONFIG" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['ETag'])")
        echo "$DIST_CONFIG" | python3 -c "
import sys, json
d = json.load(sys.stdin)
config = d['DistributionConfig']
config['Enabled'] = False
print(json.dumps(config))
" > /tmp/cf-disable-config.json
        aws cloudfront update-distribution --id "$DIST_ID" --if-match "$ETAG" \
          --distribution-config "file:///tmp/cf-disable-config.json" > /dev/null 2>&1 || true
        echo "Waiting for CloudFront to deploy disabled state (up to 15 min)..."
        aws cloudfront wait distribution-deployed --id "$DIST_ID" 2>/dev/null || {
          warn "CloudFront wait timed out. terraform destroy may retry."
        }
        rm -f /tmp/cf-disable-config.json
        ok "CloudFront $DIST_ID disabled"
      else
        ok "CloudFront $DIST_ID already disabled"
      fi
    fi
  fi

  # 4f. Terraform destroy
  cd "$ROOT_DIR/modules/gateway/infra"
  terraform init -backend-config="../../../environments/$ENVIRONMENT/modules/gateway-backend.tfvars" -input=false -reconfigure 2>/dev/null || true
  terraform destroy -var-file="../../../environments/$ENVIRONMENT/modules/gateway.tfvars" -auto-approve || true

  # 4g. Clean up SSM parameters
  for param in "/adp/$ENVIRONMENT/gateway/frontend-bucket" \
               "/adp/$ENVIRONMENT/gateway/cloudfront-id" \
               "/adp/$ENVIRONMENT/gateway/cloudfront-domain" \
               "/adp/$ENVIRONMENT/gateway/internal-alb-arn" \
               "/adp/$ENVIRONMENT/gateway/internal-alb-dns" \
               "/adp/$ENVIRONMENT/gateway/internal-alb-security-group-ids"; do
    aws ssm delete-parameter --name "$param" --region "$AWS_REGION" 2>/dev/null || true
  done
  ok "Gateway destroyed"

  # -------------------------------------------------------------------------
  # 6. Platform (last — EKS, VPC, ECR, IAM)
  # -------------------------------------------------------------------------
  step "Destroy 6/6: Platform"

  # Clean up K8s system namespaces before cluster destroy
  kubectl delete namespace arc-systems --wait=true --timeout=120s 2>/dev/null || true
  kubectl delete namespace keda --wait=true --timeout=120s 2>/dev/null || true

  # Clean up orphaned ENIs
  VPC_ID=$(aws eks describe-cluster --name "$EKS_CLUSTER" --region "$AWS_REGION" \
    --query 'cluster.resourcesVpcConfig.vpcId' --output text 2>/dev/null || echo "")
  if [ -n "$VPC_ID" ] && [ "$VPC_ID" != "None" ]; then
    ORPHAN_ENIS=$(aws ec2 describe-network-interfaces --region "$AWS_REGION" \
      --filters "Name=vpc-id,Values=$VPC_ID" "Name=status,Values=available" \
      --query 'NetworkInterfaces[].NetworkInterfaceId' --output text 2>/dev/null || echo "")
    for ENI in $ORPHAN_ENIS; do
      echo "  Deleting orphaned ENI: $ENI"
      aws ec2 delete-network-interface --network-interface-id "$ENI" --region "$AWS_REGION" 2>/dev/null || true
    done
  fi

  cd "$ROOT_DIR/platform/infra"
  terraform init -backend-config="../../environments/$ENVIRONMENT/backend.tfvars" -input=false -reconfigure 2>/dev/null || true
  terraform destroy -var-file="../../environments/$ENVIRONMENT/platform.tfvars" -auto-approve || true

  # Clean up any leftover retired CodeBuild projects
  for p in "adp-${ENVIRONMENT}-frontend-build" "adp-${ENVIRONMENT}-platform-infra" "adp-${ENVIRONMENT}-gateway-deploy" "adp-${ENVIRONMENT}-gateway-infra" "adp-${ENVIRONMENT}-gateway-alb-wire" "adp-${ENVIRONMENT}-agent-factory-infra" "adp-${ENVIRONMENT}-agent-context-infra" "adp-${ENVIRONMENT}-agent-context-deploy" "adp-${ENVIRONMENT}-destroy"; do
    aws codebuild delete-project --name "$p" --region "$AWS_REGION" 2>/dev/null || true
  done
  ok "Platform destroyed"

  # -------------------------------------------------------------------------
  # Summary
  # -------------------------------------------------------------------------
  step "Destroy complete"
  echo "All module infrastructure has been destroyed."
  echo ""
  echo "Surviving resources (by design):"
  echo "  - Terraform state backend: S3 $STATE_BUCKET + DynamoDB $LOCK_TABLE"
  echo "  - GitHub App secrets: adp/gh-app-* in Secrets Manager"
  echo ""
  echo "To destroy the state backend (only after verifying all modules are gone):"
  echo "  $SCRIPT_DIR/bootstrap-destroy.sh"
  exit 0
fi

# =============================================================================
# CI mode: validate module outputs exist without re-applying
# =============================================================================
if [ "$CI_MODE" = true ]; then
  step "CI Validation Mode"
  echo "Checking that Terraform outputs exist for each module."
  echo "If any are missing, run the matching GitHub Actions workflow."
  echo ""
  CI_FAILURES=0
  GH_REPO_URL="https://github.com/$(git remote get-url origin 2>/dev/null | sed 's|.*github.com[:/]||;s|\.git$||' || echo 'UNKNOWN')"

  # Platform
  ci_check_module() {
    local MODULE_NAME="$1"
    local STATE_KEY="$2"
    local WORKFLOW_FILE="$3"
    echo -n "  $MODULE_NAME: "
    if aws s3 cp "s3://${STATE_BUCKET}/${STATE_KEY}" /tmp/ci-check-state.json --region "$AWS_REGION" >/dev/null 2>&1; then
      OUTPUTS=$(python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d.get('outputs',{})))" < /tmp/ci-check-state.json 2>/dev/null || echo "0")
      rm -f /tmp/ci-check-state.json
      if [ "$OUTPUTS" -gt 0 ]; then
        ok "$OUTPUTS output(s) found"
      else
        warn "State exists but 0 outputs. Run: $GH_REPO_URL/actions/workflows/$WORKFLOW_FILE"
        CI_FAILURES=$((CI_FAILURES + 1))
      fi
    else
      warn "No state found. Run: $GH_REPO_URL/actions/workflows/$WORKFLOW_FILE"
      CI_FAILURES=$((CI_FAILURES + 1))
    fi
  }

  ci_check_module "platform"      "${ENVIRONMENT}/platform/terraform.tfstate"             "platform-infra-apply.yml"
  if [ "$AGENT_FACTORY_ONLY" = false ] && [ "$AGENT_CONTEXT_ONLY" = false ] && [ "$SUPERPLANE_ONLY" = false ]; then
    ci_check_module "gateway"       "${ENVIRONMENT}/modules/gateway/terraform.tfstate"      "gateway-infra-apply.yml"
  fi
  if [ "$GATEWAY_ONLY" = false ] && [ "$AGENT_CONTEXT_ONLY" = false ] && [ "$SUPERPLANE_ONLY" = false ]; then
    ci_check_module "agent-factory" "${ENVIRONMENT}/modules/agent-factory/terraform.tfstate" "agent-factory-infra-apply.yml"
  fi
  if [ "$AGENT_CONTEXT_ENABLED" = true ] || [ "$AGENT_CONTEXT_ONLY" = true ]; then
    ci_check_module "agent-context" "${ENVIRONMENT}/modules/agent-context/terraform.tfstate" "agent-context-infra-apply.yml"
  fi

  # Also check EKS cluster directly
  echo ""
  echo -n "  EKS cluster: "
  EKS_STATUS=$(aws eks describe-cluster --name "$EKS_CLUSTER" --query 'cluster.status' --output text --region "$AWS_REGION" 2>/dev/null || echo "NOT_FOUND")
  if [ "$EKS_STATUS" = "ACTIVE" ]; then
    ok "ACTIVE"
  else
    warn "$EKS_STATUS — Run: $GH_REPO_URL/actions/workflows/platform-infra-apply.yml"
    CI_FAILURES=$((CI_FAILURES + 1))
  fi

  echo ""
  if [ "$CI_FAILURES" -gt 0 ]; then
    fail "$CI_FAILURES module(s) missing outputs. Run the listed GitHub Actions workflows first."
  fi
  ok "All modules have outputs. Infrastructure is managed via GitHub Actions CI."
  exit 0
fi

# =============================================================================
# Step 1: Bootstrap (always local — chicken-and-egg)
# =============================================================================
refresh_credentials
if [ "$UPDATE_MODE" = true ]; then
  step "Step 1/12: Bootstrap (skipped — update mode)"
  ok "State bucket verified in preconditions: $STATE_BUCKET"
else
  step "Step 1/12: Bootstrap Terraform state backend"

  if aws s3api head-bucket --bucket "$STATE_BUCKET" 2>/dev/null; then
    ok "State bucket exists: $STATE_BUCKET"
  else
    echo "Creating S3 bucket and DynamoDB table..."
    aws s3api create-bucket --bucket "$STATE_BUCKET" --region "$AWS_REGION" \
      $([ "$AWS_REGION" != "us-east-1" ] && echo "--create-bucket-configuration LocationConstraint=$AWS_REGION") > /dev/null 2>&1
    aws s3api put-bucket-versioning --bucket "$STATE_BUCKET" --versioning-configuration Status=Enabled
    aws s3api put-bucket-encryption --bucket "$STATE_BUCKET" \
      --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
    aws s3api put-public-access-block --bucket "$STATE_BUCKET" \
      --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
    ok "S3 bucket created: $STATE_BUCKET"
  fi

  if ! aws dynamodb describe-table --table-name "$LOCK_TABLE" --region "$AWS_REGION" > /dev/null 2>&1; then
    aws dynamodb create-table --table-name "$LOCK_TABLE" \
      --attribute-definitions AttributeName=LockID,AttributeType=S \
      --key-schema AttributeName=LockID,KeyType=HASH \
      --billing-mode PAY_PER_REQUEST --region "$AWS_REGION" > /dev/null
    aws dynamodb wait table-exists --table-name "$LOCK_TABLE" --region "$AWS_REGION"
    ok "DynamoDB table created: $LOCK_TABLE"
  else
    ok "DynamoDB table exists: $LOCK_TABLE"
  fi

fi

# Backend configuration is needed for upgrades from a clean checkout too.
python3 "$SCRIPT_DIR/prepare-backends.py" "$ROOT_DIR/environments/$ENVIRONMENT" "$ACCOUNT_ID"
ok "Environment backend configs updated"

if [ "$UPDATE_MODE" = true ]; then
  step "Migrate legacy shared-key ownership"
  python3 "$SCRIPT_DIR/upgrade-migrations.py" --root "$ROOT_DIR" --directory "$UPGRADE_RUN_DIR"
fi

# =============================================================================
# Upload source for CodeBuild docker-build steps
# =============================================================================
# The 4 docker-build CodeBuild projects (gateway-build, chat-agent,
# agent-gateway, arc-runner) are Terraform-managed. Their buildspecs are
# checked into codebuild/bs-*.yml. All other work (terraform apply, npm build,
# kubectl apply) now runs directly — no CodeBuild needed.
# =============================================================================

refresh_credentials
# =============================================================================
# Step 2: Platform infra
# =============================================================================
step "Step 2/12: Deploy shared platform (VPC, EKS, ECR, IAM)"

# Platform infra runs directly (Terraform + kubectl) — no CodeBuild needed.
cd "$ROOT_DIR/platform/infra"
terraform init -backend-config="../../environments/$ENVIRONMENT/backend.tfvars" -input=false -reconfigure
if [ "$UPDATE_MODE" = true ]; then
  PLATFORM_FIRST_ARGS=()
  if [ "$NETWORK_WAS_ENABLED" = false ]; then
    PLATFORM_FIRST_ARGS+=(-var enable_network_policy_controller=false)
    ok "Deferring network-policy activation until webhook egress policies are installed"
  fi
  terraform_update_apply "platform" "../../environments/$ENVIRONMENT/platform.tfvars" ${PLATFORM_FIRST_ARGS[@]+"${PLATFORM_FIRST_ARGS[@]}"}
else
  terraform apply -var-file="../../environments/$ENVIRONMENT/platform.tfvars" -auto-approve
  ok "Platform deployed"
fi

# Configure kubectl (needed for k8s steps — local or CodeBuild deploy step)
export KUBECONFIG="${KUBECONFIG:-$(mktemp "${TMPDIR:-/tmp}/adp-${ACCOUNT_ID}-kubeconfig.XXXXXX")}"
if command -v kubectl >/dev/null 2>&1; then
  aws eks update-kubeconfig --name "$EKS_CLUSTER" --region "$AWS_REGION" --kubeconfig "$KUBECONFIG" 2>/dev/null || true
fi

refresh_credentials
# =============================================================================
# Step 3: Gateway infra
# =============================================================================
step "Step 3/12: Deploy gateway infrastructure"

if [ "$DEPLOY_GATEWAY" = false ]; then
  echo "Skipping gateway infra (scope exclusion)"
  ok "Skipped"
else
  # Ensure the GitHub OAuth secret exists when the auth broker is enabled.
  # gateway/infra reads adp/$ENVIRONMENT/cognito/github-oauth-credentials via a
  # data source at PLAN time — a missing secret aborts the apply. On fresh
  # accounts the secret won't exist yet (it's provisioned during GitHub App
  # setup), so we create a valid-schema placeholder that lets terraform proceed.
  # Real values are provisioned later by register-github-app or the UI flow.
  if [ "$UPDATE_MODE" = false ] && grep -qE '^\s*enable_github_auth_broker\s*=\s*true' \
       "$ROOT_DIR/environments/$ENVIRONMENT/modules/gateway.tfvars" 2>/dev/null; then
    OAUTH_SECRET="adp/${ENVIRONMENT}/cognito/github-oauth-credentials"
    if ! aws secretsmanager describe-secret --secret-id "$OAUTH_SECRET" \
           --region "$AWS_REGION" &>/dev/null; then
      echo "Secret '$OAUTH_SECRET' not found — creating placeholder for terraform plan..."
      aws secretsmanager create-secret \
        --name "$OAUTH_SECRET" \
        --description "GitHub OAuth credentials for ADP auth broker (placeholder — replace with real values during GitHub App setup)" \
        --secret-string '{"client_id":"PLACEHOLDER_AWAITING_GITHUB_APP_SETUP","client_secret":"PLACEHOLDER_AWAITING_GITHUB_APP_SETUP"}' \
        --region "$AWS_REGION" > /dev/null
      warn "Created placeholder secret '$OAUTH_SECRET'. GitHub login will not work until real OAuth credentials are provisioned (Settings → Connections → 'Set up GitHub App')."
    else
      ok "GitHub OAuth secret exists: $OAUTH_SECRET"
    fi
  fi

  # NOTE: The psycopg2/pyjwt Lambda layers are built automatically by the
  # gateway terraform module (null_resource.build_*_layer → CodeBuild), so we no
  # longer build them here — that path now works for stage-by-stage applies and
  # CI too, not just this script. See modules/gateway/infra/main.tf.

  # Freeze the old pricing writer before Terraform changes either Lambda.
  python3 "$ROOT_DIR/modules/gateway/scripts/pricing-rollout.py" quiesce \
    --account-id "$ACCOUNT_ID" --environment "$ENVIRONMENT" --region "$AWS_REGION" \
    || fail "Could not quiesce pricing refresh before the gateway update"

  # Gateway infra runs directly — no CodeBuild needed.
  cd "$ROOT_DIR/modules/gateway/infra"
  terraform init -backend-config="../../../environments/$ENVIRONMENT/modules/gateway-backend.tfvars" -input=false -reconfigure
  # Issue #3789: enable_webhook_secrets_kms_grant removed — the CMK now lives in
  # platform infra (applied Step 2) so the gateway grant is unconditional.
  if [ "$UPDATE_MODE" = true ]; then
    # Discover any legacy uncached ALBs before the first Terraform pass.
    ENVIRONMENT="$ENVIRONMENT" AWS_REGION="$AWS_REGION" bash "$SCRIPT_DIR/wire-gateway-alb.sh" \
      || fail "Cannot discover existing gateway load balancers"
    gateway_alb_vars
    terraform_update_apply "gateway" "$GATEWAY_UPDATE_VAR_FILE" "${GATEWAY_ALB_ARGS[@]}"
  else
    terraform apply -var-file="../../../environments/$ENVIRONMENT/modules/gateway.tfvars" \
      -auto-approve
    ok "Gateway infrastructure deployed"
  fi
fi

refresh_credentials
# =============================================================================
# Step 4: Build + deploy gateway
# =============================================================================
step "Step 4/12: Build and deploy gateway"

if [ "$DEPLOY_GATEWAY" = false ]; then
  echo "Skipping gateway deploy (scope exclusion)"
  ok "Skipped"
else
  # Migrations run after rollout on Ready replicas of this exact release.
  # --- Docker build: use CodeBuild (needs privileged mode) or local Docker ---
  if [ "$LOCAL_MODE" = true ] && docker info &>/dev/null 2>&1; then
    SOURCE_SHA="$SOURCE_SHA" REGISTRY="$REGISTRY" AWS_REGION="$AWS_REGION" \
      bash "$ROOT_DIR/platform/scripts/publish-local-image.sh" adp-gateway
  else
    # Docker build via CodeBuild (Terraform-managed project)
    run_codebuild "adp-${ENVIRONMENT}-gateway-build" "codebuild/bs-gateway-build.yml"
  fi
  GATEWAY_IMAGE=$(python3 "$ROOT_DIR/platform/scripts/resolve-ecr-image.py" "$GATEWAY_IMAGE") \
    || fail "Gateway release digest could not be verified"

  # --- K8s deploy: runs directly (no CodeBuild needed) ---
  cd "$ROOT_DIR/modules/gateway/infra"
  DB_HOST=$(terraform output -raw rds_endpoint 2>/dev/null | sed 's/:5432//' || echo "localhost")
  DB_NAME=$(terraform output -raw rds_database_name 2>/dev/null || echo "bedrockgateway")
  DB_USER="bgadmin"
  # Redis is a list of endpoint objects, so -raw silently fell back to localhost.
  REDIS_HOST=$(terraform output -json redis_endpoint | python3 -c '
import json, sys
value = json.load(sys.stdin)
print(value[0]["address"] if isinstance(value, list) and value else value or "localhost")
') || fail "Cannot resolve Redis endpoint from Terraform outputs"
  REDIS_PORT=$(terraform output -raw redis_port 2>/dev/null || echo "6379")
  # #4342: ElastiCache IAM auth needs the provisioned user name and the
  # replication group id (the connect token is signed against the group id, not
  # the endpoint host).
  REDIS_IAM_USERNAME=$(terraform output -raw redis_iam_username 2>/dev/null || echo "")
  REDIS_CACHE_NAME=$(terraform output -raw redis_cache_name 2>/dev/null || echo "")
  COGNITO_USER_POOL_ID=$(terraform output -raw cognito_user_pool_id 2>/dev/null || echo "")
  COGNITO_CLIENT_ID=$(terraform output -raw cognito_user_pool_client_id 2>/dev/null || echo "")
  COGNITO_DOMAIN=$(terraform output -raw cognito_domain 2>/dev/null || echo "")
  CF_DOMAIN=$(terraform output -raw frontend_cloudfront_domain_name 2>/dev/null || echo "")
  # The user-facing origin. Published by gateway-infra as the custom domain when
  # one is configured; falls back to the distribution default so this is a no-op
  # for deployments without an alias. Used for BG_GATEWAY_BASE_URL — which builds
  # the GitHub App Setup URL sent to GitHub and the magic links sent to users —
  # and for CORS.
  FRONTEND_URL=$(aws ssm get-parameter --name "/adp/$ENVIRONMENT/gateway/frontend-url" \
    --query "Parameter.Value" --output text --region "$AWS_REGION" 2>/dev/null || echo "")
  [ -n "$FRONTEND_URL" ] && [ "$FRONTEND_URL" != "None" ] || FRONTEND_URL="https://${CF_DOMAIN}"
  # Keep the distribution default in CORS while both hostnames serve the app.
  if [ "$FRONTEND_URL" = "https://${CF_DOMAIN}" ]; then
    CORS_ORIGINS="${FRONTEND_URL},http://localhost:5173"
  else
    CORS_ORIGINS="${FRONTEND_URL},https://${CF_DOMAIN},http://localhost:5173"
  fi
  AGENT_REGISTRY_TABLE=$(terraform output -raw agent_registry_table_name 2>/dev/null || echo "")
  AGENT_CLIENTS_TABLE=$(terraform output -raw agent_clients_table_name 2>/dev/null || echo "bedrockgw-${ENVIRONMENT}-agent-clients")
  # Gateway IRSA role ARN lives in the platform layer; read it from IAM rather
  # than cross-layer terraform_remote_state. Deterministic given name_prefix.
  GATEWAY_ROLE_ARN=$(aws iam get-role --role-name "adp-${ENVIRONMENT}-role-gateway-service" --query 'Role.Arn' --output text 2>/dev/null || echo "")
  CFN_TEMPLATE_BUCKET=$(aws ssm get-parameter --name "/adp/$ENVIRONMENT/gateway/frontend-bucket" \
    --query "Parameter.Value" --output text --region "$AWS_REGION" 2>/dev/null || echo "")
  if [ -z "$CFN_TEMPLATE_BUCKET" ] || [ "$CFN_TEMPLATE_BUCKET" = "None" ]; then
    CFN_TEMPLATE_BUCKET=""
    warn "ADP_CFN_TEMPLATE_BUCKET resolved empty — 'Connect AWS account' will not work until the frontend bucket SSM param is published"
  fi

  # --- SSM resolution for configmap values (parity with gateway-deploy.yml) ---
  # Helper: read SSM param with fallback (same pattern as the workflow's get_ssm)
  _get_ssm() { aws ssm get-parameter --name "$1" --query Parameter.Value --output text --region "$AWS_REGION" 2>/dev/null || echo "${2:-}"; }

  # Issue #143: Chat logging — conditional on chat-logs bucket existence
  CHAT_LOGS_BUCKET=$(_get_ssm "/adp/$ENVIRONMENT/gateway/chat-logs-bucket" "")
  if [ "$CHAT_LOGS_BUCKET" = "None" ]; then CHAT_LOGS_BUCKET=""; fi
  if [ -n "$CHAT_LOGS_BUCKET" ]; then
    CHAT_LOGGING_ENABLED="true"
  else
    CHAT_LOGGING_ENABLED="false"
    warn "Chat-logs bucket not found in SSM — chat logging disabled (set /adp/$ENVIRONMENT/gateway/chat-logs-bucket to enable)"
  fi
  # #5672: chat-transcript redaction level. This was the literal "basic", which
  # silently overrode the application's own default of "standard" in every
  # environment — so customer content pasted into a prompt (names, emails, phone
  # numbers, identity numbers, addresses, payment details) was stored in S3
  # exactly as typed.
  #
  # Now per-environment and fail-closed: unset means "standard", the strongest
  # level. An environment that genuinely needs something weaker sets the
  # parameter, which leaves a review trail, rather than the choice being a
  # constant buried in this script. The app also resolves an unrecognised value
  # up to "standard" (see modules/gateway/src/chat_logging/config.py).
  CHAT_LOGGING_SCRUB_LEVEL=$(_get_ssm "/adp/$ENVIRONMENT/gateway/chat-logging-scrub-level" "standard")
  if [ -z "$CHAT_LOGGING_SCRUB_LEVEL" ] || [ "$CHAT_LOGGING_SCRUB_LEVEL" = "None" ]; then
    CHAT_LOGGING_SCRUB_LEVEL="standard"
  fi

  # #4075: Budget enforcement fail mode. Default "closed" (safe-by-default: a failed
  # budget check must not admit uncapped spend). A bounded, alarmed 30s grace window
  # keeps transient DB blips from downing inference. Set to "open" via this SSM param
  # + rollout restart to roll back.
  BUDGET_FAIL_MODE=$(_get_ssm "/adp/$ENVIRONMENT/gateway/budget-fail-mode" "closed")
  if [ -z "$BUDGET_FAIL_MODE" ] || [ "$BUDGET_FAIL_MODE" = "None" ]; then BUDGET_FAIL_MODE="closed"; fi

  # #4076: Credential->host egress binding. Default "false" (shadow mode) — violations
  # are WARN-logged but allowed, so operators can confirm the service->host map covers
  # live traffic before anyone gets a 403. Flip to "true" per env via SSM once clean.
  VAULT_ENFORCE_CREDENTIAL_HOST_BINDING=$(_get_ssm "/adp/$ENVIRONMENT/gateway/vault-enforce-credential-host-binding" "false")
  if [ "$VAULT_ENFORCE_CREDENTIAL_HOST_BINDING" = "None" ]; then VAULT_ENFORCE_CREDENTIAL_HOST_BINDING="false"; fi

  # #5653 (A01): whether the pod may believe X-Caller-Identity, which it treats as
  # proof of identity (resolved against the agent registry to a privileged
  # internal/platform context). Was hard-coded "true" below, overriding the
  # application's safe default on every deploy. The param is published by the
  # api-gateway Terraform module — the same one that blanks the header on every
  # non-AWS_IAM route — so "true" means the edge control making that claim sound is
  # actually deployed here. Default "false" keeps a forged assertion inert in an
  # environment that has not applied it.
  TRUST_APIGW_HEADERS=$(_get_ssm "/adp/$ENVIRONMENT/gateway/trust-apigw-headers" "false")
  if [ -z "$TRUST_APIGW_HEADERS" ] || [ "$TRUST_APIGW_HEADERS" = "None" ]; then TRUST_APIGW_HEADERS="false"; fi

  # Issue #1158: Vault proxy host allowlist (SSRF mitigation, FAIL-CLOSED when empty)
  VAULT_PROXY_HOST_ALLOWLIST=$(_get_ssm "/adp/$ENVIRONMENT/gateway/vault-proxy-host-allowlist" "api.github.com,api.openai.com,api.anthropic.com,*.atlassian.net,api.stripe.com,slack.com")
  if [ "$VAULT_PROXY_HOST_ALLOWLIST" = "None" ]; then VAULT_PROXY_HOST_ALLOWLIST="api.github.com,api.openai.com,api.anthropic.com,*.atlassian.net,api.stripe.com,slack.com"; fi

  # #2082: Knowledge-registry ingestion queue (agent-context SQS). Empty is safe —
  # registry routes still mount; dispatch returns 503 until set.
  INGESTION_QUEUE_URL=$(_get_ssm "/adp/$ENVIRONMENT/agent-context/ingestion-queue-url" "")
  if [ "$INGESTION_QUEUE_URL" = "None" ]; then INGESTION_QUEUE_URL=""; fi

  # #3069/#3105: Agent run-logs transcript bucket (deterministic naming)
  EFFECTIVE_ACCOUNT="${ADP_CUSTOMER_ACCOUNT_ID:-$ACCOUNT_ID}"
  AGENT_RUN_LOGS_BUCKET="adp-${ENVIRONMENT}-agent-run-logs-${EFFECTIVE_ACCOUNT}"
  cd "$ROOT_DIR/modules/gateway"
  # Preserve restricted Pod Security Admission labels on upgrades. Applying a
  # generated label-free Namespace would remove them until the rollout finished.
  kubectl get namespace adp-gateway >/dev/null 2>&1 || kubectl create namespace adp-gateway
  # Issue #1008: Create bedrockgateway-secrets K8s Secret from Secrets Manager
  SM_SECRET_NAME="adp/${ENVIRONMENT}/gateway/token-secret-key"
  TOKEN_SECRET=$(aws secretsmanager get-secret-value \
    --secret-id "$SM_SECRET_NAME" \
    --query SecretString --output text 2>/dev/null || echo "")
  if [ -z "$TOKEN_SECRET" ] || [ "$TOKEN_SECRET" = "None" ]; then
    TOKEN_SECRET=$(openssl rand -hex 32)
    aws secretsmanager create-secret \
      --name "$SM_SECRET_NAME" \
      --secret-string "$TOKEN_SECRET" \
      --description "JWT token signing key for Bedrock Gateway" \
      --region "${AWS_REGION}" 2>/dev/null || \
    aws secretsmanager put-secret-value \
      --secret-id "$SM_SECRET_NAME" \
      --secret-string "$TOKEN_SECRET" \
      --region "${AWS_REGION}"
  fi
  # Issue #2824 gap: also create the internal-api-key used by the webhook Lambda
  # to call /internal/v1/* endpoints on the gateway. Without this, webhook
  # identity resolution fails with "Internal API key not available".
  INTERNAL_API_SM="adp/${ENVIRONMENT}/gateway/internal-api-key"
  INTERNAL_API_KEY=$(aws secretsmanager get-secret-value \
    --secret-id "$INTERNAL_API_SM" \
    --query SecretString --output text 2>/dev/null || echo "")
  if [ -z "$INTERNAL_API_KEY" ] || [ "$INTERNAL_API_KEY" = "None" ]; then
    INTERNAL_API_KEY=$(openssl rand -hex 32)
    aws secretsmanager create-secret \
      --name "$INTERNAL_API_SM" \
      --secret-string "$INTERNAL_API_KEY" \
      --description "Shared secret for /internal/v1/* webhook-to-gateway auth" \
      --region "${AWS_REGION}" 2>/dev/null || \
    aws secretsmanager put-secret-value \
      --secret-id "$INTERNAL_API_SM" \
      --secret-string "$INTERNAL_API_KEY" \
      --region "${AWS_REGION}"
  fi
  # Issue #5656 (A05): dedicated signing key for single-use identity-linking
  # ("magic link") tokens. src/shared/config.py no longer falls back to the
  # session-signing key (BG_TOKEN_SECRET_KEY), so this must exist for the
  # identity-linking endpoints to work — and because it is now separate, it can be
  # replaced without signing every user out. The operator path creates it if
  # absent; .github/workflows/gateway-deploy.yml only reads the stable key.
  # Both paths must use the same secret name or identity linking returns 503.
  # Rotation: docs/runbooks/gateway-secret-rotation.md
  MAGIC_LINK_SM="adp/${ENVIRONMENT}/gateway/magic-link-secret"
  MAGIC_LINK_SECRET=$(python3 "$ROOT_DIR/modules/gateway/scripts/ensure-signing-secret.py" \
    --name "$MAGIC_LINK_SM" --region "$AWS_REGION")
  APIGW_PROVENANCE_SECRET=$(aws ssm get-parameter \
    --name "/adp/${ENVIRONMENT}/gateway/apigw-provenance-secret" \
    --with-decryption --query Parameter.Value --output text --region "$AWS_REGION" 2>/dev/null || echo "")
  if [ "$TRUST_APIGW_HEADERS" = "true" ] && { [ -z "$APIGW_PROVENANCE_SECRET" ] || [ "$APIGW_PROVENANCE_SECRET" = "None" ]; }; then
    echo "ERROR: API Gateway header trust is enabled but its provenance secret is unavailable" >&2
    exit 1
  fi
  SECRET_APPLY_RESULT=$(kubectl create secret generic bedrockgateway-secrets \
    --from-literal=token-secret-key="$TOKEN_SECRET" \
    --from-literal=internal-api-key="$INTERNAL_API_KEY" \
    --from-literal=apigw-provenance-secret="$APIGW_PROVENANCE_SECRET" \
    --from-literal=magic-link-secret="$MAGIC_LINK_SECRET" \
    -n adp-gateway --dry-run=client -o yaml | kubectl apply -f -)
  echo "$SECRET_APPLY_RESULT"
  COGNITO_CLI_CLIENT_ID=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/cognito-cli-client-id" "")
  COGNITO_AGENT_CLIENT_ID=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/cognito-agent-client-id" "")
  COGNITO_GITLAB_CLIENT_ID=$(_get_ssm "/adp/${ENVIRONMENT}/gitlab/oidc-client-id" "")
  COGNITO_PENTEST_CLIENT_ID=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/cognito-pentest-client-id" "")
  BEDROCK_ROUTING_SHADOW_MODE=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/bedrock-routing-shadow-mode" "true")

  # PMM-03 / D3: render the same production org/team allowlist source as the
  # GitHub deployment path. Invalid JSON or invalid value shapes stop before a
  # ConfigMap can silently widen admission.
  PERSONA_MODEL_MAPPING_ENABLED=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/persona-model-mapping-enabled" "true")
  MODEL_ALLOWED_MODELS_CONFIG_RAW=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/model-allowed-models-config" "__ADP_SSM_UNAVAILABLE__")
  MODEL_ALLOWED_MODELS_CONFIG=$(printf '%s' "$MODEL_ALLOWED_MODELS_CONFIG_RAW" | python3 "$ROOT_DIR/platform/scripts/validate-model-allowlist-config.py")
  MODEL_ALLOWED_MODELS_CONFIG_SED=${MODEL_ALLOWED_MODELS_CONFIG//\\/\\\\}
  MODEL_ALLOWED_MODELS_CONFIG_SED=${MODEL_ALLOWED_MODELS_CONFIG_SED//&/\\&}
  MODEL_ALLOWED_MODELS_CONFIG_SED=${MODEL_ALLOWED_MODELS_CONFIG_SED//|/\\|}

  # Issue #3960: CIDRs the gateway may dial an in-pod control listener in.
  # This is the SSRF allowlist for the one outbound path that deliberately
  # targets PRIVATE addresses, so it cannot be defaulted to something
  # convenient — the default is EMPTY, which fails closed (every control
  # request answers 409 "not configured"). An operator who forgets the SSM
  # param gets a clear misconfiguration instead of a gateway willing to
  # dial arbitrary private addresses.
  #
  # Not discovered from the cluster at deploy time on purpose: reading the
  # live pod CIDR would silently re-widen the allowlist whenever the VPC
  # changed, which is exactly the kind of change nobody reviews.
  AGENT_CONTROL_CLUSTER_POD_CIDRS=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/agent-control-cluster-pod-cidrs" "")
  # The authority configuration parameters are encrypted at rest.
  get_authority_ssm() { aws ssm get-parameter --with-decryption --name "$1" --query Parameter.Value --output text 2>/dev/null || echo "${2:-}"; }
  AGENT_AUTHORITY_ENABLED=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/agent-authority-enabled" "false")
  ADP_WORK_CLAIMS_ENABLED=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/work-claims-enabled" "false")
  ADP_WORK_CLAIM_PRODUCER_ROLES=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/work-claim-producer-roles" "")
  AGENT_AUTHORITY_TABLE=$(get_authority_ssm "/adp/${ENVIRONMENT}/webhook-ingress/agent-authority-table" "")
  WEBHOOK_EVENTS_TABLE=$(_get_ssm "/adp/${ENVIRONMENT}/webhook-ingress/webhook-events-table" "adp-${ENVIRONMENT}-webhook-events")
  ADP_TASK_API_ADMISSION_ENABLED=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/task-api-admission-enabled" "false")
  ADP_TASK_API_READ_ENABLED=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/task-api-read-enabled" "false")
  ADP_TASK_API_WORKER_ENABLED=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/task-api-worker-enabled" "false")
  ADP_TASK_API_RECOVERY_ENABLED=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/task-api-recovery-enabled" "false")
  TASK_ARTIFACT_BUCKET_NAME=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/task-artifact-bucket-name" "")
  ADP_TASK_API_QUEUE_URL=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/task-api-queue-url" "")
  ADP_TASK_ADMISSION_PRODUCER_ROLES=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/task-admission-producer-roles" "")
  ADP_TASK_DISPATCH_PRODUCER_ROLES=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/task-dispatch-producer-roles" "")
  ADP_TASK_RECOVERY_PRODUCER_ROLES=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/task-recovery-producer-roles" "")
  ADP_TASK_QUALIFICATION_ID=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/task-qualification-id" "")
  ADP_TASK_WORKER_IMAGE_DIGESTS=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/task-worker-image-digests" "disabled")
  ADP_TASK_WORKER_SERVICE_ACCOUNT=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/task-worker-service-account" "agent-scaledjob-sa")
  AGENT_DISPATCH_QUEUE_URL=$(_get_ssm "/adp/${ENVIRONMENT}/webhook-ingress/sqs-queue-url" "")
  BG_ORCH_DISPATCH_REPO=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/orchestration-dispatch-repo" "")
  AGENT_WORKER_IMAGE_DIGESTS=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/agent-authority-worker-images" "disabled")
  ADP_MODEL_ROOT_BINDINGS_RAW=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/model-root-bindings" "[]")
  ADP_MODEL_ROOT_BINDINGS_SED=$(printf '%s' "$ADP_MODEL_ROOT_BINDINGS_RAW" | python3 "$ROOT_DIR/platform/scripts/render-model-root-config.py" bindings)
  ADP_ARC_MODEL_BINDINGS_RAW=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/arc-model-bindings" "[]")
  ADP_ARC_MODEL_BINDINGS_SED=$(printf '%s' "$ADP_ARC_MODEL_BINDINGS_RAW" | python3 "$ROOT_DIR/platform/scripts/render-model-root-config.py" bindings)
  ADP_CHAT_WORKER_IMAGE_DIGESTS_RAW=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/chat-authority-worker-images" "")
  ADP_CHAT_WORKER_IMAGE_DIGESTS_SED=$(printf '%s' "$ADP_CHAT_WORKER_IMAGE_DIGESTS_RAW" | python3 "$ROOT_DIR/platform/scripts/render-model-root-config.py" images)
  AGENT_AUTHORITY_KEY_ID=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/agent-authority-key-id" "disabled")
  AGENT_TASK_SOURCE_ROLE_ARN=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/agent-task-source-role-arn" "")
  AGENT_TASK_SOURCE_EKS_CLUSTER=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/agent-task-source-eks-cluster" "")
  AGENT_TASK_SOURCE_ISOLATION_CONFIRMED=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/agent-task-source-isolation-confirmed" "false")

  CONFIGMAP_APPLY_RESULT=$(sed -e "s|__AWS_REGION__|${AWS_REGION}|g" \
      -e "s|__ENVIRONMENT__|${ENVIRONMENT}|g" \
      -e "s|__CYBER_ACCOUNT_ID__|${EFFECTIVE_ACCOUNT}|g" \
      -e "s|__DB_HOST__|${DB_HOST}|g" \
      -e "s|__DB_USER__|${DB_USER}|g" \
      -e "s|__DB_NAME__|${DB_NAME}|g" \
      -e "s|__REDIS_HOST__|${REDIS_HOST:-localhost}|g" \
      -e "s|__REDIS_PORT__|${REDIS_PORT:-6379}|g" \
      -e "s|__REDIS_IAM_USERNAME__|${REDIS_IAM_USERNAME}|g" \
      -e "s|__REDIS_CACHE_NAME__|${REDIS_CACHE_NAME}|g" \
      -e "s|__REDIS_IAM_AUTH__|$([ -n "$REDIS_IAM_USERNAME" ] && [ -n "$REDIS_CACHE_NAME" ] && echo true || echo false)|g" \
      -e "s|__COGNITO_USER_POOL_ID__|${COGNITO_USER_POOL_ID}|g" \
      -e "s|__COGNITO_CLIENT_ID__|${COGNITO_CLIENT_ID}|g" \
      -e "s|__COGNITO_AGENT_CLIENT_ID__|${COGNITO_AGENT_CLIENT_ID}|g" \
      -e "s|__AGENT_CLIENTS_TABLE__|${AGENT_CLIENTS_TABLE}|g" \
      -e "s|__COGNITO_GITLAB_CLIENT_ID__|${COGNITO_GITLAB_CLIENT_ID}|g" \
      -e "s|__COGNITO_PENTEST_CLIENT_ID__|${COGNITO_PENTEST_CLIENT_ID}|g" \
      -e "s|__COGNITO_DOMAIN__|${COGNITO_DOMAIN}|g" \
      -e "s|__CORS_ALLOWED_ORIGINS__|${CORS_ORIGINS}|g" \
      -e "s|__GATEWAY_BASE_URL__|${FRONTEND_URL}|g" \
      -e "s|__CFN_TEMPLATE_BUCKET__|${CFN_TEMPLATE_BUCKET}|g" \
      -e "s|__GATEWAY_ROLE_ARN__|${GATEWAY_ROLE_ARN}|g" \
      -e "s|__CHAT_LOGGING_ENABLED__|${CHAT_LOGGING_ENABLED}|g" \
      -e "s|__CHAT_LOGGING_BUCKET__|${CHAT_LOGS_BUCKET}|g" \
      -e "s|__CHAT_LOGGING_SCRUB_LEVEL__|${CHAT_LOGGING_SCRUB_LEVEL}|g" \
      -e "s|__TRUST_APIGW_HEADERS__|${TRUST_APIGW_HEADERS}|g" \
      -e "s|__AGENT_REGISTRY_TABLE__|${AGENT_REGISTRY_TABLE}|g" \
      -e "s|__VAULT_PROXY_HOST_ALLOWLIST__|${VAULT_PROXY_HOST_ALLOWLIST}|g" \
      -e "s|__INGESTION_QUEUE_URL__|${INGESTION_QUEUE_URL}|g" \
      -e "s|__AGENT_RUN_LOGS_BUCKET__|${AGENT_RUN_LOGS_BUCKET}|g" \
      -e "s|__BUDGET_FAIL_MODE__|${BUDGET_FAIL_MODE}|g" \
      -e "s|__VAULT_ENFORCE_CREDENTIAL_HOST_BINDING__|${VAULT_ENFORCE_CREDENTIAL_HOST_BINDING}|g" \
      -e "s|__COGNITO_CLI_CLIENT_ID__|${COGNITO_CLI_CLIENT_ID}|g" \
      -e "s|__BEDROCK_ROUTING_SHADOW_MODE__|${BEDROCK_ROUTING_SHADOW_MODE}|g" \
      -e "s|__PLATFORM_BEDROCK_ACCOUNT_ID__|${EFFECTIVE_ACCOUNT}|g" \
      -e "s|__PERSONA_MODEL_MAPPING_ENABLED__|${PERSONA_MODEL_MAPPING_ENABLED}|g" \
      -e "s|__MODEL_ALLOWED_MODELS_CONFIG__|${MODEL_ALLOWED_MODELS_CONFIG_SED}|g" \
      -e "s|__AGENT_CONTROL_CLUSTER_POD_CIDRS__|${AGENT_CONTROL_CLUSTER_POD_CIDRS}|g" \
      -e "s|__AGENT_AUTHORITY_ENABLED__|${AGENT_AUTHORITY_ENABLED}|g" \
      -e "s|__ADP_WORK_CLAIMS_ENABLED__|${ADP_WORK_CLAIMS_ENABLED}|g" \
      -e "s|__ADP_WORK_CLAIM_PRODUCER_ROLES__|${ADP_WORK_CLAIM_PRODUCER_ROLES}|g" \
      -e "s|__AGENT_AUTHORITY_TABLE__|${AGENT_AUTHORITY_TABLE}|g" \
      -e "s|__WEBHOOK_EVENTS_TABLE__|${WEBHOOK_EVENTS_TABLE}|g" \
      -e "s|__ADP_TASK_API_ADMISSION_ENABLED__|${ADP_TASK_API_ADMISSION_ENABLED}|g" \
      -e "s|__ADP_TASK_API_READ_ENABLED__|${ADP_TASK_API_READ_ENABLED}|g" \
      -e "s|__ADP_TASK_API_WORKER_ENABLED__|${ADP_TASK_API_WORKER_ENABLED}|g" \
      -e "s|__ADP_TASK_API_RECOVERY_ENABLED__|${ADP_TASK_API_RECOVERY_ENABLED}|g" \
      -e "s|__TASK_ARTIFACT_BUCKET_NAME__|${TASK_ARTIFACT_BUCKET_NAME}|g" \
      -e "s|__ADP_TASK_API_QUEUE_URL__|${ADP_TASK_API_QUEUE_URL}|g" \
      -e "s|__ADP_TASK_ADMISSION_PRODUCER_ROLES__|${ADP_TASK_ADMISSION_PRODUCER_ROLES}|g" \
      -e "s|__ADP_TASK_DISPATCH_PRODUCER_ROLES__|${ADP_TASK_DISPATCH_PRODUCER_ROLES}|g" \
      -e "s|__ADP_TASK_RECOVERY_PRODUCER_ROLES__|${ADP_TASK_RECOVERY_PRODUCER_ROLES}|g" \
      -e "s|__ADP_TASK_QUALIFICATION_ID__|${ADP_TASK_QUALIFICATION_ID}|g" \
      -e "s|__ADP_TASK_WORKER_IMAGE_DIGESTS__|${ADP_TASK_WORKER_IMAGE_DIGESTS}|g" \
      -e "s|__ADP_TASK_WORKER_SERVICE_ACCOUNT__|${ADP_TASK_WORKER_SERVICE_ACCOUNT}|g" \
      -e "s|__AGENT_DISPATCH_QUEUE_URL__|${AGENT_DISPATCH_QUEUE_URL}|g" \
      -e "s|__BG_ORCH_DISPATCH_REPO__|${BG_ORCH_DISPATCH_REPO}|g" \
      -e "s|__AGENT_WORKER_IMAGE_DIGESTS__|${AGENT_WORKER_IMAGE_DIGESTS}|g" \
      -e "s|__ADP_MODEL_ROOT_BINDINGS__|${ADP_MODEL_ROOT_BINDINGS_SED}|g" \
      -e "s|__ADP_ARC_MODEL_BINDINGS__|${ADP_ARC_MODEL_BINDINGS_SED}|g" \
      -e "s|__ADP_CHAT_WORKER_IMAGE_DIGESTS__|${ADP_CHAT_WORKER_IMAGE_DIGESTS_SED}|g" \
      -e "s|__AGENT_AUTHORITY_KEY_ID__|${AGENT_AUTHORITY_KEY_ID}|g" \
      -e "s|__AGENT_TASK_SOURCE_ROLE_ARN__|${AGENT_TASK_SOURCE_ROLE_ARN}|g" \
      -e "s|__AGENT_TASK_SOURCE_EKS_CLUSTER__|${AGENT_TASK_SOURCE_EKS_CLUSTER}|g" \
      -e "s|__AGENT_TASK_SOURCE_ISOLATION_CONFIRMED__|${AGENT_TASK_SOURCE_ISOLATION_CONFIRMED}|g" \
      k8s/configmap.yaml | kubectl apply -f -)
  echo "$CONFIGMAP_APPLY_RESULT"
  # Render serviceaccount with the correct IRSA role ARN (Issue #1008)
  sed -e "s|__GATEWAY_IRSA_ROLE_ARN__|${GATEWAY_ROLE_ARN}|g" \
      k8s/serviceaccount.yaml | kubectl apply -f -
  for f in k8s/*.yaml; do
    case "$(basename "$f")" in
      configmap.yaml|serviceaccount.yaml|deployment.yaml|targetgroupbinding.yaml) continue ;;
    esac
    if kubectl create --dry-run=client --validate=false -f "$f" \
         -o jsonpath='{.kind}{"\n"}{range .items[*]}{.kind}{"\n"}{end}' \
         | grep -qx Namespace; then
      continue
    fi
    kubectl apply -f "$f" -n adp-gateway
  done

  # Render deployment settings as well as the ConfigMap. Applying the raw
  # manifest leaves feature flags and the image as literal placeholders.
  FEATURE_ORCHESTRATION_ENGINE_ENABLED=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/feature-orchestration-engine" "false")
  FEATURE_AGENT_EXPLANATIONS_ENABLED=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/feature-agent-explanations" "false")
  FEATURE_AGENT_CONTROL_ENABLED=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/feature-agent-control" "false")
  FEATURE_NEW_UI_ENABLED=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/feature-new-ui" "false")
  FEATURE_AGENT_MODELS_ENABLED=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/feature-agent-models" "$PERSONA_MODEL_MAPPING_ENABLED")
  DEPLOYMENT_APPLY_RESULT=$(sed -e "s|__FEATURE_ORCHESTRATION_ENGINE_ENABLED__|${FEATURE_ORCHESTRATION_ENGINE_ENABLED}|g" \
      -e "s|__FEATURE_AGENT_EXPLANATIONS_ENABLED__|${FEATURE_AGENT_EXPLANATIONS_ENABLED}|g" \
      -e "s|__FEATURE_AGENT_CONTROL_ENABLED__|${FEATURE_AGENT_CONTROL_ENABLED}|g" \
      -e "s|__FEATURE_NEW_UI_ENABLED__|${FEATURE_NEW_UI_ENABLED}|g" \
      -e "s|__FEATURE_AGENT_MODELS_ENABLED__|${FEATURE_AGENT_MODELS_ENABLED}|g" \
      -e "s|REPLACE_WITH_GATEWAY_IMAGE|${GATEWAY_IMAGE}|g" \
      k8s/deployment.yaml | kubectl apply -f - -n adp-gateway)
  echo "$DEPLOYMENT_APPLY_RESULT"

  if [ "$UPDATE_MODE" = true ]; then
    # Applying a changed pod template already starts a rollout. Restart only
    # when a Secret/ConfigMap changed without a pod-template update; restarting
    # an unchanged deployment on every retry can race EKS node provisioning.
    CURRENT_IMAGE=$(kubectl get deployment/bedrockgateway -n adp-gateway \
      -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || echo "")
    if [ "$CURRENT_IMAGE" != "${GATEWAY_IMAGE}" ]; then
      kubectl set image deployment/bedrockgateway \
        bedrockgateway="${GATEWAY_IMAGE}" -n adp-gateway
    elif { [[ "$SECRET_APPLY_RESULT" == *configured* ]] || [[ "$CONFIGMAP_APPLY_RESULT" == *configured* ]]; } \
         && [[ "$DEPLOYMENT_APPLY_RESULT" == *unchanged* ]]; then
      echo "Secret or ConfigMap changed. Restarting gateway to load it..."
      kubectl rollout restart deployment/bedrockgateway -n adp-gateway
    else
      echo "Gateway pod template already matches the release; checking rollout..."
    fi
    kubectl rollout status deployment/bedrockgateway -n adp-gateway --timeout=600s \
      || fail "Gateway rollout failed. Check: kubectl describe deployment/bedrockgateway -n adp-gateway"

    # Post-rollout health check (§2)
    sleep 5
    HEALTH=$(kubectl exec -n adp-gateway deploy/bedrockgateway -- \
      curl -sf http://localhost:8080/health 2>/dev/null) || true
    echo "$HEALTH" | grep -q '"status"' \
      || warn "Health check inconclusive. Verify: kubectl exec -n adp-gateway deploy/bedrockgateway -- curl http://localhost:8080/health"

  else
    # Fresh deployments require the same release-image readiness as updates.
    kubectl set image deployment/bedrockgateway bedrockgateway="${GATEWAY_IMAGE}" -n adp-gateway
    kubectl rollout status deployment/bedrockgateway -n adp-gateway --timeout=300s || fail "Gateway rollout not complete"
  fi

  # Enforce the restricted namespace policy only after the hardened image and
  # pod spec are Ready, then prove the API server rejects a privileged pod.
  kubectl apply -f k8s/namespace.yaml
  scripts/verify-restricted-admission.sh adp-gateway

  # The scheduled engine consumes the same image but is a separate deployment.
  python3 "$ROOT_DIR/modules/gateway/scripts/sync-gateway-engine.py" \
    --image "$GATEWAY_IMAGE" --account "$ACCOUNT_ID" --region "$AWS_REGION" \
    --environment "$ENVIRONMENT" \
    || fail "Gateway rolled out but engine alignment failed; release is incomplete"

  PRICING_RELEASE_IMAGE="${GATEWAY_IMAGE}"
  python3 "$ROOT_DIR/modules/gateway/scripts/pricing-rollout.py" migrate \
    --account-id "$ACCOUNT_ID" --environment "$ENVIRONMENT" --region "$AWS_REGION" \
    --expected-image "$PRICING_RELEASE_IMAGE" \
    || fail "Gateway image is deployed but pricing migrations or activation are incomplete"
  PRICING_FINALIZE_ARGS=()
  case "${ADP_PRICING_ALLOW_PARTIAL_REFRESH:-false}" in
    true) PRICING_FINALIZE_ARGS+=(--allow-partial-refresh) ;;
    false) ;;
    *) fail "ADP_PRICING_ALLOW_PARTIAL_REFRESH must be true or false" ;;
  esac
  python3 "$ROOT_DIR/modules/gateway/scripts/pricing-rollout.py" finalize \
    --account-id "$ACCOUNT_ID" --environment "$ENVIRONMENT" --region "$AWS_REGION" \
    --expected-image "$PRICING_RELEASE_IMAGE" \
    ${PRICING_FINALIZE_ARGS[@]+"${PRICING_FINALIZE_ARGS[@]}"} \
    || fail "Pricing refresh verification failed; its schedule remains disabled"
  # Recheck both consumers before declaring this release complete.
  python3 "$ROOT_DIR/modules/gateway/scripts/sync-gateway-engine.py" \
    --verify-only --image "$GATEWAY_IMAGE" --account "$ACCOUNT_ID" --region "$AWS_REGION" \
    --environment "$ENVIRONMENT" \
    || fail "Gateway rolled out but engine alignment failed; release is incomplete"

fi
ok "Gateway deployed"

refresh_credentials
# =============================================================================
# Step 5/12: Discover internal ALB and wire to API Gateway + CloudFront
# =============================================================================
if [ "$DEPLOY_GATEWAY" = true ]; then
  step "Step 5/12: Wire internal ALB to API Gateway and CloudFront"

  # Discover ALB, cache to SSM, export ALB_ARN / ALB_DNS / ALB_SG_IDS.
  # The shared script exits 1 if the ALB is not found after 10 min; in
  # deploy-all.sh we downgrade that to a warning so the rest of the deploy
  # can continue (API Gateway will keep MOCK integrations).
  if ENVIRONMENT="$ENVIRONMENT" AWS_REGION="$AWS_REGION" bash "$SCRIPT_DIR/wire-gateway-alb.sh"; then
    # Source the exported variables into this shell (the script also writes
    # to SSM, but we need the values locally for the terraform re-apply).
    ALB_ARN=$(aws ssm get-parameter --name "/adp/$ENVIRONMENT/gateway/internal-alb-arn" --query "Parameter.Value" --output text --region "$AWS_REGION" 2>/dev/null || echo "")
    ALB_DNS=$(aws ssm get-parameter --name "/adp/$ENVIRONMENT/gateway/internal-alb-dns" --query "Parameter.Value" --output text --region "$AWS_REGION" 2>/dev/null || echo "")
    ALB_SG_IDS=$(aws ssm get-parameter --name "/adp/$ENVIRONMENT/gateway/internal-alb-security-group-ids" --query "Parameter.Value" --output text --region "$AWS_REGION" 2>/dev/null || echo "[]")
    # Issue #4010: internal-plane ALB (serves /internal/*, not fronted by
    # CloudFront). Absent until modules/gateway/k8s/ingress-internal.yaml has
    # been applied, in which case these stay empty and the Terraform falls back
    # to the edge ALB — i.e. pre-#4010 behavior.
    INTERNAL_PLANE_ALB_ARN=$(aws ssm get-parameter --name "/adp/$ENVIRONMENT/gateway/internal-plane-alb-arn" --query "Parameter.Value" --output text --region "$AWS_REGION" 2>/dev/null || echo "")
    INTERNAL_PLANE_ALB_DNS=$(aws ssm get-parameter --name "/adp/$ENVIRONMENT/gateway/internal-plane-alb-dns" --query "Parameter.Value" --output text --region "$AWS_REGION" 2>/dev/null || echo "")
    INTERNAL_PLANE_ALB_SG_IDS=$(aws ssm get-parameter --name "/adp/$ENVIRONMENT/gateway/internal-plane-alb-security-group-ids" --query "Parameter.Value" --output text --region "$AWS_REGION" 2>/dev/null || echo "[]")
  else
    [ "$UPDATE_MODE" = false ] || fail "ALB wiring failed during upgrade"
    warn "ALB not found after 10 minutes. Skipping ALB wiring — API Gateway will use MOCK integration."
    ALB_ARN=""
    ALB_DNS=""
    ALB_SG_IDS="[]"
    INTERNAL_PLANE_ALB_ARN=""
    INTERNAL_PLANE_ALB_DNS=""
    INTERNAL_PLANE_ALB_SG_IDS="[]"
  fi

  # Only pass the internal-plane vars when that ALB actually exists, so the
  # applied plan is byte-identical to today's on clusters without it.
  INTERNAL_PLANE_ARGS=()
  if [ -n "$INTERNAL_PLANE_ALB_ARN" ] && [ "$INTERNAL_PLANE_ALB_ARN" != "None" ]; then
    INTERNAL_PLANE_ARGS+=(
      -var "internal_plane_alb_arn=$INTERNAL_PLANE_ALB_ARN"
      -var "internal_plane_alb_dns=$INTERNAL_PLANE_ALB_DNS"
      -var "internal_plane_alb_security_group_ids=$INTERNAL_PLANE_ALB_SG_IDS"
    )
  fi

  # Re-apply gateway Terraform with ALB details to wire API Gateway VPC Link v2
  # (needs ARN for integration target, DNS for integration URI, SG IDs for egress)
  # and CloudFront VPC Origin (needs ARN only).
  if [ -n "$ALB_ARN" ] && [ "$ALB_ARN" != "None" ] && [ -n "$ALB_DNS" ]; then
    echo "Re-applying gateway Terraform with ALB details to wire API Gateway VPC Link v2 and CloudFront..."
    cd "$ROOT_DIR/modules/gateway/infra"
    if [ "$UPDATE_MODE" = true ]; then
      terraform_update_apply "gateway-alb-wire" "$GATEWAY_UPDATE_VAR_FILE" \
        -var "internal_alb_arn=$ALB_ARN" \
        -var "internal_alb_dns=$ALB_DNS" \
        -var "alb_security_group_ids=$ALB_SG_IDS" \
        "${INTERNAL_PLANE_ARGS[@]+"${INTERNAL_PLANE_ARGS[@]}"}" \
        -var "enable_vpc_origin=true"
    else
      terraform apply \
        -var-file="../../../environments/$ENVIRONMENT/modules/gateway.tfvars" \
        -var "internal_alb_arn=$ALB_ARN" \
        -var "internal_alb_dns=$ALB_DNS" \
        -var "alb_security_group_ids=$ALB_SG_IDS" \
        "${INTERNAL_PLANE_ARGS[@]+"${INTERNAL_PLANE_ARGS[@]}"}" \
        -var "enable_vpc_origin=true" \
        -auto-approve
      ok "API Gateway VPC Link and CloudFront VPC Origin wired to ALB"
    fi

    # Issue #4010: apply the edge `/internal` -> 403 deny LAST, and only if the
    # apply above actually repointed `/internal/{proxy+}` at the internal-plane
    # ALB. The script re-reads the live integration to confirm that, and skips
    # harmlessly otherwise. Ordering matters: applying the deny while the
    # integration still targets the edge ALB 403s every SigV4 internal call.
    ENVIRONMENT="$ENVIRONMENT" AWS_REGION="$AWS_REGION" \
      bash "$ROOT_DIR/modules/gateway/scripts/apply-internal-plane-deny.sh" || \
      fail "Internal-plane deny validation failed after API GW wiring"
  fi
else
  # Announce the skip rather than passing over silently. Every other phase reports its
  # own exclusion, and a run that jumps from Step 4 to Step 6 with no explanation reads
  # like the script lost a phase.
  step "Step 5/12: Skipping ALB and API Gateway wiring (scope exclusion)"
fi

refresh_credentials
# =============================================================================
# Step 5: Frontend
# =============================================================================

refresh_credentials
# =============================================================================
# Step 7/12: Broker Lambda code
# =============================================================================
# deploy-broker.sh packages the real github-auth-broker Lambda code and updates
# the live Lambda (terraform ships a 503 placeholder). Required for GitHub login.
# Gateway-scope: runs only when the resolved scope includes gateway.
if [ "$DEPLOY_GATEWAY" = true ] && [ "$SKIP_BROKER" = false ] && [ "${UPGRADE_BROKER_ENABLED:-true}" = true ]; then
  step "Step 7/12: Deploy broker Lambda code"
  bash "$ROOT_DIR/modules/gateway/scripts/deploy-broker.sh" --env "$ENVIRONMENT" --region "$AWS_REGION"
  ok "Broker Lambda deployed"
elif [ "$SKIP_BROKER" = true ]; then
  step "Step 7/12: Skipping broker Lambda (--skip-broker)"
else
  step "Step 7/12: Skipping broker Lambda (scope exclusion)"
fi

refresh_credentials
# =============================================================================
# Step 8/12: Bootstrap first admin
# =============================================================================
# bootstrap-admin.sh seeds the first platform_admin's DB rows via kubectl exec.
# Without it, the onboarding gate shows "request access" for everyone. Requires
# the gateway pod to be healthy — we enforce a strict rollout gate here.
# Gateway-scope: runs only when the resolved scope includes gateway.
if [ "$UPDATE_MODE" = true ]; then
  step "Step 8/12: Admin bootstrap (skipped — update mode)"
  ok "Admin already exists on live platform"
elif [ "$DEPLOY_GATEWAY" = true ] && [ "$SKIP_ADMIN_BOOTSTRAP" = false ]; then
  step "Step 8/12: Bootstrap first admin"
  # Strict rollout gate: bootstrap-admin.sh does kubectl exec into the gateway
  # pod, so the deployment must be fully healthy. Wait up to 300s (retries).
  echo "Waiting for gateway rollout to complete (required for admin bootstrap)..."
  if ! kubectl rollout status deployment/bedrockgateway -n adp-gateway --timeout=300s 2>/dev/null; then
    fail "Gateway deployment not healthy after 300s. Cannot bootstrap admin (kubectl exec requires a running pod). Fix the gateway first, then re-run."
  fi
  bash "$ROOT_DIR/modules/gateway/scripts/bootstrap-admin.sh" --env "$ENVIRONMENT" --region "$AWS_REGION"
  ok "First admin bootstrapped"
elif [ "$SKIP_ADMIN_BOOTSTRAP" = true ]; then
  step "Step 8/12: Skipping admin bootstrap (--skip-admin-bootstrap)"
else
  step "Step 8/12: Skipping admin bootstrap (scope exclusion)"
fi

refresh_credentials
# =============================================================================
# Step 9/12: Webhook-ingress stack (KEDA + agent-runtime)
# =============================================================================
# deploy-webhook-ingress.sh builds the agent-runtime image, packages the webhook
# Lambda zip, and terraform-applies the webhook-ingress stack (API GW → Lambda →
# SQS → KEDA → agent-worker). Runs BEFORE agent-factory because agent-factory's
# gateway-main.tf references the KEDA CRD and keda-operator-role that this step
# creates (Issue #1052).
if [ "$DEPLOY_WEBHOOK" = true ]; then
  step "Step 9/12: Deploy webhook-ingress stack"
  WEBHOOK_UPDATE_ARGS=()
  if [ "$UPDATE_MODE" = true ]; then
    WEBHOOK_UPDATE_ARGS+=(--update)
    [ "$CONFIRM_DESTRUCTIVE" = true ] && WEBHOOK_UPDATE_ARGS+=(--confirm-destructive)
  fi
  bash "$ROOT_DIR/modules/agent-factory/webhook-ingress/scripts/deploy-webhook-ingress.sh" \
    --env "$ENVIRONMENT" --region "$AWS_REGION" ${WEBHOOK_UPDATE_ARGS[@]+"${WEBHOOK_UPDATE_ARGS[@]}"}
  ok "Webhook-ingress deployed"
  # The gateway's first pass precedes the webhook-owned table/key/queue. Finish
  # its prepared tick policy once Terraform can discover those identifiers.
  # This is a bootstrap second pass, like the ALB wire step; ongoing full gateway
  # plans retain ownership of the same policy. Dispatch remains separately gated.
  # Partial webhook/factory upgrades must not enter an uninitialized or
  # explicitly excluded gateway module.
  if [ "$DEPLOY_GATEWAY" = true ]; then
    refresh_credentials
    (
      cd "$ROOT_DIR/modules/gateway/infra"
      terraform_update_apply "gateway-worker-authority" \
        "$GATEWAY_UPDATE_VAR_FILE" \
        '-target=module.orchestration_tick[0].aws_iam_role_policy.agent_authority'
    )
  fi
elif [ "$SKIP_WEBHOOK_INGRESS" = true ]; then
  step "Step 9/12: Skipping webhook-ingress (--skip-webhook-ingress)"
else
  step "Step 9/12: Skipping webhook-ingress (scope exclusion)"
fi

refresh_credentials
# =============================================================================
# Step 10/12: Agent Factory
# =============================================================================
# Runs after webhook-ingress which installs KEDA (CRD + operator role).
# GitHub App secrets (ARC runner) are optional — enable_github_apps=false on
# fresh deploys where Apps haven't been registered yet.
if [ "$DEPLOY_FACTORY" = true ]; then
  step "Step 10/12: Deploy agent-factory"
  bash "$SCRIPT_DIR/build-agent-factory-lambdas.sh"

  # Agent factory infra runs directly — no CodeBuild needed.
  cd "$ROOT_DIR/modules/agent-factory/infra"
  BACKEND_FILE="$ROOT_DIR/environments/$ENVIRONMENT/modules/agent-factory-backend.tfvars"
  [ ! -f "$BACKEND_FILE" ] && cat > "$BACKEND_FILE" << EOF
bucket         = "${STATE_BUCKET}"
key            = "${ENVIRONMENT}/modules/agent-factory/terraform.tfstate"
region         = "${AWS_REGION}"
encrypt        = true
dynamodb_table = "${LOCK_TABLE}"
EOF
  if [ "$UPDATE_MODE" = false ]; then
  # Detect current state to set conditional flags
  _GH_APPS_EXIST=false
  if aws secretsmanager describe-secret --secret-id "adp/${ADP_GITHUB_ORG:-aws-e}/gh-app-dev-id" --region "$AWS_REGION" &>/dev/null; then
    _GH_APPS_EXIST=true
  fi
  _AC_NS_EXISTS=false
  if kubectl get namespace agent-context &>/dev/null; then
    _AC_NS_EXISTS=true
  fi
  # Check if agent-registry already has the scaledjob-worker entry (from
  # gateway-infra seed or a prior partial apply). Skip seeding if present to
  # avoid ConditionalCheckFailedException on PutItem.
  _SEED_REGISTRY=true
  _REG_TABLE=$(aws ssm get-parameter --name "/adp/${ENVIRONMENT}/gateway/agent-registry-table" --query Parameter.Value --output text 2>/dev/null || echo "")
  if [ -n "$_REG_TABLE" ]; then
    if aws dynamodb get-item --table-name "$_REG_TABLE" --key '{"agent_id":{"S":"scaledjob-worker"}}' --query 'Item.agent_id' --output text 2>/dev/null | grep -q "scaledjob-worker"; then
      _SEED_REGISTRY=false
    fi
  fi
  # Always regenerate tfvars to reflect current state (gateway is deployed by
  # the time we reach this step; enable_github_apps tracks secret presence).
  cat > terraform.tfvars << EOF
environment              = "${ENVIRONMENT}"
aws_region               = "${AWS_REGION}"
github_org               = "${ADP_GITHUB_ORG:-aws-e}"
runner_namespace         = "arc-runners"
enable_github_apps       = ${_GH_APPS_EXIST}
enable_agent_context_rbac = ${_AC_NS_EXISTS}
seed_agent_registry      = ${_SEED_REGISTRY}
gateway_deployed         = true
EOF
  else
    FACTORY_VAR_FILE="$UPGRADE_RUN_DIR/agent-factory.tfvars.json"
  fi
  terraform init -backend-config="$BACKEND_FILE" -input=false
  if [ "$UPDATE_MODE" = true ]; then
    terraform_update_apply "agent-factory" "$FACTORY_VAR_FILE"
  else
    terraform apply -var-file=terraform.tfvars -auto-approve
    ok "Agent-factory deployed"
  fi

  if [ "$DEPLOY_GATEWAY" = true ] && [ "$ENVIRONMENT" = "dev" ]; then
    COGNITO_PENTEST_CLIENT_ID=$(terraform output -raw pentest_actor_client_id 2>/dev/null || echo "")
    if [ -z "$COGNITO_PENTEST_CLIENT_ID" ] || [ "$COGNITO_PENTEST_CLIENT_ID" = "None" ]; then
      fail "Agent-factory deployed without publishing the dev pentest Cognito client ID"
    fi
    PENTEST_CLIENT_PATCH=$(PENTEST_CLIENT_ID="$COGNITO_PENTEST_CLIENT_ID" python3 -c \
      'import json, os; print(json.dumps({"data": {"BG_COGNITO_PENTEST_CLIENT_ID": os.environ["PENTEST_CLIENT_ID"]}}))')
    kubectl patch configmap bedrockgateway-config -n adp-gateway \
      --type merge --patch "$PENTEST_CLIENT_PATCH"
    kubectl rollout restart deployment/bedrockgateway -n adp-gateway
    kubectl rollout status deployment/bedrockgateway -n adp-gateway --timeout=300s \
      || fail "Gateway rollout failed after adding the dev pentest Cognito client"
    ok "Gateway reconciled with the dev pentest Cognito client"
  fi

  # --- Agent Gateway build + deploy (part of agent-factory) ---
  step "Step 10b/12: Build and deploy agent gateway"

  # --- Docker build: use CodeBuild (needs privileged mode) or local Docker ---
  if [ "$LOCAL_MODE" = true ] && docker info &>/dev/null 2>&1; then
    SOURCE_SHA="$SOURCE_SHA" REGISTRY="$REGISTRY" AWS_REGION="$AWS_REGION" \
      bash "$ROOT_DIR/platform/scripts/publish-local-image.sh" adp-agent-gateway
  else
    # Docker build via CodeBuild (Terraform-managed project)
    run_codebuild "adp-${ENVIRONMENT}-agent-gateway" "codebuild/bs-agent-gateway.yml"
  fi
  AGENT_IMAGE=$(python3 "$ROOT_DIR/platform/scripts/resolve-ecr-image.py" \
    "${ADP_RELEASE_AGENT_GATEWAY_IMAGE:-$REGISTRY/adp-agent-gateway:$IMAGE_TAG}") \
    || fail "Agent gateway release digest could not be verified"

  # --- K8s deploy: runs directly (no CodeBuild needed) ---
  cd "$ROOT_DIR/modules/agent-factory"
  kubectl create namespace adp-gateway-agents --dry-run=client -o yaml | kubectl apply -f -
  INPUT_QUEUE_URL=$(cd infra && terraform output -raw gateway_input_queue_url)
  RESPONSE_QUEUE_URL=$(cd infra && terraform output -raw gateway_response_queue_url)
  SESSIONS_TABLE=$(cd infra && terraform output -raw gateway_sessions_table)
  sed -e "s|REPLACE_WITH_INPUT_QUEUE_URL|${INPUT_QUEUE_URL}|g" \
      -e "s|REPLACE_WITH_RESPONSE_QUEUE_URL|${RESPONSE_QUEUE_URL}|g" \
      -e "s|REPLACE_WITH_SESSIONS_TABLE_NAME|${SESSIONS_TABLE}|g" \
      -e "s|REPLACE_WITH_AGENT_IMAGE|${AGENT_IMAGE}|g" \
      gateway/k8s/keda-scaledjob.yaml | kubectl apply -f -

  kubectl wait --for=condition=Ready scaledjob/agent-gateway-worker -n adp-gateway-agents --timeout=300s \
    || fail "Agent gateway ScaledJob is not ready"
  DEPLOYED_AGENT_IMAGE=$(kubectl get scaledjob agent-gateway-worker -n adp-gateway-agents \
    -o jsonpath='{.spec.jobTargetRef.template.spec.containers[0].image}')
  [ "$DEPLOYED_AGENT_IMAGE" = "$AGENT_IMAGE" ] || fail "Agent gateway ScaledJob is not using the intended release"
  if [ "$UPDATE_MODE" = true ]; then
    ok "Agent gateway deployed (SHA: $IMAGE_TAG)"
  else
    ok "Agent gateway deployed"
    warn "Store GitHub App creds in Secrets Manager (see modules/agent-factory/SETUP-GUIDE.md)"
  fi

  # The WebSocket ingest Lambda sends to the chat FIFO queue. Its TypeScript
  # consumer has its own repository, so both images use the full source SHA
  # without a suffix that would violate the shared publication contract.
  step "Step 10c/12: Build and deploy chat agent"
  if [ "$LOCAL_MODE" = true ] && docker info &>/dev/null 2>&1; then
    SOURCE_SHA="$SOURCE_SHA" REGISTRY="$REGISTRY" AWS_REGION="$AWS_REGION" \
      bash "$ROOT_DIR/platform/scripts/publish-local-image.sh" adp-chat-agent
  else
    run_codebuild "adp-${ENVIRONMENT}-chat-agent" "codebuild/bs-chat-agent.yml"
  fi
  CHAT_IMAGE=$(python3 "$ROOT_DIR/platform/scripts/resolve-ecr-image.py" \
    "${ADP_RELEASE_CHAT_AGENT_IMAGE:-$REGISTRY/adp-chat-agent:$IMAGE_TAG}") \
    || fail "Chat agent release digest could not be verified"
  ENVIRONMENT="$ENVIRONMENT" AWS_REGION="$AWS_REGION" STATE_BUCKET="$STATE_BUCKET" \
    AGENT_IMAGE="$CHAT_IMAGE" \
    bash "$ROOT_DIR/modules/agent-factory/agent/k8s/deploy-chat-scaledjob.sh"
  ok "Chat agent deployed (SHA: $IMAGE_TAG)"
else
  step "Step 10/12: Skipping agent-factory"
fi

refresh_credentials
# =============================================================================
# Step 11/12: Agent Context (optional — gated by AGENT_CONTEXT_ENABLED or --agent-context-only)
# =============================================================================

if [ "$DEPLOY_AGENT_CONTEXT" = true ]; then
  step "Step 11/12: Deploy agent-context"

  # Agent context runs directly — no CodeBuild needed.
  cd "$ROOT_DIR/modules/agent-context/terraform"
  BACKEND_FILE="$ROOT_DIR/environments/$ENVIRONMENT/modules/agent-context-backend.tfvars"
  [ ! -f "$BACKEND_FILE" ] && cat > "$BACKEND_FILE" << EOF
bucket         = "${STATE_BUCKET}"
key            = "${ENVIRONMENT}/modules/agent-context/terraform.tfstate"
region         = "${AWS_REGION}"
encrypt        = true
dynamodb_table = "${LOCK_TABLE}"
EOF
  terraform init -backend-config="$BACKEND_FILE" -input=false
  if [ "$UPDATE_MODE" = true ]; then
    terraform_update_apply "agent-context" "$ROOT_DIR/environments/$ENVIRONMENT/modules/agent-context.tfvars"
  else
    terraform apply -var-file="$ROOT_DIR/environments/$ENVIRONMENT/modules/agent-context.tfvars" -auto-approve
    ok "Agent-context infrastructure deployed"
  fi

  # Deploy k8s manifests
  cd "$ROOT_DIR/modules/agent-context"
  if [ "$UPDATE_MODE" = true ]; then
    bash deploy.sh --skip-terraform
  else
    bash deploy.sh --skip-validate
  fi
  ok "Agent-context deployed"
else
  step "Step 11/12: Skipping agent-context (set AGENT_CONTEXT_ENABLED=true or use --agent-context-only)"
fi

refresh_credentials
# =============================================================================
# Step 12/12: Superplane domain app (optional — gated by SUPERPLANE_ENABLED or
# --superplane-only)
#
# LAST in the deploy order, and first in undeploy's PHASE_ORDER. A domain app sits on
# top of the platform, the gateway and the agent runtime, so it deploys after all of
# them and is destroyed before any of them (Issue #5037).
#
# Registered here deliberately: `modules/domain-apps/cyber/` is absent from this script
# entirely, which is why its resources survive teardown. That is the failure mode this
# phase exists not to repeat.
# =============================================================================
DEPLOY_SUPERPLANE=false
if [ "$SUPERPLANE_ONLY" = true ]; then
  DEPLOY_SUPERPLANE=true
elif [ "$GATEWAY_ONLY" = true ] || [ "$AGENT_FACTORY_ONLY" = true ] || [ "$AGENT_CONTEXT_ONLY" = true ] || [ "$SKIP_SUPERPLANE" = true ]; then
  DEPLOY_SUPERPLANE=false
elif [ "$SUPERPLANE_ENABLED" = true ]; then
  DEPLOY_SUPERPLANE=true
fi

if [ "$DEPLOY_SUPERPLANE" = true ]; then
  step "Step 12/12: Deploy superplane domain app"

  # The module's Terraform belongs to a later unit (U3). Until it lands there is
  # nothing to apply, so this phase reports that plainly and succeeds rather than
  # failing an otherwise healthy deploy. When U3 adds infra/control-plane/*.tf the
  # apply below starts doing work with no further edit to this script.
  SUPERPLANE_TF_DIR="$ROOT_DIR/modules/domain-apps/superplane/infra/control-plane"
  if ! ls "$SUPERPLANE_TF_DIR"/*.tf >/dev/null 2>&1; then
    warn "Superplane: no Terraform in $SUPERPLANE_TF_DIR yet — skipping infrastructure apply"
    ok "Superplane: nothing to deploy (module skeleton only)"
  else
    cd "$SUPERPLANE_TF_DIR"
    BACKEND_FILE="$ROOT_DIR/environments/$ENVIRONMENT/modules/superplane-backend.tfvars"
    [ ! -f "$BACKEND_FILE" ] && cat > "$BACKEND_FILE" << EOF
bucket         = "${STATE_BUCKET}"
key            = "${ENVIRONMENT}/modules/superplane/terraform.tfstate"
region         = "${AWS_REGION}"
encrypt        = true
dynamodb_table = "${LOCK_TABLE}"
EOF
    terraform init -backend-config="$BACKEND_FILE" -input=false
    if [ "$UPDATE_MODE" = true ]; then
      terraform_update_apply "superplane" "$ROOT_DIR/environments/$ENVIRONMENT/modules/superplane.tfvars"
    else
      terraform apply -var-file="$ROOT_DIR/environments/$ENVIRONMENT/modules/superplane.tfvars" -auto-approve
      ok "Superplane infrastructure deployed"
    fi
  fi
else
  step "Step 12/12: Skipping superplane (set SUPERPLANE_ENABLED=true or use --superplane-only)"
fi

# Finalize after all installed modules have been updated.
if [ "$UPDATE_MODE" = true ]; then
  step "Finalize network-policy enforcement"
  python3 "$SCRIPT_DIR/upgrade-network.py" audit
  cd "$ROOT_DIR/platform/infra"
  terraform_update_apply platform "../../environments/$ENVIRONMENT/platform.tfvars"
  if [ "$DEPLOY_GATEWAY" = true ]; then
    step "Reconcile gateway after ALB/controller changes"
    cd "$ROOT_DIR/modules/gateway/infra"
    gateway_alb_vars
    terraform_update_apply gateway-final "$GATEWAY_UPDATE_VAR_FILE" "${GATEWAY_ALB_ARGS[@]}"
    UPGRADE_CHECK_ONLY=true terraform_update_apply gateway-final "$GATEWAY_UPDATE_VAR_FILE" "${GATEWAY_ALB_ARGS[@]}"
  fi
fi

if [ "$SKIP_FRONTEND" = false ] && [ "$DEPLOY_GATEWAY" = true ]; then
  step "Step 6/12: Publish frontend and both account-connection templates"
  bash "$ROOT_DIR/modules/gateway/scripts/deploy-frontend.sh" --env "$ENVIRONMENT" --region "$AWS_REGION"
else
  step "Step 6/12: Skipping frontend"
fi

if [ "$UPDATE_MODE" = true ]; then
  REQUIRED_MODULE_ARGS=()
  [ "$DEPLOY_FACTORY" != true ] || REQUIRED_MODULE_ARGS+=(--require-module agent-factory)
  python3 "$SCRIPT_DIR/upgrade-state.py" verify --directory "$UPGRADE_RUN_DIR" --region "$AWS_REGION" \
    ${REQUIRED_MODULE_ARGS[@]+"${REQUIRED_MODULE_ARGS[@]}"}
  if [ "$DEPLOY_GATEWAY" = true ]; then
    CF_DOMAIN=$(aws ssm get-parameter --name "/adp/$ENVIRONMENT/gateway/cloudfront-domain" --query Parameter.Value --output text)
    curl --fail --silent --show-error --retry 5 --retry-all-errors "https://$CF_DOMAIN/api/health" \
      | python3 -c 'import json,sys; assert json.load(sys.stdin).get("status")=="healthy", "CDN API is unhealthy"'
  fi
fi

if [ "$CI_MODE" = false ] && [ "${ADP_BEDROCK_VERIFY_DEFERRED:-false}" != true ]; then
  step "Verify default Bedrock model invocations"
  bash "$SCRIPT_DIR/enable-bedrock-models.sh" --verify || fail "Default model invocation failed."
fi

# =============================================================================
# Summary
# =============================================================================
step "Deployment complete"

echo "Platform:  $EKS_CLUSTER"
echo "Gateway:   kubectl get pods -n adp-gateway (configure kubectl: aws eks update-kubeconfig --name $EKS_CLUSTER --region $AWS_REGION)"

CF_DOMAIN=$(aws ssm get-parameter --name "/adp/$ENVIRONMENT/gateway/cloudfront-domain" --query "Parameter.Value" --output text 2>/dev/null) || true
[ -n "$CF_DOMAIN" ] && [ "$CF_DOMAIN" != "None" ] && echo "Frontend:  https://${CF_DOMAIN}" && echo "API:       https://${CF_DOMAIN}/api/health"
[ "$GATEWAY_ONLY" = false ] && [ "$SUPERPLANE_ONLY" = false ] && echo "Agents:    kubectl get pods -n arc-runners"
[ "$DEPLOY_AGENT_CONTEXT" = true ] && echo "Context:   kubectl get pods -n agent-context"
GW_WS=""
if [ "$SUPERPLANE_ONLY" = false ]; then
  GW_WS=$(cd "$ROOT_DIR/modules/agent-factory/infra" && terraform output -raw gateway_ws_endpoint 2>/dev/null) || true
fi
[ -n "$GW_WS" ] && [ "$GW_WS" != "" ] && echo "AgentGW:   $GW_WS"

if [ -n "$CF_DOMAIN" ] && [ "$CF_DOMAIN" != "None" ]; then
  echo "CLI installation (no sign-in required):"
  echo "  curl -fsSL https://${CF_DOMAIN}/api/cli/install.sh | sh -s -- --gateway-url https://${CF_DOMAIN}/api"
  echo '  "$HOME/.adp/bin/adp" admin setup'
  echo "Sign in with the bootstrap Cognito administrator account; GitHub is not needed for CLI admin login."
fi

# --- Next steps (manual — GitHub App wiring; skipped in update mode) ---
if [ "$UPDATE_MODE" = false ] && [ "$GATEWAY_ONLY" = false ] && [ "$AGENT_CONTEXT_ONLY" = false ] && [ "$SUPERPLANE_ONLY" = false ]; then
  echo ""
  echo "━━━ Next steps (manual) ━━━"
  echo "To complete the agent path, wire a GitHub App:"
  if [ -n "$CF_DOMAIN" ] && [ "$CF_DOMAIN" != "None" ]; then
    echo "  1. Log in as platform_admin at https://${CF_DOMAIN}"
  else
    echo "  1. Log in as platform_admin"
  fi
  echo "     → Settings → Connections → 'Set up GitHub App'"
  echo "  2. Or CLI fallback:"
  echo "     modules/agent-factory/webhook-ingress/scripts/register-github-app.sh <org> --env $ENVIRONMENT"
  echo "  3. Install the App on target repo(s)"
  echo "  4. Comment '@agent-developer <task>' on an issue to trigger an agent"
  echo ""
  echo "Admin credentials location: Secrets Manager → adp/$ENVIRONMENT/gateway/test-admin-credentials"
fi

echo ""
echo "To destroy (legacy): $0 --destroy"
echo "To destroy (recommended): ./platform/scripts/undeploy.sh"
