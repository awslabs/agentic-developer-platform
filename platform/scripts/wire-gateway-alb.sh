#!/usr/bin/env bash
# Discovers the EKS Ingress-managed internal ALB, caches ARN/DNS/SG IDs to
# SSM, and writes them to $GITHUB_OUTPUT / $GITHUB_ENV for downstream steps.
# Idempotent: no-op if the ALB is already registered in SSM and still exists.
#
# Usage: bash platform/scripts/wire-gateway-alb.sh
# Reads: AWS_REGION, ENVIRONMENT
# Writes (stdout):    ALB_ARN, ALB_DNS, ALB_SG_IDS
# Writes (SSM):       /adp/<env>/gateway/internal-alb-{arn,dns,security-group-ids}
# Writes (GitHub):    $GITHUB_OUTPUT entries when run in Actions
#
# Exit:
#   0 on success (ALB found and cached)
#   1 if ALB not found after 10 min
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
ENVIRONMENT="${ENVIRONMENT:-dev}"

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
# ---------------------------------------------------------------------------
if [ -z "$ALB_ARN" ]; then
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
