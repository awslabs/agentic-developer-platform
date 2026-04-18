#!/usr/bin/env bash
# Refresh GitHub App installation token and update Sourcebot
# Can be run manually or via cron.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

source "${ROOT_DIR}/config.env"
[[ -f "${ROOT_DIR}/config.local.env" ]] && source "${ROOT_DIR}/config.local.env"

echo "[$(date)] Refreshing GitHub App token..."

if [[ -n "${GITHUB_APP_ID_SECRET:-}" && -n "${GITHUB_APP_KEY_SECRET:-}" ]]; then
  python3 "${SCRIPT_DIR}/github-app-token.py" \
    --app-id-secret "${GITHUB_APP_ID_SECRET}" \
    --app-key-secret "${GITHUB_APP_KEY_SECRET}" \
    --region "${SECRETS_MANAGER_REGION}" \
    --owner "${GITHUB_APP_OWNER:-aws-innovate}" \
    --k8s-secret "agent-context-secrets" \
    --k8s-key "github-token" \
    --namespace "${NAMESPACE}"
elif [[ -n "${GITHUB_APP_SECRET_ID:-}" ]]; then
  python3 "${SCRIPT_DIR}/github-app-token.py" \
    --secret-id "${GITHUB_APP_SECRET_ID}" \
    --region "${SECRETS_MANAGER_REGION}" \
    --k8s-secret "agent-context-secrets" \
    --k8s-key "github-token" \
    --namespace "${NAMESPACE}"
else
  echo "ERROR: No GitHub App secret configured."
  exit 1
fi

echo "[$(date)] Restarting Sourcebot..."
kubectl rollout restart deploy/sourcebot -n "${NAMESPACE}"
kubectl rollout status deploy/sourcebot -n "${NAMESPACE}" --timeout=180s
echo "[$(date)] Token refresh complete."
