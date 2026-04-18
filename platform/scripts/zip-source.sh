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
zip -r "$OUTPUT" \
  platform/ modules/ environments/ libs/ \
  -x '*/node_modules/*' '*/.terraform/*' '*/coverage/*' '*/__pycache__/*' \
  '*.pyc' '*.tfstate*' '*/dist/*' '*/uv.lock' '*/package-lock.json' \
  > /dev/null 2>&1

echo "$OUTPUT"
