#!/usr/bin/env bash
# =============================================================================
# apply-internal-plane-deny.sh — gated edge deny for the internal plane (#4010)
# =============================================================================
# Applies `modules/gateway/k8s/patches/edge-internal-deny.yaml` to the edge
# Ingress, but ONLY once the API Gateway `/internal/{proxy+}` integration has
# actually been repointed at the dedicated internal-plane ALB.
#
# WHY THIS SCRIPT EXISTS
# ----------------------
# The deny and the repoint must be atomic in effect: the edge ALB must not start
# 403-ing `/internal/*` while the API-GW integration still targets that same
# ALB. If it does, every SigV4 internal call (agent provenance writes,
# credential paths) gets a 403 with no automatic recovery.
#
# That inversion is easy to reach by accident, because the two live in different
# layers: the deny is Kubernetes (applied by `gateway-deploy.yml` on every merge)
# and the repoint is Terraform (gated on the internal-plane vars being
# populated). Shipping the deny inside `k8s/ingress.yaml` made it fire first —
# the bug this script fixes. So the precondition is checked against the LIVE
# integration URI rather than against SSM or Terraform state: it verifies the
# repoint actually LANDED, not merely that it was requested.
#
# MODES
#   (default)   Apply the deny if and only if the precondition holds.
#               Skips with a notice (exit 0) otherwise — safe and idempotent, so
#               a later deploy applies it once the repoint has landed.
#   --verify    Assert the invariant without changing anything. FAILS if the edge
#               ALB denies `/internal` while the integration still targets it
#               (the outage state). Intended as a deploy smoke assertion.
#   --remove    Roll the deny back off the edge Ingress (restores plain
#               forwarding). Use if the internal plane must be re-pointed at the
#               edge ALB.
#
# Reads: AWS_REGION, ENVIRONMENT, NAMESPACE (default adp-gateway)
# Exit:  0 = applied, or correctly skipped, or verified consistent
#        1 = error, or (in --verify) the outage state was detected
# =============================================================================
set -euo pipefail

MODE="apply"
for arg in "$@"; do
  case "$arg" in
    --verify) MODE="verify" ;;
    --remove) MODE="remove" ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "Unknown argument: $arg" >&2; exit 1 ;;
  esac
done

AWS_REGION="${AWS_REGION:-us-east-1}"
ENVIRONMENT="${ENVIRONMENT:-dev}"
NAMESPACE="${NAMESPACE:-adp-gateway}"
INGRESS_NAME="bedrockgateway"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
PATCH_FILE="${REPO_ROOT}/modules/gateway/k8s/patches/edge-internal-deny.yaml"

# ---------------------------------------------------------------------------
# Step 1: Is the deny currently live on the edge Ingress?
# ---------------------------------------------------------------------------
# The `actions.deny-internal` annotation is the marker — it only exists when the
# patch has been applied.
deny_is_live() {
  local ann
  ann=$(kubectl get ingress "$INGRESS_NAME" -n "$NAMESPACE" \
    -o jsonpath='{.metadata.annotations.alb\.ingress\.kubernetes\.io/actions\.deny-internal}' \
    2>/dev/null || echo "")
  [ -n "$ann" ]
}

# ---------------------------------------------------------------------------
# Step 2: Where does the LIVE `/internal/{proxy+}` integration point?
# ---------------------------------------------------------------------------
# Deliberately reads the deployed API Gateway rather than SSM or Terraform
# state: only the live integration proves the repoint landed. The REST API id is
# derived from the invoke URL that gateway Terraform publishes to SSM
# (https://<api-id>.execute-api.<region>.amazonaws.com/<stage>).
#
# Echoes the integration URI, or nothing if it cannot be resolved.
get_internal_integration_uri() {
  local invoke_url api_id resource_id
  invoke_url=$(aws ssm get-parameter \
    --name "/adp/$ENVIRONMENT/gateway/apigw-invoke-url" \
    --query "Parameter.Value" --output text \
    --region "$AWS_REGION" 2>/dev/null || echo "")
  # NOTE: these guards are written as explicit `if` blocks rather than
  # `[ ... ] && return 0` — under `set -e` a trailing test that evaluates false
  # makes the function return non-zero, which would be read as an error.
  if [ -z "$invoke_url" ] || [ "$invoke_url" = "None" ]; then
    return 0
  fi

  api_id=$(printf '%s' "$invoke_url" | sed -n 's|^https://\([^.]*\)\..*|\1|p')
  if [ -z "$api_id" ]; then
    return 0
  fi

  resource_id=$(aws apigateway get-resources --rest-api-id "$api_id" \
    --region "$AWS_REGION" \
    --query "items[?path=='/internal/{proxy+}'].id" \
    --output text 2>/dev/null | head -1 || true)
  if [ -z "$resource_id" ] || [ "$resource_id" = "None" ]; then
    return 0
  fi

  aws apigateway get-integration --rest-api-id "$api_id" \
    --resource-id "$resource_id" --http-method ANY \
    --region "$AWS_REGION" --query 'uri' --output text 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# Step 3: Resolve the two ALB DNS names we compare the integration URI against
# ---------------------------------------------------------------------------
INTERNAL_PLANE_ALB_DNS=$(aws ssm get-parameter \
  --name "/adp/$ENVIRONMENT/gateway/internal-plane-alb-dns" \
  --query "Parameter.Value" --output text \
  --region "$AWS_REGION" 2>/dev/null || echo "")
[ "$INTERNAL_PLANE_ALB_DNS" = "None" ] && INTERNAL_PLANE_ALB_DNS=""

EDGE_ALB_DNS=$(aws ssm get-parameter \
  --name "/adp/$ENVIRONMENT/gateway/internal-alb-dns" \
  --query "Parameter.Value" --output text \
  --region "$AWS_REGION" 2>/dev/null || echo "")
[ "$EDGE_ALB_DNS" = "None" ] && EDGE_ALB_DNS=""

INTEGRATION_URI="$(get_internal_integration_uri)"
[ "$INTEGRATION_URI" = "None" ] && INTEGRATION_URI=""

# The precondition: the live integration resolves to the internal-plane ALB.
# Requires a non-empty DNS to compare against, so an empty SSM param can never
# vacuously satisfy it.
repoint_landed() {
  [ -n "$INTERNAL_PLANE_ALB_DNS" ] && [ -n "$INTEGRATION_URI" ] &&
    [[ "$INTEGRATION_URI" == *"$INTERNAL_PLANE_ALB_DNS"* ]]
}

# The outage state: the integration still targets the edge ALB, which is the ALB
# the deny would be (or is) applied to.
integration_still_on_edge() {
  [ -n "$EDGE_ALB_DNS" ] && [ -n "$INTEGRATION_URI" ] &&
    [[ "$INTEGRATION_URI" == *"$EDGE_ALB_DNS"* ]]
}

echo "Internal-plane deny gate (#4010)"
echo "  environment:            $ENVIRONMENT"
echo "  edge ALB DNS:           ${EDGE_ALB_DNS:-<unset>}"
echo "  internal-plane ALB DNS: ${INTERNAL_PLANE_ALB_DNS:-<unset>}"
echo "  live /internal/{proxy+} integration URI: ${INTEGRATION_URI:-<unresolved>}"

# ---------------------------------------------------------------------------
# --remove: roll the deny back off
# ---------------------------------------------------------------------------
if [ "$MODE" = "remove" ]; then
  if ! deny_is_live; then
    echo "Deny is not applied — nothing to remove."
    exit 0
  fi
  echo "Removing the edge deny (restoring plain forwarding for /internal)..."
  # Drop the annotation, then restore the single catch-all rule. Both are needed:
  # the annotation alone leaves a rule pointing at a nonexistent Service, and the
  # rule alone leaves a dangling action.
  kubectl annotate ingress "$INGRESS_NAME" -n "$NAMESPACE" \
    "alb.ingress.kubernetes.io/actions.deny-internal-" --overwrite >/dev/null
  kubectl apply -f "${REPO_ROOT}/modules/gateway/k8s/ingress.yaml" -n "$NAMESPACE"
  echo "Deny removed. NOTE: ELB config takes ~30s to propagate — re-test after a delay."
  exit 0
fi

# ---------------------------------------------------------------------------
# --verify: assert the invariant, change nothing
# ---------------------------------------------------------------------------
# The state that must never exist is: deny live on the edge ALB AND the API-GW
# integration still targeting that same edge ALB. That combination 403s every
# SigV4 internal call. This is the assertion the PR review asked for.
if [ "$MODE" = "verify" ]; then
  if deny_is_live && integration_still_on_edge; then
    echo "::error::INTERNAL-PLANE OUTAGE STATE DETECTED (#4010)." >&2
    echo "::error::The edge ALB is denying /internal (actions.deny-internal is live on" >&2
    echo "::error::ingress/${INGRESS_NAME}), but the API Gateway /internal/{proxy+}" >&2
    echo "::error::integration STILL TARGETS THE EDGE ALB (${EDGE_ALB_DNS})." >&2
    echo "::error::Every SigV4 internal call (agent provenance, credential paths) is" >&2
    echo "::error::getting a 403 from the edge ALB." >&2
    echo "::error::" >&2
    echo "::error::To recover immediately, remove the deny:" >&2
    echo "::error::  bash modules/gateway/scripts/apply-internal-plane-deny.sh --remove" >&2
    echo "::error::Then repoint the integration before re-applying it:" >&2
    echo "::error::  bash platform/scripts/wire-gateway-alb.sh --apply" >&2
    exit 1
  fi
  if deny_is_live; then
    echo "OK: deny is live and the integration is repointed off the edge ALB."
  else
    echo "OK: deny is not applied; the internal plane is served as before."
  fi
  exit 0
fi

# ---------------------------------------------------------------------------
# Default: apply, but only when the precondition holds
# ---------------------------------------------------------------------------
if deny_is_live; then
  # Check the outage state BEFORE re-asserting anything. If the deny is live while
  # the integration still targets this same ALB, re-applying the deny would
  # entrench an active outage. Fail loudly with the recovery command instead.
  if integration_still_on_edge; then
    echo "::error::Deny is live but /internal/{proxy+} still targets the edge ALB" >&2
    echo "::error::(${EDGE_ALB_DNS}) — internal calls are being 403'd right now." >&2
    echo "::error::Refusing to re-assert the deny. To recover immediately:" >&2
    echo "::error::  bash modules/gateway/scripts/apply-internal-plane-deny.sh --remove" >&2
    exit 1
  fi
  # Re-assert the manifest so drift (a manual edit, or a blanket re-apply of
  # ingress.yaml that reverted the rules) is corrected. Safe: the deny is already
  # in effect and the integration is off this ALB, so reachability is unchanged.
  echo "Deny already applied — re-asserting the patch to correct any drift."
  kubectl patch ingress "$INGRESS_NAME" -n "$NAMESPACE" \
    --type=merge --patch-file "$PATCH_FILE"
  exit 0
fi

if ! repoint_landed; then
  echo "::notice::Skipping the edge internal-plane deny (#4010) — precondition not met."
  if [ -z "$INTERNAL_PLANE_ALB_DNS" ]; then
    echo "  Reason: the internal-plane ALB is not discovered yet."
    echo "  (k8s/ingress-internal.yaml may not have been applied, or its ALB is"
    echo "   still provisioning — that takes ~2-3 min.)"
  elif [ -z "$INTEGRATION_URI" ]; then
    echo "  Reason: could not read the live /internal/{proxy+} integration URI."
    echo "  Not applying the deny — doing so blind risks a 403 on all internal calls."
  else
    echo "  Reason: the integration still targets ${INTEGRATION_URI}."
    echo "  It must be repointed at the internal-plane ALB first, via"
    echo "  gateway-infra-apply.yml (or platform/scripts/wire-gateway-alb.sh --apply)."
  fi
  echo "  This is safe and expected on a first rollout: the internal plane keeps"
  echo "  working exactly as before, and a later deploy applies the deny once the"
  echo "  repoint has landed. No manual action is required."
  exit 0
fi

echo "Precondition met: /internal/{proxy+} is repointed at the internal-plane ALB."
echo "Applying the edge deny..."
kubectl patch ingress "$INGRESS_NAME" -n "$NAMESPACE" \
  --type=merge --patch-file "$PATCH_FILE"

echo "Edge deny applied. /internal/* now returns 403 at the edge ALB."
echo "NOTE: ELB config changes take ~30s to propagate. A smoke test run"
echo "      immediately will read a STALE PASS — retry for ~60s."
