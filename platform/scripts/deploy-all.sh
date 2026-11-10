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
#   ./platform/scripts/deploy-all.sh --dev-box on               # Also run the optional Dev Box module (step 12)
#   ./platform/scripts/deploy-all.sh --dev-box-only             # Platform + Dev Box only (existing environment)
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/deploy-prerequisites.sh"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "${SCRIPT_DIR}/gateway-rollout.sh"

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
INSIGHTS_ONLY=false
TOOLS_ONLY=false
TOOLS_ADAPTERS_ONLY=false
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
RESUME=false
FROM_PHASE=""
ALLOW_KNOWN_CLAUDE_GAP=false
# Chat rollout mode (#7899): retire (default, today's behaviour) | delegated | keep-legacy.
CHAT_MODE="${ADP_CHAT_MODE:-retire}"
CHAT_EXCEPTION="${ADP_CHAT_EXCEPTION:-}"
# Dev Box (#7998): OPTIONAL module. on | off | "" (decide from the environment's
# dev-box.tfvars stage_enabled); --dev-box-only runs platform + Dev Box alone.
DEV_BOX_MODE="${ADP_DEV_BOX:-}"
DEV_BOX_ONLY=false

while [ "$#" -gt 0 ]; do
  case "$1" in
    --env) ENVIRONMENT="${2:?--env requires a value}"; shift ;;
    --region) AWS_REGION="${2:?--region requires a value}"; shift ;;
    --gateway-only) GATEWAY_ONLY=true ;;
    --agent-factory-only) AGENT_FACTORY_ONLY=true ;;
    --agent-context-only) AGENT_CONTEXT_ONLY=true ;;
    --insights-only) INSIGHTS_ONLY=true ;;
    --tools-only) TOOLS_ONLY=true ;;
    --tools-adapters-only) TOOLS_ADAPTERS_ONLY=true ;;
    --skip-agent-context) SKIP_AGENT_CONTEXT=true ;;
    --superplane-only|--skip-superplane)
      echo "Superplane is deployed from modules/domain-apps/superplane/; use its deploy.sh or module workflows." >&2
      exit 2 ;;
    --destroy) DESTROY=true ;;
    --skip-frontend) SKIP_FRONTEND=true ;;
    --skip-broker) SKIP_BROKER=true ;;
    --skip-admin-bootstrap) SKIP_ADMIN_BOOTSTRAP=true ;;
    --skip-webhook-ingress) SKIP_WEBHOOK_INGRESS=true ;;
    --local) LOCAL_MODE=true ;;
    --ci) CI_MODE=true ;;
    --resume) RESUME=true ;;
    --from) FROM_PHASE="${2:?--from requires a phase}"; RESUME=true; shift ;;
    --allow-known-claude-gap) ALLOW_KNOWN_CLAUDE_GAP=true ;;
    --update) UPDATE_MODE=true ;;
    --confirm-destructive) CONFIRM_DESTRUCTIVE=true ;;
    --chat) CHAT_MODE="${2:?--chat requires retire, delegated or keep-legacy}"; shift ;;
    --chat-exception) CHAT_EXCEPTION="${2:?--chat-exception requires a qualification/exception record reference}"; shift ;;
    --dev-box) DEV_BOX_MODE="${2:?--dev-box requires on or off}"; shift ;;
    --dev-box-only) DEV_BOX_ONLY=true; DEV_BOX_MODE=on ;;
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
      echo "  --resume               Resume completed named phases for this source and target"
      echo "  --from PHASE           Rerun PHASE and later phases (requires matching checkpoints)"
      echo "  --allow-known-claude-gap Accept the reviewed pricing gap across accounts"
      echo "  Phases: bootstrap platform gateway-infra gateway gateway-alb broker admin webhook factory tools-adapters dev-box context finalize frontend verify"
      echo "Target: --env <dev|staging|prod> --region <aws-region> (AWS_PROFILE selects account)"
      echo ""
      echo "Scope:"
      echo "  --gateway-only         Platform + gateway only"
      echo "  --agent-factory-only   Platform + agent-factory only"
      echo "  --agent-context-only   Platform + agent-context only"
      echo "  --dev-box-only         Platform + Dev Box only (add the optional module to an existing environment)"
      echo "  --insights-only        Deploy isolated Insights infrastructure only"
      echo "  --tools-only           Deploy the independently gated tools module"
      echo "  --tools-adapters-only  Install adapter foundations using ADP_TOOLS_ADAPTERS_CONFIG"
      echo "                         --ci plans only; runtime deployment remains in the tools workflow"
      echo ""
      echo "Skip:"
      echo "  --skip-frontend        Skip frontend build and deploy"
      echo "  --skip-broker          Skip broker Lambda deploy"
      echo "  --skip-admin-bootstrap Skip first-admin DB seeding"
      echo "  --skip-webhook-ingress Skip webhook-ingress stack"
      echo "  --skip-agent-context   Skip agent-context even if AGENT_CONTEXT_ENABLED=true"
      echo "  Domain apps and their build jobs are deployed from their own modules"
      echo ""
      echo "Build:"
      echo "  --local                Use local Docker for image builds (instead of CodeBuild)"
      echo ""
      echo "Chat (Step 10; ADP_CHAT_MODE / ADP_CHAT_EXCEPTION set the same values):"
      echo "  --chat retire          Retire the legacy credentialed chat worker; chat stays unavailable (default)"
      echo "  --chat keep-legacy     Leave the legacy credentialed chat worker running; skip retirement"
      echo "  --chat delegated       Build supervisor + sandbox images, provision the sandbox gateway TLS"
      echo "                         endpoint, apply the delegated ScaledJob and Crew controls, then retire"
      echo "                         the legacy worker. Requires --chat-exception."
      echo "  --chat-exception REF   Qualification/exception record for delegated chat (#6937 evidence ID);"
      echo "                         recorded in the deployment journal"
      echo ""
      echo "Dev Box (Step 12, optional module; ADP_DEV_BOX sets the same value):"
      echo "  --dev-box on|off       Run modules/user-services/dev-box/scripts/deploy-dev-box.sh after the"
      echo "                         agent factory (default: off unless environments/<env>/modules/dev-box.tfvars"
      echo "                         sets stage_enabled = true, as dev does). Off deploys exactly today's platform."
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

# =============================================================================
# Chat rollout mode (#7899) — decided before preflight, builds or any apply.
# =============================================================================
# retire      Today's behaviour: Step 10 drains the legacy credentialed worker
#             (`upgrade-chat.py drain` deletes ScaledJob chat-agent-worker) and
#             verifies it is gone. Chat is unavailable afterwards.
# keep-legacy Leaves the legacy worker and its grants in place; no retirement step
#             runs. The journal records that a credentialed worker remains.
# delegated   Builds the supervisor and sandbox images, provisions the sandbox
#             gateway TLS endpoint, applies the supervisor/sandbox ScaledJob and
#             the Crew pod controls, and only then retires the legacy worker.
#             Delegated chat is gated on the #6937 qualification, so the mode
#             refuses to start without a recorded qualification/exception
#             reference (--chat-exception), which goes into the journal.
case "$CHAT_MODE" in
  retire|delegated|keep-legacy) ;;
  *) fail "--chat must be retire, delegated or keep-legacy (got: $CHAT_MODE)" ;;
esac
if [ "$CHAT_MODE" = delegated ]; then
  [ -n "$CHAT_EXCEPTION" ] || fail "--chat delegated requires --chat-exception <qualification/exception record, e.g. the #6937 evidence ID>; refusing before any build"
  [[ "$CHAT_EXCEPTION" =~ ^[A-Za-z0-9#][A-Za-z0-9#:/._@+\ -]{0,199}$ ]] || fail "--chat-exception must be a short record reference (letters, digits, spaces, #:/._@+-)"
elif [ -n "$CHAT_EXCEPTION" ]; then
  fail "--chat-exception is only meaningful with --chat delegated"
fi
# The ingest Lambda's ADP_CHAT_MODEL_POLICY_ENABLED must agree with the delegated
# ScaledJob's requirement (true); every other mode keeps the module default. This is
# the delegated-chat plumbing switch (the ingest roots every turn as a model root and
# the sandbox obtains its model from /v1/chat/model/decision; the ingest answers
# "Chat is currently unavailable" and the sandbox admission policies refuse pods
# without it). Whether that decision REQUIRES invocability evidence is the separate
# chat model-policy posture, SSM /adp/<env>/gateway/chat-model-policy-posture
# (#7997; default advisory), read in Step 10c below.
CHAT_MODEL_POLICY_ENABLED=false
[ "$CHAT_MODE" != delegated ] || CHAT_MODEL_POLICY_ENABLED=true
case "$CHAT_MODE" in
  delegated) ok "Chat mode: delegated (qualification/exception record: $CHAT_EXCEPTION)" ;;
  keep-legacy) warn "Chat mode: keep-legacy — the legacy credentialed chat worker will remain running" ;;
  retire) ok "Chat mode: retire — the legacy credentialed chat worker will be retired; chat becomes unavailable" ;;
esac

if [ "${ADP_ENABLED_DOMAIN_APPS+x}" = x ]; then
  fail "ADP_ENABLED_DOMAIN_APPS is retired; deploy optional build jobs from their domain modules"
fi
if [ "${SUPERPLANE_ENABLED:-false}" = true ]; then
  fail "SUPERPLANE_ENABLED no longer deploys Superplane; use modules/domain-apps/superplane/deploy.sh"
fi

# =============================================================================
# Dev Box mode (#7998) — optional module, decided before preflight or any apply.
# =============================================================================
# off  (default) Nothing Dev Box-related runs; a fresh environment is exactly
#      today's platform. The gateway renders empty BG_DEV_BOX_* values.
# on   Step 12 calls modules/user-services/dev-box/scripts/deploy-dev-box.sh
#      (platform prerequisites, AMI, foundation, controller, seed, verification).
#      Requires environments/<env>/modules/dev-box.tfvars.
# An empty mode is decided by that tfvars file: `stage_enabled = true` turns the
# stage on (dev), anything else leaves it off.
DEV_BOX_TFVARS="$ROOT_DIR/environments/$ENVIRONMENT/modules/dev-box.tfvars"
if [ -z "$DEV_BOX_MODE" ]; then
  DEV_BOX_MODE=off
  if [ -f "$DEV_BOX_TFVARS" ] && grep -Eq '^[[:space:]]*stage_enabled[[:space:]]*=[[:space:]]*true' "$DEV_BOX_TFVARS"; then
    DEV_BOX_MODE=on
  fi
fi
case "$DEV_BOX_MODE" in
  on)
    [ -f "$DEV_BOX_TFVARS" ] || fail "--dev-box on requires environments/$ENVIRONMENT/modules/dev-box.tfvars (copy environments/dev/modules/dev-box.tfvars and set the platform-account values); refusing before any apply"
    ok "Dev Box: on — Step 12 runs modules/user-services/dev-box/scripts/deploy-dev-box.sh ($DEV_BOX_TFVARS)" ;;
  off) [ "$DEV_BOX_ONLY" = false ] || fail "--dev-box-only cannot combine with --dev-box off"
       echo "Dev Box: off (optional module not selected; --dev-box on or stage_enabled = true in environments/$ENVIRONMENT/modules/dev-box.tfvars enables it)" ;;
  *) fail "--dev-box must be on or off (got: $DEV_BOX_MODE)" ;;
esac

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
for _scope in "$GATEWAY_ONLY" "$AGENT_FACTORY_ONLY" "$AGENT_CONTEXT_ONLY" "$DEV_BOX_ONLY" "$INSIGHTS_ONLY" "$TOOLS_ONLY" "$TOOLS_ADAPTERS_ONLY"; do
  [ "$_scope" != true ] || _SCOPE_COUNT=$((_SCOPE_COUNT + 1))
done
[ "$_SCOPE_COUNT" -le 1 ] || fail "Choose only one scope flag"
if [ -n "${ADP_TOOLS_ADAPTERS_CONFIG:-}" ] && [ "$DESTROY" = false ] \
  && { [ "$_SCOPE_COUNT" -eq 0 ] || [ "$TOOLS_ADAPTERS_ONLY" = true ]; }; then
  ADP_TOOLS_ADAPTERS_CONFIG=$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).expanduser().resolve(strict=True))' "$ADP_TOOLS_ADAPTERS_CONFIG") \
    || fail "Cannot resolve the private adapter installer configuration"
  export ADP_TOOLS_ADAPTERS_CONFIG
  (cd "$ROOT_DIR" && python3 -m modules.tools.adapters.installer validate \
    --config "$ADP_TOOLS_ADAPTERS_CONFIG" --account "$ADP_ACCOUNT_ID" \
    --region "$AWS_REGION" --environment "$ENVIRONMENT") || fail "Adapter installer configuration does not match this target"
fi
install_tools_adapters() {
  [ -f "${ADP_TOOLS_ADAPTERS_CONFIG:-}" ] || fail "Set ADP_TOOLS_ADAPTERS_CONFIG to the private installer configuration"
  local adapter_work adapter_mode
  adapter_work=$(mktemp -d "${TMPDIR:-/tmp}/adp-tools-adapters-install.XXXXXX")
  adapter_mode=install
  [ "$CI_MODE" = false ] || adapter_mode=plan
  echo "Adapter foundation plans and receipts: $adapter_work"
  (cd "$ROOT_DIR" && python3 -m modules.tools.adapters.installer "$adapter_mode" \
    --config "$ADP_TOOLS_ADAPTERS_CONFIG" --work-dir "$adapter_work" \
    --account "$ADP_ACCOUNT_ID" --region "$AWS_REGION" --environment "$ENVIRONMENT")
}
if [ "$TOOLS_ADAPTERS_ONLY" = true ]; then
  [ "$DESTROY" = false ] && [ "$LOCAL_MODE" = false ] && [ -z "$FROM_PHASE" ] \
    || fail "--tools-adapters-only cannot combine with destroy, local, or from"
  # Each invocation replans against both original backends; interrupted installs
  # converge through a fresh saved plan, never a stale plan or an empty state.
  install_tools_adapters
  exit 0
fi
if [ "$TOOLS_ONLY" = true ]; then
  [ "$DESTROY" = false ] && [ "$UPDATE_MODE" = false ] && [ "$LOCAL_MODE" = false ] && [ -z "$FROM_PHASE" ] || fail "--tools-only cannot combine with destroy, update, local, or from"
  export ADP_TOOLS_ENVIRONMENT="$ENVIRONMENT" ADP_TOOLS_AWS_REGION="$AWS_REGION" ADP_TOOLS_ACCOUNT_ID="$ADP_ACCOUNT_ID"
  args=()
  [ "$CI_MODE" = false ] || args+=(--plan)
  [ "$RESUME" = false ] || args+=(--resume)
  exec bash "$ROOT_DIR/modules/tools/deploy.sh" "${args[@]}"
fi
if [ "$INSIGHTS_ONLY" = true ]; then
  [ "$DESTROY" = false ] && [ "$UPDATE_MODE" = false ] && [ "$RESUME" = false ] && [ "$LOCAL_MODE" = false ] || fail "--insights-only cannot combine with destroy, update, resume, or local modes"
  args=(--infra --env "$ENVIRONMENT" --region "$AWS_REGION")
  if [ "$CI_MODE" = true ]; then args+=(--plan); fi
  exec bash "$ROOT_DIR/modules/insights/deploy.sh" "${args[@]}"
fi
WORKER_MIGRATION=false
if [ -n "${ADP_WORKER_MIGRATION_EVIDENCE:-}" ]; then
  [ "$UPDATE_MODE" = true ] && [ "$_SCOPE_COUNT" -eq 0 ] && [ "$SKIP_WEBHOOK_INGRESS" = false ] \
    && [ "$CONFIRM_DESTRUCTIVE" = false ] || fail "Worker migration requires a full guarded --update"
  WORKER_MIGRATION=true
  export ADP_PORTABLE_RELEASE_CONFIG=true
fi
worker_migration() {
  python3 "$SCRIPT_DIR/upgrade-workers.py" "$1" --directory "$UPGRADE_RUN_DIR" \
    || fail "Worker migration stopped; retain the upgrade directory and retry with the same evidence"
}

# Prepared releases only enter through release/upgrade.py, which checks the
# source, account, manifest and every artifact before any infrastructure apply.
if [ -n "${ADP_RELEASE_DIR:-}" ]; then
  [ "$UPDATE_MODE" = true ] && [ "$ENVIRONMENT" = dev ] && [ "$AWS_REGION" = us-east-1 ] || fail "Prepared releases require --update --env dev --region us-east-1"
  for flag in "$DESTROY" "$CI_MODE" "$LOCAL_MODE" "$CONFIRM_DESTRUCTIVE" "$GATEWAY_ONLY" "$AGENT_FACTORY_ONLY" "$AGENT_CONTEXT_ONLY" "$DEV_BOX_ONLY" "$SKIP_FRONTEND" "$SKIP_BROKER" "$SKIP_ADMIN_BOOTSTRAP" "$SKIP_WEBHOOK_INGRESS" "$SKIP_AGENT_CONTEXT"; do
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
if [ "$RESUME" = true ] && { [ "$DESTROY" = true ] || [ "$CI_MODE" = true ]; }; then
  fail "--resume/--from cannot be combined with --destroy or --ci"
fi
if [ "$CI_MODE" = false ] && [ "$DESTROY" = false ]; then
  source "$SCRIPT_DIR/deploy-checkpoints.sh"
  deploy_checkpoint_init
  # The operator's chat decision is part of the journal for this target.
  deploy_checkpoint note chat-mode "$CHAT_MODE" || fail "Cannot record the chat mode in the deployment journal"
  if [ -n "$CHAT_EXCEPTION" ]; then
    deploy_checkpoint note chat-exception "$CHAT_EXCEPTION" || fail "Cannot record the chat exception in the deployment journal"
  fi
  # The optional Dev Box module leaves no footprint unless selected: the journal
  # note is written only when the stage is on (the mode is always in the resume
  # binding, so a resumed run cannot flip it).
  if [ "$DEV_BOX_MODE" = on ]; then
    deploy_checkpoint note dev-box-mode "$DEV_BOX_MODE" || fail "Cannot record the Dev Box mode in the deployment journal"
  fi
fi


# Validate admin selection before provisioning; updates keep existing administrators.
if [ "$UPDATE_MODE" = false ] && [ "$DESTROY" = false ] && [ "$CI_MODE" = false ] && \
   [ "$AGENT_FACTORY_ONLY" = false ] && [ "$AGENT_CONTEXT_ONLY" = false ] && [ "$DEV_BOX_ONLY" = false ] && [ "$SKIP_ADMIN_BOOTSTRAP" = false ]; then
  python3 "$ROOT_DIR/modules/gateway/scripts/bootstrap-admin.py" --env "$ENVIRONMENT" --check-config \
    || fail "Select ADP_BOOTSTRAP_ADMIN_EMAIL before installing (see bootstrap-administrator.md)."
fi

# ---------------------------------------------------------------------------
# Detect operator's public IP and lock EKS public API to /32 (portable)
# ---------------------------------------------------------------------------
# Anyone cloning this repo can run the script without editing tfvars. The
# detected IP is exported as TF_VAR_eks_public_access_cidrs so Terraform picks
# it up. If the caller already set the env var, respect it.
#
# Every later Terraform and kubectl request must leave from that one address.
# Hosts behind rotating NAT or proxy egress pools pass a single lookup and then
# time out against the EKS API minutes later, so sample the address and refuse
# when the samples disagree. Sampling is a heuristic: a small pool can still
# agree by chance. Prints the distinct IPv4 addresses seen.
detect_public_ips() {
  local ip url seen=""
  for _ in 1 2 3 4 5; do
    ip=$(curl -fsS --max-time 5 https://checkip.amazonaws.com 2>/dev/null | tr -d '[:space:]' || true)
    [[ "$ip" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || continue
    case " $seen " in *" $ip "*) ;; *) seen="${seen:+$seen }$ip" ;; esac
  done
  if [ -z "$seen" ]; then
    for url in https://api.ipify.org https://ifconfig.me; do
      ip=$(curl -fsS --max-time 5 "$url" 2>/dev/null | tr -d '[:space:]' || true)
      if [[ "$ip" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then seen="$ip"; break; fi
    done
  fi
  printf '%s' "$seen"
}
# The detected /32 only matters while this run can still apply it to a public
# endpoint: not when a resumed run has already completed the platform phase, and
# not when the configuration disables the public endpoint.
eks_public_allowlist_applies() {
  if declare -F deploy_checkpoint >/dev/null && [ "$(deploy_checkpoint status platform 2>/dev/null)" = complete ]; then
    return 1
  fi
  [ "${TF_VAR_eks_endpoint_public_access:-true}" != false ] || return 1
  ! grep -Eq '^[[:space:]]*eks_endpoint_public_access[[:space:]]*=[[:space:]]*false' \
    "$ROOT_DIR/environments/$ENVIRONMENT/platform.tfvars" 2>/dev/null
}
if [ "$UPDATE_MODE" = false ] && [ -z "${TF_VAR_eks_public_access_cidrs:-}" ]; then
  MY_IP=$(detect_public_ips)
  if [ -z "$MY_IP" ]; then
    fail "Could not detect your public IP. Set TF_VAR_eks_public_access_cidrs='[\"<ip>/32\"]' manually."
  fi
  if [ "$MY_IP" != "${MY_IP%% *}" ]; then
    if eks_public_allowlist_applies; then
      fail "Public IP lookups disagree (${MY_IP// /, }): this host's outbound address changes between connections, so a /32 allowlist would lock Terraform and kubectl out of the EKS API. Set TF_VAR_eks_public_access_cidrs='[\"<egress-cidr>\"]' to the range your traffic leaves from, or deploy from a host with a stable address."
    fi
    warn "Public IP lookups disagree (${MY_IP// /, }); continuing because this run does not apply the EKS public allowlist"
    MY_IP="${MY_IP%% *}"
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
  if [ "${UPGRADE_REUSE_CONTEXT:-false}" = false ]; then
    python3 "$SCRIPT_DIR/upgrade-state.py" prepare --directory "$UPGRADE_RUN_DIR" \
      --account "$ACCOUNT_ID" --environment "$ENVIRONMENT" --region "$AWS_REGION" \
      || fail "Cannot safely discover the existing deployment"
    printf '%s\n' "$UPGRADE_RUN_DIR" > "$DEPLOY_CHECKPOINT_FILE.upgrade-path"
  else
    ok "Resuming with the original upgrade preservation snapshot: $UPGRADE_RUN_DIR"
  fi
  source "$UPGRADE_RUN_DIR/context.env"
  resolve_deploy_scope
  if [ -n "${ADP_RELEASE_DIR:-}" ]; then
    [ "$DEPLOY_AGENT_CONTEXT" = false ] || fail "This release contract does not cover agent-context"
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
  COMPATIBILITY_ARGS=()
  [ "$DEPLOY_GATEWAY" = true ] || COMPATIBILITY_ARGS+=(--skip-gateway)
  [ "$DEPLOY_WEBHOOK" = true ] || COMPATIBILITY_ARGS+=(--skip-webhook)
  [ "$WORKER_MIGRATION" = false ] || COMPATIBILITY_ARGS+=(--worker-migration)
  python3 "$SCRIPT_DIR/upgrade-preflight.py" --directory "$UPGRADE_RUN_DIR" \
    --account "$ACCOUNT_ID" --region "$AWS_REGION" --environment "$ENVIRONMENT" \
    "${COMPATIBILITY_ARGS[@]}" || fail "Resolve upgrade compatibility findings before changing this account"
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
      || fail "Cannot reach the existing gateway namespace. Verify network reachability and operator EKS access; see platform_upgrades.md (operator access)."
  fi

  if [ "$DEPLOY_WEBHOOK" = true ]; then
    python3 "$SCRIPT_DIR/upgrade-preflight.py" --directory "$UPGRADE_RUN_DIR" \
      --account "$ACCOUNT_ID" --region "$AWS_REGION" --environment "$ENVIRONMENT" --verify-live-workers \
      || fail "Live worker identity differs from the preserved upgrade configuration"
  fi
  if [ "$WORKER_MIGRATION" = true ]; then
    [ "$DEPLOY_GATEWAY" = true ] && [ "$DEPLOY_WEBHOOK" = true ] || fail "Worker migration requires gateway and webhook"
    worker_migration pause
    ADP_RELEASE_GATEWAY_IMAGE="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["gateway_image"])' "$UPGRADE_RUN_DIR/worker-migration.json")" || fail "Cannot read qualified gateway image"
    ADP_RELEASE_AGENT_RUNTIME_IMAGE="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["worker_image"])' "$UPGRADE_RUN_DIR/worker-migration.json")" || fail "Cannot read qualified worker image"
    export ADP_RELEASE_GATEWAY_IMAGE ADP_RELEASE_AGENT_RUNTIME_IMAGE
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
# Helper: adopt an object that already exists live but is not yet in this stack's
# Terraform state. Several dev objects were first created by hand and declared
# afterwards (#7892); without adoption a plan fails with "already exists".
# Usage: tf_adopt_existing <address> <import-id> <probe command...> -- <terraform var args...>
# The probe decides whether the live object exists; the var args are the same
# -var/-var-file arguments the following apply uses, so the import evaluates the
# configuration identically. A fresh environment has nothing to adopt.
tf_adopt_existing() {
  local ADDR="$1" IMPORT_ID="$2"; shift 2
  local PROBE=()
  while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do PROBE+=("$1"); shift; done
  [ "$#" -gt 0 ] && shift
  if terraform state show "$ADDR" >/dev/null 2>&1; then return 0; fi
  if ! "${PROBE[@]}" >/dev/null 2>&1; then return 0; fi
  echo "Adopting existing $IMPORT_ID into $ADDR..."
  terraform import -input=false "$@" "$ADDR" "$IMPORT_ID" >/dev/null \
    || warn "Could not adopt $IMPORT_ID into $ADDR; the apply will report the conflict"
}

# Helper: run the gateway schema migration Job pinned to the release image before
# the Deployment changes, mirroring gateway-deploy.yml. Nullable schema expansion
# must finish before the new ORM can serve; a failed migration leaves the previous
# release serving. Arguments: <rendered deployment manifest> <release image>.
migrate_gateway_before_rollout() {
  python3 "$ROOT_DIR/modules/gateway/scripts/migrate-before-rollout.py" \
    --manifest "$1" --image "$2" --namespace adp-gateway \
    || fail "Gateway schema migration failed; the previous release keeps serving"
}

# Helper: gateway service role IAM budget (#7944). The gateway module's grants on
# adp-<env>-role-gateway-service are three consolidated managed policies. An
# environment that still carries the old inline policies gets an extra complete
# saved-plan pass before the normal gateway apply, so the role never loses a
# permission: pass 1 attaches the managed policies while every live inline policy
# is retained byte-for-byte (no PutRolePolicy at the limit); the normal pass then
# retires them under the superset gate in upgrade-plan-policy.py. The helper
# script only reads the role; it exits 2 when the attachment quota cannot take
# pass 1 and names what to detach. Arguments: the same plan args the following
# gateway apply uses (ALB vars, engine digest).
gateway_role_policy_cutover() {
  local CUTOVER="$UPGRADE_RUN_DIR/gateway-role-policy-cutover.tfvars.json" PHASE
  PHASE=$(python3 "$ROOT_DIR/modules/gateway/scripts/gateway-role-policy-cutover.py" \
    --environment "$ENVIRONMENT" --region "$AWS_REGION" --output "$CUTOVER") \
    || fail "Cannot prepare the gateway service role policy cutover (see docs/adp-platform-deployment/deployment-reference.md, 'Gateway service role IAM budget')"
  case "$PHASE" in
    cutover)
      step "Step 3a/11: Gateway service role policy cutover (pass 1: attach consolidated policies, retain inline)"
      terraform_update_apply "gateway" "$GATEWAY_UPDATE_VAR_FILE" "$@" -var-file="$CUTOVER"
      ok "Consolidated gateway-role policies attached; the next pass retires the legacy grants"
      ;;
    retire) echo "Gateway service role: consolidated policies attached; this pass retires the legacy grants" ;;
    complete) ;;
    *) fail "Invalid gateway service role cutover phase: $PHASE" ;;
  esac
}

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
  local IMAGE_REPOSITORY="${3:-}"

  if [ "$UPDATE_MODE" = true ] && [ -n "$IMAGE_REPOSITORY" ]; then
    local EXISTING_IMAGE
    EXISTING_IMAGE=$(python3 "$SCRIPT_DIR/upgrade-image-cache.py" \
      "${REGISTRY}/${IMAGE_REPOSITORY}:${IMAGE_TAG}") \
      || fail "Could not verify the existing $IMAGE_REPOSITORY image"
    if [ -n "$EXISTING_IMAGE" ]; then
      ok "Reusing immutable upgrade image: $EXISTING_IMAGE"
      return 0
    fi
  fi

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
if deploy_phase_begin bootstrap; then
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

fi

deploy_phase_complete
fi

# Backend configuration was prepared under the checkpoint lock before hashing
# inputs, so our own placeholder substitutions cannot invalidate a resume.
# CI does not create checkpoints and retains its existing preparation path.
if [ "$CI_MODE" = true ]; then
  python3 "$SCRIPT_DIR/prepare-backends.py" "$ROOT_DIR/environments/$ENVIRONMENT" "$ACCOUNT_ID"
fi

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
# Build once per invocation; on checkpoint resumes CodeBuild uses its existing
# immutable-source cache. Also used before the initial plan for legacy engines.
prepare_gateway_image() {
  [ "${GATEWAY_IMAGE_PREPARED:-false}" = false ] || return 0
  if [ "$LOCAL_MODE" = true ] && docker info &>/dev/null 2>&1; then
    SOURCE_SHA="$SOURCE_SHA" REGISTRY="$REGISTRY" AWS_REGION="$AWS_REGION" \
      bash "$ROOT_DIR/platform/scripts/publish-local-image.sh" adp-gateway
  else
    # Docker build via CodeBuild (Terraform-managed project)
    run_codebuild "adp-${ENVIRONMENT}-gateway-build" "codebuild/bs-gateway-build.yml" adp-gateway
  fi
  GATEWAY_IMAGE=$(python3 "$ROOT_DIR/platform/scripts/resolve-ecr-image.py" "$GATEWAY_IMAGE") \
    || fail "Gateway release digest could not be verified"

  GATEWAY_IMAGE_PREPARED=true
}

prepare_gateway_capacity() {
  CAPACITY_LOCK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/adp-gateway-owner.XXXXXX")
  CAPACITY_OWNER_FILE="$CAPACITY_LOCK_DIR/owner.json"
  capacity_lock() {
    python3 "$SCRIPT_DIR/gateway-capacity-lock.py" "$1" \
      --account "$ACCOUNT_ID" --region "$AWS_REGION" --environment "$ENVIRONMENT" \
      --cluster "$EKS_CLUSTER" --namespace adp-gateway --owner-file "$CAPACITY_OWNER_FILE"
  }
  capacity_lock acquire || fail "Another gateway capacity deployment owns this target"
  capacity_cleanup() {
    local result=$?
    if [ -f "$CAPACITY_OWNER_FILE" ]; then
      capacity_lock release || warn "Gateway capacity owner needs recovery"
    fi
    [ -z "${CAPACITY_BUNDLE:-}" ] || rm -rf "$CAPACITY_BUNDLE"
    rm -rf "$CAPACITY_LOCK_DIR"
    if declare -F deploy_checkpoint_exit >/dev/null; then
      deploy_checkpoint_exit "$result" || true
    fi
    return "$result"
  }
  trap capacity_cleanup EXIT
}

# =============================================================================
# Step 2: Platform infra
# =============================================================================
if deploy_phase_begin platform; then
step "Step 2/11: Deploy shared platform (VPC, EKS, ECR, IAM)"

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

deploy_phase_complete
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
if deploy_phase_begin gateway-infra; then
step "Step 3/11: Deploy gateway infrastructure"

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

  # Keep an existing engine pinned during the first upgrade plan. Fresh installs
  # need the real gateway image before Terraform can resolve the engine digest.
  if [ "$UPDATE_MODE" = true ]; then
    GATEWAY_INITIAL_ENGINE_DIGEST=$(python3 "$ROOT_DIR/modules/gateway/scripts/sync-gateway-engine.py" \
      --current-image-digest --allow-missing --account "$ACCOUNT_ID" --region "$AWS_REGION" --environment "$ENVIRONMENT") \
      || fail "Cannot verify the installed orchestration image before the gateway plan"
    if [ "$GATEWAY_INITIAL_ENGINE_DIGEST" = MISSING ]; then
      python3 - "$UPGRADE_RUN_DIR/engine-before.json" <<'PYENGINE' || fail "Previously installed engine disappeared; refusing to recreate it"
import json, sys
with open(sys.argv[1]) as evidence:
    if json.load(evidence)['missing'] is not True:
        sys.exit("Engine was present during preflight")
PYENGINE
      # Terraform's engine image data source requires an existing immutable image.
      # Platform has now created the repository and build project, so build first.
      prepare_gateway_image
      GATEWAY_INITIAL_ENGINE_DIGEST="${GATEWAY_IMAGE##*@}"
    else
      python3 "$ROOT_DIR/modules/gateway/scripts/sync-gateway-engine.py" \
        --quiesce --account "$ACCOUNT_ID" --region "$AWS_REGION" --environment "$ENVIRONMENT" \
        --product-dispatch-inputs "$UPGRADE_RUN_DIR/gateway.tfvars.json" \
        || fail "Cannot pause and drain the existing engine before gateway changes"
    fi
  else
    prepare_gateway_image
    GATEWAY_INITIAL_ENGINE_DIGEST="${GATEWAY_IMAGE##*@}"
  fi

  # Freeze the old pricing writer before Terraform changes either Lambda.
  python3 "$ROOT_DIR/modules/gateway/scripts/pricing-rollout.py" quiesce \
    --account-id "$ACCOUNT_ID" --environment "$ENVIRONMENT" --region "$AWS_REGION" \
    || fail "Could not quiesce pricing refresh before the gateway update"

  # Gateway infra runs directly — no CodeBuild needed.
  cd "$ROOT_DIR/modules/gateway/infra"
  terraform init -backend-config="../../../environments/$ENVIRONMENT/modules/gateway-backend.tfvars" -input=false -reconfigure
  # Chief-of-Staff flag parameters (#7197) were first set by an operator on dev;
  # adopt any that already exist so the declared value takes over (#7892).
  GATEWAY_ADOPT_VAR_FILE="../../../environments/$ENVIRONMENT/modules/gateway.tfvars"
  [ "$UPDATE_MODE" = true ] && GATEWAY_ADOPT_VAR_FILE="$GATEWAY_UPDATE_VAR_FILE"
  for pair in \
    "aws_ssm_parameter.feature_chat=/adp/${ENVIRONMENT}/gateway/feature-chat" \
    "aws_ssm_parameter.feature_assistant_ui=/adp/${ENVIRONMENT}/gateway/feature-assistant-ui" \
    "aws_ssm_parameter.feature_crew_dashboard=/adp/${ENVIRONMENT}/gateway/feature-crew-dashboard" \
    "aws_ssm_parameter.crew_proxy_enabled=/adp/${ENVIRONMENT}/gateway/crew-proxy-enabled" \
    "aws_ssm_parameter.chat_authority_worker_images=/adp/${ENVIRONMENT}/gateway/chat-authority-worker-images" \
    "aws_ssm_parameter.chat_sandbox_image_digests=/adp/${ENVIRONMENT}/gateway/chat-sandbox-image-digests" \
    "aws_ssm_parameter.chat_crew_image_digests=/adp/${ENVIRONMENT}/gateway/chat-crew-image-digests" \
    "aws_ssm_parameter.chat_worker_service_account=/adp/${ENVIRONMENT}/gateway/chat-worker-service-account" \
    "aws_ssm_parameter.chat_persistent_executor=/adp/${ENVIRONMENT}/gateway/chat-persistent-executor" \
    "aws_ssm_parameter.model_root_bindings=/adp/${ENVIRONMENT}/gateway/model-root-bindings" \
    "aws_ssm_parameter.chat_data_enabled=/adp/${ENVIRONMENT}/gateway/chat-data-enabled" \
    "aws_ssm_parameter.github_user_access_enabled=/adp/${ENVIRONMENT}/gateway/github-user-access-enabled"; do
    tf_adopt_existing "${pair%%=*}" "${pair#*=}" \
      aws ssm get-parameter --name "${pair#*=}" --region "$AWS_REGION" \
      -- -var-file="$GATEWAY_ADOPT_VAR_FILE" -var "orchestration_tick_image_digest=$GATEWAY_INITIAL_ENGINE_DIGEST"
  done
  # Issue #3789: enable_webhook_secrets_kms_grant removed — the CMK now lives in
  # platform infra (applied Step 2) so the gateway grant is unconditional.
  if [ "$UPDATE_MODE" = true ]; then
    # Discover any legacy uncached ALBs before the first Terraform pass.
    ENVIRONMENT="$ENVIRONMENT" AWS_REGION="$AWS_REGION" bash "$SCRIPT_DIR/wire-gateway-alb.sh" \
      || fail "Cannot discover existing gateway load balancers"
    gateway_alb_vars
    # Gateway service role IAM budget (#7944): an environment that still carries
    # the retired inline policies gets pass 1 of the cutover first (see the
    # gateway_role_policy_cutover helper); this apply then retires them.
    gateway_role_policy_cutover "${GATEWAY_ALB_ARGS[@]}" \
      -var "orchestration_tick_image_digest=$GATEWAY_INITIAL_ENGINE_DIGEST"
    terraform_update_apply "gateway" "$GATEWAY_UPDATE_VAR_FILE" "${GATEWAY_ALB_ARGS[@]}" \
      -var "orchestration_tick_image_digest=$GATEWAY_INITIAL_ENGINE_DIGEST"
  else
    # A restarted fresh install may already have its ALBs wired. Preserve them
    # so this first pass does not delete routes and recreate the VPC origin.
    gateway_alb_vars true
    terraform apply -var-file="../../../environments/$ENVIRONMENT/modules/gateway.tfvars" \
      "${GATEWAY_ALB_ARGS[@]+"${GATEWAY_ALB_ARGS[@]}"}" \
      -var "orchestration_tick_image_digest=$GATEWAY_INITIAL_ENGINE_DIGEST" \
      -auto-approve
    ok "Gateway infrastructure deployed"
  fi
fi

deploy_phase_complete
fi

refresh_credentials
# =============================================================================
# Step 4: Build + deploy gateway
# =============================================================================
if deploy_phase_begin gateway; then
step "Step 4/11: Build and deploy gateway"

if [ "$DEPLOY_GATEWAY" = false ]; then
  echo "Skipping gateway deploy (scope exclusion)"
  ok "Skipped"
else
  prepare_gateway_image
  prepare_gateway_capacity

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

  # #8848/#4076: Enforce credential destinations by default. Only an explicit
  # "false" opts into temporary shadow mode; reject typos.
  VAULT_ENFORCE_CREDENTIAL_HOST_BINDING=$(_get_ssm "/adp/$ENVIRONMENT/gateway/vault-enforce-credential-host-binding" "true")
  case "$VAULT_ENFORCE_CREDENTIAL_HOST_BINDING" in
    ""|None) VAULT_ENFORCE_CREDENTIAL_HOST_BINDING="true" ;;
    true) ;;
    false) echo "WARNING: Credential destination binding explicitly disabled (shadow mode)" >&2 ;;
    *) echo "ERROR: vault-enforce-credential-host-binding must be true or false" >&2; exit 1 ;;
  esac

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
  VAULT_PROXY_HOST_ALLOWLIST=$(_get_ssm "/adp/$ENVIRONMENT/gateway/vault-proxy-host-allowlist" "api.github.com,api.openai.com,api.anthropic.com,api.stripe.com,slack.com")
  if [ "$VAULT_PROXY_HOST_ALLOWLIST" = "None" ]; then VAULT_PROXY_HOST_ALLOWLIST="api.github.com,api.openai.com,api.anthropic.com,api.stripe.com,slack.com"; fi

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
  # Crew proxy tuning and checkpoints (#7902): gateway-infra SSM parameters
  # (chief-of-staff-flags.tf, crew-checkpoints.tf); same parameters and defaults as
  # gateway-deploy.yml. Malformed numbers fall back to the code's defaults; the
  # bucket parameter exists only while crew_checkpoints_enabled is on (absent => "").
  CREW_PROXY_KEEPALIVE_SECONDS=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/crew-proxy-keepalive-seconds" "25")
  [[ "$CREW_PROXY_KEEPALIVE_SECONDS" =~ ^[0-9]+$ ]] || CREW_PROXY_KEEPALIVE_SECONDS="25"
  CREW_PROXY_MAX_CONN_PER_USER=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/crew-proxy-max-conn-per-user" "64")
  [[ "$CREW_PROXY_MAX_CONN_PER_USER" =~ ^[0-9]+$ ]] || CREW_PROXY_MAX_CONN_PER_USER="64"
  ADP_CREW_CHECKPOINTS_ENABLED=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/crew-checkpoints-enabled" "false")
  [ "$ADP_CREW_CHECKPOINTS_ENABLED" = "true" ] || ADP_CREW_CHECKPOINTS_ENABLED="false"
  ADP_GITHUB_USER_ACCESS_ENABLED=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/github-user-access-enabled" "false")
  [ "$ADP_GITHUB_USER_ACCESS_ENABLED" = "true" ] || ADP_GITHUB_USER_ACCESS_ENABLED="false"
  # The Dev Box controller reads the Crew dashboard switch and edge origin from this
  # ConfigMap (it has no Deployment env of its own); same parameters as the Deployment
  # render later in this step (#8223 cause 20).
  FEATURE_CREW_DASHBOARD=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/feature-crew-dashboard" "false")
  [ -n "$FEATURE_CREW_DASHBOARD" ] && [ "$FEATURE_CREW_DASHBOARD" != "None" ] || FEATURE_CREW_DASHBOARD="false"
  CREW_DASHBOARD_ORIGIN=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/crew-dashboard-origin" "")
  if [ "$CREW_DASHBOARD_ORIGIN" = "None" ]; then CREW_DASHBOARD_ORIGIN=""; fi
  # #8387: tools consent posture; anything but granted|explicit falls back to granted (the code fails closed on invalid values).
  ADP_TOOLS_CONSENT_DEFAULT=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/tools-consent-default" "granted")
  [[ "$ADP_TOOLS_CONSENT_DEFAULT" =~ ^(granted|explicit)$ ]] || ADP_TOOLS_CONSENT_DEFAULT="granted"
  ADP_CREW_CHECKPOINT_BUCKET=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/crew-checkpoint-bucket" "")
  [ "$ADP_CREW_CHECKPOINT_BUCKET" != "None" ] || ADP_CREW_CHECKPOINT_BUCKET=""
  ADP_CREW_CHECKPOINT_RETENTION_SECONDS=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/crew-checkpoint-retention-seconds" "2592000")
  [[ "$ADP_CREW_CHECKPOINT_RETENTION_SECONDS" =~ ^[0-9]+$ ]] || ADP_CREW_CHECKPOINT_RETENTION_SECONDS="2592000"
  # Shared AgentCore user memory (#7901): agentcore-memory.tf writes all four
  # parameters while agentcore_memory_enabled is on. agentcore_recall() treats
  # all four empty as "memory disabled" and a partial set as an outage ("memory
  # target unconfigured"), so a partial set is refused here, before any rollout.
  # The namespace key is a SecureString: decrypted here, landed only in the
  # bedrockgateway-secrets Secret below, never printed and never in the ConfigMap.
  ADP_AGENTCORE_MEMORY_ID=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/agentcore-memory-id" "")
  ADP_AGENTCORE_MEMORY_STRATEGY_ID=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/agentcore-memory-strategy-id" "")
  ADP_AGENTCORE_MEMORY_REGION=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/agentcore-memory-region" "")
  ADP_AGENTCORE_NAMESPACE_KEY=$(aws ssm get-parameter \
    --name "/adp/${ENVIRONMENT}/gateway/agentcore-namespace-key" \
    --with-decryption --query Parameter.Value --output text --region "$AWS_REGION" 2>/dev/null || echo "")
  _memory_values_set=0
  for _name in ADP_AGENTCORE_MEMORY_ID ADP_AGENTCORE_MEMORY_STRATEGY_ID ADP_AGENTCORE_MEMORY_REGION ADP_AGENTCORE_NAMESPACE_KEY; do
    [ "${!_name}" != "None" ] || printf -v "$_name" '%s' ""
    [ -z "${!_name}" ] || _memory_values_set=$((_memory_values_set + 1))
  done
  if [ "$_memory_values_set" -ne 0 ] && [ "$_memory_values_set" -ne 4 ]; then
    fail "AgentCore memory is partially configured (${_memory_values_set}/4 of /adp/${ENVIRONMENT}/gateway/agentcore-{memory-id,memory-strategy-id,memory-region,namespace-key} set); apply gateway infra with agentcore_memory_enabled fully on or off"
  fi
  if [ -n "$ADP_AGENTCORE_MEMORY_REGION" ] && [ "$ADP_AGENTCORE_MEMORY_REGION" != "$AWS_REGION" ]; then
    fail "AgentCore memory region ${ADP_AGENTCORE_MEMORY_REGION} differs from the gateway region ${AWS_REGION}; the gateway refuses recall across regions"
  fi
  SECRET_APPLY_RESULT=$(kubectl create secret generic bedrockgateway-secrets \
    --from-literal=token-secret-key="$TOKEN_SECRET" \
    --from-literal=internal-api-key="$INTERNAL_API_KEY" \
    --from-literal=apigw-provenance-secret="$APIGW_PROVENANCE_SECRET" \
    --from-literal=magic-link-secret="$MAGIC_LINK_SECRET" \
    --from-literal=agentcore-namespace-key="$ADP_AGENTCORE_NAMESPACE_KEY" \
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

  # Model invocability probing (#7967): gateway-infra SSM parameters
  # (model-probe.tf, values from the environment's gateway tfvars); same parameters,
  # defaults and validation in gateway-deploy.yml and deploy-all.sh. Absent or
  # malformed values render the fail-closed defaults (off, 0 slots, 0 budget, whole
  # catalogue, 24 h). Delegated chat enforces model policy and refuses every turn
  # without this evidence, so an environment running --chat delegated must arm it.
  MODEL_PROBE_ENABLED=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/model-probe-enabled" "false")
  [ "$MODEL_PROBE_ENABLED" = "true" ] || MODEL_PROBE_ENABLED="false"
  MODEL_PROBE_MAX_SLOTS_PER_CYCLE=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/model-probe-max-slots-per-cycle" "0")
  [[ "$MODEL_PROBE_MAX_SLOTS_PER_CYCLE" =~ ^[0-9]+$ ]] || MODEL_PROBE_MAX_SLOTS_PER_CYCLE="0"
  MODEL_PROBE_BUDGET_USD_PER_CYCLE=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/model-probe-budget-usd-per-cycle" "0")
  [[ "$MODEL_PROBE_BUDGET_USD_PER_CYCLE" =~ ^[0-9]+(\.[0-9]{1,6})?$ ]] || MODEL_PROBE_BUDGET_USD_PER_CYCLE="0"
  MODEL_PROBE_MAX_BUDGET_USD_PER_ATTEMPT=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/model-probe-max-budget-usd-per-attempt" "0")
  [[ "$MODEL_PROBE_MAX_BUDGET_USD_PER_ATTEMPT" =~ ^[0-9]+(\.[0-9]{1,6})?$ ]] || MODEL_PROBE_MAX_BUDGET_USD_PER_ATTEMPT="0"
  MODEL_PROBE_MODEL_ALLOWLIST=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/model-probe-model-allowlist" "[]")
  # Must be a JSON array of strings; anything else means the whole catalogue ([]).
  MODEL_PROBE_MODEL_ALLOWLIST=$(printf '%s' "$MODEL_PROBE_MODEL_ALLOWLIST" | python3 -c 'import json, sys
try:
    v = json.loads(sys.stdin.read())
    assert isinstance(v, list) and all(isinstance(x, str) for x in v)
except Exception:
    v = []
print(json.dumps(v, separators=(",", ":")))')
  MODEL_PROBE_MODEL_ALLOWLIST_SED=${MODEL_PROBE_MODEL_ALLOWLIST//\\/\\\\}
  MODEL_PROBE_MODEL_ALLOWLIST_SED=${MODEL_PROBE_MODEL_ALLOWLIST_SED//&/\\&}
  MODEL_PROBE_MODEL_ALLOWLIST_SED=${MODEL_PROBE_MODEL_ALLOWLIST_SED//|/\\|}
  MODEL_PROBE_EVIDENCE_TTL_HOURS=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/model-probe-evidence-ttl-hours" "24")
  [[ "$MODEL_PROBE_EVIDENCE_TTL_HOURS" =~ ^[0-9]+$ ]] || MODEL_PROBE_EVIDENCE_TTL_HOURS="24"
  # Dev Box (#7998, optional module): /adp/${ENVIRONMENT}/dev-box/* exists only
  # where modules/user-services/dev-box/scripts/deploy-dev-box.sh ran. Absent =>
  # "" / "0": admission disabled, bounds fail closed, no desktop option in the UI.
  BG_DEV_BOX_DEPLOYMENT_ID=$(_get_ssm "/adp/${ENVIRONMENT}/dev-box/deployment-id" "")
  [ "$BG_DEV_BOX_DEPLOYMENT_ID" != "None" ] && [ "$BG_DEV_BOX_DEPLOYMENT_ID" != " " ] || BG_DEV_BOX_DEPLOYMENT_ID=""
  BG_DEV_BOX_MAX_DEPLOYMENT_LIVE_BOXES=$(_get_ssm "/adp/${ENVIRONMENT}/dev-box/max-deployment-live-boxes" "0")
  [[ "$BG_DEV_BOX_MAX_DEPLOYMENT_LIVE_BOXES" =~ ^[0-9]+$ ]] || BG_DEV_BOX_MAX_DEPLOYMENT_LIVE_BOXES="0"
  BG_DEV_BOX_MAX_DEPLOYMENT_RUNNING_BOXES=$(_get_ssm "/adp/${ENVIRONMENT}/dev-box/max-deployment-running-boxes" "0")
  [[ "$BG_DEV_BOX_MAX_DEPLOYMENT_RUNNING_BOXES" =~ ^[0-9]+$ ]] || BG_DEV_BOX_MAX_DEPLOYMENT_RUNNING_BOXES="0"
  BG_DEV_BOX_MAX_DEPLOYMENT_STORAGE_GB=$(_get_ssm "/adp/${ENVIRONMENT}/dev-box/max-deployment-storage-gb" "0")
  [[ "$BG_DEV_BOX_MAX_DEPLOYMENT_STORAGE_GB" =~ ^[0-9]+$ ]] || BG_DEV_BOX_MAX_DEPLOYMENT_STORAGE_GB="0"
  BG_DEV_BOX_MAX_DEPLOYMENT_RUNNING_HOURS=$(_get_ssm "/adp/${ENVIRONMENT}/dev-box/max-deployment-running-hours" "0")
  [[ "$BG_DEV_BOX_MAX_DEPLOYMENT_RUNNING_HOURS" =~ ^[0-9]+$ ]] || BG_DEV_BOX_MAX_DEPLOYMENT_RUNNING_HOURS="0"
  BG_DEV_BOX_MAX_DEPLOYMENT_BUDGET_USD=$(_get_ssm "/adp/${ENVIRONMENT}/dev-box/max-deployment-budget-usd" "0")
  [[ "$BG_DEV_BOX_MAX_DEPLOYMENT_BUDGET_USD" =~ ^[0-9]+(\.[0-9]{1,6})?$ ]] || BG_DEV_BOX_MAX_DEPLOYMENT_BUDGET_USD="0"
  # SSH grant role (#8049): published by the Dev Box foundation when gateway_principal_arn
  # is set. Absent, " " sentinel or malformed => "" and POST /cloud-desktops/{id}/ssh-access
  # keeps failing closed with 503 "Dev Box SSH access is not configured".
  BG_DEV_BOX_SSH_GRANT_ROLE_ARN=$(_get_ssm "/adp/${ENVIRONMENT}/dev-box/ssh-grant-role-arn" "")
  [ "$BG_DEV_BOX_SSH_GRANT_ROLE_ARN" != "None" ] && [ "$BG_DEV_BOX_SSH_GRANT_ROLE_ARN" != " " ] || BG_DEV_BOX_SSH_GRANT_ROLE_ARN=""
  [[ "$BG_DEV_BOX_SSH_GRANT_ROLE_ARN" =~ ^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9_+=,.@/-]+$ ]] || BG_DEV_BOX_SSH_GRANT_ROLE_ARN=""
  BG_DEV_BOX_CREW_BOOTSTRAP=$(_get_ssm "/adp/${ENVIRONMENT}/dev-box/crew-bootstrap" "image")
  [[ "$BG_DEV_BOX_CREW_BOOTSTRAP" =~ ^(image|user-data)$ ]] || BG_DEV_BOX_CREW_BOOTSTRAP="image"
  # Model policy posture for chat (#7997): off | advisory | enforced, from gateway infra
  # chief-of-staff-flags.tf (chat_model_policy_posture in the environment's gateway
  # tfvars). Absent or unrecognised renders the default, advisory; same in gateway-deploy.yml.
  CHAT_MODEL_POLICY_POSTURE=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/chat-model-policy-posture" "advisory")
  case "$CHAT_MODEL_POLICY_POSTURE" in off|advisory|enforced) ;; *) CHAT_MODEL_POLICY_POSTURE="advisory" ;; esac

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
  ATLASSIAN_CONFIG_REF=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/atlassian-mcp-config-ref" "")
  ATLASSIAN_CONTRACT_REVISION=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/atlassian-mcp-contract-revision" "")
  ATLASSIAN_METADATA_SHA256=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/atlassian-mcp-metadata-sha256" "")
  if [[ "$ATLASSIAN_CONFIG_REF" != "None" && -n "$ATLASSIAN_CONFIG_REF" ]] &&
     [[ ! "$ATLASSIAN_CONFIG_REF" =~ ^arn:aws:secretsmanager:${AWS_REGION}:${ACCOUNT_ID}:secret:adp/${ENVIRONMENT}/connections/atlassian-mcp/config-[A-Za-z0-9]{6}$ ]]; then
    fail "Invalid Atlassian MCP config reference"
  fi
  if [[ "$ATLASSIAN_CONTRACT_REVISION" != "None" && -n "$ATLASSIAN_CONTRACT_REVISION" ]] &&
     [[ ! "$ATLASSIAN_CONTRACT_REVISION" =~ ^[A-Za-z0-9_.-]{1,128}$ ]]; then
    fail "Invalid Atlassian MCP contract revision"
  fi
  if [[ "$ATLASSIAN_METADATA_SHA256" != "None" && -n "$ATLASSIAN_METADATA_SHA256" ]] &&
     [[ ! "$ATLASSIAN_METADATA_SHA256" =~ ^[0-9a-f]{64}$ ]]; then
    fail "Invalid Atlassian MCP metadata digest"
  fi
  for name in ATLASSIAN_CONFIG_REF ATLASSIAN_CONTRACT_REVISION ATLASSIAN_METADATA_SHA256; do
    [ "${!name}" != "None" ] || printf -v "$name" '%s' ""
  done
  if [[ -n "$ATLASSIAN_CONTRACT_REVISION" || -n "$ATLASSIAN_METADATA_SHA256" ]] &&
     [[ -z "$ATLASSIAN_CONFIG_REF" || -z "$ATLASSIAN_CONTRACT_REVISION" || -z "$ATLASSIAN_METADATA_SHA256" ]]; then
    fail "Incomplete Atlassian MCP provider plan binding"
  fi
  AGENT_AUTHORITY_ENABLED=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/agent-authority-enabled" "false")
  ADP_WORK_CLAIMS_ENABLED=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/work-claims-enabled" "false")
  ADP_PRODUCT_STORY_DISPATCH_ENABLED=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/product-story-dispatch-enabled" "false")
  ADP_WORK_CLAIM_PRODUCER_ROLES=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/work-claim-producer-roles" "")
  # The table name is published as a SecureString by webhook-ingress; a renderer
  # identity without kms:Decrypt on that key must not blank it (an empty value
  # disables model-root registration: external_roots.root_store). The name is
  # deterministic, so fall back to it (parity with WEBHOOK_EVENTS_TABLE).
  AGENT_AUTHORITY_TABLE=$(get_authority_ssm "/adp/${ENVIRONMENT}/webhook-ingress/agent-authority-table" "adp-${ENVIRONMENT}-agent-authority")
  WEBHOOK_EVENTS_TABLE=$(_get_ssm "/adp/${ENVIRONMENT}/webhook-ingress/webhook-events-table" "adp-${ENVIRONMENT}-webhook-events")
  ADP_TASK_API_ADMISSION_ENABLED=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/task-api-admission-enabled" "false")
  ADP_TASK_API_READ_ENABLED=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/task-api-read-enabled" "false")
  ADP_TASK_API_WORKER_ENABLED=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/task-api-worker-enabled" "false")
  ADP_TASK_API_RECOVERY_ENABLED=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/task-api-recovery-enabled" "false")
  TASK_ARTIFACT_BUCKET_NAME=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/task-artifact-bucket-name" "")
  PRODUCT_SOURCE_SNAPSHOT_BUCKET=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/product-source-snapshot-bucket" "")
  [[ "$PRODUCT_SOURCE_SNAPSHOT_BUCKET" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]] || PRODUCT_SOURCE_SNAPSHOT_BUCKET=""
  # Delegated chat memory/artifact stores (#8028): agent-factory publishes the table
  # names (chat-agent-infra.tf); the bucket is the task-api artifact bucket read above.
  # Validated as names, with the deterministic defaults so a fresh environment is
  # configured before the parameters exist; an invalid bucket renders empty (503
  # chat_artifacts_unconfigured, which the sandbox degrades on rather than fails).
  MEMORY_TABLE=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/chat-memory-table" "adp-${ENVIRONMENT}-agent-memory")
  ARTIFACTS_TABLE=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/chat-artifacts-table" "adp-${ENVIRONMENT}-chat-artifacts")
  ARTIFACTS_BUCKET="${TASK_ARTIFACT_BUCKET_NAME}"
  [[ "$MEMORY_TABLE" =~ ^[A-Za-z0-9_.-]{3,255}$ ]] && [ "$MEMORY_TABLE" != "None" ] || MEMORY_TABLE="adp-${ENVIRONMENT}-agent-memory"
  [[ "$ARTIFACTS_TABLE" =~ ^[A-Za-z0-9_.-]{3,255}$ ]] && [ "$ARTIFACTS_TABLE" != "None" ] || ARTIFACTS_TABLE="adp-${ENVIRONMENT}-chat-artifacts"
  [[ "$ARTIFACTS_BUCKET" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]] || ARTIFACTS_BUCKET=""
  ADP_TASK_API_QUEUE_URL=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/task-api-queue-url" "")
  ADP_TASK_ADMISSION_PRODUCER_ROLES=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/task-admission-producer-roles" "")
  ADP_TASK_DISPATCH_PRODUCER_ROLES=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/task-dispatch-producer-roles" "")
  ADP_TASK_RECOVERY_PRODUCER_ROLES=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/task-recovery-producer-roles" "")
  ADP_TASK_QUALIFICATION_ID=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/task-qualification-id" "")
  ADP_TASK_WORKER_IMAGE_DIGESTS=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/task-worker-image-digests" "disabled")
  ADP_TASK_WORKER_SERVICE_ACCOUNT=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/task-worker-service-account" "agent-scaledjob-sa")
  AGENT_DISPATCH_QUEUE_URL=$(_get_ssm "/adp/${ENVIRONMENT}/webhook-ingress/sqs-queue-url" "")
  AGENT_WORKER_IMAGE_DIGESTS=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/agent-authority-worker-images" "disabled")
  ADP_MODEL_ROOT_BINDINGS_RAW=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/model-root-bindings" "[]")
  ADP_MODEL_ROOT_BINDINGS_SED=$(printf '%s' "$ADP_MODEL_ROOT_BINDINGS_RAW" | python3 "$ROOT_DIR/platform/scripts/render-model-root-config.py" bindings)
  ADP_ARC_MODEL_BINDINGS_RAW=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/arc-model-bindings" "[]")
  ADP_ARC_MODEL_BINDINGS_SED=$(printf '%s' "$ADP_ARC_MODEL_BINDINGS_RAW" | python3 "$ROOT_DIR/platform/scripts/render-model-root-config.py" bindings)
  # Chat intake, chat queues and tools-plane identities (parity with
  # gateway-deploy.yml; the SSM parameters are published by the agent-factory
  # and tools stacks). Empty values leave the corresponding surface disabled.
  ADP_TOOLS_GATEWAY_ROLE_ARN=$(_get_ssm "/adp/${ENVIRONMENT}/tools/target-role-arn" "")
  ADP_TOOLS_FACADE_ROLE_ARN=$(_get_ssm "/adp/${ENVIRONMENT}/tools/caller-role-arn" "")
  ADP_TOOLS_ASSERTION_KEY_ARN=$(_get_ssm "/adp/${ENVIRONMENT}/tools/assertion-signing-key-arn" "")
  NATIVE_CREDENTIALS_VALUES=$(python3 "${ROOT_DIR}/modules/gateway/scripts/native-credential-config.py" "$ENVIRONMENT" "$AWS_REGION" "${ACCOUNT_ID}")
  read -r ADP_NATIVE_CREDENTIALS_ENABLED ADP_NATIVE_CREDENTIALS_ISSUER ADP_NATIVE_CREDENTIALS_APPROVAL_ORIGIN ADP_NATIVE_CREDENTIALS_KEY_ID ADP_NATIVE_CREDENTIALS_KEY_ARN <<< "$NATIVE_CREDENTIALS_VALUES"
  BG_INTAKE_SESSIONS_TABLE=$(_get_ssm "/adp/${ENVIRONMENT}/agent-gateway/sessions-table" "adp-${ENVIRONMENT}-agent-gateway-sessions")
  ADP_CHAT_RESPONSE_QUEUE_URL=$(_get_ssm "/adp/${ENVIRONMENT}/agent-gateway/response-queue-url" "")
  ADP_CHAT_INPUT_QUEUE_URL=$(_get_ssm "/adp/${ENVIRONMENT}/agent-gateway/chat-input-queue-url" "")
  BG_INTAKE_CONTEXT_TABLE=$(_get_ssm "/adp/${ENVIRONMENT}/agent-gateway/context-table" "adp-${ENVIRONMENT}-chat-context")
  BG_INTAKE_INGEST_FUNCTION=$(_get_ssm "/adp/${ENVIRONMENT}/agent-gateway/ingest-function" "adp-${ENVIRONMENT}-agent-gateway-ingest")
  # Delegated chat sandbox transport: the CA ConfigMap name is published by
  # platform/scripts/apply-chat-tls.sh (--chat delegated); empty leaves both empty.
  ADP_CHAT_DATA_ENABLED=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/chat-data-enabled" "false")
  [ "$ADP_CHAT_DATA_ENABLED" = true ] || ADP_CHAT_DATA_ENABLED=false
  ADP_CHAT_SANDBOX_CA_CONFIGMAP=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/chat-sandbox-ca-configmap" "")
  [ "$ADP_CHAT_SANDBOX_CA_CONFIGMAP" != "None" ] || ADP_CHAT_SANDBOX_CA_CONFIGMAP=""
  ADP_CHAT_DATA_URL=""
  [ -z "$ADP_CHAT_SANDBOX_CA_CONFIGMAP" ] || ADP_CHAT_DATA_URL="https://chat-sandbox-gateway.adp-gateway.svc:8443"
  ADP_CHAT_WORKER_IMAGE_DIGESTS_RAW=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/chat-authority-worker-images" "")
  ADP_CHAT_WORKER_IMAGE_DIGESTS_SED=$(printf '%s' "$ADP_CHAT_WORKER_IMAGE_DIGESTS_RAW" | python3 "$ROOT_DIR/platform/scripts/render-model-root-config.py" images)
  # Delegated chat workload identities, published by `deploy-all.sh --chat delegated`
  # (#7899); the Terraform seeds are empty digests and the legacy adp-agent account.
  ADP_CHAT_SANDBOX_IMAGE_DIGESTS_RAW=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/chat-sandbox-image-digests" "")
  ADP_CHAT_SANDBOX_IMAGE_DIGESTS_SED=$(printf '%s' "$ADP_CHAT_SANDBOX_IMAGE_DIGESTS_RAW" | python3 "$ROOT_DIR/platform/scripts/render-model-root-config.py" images)
  ADP_CHAT_CREW_IMAGE_DIGESTS_RAW=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/chat-crew-image-digests" "")
  ADP_CHAT_CREW_IMAGE_DIGESTS_SED=$(printf '%s' "$ADP_CHAT_CREW_IMAGE_DIGESTS_RAW" | python3 "$ROOT_DIR/platform/scripts/render-model-root-config.py" images)
  ADP_CHAT_WORKER_SERVICE_ACCOUNT=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/chat-worker-service-account" "adp-agent")
  [[ "$ADP_CHAT_WORKER_SERVICE_ACCOUNT" =~ ^(adp-agent|adp-chat-supervisor)$ ]] || ADP_CHAT_WORKER_SERVICE_ACCOUNT="adp-agent"
  AGENT_AUTHORITY_KEY_ID=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/agent-authority-key-id" "disabled")
  AGENT_TASK_SOURCE_ROLE_ARN=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/agent-task-source-role-arn" "")
  AGENT_TASK_SOURCE_EKS_CLUSTER=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/agent-task-source-eks-cluster" "")
  AGENT_TASK_SOURCE_ISOLATION_CONFIRMED=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/agent-task-source-isolation-confirmed" "false")

  PLATFORM_OPERATOR_ROLE_ARNS=$(python3 "${ROOT_DIR}/modules/gateway/scripts/platform-operator-roles.py" "$ENVIRONMENT" "$AWS_REGION" "$EFFECTIVE_ACCOUNT")
  CONFIGMAP_APPLY_RESULT=$(sed -e "s|__AWS_REGION__|${AWS_REGION}|g" \
      -e "s|__ENVIRONMENT__|${ENVIRONMENT}|g" \
      -e "s|__ATLASSIAN_CONFIG_REF__|${ATLASSIAN_CONFIG_REF}|g" \
      -e "s|__ATLASSIAN_CONTRACT_REVISION__|${ATLASSIAN_CONTRACT_REVISION}|g" \
      -e "s|__ATLASSIAN_METADATA_SHA256__|${ATLASSIAN_METADATA_SHA256}|g" \
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
      -e "s|__SCOPED_KNOWLEDGE_IMPORTS_ENABLED__|${SCOPED_KNOWLEDGE_IMPORTS_ENABLED:-false}|g" \
      -e "s|__AGENT_RUN_LOGS_BUCKET__|${AGENT_RUN_LOGS_BUCKET}|g" \
      -e "s|__BUDGET_FAIL_MODE__|${BUDGET_FAIL_MODE}|g" \
      -e "s|__VAULT_ENFORCE_CREDENTIAL_HOST_BINDING__|${VAULT_ENFORCE_CREDENTIAL_HOST_BINDING}|g" \
      -e "s|__COGNITO_CLI_CLIENT_ID__|${COGNITO_CLI_CLIENT_ID}|g" \
      -e "s|__BEDROCK_ROUTING_SHADOW_MODE__|${BEDROCK_ROUTING_SHADOW_MODE}|g" \
      -e "s|__PLATFORM_BEDROCK_ACCOUNT_ID__|${EFFECTIVE_ACCOUNT}|g" \
      -e "s|__PLATFORM_OPERATOR_ROLE_ARNS__|${PLATFORM_OPERATOR_ROLE_ARNS}|g" \
      -e "s|__PERSONA_MODEL_MAPPING_ENABLED__|${PERSONA_MODEL_MAPPING_ENABLED}|g" \
      -e "s|__MODEL_ALLOWED_MODELS_CONFIG__|${MODEL_ALLOWED_MODELS_CONFIG_SED}|g" \
      -e "s|__MODEL_PROBE_ENABLED__|${MODEL_PROBE_ENABLED}|g" \
      -e "s|__MODEL_PROBE_MAX_SLOTS_PER_CYCLE__|${MODEL_PROBE_MAX_SLOTS_PER_CYCLE}|g" \
      -e "s|__MODEL_PROBE_BUDGET_USD_PER_CYCLE__|${MODEL_PROBE_BUDGET_USD_PER_CYCLE}|g" \
      -e "s|__MODEL_PROBE_MAX_BUDGET_USD_PER_ATTEMPT__|${MODEL_PROBE_MAX_BUDGET_USD_PER_ATTEMPT}|g" \
      -e "s|__MODEL_PROBE_MODEL_ALLOWLIST__|${MODEL_PROBE_MODEL_ALLOWLIST_SED}|g" \
      -e "s|__MODEL_PROBE_EVIDENCE_TTL_HOURS__|${MODEL_PROBE_EVIDENCE_TTL_HOURS}|g" \
      -e "s|__BG_DEV_BOX_DEPLOYMENT_ID__|${BG_DEV_BOX_DEPLOYMENT_ID}|g" \
      -e "s|__BG_DEV_BOX_MAX_DEPLOYMENT_LIVE_BOXES__|${BG_DEV_BOX_MAX_DEPLOYMENT_LIVE_BOXES}|g" \
      -e "s|__BG_DEV_BOX_MAX_DEPLOYMENT_RUNNING_BOXES__|${BG_DEV_BOX_MAX_DEPLOYMENT_RUNNING_BOXES}|g" \
      -e "s|__BG_DEV_BOX_MAX_DEPLOYMENT_STORAGE_GB__|${BG_DEV_BOX_MAX_DEPLOYMENT_STORAGE_GB}|g" \
      -e "s|__BG_DEV_BOX_MAX_DEPLOYMENT_RUNNING_HOURS__|${BG_DEV_BOX_MAX_DEPLOYMENT_RUNNING_HOURS}|g" \
      -e "s|__BG_DEV_BOX_MAX_DEPLOYMENT_BUDGET_USD__|${BG_DEV_BOX_MAX_DEPLOYMENT_BUDGET_USD}|g" \
      -e "s|__BG_DEV_BOX_SSH_GRANT_ROLE_ARN__|${BG_DEV_BOX_SSH_GRANT_ROLE_ARN}|g" \
      -e "s|__BG_DEV_BOX_CREW_BOOTSTRAP__|${BG_DEV_BOX_CREW_BOOTSTRAP}|g" \
      -e "s|__CHAT_MODEL_POLICY_POSTURE__|${CHAT_MODEL_POLICY_POSTURE}|g" \
      -e "s|__AGENT_CONTROL_CLUSTER_POD_CIDRS__|${AGENT_CONTROL_CLUSTER_POD_CIDRS}|g" \
      -e "s|__AGENT_AUTHORITY_ENABLED__|${AGENT_AUTHORITY_ENABLED}|g" \
      -e "s|__ADP_WORK_CLAIMS_ENABLED__|${ADP_WORK_CLAIMS_ENABLED}|g" \
      -e "s|__ADP_PRODUCT_STORY_DISPATCH_ENABLED__|${ADP_PRODUCT_STORY_DISPATCH_ENABLED}|g" \
      -e "s|__ADP_WORK_CLAIM_PRODUCER_ROLES__|${ADP_WORK_CLAIM_PRODUCER_ROLES}|g" \
      -e "s|__AGENT_AUTHORITY_TABLE__|${AGENT_AUTHORITY_TABLE}|g" \
      -e "s|__WEBHOOK_EVENTS_TABLE__|${WEBHOOK_EVENTS_TABLE}|g" \
      -e "s|__ADP_TOOLS_GATEWAY_ROLE_ARN__|${ADP_TOOLS_GATEWAY_ROLE_ARN}|g" \
      -e "s|__ADP_TOOLS_FACADE_ROLE_ARN__|${ADP_TOOLS_FACADE_ROLE_ARN}|g" \
      -e "s|__ADP_TOOLS_ASSERTION_KEY_ARN__|${ADP_TOOLS_ASSERTION_KEY_ARN}|g" \
      -e "s|__ADP_NATIVE_CREDENTIALS_ENABLED__|${ADP_NATIVE_CREDENTIALS_ENABLED}|g" \
      -e "s|__ADP_NATIVE_CREDENTIALS_ISSUER__|${ADP_NATIVE_CREDENTIALS_ISSUER}|g" \
      -e "s|__ADP_NATIVE_CREDENTIALS_APPROVAL_ORIGIN__|${ADP_NATIVE_CREDENTIALS_APPROVAL_ORIGIN}|g" \
      -e "s|__ADP_NATIVE_CREDENTIALS_KEY_ID__|${ADP_NATIVE_CREDENTIALS_KEY_ID}|g" \
      -e "s|__ADP_NATIVE_CREDENTIALS_KEY_ARN__|${ADP_NATIVE_CREDENTIALS_KEY_ARN}|g" \
      -e "s|__BG_INTAKE_SESSIONS_TABLE__|${BG_INTAKE_SESSIONS_TABLE}|g" \
      -e "s|__ADP_CHAT_RESPONSE_QUEUE_URL__|${ADP_CHAT_RESPONSE_QUEUE_URL}|g" \
      -e "s|__ADP_CHAT_INPUT_QUEUE_URL__|${ADP_CHAT_INPUT_QUEUE_URL}|g" \
      -e "s|__BG_INTAKE_CONTEXT_TABLE__|${BG_INTAKE_CONTEXT_TABLE}|g" \
      -e "s|__BG_INTAKE_INGEST_FUNCTION__|${BG_INTAKE_INGEST_FUNCTION}|g" \
      -e "s|__ADP_CHAT_DATA_URL__|${ADP_CHAT_DATA_URL}|g" \
      -e "s|__ADP_CHAT_SANDBOX_CA_CONFIGMAP__|${ADP_CHAT_SANDBOX_CA_CONFIGMAP}|g" \
      -e "s|__ADP_CHAT_DATA_ENABLED__|${ADP_CHAT_DATA_ENABLED}|g" \
      -e "s|__ADP_TASK_API_ADMISSION_ENABLED__|${ADP_TASK_API_ADMISSION_ENABLED}|g" \
      -e "s|__ADP_TASK_API_READ_ENABLED__|${ADP_TASK_API_READ_ENABLED}|g" \
      -e "s|__ADP_TASK_API_WORKER_ENABLED__|${ADP_TASK_API_WORKER_ENABLED}|g" \
      -e "s|__ADP_TASK_API_RECOVERY_ENABLED__|${ADP_TASK_API_RECOVERY_ENABLED}|g" \
      -e "s|__TASK_ARTIFACT_BUCKET_NAME__|${TASK_ARTIFACT_BUCKET_NAME}|g" \
      -e "s|__PRODUCT_SOURCE_SNAPSHOT_BUCKET__|${PRODUCT_SOURCE_SNAPSHOT_BUCKET}|g" \
      -e "s|__MEMORY_TABLE__|${MEMORY_TABLE}|g" \
      -e "s|__ARTIFACTS_TABLE__|${ARTIFACTS_TABLE}|g" \
      -e "s|__ARTIFACTS_BUCKET__|${ARTIFACTS_BUCKET}|g" \
      -e "s|__ADP_TASK_API_QUEUE_URL__|${ADP_TASK_API_QUEUE_URL}|g" \
      -e "s|__ADP_TASK_ADMISSION_PRODUCER_ROLES__|${ADP_TASK_ADMISSION_PRODUCER_ROLES}|g" \
      -e "s|__ADP_TASK_DISPATCH_PRODUCER_ROLES__|${ADP_TASK_DISPATCH_PRODUCER_ROLES}|g" \
      -e "s|__ADP_TASK_RECOVERY_PRODUCER_ROLES__|${ADP_TASK_RECOVERY_PRODUCER_ROLES}|g" \
      -e "s|__ADP_TASK_QUALIFICATION_ID__|${ADP_TASK_QUALIFICATION_ID}|g" \
      -e "s|__ADP_TASK_WORKER_IMAGE_DIGESTS__|${ADP_TASK_WORKER_IMAGE_DIGESTS}|g" \
      -e "s|__ADP_TASK_WORKER_SERVICE_ACCOUNT__|${ADP_TASK_WORKER_SERVICE_ACCOUNT}|g" \
      -e "s|__AGENT_DISPATCH_QUEUE_URL__|${AGENT_DISPATCH_QUEUE_URL}|g" \
      -e "s|__AGENT_WORKER_IMAGE_DIGESTS__|${AGENT_WORKER_IMAGE_DIGESTS}|g" \
      -e "s|__ADP_MODEL_ROOT_BINDINGS__|${ADP_MODEL_ROOT_BINDINGS_SED}|g" \
      -e "s|__ADP_ARC_MODEL_BINDINGS__|${ADP_ARC_MODEL_BINDINGS_SED}|g" \
      -e "s|__ADP_CHAT_WORKER_IMAGE_DIGESTS__|${ADP_CHAT_WORKER_IMAGE_DIGESTS_SED}|g" \
      -e "s|__ADP_CHAT_SANDBOX_IMAGE_DIGESTS__|${ADP_CHAT_SANDBOX_IMAGE_DIGESTS_SED}|g" \
      -e "s|__ADP_CHAT_CREW_IMAGE_DIGESTS__|${ADP_CHAT_CREW_IMAGE_DIGESTS_SED}|g" \
      -e "s|__ADP_CHAT_WORKER_SERVICE_ACCOUNT__|${ADP_CHAT_WORKER_SERVICE_ACCOUNT}|g" \
      -e "s|__AGENT_AUTHORITY_KEY_ID__|${AGENT_AUTHORITY_KEY_ID}|g" \
      -e "s|__AGENT_TASK_SOURCE_ROLE_ARN__|${AGENT_TASK_SOURCE_ROLE_ARN}|g" \
      -e "s|__AGENT_TASK_SOURCE_EKS_CLUSTER__|${AGENT_TASK_SOURCE_EKS_CLUSTER}|g" \
      -e "s|__AGENT_TASK_SOURCE_ISOLATION_CONFIRMED__|${AGENT_TASK_SOURCE_ISOLATION_CONFIRMED}|g" \
      -e "s|__CREW_PROXY_KEEPALIVE_SECONDS__|${CREW_PROXY_KEEPALIVE_SECONDS}|g" \
      -e "s|__CREW_PROXY_MAX_CONN_PER_USER__|${CREW_PROXY_MAX_CONN_PER_USER}|g" \
      -e "s|__FEATURE_CREW_DASHBOARD__|${FEATURE_CREW_DASHBOARD}|g" \
      -e "s|__CREW_DASHBOARD_ORIGIN__|${CREW_DASHBOARD_ORIGIN}|g" \
      -e "s|__ADP_CREW_CHECKPOINTS_ENABLED__|${ADP_CREW_CHECKPOINTS_ENABLED}|g" \
      -e "s|__ADP_GITHUB_USER_ACCESS_ENABLED__|${ADP_GITHUB_USER_ACCESS_ENABLED}|g" \
      -e "s|__ADP_CREW_CHECKPOINT_BUCKET__|${ADP_CREW_CHECKPOINT_BUCKET}|g" \
      -e "s|__ADP_CREW_CHECKPOINT_RETENTION_SECONDS__|${ADP_CREW_CHECKPOINT_RETENTION_SECONDS}|g" \
      -e "s|__ADP_TOOLS_CONSENT_DEFAULT__|${ADP_TOOLS_CONSENT_DEFAULT}|g" \
      -e "s|__ADP_AGENTCORE_MEMORY_ID__|${ADP_AGENTCORE_MEMORY_ID}|g" \
      -e "s|__ADP_AGENTCORE_MEMORY_STRATEGY_ID__|${ADP_AGENTCORE_MEMORY_STRATEGY_ID}|g" \
      -e "s|__ADP_AGENTCORE_MEMORY_REGION__|${ADP_AGENTCORE_MEMORY_REGION}|g" \
      k8s/configmap.yaml |
      python3 "${ROOT_DIR}/modules/gateway/scripts/tool-adapter-config.py" "$ENVIRONMENT" "$AWS_REGION" "$ACCOUNT_ID" | kubectl apply -f -)
  echo "$CONFIGMAP_APPLY_RESULT"
  # Render serviceaccount with the correct IRSA role ARN (Issue #1008)
  sed -e "s|__GATEWAY_IRSA_ROLE_ARN__|${GATEWAY_ROLE_ARN}|g" \
      k8s/serviceaccount.yaml | kubectl apply -f -
  for f in k8s/*.yaml; do
    case "$(basename "$f")" in
      configmap.yaml|serviceaccount.yaml|deployment.yaml|atlassian-mcp-recovery.yaml|targetgroupbinding.yaml|pdb.yaml) continue ;;
      hpa.yaml|pdb.yaml) continue ;;
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
  FEATURE_KNOWLEDGE_ENABLED=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/feature-knowledge" "true")
  FEATURE_INDEXING_ENABLED=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/feature-indexing" "true")
  FEATURE_TENANT_ORG_LINKS_ENABLED=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/feature-tenant-org-links" "false")
  FEATURE_ORCHESTRATION_ENGINE_ENABLED=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/feature-orchestration-engine" "true")
  FEATURE_AGENT_EXPLANATIONS_ENABLED=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/feature-agent-explanations" "false")
  FEATURE_AGENT_CONTROL_ENABLED=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/feature-agent-control" "false")
  FEATURE_NEW_UI_ENABLED=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/feature-new-ui" "false")
  BG_INSIGHTS_RESULTS_BUCKET=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/insights-results-bucket" "")
  if [[ -n "$BG_INSIGHTS_RESULTS_BUCKET" && ! "$BG_INSIGHTS_RESULTS_BUCKET" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]]; then
    fail "Invalid Insights results bucket setting"
  fi
  L5_WORK_QUEUE_URL=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/l5-work-queue-url" "")
  [[ "$L5_WORK_QUEUE_URL" =~ ^https://sqs\.[a-z0-9-]+\.amazonaws\.com/[0-9]{12}/adp-${ENVIRONMENT}-l5-work$ ]] || L5_WORK_QUEUE_URL=""
  L5_CONSUMER_TARGETS=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/l5-consumer-targets" "{}")
  L5_CONSUMER_TARGETS=$(printf '%s' "$L5_CONSUMER_TARGETS" | python3 "$ROOT_DIR/modules/gateway/src/l5_consumer_config.py")
  L5_FINOPS_ADMISSION_ENABLED=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/l5-finops-admission-enabled" "false")
  [[ "$L5_FINOPS_ADMISSION_ENABLED" == "true" ]] || L5_FINOPS_ADMISSION_ENABLED="false"
  BG_PRODUCT_PROFILE_ENABLED=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/product-profile-enabled" "false")
  BG_PRODUCT_ONBOARDING_ENABLED=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/product-onboarding-enabled" "false")
  [[ "$BG_PRODUCT_PROFILE_ENABLED" == "true" ]] || BG_PRODUCT_PROFILE_ENABLED="false"
  [[ "$BG_PRODUCT_ONBOARDING_ENABLED" == "true" ]] || BG_PRODUCT_ONBOARDING_ENABLED="false"
  FEATURE_AGENT_MODELS_ENABLED=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/feature-agent-models" "$PERSONA_MODEL_MAPPING_ENABLED")
  # Chief-of-Staff surfaces (#7197): same parameters as gateway-deploy.yml; absent => off.
  FEATURE_CHAT_ENABLED=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/feature-chat" "false")
  FEATURE_ASSISTANT_UI_ENABLED=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/feature-assistant-ui" "false")
  FEATURE_CREW_DASHBOARD=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/feature-crew-dashboard" "false")
  CREW_PROXY_ENABLED=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/crew-proxy-enabled" "false")
  CREW_DASHBOARD_ORIGIN=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/crew-dashboard-origin" "")
  for _name in FEATURE_CHAT_ENABLED FEATURE_ASSISTANT_UI_ENABLED FEATURE_CREW_DASHBOARD CREW_PROXY_ENABLED; do
    if [ -z "${!_name}" ] || [ "${!_name}" = "None" ]; then printf -v "$_name" '%s' "false"; fi
  done
  if [ "$CREW_DASHBOARD_ORIGIN" = "None" ]; then CREW_DASHBOARD_ORIGIN=""; fi
  # Browser-hop WebSocket ping (#7902): the same parameter as the ConfigMap's
  # CREW_PROXY_KEEPALIVE_SECONDS. 0 deletes the UVICORN_WS_PING_INTERVAL name/value
  # pair from the manifest (uvicorn treats 0 as a tight loop, not "off").
  UVICORN_WS_PING_INTERVAL=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/crew-proxy-keepalive-seconds" "25")
  [[ "$UVICORN_WS_PING_INTERVAL" =~ ^[0-9]+$ ]] || UVICORN_WS_PING_INTERVAL="25"
  if [[ "$UVICORN_WS_PING_INTERVAL" =~ ^0+$ ]]; then
    UVICORN_WS_PING_INTERVAL_SED='/^ *- name: UVICORN_WS_PING_INTERVAL$/{N;d;}'
  else
    UVICORN_WS_PING_INTERVAL_SED="s|__UVICORN_WS_PING_INTERVAL__|${UVICORN_WS_PING_INTERVAL}|g"
  fi
  GATEWAY_DEPLOYMENT_REPLICAS=$(gateway_deployment_replicas "$UPDATE_MODE") \
    || fail "Cannot read gateway replica count; refusing to reset upgrade capacity"
  RENDERED_DEPLOYMENT=$(mktemp "${TMPDIR:-/tmp}/adp-gateway-deployment.XXXXXX.yaml")
  sed -e "s|^  replicas: .*|  replicas: ${GATEWAY_DEPLOYMENT_REPLICAS}|" \
      -e "$UVICORN_WS_PING_INTERVAL_SED" \
      -e "s|__FEATURE_ORCHESTRATION_ENGINE_ENABLED__|${FEATURE_ORCHESTRATION_ENGINE_ENABLED}|g" \
      -e "s|__FEATURE_KNOWLEDGE_ENABLED__|${FEATURE_KNOWLEDGE_ENABLED}|g" \
      -e "s|__FEATURE_INDEXING_ENABLED__|${FEATURE_INDEXING_ENABLED}|g" \
      -e "s|__FEATURE_TENANT_ORG_LINKS_ENABLED__|${FEATURE_TENANT_ORG_LINKS_ENABLED}|g" \
      -e "s|__FEATURE_AGENT_EXPLANATIONS_ENABLED__|${FEATURE_AGENT_EXPLANATIONS_ENABLED}|g" \
      -e "s|__FEATURE_AGENT_CONTROL_ENABLED__|${FEATURE_AGENT_CONTROL_ENABLED}|g" \
      -e "s|__FEATURE_NEW_UI_ENABLED__|${FEATURE_NEW_UI_ENABLED}|g" \
      -e "s|__L5_DEPLOYMENT_ENV__|${ENVIRONMENT}|g" \
              -e "s|__L5_WORK_QUEUE_URL__|${L5_WORK_QUEUE_URL}|g" \
      -e "s|__L5_FINOPS_ADMISSION_ENABLED__|${L5_FINOPS_ADMISSION_ENABLED}|g" \
      -e "s|__L5_CONSUMER_TARGETS__|${L5_CONSUMER_TARGETS}|g" \
      -e "s|__BG_PRODUCT_PROFILE_ENABLED__|${BG_PRODUCT_PROFILE_ENABLED}|g" \
      -e "s|__BG_INSIGHTS_RESULTS_BUCKET__|${BG_INSIGHTS_RESULTS_BUCKET}|g" \
      -e "s|__BG_PRODUCT_ONBOARDING_ENABLED__|${BG_PRODUCT_ONBOARDING_ENABLED}|g" \
      -e "s|__FEATURE_AGENT_MODELS_ENABLED__|${FEATURE_AGENT_MODELS_ENABLED}|g" \
      -e "s|__FEATURE_CHAT_ENABLED__|${FEATURE_CHAT_ENABLED}|g" \
      -e "s|__FEATURE_ASSISTANT_UI_ENABLED__|${FEATURE_ASSISTANT_UI_ENABLED}|g" \
      -e "s|__FEATURE_CREW_DASHBOARD__|${FEATURE_CREW_DASHBOARD}|g" \
      -e "s|__CREW_PROXY_ENABLED__|${CREW_PROXY_ENABLED}|g" \
      -e "s|__CREW_DASHBOARD_ORIGIN__|${CREW_DASHBOARD_ORIGIN}|g" \
      -e "s|REPLACE_WITH_GATEWAY_IMAGE|${GATEWAY_IMAGE}|g" \
      k8s/deployment.yaml > "$RENDERED_DEPLOYMENT"
  CAPACITY_BUNDLE=$(mktemp -d "${TMPDIR:-/tmp}/adp-gateway-capacity.XXXXXX")
  python3 "${SCRIPT_DIR:-../../platform/scripts}/render-gateway-capacity.py" \
    --manifest "$RENDERED_DEPLOYMENT" --root k8s --environment "$ENVIRONMENT" \
    --output-dir "$CAPACITY_BUNDLE" || fail "Invalid gateway capacity selection; rollout unchanged"
  if [ -n "${DEPLOY_CHECKPOINT_FILE:-}" ]; then
    install -d -m 700 "$DEPLOY_CHECKPOINT_FILE.capacity"
    cp "$CAPACITY_BUNDLE/"*.json "$DEPLOY_CHECKPOINT_FILE.capacity/"
  fi
  cp "$CAPACITY_BUNDLE/deployment.json" "$RENDERED_DEPLOYMENT"
  capacity_lock verify || fail "Gateway capacity ownership changed before migration"
  # Schema migration runs against the release image before the pod template
  # changes (parity with gateway-deploy.yml); the Job is pinned to the same digest.
  migrate_gateway_before_rollout "$RENDERED_DEPLOYMENT" "$GATEWAY_IMAGE"
  capacity_lock verify || fail "Gateway capacity ownership changed during migration"
  python3 "$SCRIPT_DIR/render-gateway-capacity.py" --environment "$ENVIRONMENT" \
    --verify-bundle "$CAPACITY_BUNDLE" || fail "Capacity selection changed during migration"
  DEPLOYMENT_APPLY_RESULT=$(python3 scripts/apply-gateway-deployment.py \
    --manifest "$RENDERED_DEPLOYMENT" --image "$GATEWAY_IMAGE" --namespace adp-gateway) \
    || fail "Gateway replica-safe apply failed"
  sed -e "s|REPLACE_WITH_GATEWAY_IMAGE|${GATEWAY_IMAGE}|g" \
    k8s/atlassian-mcp-recovery.yaml | kubectl apply -n adp-gateway -f - \
    || fail "Atlassian MCP recovery worker apply failed"
  rm -f "$RENDERED_DEPLOYMENT"
  echo "$DEPLOYMENT_APPLY_RESULT"
  capacity_lock verify || fail "Gateway capacity ownership changed during apply"
  python3 "$SCRIPT_DIR/render-gateway-capacity.py" --environment "$ENVIRONMENT" \
    --verify-bundle "$CAPACITY_BUNDLE" || fail "Capacity selection changed during apply"
  kubectl apply -f "$CAPACITY_BUNDLE/pdb.json" -n adp-gateway
  if [ "$UPDATE_MODE" = true ]; then
    capacity_lock verify || fail "Gateway capacity ownership changed before HPA apply"
    python3 "$SCRIPT_DIR/render-gateway-capacity.py" --environment "$ENVIRONMENT" \
      --verify-bundle "$CAPACITY_BUNDLE" || fail "Capacity selection changed before HPA apply"
    kubectl apply -f "$CAPACITY_BUNDLE/hpa.json" -n adp-gateway
  fi

  if [ "$UPDATE_MODE" = true ]; then
    # Applying a changed pod template already starts a rollout. Restart only
    # when a Secret/ConfigMap changed without a pod-template update; restarting
    # an unchanged deployment on every retry can race EKS node provisioning.
    CURRENT_IMAGE=$(kubectl get deployment/bedrockgateway -n adp-gateway \
      -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || echo "")
    if [ "$CURRENT_IMAGE" != "${GATEWAY_IMAGE}" ]; then
      fail "Gateway image changed during protected apply"
    elif { [[ "$SECRET_APPLY_RESULT" == *configured* ]] || [[ "$CONFIGMAP_APPLY_RESULT" == *configured* ]]; } \
         && [[ "$DEPLOYMENT_APPLY_RESULT" == *unchanged* ]]; then
      echo "Secret or ConfigMap changed. Restarting gateway to load it..."
      kubectl rollout restart deployment/bedrockgateway -n adp-gateway
    else
      echo "Gateway pod template already matches the release; checking rollout..."
    fi
    wait_for_gateway_rollout \
      || fail "Gateway rollout failed. Check: kubectl describe deployment/bedrockgateway -n adp-gateway"

    # Post-rollout health check (§2)
    sleep 5
    gateway_healthcheck >/dev/null 2>&1 \
      || warn "Health check inconclusive. Verify: source \"${SCRIPT_DIR}/gateway-rollout.sh\"; gateway_healthcheck"

  else
    # Fresh deployments require the same release-image readiness as updates.
    wait_for_gateway_rollout || fail "Gateway rollout not complete"
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
  [ "$ALLOW_KNOWN_CLAUDE_GAP" = false ] || PRICING_FINALIZE_ARGS+=(--allow-known-claude-gap)
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

deploy_phase_complete
fi

if [ "$DEPLOY_GATEWAY" = true ] && ! declare -F capacity_lock >/dev/null; then
  prepare_gateway_capacity
  CAPACITY_BUNDLE=$(mktemp -d "${TMPDIR:-/tmp}/adp-gateway-capacity.XXXXXX")
  for capacity_resource in deployment hpa pdb; do
    cp "$DEPLOY_CHECKPOINT_FILE.capacity/$capacity_resource.json" "$CAPACITY_BUNDLE/" \
      || fail "Saved gateway capacity bundle is missing; replay from the gateway phase"
  done
  python3 "$SCRIPT_DIR/render-gateway-capacity.py" --environment "$ENVIRONMENT" \
    --verify-deployment "$CAPACITY_BUNDLE" || fail "Saved gateway capacity no longer matches selection or Deployment; replay from the gateway phase"
fi

refresh_credentials
# =============================================================================
# Step 5/11: Discover internal ALB and wire to API Gateway + CloudFront
# =============================================================================
if [ "$DEPLOY_GATEWAY" = true ]; then
  GATEWAY_IMAGE=$(python3 "$ROOT_DIR/platform/scripts/resolve-ecr-image.py" "$GATEWAY_IMAGE") \
    || fail "Cannot resolve the gateway image for subsequent phases"
fi
if deploy_phase_begin gateway-alb; then
if [ "$DEPLOY_GATEWAY" = true ]; then
  step "Step 5/11: Wire internal ALB to API Gateway and CloudFront"

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
        -var "orchestration_tick_image_digest=${GATEWAY_IMAGE##*@}" \
        -var "orchestration_tick_image_tag=$IMAGE_TAG" \
        -var "internal_alb_arn=$ALB_ARN" \
        -var "internal_alb_dns=$ALB_DNS" \
        -var "alb_security_group_ids=$ALB_SG_IDS" \
        "${INTERNAL_PLANE_ARGS[@]+"${INTERNAL_PLANE_ARGS[@]}"}" \
        -var "enable_vpc_origin=true"
    else
      terraform apply \
        -var-file="../../../environments/$ENVIRONMENT/modules/gateway.tfvars" \
        -var "orchestration_tick_image_digest=${GATEWAY_IMAGE##*@}" \
        -var "orchestration_tick_image_tag=$IMAGE_TAG" \
        -var "internal_alb_arn=$ALB_ARN" \
        -var "internal_alb_dns=$ALB_DNS" \
        -var "alb_security_group_ids=$ALB_SG_IDS" \
        "${INTERNAL_PLANE_ARGS[@]+"${INTERNAL_PLANE_ARGS[@]}"}" \
        -var "enable_vpc_origin=true" \
        -auto-approve
      ok "API Gateway VPC Link and CloudFront VPC Origin wired to ALB"
    fi

    # This second gateway Terraform pass must keep the tick on the image built
    # above. Its default image tag can still resolve to an older release.
    python3 "$ROOT_DIR/modules/gateway/scripts/sync-gateway-engine.py" \
      --verify-only --image "$GATEWAY_IMAGE" --account "$ACCOUNT_ID" \
      --region "$AWS_REGION" --environment "$ENVIRONMENT" \
      || fail "ALB wiring changed the orchestration engine release image"

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
  step "Step 5/11: Skipping ALB and API Gateway wiring (scope exclusion)"
fi

deploy_phase_complete
fi

refresh_credentials
# =============================================================================
# Step 5: Frontend
# =============================================================================

refresh_credentials
# =============================================================================
# Step 7/11: Broker Lambda code
# =============================================================================
# deploy-broker.sh packages the real github-auth-broker Lambda code and updates
# the live Lambda (terraform ships a 503 placeholder). Required for GitHub login.
# Gateway-scope: runs only when the resolved scope includes gateway.
if deploy_phase_begin broker; then
if [ "$DEPLOY_GATEWAY" = true ] && [ "$SKIP_BROKER" = false ] && [ "${UPGRADE_BROKER_ENABLED:-true}" = true ]; then
  step "Step 7/11: Deploy broker Lambda code"
  bash "$ROOT_DIR/modules/gateway/scripts/deploy-broker.sh" --env "$ENVIRONMENT" --region "$AWS_REGION"
  ok "Broker Lambda deployed"
elif [ "$SKIP_BROKER" = true ]; then
  step "Step 7/11: Skipping broker Lambda (--skip-broker)"
else
  step "Step 7/11: Skipping broker Lambda (scope exclusion)"
fi

deploy_phase_complete
fi

refresh_credentials
# =============================================================================
# Step 8/11: Bootstrap first admin
# =============================================================================
# bootstrap-admin.sh seeds the first platform_admin's DB rows via kubectl exec.
# Without it, the onboarding gate shows "request access" for everyone. Requires
# the gateway pod to be healthy — we enforce a strict rollout gate here.
# Gateway-scope: runs only when the resolved scope includes gateway.
if deploy_phase_begin admin; then
if [ "$UPDATE_MODE" = true ]; then
  step "Step 8/11: Admin bootstrap (skipped — update mode)"
  ok "Admin already exists on live platform"
elif [ "$DEPLOY_GATEWAY" = true ] && [ "$SKIP_ADMIN_BOOTSTRAP" = false ]; then
  step "Step 8/11: Bootstrap first admin"
  # Strict rollout gate: bootstrap-admin.sh does kubectl exec into the gateway
  # pod, so the deployment must be fully healthy. Use the shared bounded rollout wait.
  echo "Waiting for gateway rollout to complete (required for admin bootstrap)..."
  if ! wait_for_gateway_rollout; then
    fail "Gateway deployment did not become healthy within the rollout budget. Cannot bootstrap admin (kubectl exec requires a running pod). Fix the gateway first, then re-run."
  fi
  bash "$ROOT_DIR/modules/gateway/scripts/bootstrap-admin.sh" --env "$ENVIRONMENT" --region "$AWS_REGION"
  ok "First admin bootstrapped"
elif [ "$SKIP_ADMIN_BOOTSTRAP" = true ]; then
  step "Step 8/11: Skipping admin bootstrap (--skip-admin-bootstrap)"
else
  step "Step 8/11: Skipping admin bootstrap (scope exclusion)"
fi

deploy_phase_complete
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
if deploy_phase_begin webhook; then
if [ "$DEPLOY_WEBHOOK" = true ]; then
  step "Step 9/11: Deploy webhook-ingress stack"
  [ "$WORKER_MIGRATION" = false ] || worker_migration check
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
  step "Step 9/11: Skipping webhook-ingress (--skip-webhook-ingress)"
else
  step "Step 9/11: Skipping webhook-ingress (scope exclusion)"
fi

deploy_phase_complete
fi

refresh_credentials
# =============================================================================
# Step 10/11: Agent Factory
# =============================================================================
# Runs after webhook-ingress which installs KEDA (CRD + operator role).
# GitHub App secrets (ARC runner) are optional — enable_github_apps=false on
# fresh deploys where Apps haven't been registered yet.
if [ "$WORKER_MIGRATION" = true ]; then
  worker_migration tick
  (
    cd "$ROOT_DIR/modules/gateway/infra"
    gateway_alb_vars
    terraform_update_apply gateway-worker-authority "$GATEWAY_UPDATE_VAR_FILE" "${GATEWAY_ALB_ARGS[@]}" \
      -var "orchestration_tick_image_digest=${GATEWAY_IMAGE##*@}" -var "orchestration_tick_image_tag=$IMAGE_TAG"
  )
  worker_migration verify
fi
if deploy_phase_begin factory; then
if [ "$DEPLOY_FACTORY" = true ]; then
  step "Step 10/11: Deploy agent-factory"
  # Chat rollout mode is decided at startup; the stage itself defaults to retire.
  CHAT_MODE="${CHAT_MODE:-retire}"
  CHAT_EXCEPTION="${CHAT_EXCEPTION:-}"
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
runner_role_name         = "adp-${ENVIRONMENT}-agent-factory-runner-role"
gateway_intake_managed_policy = true
enable_github_apps       = ${_GH_APPS_EXIST}
enable_agent_context_rbac = ${_AC_NS_EXISTS}
seed_agent_registry      = ${_SEED_REGISTRY}
gateway_deployed         = true
chat_model_policy_enabled = ${CHAT_MODEL_POLICY_ENABLED:-false}
EOF
  else
    FACTORY_VAR_FILE="$UPGRADE_RUN_DIR/agent-factory.tfvars.json"
  fi
  terraform init -backend-config="$BACKEND_FILE" -input=false
  # The runtime verifier Role in adp-gateway was granted live on dev before it was
  # declared (#7870/#7892); adopt it so the apply converges instead of conflicting.
  FACTORY_ADOPT_VAR_FILE=terraform.tfvars
  [ "$UPDATE_MODE" = true ] && FACTORY_ADOPT_VAR_FILE="$FACTORY_VAR_FILE"
  for kind in role role_binding; do
    tf_adopt_existing "kubernetes_${kind}.runtime_verifier" "adp-gateway/adp-runtime-verifier" \
      kubectl get "${kind/_/}" adp-runtime-verifier -n adp-gateway \
      -- -var-file="$FACTORY_ADOPT_VAR_FILE"
  done
  # Publish the chat ingress before retiring the old identity. The helper
  # preserves only observed grants. In `delegated` mode the ingest Lambda is
  # applied with the model policy enabled (the delegated ScaledJob requires it)
  # and the helper runs with --delegated so the supervisor ScaledJob that reuses
  # the legacy name is recognised instead of deleted; otherwise it never starts
  # delegated chat.
  export ADP_CHAT_RETIREMENT_DIR="${UPGRADE_RUN_DIR:-$(mktemp -d)}"
  CHAT_RETIREMENT_ARGS=(--directory "$ADP_CHAT_RETIREMENT_DIR" --account "$ACCOUNT_ID"
    --region "$AWS_REGION" --environment "$ENVIRONMENT")
  CHAT_POLICY_ARGS=()
  if [ "$CHAT_MODE" = delegated ]; then
    CHAT_RETIREMENT_ARGS+=(--delegated)
    CHAT_POLICY_ARGS=(-var=chat_model_policy_enabled=true)
  fi
  python3 "$SCRIPT_DIR/upgrade-chat.py" prepare "${CHAT_RETIREMENT_ARGS[@]}"
  CHAT_RETAIN_VARS="$ADP_CHAT_RETIREMENT_DIR/chat-retirement.tfvars.json"
  if [ "$UPDATE_MODE" = true ]; then
    # Terraform may load managed mode from terraform.tfvars as well as the
    # recovered JSON. Ask Terraform for the effective value before the first
    # pass. A targeted plan cannot exclude the old inline grant because its
    # state address is moved to [0], so use two complete saved-plan gates:
    # first attach the managed policy while retaining the inline grant, then
    # retire the inline grant after the attachment is confirmed in state.
    INTAKE_MANAGED=$(printf 'var.gateway_intake_managed_policy\n' |
      terraform console -var-file="$FACTORY_VAR_FILE" -no-color | tail -n 1) \
      || fail "Cannot resolve the effective agent-factory intake policy mode"
    case "$INTAKE_MANAGED" in
      true)
        INTAKE_ROLE="adp-${ENVIRONMENT}-role-gateway-service"
        INTAKE_POLICY="adp-${ENVIRONMENT}-policy-gateway-intake"
        INTAKE_NAMES="$UPGRADE_RUN_DIR/gateway-intake-inline-names.json"
        aws iam list-role-policies --role-name "$INTAKE_ROLE" --region "$AWS_REGION" \
          --output json > "$INTAKE_NAMES" || fail "Cannot inspect gateway intake inline policy"
        if python3 - "$INTAKE_NAMES" "$INTAKE_POLICY" <<'PY'
import json, sys
raise SystemExit(0 if sys.argv[2] in json.load(open(sys.argv[1]))["PolicyNames"] else 1)
PY
        then
          INTAKE_LIVE="$UPGRADE_RUN_DIR/gateway-intake-inline-live.json"
          INTAKE_CUTOVER="$UPGRADE_RUN_DIR/gateway-intake-cutover.tfvars.json"
          aws iam get-role-policy --role-name "$INTAKE_ROLE" --policy-name "$INTAKE_POLICY" \
            --region "$AWS_REGION" --output json > "$INTAKE_LIVE" \
            || fail "Cannot preserve the live gateway intake inline policy"
          python3 - "$INTAKE_LIVE" "$INTAKE_CUTOVER" <<'PY'
import json, sys
from pathlib import Path
document = json.load(open(sys.argv[1]))["PolicyDocument"]
if not isinstance(document, dict) or document.get("Version") != "2012-10-17":
    raise SystemExit("Invalid live gateway intake inline policy")
Path(sys.argv[2]).write_text(json.dumps({
    "gateway_intake_cutover_keep_inline": True,
    "gateway_intake_cutover_inline_policy_json": json.dumps(document),
}))
PY
          terraform_update_apply "agent-factory" "$FACTORY_VAR_FILE" \
            -var-file="$INTAKE_CUTOVER" -var-file="$CHAT_RETAIN_VARS" "${CHAT_POLICY_ARGS[@]}"
        fi
        ;;
      false) ;;
      *) fail "Invalid effective agent-factory intake policy mode: $INTAKE_MANAGED" ;;
    esac
    terraform_update_apply "agent-factory" "$FACTORY_VAR_FILE" -var-file="$CHAT_RETAIN_VARS" "${CHAT_POLICY_ARGS[@]}"
  else
    terraform apply -var-file=terraform.tfvars -var-file="$CHAT_RETAIN_VARS" "${CHAT_POLICY_ARGS[@]}" -auto-approve
  fi

  if [ "$CHAT_MODE" = delegated ]; then
    # ------------------------------------------------------------------------
    # Step 10c (delegated): activate delegated chat BEFORE retiring the legacy
    # worker, so the environment is never left without chat (#7899 AC-01/AC-03).
    # Both chat images are built, the sandbox gateway TLS endpoint is provisioned
    # (apply-chat-tls.sh), the Crew pod controls and the supervisor/sandbox
    # ScaledJob are applied (deploy-chat-scaledjob.sh), and the ScaledJob must be
    # Ready with the supervisor service account before `drain --delegated` runs.
    # ------------------------------------------------------------------------
    step "Step 10c/11: Delegated chat rollout (qualification/exception record: $CHAT_EXCEPTION)"
    _chat_ssm() { aws ssm get-parameter --name "$1" --query Parameter.Value --output text --region "$AWS_REGION" 2>/dev/null || echo ""; }
    # Chat model-policy posture (#7997; gateway infra chief-of-staff-flags.tf, Step 3,
    # rendered into BG_CHAT_MODEL_POLICY_POSTURE in Step 4). Under "enforced" the
    # gateway refuses every /v1/chat/model/decision without fresh invocability
    # evidence (503 probing_disabled), and only the probe writes that evidence, so the
    # checks below stop the rollout before building anything (#7967). Under the
    # default "advisory" (and "off") chat answers without evidence, so the same
    # checks only warn. Unrecognised values are treated as the default, advisory.
    CHAT_MODEL_POLICY_POSTURE=$(_chat_ssm "/adp/${ENVIRONMENT}/gateway/chat-model-policy-posture")
    case "$CHAT_MODEL_POLICY_POSTURE" in off|advisory|enforced) ;; *) CHAT_MODEL_POLICY_POSTURE="advisory" ;; esac
    ok "Chat model policy posture: ${CHAT_MODEL_POLICY_POSTURE} (SSM /adp/${ENVIRONMENT}/gateway/chat-model-policy-posture)"
    # The parameters are gateway infra (model-probe.tf, Step 3) rendered into the
    # ConfigMap in Step 4; the CronJob is webhook-ingress (Step 9).
    MODEL_PROBE_GUARD_ENABLED=$(_chat_ssm "/adp/${ENVIRONMENT}/gateway/model-probe-enabled")
    MODEL_PROBE_GUARD_SLOTS=$(_chat_ssm "/adp/${ENVIRONMENT}/gateway/model-probe-max-slots-per-cycle")
    MODEL_PROBE_GUARD_CYCLE_BUDGET=$(_chat_ssm "/adp/${ENVIRONMENT}/gateway/model-probe-budget-usd-per-cycle")
    MODEL_PROBE_GUARD_ATTEMPT_BUDGET=$(_chat_ssm "/adp/${ENVIRONMENT}/gateway/model-probe-max-budget-usd-per-attempt")
    _probe_bound_is_positive() { [[ "$1" =~ ^[0-9]+(\.[0-9]{1,6})?$ ]] && [[ "$1" =~ [1-9] ]]; }
    MODEL_PROBE_GUARD_STATE="SSM /adp/${ENVIRONMENT}/gateway/model-probe-*: enabled=${MODEL_PROBE_GUARD_ENABLED:-unset} slots=${MODEL_PROBE_GUARD_SLOTS:-unset} budget_per_cycle=${MODEL_PROBE_GUARD_CYCLE_BUDGET:-unset} budget_per_attempt=${MODEL_PROBE_GUARD_ATTEMPT_BUDGET:-unset}"
    if [ "$MODEL_PROBE_GUARD_ENABLED" != "true" ] \
        || ! _probe_bound_is_positive "$MODEL_PROBE_GUARD_SLOTS" \
        || ! _probe_bound_is_positive "$MODEL_PROBE_GUARD_CYCLE_BUDGET" \
        || ! _probe_bound_is_positive "$MODEL_PROBE_GUARD_ATTEMPT_BUDGET"; then
      if [ "$CHAT_MODEL_POLICY_POSTURE" = enforced ]; then
        fail "Delegated chat enforces model policy (chat_model_policy_posture=enforced) and needs invocability evidence; set model_probe_enabled/model_probe_max_slots_per_cycle/model_probe_budget_usd_per_cycle/model_probe_max_budget_usd_per_attempt in environments/${ENVIRONMENT}/modules/gateway.tfvars and persona_model_probe_enabled=true in webhook-ingress.tfvars (or set chat_model_policy_posture = \"advisory\"), then re-run (${MODEL_PROBE_GUARD_STATE})"
      fi
      warn "Model invocability probing is not armed; chat model policy posture is ${CHAT_MODEL_POLICY_POSTURE}, so delegated chat answers without invocability evidence (decisions are journaled as ${CHAT_MODEL_POLICY_POSTURE}). Arm model_probe_* in environments/${ENVIRONMENT}/modules/gateway.tfvars and persona_model_probe_enabled=true in webhook-ingress.tfvars before setting the posture to enforced (${MODEL_PROBE_GUARD_STATE})"
    else
      ok "Model invocability probing is armed (slots=${MODEL_PROBE_GUARD_SLOTS}/cycle, budget ${MODEL_PROBE_GUARD_CYCLE_BUDGET} USD/cycle, ${MODEL_PROBE_GUARD_ATTEMPT_BUDGET} USD/attempt)"
    fi
    if [ "$LOCAL_MODE" = true ] && docker info &>/dev/null 2>&1; then
      SOURCE_SHA="$SOURCE_SHA" REGISTRY="$REGISTRY" AWS_REGION="$AWS_REGION" \
        bash "$ROOT_DIR/platform/scripts/publish-local-image.sh" adp-chat-agent
      # The restricted sandbox variant is published next to the supervisor image
      # (CodeBuild's bs-chat-agent.yml does both; local Docker needs the second step).
      (cd "$ROOT_DIR" && ADP_SOURCE_SHA="$SOURCE_SHA" REGISTRY="$REGISTRY" AWS_REGION="$AWS_REGION" \
        bash modules/agent-factory/agent/scripts/publish-chat-sandbox-image.sh)
    else
      run_codebuild "adp-${ENVIRONMENT}-chat-agent" "codebuild/bs-chat-agent.yml" adp-chat-agent
    fi
    SUPERVISOR_IMAGE=$(python3 "$ROOT_DIR/platform/scripts/resolve-ecr-image.py" \
      "${ADP_RELEASE_CHAT_AGENT_IMAGE:-$REGISTRY/adp-chat-agent:$IMAGE_TAG}") \
      || fail "Chat supervisor release digest could not be verified"
    SANDBOX_IMAGE=$(python3 "$ROOT_DIR/platform/scripts/resolve-ecr-image.py" \
      "${ADP_RELEASE_CHAT_SANDBOX_IMAGE:-$REGISTRY/adp-chat-agent:sandbox-$IMAGE_TAG}") \
      || fail "Chat sandbox release digest could not be verified"
    [ "$SUPERVISOR_IMAGE" != "$SANDBOX_IMAGE" ] || fail "Supervisor and sandbox images must be distinct digests"
    CHAT_IMAGE="$SUPERVISOR_IMAGE"
    # The supervisor reaches the gateway's internal plane (/internal/v1/agent/chat/*)
    # through the AWS_IAM-authorized API Gateway stage URL that gateway Terraform
    # publishes (Step 4; output api_gateway_invoke_url is the fallback). It signs
    # every request for execute-api with its IRSA identity. Never the CloudFront
    # frontend origin (it only forwards /api/*, so /internal/* is an S3 403) and
    # never the restricted sandbox listener.
    GATEWAY_HTTPS_ORIGIN=$(_chat_ssm "/adp/${ENVIRONMENT}/gateway/apigw-invoke-url")
    [ -n "$GATEWAY_HTTPS_ORIGIN" ] && [ "$GATEWAY_HTTPS_ORIGIN" != "None" ] \
      || GATEWAY_HTTPS_ORIGIN=$(cd "$ROOT_DIR/modules/gateway/infra" && terraform output -raw api_gateway_invoke_url 2>/dev/null || echo "")
    GATEWAY_HTTPS_ORIGIN=$(CHAT_REGION="$AWS_REGION" python3 -c 'import os, re, sys
from urllib.parse import urlsplit
u = urlsplit(sys.argv[1].strip())
host = u.hostname or ""
ok = (u.scheme == "https" and not u.username and not u.password and not u.port and not u.query and not u.fragment
      and re.fullmatch(r"[a-z0-9]+\.execute-api\." + re.escape(os.environ["CHAT_REGION"]) + r"\.amazonaws\.com", host)
      and re.fullmatch(r"/[A-Za-z0-9_-]+/?", u.path))
print(f"https://{host}{u.path.rstrip(chr(47))}" if ok else "")' "$GATEWAY_HTTPS_ORIGIN")
    [ -n "$GATEWAY_HTTPS_ORIGIN" ] || fail "Delegated chat needs the gateway API stage URL (https://<id>.execute-api.${AWS_REGION}.amazonaws.com/<stage>) in SSM /adp/${ENVIRONMENT}/gateway/apigw-invoke-url or gateway infra output api_gateway_invoke_url"
    # Optional Crew values. deploy-chat-scaledjob.sh resolves both from the same SSM
    # parameters itself when the caller leaves them unset (#8058: the ADP-managed
    # chat-agent-deploy.yml sets neither); they are read here so this run's log and
    # journal show the values the ScaledJob will carry.
    ADP_CREW_DASHBOARD_ORIGIN=$(_chat_ssm "/adp/${ENVIRONMENT}/gateway/crew-dashboard-origin")
    [ "$ADP_CREW_DASHBOARD_ORIGIN" != "None" ] || ADP_CREW_DASHBOARD_ORIGIN=""
    ADP_CREW_S3_INTERFACE_IP=$(_chat_ssm "/adp/${ENVIRONMENT}/gateway/crew-s3-interface-ip")
    [ "$ADP_CREW_S3_INTERFACE_IP" != "None" ] || ADP_CREW_S3_INTERFACE_IP=""
    # Deployment default for the persistent-session executor (#8115): an operator export
    # (ADP_CHAT_PERSISTENT_EXECUTOR=claude-code) wins and is published back to SSM by
    # deploy-chat-scaledjob.sh after the apply; otherwise the SSM value is passed through so
    # the log shows what the ScaledJob will carry (empty = the supervisor's sandbox-loop).
    ADP_CHAT_PERSISTENT_EXECUTOR="${ADP_CHAT_PERSISTENT_EXECUTOR:-$(_chat_ssm "/adp/${ENVIRONMENT}/gateway/chat-persistent-executor")}"
    [ "$ADP_CHAT_PERSISTENT_EXECUTOR" != "None" ] || ADP_CHAT_PERSISTENT_EXECUTOR=""
    kubectl create namespace adp-gateway-agents --dry-run=client -o yaml | kubectl apply -f -
    # Sandbox gateway TLS endpoint; idempotent, publishes the CA ConfigMap name to
    # SSM /adp/<env>/gateway/chat-sandbox-ca-configmap and prints it.
    CHAT_SANDBOX_CA_CONFIGMAP=$(bash "$SCRIPT_DIR/apply-chat-tls.sh" --env "$ENVIRONMENT" --region "$AWS_REGION") \
      || fail "Chat sandbox gateway TLS endpoint could not be provisioned"
    CHAT_SANDBOX_CA_CONFIGMAP="${CHAT_SANDBOX_CA_CONFIGMAP##*$'\n'}"
    [[ "$CHAT_SANDBOX_CA_CONFIGMAP" =~ ^chat-sandbox-gateway-ca-[a-f0-9]{16}$ ]] \
      || fail "apply-chat-tls.sh did not report the sandbox CA ConfigMap name"
    ok "Sandbox gateway endpoint ready; CA ConfigMap $CHAT_SANDBOX_CA_CONFIGMAP"
    # Reviewed Crew pod controls first (same set chat-agent-deploy.yml applies), then
    # the supervisor/sandbox ScaledJob. deploy-chat-scaledjob.sh re-checks every
    # precondition (model access, Terraform outputs, Service, CA ConfigMap) and, before
    # the ScaledJob, pins both release digests into the chat-agent-image-prepull
    # DaemonSet and waits for every node (#8059) — no separate DaemonSet apply here.
    # After the ScaledJob apply the same script publishes the release identities the
    # gateway verifies chat workloads against (SSM chat-authority-worker-images /
    # chat-sandbox-image-digests / chat-crew-image-digests + chat-worker-service-account /
    # chat-data-enabled; seeded by gateway Terraform with lifecycle ignore_changes) and
    # converges the live bedrockgateway-config (ADP_CHAT_*_DIGESTS, CA ConfigMap, data
    # URL) with a gateway restart — on this path and on chat-agent-deploy.yml alike, so
    # the ADP-managed image deploy no longer leaves the gateway refusing the new digests
    # (#8086). Step 4 renders the same parameters on every later run.
    ENVIRONMENT="$ENVIRONMENT" AWS_REGION="$AWS_REGION" STATE_BUCKET="$STATE_BUCKET" \
      ADP_CREW_S3_INTERFACE_IP="$ADP_CREW_S3_INTERFACE_IP" ADP_CHAT_CREW_CONTROLS_ONLY=true \
      bash "$ROOT_DIR/modules/agent-factory/agent/k8s/deploy-chat-scaledjob.sh" \
      || fail "Crew pod controls could not be applied"
    ENVIRONMENT="$ENVIRONMENT" AWS_REGION="$AWS_REGION" STATE_BUCKET="$STATE_BUCKET" \
      SUPERVISOR_IMAGE="$SUPERVISOR_IMAGE" SANDBOX_IMAGE="$SANDBOX_IMAGE" \
      GATEWAY_HTTPS_ORIGIN="$GATEWAY_HTTPS_ORIGIN" ADP_CREW_DASHBOARD_ORIGIN="$ADP_CREW_DASHBOARD_ORIGIN" \
      ADP_CHAT_PERSISTENT_EXECUTOR="$ADP_CHAT_PERSISTENT_EXECUTOR" \
      ADP_CHAT_EXISTING_NAMESPACE_ONLY=true ADP_CHAT_MODEL_POLICY_ENABLED=true ADP_CHAT_CREW_CONTROLS_ONLY=false \
      bash "$ROOT_DIR/modules/agent-factory/agent/k8s/deploy-chat-scaledjob.sh" \
      || fail "Delegated chat ScaledJob rollout failed"
    kubectl wait --for=condition=Ready scaledjob/chat-agent-worker -n adp-gateway-agents --timeout=300s \
      || fail "Delegated chat ScaledJob is not ready"
    DELEGATED_SA=$(kubectl get scaledjob chat-agent-worker -n adp-gateway-agents \
      -o jsonpath='{.spec.jobTargetRef.template.spec.serviceAccountName}')
    [ "$DELEGATED_SA" = adp-chat-supervisor ] \
      || fail "ScaledJob chat-agent-worker is not the delegated supervisor (service account: ${DELEGATED_SA:-none})"
    SUPERVISOR_DIGEST="${SUPERVISOR_IMAGE##*@}"
    SANDBOX_DIGEST="${SANDBOX_IMAGE##*@}"
    ok "Published chat release identities (deploy-chat-scaledjob.sh): SSM /adp/${ENVIRONMENT}/gateway/chat-authority-worker-images=${SUPERVISOR_DIGEST}, chat-sandbox-image-digests=${SANDBOX_DIGEST}, chat-crew-image-digests=${SANDBOX_DIGEST}, chat-worker-service-account=adp-chat-supervisor, chat-data-enabled=true"
    ok "Gateway reconfigured for delegated chat (CA ConfigMap ${CHAT_SANDBOX_CA_CONFIGMAP}, supervisor/sandbox/Crew digests) by deploy-chat-scaledjob.sh"
    # Record invocability evidence now instead of waiting for the CronJob's next
    # scheduled tick (#7967): one cycle from cronjob/persona-model-invocability-probe
    # (webhook-ingress, Step 9). Exit 3 means nothing was claimable (every catalogue
    # model already holds fresh evidence, or today's cycle budget is spent). Any other
    # failure means chat would refuse every turn under the enforced posture, so the
    # rollout stops there; under advisory/off (#7997) chat answers without evidence
    # and the failure is a warning.
    ENVIRONMENT="$ENVIRONMENT" AWS_REGION="$AWS_REGION" bash "$SCRIPT_DIR/run-probe-cycle.sh" \
      && PROBE_CYCLE_STATUS=0 || PROBE_CYCLE_STATUS=$?
    case "$PROBE_CYCLE_STATUS" in
      0) ok "Model invocability probe cycle recorded proven evidence" ;;
      3) warn "Model invocability probe cycle had nothing to claim; existing evidence stands (see the job log above)" ;;
      *) if [ "$CHAT_MODEL_POLICY_POSTURE" = enforced ]; then
           fail "Model invocability probe cycle did not record proven evidence (exit $PROBE_CYCLE_STATUS); delegated chat would refuse every turn under chat_model_policy_posture=enforced"
         fi
         warn "Model invocability probe cycle did not record proven evidence (exit $PROBE_CYCLE_STATUS); chat model policy posture is ${CHAT_MODEL_POLICY_POSTURE}, so delegated chat answers without evidence (see the job log above)" ;;
    esac
    ok "Delegated chat ScaledJob is Ready; retiring the legacy credentialed worker"
  fi

  case "$CHAT_MODE" in
    retire|delegated)
      # Legacy retirement. With --delegated the helper leaves the supervisor
      # ScaledJob (same name, service account adp-chat-supervisor) in place and
      # only waits for credentialed adp-agent work to finish.
      python3 "$SCRIPT_DIR/upgrade-chat.py" drain "${CHAT_RETIREMENT_ARGS[@]}"
      # Install the stronger denial while retaining all existing policies. The
      # retirement gate requires it to be observed as a no-op in the removal plan.
      if [ "$UPDATE_MODE" = true ]; then
        terraform_update_apply "agent-factory" "$FACTORY_VAR_FILE" -var-file="$CHAT_RETAIN_VARS" \
          -var=chat_legacy_upgrade_deny_ready=true "${CHAT_POLICY_ARGS[@]}"
      else
        terraform apply -var-file=terraform.tfvars -var-file="$CHAT_RETAIN_VARS" \
          -var=chat_legacy_upgrade_deny_ready=true "${CHAT_POLICY_ARGS[@]}" -auto-approve
      fi
      if [ "$UPDATE_MODE" = true ]; then
        terraform_update_apply "agent-factory" "$FACTORY_VAR_FILE" -var='chat_legacy_upgrade_policies={}' "${CHAT_POLICY_ARGS[@]}"
      else
        terraform apply -var-file=terraform.tfvars -var='chat_legacy_upgrade_policies={}' "${CHAT_POLICY_ARGS[@]}" -auto-approve
      fi
      python3 "$SCRIPT_DIR/upgrade-chat.py" verify "${CHAT_RETIREMENT_ARGS[@]}"
      ;;
    keep-legacy)
      warn "Chat mode keep-legacy: the legacy credentialed chat worker (ScaledJob chat-agent-worker, service account adp-agent) and its IAM grants remain in place; no drain or verification ran"
      ;;
  esac

  if [ "$DEPLOY_GATEWAY" = true ] && [ "$ENVIRONMENT" = "dev" ]; then
    COGNITO_PENTEST_CLIENT_ID=$(terraform output -raw pentest_actor_client_id 2>/dev/null || echo "")
    if [ -z "$COGNITO_PENTEST_CLIENT_ID" ] || [ "$COGNITO_PENTEST_CLIENT_ID" = "None" ]; then
      fail "Agent-factory deployed without publishing the dev pentest Cognito client ID"
    fi
    CURRENT_PENTEST_CLIENT_ID=$(kubectl get configmap bedrockgateway-config -n adp-gateway \
      -o jsonpath='{.data.BG_COGNITO_PENTEST_CLIENT_ID}')
    if [ "$CURRENT_PENTEST_CLIENT_ID" != "$COGNITO_PENTEST_CLIENT_ID" ]; then
      PENTEST_CLIENT_PATCH=$(PENTEST_CLIENT_ID="$COGNITO_PENTEST_CLIENT_ID" python3 -c \
        'import json, os; print(json.dumps({"data": {"BG_COGNITO_PENTEST_CLIENT_ID": os.environ["PENTEST_CLIENT_ID"]}}))')
      kubectl patch configmap bedrockgateway-config -n adp-gateway \
        --type merge --patch "$PENTEST_CLIENT_PATCH"
      kubectl rollout restart deployment/bedrockgateway -n adp-gateway
    else
      echo "Gateway pentest Cognito client is already configured; checking rollout"
    fi
    wait_for_gateway_rollout \
      || fail "Gateway rollout failed after adding the dev pentest Cognito client"
    ok "Gateway reconciled with the dev pentest Cognito client"
  fi

  # --- Agent Gateway build + deploy (part of agent-factory) ---
  step "Step 10b/11: Build and deploy agent gateway"

  # --- Docker build: use CodeBuild (needs privileged mode) or local Docker ---
  if [ "$LOCAL_MODE" = true ] && docker info &>/dev/null 2>&1; then
    SOURCE_SHA="$SOURCE_SHA" REGISTRY="$REGISTRY" AWS_REGION="$AWS_REGION" \
      bash "$ROOT_DIR/platform/scripts/publish-local-image.sh" adp-agent-gateway
  else
    # Docker build via CodeBuild (Terraform-managed project)
    run_codebuild "adp-${ENVIRONMENT}-agent-gateway" "codebuild/bs-agent-gateway.yml" adp-agent-gateway
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

  if [ "$CHAT_MODE" = delegated ]; then
    # Images were built and the ScaledJob applied before retirement (Step 10c above).
    # deploy-chat-scaledjob.sh already rendered the pre-pull DaemonSet with BOTH release
    # digests (containers prepull + prepull-sandbox) and waited for it before the
    # ScaledJob, on this path and on chat-agent-deploy.yml alike (#8059). Re-applying
    # the single-container manifest here would drop the sandbox digest again.
    step "Step 10c/11: Delegated chat images cached by deploy-chat-scaledjob.sh"
  else
    # Cache the release image without launching the retired credentialed worker.
    # Delegated chat activation requires its separate qualification (#6937) and
    # `--chat delegated` with a recorded exception.
    step "Step 10c/11: Cache chat release image (delegated chat not selected)"
    if [ "$LOCAL_MODE" = true ] && docker info &>/dev/null 2>&1; then
      SOURCE_SHA="$SOURCE_SHA" REGISTRY="$REGISTRY" AWS_REGION="$AWS_REGION" \
        bash "$ROOT_DIR/platform/scripts/publish-local-image.sh" adp-chat-agent
    else
      run_codebuild "adp-${ENVIRONMENT}-chat-agent" "codebuild/bs-chat-agent.yml" adp-chat-agent
    fi
    CHAT_IMAGE=$(python3 "$ROOT_DIR/platform/scripts/resolve-ecr-image.py" \
      "${ADP_RELEASE_CHAT_AGENT_IMAGE:-$REGISTRY/adp-chat-agent:$IMAGE_TAG}") \
      || fail "Chat agent release digest could not be verified"
    sed -e "s|REPLACE_WITH_AGENT_IMAGE|${CHAT_IMAGE}|g" \
      "$ROOT_DIR/modules/agent-factory/agent/k8s/image-prepull-daemonset.yaml" | kubectl apply -f -
    kubectl rollout status daemonset/chat-agent-image-prepull -n adp-gateway-agents --timeout=600s
  fi
  case "$CHAT_MODE" in
    delegated) ok "Delegated chat is live (supervisor $SUPERVISOR_IMAGE, sandbox $SANDBOX_IMAGE); legacy worker retired. Exception record: $CHAT_EXCEPTION" ;;
    keep-legacy) warn "Chat image cached; the legacy credentialed chat worker remains running (keep-legacy)" ;;
    retire) ok "Chat image cached; legacy worker retired and delegated chat remains disabled" ;;
  esac
else
  step "Step 10/11: Skipping agent-factory"
fi

deploy_phase_complete
fi

# =============================================================================
# Step 12: Dev Box (optional module — --dev-box on / --dev-box-only, #7998)
# =============================================================================
# Runs after the agent factory and chat (Steps 10 and 10c): the module's entrypoint
# owns everything Dev Box-specific (two Terraform stacks, AMI build, controller
# Deployment, gateway seed, live verification); this block only decides whether to
# call it and in which mode. Off is a no-op so an environment without the module
# gets exactly today's platform.
if deploy_phase_begin tools-adapters; then
  if [ -n "${ADP_TOOLS_ADAPTERS_CONFIG:-}" ] && [ "$_SCOPE_COUNT" -eq 0 ]; then
    install_tools_adapters
  else
    echo "Adapter foundations not selected for this deployment"
  fi
  deploy_phase_complete
fi

if deploy_phase_begin dev-box; then
if [ "$DEV_BOX_MODE" = on ]; then
  step "Step 12 (optional): Dev Box (modules/user-services/dev-box)"
  refresh_credentials
  DEV_BOX_ARGS=(--env "$ENVIRONMENT" --region "$AWS_REGION")
  [ "$UPDATE_MODE" = false ] || DEV_BOX_ARGS+=(--update)
  [ "$CONFIRM_DESTRUCTIVE" = false ] || DEV_BOX_ARGS+=(--confirm-destructive)
  bash "$ROOT_DIR/modules/user-services/dev-box/scripts/deploy-dev-box.sh" "${DEV_BOX_ARGS[@]}" \
    || fail "Dev Box stage failed; fix the named input and re-run with --resume (or --dev-box-only)"
  ok "Dev Box stage complete"
else
  step "Step 12 (optional): Skipping Dev Box (--dev-box on enables it)"
fi

deploy_phase_complete
fi

refresh_credentials
# =============================================================================
# Step 11/11: Agent Context (optional — gated by AGENT_CONTEXT_ENABLED or --agent-context-only)
# =============================================================================

if deploy_phase_begin context; then
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

  # Deploy k8s manifests. deploy.sh renders ${ENVIRONMENT} into the runtime
  # verifier RoleBinding group (adp:runtime-verifier-<env>); without the export
  # a non-dev environment would bind the dev group.
  cd "$ROOT_DIR/modules/agent-context"
  export ENVIRONMENT
  if [ "$UPDATE_MODE" = true ]; then
    bash deploy.sh --skip-terraform
  else
    bash deploy.sh --skip-validate
  fi
  ok "Agent-context deployed"
else
  step "Step 11/11: Skipping agent-context (set AGENT_CONTEXT_ENABLED=true or use --agent-context-only)"
fi

deploy_phase_complete
fi

if deploy_phase_begin finalize; then
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
    # Keep the engine on the selected release through the last Terraform pass.
    # The environment default can otherwise resolve to an older gateway image.
    terraform_update_apply gateway-final "$GATEWAY_UPDATE_VAR_FILE" "${GATEWAY_ALB_ARGS[@]}" \
      -var "orchestration_tick_image_digest=${GATEWAY_IMAGE##*@}" \
      -var "orchestration_tick_image_tag=$IMAGE_TAG"
    UPGRADE_CHECK_ONLY=true terraform_update_apply gateway-final "$GATEWAY_UPDATE_VAR_FILE" "${GATEWAY_ALB_ARGS[@]}" \
      -var "orchestration_tick_image_digest=${GATEWAY_IMAGE##*@}" \
      -var "orchestration_tick_image_tag=$IMAGE_TAG"
    python3 "$ROOT_DIR/modules/gateway/scripts/sync-gateway-engine.py" \
      --verify-only --image "$GATEWAY_IMAGE" --account "$ACCOUNT_ID" \
      --region "$AWS_REGION" --environment "$ENVIRONMENT" \
      || fail "Final gateway reconciliation changed the orchestration engine release image"
  fi
fi

deploy_phase_complete
fi

if deploy_phase_begin frontend; then
if [ "$SKIP_FRONTEND" = false ] && [ "$DEPLOY_GATEWAY" = true ]; then
  step "Step 6/11: Publish frontend and both account-connection templates"
  bash "$ROOT_DIR/modules/gateway/scripts/deploy-frontend.sh" --env "$ENVIRONMENT" --region "$AWS_REGION"
else
  step "Step 6/11: Skipping frontend"
fi

deploy_phase_complete
fi

if [ "$WORKER_MIGRATION" = true ]; then
  worker_migration admit
  bash "$ROOT_DIR/modules/agent-factory/webhook-ingress/scripts/deploy-webhook-ingress.sh" \
    --env "$ENVIRONMENT" --region "$AWS_REGION" --update --skip-image --skip-lambda
  worker_migration complete
  (
    cd "$ROOT_DIR/modules/gateway/infra"
    gateway_alb_vars
    ADP_WORKER_MIGRATION_EVIDENCE= terraform_update_apply gateway-final "$GATEWAY_UPDATE_VAR_FILE" "${GATEWAY_ALB_ARGS[@]}" \
      -var "orchestration_tick_image_digest=${GATEWAY_IMAGE##*@}" -var "orchestration_tick_image_tag=$IMAGE_TAG"
  )
fi
if deploy_phase_begin verify; then
if [ "$UPDATE_MODE" = true ]; then
  REQUIRED_MODULE_ARGS=()
  [ "$DEPLOY_FACTORY" != true ] || REQUIRED_MODULE_ARGS+=(--require-module agent-factory)
  python3 "$SCRIPT_DIR/upgrade-state.py" verify --directory "$UPGRADE_RUN_DIR" --region "$AWS_REGION" \
    ${REQUIRED_MODULE_ARGS[@]+"${REQUIRED_MODULE_ARGS[@]}"}
fi
if [ "$DEPLOY_GATEWAY" = true ]; then
  if [ "$UPDATE_MODE" = false ]; then
    step "Enable gateway autoscaling after initial configuration"
    capacity_lock verify || fail "Gateway capacity ownership changed before HPA apply"
    python3 "$SCRIPT_DIR/render-gateway-capacity.py" --environment "$ENVIRONMENT" \
      --verify-bundle "$CAPACITY_BUNDLE" || fail "Capacity selection changed before HPA apply"
    kubectl apply -f "$CAPACITY_BUNDLE/hpa.json" -n adp-gateway
    wait_for_gateway_rollout || fail "Gateway is not ready after enabling autoscaling"
  fi
  CF_DOMAIN=$(aws ssm get-parameter --name "/adp/$ENVIRONMENT/gateway/cloudfront-domain" --query Parameter.Value --output text)
  curl --fail --silent --show-error --retry 5 --retry-all-errors "https://$CF_DOMAIN/api/health" \
    | python3 -c 'import json,sys; assert json.load(sys.stdin).get("status")=="healthy", "CDN API is unhealthy"'
  capacity_lock verify || fail "Gateway capacity ownership changed after rollout"
  python3 "$SCRIPT_DIR/render-gateway-capacity.py" --environment "$ENVIRONMENT" \
    --verify-applied "$CAPACITY_BUNDLE" || fail "Gateway capacity readback differs from selected bundle"
  capacity_lock release || fail "Gateway capacity ownership release failed"
  rm -rf "$CAPACITY_BUNDLE" "$CAPACITY_LOCK_DIR"
  if declare -F deploy_checkpoint_exit >/dev/null; then
    trap 'deploy_checkpoint_exit $?' EXIT
  else
    trap - EXIT
  fi
fi

if [ "$CI_MODE" = false ] && [ "${ADP_BEDROCK_VERIFY_DEFERRED:-false}" != true ]; then
  step "Verify default Bedrock model invocations"
  bash "$SCRIPT_DIR/enable-bedrock-models.sh" --verify || fail "Default model invocation failed."
fi

deploy_phase_complete
fi

# =============================================================================
# Summary
# =============================================================================
deploy_checkpoint finish
step "Deployment complete"

echo "Platform:  $EKS_CLUSTER"
echo "Gateway:   kubectl get pods -n adp-gateway (configure kubectl: aws eks update-kubeconfig --name $EKS_CLUSTER --region $AWS_REGION)"

CF_DOMAIN=$(aws ssm get-parameter --name "/adp/$ENVIRONMENT/gateway/cloudfront-domain" --query "Parameter.Value" --output text 2>/dev/null) || true
[ -n "$CF_DOMAIN" ] && [ "$CF_DOMAIN" != "None" ] && echo "Frontend:  https://${CF_DOMAIN}" && echo "API:       https://${CF_DOMAIN}/api/health"
[ "$DEPLOY_WEBHOOK" = true ] && echo "Hosted agents: kubectl get scaledjobs -n adp-agents (check admission pause status)"
[ "$DEPLOY_FACTORY" = true ] && echo "Factory workers: kubectl get scaledjobs -n adp-gateway-agents"
[ "$DEPLOY_AGENT_CONTEXT" = true ] && echo "Context:   kubectl get pods -n agent-context"
[ "$DEV_BOX_MODE" = on ] && echo "Dev Box:   kubectl -n adp-gateway logs deploy/dev-box-controller; acceptance: modules/user-services/dev-box/scripts/verify-dev-box.py --env $ENVIRONMENT"
GW_WS=$(cd "$ROOT_DIR/modules/agent-factory/infra" && terraform output -raw gateway_ws_endpoint 2>/dev/null) || true
[ -n "$GW_WS" ] && [ "$GW_WS" != "" ] && echo "AgentGW:   $GW_WS"

if [ -n "$CF_DOMAIN" ] && [ "$CF_DOMAIN" != "None" ]; then
  echo "CLI installation (no sign-in required):"
  echo "  curl -fsSL https://${CF_DOMAIN}/api/cli/install.sh | sh -s -- --gateway-url https://${CF_DOMAIN}/api"
  echo '  "$HOME/.adp/bin/adp" admin setup'
  echo "Sign in with the bootstrap Cognito administrator account; GitHub is not needed for CLI admin login."
fi

# --- Next steps (manual — GitHub App wiring; skipped in update mode) ---
if [ "$UPDATE_MODE" = false ] && [ "$GATEWAY_ONLY" = false ] && [ "$AGENT_CONTEXT_ONLY" = false ] && [ "$DEV_BOX_ONLY" = false ]; then
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
  echo "  4. Complete worker-security activation and admission checks: docs/security/terraform-worker-rollout.md"
  echo "  5. With approval for a live GitHub test, comment '@agent-developer <task>' on an issue"
  echo ""
  echo "Sign in with your selected administrator; see docs/adp-platform-deployment/bootstrap-administrator.md"
fi

echo ""
echo "To destroy (legacy): $0 --destroy"
echo "To destroy (recommended): ./platform/scripts/undeploy.sh"
