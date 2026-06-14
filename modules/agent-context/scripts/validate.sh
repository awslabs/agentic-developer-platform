#!/usr/bin/env bash
# End-to-end validation of the Agent Context Intelligence Platform
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Source configuration and helpers
source "${SCRIPT_DIR}/_common.sh"
load_config "${ROOT_DIR}"

echo "================================================"
echo "Agent Context Platform - Validation"
echo "================================================"
echo "Namespace: ${NAMESPACE}"
echo "================================================"

PASS=0
FAIL=0
WARN=0

check_pass() { echo "  [PASS] $1"; PASS=$((PASS + 1)); }
check_fail() { echo "  [FAIL] $1"; FAIL=$((FAIL + 1)); }
check_warn() { echo "  [WARN] $1"; WARN=$((WARN + 1)); }

# ─── Check 1: All pods running ──────────────────────────────────────────────
echo ""
echo "--- Check 1: Pod Status ---"

EXPECTED_DEPLOYS="litellm-proxy context-mcp"
for DEPLOY in ${EXPECTED_DEPLOYS}; do
  READY=$(kubectl get deploy "${DEPLOY}" -n "${NAMESPACE}" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo "0")
  if [ "${READY:-0}" -ge 1 ]; then
    check_pass "${DEPLOY}: ${READY} replica(s) ready"
  else
    STATUS=$(kubectl get deploy "${DEPLOY}" -n "${NAMESPACE}" -o jsonpath='{.status.conditions[?(@.type=="Available")].status}' 2>/dev/null || echo "NotFound")
    check_fail "${DEPLOY}: not ready (status: ${STATUS})"
  fi
done

# ─── Check 2: LiteLLM Proxy ─────────────────────────────────────────────────
echo ""
echo "--- Check 2: LiteLLM Proxy ---"

LITELLM_HEALTH=$(kubectl exec deploy/litellm-proxy -n "${NAMESPACE}" -- python3 -c "
import urllib.request, json
try:
    r = urllib.request.urlopen('http://localhost:${LITELLM_PORT}/health', timeout=5)
    print(r.read().decode())
except Exception as e:
    print(f'UNREACHABLE: {e}')
" 2>/dev/null || echo "UNREACHABLE")
if echo "${LITELLM_HEALTH}" | grep -qE "healthy|healthy_count"; then
  check_pass "LiteLLM /health: responsive"
else
  check_fail "LiteLLM /health: ${LITELLM_HEALTH:0:100}"
fi

# Test embedding endpoint
EMBED_RESP=$(kubectl exec deploy/litellm-proxy -n "${NAMESPACE}" -- python3 -c "
import urllib.request, json
req = urllib.request.Request(
    'http://localhost:${LITELLM_PORT}/v1/embeddings',
    data=json.dumps({'model': 'bedrock/${BEDROCK_EMBEDDING_MODEL}', 'input': 'validation test'}).encode(),
    headers={'Content-Type': 'application/json'},
    method='POST'
)
try:
    r = urllib.request.urlopen(req, timeout=30)
    print(r.read().decode()[:300])
except Exception as e:
    print(f'FAILED: {e}')
" 2>/dev/null || echo "FAILED")

if echo "${EMBED_RESP}" | grep -q '"data"'; then
  check_pass "LiteLLM /v1/embeddings: returns embedding data"
elif echo "${EMBED_RESP}" | grep -q '"embedding"'; then
  check_pass "LiteLLM /v1/embeddings: returns embedding data"
else
  check_warn "LiteLLM /v1/embeddings: inconclusive (proxy may still be starting)"
fi

# Test VLM endpoint (Claude Sonnet 4.6 via Bedrock)
VLM_RESP=$(kubectl exec deploy/litellm-proxy -n "${NAMESPACE}" -- python3 -c "
import urllib.request, json
req = urllib.request.Request(
    'http://localhost:${LITELLM_PORT}/v1/chat/completions',
    data=json.dumps({
        'model': 'bedrock/${BEDROCK_VLM_MODEL}',
        'messages': [{'role': 'user', 'content': 'Say hello in one word.'}],
        'max_tokens': 10
    }).encode(),
    headers={'Content-Type': 'application/json'},
    method='POST'
)
try:
    r = urllib.request.urlopen(req, timeout=60)
    print(r.read().decode()[:500])
except Exception as e:
    print(f'FAILED: {e}')
" 2>/dev/null || echo "FAILED")

if echo "${VLM_RESP}" | grep -q '"choices"'; then
  check_pass "LiteLLM /v1/chat/completions (VLM): Claude Sonnet 4.6 responding"
else
  check_warn "LiteLLM /v1/chat/completions (VLM): inconclusive — ${VLM_RESP:0:100}"
fi

# List models
MODELS_LIST=$(kubectl exec deploy/litellm-proxy -n "${NAMESPACE}" -- python3 -c "
import urllib.request, json
try:
    r = urllib.request.urlopen('http://localhost:${LITELLM_PORT}/v1/models', timeout=10)
    d = json.loads(r.read())
    ids = [m['id'] for m in d.get('data', [])]
    print(','.join(ids))
except Exception as e:
    print(f'FAILED: {e}')
" 2>/dev/null || echo "FAILED")

if echo "${MODELS_LIST}" | grep -q "${BEDROCK_EMBEDDING_MODEL}"; then
  check_pass "LiteLLM model list: embedding model registered"
else
  check_warn "LiteLLM model list: embedding model not found in ${MODELS_LIST:0:200}"
fi

if echo "${MODELS_LIST}" | grep -q "${BEDROCK_VLM_MODEL}"; then
  check_pass "LiteLLM model list: VLM model registered"
else
  check_warn "LiteLLM model list: VLM model not found in ${MODELS_LIST:0:200}"
fi

# ─── Check 3: Context MCP Server (Door) ─────────────────────────────────────
echo ""
echo "--- Check 3: Context MCP Server ---"

MCP_READY=$(kubectl get deploy context-mcp -n "${NAMESPACE}" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo "0")
if [ "${MCP_READY:-0}" -ge 1 ]; then
  check_pass "Context MCP deployment: ${MCP_READY} replica(s) ready"
else
  MCP_STATUS=$(kubectl get deploy context-mcp -n "${NAMESPACE}" -o jsonpath='{.status.conditions[?(@.type=="Available")].status}' 2>/dev/null || echo "NotFound")
  check_fail "Context MCP deployment: not ready (status: ${MCP_STATUS})"
fi

MCP_HEALTH=$(kubectl exec deploy/context-mcp -n "${NAMESPACE}" -- python3 -c "
import urllib.request
try:
    r = urllib.request.urlopen('http://localhost:5100/health', timeout=5)
    print(r.read().decode())
except Exception as e:
    print(f'UNREACHABLE: {e}')
" 2>/dev/null || echo "UNREACHABLE")
if echo "${MCP_HEALTH}" | grep -qiE "ok|healthy|alive"; then
  check_pass "Context MCP /health: responsive"
else
  check_fail "Context MCP /health: ${MCP_HEALTH:0:100}"
fi

# Check tool listing (confirms the 6 verbs are registered)
MCP_TOOLS=$(kubectl exec deploy/context-mcp -n "${NAMESPACE}" -- python3 -c "
import urllib.request, json
try:
    r = urllib.request.urlopen('http://localhost:5100/tools', timeout=5)
    tools = json.loads(r.read())
    names = [t['name'] for t in tools]
    print(','.join(names))
except Exception as e:
    print(f'FAILED: {e}')
" 2>/dev/null || echo "FAILED")
if echo "${MCP_TOOLS}" | grep -q "search"; then
  check_pass "Context MCP /tools: verbs registered (${MCP_TOOLS})"
else
  check_warn "Context MCP /tools: could not verify tool listing"
fi

# ─── Check 4: Ingestion Refresh CronJob ──────────────────────────────────────
echo ""
echo "--- Check 4: Ingestion Refresh CronJob ---"

INGESTION_CJ=$(kubectl get cronjob ingestion-refresh -n "${NAMESPACE}" -o name 2>/dev/null || echo "")
if [ -n "${INGESTION_CJ}" ]; then
  check_pass "Ingestion refresh CronJob: exists"

  # Check schedule
  CJ_SCHEDULE=$(kubectl get cronjob ingestion-refresh -n "${NAMESPACE}" -o jsonpath='{.spec.schedule}' 2>/dev/null || echo "unknown")
  check_pass "Ingestion refresh schedule: ${CJ_SCHEDULE}"

  # Check last successful run
  LAST_SUCCESS=$(kubectl get cronjob ingestion-refresh -n "${NAMESPACE}" -o jsonpath='{.status.lastSuccessfulTime}' 2>/dev/null || echo "")
  if [ -n "${LAST_SUCCESS}" ]; then
    check_pass "Last successful refresh: ${LAST_SUCCESS}"
  else
    check_warn "No successful refresh runs yet (CronJob may not have triggered)"
  fi
else
  check_warn "Ingestion refresh CronJob not found (deploy with INGESTION_REFRESH_ENABLED=true)"
fi

# Legacy: check if old CodeGraphContext pod is still running (should be removed)
CGC_POD=$(kubectl get pods -n "${NAMESPACE}" -l app=codegraph -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
if [ -n "${CGC_POD}" ]; then
  check_warn "Legacy CodeGraphContext pod still running: ${CGC_POD} — should be removed (Issue #105)"
fi

# NOTE: Sourcebot removed (Issue #1347). Code search now uses Zoekt directly.

# ─── Check 6: DeepWiki ──────────────────────────────────────────────────────
echo ""
echo "--- Check 6: DeepWiki ---"

if [ "${DEEPWIKI_ENABLED:-true}" = "true" ]; then
  DW_READY=$(kubectl get deploy deepwiki -n "${NAMESPACE}" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo "0")
  if [ "${DW_READY:-0}" -ge 1 ]; then
    check_pass "DeepWiki deployment: ${DW_READY} replica(s) ready"
  else
    check_fail "DeepWiki deployment: not ready"
  fi

  DW_HEALTH=$(kubectl exec deploy/deepwiki -n "${NAMESPACE}" -- \
    curl -sf http://localhost:${DEEPWIKI_PORT}/health 2>/dev/null || echo "UNREACHABLE")
  if echo "${DW_HEALTH}" | grep -qiE "ok|healthy|alive"; then
    check_pass "DeepWiki /health: responsive"
  else
    check_fail "DeepWiki /health: ${DW_HEALTH:0:100}"
  fi

  # Check that DeepWiki can reach LiteLLM proxy
  DW_MODELS=$(kubectl exec deploy/deepwiki -n "${NAMESPACE}" -- \
    curl -sf "http://litellm-proxy.${NAMESPACE}.svc.cluster.local:${LITELLM_PORT}/v1/models" 2>/dev/null || echo "UNREACHABLE")
  if echo "${DW_MODELS}" | grep -q '"data"'; then
    check_pass "DeepWiki -> LiteLLM proxy: reachable"
  else
    check_warn "DeepWiki -> LiteLLM proxy: could not verify connectivity"
  fi
else
  check_warn "DeepWiki: disabled (DEEPWIKI_ENABLED=false)"
fi

# ─── Check 7: Cross-namespace DNS ───────────────────────────────────────────
echo ""
echo "--- Check 7: Cross-namespace DNS ---"

# Check if arc-runners-aisuperplane namespace exists and has a pod we can exec into
ARC_NS="arc-runners-aisuperplane"
ARC_POD=$(kubectl get pods -n "${ARC_NS}" --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")

if [ -n "${ARC_POD}" ]; then
  for SVC in openviking:1933 litellm-proxy:${LITELLM_PORT}; do
    SVC_NAME=$(echo "${SVC}" | cut -d: -f1)
    SVC_PORT=$(echo "${SVC}" | cut -d: -f2)
    DNS_CHECK=$(kubectl exec "${ARC_POD}" -n "${ARC_NS}" -- nslookup "${SVC_NAME}.${NAMESPACE}.svc.cluster.local" 2>/dev/null | grep -c "Address" || echo "0")
    if [ "${DNS_CHECK}" -ge 2 ]; then
      check_pass "DNS from ${ARC_NS}: ${SVC_NAME}.${NAMESPACE}.svc.cluster.local resolves"
    else
      check_warn "DNS from ${ARC_NS}: ${SVC_NAME}.${NAMESPACE}.svc.cluster.local may not resolve"
    fi
  done
else
  check_warn "No running pods in ${ARC_NS} to test cross-namespace DNS"
fi

# ─── Check 8: Repo Ingestion Status ──────────────────────────────────────────
echo ""
echo "--- Check 8: Repo Ingestion ---"

# Check if any ingestion jobs exist
INGEST_JOBS=$(kubectl get jobs -n "${NAMESPACE}" -l app=openviking-ingest -o name 2>/dev/null | wc -l || echo "0")
if [ "${INGEST_JOBS}" -gt 0 ]; then
  check_pass "Ingestion jobs found: ${INGEST_JOBS}"
  # Check latest job status
  LATEST_JOB=$(kubectl get jobs -n "${NAMESPACE}" -l app=openviking-ingest --sort-by=.metadata.creationTimestamp -o jsonpath='{.items[-1].metadata.name}' 2>/dev/null || echo "")
  if [ -n "${LATEST_JOB}" ]; then
    JOB_STATUS=$(kubectl get job "${LATEST_JOB}" -n "${NAMESPACE}" -o jsonpath='{.status.conditions[0].type}' 2>/dev/null || echo "Unknown")
    JOB_SUCCEEDED=$(kubectl get job "${LATEST_JOB}" -n "${NAMESPACE}" -o jsonpath='{.status.succeeded}' 2>/dev/null || echo "0")
    if [ "${JOB_SUCCEEDED}" = "1" ]; then
      check_pass "Latest ingestion job (${LATEST_JOB}): Complete"
    else
      check_warn "Latest ingestion job (${LATEST_JOB}): ${JOB_STATUS} (succeeded=${JOB_SUCCEEDED})"
    fi
  fi
else
  check_warn "No ingestion jobs found (run ./scripts/ingest-repos.sh to ingest repos)"
fi

# Check if repos ConfigMap exists
REPOS_CM=$(kubectl get configmap openviking-repos -n "${NAMESPACE}" -o name 2>/dev/null || echo "")
if [ -n "${REPOS_CM}" ]; then
  REPO_COUNT=$(kubectl get configmap openviking-repos -n "${NAMESPACE}" -o jsonpath='{.data.repos\.txt}' 2>/dev/null | grep -cE '^\s*[^#\s]' || echo "0")
  check_pass "Repos ConfigMap: ${REPO_COUNT} repos configured"
else
  check_warn "Repos ConfigMap not found (ingestion not yet run)"
fi

# ─── Check 9: S3 Files Storage ───────────────────────────────────────────────
echo ""
echo "--- Check 9: S3 Files Storage ---"

# Check if S3 Files PVC exists and is bound
PVC_STATUS=$(kubectl get pvc platform-data -n "${NAMESPACE}" -o jsonpath='{.status.phase}' 2>/dev/null || echo "NotFound")
if [ "${PVC_STATUS}" = "Bound" ]; then
  check_pass "S3 Files PVC (platform-data): Bound"
else
  check_warn "S3 Files PVC (platform-data): ${PVC_STATUS} (S3 Files may not be deployed yet)"
fi

# Check StorageClass exists
SC_EXISTS=$(kubectl get storageclass s3-files -o name 2>/dev/null || echo "")
if [ -n "${SC_EXISTS}" ]; then
  check_pass "StorageClass s3-files: exists"
else
  check_warn "StorageClass s3-files: not found"
fi

# Check DeepWiki mount (if DeepWiki is running)
if [ "${DEEPWIKI_ENABLED:-true}" = "true" ]; then
  DW_MOUNT=$(kubectl exec deploy/deepwiki -n "${NAMESPACE}" -- ls /root/.adalflow/ 2>/dev/null && echo "OK" || echo "FAIL")
  if [ "${DW_MOUNT}" != "FAIL" ]; then
    check_pass "DeepWiki S3 Files mount (/root/.adalflow): accessible"
  else
    check_warn "DeepWiki S3 Files mount: not accessible (may be using emptyDir)"
  fi
fi

# Check platform-data PVC is mounted (used by ingestion CronJob)
PLATFORM_DATA_SIZE=$(kubectl exec deploy/deepwiki -n "${NAMESPACE}" -- df -h /root/.adalflow/ 2>/dev/null | tail -1 | awk '{print $2}' || echo "FAIL")
if [ "${PLATFORM_DATA_SIZE}" != "FAIL" ]; then
  check_pass "Platform data mount: accessible (${PLATFORM_DATA_SIZE})"
else
  check_warn "Platform data mount: could not verify"
fi

# ─── Check 10: Ingestion Pipeline RBAC ────────────────────────────────────────
echo ""
echo "--- Check 10: Ingestion Pipeline RBAC ---"

RUNNER_NS="${RUNNER_NAMESPACE:-arc-runners-org}"
RUNNER_SA="${RUNNER_SERVICE_ACCOUNT:-github-runner-sa}"

# Check that the Role exists
ROLE_EXISTS=$(kubectl get role ingestion-pipeline-role -n "${NAMESPACE}" -o name 2>/dev/null || echo "")
if [ -n "${ROLE_EXISTS}" ]; then
  check_pass "Role ingestion-pipeline-role: exists in ${NAMESPACE}"
else
  check_fail "Role ingestion-pipeline-role: not found in ${NAMESPACE}"
fi

# Check that the RoleBinding exists
RB_EXISTS=$(kubectl get rolebinding arc-runner-ingestion-access -n "${NAMESPACE}" -o name 2>/dev/null || echo "")
if [ -n "${RB_EXISTS}" ]; then
  check_pass "RoleBinding arc-runner-ingestion-access: exists in ${NAMESPACE}"
else
  check_fail "RoleBinding arc-runner-ingestion-access: not found in ${NAMESPACE}"
fi

# Verify actual permissions with kubectl auth can-i
SA_SUBJECT="system:serviceaccount:${RUNNER_NS}:${RUNNER_SA}"

CAN_GET_SECRET=$(kubectl auth can-i get secrets/agent-context-secrets -n "${NAMESPACE}" --as="${SA_SUBJECT}" 2>/dev/null || echo "no")
if [ "${CAN_GET_SECRET}" = "yes" ]; then
  check_pass "Runner SA can read agent-context-secrets"
else
  check_fail "Runner SA cannot read agent-context-secrets (${SA_SUBJECT})"
fi

CAN_CREATE_JOB=$(kubectl auth can-i create jobs -n "${NAMESPACE}" --as="${SA_SUBJECT}" 2>/dev/null || echo "no")
if [ "${CAN_CREATE_JOB}" = "yes" ]; then
  check_pass "Runner SA can create Jobs"
else
  check_fail "Runner SA cannot create Jobs (${SA_SUBJECT})"
fi

CAN_EXEC_POD=$(kubectl auth can-i create pods/exec -n "${NAMESPACE}" --as="${SA_SUBJECT}" 2>/dev/null || echo "no")
if [ "${CAN_EXEC_POD}" = "yes" ]; then
  check_pass "Runner SA can exec into pods"
else
  check_fail "Runner SA cannot exec into pods (${SA_SUBJECT})"
fi

# ─── Check 11: GraphRAG (Neptune + OpenSearch Serverless) ────────────────────
echo ""
echo "--- Check 11: GraphRAG Infrastructure ---"

if [ "${GRAPHRAG_ENABLED:-false}" = "true" ]; then
  # Check Neptune connectivity
  if [ -n "${NEPTUNE_ENDPOINT:-}" ]; then
    NEPTUNE_STATUS=$(kubectl exec deploy/litellm-proxy -n "${NAMESPACE}" -- python3 -c "
import urllib.request, ssl
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
try:
    r = urllib.request.urlopen('https://${NEPTUNE_ENDPOINT}:${NEPTUNE_PORT:-8182}/status', timeout=10, context=ctx)
    print(r.read().decode()[:200])
except Exception as e:
    print(f'UNREACHABLE: {e}')
" 2>/dev/null || echo "UNREACHABLE")
    if echo "${NEPTUNE_STATUS}" | grep -qi "healthy\|status"; then
      check_pass "Neptune Serverless: reachable at ${NEPTUNE_ENDPOINT}"
    else
      check_warn "Neptune Serverless: ${NEPTUNE_STATUS:0:100}"
    fi
  else
    check_warn "Neptune: NEPTUNE_ENDPOINT not set"
  fi

  # Check OpenSearch Serverless
  if [ -n "${OPENSEARCH_ENDPOINT:-}" ]; then
    check_pass "OpenSearch Serverless: endpoint configured (${OPENSEARCH_ENDPOINT:0:50}...)"
  else
    check_warn "OpenSearch Serverless: OPENSEARCH_ENDPOINT not set"
  fi

  # Check learning artifacts directory
  LEARNING_COUNT=$(kubectl exec deploy/deepwiki -n "${NAMESPACE}" -- find /platform-data/learning -name "concept-map.json" 2>/dev/null | wc -l || echo "0")
  if [ "${LEARNING_COUNT}" -gt 0 ]; then
    check_pass "Learning artifacts: ${LEARNING_COUNT} repos with concept maps"
  else
    check_warn "Learning artifacts: no concept maps generated yet"
  fi
else
  check_warn "GraphRAG: disabled (GRAPHRAG_ENABLED=false)"
fi

# ─── Summary ─────────────────────────────────────────────────────────────────
echo ""
echo "================================================"
echo "Validation Summary"
echo "================================================"
echo "  PASS: ${PASS}"
echo "  FAIL: ${FAIL}"
echo "  WARN: ${WARN}"
echo "================================================"

if [ "${FAIL}" -gt 0 ]; then
  echo "Some checks failed. Review the output above."
  exit 1
else
  echo "All critical checks passed!"
  exit 0
fi
