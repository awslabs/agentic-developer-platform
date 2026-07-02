#!/bin/bash
# =============================================================================
# undeploy-phases.sh — Shared idempotent phase library for ADP undeploy
# =============================================================================
# Exports functions for each module's undeploy phase. Source this file from
# both the self-managed undeploy script and the ADP-managed workflow to ensure
# phase logic is never duplicated.
#
# Functions:
#   phase_agent_context   — Tear down Agent Context module
#   phase_webhook_ingress — Tear down Webhook Ingress sub-module
#   phase_agent_factory   — Tear down Agent Factory module
#   phase_gateway         — Tear down Gateway module (7-step ordering)
#   phase_platform        — Tear down Platform shared infra (last)
#
# Each function:
#   - Returns 0 on success, non-zero on failure
#   - Is idempotent (safe to re-run)
#   - Propagates terraform exit codes (no stderr suppression on terraform)
#   - Never touches state backend or protected secrets (survive-by-design)
#
# Required environment variables:
#   ENVIRONMENT  — deployment environment (dev/staging/prod)
#   AWS_REGION   — AWS region
#   ROOT_DIR     — repository root (absolute path)
#
# Optional environment variables:
#   EKS_CLUSTER  — EKS cluster name (default: adp-${ENVIRONMENT}-eks-cluster)
#   STATE_BUCKET — Terraform state bucket (default: adp-terraform-state-<account>)
#   DRY_RUN      — if "true", print actions without executing destructive ops
#
# Usage:
#   source platform/scripts/undeploy-phases.sh
#   phase_gateway    # returns exit code
# =============================================================================

# Guard: must be sourced, not executed directly
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  echo "ERROR: This script must be sourced, not executed directly." >&2
  echo "Usage: source ${BASH_SOURCE[0]}" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Validate required env vars
# ---------------------------------------------------------------------------
_undeploy_check_env() {
  local missing=()
  [ -z "${ENVIRONMENT:-}" ] && missing+=("ENVIRONMENT")
  [ -z "${AWS_REGION:-}" ] && missing+=("AWS_REGION")
  [ -z "${ROOT_DIR:-}" ] && missing+=("ROOT_DIR")
  if [ ${#missing[@]} -gt 0 ]; then
    echo "ERROR: Required environment variables not set: ${missing[*]}" >&2
    return 1
  fi
  return 0
}

# ---------------------------------------------------------------------------
# Derived defaults
# ---------------------------------------------------------------------------
_undeploy_init() {
  _undeploy_check_env || return 1
  SCRIPT_DIR="${SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
  EKS_CLUSTER="${EKS_CLUSTER:-adp-${ENVIRONMENT}-eks-cluster}"
  DRY_RUN="${DRY_RUN:-false}"
}

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
_undeploy_log() {
  echo "[undeploy] $*"
}

_undeploy_warn() {
  echo "[undeploy] WARNING: $*" >&2
}

# Check if kubectl can reach the cluster
_undeploy_kubectl_available() {
  command -v kubectl >/dev/null 2>&1 && kubectl cluster-info >/dev/null 2>&1
}

# Delete a K8s namespace idempotently
_undeploy_delete_namespace() {
  local ns="$1"
  local timeout="${2:-120s}"
  if [ "$DRY_RUN" = "true" ]; then
    _undeploy_log "DRY RUN: would delete namespace $ns"
    return 0
  fi
  if ! _undeploy_kubectl_available; then
    _undeploy_log "kubectl not available, skipping namespace $ns deletion"
    return 0
  fi
  if kubectl get namespace "$ns" >/dev/null 2>&1; then
    _undeploy_log "Deleting namespace: $ns"
    kubectl delete namespace "$ns" --wait=true --timeout="$timeout" 2>/dev/null || true
  else
    _undeploy_log "Namespace $ns does not exist, skipping"
  fi
  return 0
}

# Run terraform destroy for a module
_undeploy_terraform_destroy() {
  local tf_dir="$1"
  local backend_config="$2"
  local var_file="$3"

  if [ ! -d "$tf_dir" ]; then
    _undeploy_log "Terraform directory not found: $tf_dir — skipping"
    return 0
  fi

  if [ "$DRY_RUN" = "true" ]; then
    _undeploy_log "DRY RUN: would run terraform destroy in $tf_dir"
    return 0
  fi

  _undeploy_log "Running terraform destroy in $tf_dir"
  (
    cd "$tf_dir" || return 1
    terraform init -backend-config="$backend_config" -input=false -reconfigure || {
      _undeploy_warn "terraform init failed in $tf_dir"
      return 1
    }
    terraform destroy -var-file="$var_file" -auto-approve || {
      _undeploy_warn "terraform destroy failed in $tf_dir"
      return 1
    }
  )
}

# Find S3 buckets matching a name prefix
_undeploy_find_buckets() {
  local pattern="$1"
  aws s3api list-buckets \
    --query "Buckets[?starts_with(Name,'${pattern}')].Name" \
    --output text 2>/dev/null || echo ""
}

# =============================================================================
# phase_agent_context — Tear down Agent Context module
# =============================================================================
phase_agent_context() {
  _undeploy_init || return 1
  _undeploy_log "=== Phase: Agent Context ==="

  local tf_dir="$ROOT_DIR/modules/agent-context/terraform"
  if [ ! -d "$tf_dir" ]; then
    _undeploy_log "Agent Context module not present, skipping"
    return 0
  fi

  # Step 1: Delete K8s namespace
  _undeploy_delete_namespace "agent-context"

  # Step 2: Terraform destroy
  _undeploy_terraform_destroy \
    "$tf_dir" \
    "../../../environments/$ENVIRONMENT/modules/agent-context-backend.tfvars" \
    "../../../environments/$ENVIRONMENT/modules/agent-context.tfvars"
  local rc=$?

  if [ $rc -eq 0 ]; then
    _undeploy_log "=== Agent Context: destroyed ==="
  else
    _undeploy_warn "Agent Context: terraform destroy failed (exit $rc)"
  fi
  return $rc
}

# =============================================================================
# phase_webhook_ingress — Tear down Webhook Ingress sub-module
# =============================================================================
phase_webhook_ingress() {
  _undeploy_init || return 1
  _undeploy_log "=== Phase: Webhook Ingress ==="

  local tf_dir="$ROOT_DIR/modules/agent-factory/webhook-ingress/infra"
  if [ ! -d "$tf_dir" ]; then
    _undeploy_log "Webhook Ingress module not present, skipping"
    return 0
  fi

  # Step 1: Clean up Lambda artifacts from S3
  local state_bucket="${STATE_BUCKET:-adp-terraform-state-$(aws sts get-caller-identity --query Account --output text 2>/dev/null)}"
  if [ "$DRY_RUN" = "true" ]; then
    _undeploy_log "DRY RUN: would remove s3://${state_bucket}/lambda-artifacts/webhook-ingress/"
  else
    _undeploy_log "Removing Lambda artifacts from S3..."
    aws s3 rm "s3://${state_bucket}/lambda-artifacts/webhook-ingress/" --recursive 2>/dev/null || true
  fi

  # Step 2: Delete K8s ScaledJobs (KEDA resources) before namespace
  if [ "$DRY_RUN" != "true" ] && _undeploy_kubectl_available; then
    kubectl delete scaledjobs --all -n adp-gateway-agents 2>/dev/null || true
  fi

  # Step 3: Terraform destroy
  _undeploy_terraform_destroy \
    "$tf_dir" \
    "../../../../environments/$ENVIRONMENT/modules/webhook-ingress-backend.tfvars" \
    "terraform.tfvars"
  local rc=$?

  if [ $rc -eq 0 ]; then
    _undeploy_log "=== Webhook Ingress: destroyed ==="
  else
    _undeploy_warn "Webhook Ingress: terraform destroy failed (exit $rc)"
  fi
  return $rc
}

# =============================================================================
# phase_agent_factory — Tear down Agent Factory module
# =============================================================================
phase_agent_factory() {
  _undeploy_init || return 1
  _undeploy_log "=== Phase: Agent Factory ==="

  local tf_dir="$ROOT_DIR/modules/agent-factory/infra"
  if [ ! -f "$tf_dir/terraform.tfvars" ]; then
    _undeploy_log "Agent Factory not configured (no terraform.tfvars), skipping"
    return 0
  fi

  # Step 1: Clean up K8s resources
  _undeploy_delete_namespace "adp-gateway-agents"
  _undeploy_delete_namespace "arc-runners"

  # Step 2: Empty S3 buckets (beads state, chat artifacts)
  local factory_buckets=""
  for pattern in "adp-${ENVIRONMENT}-agent-beads-state-" "adp-${ENVIRONMENT}-chat-artifacts-"; do
    local found
    found=$(_undeploy_find_buckets "$pattern")
    if [ -n "$found" ] && [ "$found" != "None" ]; then
      factory_buckets="$factory_buckets $found"
    fi
  done
  factory_buckets=$(echo "$factory_buckets" | xargs)  # trim whitespace

  if [ -n "$factory_buckets" ]; then
    if [ "$DRY_RUN" = "true" ]; then
      _undeploy_log "DRY RUN: would empty S3 buckets: $factory_buckets"
    else
      _undeploy_log "Emptying S3 buckets: $factory_buckets"
      # shellcheck disable=SC2086
      bash "$SCRIPT_DIR/empty-s3-buckets.sh" $factory_buckets  # intentional word-split: each bucket is a separate arg
    fi
  fi

  # Step 3: Terraform destroy
  _undeploy_terraform_destroy \
    "$tf_dir" \
    "../../../environments/$ENVIRONMENT/modules/agent-factory-backend.tfvars" \
    "terraform.tfvars"
  local rc=$?

  if [ $rc -eq 0 ]; then
    _undeploy_log "=== Agent Factory: destroyed ==="
  else
    _undeploy_warn "Agent Factory: terraform destroy failed (exit $rc)"
  fi
  return $rc
}

# =============================================================================
# phase_gateway — Tear down Gateway module (7-step internal ordering)
# =============================================================================
# Internal ordering per design §1:
#   1. Ingress/ALB cleanup (delete-ingress-and-wait.sh)
#   2. Delete K8s namespace
#   3. Empty S3 buckets (frontend)
#   4. Force-delete Secrets Manager secrets
#   5. Disable CloudFront distribution
#   6. Terraform destroy
#   7. Clean up SSM parameters
# =============================================================================
phase_gateway() {
  _undeploy_init || return 1
  _undeploy_log "=== Phase: Gateway ==="

  # ---- Step 1: Ingress/ALB cleanup ----
  _undeploy_log "Step 1/7: Ingress/ALB cleanup"
  if [ "$DRY_RUN" = "true" ]; then
    _undeploy_log "DRY RUN: would run delete-ingress-and-wait.sh"
  else
    if [ -f "$SCRIPT_DIR/delete-ingress-and-wait.sh" ]; then
      ENVIRONMENT="$ENVIRONMENT" AWS_REGION="$AWS_REGION" \
        bash "$SCRIPT_DIR/delete-ingress-and-wait.sh" || true
    else
      _undeploy_warn "delete-ingress-and-wait.sh not found, skipping"
    fi
  fi

  # ---- Step 2: Delete K8s namespace ----
  _undeploy_log "Step 2/7: Delete gateway namespace"
  _undeploy_delete_namespace "adp-gateway"

  # ---- Step 3: Empty S3 buckets ----
  _undeploy_log "Step 3/7: Empty S3 buckets"
  local gw_buckets=""

  # Frontend bucket from SSM
  local frontend_bucket
  frontend_bucket=$(aws ssm get-parameter \
    --name "/adp/$ENVIRONMENT/gateway/frontend-bucket" \
    --query "Parameter.Value" --output text \
    --region "$AWS_REGION" 2>/dev/null || echo "")
  if [ -n "$frontend_bucket" ] && [ "$frontend_bucket" != "None" ]; then
    gw_buckets="$frontend_bucket"
  fi

  # Additional frontend buckets by pattern
  local found
  found=$(_undeploy_find_buckets "bedrockgw-${ENVIRONMENT}-frontend-")
  if [ -n "$found" ] && [ "$found" != "None" ]; then
    gw_buckets="$gw_buckets $found"
  fi
  gw_buckets=$(echo "$gw_buckets" | xargs)  # trim/dedupe whitespace

  if [ -n "$gw_buckets" ]; then
    if [ "$DRY_RUN" = "true" ]; then
      _undeploy_log "DRY RUN: would empty S3 buckets: $gw_buckets"
    else
      _undeploy_log "Emptying S3 buckets: $gw_buckets"
      # shellcheck disable=SC2086
      bash "$SCRIPT_DIR/empty-s3-buckets.sh" $gw_buckets  # intentional word-split: each bucket is a separate arg
    fi
  fi

  # ---- Step 4: Force-delete Secrets Manager secrets ----
  _undeploy_log "Step 4/7: Force-delete secrets"
  if [ "$DRY_RUN" = "true" ]; then
    _undeploy_log "DRY RUN: would force-delete secrets with prefixes: bedrockgw-${ENVIRONMENT}-, adp/${ENVIRONMENT}/gateway/test-"
  else
    if [ -f "$SCRIPT_DIR/force-delete-secrets.sh" ]; then
      bash "$SCRIPT_DIR/force-delete-secrets.sh" \
        "bedrockgw-${ENVIRONMENT}-" \
        "adp/${ENVIRONMENT}/gateway/test-" || true
    else
      _undeploy_warn "force-delete-secrets.sh not found, skipping"
    fi
  fi

  # ---- Step 5: Disable CloudFront distribution ----
  _undeploy_log "Step 5/7: Disable CloudFront distribution"
  local dist_id
  dist_id=$(aws ssm get-parameter \
    --name "/adp/$ENVIRONMENT/gateway/cloudfront-id" \
    --query "Parameter.Value" --output text \
    --region "$AWS_REGION" 2>/dev/null || echo "")

  if [ -n "$dist_id" ] && [ "$dist_id" != "None" ]; then
    if [ "$DRY_RUN" = "true" ]; then
      _undeploy_log "DRY RUN: would disable CloudFront distribution $dist_id"
    else
      _undeploy_log "Disabling CloudFront distribution $dist_id..."
      local dist_config
      dist_config=$(aws cloudfront get-distribution-config --id "$dist_id" 2>/dev/null || echo "")
      if [ -n "$dist_config" ]; then
        local enabled
        enabled=$(echo "$dist_config" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(str(d['DistributionConfig']['Enabled']).lower())
" 2>/dev/null || echo "false")
        if [ "$enabled" = "true" ]; then
          local etag
          etag=$(echo "$dist_config" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(d['ETag'])
")
          echo "$dist_config" | python3 -c "
import sys, json
d = json.load(sys.stdin)
config = d['DistributionConfig']
config['Enabled'] = False
print(json.dumps(config))
" > /tmp/cf-disable-config.json
          aws cloudfront update-distribution --id "$dist_id" --if-match "$etag" \
            --distribution-config "file:///tmp/cf-disable-config.json" > /dev/null 2>&1 || {
            _undeploy_warn "Failed to disable CloudFront $dist_id"
          }
          _undeploy_log "Waiting for CloudFront to deploy disabled state (up to 15 min)..."
          aws cloudfront wait distribution-deployed --id "$dist_id" 2>/dev/null || {
            _undeploy_warn "CloudFront wait timed out. terraform destroy may retry."
          }
          rm -f /tmp/cf-disable-config.json
          _undeploy_log "CloudFront $dist_id disabled"
        else
          _undeploy_log "CloudFront $dist_id already disabled"
        fi
      fi
    fi
  else
    _undeploy_log "No CloudFront distribution found in SSM, skipping"
  fi

  # ---- Step 6: Terraform destroy ----
  _undeploy_log "Step 6/7: Terraform destroy"
  _undeploy_terraform_destroy \
    "$ROOT_DIR/modules/gateway/infra" \
    "../../../environments/$ENVIRONMENT/modules/gateway-backend.tfvars" \
    "../../../environments/$ENVIRONMENT/modules/gateway.tfvars"
  local rc=$?

  # ---- Step 7: Clean up SSM parameters ----
  _undeploy_log "Step 7/7: Clean up SSM parameters"
  local ssm_params=(
    "/adp/$ENVIRONMENT/gateway/frontend-bucket"
    "/adp/$ENVIRONMENT/gateway/cloudfront-id"
    "/adp/$ENVIRONMENT/gateway/cloudfront-domain"
    "/adp/$ENVIRONMENT/gateway/internal-alb-arn"
    "/adp/$ENVIRONMENT/gateway/internal-alb-dns"
    "/adp/$ENVIRONMENT/gateway/internal-alb-security-group-ids"
  )
  for param in "${ssm_params[@]}"; do
    if [ "$DRY_RUN" = "true" ]; then
      _undeploy_log "DRY RUN: would delete SSM parameter: $param"
    else
      aws ssm delete-parameter --name "$param" --region "$AWS_REGION" 2>/dev/null || true
    fi
  done

  if [ $rc -eq 0 ]; then
    _undeploy_log "=== Gateway: destroyed ==="
  else
    _undeploy_warn "Gateway: terraform destroy failed (exit $rc)"
  fi
  return $rc
}

# =============================================================================
# phase_platform — Tear down Platform shared infra (run last)
# =============================================================================
phase_platform() {
  _undeploy_init || return 1
  _undeploy_log "=== Phase: Platform ==="

  # Step 1: Clean up K8s system namespaces
  _undeploy_delete_namespace "arc-systems"
  _undeploy_delete_namespace "keda"

  # Step 2: Clean up orphaned ENIs in the VPC
  if [ "$DRY_RUN" != "true" ] && _undeploy_kubectl_available; then
    local vpc_id
    vpc_id=$(aws eks describe-cluster --name "$EKS_CLUSTER" --region "$AWS_REGION" \
      --query 'cluster.resourcesVpcConfig.vpcId' --output text 2>/dev/null || echo "")
    if [ -n "$vpc_id" ] && [ "$vpc_id" != "None" ]; then
      _undeploy_log "Cleaning up orphaned ENIs in VPC $vpc_id..."
      local orphan_enis
      orphan_enis=$(aws ec2 describe-network-interfaces --region "$AWS_REGION" \
        --filters "Name=vpc-id,Values=$vpc_id" "Name=status,Values=available" \
        --query 'NetworkInterfaces[].NetworkInterfaceId' --output text 2>/dev/null || echo "")
      for eni in $orphan_enis; do
        _undeploy_log "  Deleting orphaned ENI: $eni"
        aws ec2 delete-network-interface --network-interface-id "$eni" --region "$AWS_REGION" 2>/dev/null || true
      done
    fi
  elif [ "$DRY_RUN" = "true" ]; then
    _undeploy_log "DRY RUN: would clean up orphaned ENIs"
  fi

  # Step 3: Terraform destroy
  _undeploy_terraform_destroy \
    "$ROOT_DIR/platform/infra" \
    "../../environments/$ENVIRONMENT/backend.tfvars" \
    "../../environments/$ENVIRONMENT/platform.tfvars"
  local rc=$?

  # Step 4: Clean up retired CodeBuild projects
  local codebuild_projects=(
    "adp-${ENVIRONMENT}-frontend-build"
    "adp-${ENVIRONMENT}-platform-infra"
    "adp-${ENVIRONMENT}-gateway-deploy"
    "adp-${ENVIRONMENT}-gateway-infra"
    "adp-${ENVIRONMENT}-gateway-alb-wire"
    "adp-${ENVIRONMENT}-agent-factory-infra"
    "adp-${ENVIRONMENT}-agent-context-infra"
    "adp-${ENVIRONMENT}-agent-context-deploy"
    "adp-${ENVIRONMENT}-destroy"
  )
  for project in "${codebuild_projects[@]}"; do
    if [ "$DRY_RUN" = "true" ]; then
      _undeploy_log "DRY RUN: would delete CodeBuild project: $project"
    else
      aws codebuild delete-project --name "$project" --region "$AWS_REGION" 2>/dev/null || true
    fi
  done

  if [ $rc -eq 0 ]; then
    _undeploy_log "=== Platform: destroyed ==="
  else
    _undeploy_warn "Platform: terraform destroy failed (exit $rc)"
  fi
  return $rc
}
