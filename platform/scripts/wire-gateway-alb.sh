#!/usr/bin/env bash
# Discovers the EKS Ingress-managed internal ALB, caches ARN/DNS/SG IDs to
# SSM, and writes them to $GITHUB_OUTPUT / $GITHUB_ENV for downstream steps.
# Idempotent: no-op if the ALB is already registered in SSM and still exists.
#
# Usage:
#   bash platform/scripts/wire-gateway-alb.sh             # poll for up to 10 min (default)
#   bash platform/scripts/wire-gateway-alb.sh --no-wait   # return empty values immediately if ALB not found
#   bash platform/scripts/wire-gateway-alb.sh --apply     # discover ALB, then RE-APPLY gateway TF (second pass)
#
# The --no-wait mode is for the pre-plan invocation in gateway-infra-apply.yml.
# On a fresh deploy the ALB doesn't exist yet; the script returns empty ARN/DNS
# and terraform plan skips the VPC origin. On subsequent deploys the SSM cache
# hits and the script returns the real ARN, so the plan keeps the VPC origin.
# The default (wait) mode is for gateway-deploy.yml, where the EKS Ingress
# controller is expected to materialize the ALB shortly after pod rollout.
#
# The --apply mode is the GATEWAY SECOND PASS for stage-by-stage operators: the
# gateway API Gateway's OpenAPI body is gated on internal_alb_dns, so on first
# apply it ships a MOCK body (no backend /{proxy+}, no /auth/github route). This
# mode discovers the ALB and re-applies gateway-infra with the ALB vars so the
# real body (backend proxy + GitHub auth broker route) is generated, then forces
# an API Gateway stage redeploy so it goes live. Idempotent. (deploy-all.sh does
# this inline as "Step 4b"; --apply makes it runnable standalone.)
#
# Reads: AWS_REGION, ENVIRONMENT
# Writes (stdout):    ALB_ARN, ALB_DNS, ALB_SG_IDS
# Writes (SSM):       /adp/<env>/gateway/internal-alb-{arn,dns,security-group-ids}
# Writes (GitHub):    $GITHUB_OUTPUT entries when run in Actions
#
# Exit:
#   0 on success (ALB found, OR --no-wait + ALB not found yet — empty outputs)
#   1 if ALB not found after 10 min in wait-mode, or apply fails in --apply mode
set -euo pipefail

NO_WAIT=false
DO_APPLY=false
for arg in "$@"; do
  case "$arg" in
    --no-wait) NO_WAIT=true ;;
    --apply)   DO_APPLY=true ;;
    *) echo "Unknown arg: $arg" >&2; exit 2 ;;
  esac
done

AWS_REGION="${AWS_REGION:-us-east-1}"
ENVIRONMENT="${ENVIRONMENT:-dev}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# ---------------------------------------------------------------------------
# Helper: write a key=value pair to $GITHUB_OUTPUT if running in Actions
# ---------------------------------------------------------------------------
gh_output() {
  local key="$1" value="$2"
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    echo "${key}=${value}" >> "$GITHUB_OUTPUT"
  fi
}

# ---------------------------------------------------------------------------
# Helper: write a key=value pair to $GITHUB_ENV if running in Actions
# ---------------------------------------------------------------------------
gh_env() {
  local key="$1" value="$2"
  if [ -n "${GITHUB_ENV:-}" ]; then
    echo "${key}=${value}" >> "$GITHUB_ENV"
  fi
}

# ---------------------------------------------------------------------------
# Step 1: Check SSM cache for an existing ALB
# ---------------------------------------------------------------------------
CACHED_ALB_ARN=$(aws ssm get-parameter \
  --name "/adp/$ENVIRONMENT/gateway/internal-alb-arn" \
  --query "Parameter.Value" --output text \
  --region "$AWS_REGION" 2>/dev/null || echo "")

ALB_ARN=""
ALB_DNS=""

if [ -n "$CACHED_ALB_ARN" ] && [ "$CACHED_ALB_ARN" != "pending" ] && [ "$CACHED_ALB_ARN" != "None" ]; then
  # Validate cached ALB still exists
  if aws elbv2 describe-load-balancers \
       --load-balancer-arns "$CACHED_ALB_ARN" \
       --region "$AWS_REGION" > /dev/null 2>&1; then
    ALB_ARN="$CACHED_ALB_ARN"
    ALB_DNS=$(aws ssm get-parameter \
      --name "/adp/$ENVIRONMENT/gateway/internal-alb-dns" \
      --query "Parameter.Value" --output text \
      --region "$AWS_REGION" 2>/dev/null || echo "")
    echo "ALB found in SSM cache: $ALB_DNS"
  else
    echo "Cached ALB no longer valid, rediscovering..."
  fi
fi

# ---------------------------------------------------------------------------
# Step 2: If no cache hit, poll for the Ingress-managed ALB (up to 10 min)
# In --no-wait mode, do a single discovery attempt and return empty if not found.
# ---------------------------------------------------------------------------
if [ -z "$ALB_ARN" ]; then
  if [ "$NO_WAIT" = true ]; then
    # Single discovery attempt; the post-deploy invocation will retry with full polling.
    ALB_ARN=$(aws elbv2 describe-load-balancers --region "$AWS_REGION" \
      --query 'LoadBalancers[?Scheme==`internal`].LoadBalancerArn' \
      --output text 2>/dev/null | head -1 || true)
    if [ -z "$ALB_ARN" ] || [ "$ALB_ARN" = "None" ]; then
      ALB_ARN=$(aws elbv2 describe-load-balancers --region "$AWS_REGION" \
        --query 'LoadBalancers[?contains(LoadBalancerName,`bedrockgw`) || contains(LoadBalancerName,`k8s-bedrockgw`)].LoadBalancerArn' \
        --output text 2>/dev/null | head -1 || true)
    fi
    if [ -n "$ALB_ARN" ] && [ "$ALB_ARN" != "None" ]; then
      ALB_DNS=$(aws elbv2 describe-load-balancers \
        --load-balancer-arns "$ALB_ARN" --region "$AWS_REGION" \
        --query 'LoadBalancers[0].DNSName' --output text 2>/dev/null || echo "")
      echo "[--no-wait] ALB found: $ALB_DNS"
    else
      ALB_ARN=""
      ALB_DNS=""
      echo "[--no-wait] ALB not found yet — returning empty values (fresh deploy / pre-plan)."
    fi
  else
  echo "Waiting for EKS Ingress ALB to be provisioned..."
  for i in $(seq 1 40); do
    # Look for internal ALBs
    ALB_ARN=$(aws elbv2 describe-load-balancers --region "$AWS_REGION" \
      --query 'LoadBalancers[?Scheme==`internal`].LoadBalancerArn' \
      --output text 2>/dev/null | head -1 || true)

    if [ -z "$ALB_ARN" ] || [ "$ALB_ARN" = "None" ]; then
      # Also check by name pattern from ingress group
      ALB_ARN=$(aws elbv2 describe-load-balancers --region "$AWS_REGION" \
        --query 'LoadBalancers[?contains(LoadBalancerName,`bedrockgw`) || contains(LoadBalancerName,`k8s-bedrockgw`)].LoadBalancerArn' \
        --output text 2>/dev/null | head -1 || true)
    fi

    if [ -n "$ALB_ARN" ] && [ "$ALB_ARN" != "None" ]; then
      ALB_DNS=$(aws elbv2 describe-load-balancers \
        --load-balancer-arns "$ALB_ARN" --region "$AWS_REGION" \
        --query 'LoadBalancers[0].DNSName' --output text 2>/dev/null || echo "")
      ALB_STATE=$(aws elbv2 describe-load-balancers \
        --load-balancer-arns "$ALB_ARN" --region "$AWS_REGION" \
        --query 'LoadBalancers[0].State.Code' --output text 2>/dev/null || echo "")
      if [ "$ALB_STATE" = "active" ]; then
        echo "ALB active: $ALB_DNS"
        break
      fi
      echo "  ALB found but state=$ALB_STATE, waiting..."
    else
      echo "  Attempt $i/40: ALB not yet created, waiting 15s..."
    fi
    sleep 15
  done

  if [ -z "$ALB_ARN" ] || [ "$ALB_ARN" = "None" ]; then
    echo "::error::ALB not found after 10 minutes."
    exit 1
  fi

  # Cache ARN + DNS to SSM
  aws ssm put-parameter \
    --name "/adp/$ENVIRONMENT/gateway/internal-alb-arn" \
    --value "$ALB_ARN" --type String --overwrite \
    --region "$AWS_REGION" > /dev/null
  aws ssm put-parameter \
    --name "/adp/$ENVIRONMENT/gateway/internal-alb-dns" \
    --value "$ALB_DNS" --type String --overwrite \
    --region "$AWS_REGION" > /dev/null
  echo "ALB ARN/DNS cached in SSM"
  fi  # end !NO_WAIT
fi

# ---------------------------------------------------------------------------
# Step 3: Discover ALB security groups (always, even on cache hit)
# ---------------------------------------------------------------------------
# The api_gateway module needs these for VPC Link v2 egress rules + reciprocal
# ingress rule on each ALB SG. Rendered as a Terraform list literal via shell
# tr+sed to avoid a jq runtime dependency.
ALB_SG_IDS="[]"
if [ -n "$ALB_ARN" ] && [ "$ALB_ARN" != "None" ]; then
  ALB_SG_LIST=$(aws elbv2 describe-load-balancers \
    --load-balancer-arns "$ALB_ARN" --region "$AWS_REGION" \
    --query 'LoadBalancers[0].SecurityGroups' --output text 2>/dev/null || echo "")
  if [ -n "$ALB_SG_LIST" ]; then
    ALB_SG_IDS="[$(echo "$ALB_SG_LIST" | tr '[:space:]' ',' | sed 's/,$//' | sed 's/\([^,][^,]*\)/"\1"/g')]"
  fi
  aws ssm put-parameter \
    --name "/adp/$ENVIRONMENT/gateway/internal-alb-security-group-ids" \
    --value "$ALB_SG_IDS" --type String --overwrite \
    --region "$AWS_REGION" > /dev/null
  echo "ALB security groups: $ALB_SG_IDS"
fi

# ---------------------------------------------------------------------------
# Step 4: Export results
# ---------------------------------------------------------------------------
echo "ALB_ARN=$ALB_ARN"
echo "ALB_DNS=$ALB_DNS"
echo "ALB_SG_IDS=$ALB_SG_IDS"

gh_output "ALB_ARN" "$ALB_ARN"
gh_output "ALB_DNS" "$ALB_DNS"
gh_output "ALB_SG_IDS" "$ALB_SG_IDS"

gh_env "ALB_ARN" "$ALB_ARN"
gh_env "ALB_DNS" "$ALB_DNS"
gh_env "ALB_SG_IDS" "$ALB_SG_IDS"

# Also export as shell variables for callers that source this script
export ALB_ARN ALB_DNS ALB_SG_IDS

# ---------------------------------------------------------------------------
# Step 5 (--apply only): gateway second-pass re-apply + API GW stage redeploy
# ---------------------------------------------------------------------------
# Mirrors deploy-all.sh "Step 4b". Re-applies gateway-infra with the ALB vars so
# the API Gateway OpenAPI body switches from MOCK to the real body (backend
# /{proxy+} + /auth/github broker route), then forces a stage redeploy so the
# new body is served (the regenerated body needs a fresh deployment to take).
if [ "$DO_APPLY" = true ]; then
  echo ""
  echo "── --apply: gateway second pass ──"
  if [ -z "$ALB_ARN" ] || [ "$ALB_ARN" = "None" ] || [ -z "$ALB_DNS" ]; then
    echo "ERROR: ALB not discovered (ARN/DNS empty) — cannot re-apply gateway. Is the EKS Ingress ALB up?" >&2
    exit 1
  fi

  GW_INFRA_DIR="${REPO_ROOT}/modules/gateway/infra"
  GW_BACKEND="${REPO_ROOT}/environments/${ENVIRONMENT}/modules/gateway-backend.tfvars"
  GW_VARS="${REPO_ROOT}/environments/${ENVIRONMENT}/modules/gateway.tfvars"

  echo "Re-applying gateway-infra with ALB vars (internal_alb_dns=$ALB_DNS)..."
  ( cd "$GW_INFRA_DIR" \
    && terraform init -backend-config="$GW_BACKEND" -input=false -reconfigure >/dev/null \
    && terraform apply \
         -var-file="$GW_VARS" \
         -var "internal_alb_arn=$ALB_ARN" \
         -var "internal_alb_dns=$ALB_DNS" \
         -var "alb_security_group_ids=$ALB_SG_IDS" \
         -var "enable_vpc_origin=true" \
         -input=false -auto-approve )
  echo "Gateway re-apply complete."

  # Force an API Gateway stage redeploy so the regenerated OpenAPI body serves.
  # The REST API is named bedrockgw-<env>-api.
  API_ID=$(aws apigateway get-rest-apis --region "$AWS_REGION" \
    --query "items[?name=='bedrockgw-${ENVIRONMENT}-api'].id" --output text 2>/dev/null || echo "")
  if [ -n "$API_ID" ] && [ "$API_ID" != "None" ]; then
    aws apigateway create-deployment --rest-api-id "$API_ID" --stage-name "$ENVIRONMENT" \
      --description "wire-gateway-alb second-pass redeploy" --region "$AWS_REGION" \
      --query 'id' --output text >/dev/null \
      && echo "API Gateway $API_ID stage '$ENVIRONMENT' redeployed (broker + backend routes live)." \
      || echo "WARN: could not force API GW stage redeploy; routes may need a manual create-deployment."
  else
    echo "WARN: REST API bedrockgw-${ENVIRONMENT}-api not found — skipping stage redeploy."
  fi
fi
