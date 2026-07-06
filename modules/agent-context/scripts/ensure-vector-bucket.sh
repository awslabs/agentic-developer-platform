#!/usr/bin/env bash
# Ensure the S3 Vectors bucket exists (idempotent).
#
# The vector bucket backs semantic search (code shards) and per-user memory
# (remember/experience personal-context indexes). The Terraform
# aws_s3vectors_vector_bucket resource is commented out pending AWS provider
# >= 5.101 support (see terraform/modules/s3-vectors/main.tf), so the bucket is
# created here via the AWS CLI instead.
#
# Safe to run repeatedly: if the bucket already exists this is a no-op.
#
# Usage: ./scripts/ensure-vector-bucket.sh
#
# Config (from config.env / config.local.env, override via env):
#   S3_VECTORS_BUCKET_NAME  Bucket name (default: adp-<env>-code-vectors-<account>)
#   S3_VECTORS_REGION       Region for the bucket (falls back to AWS_REGION)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Source configuration (config.env + optional config.local.env)
source "${SCRIPT_DIR}/_common.sh"
load_config "${ROOT_DIR}"

REGION="${S3_VECTORS_REGION:-${AWS_REGION:-us-east-1}}"

# Resolve the bucket name. If config left the account-id placeholder unresolved
# or the var is empty, derive it from the caller identity.
BUCKET_NAME="${S3_VECTORS_BUCKET_NAME:-}"
if [ -z "${BUCKET_NAME}" ]; then
  ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
  BUCKET_NAME="adp-${ENVIRONMENT:-dev}-code-vectors-${ACCOUNT_ID}"
fi

echo "============================================"
echo "Ensuring S3 Vectors bucket"
echo "============================================"
echo "  Bucket: ${BUCKET_NAME}"
echo "  Region: ${REGION}"

if aws s3vectors get-vector-bucket \
  --vector-bucket-name "${BUCKET_NAME}" \
  --region "${REGION}" >/dev/null 2>&1; then
  echo "  Status: already exists — no action needed"
  exit 0
fi

echo "  Status: not found — creating..."
aws s3vectors create-vector-bucket \
  --vector-bucket-name "${BUCKET_NAME}" \
  --region "${REGION}"

echo "  Created vector bucket: ${BUCKET_NAME}"
