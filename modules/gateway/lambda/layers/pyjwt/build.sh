#!/bin/bash
# =============================================================================
# PyJWT Lambda Layer Build Script (Issue #239)
# =============================================================================
# Builds PyJWT with cryptography extras into a Lambda layer structure.
# Uses Docker to build inside the Lambda runtime image for binary compatibility.
#
# Output: lambda/layers/pyjwt/python/ directory ready for Lambda Layer zip
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

echo "Building PyJWT Lambda layer using ${RUNTIME}..."

# Clean previous build
rm -rf "${OUTPUT_DIR}"
mkdir -p "${OUTPUT_DIR}"

# Build using the official Lambda Python 3.13 image
# --platform linux/amd64 ensures x86_64 binaries matching CI runner and Lambda default arch
${RUNTIME} run --rm \
  --platform linux/amd64 \
  -v "${OUTPUT_DIR}:/output" \
  --entrypoint /bin/bash \
  public.ecr.aws/lambda/python:3.13 \
  -c "pip install 'PyJWT[crypto]>=2.9.0' -t /output --no-cache-dir --quiet && \
      find /output -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true && \
      find /output -name '*.dist-info' -exec rm -rf {} + 2>/dev/null || true && \
      find /output -name '*.egg-info' -exec rm -rf {} + 2>/dev/null || true"

# Verify the build
if [[ -d "${OUTPUT_DIR}/jwt" ]]; then
    echo "PyJWT layer built successfully at ${OUTPUT_DIR}"
    echo "Size: $(du -sh "${OUTPUT_DIR}" | cut -f1)"
else
    echo "ERROR: PyJWT installation failed - jwt module not found"
    exit 1
fi
