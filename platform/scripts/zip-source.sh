#!/bin/bash
# =============================================================================
# Shared source packaging script
# =============================================================================
# Used by both deploy-all.sh and gateway-deploy.yml to create a consistent
# source zip for CodeBuild. Keep the exclude list in sync!
# =============================================================================
set -euo pipefail

ROOT_DIR="${1:-.}"
OUTPUT="${2:-/tmp/adp-source.zip}"

cd "$ROOT_DIR"
# Include codebuild/ when present so checked-in buildspecs reach CodeBuild.
INCLUDE_DIRS=(platform/ modules/ environments/ libs/)
[ -d codebuild ] && INCLUDE_DIRS+=(codebuild/)

# Keep package-lock.json files — `npm ci` needs them for reproducible
# Docker builds (e.g. the TS agent image in modules/agent-factory/agent).
zip -r "$OUTPUT" \
  "${INCLUDE_DIRS[@]}" \
  -x '*/node_modules/*' '*/.terraform/*' '*/coverage/*' '*/__pycache__/*' \
  '*.pyc' '*.tfstate*' '*/dist/*' '*/uv.lock' \
  > /dev/null 2>&1

echo "$OUTPUT"
