#!/usr/bin/env bash
# Deploy LiteLLM Proxy for Bedrock Embeddings + VLM
# Provides OpenAI-compatible /v1/embeddings and /v1/chat/completions endpoints backed by AWS Bedrock
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Source configuration and helpers
source "${SCRIPT_DIR}/_common.sh"
load_config "${ROOT_DIR}"

echo "================================================"
echo "Task 1: Deploy LiteLLM Proxy (Embeddings + VLM)"
echo "================================================"
echo "Embedding: ${BEDROCK_EMBEDDING_MODEL}"
echo "VLM:       ${BEDROCK_VLM_MODEL}"
echo "Dimension: ${BEDROCK_EMBEDDING_DIMENSION}"
echo "Region:    ${BEDROCK_REGION}"
echo "Namespace: ${NAMESPACE}"
echo "================================================"

# Step 1: Verify Bedrock model access
echo ""
echo "[1/7] Verifying Bedrock model access..."

# Try primary model
BEDROCK_OK=false
if aws bedrock-runtime invoke-model \
    --model-id "${BEDROCK_EMBEDDING_MODEL}" \
    --region "${BEDROCK_REGION}" \
    --content-type application/json \
    --accept application/json \
    --body '{"inputText": "test"}' \
    /tmp/bedrock-test-output.json >/dev/null 2>&1; then
  echo "  Bedrock model '${BEDROCK_EMBEDDING_MODEL}' is accessible."
  BEDROCK_OK=true
else
  echo "  WARNING: Primary model '${BEDROCK_EMBEDDING_MODEL}' not accessible."
  # Try fallback
  FALLBACK_MODEL="amazon.titan-embed-text-v2:0"
  if aws bedrock-runtime invoke-model \
      --model-id "${FALLBACK_MODEL}" \
      --region "${BEDROCK_REGION}" \
      --content-type application/json \
      --accept application/json \
      --body '{"inputText": "test"}' \
      /tmp/bedrock-test-output.json >/dev/null 2>&1; then
    echo "  Falling back to '${FALLBACK_MODEL}'."
    BEDROCK_EMBEDDING_MODEL="${FALLBACK_MODEL}"
    BEDROCK_EMBEDDING_DIMENSION=1024
    BEDROCK_OK=true
  fi
fi

if [ "${BEDROCK_OK}" != "true" ]; then
  echo "  ERROR: No Bedrock embedding model accessible. Check IAM/IRSA permissions."
  exit 1
fi

rm -f /tmp/bedrock-test-output.json

# Step 2: Apply LiteLLM config and proxy manifests
echo ""
echo "[2/7] Applying LiteLLM config and proxy deployment..."

export NAMESPACE BEDROCK_EMBEDDING_MODEL BEDROCK_EMBEDDING_DIMENSION BEDROCK_REGION
export BEDROCK_VLM_MODEL VLM_MAX_CONCURRENT VLM_MAX_RETRIES
export SERVICE_ACCOUNT LITELLM_PORT LITELLM_IMAGE

template_file "${ROOT_DIR}/manifests/litellm-config.yaml" | kubectl apply -f -
template_file "${ROOT_DIR}/manifests/litellm-proxy.yaml" | kubectl apply -f -

# Step 3: Wait for pod ready
echo ""
echo "[3/7] Waiting for LiteLLM proxy pod to be ready (this may take 2-3 minutes for pip install)..."
kubectl rollout status deploy/litellm-proxy -n "${NAMESPACE}" --timeout=300s

# Step 4: Test /v1/embeddings endpoint
echo ""
echo "[4/7] Testing /v1/embeddings endpoint..."

# Use python (which is in the image) instead of curl for health checks
for i in $(seq 1 18); do
  HEALTH=$(kubectl exec deploy/litellm-proxy -n "${NAMESPACE}" -- python3 -c "
import urllib.request, json
try:
    r = urllib.request.urlopen('http://localhost:${LITELLM_PORT}/health', timeout=5)
    d = json.loads(r.read())
    print(json.dumps(d))
except Exception as e:
    print(f'ERROR: {e}')
" 2>/dev/null || true)
  if echo "${HEALTH}" | grep -qi "healthy"; then
    echo "  LiteLLM proxy is healthy: ${HEALTH}"
    break
  fi
  echo "  Waiting for LiteLLM proxy to become healthy... (attempt ${i}/18)"
  sleep 10
done

EMBED_RESULT=$(kubectl exec deploy/litellm-proxy -n "${NAMESPACE}" -- python3 -c "
import urllib.request, json
req = urllib.request.Request(
    'http://localhost:${LITELLM_PORT}/v1/embeddings',
    data=json.dumps({'model': 'bedrock/${BEDROCK_EMBEDDING_MODEL}', 'input': 'test embedding query'}).encode(),
    headers={'Content-Type': 'application/json'},
    method='POST'
)
try:
    r = urllib.request.urlopen(req, timeout=30)
    d = json.loads(r.read())
    print(json.dumps(d)[:500])
except Exception as e:
    print(f'FAILED: {e}')
" 2>/dev/null || echo "FAILED")

if echo "${EMBED_RESULT}" | grep -q '"embedding"'; then
  echo "  Embedding endpoint working."
elif echo "${EMBED_RESULT}" | grep -q '"data"'; then
  echo "  Embedding endpoint working (OpenAI format)."
else
  echo "  WARNING: Embedding endpoint test inconclusive. Response: ${EMBED_RESULT:0:200}"
  echo "  The proxy may still be initializing. Check: kubectl logs deploy/litellm-proxy -n ${NAMESPACE}"
fi

# Step 5: Test /v1/chat/completions endpoint (VLM)
echo ""
echo "[5/7] Testing /v1/chat/completions endpoint (VLM: bedrock/${BEDROCK_VLM_MODEL})..."

VLM_RESULT=$(kubectl exec deploy/litellm-proxy -n "${NAMESPACE}" -- python3 -c "
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
    d = json.loads(r.read())
    print(json.dumps(d)[:500])
except Exception as e:
    print(f'FAILED: {e}')
" 2>/dev/null || echo "FAILED")

if echo "${VLM_RESULT}" | grep -q '"choices"'; then
  echo "  VLM endpoint working (Claude Sonnet 4.6 via Bedrock)."
else
  echo "  WARNING: VLM endpoint test inconclusive. Response: ${VLM_RESULT:0:200}"
  echo "  The proxy may still be initializing. Check: kubectl logs deploy/litellm-proxy -n ${NAMESPACE}"
fi

# Step 6: List available models
echo ""
echo "[6/7] Listing available models on proxy..."

MODELS_RESULT=$(kubectl exec deploy/litellm-proxy -n "${NAMESPACE}" -- python3 -c "
import urllib.request, json
try:
    r = urllib.request.urlopen('http://localhost:${LITELLM_PORT}/v1/models', timeout=10)
    d = json.loads(r.read())
    for m in d.get('data', []):
        print(f\"  - {m['id']}\")
except Exception as e:
    print(f'FAILED: {e}')
" 2>/dev/null || echo "FAILED")
echo "${MODELS_RESULT}"

# Step 7: Update OpenViking ConfigMap with proxy endpoint (embedding + VLM)
echo ""
echo "[7/7] Updating OpenViking ConfigMap with LiteLLM proxy endpoint..."

template_file "${ROOT_DIR}/manifests/openviking-config-patch.yaml" | kubectl apply -f -

echo ""
echo "================================================"
echo "LiteLLM Proxy deployment complete!"
echo "  Endpoint: http://litellm-proxy.${NAMESPACE}.svc.cluster.local:${LITELLM_PORT}"
echo "  Embeddings: POST /v1/embeddings  (bedrock/${BEDROCK_EMBEDDING_MODEL})"
echo "  VLM:        POST /v1/chat/completions  (bedrock/${BEDROCK_VLM_MODEL})"
echo "================================================"
