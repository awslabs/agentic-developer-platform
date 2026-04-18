#!/usr/bin/env bash
# Re-ingest all repos from repos.txt into OpenViking
# Idempotent — safe to run repeatedly. Changed repos get re-indexed,
# unchanged repos are processed quickly.
#
# Unlike ingest-repos.sh (which waits for processing), this script
# submits all repos and exits immediately — designed for CronJob use.
#
# Usage: ./scripts/refresh-repos.sh [--repos-file repos.txt]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Source configuration and helpers
source "${SCRIPT_DIR}/_common.sh"
load_config "${ROOT_DIR}"

# Parse arguments
REPOS_FILE_ARG=""
while [[ $# -gt 0 ]]; do
  case $1 in
    --repos-file)
      REPOS_FILE_ARG="$2"
      shift 2
      ;;
    --help)
      echo "Usage: ./scripts/refresh-repos.sh [--repos-file repos.txt]"
      echo ""
      echo "Re-ingests all repos from repos.txt into OpenViking."
      echo "Does NOT wait for processing — lets it happen async."
      exit 0
      ;;
    *)
      echo "Unknown option: $1"
      exit 1
      ;;
  esac
done

# Resolve repos file path
if [ -n "${REPOS_FILE_ARG}" ]; then
  RESOLVED_REPOS_FILE="${REPOS_FILE_ARG}"
elif [ -n "${REPOS_FILE:-}" ]; then
  if [[ "${REPOS_FILE}" != /* ]]; then
    RESOLVED_REPOS_FILE="${ROOT_DIR}/${REPOS_FILE}"
  else
    RESOLVED_REPOS_FILE="${REPOS_FILE}"
  fi
else
  RESOLVED_REPOS_FILE="${ROOT_DIR}/index_content/repos.txt"
fi

# Also check /config/repos.txt (when running inside a K8s pod with ConfigMap mount)
if [ ! -f "${RESOLVED_REPOS_FILE}" ] && [ -f "/config/repos.txt" ]; then
  RESOLVED_REPOS_FILE="/config/repos.txt"
fi

if [ ! -f "${RESOLVED_REPOS_FILE}" ]; then
  echo "ERROR: Repos file not found: ${RESOLVED_REPOS_FILE}"
  exit 1
fi

# Read repos (exclude comments and blank lines)
REPOS=$(grep -v '^\s*#' "${RESOLVED_REPOS_FILE}" | grep -v '^\s*$' | sort)
TOTAL=$(echo "${REPOS}" | wc -l | tr -d ' ')

echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] Starting repo refresh: ${TOTAL} repos"
echo "  Repos file: ${RESOLVED_REPOS_FILE}"

# Get OpenViking root key
# Try kubectl first (CronJob pod), then fall back to environment variable
if command -v kubectl &>/dev/null; then
  ROOT_KEY=$(kubectl get secret agent-context-secrets -n "${NAMESPACE}" \
    -o jsonpath='{.data.openviking-root-key}' 2>/dev/null | base64 -d 2>/dev/null || true)
fi

# Fall back to ROOT_KEY env var (set by K8s secret mount)
ROOT_KEY="${ROOT_KEY:-${OPENVIKING_ROOT_KEY:-}}"

if [ -z "${ROOT_KEY}" ]; then
  echo "ERROR: Could not retrieve OpenViking root key"
  echo "  Ensure agent-context-secrets exists in namespace ${NAMESPACE}"
  exit 1
fi

OV_URL="http://openviking.${NAMESPACE}.svc.cluster.local:1933"

# Use a secure temp file for curl responses
RESPONSE_FILE=$(mktemp /tmp/ov-response-XXXXXX.json)
trap 'rm -f "${RESPONSE_FILE}"' EXIT

SUCCESS=0
FAILED=0
SKIPPED=0

for repo in ${REPOS}; do
  # Validate org/repo format
  if ! echo "${repo}" | grep -qE '^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$'; then
    echo "  [SKIP] ${repo} (invalid format)"
    SKIPPED=$((SKIPPED + 1))
    continue
  fi

  URL="https://github.com/${repo}"
  HTTP_CODE=$(curl -s -w "%{http_code}" -o "${RESPONSE_FILE}" \
    -X POST "${OV_URL}/api/v1/resources" \
    -H "Content-Type: application/json" \
    -H "X-API-Key: ${ROOT_KEY}" \
    -d "{\"path\": \"${URL}\"}" \
    --connect-timeout 10 --max-time 30 2>/dev/null || echo "000")

  if [[ "${HTTP_CODE}" == "200" || "${HTTP_CODE}" == "201" ]]; then
    echo "  [OK] ${repo}"
    SUCCESS=$((SUCCESS + 1))
  elif [[ "${HTTP_CODE}" == "409" ]]; then
    echo "  [SKIP] ${repo} (already processing)"
    SKIPPED=$((SKIPPED + 1))
  else
    echo "  [FAIL] ${repo} (HTTP ${HTTP_CODE})"
    cat "${RESPONSE_FILE}" 2>/dev/null || true
    FAILED=$((FAILED + 1))
  fi
done

echo ""
echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] Refresh complete: ${SUCCESS} added, ${SKIPPED} skipped, ${FAILED} failed out of ${TOTAL}"

# Exit with error only if ALL repos failed
if [ "${FAILED}" -gt 0 ] && [ "${SUCCESS}" -eq 0 ]; then
  exit 1
fi
