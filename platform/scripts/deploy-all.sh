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
source "${SCRIPT_DIR}/load-deploy-config.sh"

AWS_REGION="$ADP_REGION"
ENVIRONMENT="$ADP_ENVIRONMENT"
GATEWAY_ONLY=false
AGENT_FACTORY_ONLY=false
AGENT_CONTEXT_ONLY=false
SKIP_AGENT_CONTEXT=false
AGENT_CONTEXT_ENABLED="${AGENT_CONTEXT_ENABLED:-false}"
DESTROY=false
SKIP_FRONTEND=false
SKIP_BROKER=false
SKIP_ADMIN_BOOTSTRAP=false
SKIP_WEBHOOK_INGRESS=false
LOCAL_MODE=false
CI_MODE=false
UPDATE_MODE=false
CONFIRM_DESTRUCTIVE=false

for arg in "$@"; do
  case $arg in
    --gateway-only) GATEWAY_ONLY=true ;;
    --agent-factory-only) AGENT_FACTORY_ONLY=true ;;
    --agent-context-only) AGENT_CONTEXT_ONLY=true ;;
    --skip-agent-context) SKIP_AGENT_CONTEXT=true ;;
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
      echo "Scope:"
      echo "  --gateway-only         Platform + gateway only"
      echo "  --agent-factory-only   Platform + agent-factory only"
      echo "  --agent-context-only   Platform + agent-context only"
      echo ""
      echo "Skip:"
      echo "  --skip-frontend        Skip frontend build and deploy"
      echo "  --skip-broker          Skip broker Lambda deploy"
      echo "  --skip-admin-bootstrap Skip first-admin DB seeding"
      echo "  --skip-webhook-ingress Skip webhook-ingress stack"
      echo "  --skip-agent-context   Skip agent-context even if AGENT_CONTEXT_ENABLED=true"
      echo ""
      echo "Build:"
      echo "  --local                Use local Docker for image builds (instead of CodeBuild)"
      exit 0
      ;;
  esac
done

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
step() { echo -e "\n${BLUE}━━━ $1 ━━━${NC}\n"; }
ok()   { echo -e "${GREEN}✓ $1${NC}"; }
warn() { echo -e "${YELLOW}⚠ $1${NC}"; }
fail() { echo -e "${RED}✗ $1${NC}"; exit 1; }

# =============================================================================
# Mutual exclusion checks
# =============================================================================
if [ "$UPDATE_MODE" = true ] && [ "$DESTROY" = true ]; then
  fail "--update and --destroy are mutually exclusive"
fi
if [ "$UPDATE_MODE" = true ] && [ "$CI_MODE" = true ]; then
  fail "--update and --ci are mutually exclusive"
fi

# =============================================================================
# Helper: terraform_update_apply — plan-gated apply for update mode (§4)
# =============================================================================
# In update mode, every terraform apply is replaced with a plan-first gate that
# refuses to apply if the plan includes resource destroys (unless
# --confirm-destructive was passed). This prevents silent destruction of live
# resources from TF drift.
terraform_update_apply() {
  local MODULE_NAME="$1"
  local VAR_FILE="$2"
  shift 2
  local EXTRA_VARS=("${@}")

  # 1. Plan to a file (captures the plan for inspection)
  local PLAN_FILE="/tmp/tf-plan-${MODULE_NAME}-$$.tfplan"
  local PLAN_OUTPUT="/tmp/tf-plan-${MODULE_NAME}-$$.txt"

  # Disable pipefail around the plan command: terraform plan -detailed-exitcode
  # returns exit code 2 when changes are present (the expected case for update
  # mode). With pipefail, the pipeline's exit code would be 2, and set -e would
  # terminate the script before PIPESTATUS can be captured.
  set +o pipefail
  if [ ${#EXTRA_VARS[@]} -gt 0 ]; then
    terraform plan \
      -var-file="$VAR_FILE" \
      "${EXTRA_VARS[@]}" \
      -out="$PLAN_FILE" \
      -detailed-exitcode 2>&1 | tee "$PLAN_OUTPUT"
  else
    terraform plan \
      -var-file="$VAR_FILE" \
      -out="$PLAN_FILE" \
      -detailed-exitcode 2>&1 | tee "$PLAN_OUTPUT"
  fi
  local EXIT_CODE=${PIPESTATUS[0]}
  set -o pipefail

  # Exit code: 0 = no changes, 1 = error, 2 = changes present
  if [ "$EXIT_CODE" -eq 0 ]; then
    ok "$MODULE_NAME: no changes"
    rm -f "$PLAN_FILE" "$PLAN_OUTPUT"
    return 0
  elif [ "$EXIT_CODE" -eq 1 ]; then
    rm -f "$PLAN_FILE" "$PLAN_OUTPUT"
    fail "$MODULE_NAME: terraform plan failed"
  fi

  # 2. Check for destroys
  DESTROYS=$(grep -c 'will be destroyed' "$PLAN_OUTPUT" 2>/dev/null) || DESTROYS=0

  if [ "$DESTROYS" -gt 0 ]; then
    echo ""
    echo -e "${RED}━━━ DESTROY GATE ━━━${NC}"
    echo -e "${RED}$MODULE_NAME plan includes $DESTROYS resource(s) to be destroyed:${NC}"
    echo ""
    grep 'will be destroyed' "$PLAN_OUTPUT"
    echo ""

    if [ "$CONFIRM_DESTRUCTIVE" = true ]; then
      warn "Operator confirmed destructive apply (--confirm-destructive). Proceeding."
    else
      echo "To authorize this apply, re-run with --confirm-destructive."
      echo "To inspect the full plan: cat $PLAN_OUTPUT"
      echo ""
      echo "Known drift issues to check:"
      echo "  - agent-context/Neptune: full apply may want to destroy 9 Neptune resources (Issue #2769)"
      echo "  - gateway/kms:Decrypt policy: missing 'moved' block causes destroy+recreate (Issue #2909)"
      echo "  - platform/EKS access entries: role ARN format drift between CI and manual applies"
      echo ""
      fail "Refusing to apply $MODULE_NAME plan with $DESTROYS destroy(s). Pass --confirm-destructive to override."
    fi
  fi

  # 3. Apply the saved plan (no -auto-approve needed — plan file is pre-approved)
  terraform apply "$PLAN_FILE"
  ok "$MODULE_NAME: applied successfully"

  # Cleanup
  rm -f "$PLAN_FILE" "$PLAN_OUTPUT"
}

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
if [ -f "$ROOT_DIR/config/deployment.yml" ] && [ -n "$ADP_ACCOUNT_ID" ] && [ "$ADP_ACCOUNT_ID" != "$ACCOUNT_ID" ]; then
  fail "config/deployment.yml says account_id=$ADP_ACCOUNT_ID but caller resolves to $ACCOUNT_ID. Either fix config/deployment.yml or switch AWS_PROFILE."
fi

ok "AWS Account: $ACCOUNT_ID | Region: $AWS_REGION | Env: $ENVIRONMENT"

# ---------------------------------------------------------------------------
# Accept Bedrock marketplace agreements for the Claude models the platform
# invokes. Fresh accounts have none; without them every model call fails with
# AccessDeniedException and the agent-worker misreports it as "no changes
# needed". Idempotent — skips models already enabled.
# ---------------------------------------------------------------------------
if [ "$UPDATE_MODE" = true ]; then
  step "Bedrock model access (skipped — update mode)"
else
  step "Bedrock model access"
  bash "$SCRIPT_DIR/enable-bedrock-models.sh" || fail "Bedrock model agreements could not be enabled. Agents cannot invoke Claude without them."
fi

# ---------------------------------------------------------------------------
# Detect operator's public IP and lock EKS public API to /32 (portable)
# ---------------------------------------------------------------------------
# Anyone cloning this repo can run the script without editing tfvars. The
# detected IP is exported as TF_VAR_eks_public_access_cidrs so Terraform picks
# it up. If the caller already set the env var, respect it.
if [ -z "${TF_VAR_eks_public_access_cidrs:-}" ]; then
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
  ok "EKS public API CIDRs: ${TF_VAR_eks_public_access_cidrs} (from env)"
fi

REGISTRY="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
STATE_BUCKET="adp-terraform-state-${ACCOUNT_ID}"
LOCK_TABLE="adp-terraform-locks"
EKS_CLUSTER="adp-${ENVIRONMENT}-eks-cluster"
CB_ROLE_NAME="adp-${ENVIRONMENT}-codebuild-role"

# =============================================================================
# Update mode: precondition checks (§1)
# =============================================================================
if [ "$UPDATE_MODE" = true ]; then
  step "Update mode: precondition checks"

  # 1. State bucket must exist
  aws s3api head-bucket --bucket "$STATE_BUCKET" 2>/dev/null \
    || fail "--update requires an existing deployment. State bucket '$STATE_BUCKET' not found."

  # 2. EKS cluster must exist and be ACTIVE
  CLUSTER_STATUS=$(aws eks describe-cluster --name "$EKS_CLUSTER" \
    --query 'cluster.status' --output text 2>/dev/null) || true
  [ "$CLUSTER_STATUS" = "ACTIVE" ] \
    || fail "--update requires a running EKS cluster. Got status: ${CLUSTER_STATUS:-not found}"

  # 3. Gateway namespace must exist (indicates prior deploy)
  kubectl get namespace adp-gateway &>/dev/null \
    || fail "--update requires prior gateway deployment. Namespace 'adp-gateway' not found."

  ok "Preconditions met: state bucket exists, EKS ACTIVE, adp-gateway namespace present"

  # Determine source SHA for image tagging (§2)
  SOURCE_SHA=$(git rev-parse --short=12 HEAD 2>/dev/null || echo "manual-$(date +%s)")
  export IMAGE_TAG="$SOURCE_SHA"
  ok "Image tag for this update: $IMAGE_TAG"
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
# Helper: check if the webhook-secrets KMS alias exists
# =============================================================================
# Issue #3419: The enable_webhook_secrets_kms_grant flag in gateway.tfvars
# references a KMS alias created by webhook-ingress (Step 9). On a fresh
# account Step 3 runs BEFORE Step 9, so the alias doesn't exist yet and the
# data source hard-fails at plan time. This helper returns "true" (re-deploy)
# or "false" (fresh deploy) so we can override the tfvars value safely.
webhook_kms_grant_value() {
  if aws kms describe-key --key-id "alias/adp-${ENVIRONMENT}-webhook-secrets" \
       --region "$AWS_REGION" &>/dev/null; then
    echo "true"
  else
    echo "false"
  fi
}

# =============================================================================
# Helper: ensure CodeBuild IAM role exists
# =============================================================================
# The role and the 4 docker-build projects are Terraform-managed in
# platform/infra/modules/codebuild/. This helper only validates the role
# exists (it should after platform infra apply). If missing (bootstrap
# chicken-and-egg), it creates it imperatively as a fallback.
ensure_codebuild_role() {
  if aws iam get-role --role-name "$CB_ROLE_NAME" 2>/dev/null > /dev/null; then return; fi
  echo "Creating CodeBuild service role (bootstrap fallback)..."
  aws iam create-role --role-name "$CB_ROLE_NAME" \
    --assume-role-policy-document '{
      "Version":"2012-10-17",
      "Statement":[{"Effect":"Allow","Principal":{"Service":"codebuild.amazonaws.com"},"Action":"sts:AssumeRole"}]
    }' > /dev/null
  aws iam attach-role-policy --role-name "$CB_ROLE_NAME" \
    --policy-arn "arn:aws:iam::aws:policy/AdministratorAccess"
  echo "Waiting for IAM propagation..."
  sleep 15
  ok "CodeBuild role created with AdministratorAccess"
}

# =============================================================================
# Helper: run a CodeBuild job (project must already exist via Terraform)
# =============================================================================
# Uses codebuild-run.sh which uploads source to a per-build-unique S3 key
# and passes --source-location-override, eliminating the shared-key race.
run_codebuild() {
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
  # In update mode, forward IMAGE_TAG so CodeBuild pushes the SHA-tagged image
  # (without this, only :latest is pushed and kubectl set image :<sha> fails).
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
# Order: agent-context → webhook-ingress → agent-factory → gateway → platform
# State backend (S3 + DynamoDB) is NOT destroyed — use bootstrap-destroy.sh.
# GitHub App secrets (adp/gh-app-*) are NOT touched — survive by design.
# =============================================================================
if [ "$DESTROY" = true ]; then
  step "Destroying all infrastructure"
  echo "This will destroy ALL ADP infrastructure in $ENVIRONMENT."
  echo ""
  echo "Destroy order: agent-context → webhook-ingress → agent-factory → gateway → platform"
  echo "State backend and GitHub App secrets will NOT be deleted."
  echo ""
  echo "Type 'yes' to proceed:"
  read -r confirm
  [ "$confirm" = "yes" ] || { echo "Aborted."; exit 0; }

  # Configure kubectl for K8s cleanup steps
  if command -v kubectl >/dev/null 2>&1; then
    aws eks update-kubeconfig --name "$EKS_CLUSTER" --region "$AWS_REGION" 2>/dev/null || true
  fi

  # -------------------------------------------------------------------------
  # 1. Agent Context
  # -------------------------------------------------------------------------
  step "Destroy 1/5: Agent Context"
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
  # 2. Webhook Ingress (KEDA + Lambda + SQS + API GW + DynamoDB + WAFv2 + KMS)
  # -------------------------------------------------------------------------
  step "Destroy 2/5: Webhook Ingress"
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
  # 3. Agent Factory
  # -------------------------------------------------------------------------
  step "Destroy 3/5: Agent Factory"
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
  # 4. Gateway (most complex — ALB, S3, Secrets, CloudFront cleanup first)
  # -------------------------------------------------------------------------
  step "Destroy 4/5: Gateway"

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
  terraform init -backend-config="../../../environments/$ENVIRONMENT/modules/gateway-backend.tfvars" -input=false 2>/dev/null || true
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
  # 5. Platform (last — EKS, VPC, ECR, IAM)
  # -------------------------------------------------------------------------
  step "Destroy 5/5: Platform"

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
  terraform init -backend-config="../../environments/$ENVIRONMENT/backend.tfvars" -input=false 2>/dev/null || true
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
  if [ "$AGENT_FACTORY_ONLY" = false ] && [ "$AGENT_CONTEXT_ONLY" = false ]; then
    ci_check_module "gateway"       "${ENVIRONMENT}/modules/gateway/terraform.tfstate"      "gateway-infra-apply.yml"
  fi
  if [ "$GATEWAY_ONLY" = false ] && [ "$AGENT_CONTEXT_ONLY" = false ]; then
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
  step "Step 1/11: Bootstrap (skipped — update mode)"
  ok "State bucket verified in preconditions: $STATE_BUCKET"
else
  step "Step 1/11: Bootstrap Terraform state backend"

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

  # Replace ACCOUNT_ID placeholders
  find "$ROOT_DIR/environments/" -name "*.tfvars" 2>/dev/null | while read f; do
    if grep -q "ACCOUNT_ID" "$f" 2>/dev/null; then
      sed -i '' "s/ACCOUNT_ID/${ACCOUNT_ID}/g" "$f" 2>/dev/null || sed -i "s/ACCOUNT_ID/${ACCOUNT_ID}/g" "$f"
    fi
  done
  ok "Environment configs updated"
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
step "Step 2/11: Deploy shared platform (VPC, EKS, ECR, IAM)"

# Platform infra runs directly (Terraform + kubectl) — no CodeBuild needed.
cd "$ROOT_DIR/platform/infra"
terraform init -backend-config="../../environments/$ENVIRONMENT/backend.tfvars" -input=false
if [ "$UPDATE_MODE" = true ]; then
  terraform_update_apply "platform" "../../environments/$ENVIRONMENT/platform.tfvars"
else
  terraform apply -var-file="../../environments/$ENVIRONMENT/platform.tfvars" -auto-approve
  ok "Platform deployed"
fi

# Configure kubectl (needed for k8s steps — local or CodeBuild deploy step)
if command -v kubectl >/dev/null 2>&1; then
  aws eks update-kubeconfig --name "$EKS_CLUSTER" --region "$AWS_REGION" 2>/dev/null || true
fi

refresh_credentials
# =============================================================================
# Step 3: Gateway infra
# =============================================================================
step "Step 3/11: Deploy gateway infrastructure"

if [ "$AGENT_FACTORY_ONLY" = true ] || [ "$AGENT_CONTEXT_ONLY" = true ]; then
  echo "Skipping gateway infra (--agent-factory-only or --agent-context-only)"
  ok "Skipped"
else
  # Ensure the GitHub OAuth secret exists when the auth broker is enabled.
  # gateway/infra reads adp/$ENVIRONMENT/cognito/github-oauth-credentials via a
  # data source at PLAN time — a missing secret aborts the apply. On fresh
  # accounts the secret won't exist yet (it's provisioned during GitHub App
  # setup), so we create a valid-schema placeholder that lets terraform proceed.
  # Real values are provisioned later by register-github-app or the UI flow.
  if grep -qE '^\s*enable_github_auth_broker\s*=\s*true' \
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

  # Gateway infra runs directly — no CodeBuild needed.
  cd "$ROOT_DIR/modules/gateway/infra"
  terraform init -backend-config="../../../environments/$ENVIRONMENT/modules/gateway-backend.tfvars" -input=false
  # Issue #3419: override enable_webhook_secrets_kms_grant based on whether the
  # KMS alias actually exists. On fresh deploys it won't (webhook-ingress is
  # Step 9); on re-deploys it will.
  KMS_GRANT=$(webhook_kms_grant_value)
  if [ "$UPDATE_MODE" = true ]; then
    terraform_update_apply "gateway" "../../../environments/$ENVIRONMENT/modules/gateway.tfvars" \
      -var "enable_webhook_secrets_kms_grant=$KMS_GRANT"
  else
    terraform apply -var-file="../../../environments/$ENVIRONMENT/modules/gateway.tfvars" \
      -var "enable_webhook_secrets_kms_grant=$KMS_GRANT" \
      -auto-approve
    ok "Gateway infrastructure deployed (enable_webhook_secrets_kms_grant=$KMS_GRANT)"
  fi
fi

refresh_credentials
# =============================================================================
# Step 4: Build + deploy gateway
# =============================================================================
step "Step 4/11: Build and deploy gateway"

if [ "$AGENT_FACTORY_ONLY" = true ] || [ "$AGENT_CONTEXT_ONLY" = true ]; then
  echo "Skipping gateway deploy (--agent-factory-only or --agent-context-only)"
  ok "Skipped"
else
  # ─── Migrations (update mode only — run BEFORE backend rollout, §3) ───
  if [ "$UPDATE_MODE" = true ]; then
    step "Step 4a/11: Run database migrations"

    # Find a running gateway pod (mirrors run-gateway-migrations.yml logic)
    NS="adp-gateway"
    POD=$(kubectl get pods -n "$NS" --field-selector=status.phase=Running \
      -o jsonpath='{.items[0].metadata.name}' 2>/dev/null) || true

    if [ -z "$POD" ]; then
      warn "No running gateway pod found. Skipping migrations (will run after rollout if pods start)."
    else
      echo "Running alembic migrations on $NS/$POD..."
      echo "=== Current revision ==="
      kubectl exec -n "$NS" "$POD" -- env PYTHONPATH=/app alembic current 2>&1 || true
      echo ""
      echo "=== Upgrading to head ==="
      kubectl exec -n "$NS" "$POD" -- env PYTHONPATH=/app alembic upgrade head \
        || fail "Alembic migration failed. Check: kubectl exec -n $NS $POD -- env PYTHONPATH=/app alembic history"
      echo ""
      echo "=== New revision ==="
      kubectl exec -n "$NS" "$POD" -- env PYTHONPATH=/app alembic current
      ok "Migrations complete"
    fi
  fi

  # --- Docker build: use CodeBuild (needs privileged mode) or local Docker ---
  if [ "$LOCAL_MODE" = true ] && docker info &>/dev/null 2>&1; then
    cd "$ROOT_DIR/modules/gateway"
    docker build -t adp-gateway .
    aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$REGISTRY"
    if [ "$UPDATE_MODE" = true ]; then
      docker tag adp-gateway:latest "$REGISTRY/adp-gateway:${IMAGE_TAG}"
      docker push "$REGISTRY/adp-gateway:${IMAGE_TAG}"
    fi
    docker tag adp-gateway:latest "$REGISTRY/adp-gateway:latest"
    docker push "$REGISTRY/adp-gateway:latest"
  else
    # Docker build via CodeBuild (Terraform-managed project)
    run_codebuild "adp-${ENVIRONMENT}-gateway-build" "codebuild/bs-gateway-build.yml"
  fi

  # --- K8s deploy: runs directly (no CodeBuild needed) ---
  cd "$ROOT_DIR/modules/gateway/infra"
  DB_HOST=$(terraform output -raw rds_endpoint 2>/dev/null | sed 's/:5432//' || echo "localhost")
  DB_NAME=$(terraform output -raw rds_database_name 2>/dev/null || echo "bedrockgateway")
  DB_USER="bgadmin"
  REDIS_HOST=$(terraform output -raw redis_endpoint 2>/dev/null || echo "localhost")
  REDIS_PORT=$(terraform output -raw redis_port 2>/dev/null || echo "6379")
  COGNITO_USER_POOL_ID=$(terraform output -raw cognito_user_pool_id 2>/dev/null || echo "")
  COGNITO_CLIENT_ID=$(terraform output -raw cognito_user_pool_client_id 2>/dev/null || echo "")
  COGNITO_DOMAIN=$(terraform output -raw cognito_domain 2>/dev/null || echo "")
  CF_DOMAIN=$(terraform output -raw frontend_cloudfront_domain_name 2>/dev/null || echo "")
  AGENT_REGISTRY_TABLE=$(terraform output -raw agent_registry_table_name 2>/dev/null || echo "")
  # Gateway IRSA role ARN lives in the platform layer; read it from IAM rather
  # than cross-layer terraform_remote_state. Deterministic given name_prefix.
  GATEWAY_ROLE_ARN=$(aws iam get-role --role-name "adp-${ENVIRONMENT}-role-gateway-service" --query 'Role.Arn' --output text 2>/dev/null || echo "")
  cd "$ROOT_DIR/modules/gateway"
  kubectl create namespace adp-gateway --dry-run=client -o yaml | kubectl apply -f -
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
  kubectl create secret generic bedrockgateway-secrets \
    --from-literal=token-secret-key="$TOKEN_SECRET" \
    -n adp-gateway --dry-run=client -o yaml | kubectl apply -f -
  sed -e "s|__AWS_REGION__|${AWS_REGION}|g" \
      -e "s|__ENVIRONMENT__|${ENVIRONMENT}|g" \
      -e "s|__DB_HOST__|${DB_HOST}|g" \
      -e "s|__DB_USER__|${DB_USER}|g" \
      -e "s|__DB_NAME__|${DB_NAME}|g" \
      -e "s|__REDIS_HOST__|${REDIS_HOST:-localhost}|g" \
      -e "s|__REDIS_PORT__|${REDIS_PORT:-6379}|g" \
      -e "s|__COGNITO_USER_POOL_ID__|${COGNITO_USER_POOL_ID}|g" \
      -e "s|__COGNITO_CLIENT_ID__|${COGNITO_CLIENT_ID}|g" \
      -e "s|__COGNITO_DOMAIN__|${COGNITO_DOMAIN}|g" \
      -e "s|__CORS_ALLOWED_ORIGINS__|https://${CF_DOMAIN},http://localhost:5173|g" \
      -e "s|__GATEWAY_BASE_URL__|https://${CF_DOMAIN}|g" \
      -e "s|__CFN_TEMPLATE_BUCKET__|$(terraform output -raw frontend_bucket_name 2>/dev/null || echo '')|g" \
      -e "s|__GATEWAY_ROLE_ARN__|${GATEWAY_ROLE_ARN}|g" \
      -e "s|__CHAT_LOGGING_ENABLED__|false|g" \
      -e "s|__CHAT_LOGGING_BUCKET__||g" \
      -e "s|__CHAT_LOGGING_SCRUB_LEVEL__|off|g" \
      -e "s|__TRUST_APIGW_HEADERS__|true|g" \
      -e "s|__AGENT_REGISTRY_TABLE__|${AGENT_REGISTRY_TABLE}|g" \
      -e "s|__ENFORCE_CREDENTIAL_BINDING__|false|g" \
      -e "s|__VAULT_PROXY_HOST_ALLOWLIST__||g" \
      -e "s|__INGESTION_QUEUE_URL__||g" \
      -e "s|__AGENT_RUN_LOGS_BUCKET__||g" \
      k8s/configmap.yaml | kubectl apply -f -
  # Render serviceaccount with the correct IRSA role ARN (Issue #1008)
  sed -e "s|__GATEWAY_IRSA_ROLE_ARN__|${GATEWAY_ROLE_ARN}|g" \
      k8s/serviceaccount.yaml | kubectl apply -f -
  for f in k8s/*.yaml; do
    case "$(basename "$f")" in
      configmap.yaml|serviceaccount.yaml|targetgroupbinding.yaml) continue ;;
      *) kubectl apply -f "$f" -n adp-gateway ;;
    esac
  done

  if [ "$UPDATE_MODE" = true ]; then
    # Update mode: SHA-tagged image + mandatory rollout + health check (§2, §8)
    CURRENT_IMAGE=$(kubectl get deployment/bedrockgateway -n adp-gateway \
      -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || echo "")
    if [ "$CURRENT_IMAGE" = "${REGISTRY}/adp-gateway:${IMAGE_TAG}" ]; then
      echo "Image tag unchanged. Forcing rollout restart..."
      kubectl rollout restart deployment/bedrockgateway -n adp-gateway
    else
      kubectl set image deployment/bedrockgateway \
        bedrockgateway="${REGISTRY}/adp-gateway:${IMAGE_TAG}" -n adp-gateway
    fi
    kubectl rollout status deployment/bedrockgateway -n adp-gateway --timeout=300s \
      || fail "Gateway rollout failed. Check: kubectl describe deployment/bedrockgateway -n adp-gateway"

    # Post-rollout health check (§2)
    sleep 5
    HEALTH=$(kubectl exec -n adp-gateway deploy/bedrockgateway -- \
      curl -sf http://localhost:8080/health 2>/dev/null) || true
    echo "$HEALTH" | grep -q '"status"' \
      || warn "Health check inconclusive. Verify: kubectl exec -n adp-gateway deploy/bedrockgateway -- curl http://localhost:8080/health"

    # Post-rollout migration (runs on the NEW pod which has updated alembic files).
    # The pre-rollout step (Step 4a) catches half-applied prior migrations but
    # cannot apply new revisions introduced by the update (old image lacks them).
    # This step mirrors CI's ordering: gateway-deploy.yml runs migrations AFTER
    # the new image is live (needs: [deploy-backend]).
    # Pick the NEWEST running gateway pod: right after rollout status returns,
    # old pods can still be Terminating (phase=Running), and an unordered pick
    # could exec into an old pod — silently no-oping the new revisions again.
    POST_POD=$(kubectl get pods -n adp-gateway -l app=bedrockgateway \
      --field-selector=status.phase=Running \
      --sort-by=.metadata.creationTimestamp \
      -o jsonpath='{.items[-1:].metadata.name}' 2>/dev/null) || true
    if [ -n "$POST_POD" ]; then
      echo "Running post-rollout migrations on new pod ($POST_POD)..."
      kubectl exec -n adp-gateway "$POST_POD" -- env PYTHONPATH=/app alembic upgrade head \
        || fail "Post-rollout alembic migration failed. The new image is live but schema may be stale. Check: kubectl exec -n adp-gateway $POST_POD -- env PYTHONPATH=/app alembic history"
      ok "Post-rollout migrations complete"
    else
      warn "No running pod found after rollout — cannot run post-rollout migrations"
    fi
  else
    # Fresh-deploy mode: :latest tag, non-fatal rollout check
    kubectl set image deployment/bedrockgateway bedrockgateway="${REGISTRY}/adp-gateway:latest" -n adp-gateway 2>/dev/null || true
    kubectl rollout status deployment/bedrockgateway -n adp-gateway --timeout=300s || warn "Rollout not complete"

    # Run database migrations (alembic upgrade head) — required for fresh deploys
    # where Terraform creates the RDS instance but doesn't populate the schema.
    # Idempotent on re-deploys (alembic tracks applied versions).
    echo "Running database migrations..."
    _GW_POD=$(kubectl get pods -n adp-gateway -l app=bedrockgateway --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
    if [ -n "$_GW_POD" ]; then
      kubectl exec -n adp-gateway "$_GW_POD" -- env PYTHONPATH=/app alembic upgrade head 2>&1 | tail -5
      ok "Database migrations applied"
    else
      warn "No running gateway pod found — skipping migrations (will retry at Step 8)"
    fi
  fi
fi
ok "Gateway deployed"

refresh_credentials
# =============================================================================
# Step 5/11: Discover internal ALB and wire to API Gateway + CloudFront
# =============================================================================
if [ "$AGENT_FACTORY_ONLY" = false ] && [ "$AGENT_CONTEXT_ONLY" = false ]; then
  step "Step 5/11: Wire internal ALB to API Gateway and CloudFront"

  # Discover ALB, cache to SSM, export ALB_ARN / ALB_DNS / ALB_SG_IDS.
  # The shared script exits 1 if the ALB is not found after 10 min; in
  # deploy-all.sh we downgrade that to a warning so the rest of the deploy
  # can continue (API Gateway will keep MOCK integrations).
  if bash "$SCRIPT_DIR/wire-gateway-alb.sh"; then
    # Source the exported variables into this shell (the script also writes
    # to SSM, but we need the values locally for the terraform re-apply).
    ALB_ARN=$(aws ssm get-parameter --name "/adp/$ENVIRONMENT/gateway/internal-alb-arn" --query "Parameter.Value" --output text --region "$AWS_REGION" 2>/dev/null || echo "")
    ALB_DNS=$(aws ssm get-parameter --name "/adp/$ENVIRONMENT/gateway/internal-alb-dns" --query "Parameter.Value" --output text --region "$AWS_REGION" 2>/dev/null || echo "")
    ALB_SG_IDS=$(aws ssm get-parameter --name "/adp/$ENVIRONMENT/gateway/internal-alb-security-group-ids" --query "Parameter.Value" --output text --region "$AWS_REGION" 2>/dev/null || echo "[]")
  else
    warn "ALB not found after 10 minutes. Skipping ALB wiring — API Gateway will use MOCK integration."
    ALB_ARN=""
    ALB_DNS=""
    ALB_SG_IDS="[]"
  fi

  # Re-apply gateway Terraform with ALB details to wire API Gateway VPC Link v2
  # (needs ARN for integration target, DNS for integration URI, SG IDs for egress)
  # and CloudFront VPC Origin (needs ARN only).
  if [ -n "$ALB_ARN" ] && [ "$ALB_ARN" != "None" ] && [ -n "$ALB_DNS" ]; then
    echo "Re-applying gateway Terraform with ALB details to wire API Gateway VPC Link v2 and CloudFront..."
    cd "$ROOT_DIR/modules/gateway/infra"
    # Issue #3419: re-check KMS alias existence for the second pass too.
    KMS_GRANT=$(webhook_kms_grant_value)
    if [ "$UPDATE_MODE" = true ]; then
      terraform_update_apply "gateway-alb-wire" "../../../environments/$ENVIRONMENT/modules/gateway.tfvars" \
        -var "internal_alb_arn=$ALB_ARN" \
        -var "internal_alb_dns=$ALB_DNS" \
        -var "alb_security_group_ids=$ALB_SG_IDS" \
        -var "enable_vpc_origin=true" \
        -var "enable_webhook_secrets_kms_grant=$KMS_GRANT"
    else
      terraform apply \
        -var-file="../../../environments/$ENVIRONMENT/modules/gateway.tfvars" \
        -var "internal_alb_arn=$ALB_ARN" \
        -var "internal_alb_dns=$ALB_DNS" \
        -var "alb_security_group_ids=$ALB_SG_IDS" \
        -var "enable_vpc_origin=true" \
        -var "enable_webhook_secrets_kms_grant=$KMS_GRANT" \
        -auto-approve
      ok "API Gateway VPC Link and CloudFront VPC Origin wired to ALB (enable_webhook_secrets_kms_grant=$KMS_GRANT)"
    fi
  fi
fi

refresh_credentials
# =============================================================================
# Step 5: Frontend
# =============================================================================
if [ "$SKIP_FRONTEND" = false ] && [ "$AGENT_FACTORY_ONLY" = false ] && [ "$AGENT_CONTEXT_ONLY" = false ]; then
  step "Step 6/11: Deploy frontend"

  # Frontend build runs directly (npm + aws s3 sync) — no CodeBuild needed.
  cd "$ROOT_DIR/modules/gateway/frontend"
  # Issue #3426: NODE_ENV=production (common in K8s pods) causes npm ci to skip
  # devDependencies (typescript, vite, etc.) needed for the build.
  _ORIG_NODE_ENV="${NODE_ENV:-}"
  export NODE_ENV=development
  npm ci
  export NODE_ENV="${_ORIG_NODE_ENV:-production}"
  # Build with the FULL VITE_* env from SSM (NOT just VITE_API_URL) — these are
  # baked into the bundle at build time; omitting them ships a broken app
  # ("GitHub sign-in is not configured" + Cognito unconfigured). Mirrors
  # gateway-deploy.yml.
  _ssm() { aws ssm get-parameter --name "$1" --query Parameter.Value --output text 2>/dev/null || echo ""; }
  VITE_API_URL="/api" \
  VITE_COGNITO_REGION="${AWS_REGION:-us-east-1}" \
  VITE_COGNITO_USER_POOL_ID="$(_ssm /adp/$ENVIRONMENT/gateway/cognito-user-pool-id)" \
  VITE_COGNITO_CLIENT_ID="$(_ssm /adp/$ENVIRONMENT/gateway/cognito-client-id)" \
  VITE_COGNITO_DOMAIN="$(_ssm /adp/$ENVIRONMENT/gateway/cognito-domain)" \
  VITE_GITHUB_AUTH_BROKER_URL="$(_ssm /adp/$ENVIRONMENT/gateway/github-auth-broker-url)" \
  VITE_AGENT_WS_URL="$(_ssm /adp/$ENVIRONMENT/gateway/agent-ws-url)" \
    npm run build
  BUCKET=$(aws ssm get-parameter --name "/adp/$ENVIRONMENT/gateway/frontend-bucket" --query "Parameter.Value" --output text 2>/dev/null) || true
  if [ -n "$BUCKET" ] && [ "$BUCKET" != "None" ]; then
    # Exclude cfn-templates/ so --delete doesn't wipe the CFN role template we
    # upload next.
    aws s3 sync dist/ "s3://${BUCKET}/" --delete --exclude "cfn-templates/*"
    # REQUIRED for the "Add AWS account" flow: the gateway pre-signs a GET for
    # this template; without it the flow fails "specified key does not exist".
    aws s3 cp "$ROOT_DIR/modules/gateway/src/auth/cfn_templates/aws_role_v1.yaml" \
      "s3://${BUCKET}/cfn-templates/aws_role_v1.yaml" --content-type text/yaml > /dev/null
    DIST_ID=$(aws ssm get-parameter --name "/adp/$ENVIRONMENT/gateway/cloudfront-id" --query "Parameter.Value" --output text 2>/dev/null) || true
    [ -n "$DIST_ID" ] && [ "$DIST_ID" != "None" ] && aws cloudfront create-invalidation --distribution-id "$DIST_ID" --paths "/*" > /dev/null
    ok "Frontend deployed (+ CFN template uploaded)"
  else
    warn "Frontend bucket not found in SSM"
  fi
else
  step "Step 6/11: Skipping frontend"
fi

refresh_credentials
# =============================================================================
# Step 7/11: Broker Lambda code
# =============================================================================
# deploy-broker.sh packages the real github-auth-broker Lambda code and updates
# the live Lambda (terraform ships a 503 placeholder). Required for GitHub login.
# Gateway-scope: runs unless --agent-factory-only or --agent-context-only.
if [ "$AGENT_FACTORY_ONLY" = false ] && [ "$AGENT_CONTEXT_ONLY" = false ] && [ "$SKIP_BROKER" = false ]; then
  step "Step 7/11: Deploy broker Lambda code"
  bash "$ROOT_DIR/modules/gateway/scripts/deploy-broker.sh" --env "$ENVIRONMENT" --region "$AWS_REGION"
  ok "Broker Lambda deployed"
elif [ "$SKIP_BROKER" = true ]; then
  step "Step 7/11: Skipping broker Lambda (--skip-broker)"
else
  step "Step 7/11: Skipping broker Lambda (scope exclusion)"
fi

refresh_credentials
# =============================================================================
# Step 8/11: Bootstrap first admin
# =============================================================================
# bootstrap-admin.sh seeds the first platform_admin's DB rows via kubectl exec.
# Without it, the onboarding gate shows "request access" for everyone. Requires
# the gateway pod to be healthy — we enforce a strict rollout gate here.
# Gateway-scope: runs unless --agent-factory-only or --agent-context-only.
if [ "$UPDATE_MODE" = true ]; then
  step "Step 8/11: Admin bootstrap (skipped — update mode)"
  ok "Admin already exists on live platform"
elif [ "$AGENT_FACTORY_ONLY" = false ] && [ "$AGENT_CONTEXT_ONLY" = false ] && [ "$SKIP_ADMIN_BOOTSTRAP" = false ]; then
  step "Step 8/11: Bootstrap first admin"
  # Strict rollout gate: bootstrap-admin.sh does kubectl exec into the gateway
  # pod, so the deployment must be fully healthy. Wait up to 300s (retries).
  echo "Waiting for gateway rollout to complete (required for admin bootstrap)..."
  if ! kubectl rollout status deployment/bedrockgateway -n adp-gateway --timeout=300s 2>/dev/null; then
    fail "Gateway deployment not healthy after 300s. Cannot bootstrap admin (kubectl exec requires a running pod). Fix the gateway first, then re-run."
  fi
  bash "$ROOT_DIR/modules/gateway/scripts/bootstrap-admin.sh" --env "$ENVIRONMENT" --region "$AWS_REGION"
  ok "First admin bootstrapped"
elif [ "$SKIP_ADMIN_BOOTSTRAP" = true ]; then
  step "Step 8/11: Skipping admin bootstrap (--skip-admin-bootstrap)"
else
  step "Step 8/11: Skipping admin bootstrap (scope exclusion)"
fi

refresh_credentials
# =============================================================================
# Step 9/11: Webhook-ingress stack (KEDA + agent-runtime)
# =============================================================================
# deploy-webhook-ingress.sh builds the agent-runtime image, packages the webhook
# Lambda zip, and terraform-applies the webhook-ingress stack (API GW → Lambda →
# SQS → KEDA → agent-worker). Runs BEFORE agent-factory because agent-factory's
# gateway-main.tf references the KEDA CRD and keda-operator-role that this step
# creates (Issue #1052).
if [ "$GATEWAY_ONLY" = false ] && [ "$AGENT_CONTEXT_ONLY" = false ] && [ "$SKIP_WEBHOOK_INGRESS" = false ]; then
  step "Step 9/11: Deploy webhook-ingress stack"
  bash "$ROOT_DIR/modules/agent-factory/webhook-ingress/scripts/deploy-webhook-ingress.sh" \
    --env "$ENVIRONMENT" --region "$AWS_REGION"
  ok "Webhook-ingress deployed"
elif [ "$SKIP_WEBHOOK_INGRESS" = true ]; then
  step "Step 9/11: Skipping webhook-ingress (--skip-webhook-ingress)"
else
  step "Step 9/11: Skipping webhook-ingress (scope exclusion)"
fi

refresh_credentials
# =============================================================================
# Step 10/11: Agent Factory
# =============================================================================
# Runs after webhook-ingress which installs KEDA (CRD + operator role).
# GitHub App secrets (ARC runner) are optional — enable_github_apps=false on
# fresh deploys where Apps haven't been registered yet.
if [ "$GATEWAY_ONLY" = false ] && [ "$AGENT_CONTEXT_ONLY" = false ]; then
  step "Step 10/11: Deploy agent-factory"

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
  terraform init -backend-config="$BACKEND_FILE" -input=false
  if [ "$UPDATE_MODE" = true ]; then
    terraform_update_apply "agent-factory" "terraform.tfvars"
  else
    terraform apply -var-file=terraform.tfvars -auto-approve
    ok "Agent-factory deployed"
  fi

  # --- Agent Gateway build + deploy (part of agent-factory) ---
  step "Step 10b/11: Build and deploy agent gateway"

  # --- Docker build: use CodeBuild (needs privileged mode) or local Docker ---
  LOCAL_IMAGE_TAG="${IMAGE_TAG:-latest}"
  if [ "$LOCAL_MODE" = true ] && docker info &>/dev/null 2>&1; then
    cd "$ROOT_DIR/modules/agent-factory"
    BUILD_DIR="/tmp/agent-gateway-build"
    rm -rf "$BUILD_DIR" && mkdir -p "$BUILD_DIR"
    cp -r gateway/app "$BUILD_DIR/app"
    cp -r agent "$BUILD_DIR/agent"
    cp gateway/Dockerfile "$BUILD_DIR/Dockerfile"
    cp gateway/entrypoint.sh "$BUILD_DIR/entrypoint.sh"

    aws ecr describe-repositories --repository-names "adp-agent-gateway" --region "$AWS_REGION" 2>/dev/null || \
      aws ecr create-repository --repository-name "adp-agent-gateway" --region "$AWS_REGION" --no-cli-pager
    aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$REGISTRY"
    docker build -t "$REGISTRY/adp-agent-gateway:$LOCAL_IMAGE_TAG" "$BUILD_DIR"
    docker tag "$REGISTRY/adp-agent-gateway:$LOCAL_IMAGE_TAG" "$REGISTRY/adp-agent-gateway:latest"
    docker push "$REGISTRY/adp-agent-gateway:$LOCAL_IMAGE_TAG"
    docker push "$REGISTRY/adp-agent-gateway:latest"
    rm -rf "$BUILD_DIR"
  else
    # Docker build via CodeBuild (Terraform-managed project)
    run_codebuild "adp-${ENVIRONMENT}-agent-gateway" "codebuild/bs-agent-gateway.yml"
  fi

  # --- K8s deploy: runs directly (no CodeBuild needed) ---
  cd "$ROOT_DIR/modules/agent-factory"
  kubectl create namespace adp-gateway-agents --dry-run=client -o yaml | kubectl apply -f -
  INPUT_QUEUE_URL=$(cd infra && terraform output -raw gateway_input_queue_url 2>/dev/null || echo "PENDING")
  RESPONSE_QUEUE_URL=$(cd infra && terraform output -raw gateway_response_queue_url 2>/dev/null || echo "PENDING")
  SESSIONS_TABLE=$(cd infra && terraform output -raw gateway_sessions_table 2>/dev/null || echo "PENDING")
  AGENT_IMAGE="$REGISTRY/adp-agent-gateway:$LOCAL_IMAGE_TAG"
  sed -e "s|REPLACE_WITH_INPUT_QUEUE_URL|${INPUT_QUEUE_URL}|g" \
      -e "s|REPLACE_WITH_RESPONSE_QUEUE_URL|${RESPONSE_QUEUE_URL}|g" \
      -e "s|REPLACE_WITH_SESSIONS_TABLE_NAME|${SESSIONS_TABLE}|g" \
      -e "s|REPLACE_WITH_AGENT_IMAGE|${AGENT_IMAGE}|g" \
      gateway/k8s/keda-scaledjob.yaml | kubectl apply -f -

  if [ "$UPDATE_MODE" = true ]; then
    # Update mode: mandatory rollout verification for agent-gateway (§2)
    kubectl rollout status deployment/adp-agent-gateway -n adp-gateway-agents --timeout=300s 2>/dev/null \
      || warn "Agent gateway rollout status check skipped (ScaledJob — no persistent deployment)"
    ok "Agent gateway deployed (SHA: $IMAGE_TAG)"
  else
    ok "Agent gateway deployed"
    warn "Store GitHub App creds in Secrets Manager (see modules/agent-factory/SETUP-GUIDE.md)"
  fi
else
  step "Step 10/11: Skipping agent-factory"
fi

refresh_credentials
# =============================================================================
# Step 11/11: Agent Context (optional — gated by AGENT_CONTEXT_ENABLED or --agent-context-only)
# =============================================================================
DEPLOY_AGENT_CONTEXT=false
if [ "$AGENT_CONTEXT_ONLY" = true ]; then
  DEPLOY_AGENT_CONTEXT=true
elif [ "$GATEWAY_ONLY" = true ] || [ "$AGENT_FACTORY_ONLY" = true ] || [ "$SKIP_AGENT_CONTEXT" = true ]; then
  DEPLOY_AGENT_CONTEXT=false
elif [ "$AGENT_CONTEXT_ENABLED" = true ]; then
  DEPLOY_AGENT_CONTEXT=true
fi

if [ "$DEPLOY_AGENT_CONTEXT" = true ]; then
  step "Step 11/11: Deploy agent-context"

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
  bash deploy.sh --skip-validate
  ok "Agent-context deployed"
else
  step "Step 11/11: Skipping agent-context (set AGENT_CONTEXT_ENABLED=true or use --agent-context-only)"
fi

# =============================================================================
# Summary
# =============================================================================
step "Deployment complete"

echo "Platform:  $EKS_CLUSTER"
echo "Gateway:   kubectl get pods -n adp-gateway (configure kubectl: aws eks update-kubeconfig --name $EKS_CLUSTER --region $AWS_REGION)"

CF_DOMAIN=$(aws ssm get-parameter --name "/adp/$ENVIRONMENT/gateway/cloudfront-domain" --query "Parameter.Value" --output text 2>/dev/null) || true
[ -n "$CF_DOMAIN" ] && [ "$CF_DOMAIN" != "None" ] && echo "Frontend:  https://${CF_DOMAIN}" && echo "API:       https://${CF_DOMAIN}/api/health"
[ "$GATEWAY_ONLY" = false ] && echo "Agents:    kubectl get pods -n arc-runners"
[ "$DEPLOY_AGENT_CONTEXT" = true ] && echo "Context:   kubectl get pods -n agent-context"
GW_WS=$(cd "$ROOT_DIR/modules/agent-factory/infra" && terraform output -raw gateway_ws_endpoint 2>/dev/null) || true
[ -n "$GW_WS" ] && [ "$GW_WS" != "" ] && echo "AgentGW:   $GW_WS"

# --- Next steps (manual — GitHub App wiring; skipped in update mode) ---
if [ "$UPDATE_MODE" = false ] && [ "$GATEWAY_ONLY" = false ] && [ "$AGENT_CONTEXT_ONLY" = false ]; then
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
