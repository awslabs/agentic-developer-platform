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
# Repository scans discover targets throughout the committed tree, including
# historical recipes under docs/. Archive that exact tree, not deployment roots
# or untracked/local outputs, so discovery and CodeBuild receive the same inputs.
if [[ "${3:-}" == "--security-scan" ]]; then
  git archive --format=zip --output="$OUTPUT" HEAD
  echo "$OUTPUT"
  exit 0
fi
if [[ -n "${3:-}" ]]; then
  echo "Unknown source archive mode" >&2
  exit 2
fi
# Include codebuild/ when present so checked-in buildspecs reach CodeBuild.
INCLUDE_DIRS=(platform/ modules/ environments/ libs/)
[ -d codebuild ] && INCLUDE_DIRS+=(codebuild/)
# Gateway staging and the worker Dockerfile both consume the shared validators.
[ -d contracts ] && INCLUDE_DIRS+=(contracts/)

# Include root-level config files needed by CodeBuild steps (e.g. grype scans).
ROOT_CONFIGS=()
[ -f .grype.yaml ] && ROOT_CONFIGS+=(.grype.yaml)

# Keep package-lock.json files — `npm ci` needs them for reproducible
# Docker builds (e.g. the TS agent image in modules/agent-factory/agent).
zip -r "$OUTPUT" \
  "${INCLUDE_DIRS[@]}" \
  "${ROOT_CONFIGS[@]}" \
  -x '*/node_modules/*' '*/.terraform/*' '*/coverage/*' '*/__pycache__/*' \
  '*.pyc' '*.tfstate*' '*/dist/*' '*/uv.lock' \
  > /dev/null 2>&1

echo "$OUTPUT"
