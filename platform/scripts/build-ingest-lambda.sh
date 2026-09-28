#!/usr/bin/env bash
# =============================================================================
# Build the ingest Lambda deployment package.
# =============================================================================
# The ingest Lambda needs pyjwt + cryptography to sign the RS256 JWT used when
# exchanging GitHub App credentials for an installation access token. Lambda
# runtimes don't ship these, so we bundle them into the deploy zip by
# pip-installing into the source directory before Terraform zips it.
#
# Uses manylinux wheels so this works regardless of the host OS (mac, linux,
# arm64, etc). pip will refuse to install incompatible wheels — no accidental
# native-darwin artifacts in the zip.
#
# Idempotent: re-installs on every run so bumping requirements.txt picks up.
#
# Usage (called manually before `terraform apply` on agent-factory, or from
# platform/scripts/deploy-all.sh):
#   bash platform/scripts/build-ingest-lambda.sh
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
INGEST_DIR="${REPO_ROOT}/modules/agent-factory/gateway/lambdas/ingest"
REQ_FILE="${INGEST_DIR}/requirements.txt"

if [[ ! -f "${REQ_FILE}" ]]; then
  echo "[build-ingest] No requirements.txt at ${REQ_FILE} — nothing to install."
  exit 0
fi

echo "[build-ingest] Installing deps from ${REQ_FILE} into ${INGEST_DIR}"

# Clean any previous dep dirs (but NOT the .py source). These match what
# `pip install -t <dir>` creates.
find "${INGEST_DIR}" -maxdepth 1 -mindepth 1 -type d \
  \( -name '*.dist-info' -o -name '*.libs' \) -exec rm -rf {} + 2>/dev/null || true
# Keep the 'channels' package (our code) but clear pip-installed top-level
# packages by removing known deps.
for pkg in jwt cryptography cffi _cffi_backend; do
  rm -rf "${INGEST_DIR:?}/${pkg}" 2>/dev/null || true
done

# Install. --platform manylinux2014_x86_64 forces wheels built for Amazon Linux
# 2/2023 compatibility. --python-version 3.12 matches the Lambda runtime.
# --only-binary=:all: refuses source builds so we never accidentally ship
# Mac/Windows binaries.
pip install \
  --quiet \
  --target "${INGEST_DIR}" \
  --platform manylinux2014_x86_64 \
  --python-version 3.12 \
  --only-binary=:all: \
  --upgrade \
  -r "${REQ_FILE}"

echo "[build-ingest] Done. Top-level packages in ${INGEST_DIR}:"
ls -1 "${INGEST_DIR}" | grep -Ev '^\.' | sed 's/^/  /'
