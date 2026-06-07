#!/usr/bin/env bash
# =============================================================================
# smoke-test.sh — Validate gbrain MCP endpoint is responding
# Usage: ./scripts/smoke-test.sh [MCP_URL]
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TF_DIR="$(dirname "$SCRIPT_DIR")/terraform"
AWS_REGION="${AWS_REGION:-us-east-1}"

# Get MCP endpoint from Terraform output or argument
if [ -n "${1:-}" ]; then
  MCP_URL="$1"
else
  MCP_URL=$(terraform -chdir="${TF_DIR}" output -raw mcp_endpoint 2>/dev/null || echo "")
  if [ -z "$MCP_URL" ]; then
    echo "ERROR: Cannot determine MCP endpoint. Pass it as argument or run from a directory with terraform state."
    exit 1
  fi
fi

# Get bearer token from Secrets Manager
TOKEN=$(aws secretsmanager get-secret-value \
  --secret-id "adp/research/gbrain/mcp-token" \
  --query SecretString --output text \
  --region "${AWS_REGION}" 2>/dev/null || echo "")

if [ -z "$TOKEN" ]; then
  echo "ERROR: Cannot retrieve MCP token from Secrets Manager"
  exit 1
fi

PASS=0
FAIL=0

echo "=== gbrain Smoke Test ==="
echo "Endpoint: ${MCP_URL}"
echo ""

# Test 1: Health check
echo -n "1. Health check... "
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "${MCP_URL}/health" 2>/dev/null || echo "000")
if [ "$HTTP_CODE" = "200" ]; then
  echo "PASS (HTTP $HTTP_CODE)"
  PASS=$((PASS + 1))
else
  echo "FAIL (HTTP $HTTP_CODE)"
  FAIL=$((FAIL + 1))
fi

# Test 2: MCP tools/list
echo -n "2. MCP tools/list... "
TOOLS_RAW=$(curl -s -X POST "${MCP_URL}/mcp" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  --max-time 10 \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' 2>/dev/null || echo "{}")
# Response is SSE format (data: {...}); extract JSON payload
TOOLS_RESPONSE=$(echo "$TOOLS_RAW" | grep "^data:" | head -1 | sed 's/^data: //')
TOOLS_COUNT=$(echo "$TOOLS_RESPONSE" | jq '.result.tools | length' 2>/dev/null || echo "0")
if [ "$TOOLS_COUNT" -gt 0 ] 2>/dev/null; then
  echo "PASS ($TOOLS_COUNT tools available)"
  PASS=$((PASS + 1))
else
  echo "FAIL (no tools returned)"
  FAIL=$((FAIL + 1))
fi

# Test 3: Write (capture)
echo -n "3. Write test (capture)... "
WRITE_RAW=$(curl -s -X POST "${MCP_URL}/mcp" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  --max-time 15 \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"put_page","arguments":{"content":"---\ntitle: Smoke Test\n---\nsmoke-test-page content","slug":"test/smoke-test"}}}' 2>/dev/null || echo "{}")
WRITE_RESULT=$(echo "$WRITE_RAW" | grep "^data:" | head -1 | sed 's/^data: //')
WRITE_STATUS=$(echo "$WRITE_RESULT" | jq -r '.result.content[0].text // .error.message // "unknown"' 2>/dev/null || echo "error")
if echo "$WRITE_STATUS" | grep -qi "error\|fail\|unknown"; then
  echo "FAIL ($WRITE_STATUS)"
  FAIL=$((FAIL + 1))
else
  echo "PASS"
  PASS=$((PASS + 1))
fi

# Test 4: Read (search)
echo -n "4. Read test (search)... "
SEARCH_RAW=$(curl -s -X POST "${MCP_URL}/mcp" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  --max-time 15 \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"search","arguments":{"query":"smoke-test-page"}}}' 2>/dev/null || echo "{}")
SEARCH_RESULT=$(echo "$SEARCH_RAW" | grep "^data:" | head -1 | sed 's/^data: //')
SEARCH_STATUS=$(echo "$SEARCH_RESULT" | jq -r '.result.content[0].text // .error.message // "unknown"' 2>/dev/null || echo "error")
if echo "$SEARCH_STATUS" | grep -qi "error\|fail\|unknown"; then
  echo "FAIL ($SEARCH_STATUS)"
  FAIL=$((FAIL + 1))
else
  echo "PASS"
  PASS=$((PASS + 1))
fi

echo ""
echo "=== Results: ${PASS} passed, ${FAIL} failed ==="

if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
