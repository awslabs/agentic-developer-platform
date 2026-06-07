#!/bin/bash
# =============================================================================
# build-lambda-layers.sh
# =============================================================================
# Builds one or more Lambda layer artifacts via CodeBuild and uploads them to
# the Terraform state bucket, where gateway/infra's `aws_s3_object` data
# sources read them at plan time.
#
# This is the SINGLE SOURCE OF TRUTH for "make the layer zips exist". It is
# invoked automatically by the gateway Terraform module (null_resource +
# local-exec) so that EVERY deploy path — deploy-all.sh, stage-by-stage
# `terraform apply`, or CI — builds the layers without the operator having to
# remember a manual step. It can also be run by hand:
#
#   ./platform/scripts/build-lambda-layers.sh psycopg2 pyjwt
#
# Args: one or more layer names. Valid: psycopg2, pyjwt. Defaults to both.
#
# Env (resolved from config/deployment.yml or runtime fallback if unset):
#   AWS_REGION / ADP_REGION   — region (default us-east-1)
#   ADP_ENVIRONMENT / ENVIRONMENT — env name (default dev), selects CodeBuild project prefix
#   ADP_STATE_BUCKET / STATE_BUCKET — state bucket (default adp-terraform-state-<account>)
#
# Idempotent and safe to re-run: each call repackages source and rebuilds.
# CodeBuild does the actual (Docker-based) layer build, so no local Docker is
# required.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# --- Resolve deploy config (account/region/env/bucket) -----------------------
# Prefer already-exported env (e.g. when called from deploy-all.sh) and fall
# back to the shared loader, which reads config/deployment.yml or derives from
# the caller's identity.
if [ -z "${ADP_STATE_BUCKET:-}" ] || [ -z "${AWS_REGION:-}" ]; then
  # shellcheck source=load-deploy-config.sh
  source "${SCRIPT_DIR}/load-deploy-config.sh"
fi

REGION="${AWS_REGION:-${ADP_REGION:-us-east-1}}"
ENV_NAME="${ENVIRONMENT:-${ADP_ENVIRONMENT:-dev}}"
ACCOUNT_ID="${ADP_ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"
STATE_BUCKET="${ADP_STATE_BUCKET:-${STATE_BUCKET:-adp-terraform-state-${ACCOUNT_ID}}}"

# --- Which layers to build ---------------------------------------------------
LAYERS=("$@")
if [ ${#LAYERS[@]} -eq 0 ]; then
  LAYERS=(psycopg2 pyjwt)
fi

# Map layer name → CodeBuild project + expected S3 artifact key (for logging).
layer_project() {
  case "$1" in
    psycopg2) echo "adp-${ENV_NAME}-psycopg2-layer" ;;
    pyjwt)    echo "adp-${ENV_NAME}-pyjwt-layer" ;;
    *) echo "" ;;
  esac
}
layer_key() {
  case "$1" in
    psycopg2) echo "lambda-layers/psycopg2-py312.zip" ;;
    pyjwt)    echo "lambda-layers/pyjwt-py313.zip" ;;
    *) echo "" ;;
  esac
}

echo "build-lambda-layers: region=$REGION env=$ENV_NAME bucket=$STATE_BUCKET layers=${LAYERS[*]}"

# --- Package + upload source once (CodeBuild S3 source) ----------------------
# The layer CodeBuild projects read their source from
# s3://<bucket>/codebuild/adp-source.zip. Keep this in sync with deploy-all.sh.
SRC_ZIP="/tmp/adp-layer-source.$$.zip"
echo "Packaging repository source..."
bash "${SCRIPT_DIR}/zip-source.sh" "$ROOT_DIR" "$SRC_ZIP" >/dev/null
aws s3 cp "$SRC_ZIP" "s3://${STATE_BUCKET}/codebuild/adp-source.zip" --region "$REGION" >/dev/null
rm -f "$SRC_ZIP"
echo "Source uploaded to s3://${STATE_BUCKET}/codebuild/adp-source.zip"

# --- Helper: start a CodeBuild build and wait for it -------------------------
run_build() {
  local project="$1"
  local build_id
  build_id=$(aws codebuild start-build \
    --project-name "$project" \
    --region "$REGION" \
    --environment-variables-override \
      "name=AWS_REGION,value=${REGION}" \
      "name=ACCOUNT_ID,value=${ACCOUNT_ID}" \
      "name=STATE_BUCKET,value=${STATE_BUCKET}" \
    --query 'build.id' --output text)
  echo "  $project → build $build_id"

  while true; do
    local status
    status=$(aws codebuild batch-get-builds --ids "$build_id" --region "$REGION" \
      --query 'builds[0].buildStatus' --output text 2>/dev/null || echo "IN_PROGRESS")
    case "$status" in
      SUCCEEDED) echo "  $project: SUCCEEDED"; return 0 ;;
      FAILED|FAULT|STOPPED|TIMED_OUT)
        echo "  $project: $status" >&2
        aws codebuild batch-get-builds --ids "$build_id" --region "$REGION" \
          --query 'builds[0].logs.deepLink' --output text 2>/dev/null >&2 || true
        return 1 ;;
      *) sleep 10 ;;
    esac
  done
}

# --- Build each requested layer ----------------------------------------------
for layer in "${LAYERS[@]}"; do
  project="$(layer_project "$layer")"
  key="$(layer_key "$layer")"
  if [ -z "$project" ]; then
    echo "Unknown layer '$layer' (valid: psycopg2, pyjwt)" >&2
    exit 2
  fi
  echo "Building $layer layer → s3://${STATE_BUCKET}/${key}"
  run_build "$project"
done

echo "build-lambda-layers: done."
