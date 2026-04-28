#!/bin/bash
# =============================================================================
# Phase 8: End-to-End Smoke Test
# =============================================================================
# Submit EICAR test file through the full CAPE pipeline and verify a report.
#
# EICAR is a 68-byte test string (not malware) defined by the European
# Institute for Computer Antivirus Research. It's the standard way to test
# AV/sandbox pipelines without using real samples.
#
# Hard invariant #5: No real malware. EICAR only.
#
# Can be run from:
#   a) The CAPE host itself (via SSM session)
#   b) A pod in the ADP EKS cluster (via the peered ALB)
#
# Usage (from ADP EKS pod):
#   TOKEN=$(aws secretsmanager get-secret-value --secret-id adp/cape/api-token \
#     --query SecretString --output text)
#   CAPE_URL="https://<cape-alb-dns>"
#   bash 05-smoke-test.sh "$CAPE_URL" "$TOKEN"
#
# Usage (from CAPE host, local API):
#   bash 05-smoke-test.sh "http://localhost:8000" ""
# =============================================================================
set -euo pipefail

CAPE_URL="${1:-http://localhost:8000}"
TOKEN="${2:-}"

AUTH_HEADER=""
if [ -n "$TOKEN" ]; then
  AUTH_HEADER="Authorization: Bearer $TOKEN"
fi

echo "=== Phase 8: EICAR End-to-End Smoke Test ==="
echo "CAPE URL: $CAPE_URL"
echo ""

# ---------------------------------------------------------------------------
# Step 1: Download EICAR test file
# ---------------------------------------------------------------------------
EICAR_FILE="/tmp/eicar.com.txt"
echo "Downloading EICAR test file..."

# EICAR string (68 bytes, standard test pattern)
# Hard invariant #5: This is NOT malware. It's a standardized test string.
cat > "$EICAR_FILE" <<'EICAR'
X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*
EICAR

echo "EICAR file ready ($(wc -c < "$EICAR_FILE") bytes)"

# ---------------------------------------------------------------------------
# Step 2: Submit to CAPE
# ---------------------------------------------------------------------------
echo "Submitting to CAPE..."

SUBMIT_ARGS=(-s -X POST -F "file=@$EICAR_FILE")
if [ -n "$AUTH_HEADER" ]; then
  SUBMIT_ARGS+=(-H "$AUTH_HEADER")
fi

SUBMIT_RESPONSE=$(curl "${SUBMIT_ARGS[@]}" "$CAPE_URL/apiv2/tasks/create/file/")
TASK_ID=$(echo "$SUBMIT_RESPONSE" | jq -r '.data.task_ids[0] // .data.task_id // .task_id // empty')

if [ -z "$TASK_ID" ]; then
  echo "ERROR: Failed to submit sample"
  echo "Response: $SUBMIT_RESPONSE"
  exit 1
fi

echo "Submitted. Task ID: $TASK_ID"

# ---------------------------------------------------------------------------
# Step 3: Poll for completion
# ---------------------------------------------------------------------------
echo "Polling for completion (up to 10 minutes)..."

STATUS_ARGS=(-s)
if [ -n "$AUTH_HEADER" ]; then
  STATUS_ARGS+=(-H "$AUTH_HEADER")
fi

for i in $(seq 1 60); do
  STATUS_RESPONSE=$(curl "${STATUS_ARGS[@]}" "$CAPE_URL/apiv2/tasks/view/$TASK_ID/")
  STATUS=$(echo "$STATUS_RESPONSE" | jq -r '.data.status // .status // "unknown"')

  echo "  [$i/60] Status: $STATUS"

  if [ "$STATUS" = "reported" ] || [ "$STATUS" = "completed" ]; then
    echo "Analysis complete!"
    break
  fi

  if [ "$STATUS" = "failed_analysis" ] || [ "$STATUS" = "failed_processing" ]; then
    echo "ERROR: Analysis failed with status: $STATUS"
    echo "Response: $STATUS_RESPONSE"
    exit 1
  fi

  sleep 10
done

if [ "$STATUS" != "reported" ] && [ "$STATUS" != "completed" ]; then
  echo "ERROR: Timed out waiting for analysis (last status: $STATUS)"
  exit 1
fi

# ---------------------------------------------------------------------------
# Step 4: Fetch and verify report
# ---------------------------------------------------------------------------
echo ""
echo "Fetching report..."

REPORT_ARGS=(-s)
if [ -n "$AUTH_HEADER" ]; then
  REPORT_ARGS+=(-H "$AUTH_HEADER")
fi

REPORT=$(curl "${REPORT_ARGS[@]}" "$CAPE_URL/apiv2/tasks/report/$TASK_ID/")

FILE_NAME=$(echo "$REPORT" | jq -r '.target.file.name // "unknown"')
ENDED=$(echo "$REPORT" | jq -r '.info.ended // "unknown"')
SCORE=$(echo "$REPORT" | jq -r '.info.score // "N/A"')

echo ""
echo "================================================================"
echo "SMOKE TEST RESULTS"
echo "================================================================"
echo "Task ID:    $TASK_ID"
echo "File Name:  $FILE_NAME"
echo "Ended:      $ENDED"
echo "Score:      $SCORE"
echo "================================================================"

if [ "$FILE_NAME" = "eicar.com.txt" ] && [ "$ENDED" != "unknown" ] && [ "$ENDED" != "null" ]; then
  echo ""
  echo "PASS: EICAR test completed successfully"
  echo "  - File name matches: eicar.com.txt"
  echo "  - Analysis timestamp present: $ENDED"
  exit 0
else
  echo ""
  echo "FAIL: Report validation failed"
  echo "  Expected file name: eicar.com.txt, got: $FILE_NAME"
  echo "  Expected ended timestamp, got: $ENDED"
  exit 1
fi
