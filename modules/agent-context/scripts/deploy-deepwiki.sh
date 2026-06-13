#!/usr/bin/env bash
# Deploy DeepWiki — rich repo documentation generator with architecture diagrams
# Uses the LiteLLM proxy for all LLM calls (no separate API keys needed)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Source configuration and helpers
source "${SCRIPT_DIR}/_common.sh"
load_config "${ROOT_DIR}"

echo "================================================"
echo "Deploy DeepWiki (Wiki Generation)"
echo "================================================"
echo "Namespace:  ${NAMESPACE}"
echo "Image:      ${DEEPWIKI_IMAGE}"
echo "API Port:   ${DEEPWIKI_PORT}"
echo "LiteLLM:    http://litellm-proxy.${NAMESPACE}.svc.cluster.local:${LITELLM_PORT}"
echo "================================================"

# Step 1: Create ConfigMap with DeepWiki config files
echo ""
echo "[1/4] Creating DeepWiki config ConfigMap..."

export NAMESPACE
template_file "${ROOT_DIR}/manifests/deepwiki-configmap.yaml" | kubectl apply -f -

echo "  ConfigMap created/updated."

# Step 2: Template and apply DeepWiki manifest
echo ""
echo "[2/4] Applying DeepWiki deployment and service..."

export NAMESPACE SERVICE_ACCOUNT DEEPWIKI_IMAGE DEEPWIKI_PORT
export LITELLM_PORT BEDROCK_REGION

template_file "${ROOT_DIR}/manifests/deepwiki.yaml" | kubectl apply -f -

echo "  Deployment and Service applied."

# Step 3: Wait for pod ready
echo ""
echo "[3/4] Waiting for DeepWiki pod to be ready..."
kubectl rollout status deploy/deepwiki -n "${NAMESPACE}" --timeout=300s

# Step 4: Verify health endpoint
echo ""
echo "[4/4] Verifying DeepWiki health..."

DW_HEALTHY=false
for i in $(seq 1 12); do
  DW_HEALTH=$(kubectl exec deploy/deepwiki -n "${NAMESPACE}" -- \
    curl -sf http://localhost:${DEEPWIKI_PORT}/health 2>/dev/null || echo "UNREACHABLE")
  if echo "${DW_HEALTH}" | grep -qiE "ok|healthy|alive"; then
    echo "  DeepWiki is healthy: ${DW_HEALTH}"
    DW_HEALTHY=true
    break
  fi
  echo "  Waiting for DeepWiki... (attempt ${i}/12)"
  sleep 10
done

if [ "${DW_HEALTHY}" != "true" ]; then
  echo "  WARNING: DeepWiki may not be fully healthy yet."
  echo "  Check: kubectl logs deploy/deepwiki -n ${NAMESPACE}"
fi

echo ""
echo "================================================"
echo "DeepWiki deployment complete!"
echo "  API:      http://deepwiki.${NAMESPACE}.svc.cluster.local:${DEEPWIKI_PORT}"
echo "  Frontend: http://deepwiki.${NAMESPACE}.svc.cluster.local:3000"
echo "  Health:   GET /health"
echo "  Cache:    GET /api/processed_projects"
echo "================================================"
