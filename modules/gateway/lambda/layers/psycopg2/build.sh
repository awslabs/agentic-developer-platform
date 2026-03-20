#!/usr/bin/env bash
# =============================================================================
# Build psycopg2 Lambda Layer for Python 3.12 (x86_64)
# =============================================================================
# Installs psycopg2-binary into the Lambda layer directory structure.
#
# Uses Docker/finch to build inside the Lambda runtime image, ensuring
# binary compatibility. Works on CI runners (docker) and local dev (finch).
#
# Output: lambda/layers/psycopg2/python/ directory ready for Lambda Layer zip
#
# Usage: ./build.sh [docker|finch]
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_DIR="${SCRIPT_DIR}/python"

# Auto-detect container runtime
RUNTIME="${1:-}"
if [ -z "$RUNTIME" ]; then
  if command -v docker &>/dev/null; then
    RUNTIME="docker"
  elif command -v finch &>/dev/null; then
    RUNTIME="finch"
  else
    echo "ERROR: No container runtime found. Install docker or finch." >&2
    exit 1
  fi
fi

echo "Building psycopg2 Lambda Layer for Python 3.12 using ${RUNTIME}..."

# Clean previous build
rm -rf "${OUTPUT_DIR}"
mkdir -p "${OUTPUT_DIR}"

# Build using the official Lambda Python 3.12 image
# --platform linux/amd64 ensures x86_64 binaries matching Lambda default arch
${RUNTIME} run --rm \
  --platform linux/amd64 \
  -v "${OUTPUT_DIR}:/output" \
  --entrypoint /bin/bash \
  public.ecr.aws/lambda/python:3.12 \
  -c "pip install psycopg2-binary==2.9.9 -t /output --no-cache-dir --quiet && \
      find /output -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true && \
      find /output -name '*.dist-info' -exec rm -rf {} + 2>/dev/null || true && \
      find /output -name '*.egg-info' -exec rm -rf {} + 2>/dev/null || true"

echo "psycopg2 layer built at ${OUTPUT_DIR}"
echo "Size: $(du -sh "${OUTPUT_DIR}" | cut -f1)"
