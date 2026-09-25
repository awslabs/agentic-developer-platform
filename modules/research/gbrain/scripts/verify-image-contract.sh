#!/usr/bin/env bash
# =============================================================================
# verify-image-contract.sh — Validate gbrain Dockerfile and entrypoint contract
#
# Runs without Docker. Verifies:
#   1. Dockerfile pins base image, Bun and gbrain to specific versions
#   2. Entrypoint script is syntactically valid and uses exec for serve
#   3. Non-root user (appuser) is created before USER directive
#   4. Health check targets the expected port
#   5. Bun version satisfies gbrain's engine requirement (>= 1.3.11)
#
# Usage: ./scripts/verify-image-contract.sh
# Exit 0 = all checks pass; exit 1 = at least one failure.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOCKER_DIR="$(dirname "$SCRIPT_DIR")/docker"
DOCKERFILE="${DOCKER_DIR}/Dockerfile"
ENTRYPOINT="${DOCKER_DIR}/entrypoint.sh"

PASS=0
FAIL=0

check() {
  local desc="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    echo "  PASS: ${desc}"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: ${desc}"
    FAIL=$((FAIL + 1))
  fi
}

echo "=== gbrain Image Contract Verification ==="
echo ""

# --- Dockerfile checks ---
echo "Dockerfile: ${DOCKERFILE}"

check "Base image pinned to specific node version (not just :22-slim)" \
  grep -qE '^FROM .+node:22\.[0-9]+\.[0-9]+-slim' "$DOCKERFILE"

check "Bun installer pinned to specific version" \
  grep -qE 'bun\.sh/install.*bash -s.*bun-v[0-9]+\.[0-9]+\.[0-9]+' "$DOCKERFILE"

check "gbrain pinned to full commit hash (40 hex chars)" \
  grep -qE 'bun install -g github:garrytan/gbrain#[0-9a-f]{40}' "$DOCKERFILE"

check "Non-root user created (adduser)" \
  grep -q 'adduser.*appuser' "$DOCKERFILE"

check "USER directive present for non-root execution" \
  grep -q '^USER appuser' "$DOCKERFILE"

check "USER directive precedes HEALTHCHECK" \
  awk '/^USER appuser/{found=1} /^HEALTHCHECK/{if(found) exit 0; else exit 1}' "$DOCKERFILE"

check "Health check targets port 3000" \
  grep -q 'localhost:3000/health' "$DOCKERFILE"

check "EXPOSE matches health check port (3000)" \
  grep -q '^EXPOSE 3000' "$DOCKERFILE"

check "No --privileged or --cap-add in Dockerfile" \
  bash -c '! grep -qi "privileged\|cap.add" '"$DOCKERFILE"

echo ""

# --- Entrypoint checks ---
echo "Entrypoint: ${ENTRYPOINT}"

check "Entrypoint exists and is not empty" \
  test -s "$ENTRYPOINT"

check "Entrypoint syntax valid (bash -n)" \
  bash -n "$ENTRYPOINT"

check "Entrypoint uses 'set -e' for fail-fast" \
  grep -q '^set -e' "$ENTRYPOINT"

check "Entrypoint runs 'gbrain init' before serve" \
  grep -q 'gbrain init' "$ENTRYPOINT"

check "Entrypoint uses 'exec gbrain serve' (replaces shell with serve process)" \
  grep -q '^exec gbrain serve' "$ENTRYPOINT"

check "Serve command uses --http and --port 3000" \
  grep -q 'exec gbrain serve --http --port 3000' "$ENTRYPOINT"

echo ""

# --- Version compatibility checks ---
echo "Version compatibility:"

BUN_PIN=$(grep -oP 'bun-v\K[0-9]+\.[0-9]+\.[0-9]+' "$DOCKERFILE" || echo "")
if [ -n "$BUN_PIN" ]; then
  # gbrain v0.57.0.0 requires bun >= 1.3.11
  REQ_MAJOR=1 REQ_MINOR=3 REQ_PATCH=11
  IFS='.' read -r PIN_MAJOR PIN_MINOR PIN_PATCH <<< "$BUN_PIN"
  if [ "$PIN_MAJOR" -gt "$REQ_MAJOR" ] || \
     { [ "$PIN_MAJOR" -eq "$REQ_MAJOR" ] && [ "$PIN_MINOR" -gt "$REQ_MINOR" ]; } || \
     { [ "$PIN_MAJOR" -eq "$REQ_MAJOR" ] && [ "$PIN_MINOR" -eq "$REQ_MINOR" ] && [ "$PIN_PATCH" -ge "$REQ_PATCH" ]; }; then
    echo "  PASS: Bun ${BUN_PIN} satisfies gbrain requirement (>= 1.3.11)"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: Bun ${BUN_PIN} does not satisfy gbrain requirement (>= 1.3.11)"
    FAIL=$((FAIL + 1))
  fi
else
  echo "  FAIL: Could not extract pinned Bun version from Dockerfile"
  FAIL=$((FAIL + 1))
fi

echo ""
echo "=== Results: ${PASS} passed, ${FAIL} failed ==="

if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
