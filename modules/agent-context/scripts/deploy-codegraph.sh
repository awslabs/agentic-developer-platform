#!/usr/bin/env bash
# Deploy CodeGraphContext using a reviewed pre-baked image; no runtime installer.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Source configuration and helpers
source "${SCRIPT_DIR}/_common.sh"
load_config "${ROOT_DIR}"

# Resolve every offline prerequisite before the first scale/delete operation.
# --render-only emits the exact manifest without contacting Kubernetes.
if [[ $# -gt 1 || ( $# -eq 1 && "$1" != "--render-only" ) ]]; then
  echo "Usage: $0 [--render-only]" >&2
  exit 2
fi
: "${CODEGRAPH_IMAGE:?Set CODEGRAPH_IMAGE to a reviewed pre-baked image digest}"
: "${CODEGRAPH_VALIDATION_RECEIPT:?Set CODEGRAPH_VALIDATION_RECEIPT to its passed runtime receipt}"
RENDERED_MANIFEST=$(python3 "${SCRIPT_DIR}/render-codegraph.py" \
  --image "${CODEGRAPH_IMAGE}" --receipt "${CODEGRAPH_VALIDATION_RECEIPT}" \
  --namespace "${NAMESPACE}" --service-account "${SERVICE_ACCOUNT}")
if [[ "${1:-}" == "--render-only" ]]; then
  printf '%s\n' "$RENDERED_MANIFEST"
  exit 0
fi

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

printf '%s\n' "$RENDERED_MANIFEST" | kubectl apply -f -

# Step 3: Wait for pod ready
echo ""
echo "[3/4] Waiting for pre-baked CodeGraphContext pod..."
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
kubectl exec "${CGC_POD}" -n "${NAMESPACE}" -- cgc --version 2>&1

# Check Python import
echo -n "  python3 import: "
kubectl exec "${CGC_POD}" -n "${NAMESPACE}" -- python3 -c "import codegraphcontext; print('OK')" 2>&1

echo ""
echo "================================================"
echo "CodeGraphContext deployment complete!"
echo "  Pod: ${CGC_POD}"
echo "  Access: kubectl exec -n ${NAMESPACE} ${CGC_POD} -- cgc <args>"
echo "================================================"
