#!/bin/bash
# =============================================================================
# codebuild-run.sh — Per-build-isolated CodeBuild trigger
# =============================================================================
# Uploads source to a unique S3 key and starts a CodeBuild build with
# --source-location-override, eliminating the shared-key race condition.
#
# Usage:
#   codebuild-run.sh <project-name> [env-var-overrides...]
#
# Environment variables (required):
#   STATE_BUCKET  — S3 bucket for source artifacts
#   AWS_REGION    — AWS region (aliased as REGION for compat)
#
# Environment variables (optional):
#   SOURCE_SHA    — Git SHA to use in the S3 key (default: HEAD)
#   POLL_INTERVAL — Seconds between status polls (default: 15)
#
# Each env-var-override is a CodeBuild format string:
#   "name=KEY,value=VAL"
#
# Example:
#   STATE_BUCKET=adp-terraform-state-123 AWS_REGION=us-west-2 \
#     codebuild-run.sh adp-dev-gateway-build \
#       "name=IMAGE_TAG,value=abc123" "name=REGISTRY,value=..."
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

PROJECT_NAME="${1:?Usage: codebuild-run.sh <project-name> [env-var-overrides...]}"
shift

# Resolve region (support both AWS_REGION and REGION for compat)
REGION="${AWS_REGION:-${REGION:?AWS_REGION or REGION must be set}}"
STATE_BUCKET="${STATE_BUCKET:?STATE_BUCKET must be set}"

# Build a unique source key
SOURCE_SHA="${SOURCE_SHA:-$(git -C "$ROOT_DIR" rev-parse HEAD 2>/dev/null || echo "unknown")}"
UNIQUE_ID="$(date +%s)-$$"
SOURCE_KEY="codebuild/src/${SOURCE_SHA}-${UNIQUE_ID}.zip"

POLL_INTERVAL="${POLL_INTERVAL:-15}"

# --- Upload source to per-build S3 key ---------------------------------------
ZIP_PATH="/tmp/adp-source-${UNIQUE_ID}.zip"
echo "Packaging source → s3://${STATE_BUCKET}/${SOURCE_KEY}"
bash "$SCRIPT_DIR/zip-source.sh" "$ROOT_DIR" "$ZIP_PATH" > /dev/null
aws s3 cp "$ZIP_PATH" "s3://${STATE_BUCKET}/${SOURCE_KEY}" --region "$REGION" > /dev/null
rm -f "$ZIP_PATH"

# --- Start build with source override ----------------------------------------
CMD=(aws codebuild start-build
  --region "$REGION"
  --project-name "$PROJECT_NAME"
  --source-location-override "${STATE_BUCKET}/${SOURCE_KEY}"
  --environment-variables-override
    "name=ADP_SOURCE_SHA,value=${SOURCE_SHA},type=PLAINTEXT"
)

# Append caller-provided env var overrides
for override in "$@"; do
  CMD+=("$override")
done

CMD+=(--query 'build.id' --output text)

BUILD_ID=$("${CMD[@]}")
echo "  Build: $BUILD_ID (source: ${SOURCE_KEY})"

# --- Poll until completion ----------------------------------------------------
PHASE=""
while true; do
  RESULT=$(aws codebuild batch-get-builds --ids "$BUILD_ID" --region "$REGION" \
    --query 'builds[0].{s:buildStatus,p:currentPhase}' --output json 2>/dev/null)
  STATUS=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['s'])" 2>/dev/null || echo "IN_PROGRESS")
  CUR_PHASE=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['p'])" 2>/dev/null || echo "")
  [ "$CUR_PHASE" != "$PHASE" ] && [ -n "$CUR_PHASE" ] && echo "  Phase: $CUR_PHASE" && PHASE="$CUR_PHASE"
  case "$STATUS" in
    SUCCEEDED) echo "  Build succeeded: $PROJECT_NAME"; exit 0 ;;
    FAILED|FAULT|STOPPED|TIMED_OUT)
      echo "  Build $STATUS: $PROJECT_NAME" >&2
      aws codebuild batch-get-builds --ids "$BUILD_ID" --region "$REGION" \
        --query 'builds[0].logs.deepLink' --output text 2>/dev/null >&2 || true
      exit 1
      ;;
    *) sleep "$POLL_INTERVAL" ;;
  esac
done
