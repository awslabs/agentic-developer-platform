#!/usr/bin/env bash
# Deploy Sourcebot with GitHub App token rotation
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Source configuration and helpers
source "${SCRIPT_DIR}/_common.sh"
load_config "${ROOT_DIR}"

echo "================================================"
echo "Task 3: Deploy Sourcebot with GitHub App Token Rotation"
echo "================================================"
echo "Namespace:    ${NAMESPACE}"
echo "GitHub App:   ${GITHUB_APP_SECRET_ID}"
echo "Default Org:  ${SOURCEBOT_DEFAULT_ORG}"
echo "================================================"

# Step 1: Create the github-app-token-script ConfigMap
echo ""
echo "[1/7] Creating github-app-token-script ConfigMap..."
kubectl create configmap github-app-token-script \
  --from-file=github-app-token.py="${SCRIPT_DIR}/github-app-token.py" \
  -n "${NAMESPACE}" \
  --dry-run=client -o yaml | kubectl apply -f -

# Step 2: Create/update Sourcebot config ConfigMap
echo ""
echo "[2/7] Creating sourcebot-config ConfigMap..."

export NAMESPACE SOURCEBOT_DEFAULT_ORG

# If a custom repos file is specified, generate a config with specific repos
if [ -n "${SOURCEBOT_REPOS_FILE:-}" ] && [ -f "${SOURCEBOT_REPOS_FILE}" ]; then
  echo "  Using repos from: ${SOURCEBOT_REPOS_FILE}"
  REPOS_JSON=$(while IFS= read -r repo; do
    repo=$(echo "$repo" | tr -d '[:space:]')
    [ -n "$repo" ] && echo "\"$repo\""
  done < "${SOURCEBOT_REPOS_FILE}" | paste -sd, -)

  cat <<CONFIGEOF | kubectl apply -f -
apiVersion: v1
kind: ConfigMap
metadata:
  name: sourcebot-config
  namespace: ${NAMESPACE}
  labels:
    app.kubernetes.io/name: sourcebot
    app.kubernetes.io/part-of: agent-context-platform
data:
  config.json: |
    {
      "\$schema": "https://raw.githubusercontent.com/sourcebot-dev/sourcebot/main/schemas/v3/index.json",
      "connections": {
        "github-repos": {
          "type": "github",
          "token": { "env": "GITHUB_TOKEN" },
          "repos": [${REPOS_JSON}]
        }
      }
    }
CONFIGEOF
else
  echo "  Using default org: ${SOURCEBOT_DEFAULT_ORG}"
  template_file "${ROOT_DIR}/manifests/sourcebot-config.yaml" | kubectl apply -f -
fi

# Step 3: Attempt initial GitHub App token generation
echo ""
echo "[3/7] Generating initial GitHub App token..."

GITHUB_APP_TOKEN_OK=false

# Check if the Secrets Manager secret exists
if aws secretsmanager describe-secret --secret-id "${GITHUB_APP_SECRET_ID}" --region "${SECRETS_MANAGER_REGION}" >/dev/null 2>&1; then
  echo "  GitHub App secret found in Secrets Manager. Generating token..."
  if python3 "${SCRIPT_DIR}/github-app-token.py" \
      --secret-id "${GITHUB_APP_SECRET_ID}" \
      --region "${SECRETS_MANAGER_REGION}" \
      --k8s-secret "agent-context-secrets" \
      --k8s-key "github-token" \
      --namespace "${NAMESPACE}" 2>&1; then
    GITHUB_APP_TOKEN_OK=true
    echo "  GitHub App token generated and stored in K8s secret."
  else
    echo "  WARNING: Token generation failed. Sourcebot will use existing token from K8s secret."
  fi
else
  echo "  GitHub App secret '${GITHUB_APP_SECRET_ID}' not found in Secrets Manager."
  echo "  Sourcebot will use the existing github-token from K8s secret (agent-context-secrets)."
  echo "  To enable auto-rotation, create the secret with: app_id, installation_id, private_key"
fi

# Step 4: Apply RBAC for token refresh CronJob
echo ""
echo "[4/7] Applying RBAC for token refresh CronJob..."

export SERVICE_ACCOUNT
template_file "${ROOT_DIR}/manifests/sourcebot-token-cronjob.yaml" | kubectl apply -f -

# Step 5: Apply Sourcebot deployment (postgres, redis, sourcebot)
echo ""
echo "[5/7] Applying Sourcebot deployment (Postgres + Redis + Sourcebot)..."

export SOURCEBOT_IMAGE GITHUB_APP_SECRET_ID SECRETS_MANAGER_REGION

# Scale Sourcebot to 0 first for EBS PVC (ReadWriteOnce) and to apply updated spec
kubectl scale deploy/sourcebot -n "${NAMESPACE}" --replicas=0 2>/dev/null || true
kubectl wait --for=delete pod -l app.kubernetes.io/name=sourcebot,app.kubernetes.io/component=application -n "${NAMESPACE}" --timeout=120s 2>/dev/null || true

template_file "${ROOT_DIR}/manifests/sourcebot.yaml" | kubectl apply -f - || {
  echo "  Deployment apply failed (likely immutable selector change). Deleting and re-creating..."
  kubectl delete deploy/sourcebot -n "${NAMESPACE}" 2>/dev/null || true
  kubectl wait --for=delete pod -l app.kubernetes.io/name=sourcebot -n "${NAMESPACE}" --timeout=60s 2>/dev/null || true
  template_file "${ROOT_DIR}/manifests/sourcebot.yaml" | kubectl apply -f -
}

# Step 6: Wait for Sourcebot to be ready
echo ""
echo "[6/7] Waiting for Sourcebot pods to be ready..."
kubectl rollout status deploy/sourcebot-postgres -n "${NAMESPACE}" --timeout=120s
kubectl rollout status deploy/sourcebot-redis -n "${NAMESPACE}" --timeout=120s
kubectl rollout status deploy/sourcebot -n "${NAMESPACE}" --timeout=300s

# Step 7: Verify health
echo ""
echo "[7/7] Verifying Sourcebot health..."

SB_POD=$(kubectl get pods -n "${NAMESPACE}" -l app=sourcebot,app.kubernetes.io/component=application -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)

if [ -z "${SB_POD}" ]; then
  # Try alternate label
  SB_POD=$(kubectl get pods -n "${NAMESPACE}" -l app.kubernetes.io/name=sourcebot,app.kubernetes.io/component=application -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
fi

if [ -n "${SB_POD}" ]; then
  for i in $(seq 1 6); do
    HEALTH=$(kubectl exec "${SB_POD}" -n "${NAMESPACE}" -c sourcebot -- curl -sf http://localhost:3000/api/health 2>/dev/null || true)
    if echo "${HEALTH}" | grep -q '"ok"'; then
      echo "  Sourcebot health: ${HEALTH}"
      break
    fi
    echo "  Waiting for Sourcebot health... (attempt ${i}/6)"
    sleep 10
  done
else
  echo "  WARNING: Could not find Sourcebot pod for health check."
fi

echo ""
echo "================================================"
echo "Sourcebot deployment complete!"
echo "  Endpoint: http://sourcebot.${NAMESPACE}.svc.cluster.local:3000"
echo "  GitHub App token rotation: ${GITHUB_APP_TOKEN_OK}"
if [ "${GITHUB_APP_TOKEN_OK}" = "true" ]; then
  echo "  CronJob: sourcebot-token-refresh (every 50 minutes)"
fi
echo "================================================"
