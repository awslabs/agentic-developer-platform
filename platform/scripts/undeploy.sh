#!/bin/bash
set -euo pipefail

# =============================================================================
# undeploy.sh — Self-managed ADP teardown entry point
# =============================================================================
# Interactive undeploy with typed-account-ID gate, dry-run, and resume.
# Sources load-deploy-config.sh and undeploy-phases.sh for config resolution
# and phase execution. Maintains .adp-undeploy-state.json for resume.
#
# Usage:
#   ./platform/scripts/undeploy.sh                      # Interactive teardown
#   ./platform/scripts/undeploy.sh --dry-run            # Show what would be destroyed
#   ./platform/scripts/undeploy.sh --from gateway       # Resume from a specific phase
#   ./platform/scripts/undeploy.sh --skip agent_context # Skip a phase
#   ./platform/scripts/undeploy.sh --bootstrap          # Also destroy state backend
#   ./platform/scripts/undeploy.sh --yes                # Skip confirmation (NOT account gate)
#   ./platform/scripts/undeploy.sh --environment prod   # Override environment
#
# Flags:
#   --dry-run              Report what-exists, protected resources, estimated time
#   --skip <phase>         Skip a specific phase (repeatable)
#   --from <phase>         Start from a specific phase (skip all prior)
#   --bootstrap            Also destroy Terraform state backend (prompts separately)
#   --yes                  Skip "are you sure" prompt (account-ID gate still required)
#   --environment <env>    Override deployment environment (default: from config)
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
STATE_FILE="$ROOT_DIR/.adp-undeploy-state.json"

# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------
DRY_RUN=false
SKIP_PHASES=()
FROM_PHASE=""
INCLUDE_BOOTSTRAP=false
AUTO_YES=false
ENV_OVERRIDE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --skip)
      if [[ -z "${2:-}" ]]; then
        echo "ERROR: --skip requires a phase name" >&2
        exit 1
      fi
      SKIP_PHASES+=("$2")
      shift 2
      ;;
    --from)
      if [[ -z "${2:-}" ]]; then
        echo "ERROR: --from requires a phase name" >&2
        exit 1
      fi
      FROM_PHASE="$2"
      shift 2
      ;;
    --bootstrap)
      INCLUDE_BOOTSTRAP=true
      shift
      ;;
    --yes)
      AUTO_YES=true
      shift
      ;;
    --environment)
      if [[ -z "${2:-}" ]]; then
        echo "ERROR: --environment requires a value" >&2
        exit 1
      fi
      ENV_OVERRIDE="$2"
      shift 2
      ;;
    --help|-h)
      echo "Usage: $0 [OPTIONS]"
      echo ""
      echo "Options:"
      echo "  --dry-run              Show what would be destroyed without executing"
      echo "  --skip <phase>         Skip a phase (repeatable). Phases: agent_context,"
      echo "                         webhook_ingress, agent_factory, gateway, platform"
      echo "  --from <phase>         Start from a specific phase (skip all prior)"
      echo "  --bootstrap            Also destroy Terraform state backend (separate prompt)"
      echo "  --yes                  Skip confirmation prompt (account-ID gate still required)"
      echo "  --environment <env>    Override deployment environment"
      echo "  -h, --help             Show this help"
      exit 0
      ;;
    *)
      echo "ERROR: Unknown option: $1" >&2
      echo "Run '$0 --help' for usage." >&2
      exit 1
      ;;
  esac
done

# Apply environment override before sourcing config
if [[ -n "$ENV_OVERRIDE" ]]; then
  export ADP_ENVIRONMENT="$ENV_OVERRIDE"
fi

# ---------------------------------------------------------------------------
# Source dependencies
# ---------------------------------------------------------------------------
# shellcheck source=load-deploy-config.sh
source "$SCRIPT_DIR/load-deploy-config.sh"

# shellcheck source=undeploy-phases.sh
source "$SCRIPT_DIR/undeploy-phases.sh"

# ---------------------------------------------------------------------------
# Output helpers (match deploy-all.sh style)
# ---------------------------------------------------------------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'
step() { echo -e "\n${BLUE}--- $1 ---${NC}\n"; }
ok()   { echo -e "${GREEN}  + $1${NC}"; }
warn() { echo -e "${YELLOW}  ! $1${NC}"; }
fail() { echo -e "${RED}  x $1${NC}" >&2; exit 1; }
info() { echo -e "${CYAN}  $1${NC}"; }

# ---------------------------------------------------------------------------
# Phase definitions (ordered)
# ---------------------------------------------------------------------------
# Phase order: agent_context -> webhook_ingress -> agent_factory -> gateway -> platform
# This matches undeploy-phases.sh function names and deploy-all.sh --destroy order.
PHASE_ORDER=(agent_context webhook_ingress agent_factory gateway platform)
PHASE_ESTIMATED_TIME=(
  "2-5 min"    # agent_context
  "1-3 min"    # webhook_ingress
  "3-8 min"    # agent_factory
  "10-20 min"  # gateway (CloudFront disable can take up to 15 min)
  "5-15 min"   # platform (EKS deletion is slow)
)
MAX_RETRIES=2

# ---------------------------------------------------------------------------
# State file management
# ---------------------------------------------------------------------------
_state_init() {
  if [[ -f "$STATE_FILE" ]]; then
    return 0
  fi
  local phases_json="{"
  local first=true
  for phase in "${PHASE_ORDER[@]}"; do
    if [[ "$first" = true ]]; then
      first=false
    else
      phases_json+=","
    fi
    phases_json+="\"$phase\":{\"status\":\"pending\",\"retries\":0}"
  done
  phases_json+="}"

  cat > "$STATE_FILE" << EOF
{
  "environment": "$ADP_ENVIRONMENT",
  "account_id": "$ADP_ACCOUNT_ID",
  "region": "$ADP_REGION",
  "started_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "phases": $phases_json,
  "include_bootstrap": $INCLUDE_BOOTSTRAP,
  "dry_run": $DRY_RUN
}
EOF
}

_state_read_phase() {
  local phase="$1"
  local field="$2"
  python3 -c "
import json, sys
with open('$STATE_FILE') as f:
    state = json.load(f)
phase_data = state.get('phases', {}).get('$phase', {})
print(phase_data.get('$field', ''))
" 2>/dev/null || echo ""
}

_state_update_phase() {
  local phase="$1"
  local status="$2"
  local retries="${3:-}"
  python3 -c "
import json
with open('$STATE_FILE', 'r') as f:
    state = json.load(f)
if '$phase' not in state.get('phases', {}):
    state.setdefault('phases', {})['$phase'] = {}
state['phases']['$phase']['status'] = '$status'
if '$retries':
    state['phases']['$phase']['retries'] = int('${retries:-0}')
if '$status' == 'complete' or '$status' == 'failed':
    import datetime
    state['phases']['$phase']['finished_at'] = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
with open('$STATE_FILE', 'w') as f:
    json.dump(state, f, indent=2)
" 2>/dev/null
}

_state_set_completed() {
  python3 -c "
import json, datetime
with open('$STATE_FILE', 'r') as f:
    state = json.load(f)
state['completed_at'] = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
with open('$STATE_FILE', 'w') as f:
    json.dump(state, f, indent=2)
" 2>/dev/null
}

# ---------------------------------------------------------------------------
# Phase 0: Account identity gate
# ---------------------------------------------------------------------------
_phase0_account_gate() {
  step "Phase 0: Account Identity Verification"

  local caller_account
  caller_account=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) || {
    fail "Cannot determine AWS account. Configure AWS CLI first."
  }

  local caller_arn
  caller_arn=$(aws sts get-caller-identity --query Arn --output text 2>/dev/null) || true

  echo "  Account:     $caller_account"
  echo "  ARN:         $caller_arn"
  echo "  Region:      $ADP_REGION"
  echo "  Environment: $ADP_ENVIRONMENT"
  echo ""

  # Cross-check with config/deployment.yml if present
  if [[ -n "$ADP_ACCOUNT_ID" ]] && [[ "$ADP_ACCOUNT_ID" != "$caller_account" ]]; then
    fail "config/deployment.yml says account_id=$ADP_ACCOUNT_ID but caller resolves to $caller_account. Fix config or switch AWS_PROFILE."
  fi

  # Dry-run skips the confirmation gate
  if [[ "$DRY_RUN" = true ]]; then
    info "Dry-run mode: skipping account confirmation gate"
    return 0
  fi

  # --yes skips the "are you sure" but NOT the account-ID gate
  if [[ "$AUTO_YES" != true ]]; then
    echo -e "${YELLOW}  This will DESTROY all ADP infrastructure in account $caller_account ($ADP_ENVIRONMENT).${NC}"
    echo ""
    echo "  Destroy order: agent_context -> webhook_ingress -> agent_factory -> gateway -> platform"
    echo ""
    echo "  Resources that SURVIVE (by design):"
    echo "    - Terraform state backend (S3 + DynamoDB) — use --bootstrap to include"
    echo "    - GitHub App secrets (adp/gh-app-*)"
    echo "    - AWS-managed RDS secrets (rds!*)"
    echo ""
  fi

  # Typed account-ID gate (12-digit, not "yes")
  echo -e "  ${RED}Type the full 12-digit AWS account ID to confirm destruction:${NC}"
  read -r typed_account

  if [[ "$typed_account" != "$caller_account" ]]; then
    fail "Account ID does not match. Expected: $caller_account. Aborting."
  fi

  ok "Account confirmed: $caller_account"
}

# ---------------------------------------------------------------------------
# Dry-run: report what-exists + protected + estimated time
# ---------------------------------------------------------------------------
_dry_run_report() {
  step "Dry-Run Report"
  echo "  Scanning existing resources..."
  echo ""

  local eks_cluster="adp-${ADP_ENVIRONMENT}-eks-cluster"
  local state_bucket="adp-terraform-state-${ADP_ACCOUNT_ID}"

  # Check EKS cluster
  local eks_status
  eks_status=$(aws eks describe-cluster --name "$eks_cluster" --region "$ADP_REGION" \
    --query 'cluster.status' --output text 2>/dev/null || echo "NOT_FOUND")

  echo "  === Resources by Phase ==="
  echo ""

  local phase_idx=0
  for phase in "${PHASE_ORDER[@]}"; do
    local est="${PHASE_ESTIMATED_TIME[$phase_idx]}"
    local skip_marker=""

    # Check if phase is skipped
    for sp in "${SKIP_PHASES[@]+"${SKIP_PHASES[@]}"}"; do
      if [[ "$sp" == "$phase" ]]; then
        skip_marker=" [SKIP]"
        break
      fi
    done

    echo -e "  ${BLUE}$phase${NC}${skip_marker} (est. $est):"

    case "$phase" in
      agent_context)
        local ns_exists="no"
        kubectl get namespace agent-context >/dev/null 2>&1 && ns_exists="yes"
        local tf_state="no"
        aws s3api head-object --bucket "$state_bucket" \
          --key "${ADP_ENVIRONMENT}/modules/agent-context/terraform.tfstate" >/dev/null 2>&1 && tf_state="yes"
        echo "    Namespace agent-context: $ns_exists"
        echo "    Terraform state: $tf_state"
        ;;
      webhook_ingress)
        local lambda_artifacts="no"
        aws s3 ls "s3://${state_bucket}/lambda-artifacts/webhook-ingress/" >/dev/null 2>&1 && lambda_artifacts="yes"
        local tf_state="no"
        aws s3api head-object --bucket "$state_bucket" \
          --key "${ADP_ENVIRONMENT}/modules/webhook-ingress/terraform.tfstate" >/dev/null 2>&1 && tf_state="yes"
        echo "    Lambda artifacts in S3: $lambda_artifacts"
        echo "    Terraform state: $tf_state"
        ;;
      agent_factory)
        local ns_agents="no" ns_arc="no"
        kubectl get namespace adp-gateway-agents >/dev/null 2>&1 && ns_agents="yes"
        kubectl get namespace arc-runners >/dev/null 2>&1 && ns_arc="yes"
        local tf_state="no"
        aws s3api head-object --bucket "$state_bucket" \
          --key "${ADP_ENVIRONMENT}/modules/agent-factory/terraform.tfstate" >/dev/null 2>&1 && tf_state="yes"
        echo "    Namespace adp-gateway-agents: $ns_agents"
        echo "    Namespace arc-runners: $ns_arc"
        echo "    Terraform state: $tf_state"
        ;;
      gateway)
        local ns_gw="no"
        kubectl get namespace adp-gateway >/dev/null 2>&1 && ns_gw="yes"
        local cf_id
        cf_id=$(aws ssm get-parameter --name "/adp/$ADP_ENVIRONMENT/gateway/cloudfront-id" \
          --query "Parameter.Value" --output text --region "$ADP_REGION" 2>/dev/null || echo "")
        local tf_state="no"
        aws s3api head-object --bucket "$state_bucket" \
          --key "${ADP_ENVIRONMENT}/modules/gateway/terraform.tfstate" >/dev/null 2>&1 && tf_state="yes"
        echo "    Namespace adp-gateway: $ns_gw"
        echo "    CloudFront distribution: ${cf_id:-none}"
        echo "    Terraform state: $tf_state"
        ;;
      platform)
        echo "    EKS cluster ($eks_cluster): $eks_status"
        local tf_state="no"
        aws s3api head-object --bucket "$state_bucket" \
          --key "${ADP_ENVIRONMENT}/platform/terraform.tfstate" >/dev/null 2>&1 && tf_state="yes"
        echo "    Terraform state: $tf_state"
        ;;
    esac
    echo ""
    phase_idx=$((phase_idx + 1))
  done

  # Protected resources
  echo -e "  ${GREEN}=== PROTECTED (survive by design) ===${NC}"
  echo ""
  echo "    - Terraform state backend: s3://$state_bucket + DynamoDB adp-terraform-locks"
  echo "    - GitHub App secrets: adp/gh-app-* in Secrets Manager"
  echo "    - AWS-managed RDS secrets: rds!*"
  if [[ "$INCLUDE_BOOTSTRAP" = true ]]; then
    echo ""
    echo -e "    ${YELLOW}NOTE: --bootstrap flag set. State backend will be destroyed AFTER all phases.${NC}"
  fi
  echo ""

  ok "Dry-run complete. No resources were modified."
}

# ---------------------------------------------------------------------------
# Resume: check state file and show progress
# ---------------------------------------------------------------------------
_check_resume() {
  if [[ ! -f "$STATE_FILE" ]]; then
    return 0
  fi

  step "Resuming from previous run"
  echo "  State file found: $STATE_FILE"
  echo ""

  local any_incomplete=false
  for phase in "${PHASE_ORDER[@]}"; do
    local status
    status=$(_state_read_phase "$phase" "status")
    case "$status" in
      complete)
        ok "$phase: complete"
        ;;
      failed)
        warn "$phase: failed (will retry)"
        any_incomplete=true
        ;;
      running)
        warn "$phase: interrupted (will retry)"
        any_incomplete=true
        ;;
      skipped)
        info "$phase: skipped"
        ;;
      *)
        info "$phase: pending"
        any_incomplete=true
        ;;
    esac
  done
  echo ""

  if [[ "$any_incomplete" = false ]]; then
    ok "All phases already complete."
    return 1  # Signal: nothing to do
  fi
  return 0
}

# ---------------------------------------------------------------------------
# Run a single phase with retry logic
# ---------------------------------------------------------------------------
_run_phase() {
  local phase="$1"
  local phase_fn="phase_${phase}"
  local retries=0
  local max_retries=$MAX_RETRIES

  _state_update_phase "$phase" "running" "0"

  while [[ $retries -lt $max_retries ]]; do
    if $phase_fn; then
      _state_update_phase "$phase" "complete" "$retries"
      ok "$phase: complete"
      return 0
    fi

    retries=$((retries + 1))
    if [[ $retries -lt $max_retries ]]; then
      warn "$phase: failed (attempt $retries/$max_retries), retrying..."
      _state_update_phase "$phase" "running" "$retries"
    fi
  done

  _state_update_phase "$phase" "failed" "$retries"
  warn "$phase: FAILED after $max_retries attempts"
  return 1
}

# ---------------------------------------------------------------------------
# Should this phase be executed?
# ---------------------------------------------------------------------------
_should_run_phase() {
  local phase="$1"

  # Check --skip
  for sp in "${SKIP_PHASES[@]+"${SKIP_PHASES[@]}"}"; do
    if [[ "$sp" == "$phase" ]]; then
      return 1
    fi
  done

  # Check --from (skip all phases before the specified one)
  if [[ -n "$FROM_PHASE" ]]; then
    local found_from=false
    for p in "${PHASE_ORDER[@]}"; do
      if [[ "$p" == "$FROM_PHASE" ]]; then
        found_from=true
      fi
      if [[ "$p" == "$phase" ]]; then
        if [[ "$found_from" = true ]]; then
          return 0
        else
          return 1
        fi
      fi
    done
  fi

  # Check state file: skip if already complete
  if [[ -f "$STATE_FILE" ]]; then
    local status
    status=$(_state_read_phase "$phase" "status")
    if [[ "$status" == "complete" ]]; then
      return 1
    fi
    if [[ "$status" == "skipped" ]]; then
      return 1
    fi
  fi

  return 0
}

# ---------------------------------------------------------------------------
# Bootstrap phase (optional, separate prompt)
# ---------------------------------------------------------------------------
_run_bootstrap_destroy() {
  step "Bootstrap Destroy: Terraform State Backend"

  echo "  This will PERMANENTLY delete:"
  echo "    - S3 bucket: adp-terraform-state-${ADP_ACCOUNT_ID}"
  echo "    - DynamoDB table: adp-terraform-locks"
  echo ""
  echo -e "  ${RED}After this, you cannot recover Terraform state.${NC}"
  echo ""

  if [[ "$AUTO_YES" != true ]]; then
    echo "  Type 'destroy-state' to confirm bootstrap destruction:"
    read -r confirm_bootstrap
    if [[ "$confirm_bootstrap" != "destroy-state" ]]; then
      warn "Bootstrap destroy aborted."
      return 0
    fi
  fi

  if [[ -f "$SCRIPT_DIR/bootstrap-destroy.sh" ]]; then
    # bootstrap-destroy.sh has its own typed-account-ID confirmation
    bash "$SCRIPT_DIR/bootstrap-destroy.sh"
  else
    fail "bootstrap-destroy.sh not found at $SCRIPT_DIR/bootstrap-destroy.sh"
  fi
}

# =============================================================================
# Main execution
# =============================================================================

step "ADP Undeploy"
echo "  Environment: ${ADP_ENVIRONMENT}"
echo "  Region:      ${ADP_REGION}"
echo "  Dry-run:     ${DRY_RUN}"
if [[ ${#SKIP_PHASES[@]} -gt 0 ]]; then
  echo "  Skipping:    ${SKIP_PHASES[*]}"
fi
if [[ -n "$FROM_PHASE" ]]; then
  echo "  Starting from: $FROM_PHASE"
fi

# Validate --from phase name
if [[ -n "$FROM_PHASE" ]]; then
  local_valid=false
  for p in "${PHASE_ORDER[@]}"; do
    if [[ "$p" == "$FROM_PHASE" ]]; then
      local_valid=true
      break
    fi
  done
  if [[ "$local_valid" = false ]]; then
    fail "Unknown phase '$FROM_PHASE'. Valid phases: ${PHASE_ORDER[*]}"
  fi
fi

# Validate --skip phase names
for sp in "${SKIP_PHASES[@]+"${SKIP_PHASES[@]}"}"; do
  local_valid=false
  for p in "${PHASE_ORDER[@]}"; do
    if [[ "$p" == "$sp" ]]; then
      local_valid=true
      break
    fi
  done
  if [[ "$local_valid" = false ]]; then
    fail "Unknown phase '$sp'. Valid phases: ${PHASE_ORDER[*]}"
  fi
done

# Phase 0: Account identity gate
_phase0_account_gate

# Dry-run: report and exit
if [[ "$DRY_RUN" = true ]]; then
  export DRY_RUN=true
  _dry_run_report
  exit 0
fi

# Set up environment for undeploy-phases.sh
export ENVIRONMENT="$ADP_ENVIRONMENT"
export AWS_REGION="$ADP_REGION"
export ROOT_DIR="$ROOT_DIR"
export SCRIPT_DIR="$SCRIPT_DIR"
export DRY_RUN="false"

# Configure kubectl if available
if command -v kubectl >/dev/null 2>&1; then
  aws eks update-kubeconfig --name "adp-${ADP_ENVIRONMENT}-eks-cluster" --region "$ADP_REGION" 2>/dev/null || true
fi

# Check for resume
_check_resume
resume_rc=$?
if [[ $resume_rc -ne 0 ]]; then
  # All phases already complete
  if [[ "$INCLUDE_BOOTSTRAP" = true ]]; then
    _run_bootstrap_destroy
  fi
  exit 0
fi

# Initialize state file if not resuming
_state_init

# ---------------------------------------------------------------------------
# Execute phases
# ---------------------------------------------------------------------------
step "Executing undeploy phases"
echo "  Order: ${PHASE_ORDER[*]}"
echo ""

overall_failure=false

for phase in "${PHASE_ORDER[@]}"; do
  if ! _should_run_phase "$phase"; then
    status=""
    if [[ -f "$STATE_FILE" ]]; then
      status=$(_state_read_phase "$phase" "status")
    fi
    if [[ "$status" == "complete" ]]; then
      ok "$phase: already complete (skipping)"
    else
      _state_update_phase "$phase" "skipped" "0"
      info "$phase: skipped"
    fi
    continue
  fi

  step "Phase: $phase"
  if ! _run_phase "$phase"; then
    overall_failure=true
    echo ""
    warn "Phase $phase failed. Continuing with remaining phases..."
    echo "  (Re-run this script to retry failed phases)"
    echo ""
  fi
done

# Mark overall completion
if [[ "$overall_failure" = false ]]; then
  _state_set_completed
fi

# ---------------------------------------------------------------------------
# Bootstrap (optional, after all phases)
# ---------------------------------------------------------------------------
if [[ "$INCLUDE_BOOTSTRAP" = true ]]; then
  _run_bootstrap_destroy
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
step "Undeploy Summary"

if [[ "$overall_failure" = true ]]; then
  echo -e "  ${YELLOW}Some phases failed. Re-run this script to retry.${NC}"
  echo "  State file: $STATE_FILE"
  echo ""
  for phase in "${PHASE_ORDER[@]}"; do
    status=$(_state_read_phase "$phase" "status")
    case "$status" in
      complete) ok "$phase" ;;
      failed)   warn "$phase: FAILED" ;;
      skipped)  info "$phase: skipped" ;;
      *)        info "$phase: $status" ;;
    esac
  done
  exit 1
else
  echo -e "  ${GREEN}All phases completed successfully.${NC}"
  echo ""
  echo "  Surviving resources (by design):"
  echo "    - Terraform state backend: adp-terraform-state-${ADP_ACCOUNT_ID}"
  echo "    - GitHub App secrets: adp/gh-app-* in Secrets Manager"
  echo "    - AWS-managed RDS secrets: rds!*"
  if [[ "$INCLUDE_BOOTSTRAP" != true ]]; then
    echo ""
    echo "  To also destroy the state backend:"
    echo "    $0 --bootstrap"
    echo "    # or: $SCRIPT_DIR/bootstrap-destroy.sh"
  fi
  echo ""
  ok "Undeploy complete."
fi
