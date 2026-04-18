#!/usr/bin/env bash
# Add a single repo to OpenViking and repos.txt
# Usage: ./scripts/add-repo.sh org/repo
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Source configuration and helpers
source "${SCRIPT_DIR}/_common.sh"
load_config "${ROOT_DIR}"

REPO="${1:?Usage: $0 org/repo}"

# Validate format
if ! echo "${REPO}" | grep -qE '^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$'; then
  echo "ERROR: Invalid repo format '${REPO}'. Expected: org/repo"
  exit 1
fi

# Resolve repos file
if [ -n "${REPOS_FILE:-}" ]; then
  if [[ "${REPOS_FILE}" != /* ]]; then
    RESOLVED_REPOS_FILE="${ROOT_DIR}/${REPOS_FILE}"
  else
    RESOLVED_REPOS_FILE="${REPOS_FILE}"
  fi
else
  RESOLVED_REPOS_FILE="${ROOT_DIR}/index_content/repos.txt"
fi

# Get OpenViking root key
if command -v kubectl &>/dev/null; then
  ROOT_KEY=$(kubectl get secret agent-context-secrets -n "${NAMESPACE}" \
    -o jsonpath='{.data.openviking-root-key}' 2>/dev/null | base64 -d 2>/dev/null || true)
fi
ROOT_KEY="${ROOT_KEY:-${OPENVIKING_ROOT_KEY:-}}"

if [ -z "${ROOT_KEY}" ]; then
  echo "ERROR: Could not retrieve OpenViking root key"
  exit 1
fi

OV_URL="http://openviking.${NAMESPACE}.svc.cluster.local:1933"
URL="https://github.com/${REPO}"

# Use a secure temp file for curl response
RESPONSE_FILE=$(mktemp /tmp/ov-add-response-XXXXXX.json)
trap 'rm -f "${RESPONSE_FILE}"' EXIT

echo "Adding repo: ${REPO}"
echo "  OpenViking URL: ${OV_URL}"
echo "  GitHub URL:     ${URL}"
echo ""

# Add to OpenViking
HTTP_CODE=$(curl -s -w "%{http_code}" -o "${RESPONSE_FILE}" \
  -X POST "${OV_URL}/api/v1/resources" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${ROOT_KEY}" \
  -d "{\"path\": \"${URL}\"}" \
  --connect-timeout 10 --max-time 30 2>/dev/null || echo "000")

if [[ "${HTTP_CODE}" == "200" || "${HTTP_CODE}" == "201" ]]; then
  echo "  [OK] Submitted to OpenViking for indexing"
elif [[ "${HTTP_CODE}" == "409" ]]; then
  echo "  [OK] Already being processed by OpenViking"
else
  echo "  [FAIL] HTTP ${HTTP_CODE}"
  cat "${RESPONSE_FILE}" 2>/dev/null || true
  echo ""
  echo "Failed to add ${REPO} to OpenViking. Not updating repos.txt."
  exit 1
fi

# Add to repos.txt if not already there
if [ -f "${RESOLVED_REPOS_FILE}" ]; then
  if ! grep -qxF "${REPO}" "${RESOLVED_REPOS_FILE}"; then
    echo "${REPO}" >> "${RESOLVED_REPOS_FILE}"
    echo "  [OK] Added ${REPO} to ${RESOLVED_REPOS_FILE}"
  else
    echo "  [OK] ${REPO} already in ${RESOLVED_REPOS_FILE}"
  fi
else
  echo "${REPO}" > "${RESOLVED_REPOS_FILE}"
  echo "  [OK] Created ${RESOLVED_REPOS_FILE} with ${REPO}"
fi

# Update the ConfigMap so CronJob picks it up
if command -v kubectl &>/dev/null; then
  kubectl create configmap openviking-repos \
    --from-file=repos.txt="${RESOLVED_REPOS_FILE}" \
    -n "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f - 2>/dev/null && \
    echo "  [OK] Updated openviking-repos ConfigMap" || \
    echo "  [WARN] Could not update ConfigMap (will sync on next deploy)"
fi

echo ""
echo "Done. Run 'check-index-status.sh' to verify indexing status."
