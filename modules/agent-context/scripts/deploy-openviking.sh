#!/usr/bin/env bash
# Deploy/configure OpenViking with embedding + VLM support
# Applies the config ConfigMap, restarts OpenViking, and verifies health.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Source configuration and helpers
source "${SCRIPT_DIR}/_common.sh"
load_config "${ROOT_DIR}"

echo "================================================"
echo "Deploy OpenViking (Embedding + VLM)"
echo "================================================"
echo "Namespace:  ${NAMESPACE}"
echo "Embedding:  bedrock/${BEDROCK_EMBEDDING_MODEL} (dim=${BEDROCK_EMBEDDING_DIMENSION})"
echo "VLM:        bedrock/${BEDROCK_VLM_MODEL} (concurrency=${VLM_MAX_CONCURRENT})"
echo "Proxy:      http://litellm-proxy.${NAMESPACE}.svc.cluster.local:${LITELLM_PORT}"
echo "================================================"

# Step 1: Template and apply the OpenViking ConfigMap
echo ""
echo "[1/4] Applying OpenViking ConfigMap (embedding + VLM)..."

export NAMESPACE BEDROCK_EMBEDDING_MODEL BEDROCK_EMBEDDING_DIMENSION BEDROCK_REGION
export BEDROCK_VLM_MODEL VLM_MAX_CONCURRENT VLM_MAX_RETRIES LITELLM_PORT

template_file "${ROOT_DIR}/manifests/openviking-config-patch.yaml" | kubectl apply -f -

# Step 2: Restart OpenViking to pick up new config
echo ""
echo "[2/4] Restarting OpenViking to apply config..."
# Scale to 0 first (EBS PVC is ReadWriteOnce - can't multi-attach during rolling update)
kubectl scale deploy/openviking-server -n "${NAMESPACE}" --replicas=0
kubectl wait --for=delete pod -l app.kubernetes.io/name=openviking -n "${NAMESPACE}" --timeout=120s 2>/dev/null || true
kubectl scale deploy/openviking-server -n "${NAMESPACE}" --replicas=1
kubectl rollout status deploy/openviking-server -n "${NAMESPACE}" --timeout=180s

# Step 3: Verify OpenViking health
echo ""
echo "[3/4] Verifying OpenViking health..."
sleep 5
OV_HEALTHY=false
for i in $(seq 1 12); do
  OV_HEALTH=$(kubectl exec deploy/openviking-server -n "${NAMESPACE}" -c openviking -- python3 -c "
import urllib.request, json
r = urllib.request.urlopen('http://localhost:1933/health', timeout=5)
print(r.read().decode())
" 2>/dev/null || true)
  if echo "${OV_HEALTH}" | grep -q '"healthy":true'; then
    echo "  OpenViking is healthy: ${OV_HEALTH}"
    OV_HEALTHY=true
    break
  fi
  echo "  Waiting for OpenViking... (attempt ${i}/12)"
  sleep 10
done

if [ "${OV_HEALTHY}" != "true" ]; then
  echo "  WARNING: OpenViking may not be fully healthy yet."
  echo "  Check: kubectl logs deploy/openviking-server -n ${NAMESPACE} -c openviking"
fi

# Step 4: Verify VLM config is active
echo ""
echo "[4/4] Verifying VLM configuration..."

OV_CONFIG=$(kubectl get configmap openviking-config -n "${NAMESPACE}" -o jsonpath='{.data.ov\.conf}' 2>/dev/null || echo "{}")
if echo "${OV_CONFIG}" | grep -q "\"vlm\""; then
  echo "  VLM section present in OpenViking config."
  VLM_MODEL=$(echo "${OV_CONFIG}" | python3 -c "import sys,json; c=json.load(sys.stdin); print(c.get('vlm',{}).get('model','N/A'))" 2>/dev/null || echo "N/A")
  echo "  VLM model: ${VLM_MODEL}"
else
  echo "  WARNING: VLM section not found in OpenViking config."
fi

if echo "${OV_CONFIG}" | grep -q "litellm-proxy"; then
  echo "  Embedding + VLM both routed through LiteLLM proxy."
else
  echo "  WARNING: Config may not be pointing to LiteLLM proxy."
fi

echo ""
echo "================================================"
echo "OpenViking deployment complete!"
echo "  Endpoint: http://openviking.${NAMESPACE}.svc.cluster.local:1933"
echo "  Embedding: bedrock/${BEDROCK_EMBEDDING_MODEL}"
echo "  VLM:       bedrock/${BEDROCK_VLM_MODEL}"
echo "================================================"
