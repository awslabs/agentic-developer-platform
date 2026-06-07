#!/usr/bin/env bash
# =============================================================================
# seed-brain.sh — Import existing ADP learnings into gbrain
# Usage: ./scripts/seed-brain.sh [MCP_URL]
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TF_DIR="$(dirname "$SCRIPT_DIR")/terraform"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../../" && pwd)"
AWS_REGION="${AWS_REGION:-us-east-1}"

# Get MCP endpoint
if [ -n "${1:-}" ]; then
  MCP_URL="$1"
else
  MCP_URL=$(terraform -chdir="${TF_DIR}" output -raw mcp_endpoint 2>/dev/null || echo "")
  if [ -z "$MCP_URL" ]; then
    echo "ERROR: Cannot determine MCP endpoint. Pass as argument or ensure terraform state is available."
    exit 1
  fi
fi

# Get bearer token
TOKEN=$(aws secretsmanager get-secret-value \
  --secret-id "adp/research/gbrain/mcp-token" \
  --query SecretString --output text \
  --region "${AWS_REGION}")

LEARNING_DIR="${REPO_ROOT}/agent_learning"
if [ ! -d "$LEARNING_DIR" ]; then
  echo "No agent_learning/ directory found. Nothing to seed."
  exit 0
fi

echo "=== Seeding gbrain from agent_learning/ ==="
echo "Source: ${LEARNING_DIR}"
echo "Target: ${MCP_URL}"
echo ""

COUNT=0
ERRORS=0

for file in "${LEARNING_DIR}"/*.md; do
  [ -f "$file" ] || continue
  FILENAME=$(basename "$file")
  SLUG="seed/$(echo "$FILENAME" | sed 's/\.md$//' | tr ' ' '-')"
  CONTENT=$(cat "$file" | jq -Rs .)

  echo -n "  Importing ${FILENAME}... "
  RESULT=$(curl -s -X POST "${MCP_URL}" \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "Content-Type: application/json" \
    --max-time 30 \
    -d "{\"jsonrpc\":\"2.0\",\"id\":$((COUNT+1)),\"method\":\"tools/call\",\"params\":{\"name\":\"capture\",\"arguments\":{\"content\":${CONTENT},\"slug\":\"${SLUG}\"}}}" 2>/dev/null || echo '{"error":"request failed"}')

  if echo "$RESULT" | jq -e '.error' >/dev/null 2>&1; then
    echo "FAIL"
    ERRORS=$((ERRORS + 1))
  else
    echo "OK"
    COUNT=$((COUNT + 1))
  fi
done

echo ""
echo "=== Seeding Complete: ${COUNT} imported, ${ERRORS} errors ==="
