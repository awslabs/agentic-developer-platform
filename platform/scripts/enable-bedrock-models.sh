#!/usr/bin/env bash
# =============================================================================
# enable-bedrock-models.sh — accept Bedrock marketplace agreements via CLI
# =============================================================================
# Fresh AWS accounts have no marketplace agreement for Anthropic models, so
# every InvokeModel/Converse call fails with AccessDeniedException citing
# aws-marketplace:Subscribe. The Claude SDK inside agent-worker swallows that
# failure as a graceful "no changes needed" (0 tokens, 1 turn), which masks
# the real error (see aws-innovate/adp#337 smoke-test incident, 2026-07-04).
#
# Discovers the current list of ACTIVE Anthropic models from the Bedrock API
# (list-foundation-models), so newly released models (Opus 4.7/4.8, Sonnet 5,
# ...) get their agreement accepted automatically on the next deploy without
# editing this script. Agreements are pay-per-use with no standing cost.
#
# Only the REQUIRED_MODELS (the ones the platform invokes at runtime) fail
# the deploy when they can't be enabled; newer/other models are best-effort.
# Idempotent — models with an active agreement are skipped. No console
# interaction needed.
#
# Caller permissions required: bedrock:ListFoundationModels,
# bedrock:GetFoundationModelAvailability,
# bedrock:ListFoundationModelAgreementOffers,
# bedrock:CreateFoundationModelAgreement, aws-marketplace:Subscribe,
# aws-marketplace:ViewSubscriptions.
#
# Usage:
#   ./enable-bedrock-models.sh                 # all ACTIVE Anthropic models
#   ./enable-bedrock-models.sh anthropic.claude-x anthropic.claude-y
#   BEDROCK_MODELS="a b c" ./enable-bedrock-models.sh
# =============================================================================
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"

# Models the platform invokes at runtime — the deploy FAILS if these can't be
# enabled. Keep in sync with:
#   modules/agent-factory/agent-worker-image/entrypoint.py (ANTHROPIC_MODEL)
#   modules/agent-factory/agent/k8s/chat-scaledjob.yaml    (ANTHROPIC_MODEL, LCM_SUMMARY_MODEL)
REQUIRED_MODELS=(
  "anthropic.claude-opus-4-6-v1"
  "anthropic.claude-sonnet-4-6"
)

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓ $1${NC}"; }
info() { echo -e "${YELLOW}… $1${NC}"; }
err()  { echo -e "${RED}✗ $1${NC}"; }

# The agreement APIs take the model id as Bedrock lists it (including any
# ":N" version suffix) but NOT inference-profile region prefixes — strip
# global./us./eu./apac. so callers can pass profile ids straight from env.
normalize() {
  local id="$1"
  id="${id#global.}"; id="${id#us.}"; id="${id#eu.}"; id="${id#apac.}"
  echo "$id"
}

# ---------------------------------------------------------------------------
# Build the model list: explicit args > BEDROCK_MODELS env > live discovery.
# ---------------------------------------------------------------------------
if [ "$#" -gt 0 ]; then
  RAW_MODELS="$*"
elif [ -n "${BEDROCK_MODELS:-}" ]; then
  RAW_MODELS="$BEDROCK_MODELS"
else
  RAW_MODELS="$(aws bedrock list-foundation-models \
    --by-provider anthropic --region "$REGION" \
    --query 'modelSummaries[?modelLifecycle.status==`ACTIVE`].modelId' \
    --output text 2>/dev/null | tr '\t' ' ' || true)"
  if [ -z "$RAW_MODELS" ]; then
    err "Cannot list Anthropic models from Bedrock (bedrock:ListFoundationModels) — falling back to required models only"
  fi
  # Required models are always in the list, even if discovery misses them.
  RAW_MODELS="$RAW_MODELS ${REQUIRED_MODELS[*]}"
fi

# Normalize + dedupe (portable: no mapfile — macOS ships bash 3.2)
MODELS=()
for raw in $RAW_MODELS; do
  m="$(normalize "$raw")"
  seen=0
  for existing in ${MODELS[@]+"${MODELS[@]}"}; do
    [ "$existing" = "$m" ] && seen=1 && break
  done
  [ "$seen" -eq 0 ] && MODELS+=("$m")
done

is_required() {
  local m
  for m in "${REQUIRED_MODELS[@]}"; do
    [ "$(normalize "$m")" = "$1" ] && return 0
  done
  return 1
}

agreement_status() {
  aws bedrock get-foundation-model-availability \
    --model-id "$1" --region "$REGION" \
    --query 'agreementAvailability.status' --output text 2>/dev/null || echo "ERROR"
}

# Required models flip the exit code; optional ones only warn.
FAILED=0
report_failure() {
  if is_required "$1"; then
    err "$1: $2 (REQUIRED — agents cannot invoke Claude without it)"
    FAILED=1
  else
    info "$1: $2 (optional — skipped)"
  fi
}

echo "Checking Bedrock marketplace agreements for ${#MODELS[@]} Anthropic model(s) in ${REGION}..."

# Accept all missing agreements first, then poll them together —
# activation is async (typically <2 min) and this avoids serial 5-min waits.
PENDING=()
for model in "${MODELS[@]}"; do
  status="$(agreement_status "$model")"

  case "$status" in
    AVAILABLE)
      ok "$model: agreement already active"
      continue
      ;;
    ERROR)
      report_failure "$model" "cannot read availability (check model id / bedrock permissions)"
      continue
      ;;
  esac

  info "$model: agreement $status — accepting offer..."
  offer_token="$(aws bedrock list-foundation-model-agreement-offers \
    --model-id "$model" --region "$REGION" \
    --query 'offers[0].offerToken' --output text 2>/dev/null || true)"

  if [ -z "$offer_token" ] || [ "$offer_token" = "None" ]; then
    report_failure "$model" "no agreement offer found (not orderable in $REGION)"
    continue
  fi

  if create_err="$(aws bedrock create-foundation-model-agreement \
       --model-id "$model" --offer-token "$offer_token" --region "$REGION" 2>&1 > /dev/null)"; then
    PENDING+=("$model")
  else
    # Common causes: missing aws-marketplace:Subscribe, or an org-level
    # private-marketplace policy that hasn't whitelisted this product yet.
    report_failure "$model" "create-foundation-model-agreement failed: ${create_err##*: }"
  fi
done

if [ "${#PENDING[@]}" -gt 0 ]; then
  info "Waiting for ${#PENDING[@]} agreement(s) to activate..."
  for _ in $(seq 1 30); do
    REMAINING=()
    for model in "${PENDING[@]}"; do
      if [ "$(agreement_status "$model")" = "AVAILABLE" ]; then
        ok "$model: agreement accepted and active"
      else
        REMAINING+=("$model")
      fi
    done
    PENDING=(${REMAINING[@]+"${REMAINING[@]}"})
    [ "${#PENDING[@]}" -eq 0 ] && break
    sleep 10
  done
  for model in ${PENDING[@]+"${PENDING[@]}"}; do
    report_failure "$model" "agreement still not active after 5 minutes — re-run to retry"
  done
fi

exit "$FAILED"
