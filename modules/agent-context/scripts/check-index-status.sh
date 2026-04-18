#!/usr/bin/env bash
# Compare repos.txt with OpenViking's indexed resources
# Shows which repos are missing from the index and which are extra.
#
# Usage: ./scripts/check-index-status.sh [--repos-file repos.txt]
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
      echo "Usage: ./scripts/check-index-status.sh [--repos-file repos.txt]"
      echo ""
      echo "Compares repos.txt with what's indexed in OpenViking."
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

if [ ! -f "${RESOLVED_REPOS_FILE}" ]; then
  echo "ERROR: Repos file not found: ${RESOLVED_REPOS_FILE}"
  exit 1
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

echo "================================================"
echo "OpenViking Index Status Check"
echo "================================================"
echo "Repos file: ${RESOLVED_REPOS_FILE}"
echo "OpenViking: ${OV_URL}"
echo "Namespace:  ${NAMESPACE}"
echo "================================================"
echo ""

# Get expected repos from repos.txt
EXPECTED=$(grep -v '^\s*#' "${RESOLVED_REPOS_FILE}" | grep -v '^\s*$' | sort)
EXPECTED_COUNT=$(echo "${EXPECTED}" | wc -l | tr -d ' ')

# Get indexed repos from OpenViking
echo "Querying OpenViking for indexed resources..."
RESPONSE=$(curl -s "${OV_URL}/api/v1/fs/ls?uri=viking://resources/" \
  -H "X-API-Key: ${ROOT_KEY}" \
  --connect-timeout 10 --max-time 30 2>/dev/null || echo "[]")

# Parse indexed repos using python3 if available, otherwise try jq
if command -v python3 &>/dev/null; then
  INDEXED=$(echo "${RESPONSE}" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get('is_dir'):
                print(item.get('name', ''))
    elif isinstance(data, dict) and 'items' in data:
        for item in data['items']:
            if isinstance(item, dict) and item.get('is_dir'):
                print(item.get('name', ''))
except (json.JSONDecodeError, KeyError):
    pass
" 2>/dev/null | sort || echo "")
elif command -v jq &>/dev/null; then
  INDEXED=$(echo "${RESPONSE}" | jq -r '.[] | select(.is_dir) | .name' 2>/dev/null | sort || echo "")
else
  echo "WARNING: Neither python3 nor jq available. Cannot parse response."
  echo "Raw response: ${RESPONSE}"
  exit 1
fi

INDEXED_COUNT=0
if [ -n "${INDEXED}" ]; then
  INDEXED_COUNT=$(echo "${INDEXED}" | wc -l | tr -d ' ')
fi

# Compare
echo ""
echo "=== Missing from OpenViking (in repos.txt but not indexed) ==="
MISSING=$(comm -23 <(echo "${EXPECTED}") <(echo "${INDEXED}") 2>/dev/null || true)
if [ -n "${MISSING}" ]; then
  echo "${MISSING}"
  MISSING_COUNT=$(echo "${MISSING}" | wc -l | tr -d ' ')
else
  echo "(none)"
  MISSING_COUNT=0
fi

echo ""
echo "=== Extra in OpenViking (indexed but not in repos.txt) ==="
EXTRA=$(comm -13 <(echo "${EXPECTED}") <(echo "${INDEXED}") 2>/dev/null || true)
if [ -n "${EXTRA}" ]; then
  echo "${EXTRA}"
  EXTRA_COUNT=$(echo "${EXTRA}" | wc -l | tr -d ' ')
else
  echo "(none)"
  EXTRA_COUNT=0
fi

echo ""
echo "=== Summary ==="
echo "Expected (repos.txt): ${EXPECTED_COUNT} repos"
echo "Indexed (OpenViking): ${INDEXED_COUNT} repos"
echo "Missing:              ${MISSING_COUNT} repos"
echo "Extra:                ${EXTRA_COUNT:-0} repos"

if [ "${MISSING_COUNT}" -eq 0 ] && [ "${EXTRA_COUNT:-0}" -eq 0 ]; then
  echo ""
  echo "Status: IN SYNC"
else
  echo ""
  echo "Status: DRIFT DETECTED"
  echo "  Run 'refresh-repos.sh' to re-sync missing repos."
fi
