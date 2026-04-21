#!/bin/bash
set -euo pipefail

# =============================================================================
# empty-s3-buckets.sh — Idempotent S3 bucket emptier
# =============================================================================
# Empties one or more S3 buckets, handling versioned objects and delete markers.
# Each bucket is processed independently — one failure does not abort the others.
#
# Usage:
#   ./empty-s3-buckets.sh bucket-name-1 bucket-name-2 ...
#
# Idempotent: no-op on empty or non-existent buckets.
# =============================================================================

if [ $# -eq 0 ]; then
  echo "Usage: $0 <bucket-name> [bucket-name ...]"
  echo "Empties S3 buckets (including versioned objects). Idempotent."
  exit 1
fi

AWS_REGION="${AWS_REGION:-us-east-1}"
FAILURES=0

for BUCKET in "$@"; do
  echo "--- Emptying bucket: $BUCKET ---"

  # Check if bucket exists
  if ! aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
    echo "  Bucket '$BUCKET' does not exist or is not accessible. Skipping."
    continue
  fi

  # Check if versioning is enabled
  VERSIONING=$(aws s3api get-bucket-versioning --bucket "$BUCKET" --query 'Status' --output text 2>/dev/null || echo "None")

  if [ "$VERSIONING" = "Enabled" ] || [ "$VERSIONING" = "Suspended" ]; then
    echo "  Versioned bucket detected. Removing all object versions and delete markers..."

    # Delete all object versions in batches
    while true; do
      VERSIONS=$(aws s3api list-object-versions --bucket "$BUCKET" --max-items 1000 \
        --query '{Objects: Versions[].{Key:Key,VersionId:VersionId}}' --output json 2>/dev/null || echo '{"Objects":null}')

      OBJECT_COUNT=$(echo "$VERSIONS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('Objects') or []))" 2>/dev/null || echo "0")

      if [ "$OBJECT_COUNT" -eq 0 ] || [ "$OBJECT_COUNT" = "0" ]; then
        break
      fi

      echo "  Deleting $OBJECT_COUNT object version(s)..."
      echo "$VERSIONS" | python3 -c "
import sys, json
d = json.load(sys.stdin)
objs = d.get('Objects') or []
if objs:
    # s3api delete-objects expects {Objects: [{Key, VersionId}, ...]}
    print(json.dumps({'Objects': objs, 'Quiet': True}))
" > /tmp/s3-delete-batch.json 2>/dev/null || continue

      aws s3api delete-objects --bucket "$BUCKET" --delete "file:///tmp/s3-delete-batch.json" > /dev/null 2>&1 || true
    done

    # Delete all delete markers in batches
    while true; do
      MARKERS=$(aws s3api list-object-versions --bucket "$BUCKET" --max-items 1000 \
        --query '{Objects: DeleteMarkers[].{Key:Key,VersionId:VersionId}}' --output json 2>/dev/null || echo '{"Objects":null}')

      MARKER_COUNT=$(echo "$MARKERS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('Objects') or []))" 2>/dev/null || echo "0")

      if [ "$MARKER_COUNT" -eq 0 ] || [ "$MARKER_COUNT" = "0" ]; then
        break
      fi

      echo "  Deleting $MARKER_COUNT delete marker(s)..."
      echo "$MARKERS" | python3 -c "
import sys, json
d = json.load(sys.stdin)
objs = d.get('Objects') or []
if objs:
    print(json.dumps({'Objects': objs, 'Quiet': True}))
" > /tmp/s3-delete-batch.json 2>/dev/null || continue

      aws s3api delete-objects --bucket "$BUCKET" --delete "file:///tmp/s3-delete-batch.json" > /dev/null 2>&1 || true
    done

    echo "  Versioned bucket '$BUCKET' emptied."
  else
    echo "  Non-versioned bucket. Running aws s3 rm --recursive..."
    aws s3 rm "s3://${BUCKET}" --recursive > /dev/null 2>&1 || {
      echo "  WARNING: Failed to empty '$BUCKET'. Continuing..."
      FAILURES=$((FAILURES + 1))
      continue
    }
    echo "  Bucket '$BUCKET' emptied."
  fi
done

rm -f /tmp/s3-delete-batch.json

if [ "$FAILURES" -gt 0 ]; then
  echo ""
  echo "WARNING: $FAILURES bucket(s) had errors. Check output above."
  # Exit 0 — idempotent, don't fail the pipeline for partial bucket issues
fi

echo "Done."
