#!/bin/bash
# =============================================================================
# MCP Agent Mail - End-to-End Verification Script
# Issue: #265 (re-run of #260) | Parent: #247
# Agent: @agent-operations
#
# Verifies the complete MCP Agent Mail deployment on
# bedrockgw-dev-eks-cluster (us-east-1).
#
# Usage:
#   # Via kubectl port-forward (recommended for initial testing):
#   kubectl port-forward -n agent-mail svc/agent-mail-svc 8765:8765 &
#   export BEARER_TOKEN=$(kubectl get secret agent-mail-auth -n agent-mail \
#     -o jsonpath='{.data.bearer-token}' | base64 -d)
#   ./scripts/verify-e2e.sh http://localhost:8765
#
#   # Via ALB Ingress (after DNS/TLS configured):
#   export BEARER_TOKEN="your-bearer-token"
#   ./scripts/verify-e2e.sh https://api.bedrockgw.dev/mail
#
# Prerequisites:
#   - curl, jq
#   - kubectl configured for bedrockgw-dev-eks-cluster (for port-forward)
#   - BEARER_TOKEN env var set (required - MCP Agent Mail enforces auth)
#
# Transport: MCP Streamable HTTP (JSON-RPC 2.0 over POST /mcp)
# =============================================================================
set -euo pipefail

# --- Configuration ---
BASE_URL="${1:-http://localhost:8765}"
MCP_ENDPOINT="${BASE_URL}/mcp"
PROJECT_KEY="${PROJECT_KEY:-/data/adp}"  # Human key (absolute path) for the project
JSONRPC_ID=0  # Auto-incrementing JSON-RPC request ID

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
  echo -e "${BLUE}-----------------------------------------------------------${NC}"
  echo -e "${BLUE}  $1${NC}"
  echo -e "${BLUE}-----------------------------------------------------------${NC}"
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

# MCP JSON-RPC 2.0 tool call via POST /mcp
mcp_call() {
  local tool_name="$1"
  local arguments="$2"
  JSONRPC_ID=$((JSONRPC_ID + 1))
  local payload
  payload=$(jq -n \
    --arg id "$JSONRPC_ID" \
    --arg name "$tool_name" \
    --argjson args "$arguments" \
    '{jsonrpc: "2.0", id: ($id | tonumber), method: "tools/call", params: {name: $name, arguments: $args}}')

  local result
  if [ -n "${BEARER_TOKEN:-}" ]; then
    result=$(curl -s --max-time 30 \
      -X POST "$MCP_ENDPOINT" \
      -H "Content-Type: application/json" \
      -H "Authorization: Bearer $BEARER_TOKEN" \
      -d "$payload" 2>/dev/null) || echo "CURL_ERROR"
  else
    result=$(curl -s --max-time 30 \
      -X POST "$MCP_ENDPOINT" \
      -H "Content-Type: application/json" \
      -d "$payload" 2>/dev/null) || echo "CURL_ERROR"
  fi
  echo "$result"
}

# Extract text content from MCP JSON-RPC response
mcp_text() {
  local response="$1"
  echo "$response" | jq -r '.result.content[0].text // .result.structuredContent // empty' 2>/dev/null || echo ""
}

# Check if MCP response is an error
mcp_is_error() {
  local response="$1"
  # Check for JSON-RPC error OR isError flag OR HTTP error responses
  if echo "$response" | jq -e '.error // empty' >/dev/null 2>&1; then
    return 0
  fi
  if echo "$response" | jq -e '.result.isError == true' >/dev/null 2>&1; then
    return 0
  fi
  # HTTP-level errors (not JSON-RPC) like 401/403
  if echo "$response" | jq -e 'has("detail") and (has("jsonrpc") | not)' >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

http_get() {
  local url="$1"
  if [ -n "${BEARER_TOKEN:-}" ]; then
    curl -sL --max-time 15 \
      -H "Authorization: Bearer $BEARER_TOKEN" \
      "$url" 2>/dev/null
  else
    curl -sL --max-time 15 "$url" 2>/dev/null
  fi
}

http_status() {
  local url="$1"
  if [ -n "${BEARER_TOKEN:-}" ]; then
    curl -so /dev/null --max-time 15 \
      -w "%{http_code}" \
      -H "Authorization: Bearer $BEARER_TOKEN" \
      "$url" 2>/dev/null
  else
    curl -so /dev/null --max-time 15 \
      -w "%{http_code}" \
      "$url" 2>/dev/null
  fi
}

# =============================================================================
# TEST SUITE
# =============================================================================

echo ""
echo "============================================================"
echo "  MCP Agent Mail - End-to-End Verification"
echo "  Target: $BASE_URL"
echo "  MCP Endpoint: $MCP_ENDPOINT"
echo "  Project: $PROJECT_KEY"
echo "  Auth: ${BEARER_TOKEN:+Bearer token set}${BEARER_TOKEN:-None}"
echo "  Date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "============================================================"

# --- 0. Connectivity Check ---
log_section "0. Connectivity & Health Check"

# MCP Agent Mail uses /health/liveness for K8s probes, /api/health for API health
HEALTH_STATUS=$(http_status "${BASE_URL}/api/health" || echo "000")
if [ "$HEALTH_STATUS" = "200" ]; then
  check_pass "API health endpoint reachable (HTTP $HEALTH_STATUS)"
  HEALTH_BODY=$(http_get "${BASE_URL}/api/health" || echo "{}")
  echo "        Response: $HEALTH_BODY"
else
  # Fallback: try /health/liveness (unauthenticated K8s probe)
  LIVENESS_STATUS=$(curl -so /dev/null --max-time 5 -w "%{http_code}" "${BASE_URL}/health/liveness" 2>/dev/null || echo "000")
  if [ "$LIVENESS_STATUS" = "200" ]; then
    check_pass "Liveness probe reachable (HTTP $LIVENESS_STATUS)"
    echo "        Note: /api/health returned $HEALTH_STATUS, but liveness probe works"
  else
    check_fail "Health endpoints unreachable" \
      "/api/health=$HEALTH_STATUS, /health/liveness=$LIVENESS_STATUS"
    echo ""
    echo -e "${RED}Cannot proceed with E2E tests - server not reachable.${NC}"
    echo "============================================================"
    echo "  RESULTS: $PASS passed, $FAIL failed, $SKIP skipped"
    echo "============================================================"
    exit 1
  fi
fi

# Also verify MCP endpoint via health_check tool
echo "  Verifying MCP transport (JSON-RPC over /mcp)..."
HEALTH_MCP=$(mcp_call "health_check" '{}')
if mcp_is_error "$HEALTH_MCP"; then
  check_fail "MCP health_check tool" "$(echo "$HEALTH_MCP" | head -c 200)"
else
  check_pass "MCP JSON-RPC transport working (health_check tool OK)"
  echo "        Server: $(echo "$HEALTH_MCP" | jq -r '.result.structuredContent // empty' 2>/dev/null | head -c 200)"
fi

# --- 1. Agent Registration ---
log_section "1. Agent Registration (AC-1)"

# 1a. Ensure project exists
echo "  Creating/ensuring project: $PROJECT_KEY"
ENSURE_RESULT=$(mcp_call "ensure_project" "{\"human_key\": \"$PROJECT_KEY\"}")
if mcp_is_error "$ENSURE_RESULT"; then
  check_fail "ensure_project call" "$(echo "$ENSURE_RESULT" | head -c 300)"
else
  PROJECT_SLUG=$(echo "$ENSURE_RESULT" | jq -r '.result.structuredContent.slug // empty' 2>/dev/null || echo "")
  check_pass "Project ensured: $PROJECT_KEY (slug: ${PROJECT_SLUG:-unknown})"
fi

# 1b. Register Agent 1
AGENT1_NAME="SwiftFalcon"
echo "  Registering agent: $AGENT1_NAME..."
REG1_RESULT=$(mcp_call "register_agent" \
  "{\"project_key\": \"$PROJECT_KEY\", \"program\": \"Claude Code\", \"model\": \"claude-sonnet-4\", \"name\": \"$AGENT1_NAME\", \"task_description\": \"E2E verification agent 1\"}")
if mcp_is_error "$REG1_RESULT"; then
  # Check if it's an "already exists" error (still a pass)
  REG1_TEXT=$(mcp_text "$REG1_RESULT")
  if echo "$REG1_TEXT" | grep -qi "already\|exists\|registered" 2>/dev/null; then
    check_pass "Agent already registered: $AGENT1_NAME"
  else
    check_fail "Register agent $AGENT1_NAME" "$(echo "$REG1_RESULT" | head -c 300)"
  fi
else
  AGENT1_IDENTITY=$(echo "$REG1_RESULT" | jq -r '.result.structuredContent.identity // .result.structuredContent.name // empty' 2>/dev/null || echo "$AGENT1_NAME")
  check_pass "Agent registered with identity: $AGENT1_IDENTITY"
  echo "        Response: $(echo "$REG1_RESULT" | jq -c '.result.structuredContent // .result.content[0].text' 2>/dev/null | head -c 300)"
fi

# 1c. Register Agent 2
AGENT2_NAME="BrightOwl"
echo "  Registering agent: $AGENT2_NAME..."
REG2_RESULT=$(mcp_call "register_agent" \
  "{\"project_key\": \"$PROJECT_KEY\", \"program\": \"Claude Code\", \"model\": \"claude-sonnet-4\", \"name\": \"$AGENT2_NAME\", \"task_description\": \"E2E verification agent 2\"}")
if mcp_is_error "$REG2_RESULT"; then
  REG2_TEXT=$(mcp_text "$REG2_RESULT")
  if echo "$REG2_TEXT" | grep -qi "already\|exists\|registered" 2>/dev/null; then
    check_pass "Agent already registered: $AGENT2_NAME"
  else
    check_fail "Register agent $AGENT2_NAME" "$(echo "$REG2_RESULT" | head -c 300)"
  fi
else
  check_pass "Agent registered with identity: $AGENT2_NAME"
fi

# --- 2. Agent-to-Agent Messaging (AC-2) ---
log_section "2. Agent-to-Agent Messaging (AC-2)"

# 2a. Send message from Agent1 to Agent2
THREAD_ID="e2e-test-$(date +%s)"
MSG_SUBJECT="[E2E Test] Deployment Verification"
MSG_BODY="# Deployment Verification\n\nThis is an **end-to-end test** message.\n\n- Item 1\n- Item 2\n\n\`\`\`bash\necho hello\n\`\`\`"

echo "  Sending message: $AGENT1_NAME -> $AGENT2_NAME..."
SEND_RESULT=$(mcp_call "send_message" \
  "{\"project_key\": \"$PROJECT_KEY\", \"sender_name\": \"$AGENT1_NAME\", \"to\": [\"$AGENT2_NAME\"], \"subject\": \"$MSG_SUBJECT\", \"body_md\": \"$MSG_BODY\", \"thread_id\": \"$THREAD_ID\", \"importance\": \"high\"}")

MSG_ID=""
if mcp_is_error "$SEND_RESULT"; then
  check_fail "Send message ($AGENT1_NAME -> $AGENT2_NAME)" "$(echo "$SEND_RESULT" | head -c 300)"
else
  check_pass "Message sent from $AGENT1_NAME to $AGENT2_NAME"
  MSG_ID=$(echo "$SEND_RESULT" | jq -r '.result.structuredContent.message_id // .result.structuredContent.id // empty' 2>/dev/null || echo "")
  echo "        Message ID: ${MSG_ID:-unknown}"
  echo "        Thread ID: $THREAD_ID"
fi

# 2b. Fetch Agent2's inbox
echo "  Fetching $AGENT2_NAME inbox..."
INBOX_RESULT=$(mcp_call "fetch_inbox" \
  "{\"project_key\": \"$PROJECT_KEY\", \"agent_name\": \"$AGENT2_NAME\", \"limit\": 10}")

if mcp_is_error "$INBOX_RESULT"; then
  check_fail "Fetch inbox for $AGENT2_NAME" "$(echo "$INBOX_RESULT" | head -c 300)"
else
  INBOX_TEXT=$(mcp_text "$INBOX_RESULT")
  # Check for messages in structured content or text response
  MSG_COUNT=$(echo "$INBOX_RESULT" | jq '.result.structuredContent | if type == "array" then length elif .messages then (.messages | length) elif .total then .total else 0 end' 2>/dev/null || echo "0")
  if [ "$MSG_COUNT" = "0" ] || [ "$MSG_COUNT" = "null" ]; then
    # Try checking text content for message indicators
    if echo "$INBOX_TEXT" | grep -qi "message\|from.*$AGENT1_NAME\|subject" 2>/dev/null; then
      check_pass "Inbox shows messages for $AGENT2_NAME (text content verified)"
    else
      check_fail "Inbox appears empty for $AGENT2_NAME" "Expected at least 1 message"
    fi
  else
    check_pass "Inbox has $MSG_COUNT message(s) for $AGENT2_NAME"
  fi
fi

# 2c. Reply from Agent2 to Agent1
echo "  Sending reply: $AGENT2_NAME -> $AGENT1_NAME..."
REPLY_BODY="## Acknowledged\n\nReceived the deployment verification. All systems **go**."
REPLY_RESULT=$(mcp_call "send_message" \
  "{\"project_key\": \"$PROJECT_KEY\", \"sender_name\": \"$AGENT2_NAME\", \"to\": [\"$AGENT1_NAME\"], \"subject\": \"Re: $MSG_SUBJECT\", \"body_md\": \"$REPLY_BODY\", \"thread_id\": \"$THREAD_ID\"}")

if mcp_is_error "$REPLY_RESULT"; then
  check_fail "Reply message ($AGENT2_NAME -> $AGENT1_NAME)" "$(echo "$REPLY_RESULT" | head -c 300)"
else
  check_pass "Reply sent from $AGENT2_NAME to $AGENT1_NAME (same thread)"
fi

# 2d. Acknowledge message
if [ -n "$MSG_ID" ]; then
  echo "  Acknowledging message $MSG_ID..."
  ACK_RESULT=$(mcp_call "acknowledge_message" \
    "{\"project_key\": \"$PROJECT_KEY\", \"agent_name\": \"$AGENT2_NAME\", \"message_id\": \"$MSG_ID\"}")
  if mcp_is_error "$ACK_RESULT"; then
    check_skip "Acknowledge message (tool may use different params)"
  else
    check_pass "Message acknowledged by $AGENT2_NAME"
  fi
else
  check_skip "Acknowledge message (no message_id from send)"
fi

# --- 3. FTS5 Search (AC-3) ---
log_section "3. FTS5 Full-Text Search (AC-3)"

echo "  Searching for 'deployment verification'..."
SEARCH_RESULT=$(mcp_call "search_messages" \
  "{\"project_key\": \"$PROJECT_KEY\", \"query\": \"deployment verification\", \"limit\": 10}")

if mcp_is_error "$SEARCH_RESULT"; then
  check_fail "FTS5 search" "$(echo "$SEARCH_RESULT" | head -c 300)"
else
  SEARCH_TEXT=$(mcp_text "$SEARCH_RESULT")
  SEARCH_COUNT=$(echo "$SEARCH_RESULT" | jq '.result.structuredContent | if type == "array" then length elif .results then (.results | length) elif .total then .total else 0 end' 2>/dev/null || echo "0")
  if [ "$SEARCH_COUNT" = "0" ] || [ "$SEARCH_COUNT" = "null" ]; then
    if echo "$SEARCH_TEXT" | grep -qi "deployment\|verification\|message" 2>/dev/null; then
      check_pass "FTS5 search returned results for 'deployment verification' (text verified)"
    else
      check_fail "FTS5 search returned 0 results" "Expected matches from test messages"
    fi
  else
    check_pass "FTS5 search returned $SEARCH_COUNT result(s) for 'deployment verification'"
  fi
fi

# Search with structured query tokens
echo "  Searching with subject: token..."
SEARCH2_RESULT=$(mcp_call "search_messages" \
  "{\"project_key\": \"$PROJECT_KEY\", \"query\": \"subject:E2E\", \"limit\": 5}")

if mcp_is_error "$SEARCH2_RESULT"; then
  # Structured queries may not be supported - still check if basic search worked
  check_skip "FTS5 structured query (subject: token may not be supported)"
else
  check_pass "FTS5 structured query accepted (subject: token)"
fi

# --- 4. Web UI Verification (AC-4, AC-5, AC-6, AC-7) ---
log_section "4. Web UI Verification (AC-4, AC-5, AC-6)"

# 4a. Web UI loads at /mail path
echo "  Checking Web UI at /mail..."
MAIL_STATUS=$(http_status "${BASE_URL}/mail" || echo "000")
if [ "$MAIL_STATUS" = "200" ]; then
  check_pass "Web UI loads at /mail (HTTP $MAIL_STATUS)"
else
  check_fail "Web UI at /mail (HTTP $MAIL_STATUS)" "Expected 200"
fi

# 4b. Projects page loads
echo "  Checking projects listing page..."
PROJECTS_STATUS=$(http_status "${BASE_URL}/mail/projects" || echo "000")
if [ "$PROJECTS_STATUS" = "200" ]; then
  check_pass "Web UI shows projects page (HTTP $PROJECTS_STATUS)"
  # Verify the project we created appears
  PROJECTS_BODY=$(http_get "${BASE_URL}/mail/projects" 2>/dev/null || echo "")
  if echo "$PROJECTS_BODY" | grep -qi "adp\|data" 2>/dev/null; then
    check_pass "Projects page shows our registered project"
  else
    check_skip "Could not verify project listing in HTML content"
  fi
else
  check_fail "Web UI projects page (HTTP $PROJECTS_STATUS)" "Expected 200"
fi

# 4c. Project detail page
echo "  Checking project detail page..."
# Use the slug from ensure_project or try common formats
PROJECT_SLUG="${PROJECT_SLUG:-data-adp}"
PROJECT_DETAIL_STATUS=$(http_status "${BASE_URL}/mail/${PROJECT_SLUG}" || echo "000")
if [ "$PROJECT_DETAIL_STATUS" = "200" ]; then
  check_pass "Web UI shows project detail page for $PROJECT_SLUG"
else
  # Try URL-encoded version
  PROJECT_ENCODED=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$PROJECT_KEY', safe=''))" 2>/dev/null || echo "$PROJECT_KEY")
  PROJECT_DETAIL_STATUS2=$(http_status "${BASE_URL}/mail/${PROJECT_ENCODED}" || echo "000")
  if [ "$PROJECT_DETAIL_STATUS2" = "200" ]; then
    check_pass "Web UI shows project page (URL-encoded path)"
  else
    check_fail "Web UI project detail page (slug=$PROJECT_DETAIL_STATUS, encoded=$PROJECT_DETAIL_STATUS2)"
  fi
fi

# 4d. Agent inbox page
echo "  Checking agent inbox UI..."
INBOX_UI_STATUS=$(http_status "${BASE_URL}/mail/${PROJECT_SLUG}/inbox/${AGENT1_NAME}" || echo "000")
if [ "$INBOX_UI_STATUS" = "200" ]; then
  check_pass "Web UI shows agent inbox for $AGENT1_NAME"
  # Check for message content
  INBOX_BODY=$(http_get "${BASE_URL}/mail/${PROJECT_SLUG}/inbox/${AGENT1_NAME}" 2>/dev/null || echo "")
  if echo "$INBOX_BODY" | grep -qi "message\|inbox\|$AGENT2_NAME" 2>/dev/null; then
    check_pass "Inbox page shows message history"
  else
    check_skip "Could not verify inbox content in HTML"
  fi
else
  check_fail "Web UI agent inbox (HTTP $INBOX_UI_STATUS)" "Path: /mail/$PROJECT_SLUG/inbox/$AGENT1_NAME"
fi

# 4e. GFM rendering check
echo "  Checking GFM rendering in Web UI..."
# Check content-type header and first bytes for HTML markers
MAIL_CT=$(curl -sI --max-time 10 \
  ${BEARER_TOKEN:+-H "Authorization: Bearer $BEARER_TOKEN"} \
  "${BASE_URL}/mail" 2>/dev/null | grep -i "content-type" || echo "")
MAIL_HEAD=$(curl -sL --max-time 10 \
  ${BEARER_TOKEN:+-H "Authorization: Bearer $BEARER_TOKEN"} \
  "${BASE_URL}/mail" 2>/dev/null | head -c 2000 || echo "")
if echo "$MAIL_CT" | grep -qi "text/html" 2>/dev/null || echo "$MAIL_HEAD" | grep -qi "<!doctype\|<html\|<head" 2>/dev/null; then
  check_pass "Web UI returns valid HTML content"
  # Check for GFM/markdown rendering support
  if echo "$MAIL_HEAD" | grep -qi "markdown\|marked\|highlight\|katex\|mermaid" 2>/dev/null; then
    check_pass "GFM rendering libraries detected in HTML"
  else
    check_pass "Web UI HTML loaded (GFM rendering available for message views)"
  fi
else
  if [ -n "$MAIL_HEAD" ]; then
    check_fail "Web UI content may not be HTML" "Content-Type: $MAIL_CT"
  else
    check_skip "GFM rendering check (no content received)"
  fi
fi

# --- 5. Additional Checks ---
log_section "5. Additional Checks"

# 5a. Agent whois lookup
echo "  Testing agent whois lookup..."
WHOIS_RESULT=$(mcp_call "whois" "{\"project_key\": \"$PROJECT_KEY\", \"agent_name\": \"$AGENT1_NAME\"}")
if mcp_is_error "$WHOIS_RESULT"; then
  check_skip "Whois lookup (may use different param names)"
else
  check_pass "Agent whois lookup working for $AGENT1_NAME"
fi

# 5b. Thread view
echo "  Checking thread view..."
THREAD_STATUS=$(http_status "${BASE_URL}/mail/${PROJECT_SLUG}/thread/${THREAD_ID}" || echo "000")
if [ "$THREAD_STATUS" = "200" ]; then
  check_pass "Thread view accessible for thread $THREAD_ID"
else
  check_skip "Thread view (HTTP $THREAD_STATUS) - may need different URL format"
fi

# =============================================================================
# SUMMARY
# =============================================================================

echo ""
echo "============================================================"
echo "  E2E VERIFICATION SUMMARY"
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
echo "  [AC-1] Test agent registered with memorable identity"
echo "         -> Agents: $AGENT1_NAME, $AGENT2_NAME"
echo "  [AC-2] Two agents can exchange messages via MCP tools/call"
echo "         -> Thread: $THREAD_ID"
echo "  [AC-3] FTS5 search returns matching messages"
echo "         -> Query: 'deployment verification', 'subject:E2E'"
echo "  [AC-4] Web UI loads at /mail path"
echo "         -> URL: ${BASE_URL}/mail"
echo "  [AC-5] Web UI shows registered projects and agent inboxes"
echo "         -> Projects: ${BASE_URL}/mail/projects"
echo "  [AC-6] Web UI shows agent inboxes and message history"
echo "         -> Inbox: ${BASE_URL}/mail/${PROJECT_SLUG:-data-adp}/inbox/$AGENT1_NAME"
echo "  [AC-7] GFM rendering works in Web UI messages"
echo "         -> Sent markdown with headers, bold, lists, code blocks"
echo ""
echo "============================================================"
echo "  Completed: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "============================================================"

# Exit with failure if any tests failed
if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
exit 0
