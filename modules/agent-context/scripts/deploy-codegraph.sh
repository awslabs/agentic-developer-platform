#!/usr/bin/env bash
# Deploy CodeGraphContext (fixed version — direct install, no init container)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Source configuration and helpers
source "${SCRIPT_DIR}/_common.sh"
load_config "${ROOT_DIR}"

echo "================================================"
echo "Task 2: Deploy CodeGraphContext (fixed)"
echo "================================================"
echo "Namespace: ${NAMESPACE}"
echo "Image:     ${CODEGRAPH_IMAGE}"
echo "================================================"

# Step 1: Scale down existing deployment to 0 (EBS ReadWriteOnce)
echo ""
echo "[1/4] Scaling down existing codegraph deployment..."
kubectl scale deploy/codegraph-context -n "${NAMESPACE}" --replicas=0 2>/dev/null || true
# Wait for pod termination
kubectl wait --for=delete pod -l app.kubernetes.io/name=codegraph -n "${NAMESPACE}" --timeout=120s 2>/dev/null || true
kubectl wait --for=delete pod -l app=codegraph -n "${NAMESPACE}" --timeout=120s 2>/dev/null || true
# Delete old deployment if selector labels changed (immutable field)
kubectl delete deploy/codegraph-context -n "${NAMESPACE}" 2>/dev/null || true
echo "  Existing deployment removed."

# Step 2: Apply fixed manifest
echo ""
echo "[2/4] Applying fixed CodeGraphContext deployment..."

export NAMESPACE SERVICE_ACCOUNT CODEGRAPH_IMAGE

template_file "${ROOT_DIR}/manifests/codegraph.yaml" | kubectl apply -f -

# Step 3: Wait for pod ready
echo ""
echo "[3/4] Waiting for CodeGraphContext pod (this may take 2-3 minutes for package install)..."
kubectl rollout status deploy/codegraph-context -n "${NAMESPACE}" --timeout=360s

# Step 4: Verify cgc works
echo ""
echo "[4/4] Verifying CodeGraphContext installation..."

CGC_POD=$(kubectl get pods -n "${NAMESPACE}" -l app=codegraph -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)

if [ -z "${CGC_POD}" ]; then
  echo "  ERROR: No codegraph pod found."
  exit 1
fi

# Check cgc --version
echo -n "  cgc --version: "
kubectl exec "${CGC_POD}" -n "${NAMESPACE}" -- cgc --version 2>&1 || echo "not available (may be CLI-only package)"

# Check Python import
echo -n "  python3 import: "
kubectl exec "${CGC_POD}" -n "${NAMESPACE}" -- python3 -c "import codegraphcontext; print('OK')" 2>&1

echo ""
echo "================================================"
echo "CodeGraphContext deployment complete!"
echo "  Pod: ${CGC_POD}"
echo "  Access: kubectl exec -n ${NAMESPACE} ${CGC_POD} -- cgc <args>"
echo "================================================"
