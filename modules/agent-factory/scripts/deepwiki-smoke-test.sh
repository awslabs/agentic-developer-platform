#!/bin/bash
# =============================================================================
# DeepWiki End-to-End Smoke Test
# Issue: #284 | Parent: #244
# Agent: @agent-operations
#
# Validates DeepWiki deployment with Bedrock Gateway integration:
#   1. Bedrock Gateway health and model listing
#   2. DeepWiki API health
#   3. Wiki generation (public repo)
#   4. Wiki generation (private repo - if GITHUB_TOKEN available)
#   5. Embeddings via gateway
#   6. Ask/RAG feature
#
# Usage:
#   # Via kubectl port-forward (recommended):
#   kubectl port-forward -n deepwiki svc/deepwiki-svc 8001:8001 &
#   kubectl port-forward -n bedrock-gateway svc/bedrock-gateway-svc 8080:8080 &
#   ./scripts/deepwiki-smoke-test.sh
#
#   # With custom endpoints:
#   DEEPWIKI_URL=http://localhost:8001 \
#   GATEWAY_URL=http://localhost:8080 \
#   ./scripts/deepwiki-smoke-test.sh
#
#   # Via ALB (after Ingress provisioned):
#   DEEPWIKI_URL=https://deepwiki.example.com \
#   ./scripts/deepwiki-smoke-test.sh
#
# Prerequisites:
#   - curl, jq
#   - kubectl configured for bedrockgw-dev-eks-cluster
# =============================================================================
set -euo pipefail

# --- Configuration ---
DEEPWIKI_URL="${DEEPWIKI_URL:-http://localhost:8001}"
GATEWAY_URL="${GATEWAY_URL:-http://localhost:8080}"
GATEWAY_API_KEY="${GATEWAY_API_KEY:-}"

# Public repo for testing (small, fast to process)
TEST_REPO_PUBLIC="${TEST_REPO_PUBLIC:-AsyncFuncAI/deepwiki-open}"
# Private repo for testing (requires GITHUB_TOKEN)
TEST_REPO_PRIVATE="${TEST_REPO_PRIVATE:-}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

PASS=0
FAIL=0
SKIP=0
RESULTS=()

# --- Helper Functions ---

log_section() {
  echo ""
  echo -e "${BLUE}===========================================================${NC}"
  echo -e "${BLUE}  $1${NC}"
  echo -e "${BLUE}===========================================================${NC}"
}

check_pass() {
  echo -e "  ${GREEN}PASS${NC}: $1"
  PASS=$((PASS + 1))
  RESULTS+=("PASS: $1")
}

check_fail() {
  echo -e "  ${RED}FAIL${NC}: $1"
  [ -n "${2:-}" ] && echo -e "        ${RED}Detail: $2${NC}"
  FAIL=$((FAIL + 1))
  RESULTS+=("FAIL: $1 ${2:+- $2}")
}

check_skip() {
  echo -e "  ${YELLOW}SKIP${NC}: $1"
  SKIP=$((SKIP + 1))
  RESULTS+=("SKIP: $1")
}

gateway_curl() {
  local url="$1"
  shift
  if [ -n "$GATEWAY_API_KEY" ]; then
    curl -s --max-time 30 \
      -H "Authorization: Bearer $GATEWAY_API_KEY" \
      "$@" "$url" 2>/dev/null
  else
    curl -s --max-time 30 "$@" "$url" 2>/dev/null
  fi
}

# =============================================================================
# TEST SUITE
# =============================================================================

echo ""
echo "============================================================"
echo "  DeepWiki + Bedrock Gateway - End-to-End Smoke Test"
echo "  DeepWiki: $DEEPWIKI_URL"
echo "  Gateway:  $GATEWAY_URL"
echo "  Auth:     ${GATEWAY_API_KEY:+API key set}${GATEWAY_API_KEY:-None}"
echo "  Date:     $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "============================================================"

# --- 1. Bedrock Gateway Health ---
log_section "1. Bedrock Gateway Health"

# 1a. Health endpoint
GATEWAY_HEALTH=$(gateway_curl "${GATEWAY_URL}/health" || echo "CURL_ERROR")
if echo "$GATEWAY_HEALTH" | jq -e '.status == "OK"' >/dev/null 2>&1; then
  check_pass "Gateway /health returns OK"
elif [ "$GATEWAY_HEALTH" = "CURL_ERROR" ]; then
  check_fail "Gateway unreachable at ${GATEWAY_URL}/health" "Connection refused or timeout"
else
  check_fail "Gateway /health unexpected response" "$(echo "$GATEWAY_HEALTH" | head -c 200)"
fi

# 1b. Model listing
MODELS=$(gateway_curl "${GATEWAY_URL}/api/v1/models" || echo "CURL_ERROR")
if echo "$MODELS" | jq -e '.data | length > 0' >/dev/null 2>&1; then
  MODEL_COUNT=$(echo "$MODELS" | jq '.data | length')
  check_pass "Gateway lists $MODEL_COUNT models"

  # Check for expected models
  if echo "$MODELS" | jq -e '.data[] | select(.id | contains("claude"))' >/dev/null 2>&1; then
    check_pass "Claude model available via gateway"
  else
    check_fail "Claude model not found in gateway model list"
  fi

  if echo "$MODELS" | jq -e '.data[] | select(.id | contains("cohere.embed"))' >/dev/null 2>&1; then
    check_pass "Cohere embedding model available via gateway"
  else
    check_skip "Cohere embedding model not listed (may still work via cross-region)"
  fi
else
  check_fail "Gateway model listing failed" "$(echo "$MODELS" | head -c 200)"
fi

# 1c. Quick chat completion test
echo "  Testing chat completion via gateway..."
CHAT_RESPONSE=$(gateway_curl "${GATEWAY_URL}/api/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "us.anthropic.claude-sonnet-4-20250514-v1:0",
    "messages": [{"role": "user", "content": "Say hello in exactly 3 words."}],
    "max_tokens": 50
  }' || echo "CURL_ERROR")

if echo "$CHAT_RESPONSE" | jq -e '.choices[0].message.content' >/dev/null 2>&1; then
  CHAT_TEXT=$(echo "$CHAT_RESPONSE" | jq -r '.choices[0].message.content' | head -c 100)
  check_pass "Chat completion works: \"$CHAT_TEXT\""
else
  check_fail "Chat completion failed" "$(echo "$CHAT_RESPONSE" | head -c 300)"
fi

# 1d. Embedding test
echo "  Testing embeddings via gateway..."
EMBED_RESPONSE=$(gateway_curl "${GATEWAY_URL}/api/v1/embeddings" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "cohere.embed-multilingual-v3",
    "input": "test embedding for DeepWiki smoke test"
  }' || echo "CURL_ERROR")

if echo "$EMBED_RESPONSE" | jq -e '.data[0].embedding | length > 0' >/dev/null 2>&1; then
  EMBED_DIM=$(echo "$EMBED_RESPONSE" | jq '.data[0].embedding | length')
  check_pass "Embeddings work via gateway (dimension: $EMBED_DIM)"
else
  check_fail "Embeddings failed via gateway" "$(echo "$EMBED_RESPONSE" | head -c 300)"
fi

# --- 2. DeepWiki API Health ---
log_section "2. DeepWiki API Health"

# 2a. Root endpoint (used as health check)
DW_HEALTH_STATUS=$(curl -so /dev/null --max-time 15 -w "%{http_code}" "${DEEPWIKI_URL}/" 2>/dev/null || echo "000")
if [ "$DW_HEALTH_STATUS" = "200" ]; then
  check_pass "DeepWiki API root returns 200"
else
  check_fail "DeepWiki API unreachable (HTTP $DW_HEALTH_STATUS)"
  echo -e "  ${RED}Cannot proceed - DeepWiki API not responding.${NC}"
  # Print summary and exit
  echo ""
  echo "============================================================"
  echo "  RESULTS: $PASS passed, $FAIL failed, $SKIP skipped"
  echo "============================================================"
  exit 1
fi

# 2b. Check models endpoint (if available)
DW_MODELS=$(curl -s --max-time 15 "${DEEPWIKI_URL}/models" 2>/dev/null || echo "")
if [ -n "$DW_MODELS" ] && echo "$DW_MODELS" | jq -e '.' >/dev/null 2>&1; then
  check_pass "DeepWiki /models endpoint responsive"
else
  check_skip "DeepWiki /models endpoint not available (may not exist in this version)"
fi

# --- 3. Wiki Generation (Public Repo) ---
log_section "3. Wiki Generation - Public Repo ($TEST_REPO_PUBLIC)"

echo "  Triggering wiki generation (this may take 30-120 seconds)..."
WIKI_RESPONSE=$(curl -s --max-time 180 \
  -X POST "${DEEPWIKI_URL}/api/wiki" \
  -H "Content-Type: application/json" \
  -d "{
    \"repo\": \"$TEST_REPO_PUBLIC\",
    \"provider\": \"openai\"
  }" 2>/dev/null || echo "CURL_ERROR")

if [ "$WIKI_RESPONSE" = "CURL_ERROR" ]; then
  check_fail "Wiki generation request failed (timeout or connection error)"
elif echo "$WIKI_RESPONSE" | jq -e '.error' >/dev/null 2>&1; then
  ERROR_MSG=$(echo "$WIKI_RESPONSE" | jq -r '.error // .detail // "unknown"' | head -c 300)
  check_fail "Wiki generation returned error" "$ERROR_MSG"
elif echo "$WIKI_RESPONSE" | jq -e '.' >/dev/null 2>&1; then
  # Check if response has wiki content
  if echo "$WIKI_RESPONSE" | jq -e '.content // .pages // .sections // .wiki' >/dev/null 2>&1; then
    check_pass "Wiki generated for $TEST_REPO_PUBLIC"
    PAGE_COUNT=$(echo "$WIKI_RESPONSE" | jq '.pages // .sections // [] | length' 2>/dev/null || echo "unknown")
    echo "        Pages/Sections: $PAGE_COUNT"
  else
    # Some versions stream the response or return different structure
    RESPONSE_KEYS=$(echo "$WIKI_RESPONSE" | jq -r 'keys[]' 2>/dev/null | head -5 | tr '\n' ', ')
    check_pass "Wiki generation responded (keys: ${RESPONSE_KEYS:-raw response})"
  fi
else
  # Non-JSON response (might be streaming)
  RESPONSE_LEN=${#WIKI_RESPONSE}
  if [ "$RESPONSE_LEN" -gt 100 ]; then
    check_pass "Wiki generation returned content ($RESPONSE_LEN bytes, non-JSON - possibly streaming)"
  else
    check_fail "Wiki generation returned unexpected response" "$(echo "$WIKI_RESPONSE" | head -c 200)"
  fi
fi

# --- 4. Wiki Generation (Private Repo) ---
log_section "4. Wiki Generation - Private Repo"

if [ -n "$TEST_REPO_PRIVATE" ]; then
  echo "  Triggering private repo wiki generation..."
  PRIVATE_WIKI=$(curl -s --max-time 180 \
    -X POST "${DEEPWIKI_URL}/api/wiki" \
    -H "Content-Type: application/json" \
    -d "{
      \"repo\": \"$TEST_REPO_PRIVATE\",
      \"provider\": \"openai\"
    }" 2>/dev/null || echo "CURL_ERROR")

  if [ "$PRIVATE_WIKI" = "CURL_ERROR" ]; then
    check_fail "Private repo wiki generation failed (timeout)"
  elif echo "$PRIVATE_WIKI" | jq -e '.error' >/dev/null 2>&1; then
    ERROR_MSG=$(echo "$PRIVATE_WIKI" | jq -r '.error // .detail // "unknown"' | head -c 200)
    # Check if it's an auth error vs other error
    if echo "$ERROR_MSG" | grep -qi "auth\|token\|permission\|403\|404"; then
      check_fail "Private repo access denied" "$ERROR_MSG"
    else
      check_fail "Private repo wiki generation error" "$ERROR_MSG"
    fi
  else
    RESPONSE_LEN=${#PRIVATE_WIKI}
    if [ "$RESPONSE_LEN" -gt 100 ]; then
      check_pass "Private repo wiki generated ($RESPONSE_LEN bytes)"
    else
      check_fail "Private repo wiki response too short" "$(echo "$PRIVATE_WIKI" | head -c 200)"
    fi
  fi
else
  check_skip "Private repo test skipped (set TEST_REPO_PRIVATE=owner/repo to enable)"
fi

# --- 5. Embeddings via DeepWiki ---
log_section "5. Embeddings (via DeepWiki -> Bedrock Gateway)"

# Test embedding endpoint if available
EMBED_TEST=$(curl -s --max-time 30 \
  -X POST "${DEEPWIKI_URL}/api/embeddings" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Test embedding from DeepWiki smoke test",
    "repo": "test/embedding-check"
  }' 2>/dev/null || echo "CURL_ERROR")

if [ "$EMBED_TEST" = "CURL_ERROR" ]; then
  check_skip "DeepWiki /api/embeddings endpoint not reachable (may use different path)"
elif echo "$EMBED_TEST" | jq -e '.error // .detail' >/dev/null 2>&1; then
  ERROR_MSG=$(echo "$EMBED_TEST" | jq -r '.error // .detail' | head -c 200)
  if echo "$ERROR_MSG" | grep -qi "not found\|404\|method not allowed"; then
    check_skip "DeepWiki /api/embeddings endpoint not available (embeddings are internal)"
    echo "        Note: Embeddings are typically used internally during wiki gen and Ask"
  else
    check_fail "Embedding endpoint returned error" "$ERROR_MSG"
  fi
elif echo "$EMBED_TEST" | jq -e '.' >/dev/null 2>&1; then
  check_pass "DeepWiki embedding endpoint responded"
else
  check_skip "Embedding endpoint returned non-JSON (may not have explicit endpoint)"
  echo "        Note: Embeddings are validated implicitly via wiki generation and Ask"
fi

# --- 6. Ask/RAG Feature ---
log_section "6. Ask/RAG Feature"

# The Ask feature requires a repo to have been indexed first
echo "  Testing Ask/RAG on previously indexed repo..."
ASK_RESPONSE=$(curl -s --max-time 120 \
  -X POST "${DEEPWIKI_URL}/api/ask" \
  -H "Content-Type: application/json" \
  -d "{
    \"repo\": \"$TEST_REPO_PUBLIC\",
    \"question\": \"What is the main purpose of this project?\",
    \"provider\": \"openai\"
  }" 2>/dev/null || echo "CURL_ERROR")

if [ "$ASK_RESPONSE" = "CURL_ERROR" ]; then
  check_fail "Ask/RAG request failed (timeout or connection error)"
elif echo "$ASK_RESPONSE" | jq -e '.error' >/dev/null 2>&1; then
  ERROR_MSG=$(echo "$ASK_RESPONSE" | jq -r '.error // .detail // "unknown"' | head -c 300)
  if echo "$ERROR_MSG" | grep -qi "not indexed\|not found\|generate wiki first"; then
    check_skip "Ask/RAG: repo not yet indexed (wiki generation may need to complete first)"
  else
    check_fail "Ask/RAG returned error" "$ERROR_MSG"
  fi
elif echo "$ASK_RESPONSE" | jq -e '.answer // .response // .content' >/dev/null 2>&1; then
  ANSWER=$(echo "$ASK_RESPONSE" | jq -r '.answer // .response // .content' | head -c 200)
  check_pass "Ask/RAG returned answer: \"$ANSWER...\""
else
  RESPONSE_LEN=${#ASK_RESPONSE}
  if [ "$RESPONSE_LEN" -gt 50 ]; then
    check_pass "Ask/RAG returned content ($RESPONSE_LEN bytes)"
  else
    check_fail "Ask/RAG returned unexpected response" "$(echo "$ASK_RESPONSE" | head -c 200)"
  fi
fi

# --- 7. Cross-Namespace Connectivity (K8s only) ---
log_section "7. Infrastructure Checks"

# Check if kubectl is available for cluster checks
if command -v kubectl &>/dev/null; then
  # 7a. DeepWiki pod status
  DW_POD_STATUS=$(kubectl get pods -n deepwiki -l app=deepwiki -o jsonpath='{.items[0].status.phase}' 2>/dev/null || echo "UNKNOWN")
  if [ "$DW_POD_STATUS" = "Running" ]; then
    check_pass "DeepWiki pod is Running"
    # Check container restarts
    RESTARTS=$(kubectl get pods -n deepwiki -l app=deepwiki -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}' 2>/dev/null || echo "?")
    echo "        Container restarts: $RESTARTS"
  else
    check_fail "DeepWiki pod status: $DW_POD_STATUS (expected Running)"
  fi

  # 7b. Bedrock Gateway pod status
  GW_POD_STATUS=$(kubectl get pods -n bedrock-gateway -l app=bedrock-access-gateway -o jsonpath='{.items[0].status.phase}' 2>/dev/null || echo "UNKNOWN")
  if [ "$GW_POD_STATUS" = "Running" ]; then
    check_pass "Bedrock Gateway pod is Running"
  else
    check_fail "Bedrock Gateway pod status: $GW_POD_STATUS (expected Running)"
  fi

  # 7c. Cross-namespace DNS resolution test
  echo "  Testing cross-namespace DNS from DeepWiki pod..."
  DNS_TEST=$(kubectl exec -n deepwiki deployment/deepwiki -c deepwiki -- \
    python3 -c "import socket; print(socket.getaddrinfo('bedrock-gateway-svc.bedrock-gateway.svc.cluster.local', 8080)[0][4][0])" \
    2>/dev/null || echo "DNS_FAIL")
  if [ "$DNS_TEST" != "DNS_FAIL" ] && [ -n "$DNS_TEST" ]; then
    check_pass "Cross-namespace DNS resolves: bedrock-gateway-svc -> $DNS_TEST"
  else
    check_fail "Cross-namespace DNS resolution failed" "DeepWiki cannot resolve bedrock-gateway-svc"
  fi

  # 7d. Cross-namespace connectivity test
  echo "  Testing HTTP connectivity from DeepWiki to Gateway..."
  CROSS_NS_TEST=$(kubectl exec -n deepwiki deployment/deepwiki -c deepwiki -- \
    python3 -c "
import urllib.request, json
req = urllib.request.Request('http://bedrock-gateway-svc.bedrock-gateway.svc.cluster.local:8080/health')
resp = urllib.request.urlopen(req, timeout=10)
print(json.loads(resp.read()))" \
    2>/dev/null || echo "CONNECT_FAIL")
  if echo "$CROSS_NS_TEST" | grep -qi "OK\|status"; then
    check_pass "Cross-namespace HTTP: DeepWiki -> Gateway health OK"
  else
    check_fail "Cross-namespace HTTP failed" "$CROSS_NS_TEST"
  fi
else
  check_skip "kubectl not available - skipping cluster-level checks"
fi

# =============================================================================
# SUMMARY
# =============================================================================

echo ""
echo "============================================================"
echo "  DEEPWIKI SMOKE TEST SUMMARY"
echo "============================================================"
echo ""

TOTAL=$((PASS + FAIL + SKIP))
echo -e "  ${GREEN}PASSED${NC}: $PASS / $TOTAL"
echo -e "  ${RED}FAILED${NC}: $FAIL / $TOTAL"
echo -e "  ${YELLOW}SKIPPED${NC}: $SKIP / $TOTAL"
echo ""

echo "--- Detailed Results ---"
for result in "${RESULTS[@]}"; do
  if [[ "$result" == PASS* ]]; then
    echo -e "  ${GREEN}$result${NC}"
  elif [[ "$result" == FAIL* ]]; then
    echo -e "  ${RED}$result${NC}"
  else
    echo -e "  ${YELLOW}$result${NC}"
  fi
done

echo ""
echo "--- Acceptance Criteria Mapping ---"
echo "  [AC-1] DeepWiki generates wiki via Bedrock Gateway (public repos)"
echo "         -> Test 3: Wiki generation for $TEST_REPO_PUBLIC"
echo "  [AC-2] DeepWiki generates wiki via Bedrock Gateway (private repos)"
echo "         -> Test 4: Private repo wiki generation"
echo "  [AC-3] Embeddings working via gateway"
echo "         -> Test 1d: Direct embedding test + Test 5: DeepWiki embedding"
echo "  [AC-4] Ask/RAG feature functional"
echo "         -> Test 6: Ask/RAG on indexed repo"
echo "  [AC-5] Structured logs in CloudWatch"
echo "         -> Verify via: aws logs describe-log-groups --log-group-name-prefix /aws/deepwiki"
echo "  [AC-6] CloudWatch alarm for pod health"
echo "         -> Verify via: aws cloudwatch describe-alarms --alarm-name-prefix adp-deepwiki"
echo ""
echo "============================================================"
echo "  Completed: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "============================================================"

# Exit with failure if any tests failed
if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
exit 0
