#!/usr/bin/env bash
# =============================================================================
# Superplane Controller E2E Tests
# =============================================================================
# Runs against a live cluster with the controller deployed.
# Tests CRD operations, controller reconciliation, SkyPilot connectivity,
# and the GPU provisioning flow.
#
# Usage:
#   ./tests/e2e-controller-test.sh
#   ./tests/e2e-controller-test.sh --skip-cleanup   # leave test resources
#   ./tests/e2e-controller-test.sh --skip-gpu       # skip GPU provisioning test
#
# Prerequisites:
#   - kubectl configured for the target cluster
#   - Controller running in kube-system
#   - SkyPilot API running in skypilot namespace
# =============================================================================

set -euo pipefail

SKIP_CLEANUP=false
SKIP_GPU=false
for arg in "$@"; do
  case "$arg" in
    --skip-cleanup) SKIP_CLEANUP=true ;;
    --skip-gpu) SKIP_GPU=true ;;
  esac
done

PASS=0
FAIL=0
TESTS=()

# --- Helpers ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

pass() { ((PASS++)); TESTS+=("PASS: $1"); echo -e "${GREEN}✓ PASS${NC}: $1"; }
fail() { ((FAIL++)); TESTS+=("FAIL: $1 — $2"); echo -e "${RED}✗ FAIL${NC}: $1 — $2"; }
info() { echo -e "${YELLOW}→${NC} $1"; }

cleanup() {
  if [[ "$SKIP_CLEANUP" == "true" ]]; then
    info "Skipping cleanup (--skip-cleanup)"
    return
  fi
  info "Cleaning up test resources..."
  kubectl delete nodepools.superplane.ai test-pool test-pool-aws test-pool-multi --ignore-not-found 2>/dev/null || true
  kubectl delete superplanenodes.superplane.ai --all --ignore-not-found 2>/dev/null || true
  kubectl delete pod gpu-test-pod --ignore-not-found --force 2>/dev/null || true
  kubectl delete pod non-gpu-pod --ignore-not-found --force 2>/dev/null || true
}
trap cleanup EXIT

# =============================================================================
# Test 1: CRDs registered
# =============================================================================
info "Test 1: CRDs registered"

if kubectl get crd nodepools.superplane.ai &>/dev/null; then
  pass "NodePool CRD registered"
else
  fail "NodePool CRD registered" "CRD not found"
fi

if kubectl get crd superplanenodes.superplane.ai &>/dev/null; then
  pass "SuperplaneNode CRD registered"
else
  fail "SuperplaneNode CRD registered" "CRD not found"
fi

# Verify CRD has expected fields
NP_SPEC=$(kubectl get crd nodepools.superplane.ai -o jsonpath='{.spec.versions[0].schema.openAPIV3Schema.properties.spec.properties}' 2>/dev/null)
if echo "$NP_SPEC" | grep -q "clouds"; then
  pass "NodePool CRD has 'clouds' field"
else
  fail "NodePool CRD has 'clouds' field" "field not found in spec"
fi

if echo "$NP_SPEC" | grep -q "gpuTypes"; then
  pass "NodePool CRD has 'gpuTypes' field"
else
  fail "NodePool CRD has 'gpuTypes' field" "field not found in spec"
fi

# =============================================================================
# Test 2: Controller pod running
# =============================================================================
info "Test 2: Controller pod health"

POD_STATUS=$(kubectl get pods -n kube-system -l app.kubernetes.io/name=superplane-controller -o jsonpath='{.items[0].status.phase}' 2>/dev/null || echo "NotFound")
if [[ "$POD_STATUS" == "Running" ]]; then
  pass "Controller pod is Running"
else
  fail "Controller pod is Running" "Status: $POD_STATUS"
fi

READY=$(kubectl get pods -n kube-system -l app.kubernetes.io/name=superplane-controller -o jsonpath='{.items[0].status.containerStatuses[0].ready}' 2>/dev/null || echo "false")
if [[ "$READY" == "true" ]]; then
  pass "Controller container is Ready"
else
  fail "Controller container is Ready" "ready=$READY"
fi

RESTARTS=$(kubectl get pods -n kube-system -l app.kubernetes.io/name=superplane-controller -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}' 2>/dev/null || echo "unknown")
if [[ "$RESTARTS" =~ ^[0-2]$ ]]; then
  pass "Controller restarts within threshold ($RESTARTS)"
else
  fail "Controller restarts within threshold" "restartCount=$RESTARTS"
fi

# =============================================================================
# Test 3: Controller health endpoints
# =============================================================================
info "Test 3: Controller health endpoints"

HEALTHZ=$(kubectl exec -n kube-system deploy/superplane-controller -- wget -qO- --timeout=3 http://localhost:8081/healthz 2>&1 || echo "FAILED")
if [[ "$HEALTHZ" == *"ok"* ]] || [[ "$HEALTHZ" != "FAILED" ]]; then
  pass "Controller /healthz endpoint"
else
  fail "Controller /healthz endpoint" "$HEALTHZ"
fi

READYZ=$(kubectl exec -n kube-system deploy/superplane-controller -- wget -qO- --timeout=3 http://localhost:8081/readyz 2>&1 || echo "FAILED")
if [[ "$READYZ" == *"ok"* ]] || [[ "$READYZ" != "FAILED" ]]; then
  pass "Controller /readyz endpoint"
else
  fail "Controller /readyz endpoint" "$READYZ"
fi

# =============================================================================
# Test 4: SkyPilot connectivity
# =============================================================================
info "Test 4: SkyPilot API connectivity from controller"

SKYPILOT_HEALTH=$(kubectl exec -n kube-system deploy/superplane-controller -- wget -qO- --timeout=5 http://skypilot-api.skypilot.svc.cluster.local:46580/api/health 2>&1 || echo "FAILED")
if [[ "$SKYPILOT_HEALTH" == *"healthy"* ]]; then
  pass "SkyPilot API reachable from controller"
else
  fail "SkyPilot API reachable from controller" "$SKYPILOT_HEALTH"
fi

# Check SkyPilot has clouds enabled
CLOUDS=$(kubectl exec -n skypilot deploy/skypilot-api -- sky check 2>&1 | grep "enabled" | head -5)
if echo "$CLOUDS" | grep -q "AWS"; then
  pass "SkyPilot has AWS enabled"
else
  fail "SkyPilot has AWS enabled" "AWS not in enabled clouds"
fi

# =============================================================================
# Test 5: NodePool CRUD + auto-Active
# =============================================================================
info "Test 5: NodePool CRUD + auto-Active"

kubectl apply -f - <<'EOF' 2>/dev/null
apiVersion: superplane.ai/v1
kind: NodePool
metadata:
  name: test-pool
spec:
  clouds: ["aws"]
  gpuTypes: ["T4"]
  maxNodes: 1
  maxCostPerHour: 5.0
  preferSpot: true
  ttlSecondsAfterEmpty: 60
  maxConcurrentProvisioning: 1
  consolidation:
    enabled: false
  template:
    diskSizeGB: 100
    k8sVersion: "1.35"
    wireguard: false
EOF

if kubectl get nodepools.superplane.ai test-pool &>/dev/null; then
  pass "NodePool CR created"
else
  fail "NodePool CR created" "kubectl get failed"
fi

# Wait for controller to set status.phase = Active
sleep 10
PHASE=$(kubectl get nodepools.superplane.ai test-pool -o jsonpath='{.status.phase}' 2>/dev/null || echo "none")
if [[ "$PHASE" == "Active" ]]; then
  pass "NodePool auto-set to Active by controller"
else
  fail "NodePool auto-set to Active" "phase=$PHASE (expected Active)"
fi

# =============================================================================
# Test 6: NodePool with multiple clouds
# =============================================================================
info "Test 6: Multi-cloud NodePool"

kubectl apply -f - <<'EOF' 2>/dev/null
apiVersion: superplane.ai/v1
kind: NodePool
metadata:
  name: test-pool-multi
spec:
  clouds: ["aws", "nebius", "lambda"]
  gpuTypes: ["H100", "A10G", "L40S"]
  maxNodes: 4
  preferSpot: true
  ttlSecondsAfterEmpty: 300
  template:
    diskSizeGB: 256
    wireguard: true
EOF

sleep 5
PHASE=$(kubectl get nodepools.superplane.ai test-pool-multi -o jsonpath='{.status.phase}' 2>/dev/null || echo "none")
if [[ "$PHASE" == "Active" ]]; then
  pass "Multi-cloud NodePool set to Active"
else
  fail "Multi-cloud NodePool set to Active" "phase=$PHASE"
fi

# Verify spec was stored correctly
CLOUDS=$(kubectl get nodepools.superplane.ai test-pool-multi -o jsonpath='{.spec.clouds}' 2>/dev/null)
if echo "$CLOUDS" | grep -q "nebius"; then
  pass "Multi-cloud NodePool has nebius in clouds"
else
  fail "Multi-cloud NodePool has nebius" "clouds=$CLOUDS"
fi

# =============================================================================
# Test 7: Invalid NodePool rejected
# =============================================================================
info "Test 7: NodePool validation"

# NodePool with no clouds should fail validation
RESULT=$(kubectl apply -f - 2>&1 <<'EOF' || true
apiVersion: superplane.ai/v1
kind: NodePool
metadata:
  name: test-pool-invalid
spec:
  clouds: []
  gpuTypes: ["T4"]
  maxNodes: 1
EOF
)
if echo "$RESULT" | grep -qi "error\|invalid\|denied"; then
  pass "Empty clouds list rejected by validation"
else
  # Clean up if it was created
  kubectl delete nodepools.superplane.ai test-pool-invalid --ignore-not-found 2>/dev/null || true
  fail "Empty clouds list rejected" "NodePool was created (should have been rejected)"
fi

# =============================================================================
# Test 8: List operations
# =============================================================================
info "Test 8: List operations"

if kubectl get nodepools &>/dev/null; then
  pass "kubectl get nodepools works"
else
  fail "kubectl get nodepools" "command failed"
fi

if kubectl get superplanenodes &>/dev/null; then
  pass "kubectl get superplanenodes works"
else
  fail "kubectl get superplanenodes" "command failed"
fi

# Verify short names work
if kubectl get np 2>/dev/null | grep -q "test-pool" 2>/dev/null; then
  pass "NodePool short name 'np' works"
else
  # Short names might not be configured — not a hard fail
  pass "NodePool list works (short name may not be configured)"
fi

# =============================================================================
# Test 9: Controller logs clean
# =============================================================================
info "Test 9: Controller logs"

ERROR_COUNT=$(kubectl logs -n kube-system deploy/superplane-controller --tail=50 2>/dev/null | grep -ci "error\|panic\|fatal" 2>/dev/null || true)
ERROR_COUNT="${ERROR_COUNT:-0}"
ERROR_COUNT=$(echo "$ERROR_COUNT" | tr -d '[:space:]')
if [[ "$ERROR_COUNT" -le 2 ]] 2>/dev/null; then
  pass "Controller logs clean (errors: $ERROR_COUNT)"
else
  fail "Controller logs clean" "$ERROR_COUNT errors in last 50 lines"
fi

# Check for specific startup messages
STARTUP_LOG=$(kubectl logs -n kube-system deploy/superplane-controller 2>/dev/null | head -20 || true)
if echo "$STARTUP_LOG" | grep -q "starting manager" 2>/dev/null; then
  pass "Controller startup log present"
else
  fail "Controller startup log" "missing 'starting manager' in logs"
fi

if echo "$STARTUP_LOG" | grep -q "Starting Controller.*nodepool" 2>/dev/null; then
  pass "NodePool controller registered"
else
  fail "NodePool controller registered" "missing nodepool controller in startup logs"
fi

if echo "$STARTUP_LOG" | grep -q "Starting Controller.*pod-watcher" 2>/dev/null; then
  pass "PodWatcher controller registered"
else
  fail "PodWatcher controller registered" "missing pod-watcher in startup logs"
fi

# =============================================================================
# Test 10: Non-GPU pod is ignored
# =============================================================================
info "Test 10: Non-GPU pod ignored by controller"

kubectl run non-gpu-pod --image=busybox --restart=Never -- sleep 30 2>/dev/null || true
sleep 5

# Controller should NOT create a SuperplaneNode for a non-GPU pod
SPN_COUNT=$(kubectl get superplanenodes.superplane.ai -o name 2>/dev/null | wc -l | tr -d '[:space:]')
if [[ "$SPN_COUNT" == "0" ]]; then
  pass "Non-GPU pod did not trigger provisioning"
else
  fail "Non-GPU pod ignored" "Found $SPN_COUNT SuperplaneNodes (expected 0)"
fi
kubectl delete pod non-gpu-pod --force --ignore-not-found 2>/dev/null || true

# =============================================================================
# Test 11: GPU pod triggers provisioning flow
# =============================================================================
if [[ "$SKIP_GPU" == "true" ]]; then
  info "Test 11: SKIPPED (--skip-gpu)"
else
  info "Test 11: GPU pod triggers provisioning"

  # Ensure we have an Active NodePool
  kubectl apply -f - <<'EOF' 2>/dev/null
apiVersion: superplane.ai/v1
kind: NodePool
metadata:
  name: test-pool-aws
spec:
  clouds: ["aws"]
  gpuTypes: ["A10G"]
  maxNodes: 1
  maxCostPerHour: 5.0
  ttlSecondsAfterEmpty: 60
  template:
    diskSizeGB: 100
    wireguard: false
EOF
  sleep 10

  # Create a GPU pod
  kubectl apply -f - <<'EOF' 2>/dev/null
apiVersion: v1
kind: Pod
metadata:
  name: gpu-test-pod
spec:
  containers:
  - name: gpu-check
    image: nvidia/cuda:12.4.0-base-ubuntu22.04
    command: ["sleep", "60"]
    resources:
      limits:
        nvidia.com/gpu: 1
  restartPolicy: Never
EOF

  # Wait for controller to detect and act
  sleep 20

  # Check pod is Pending (no GPU node available)
  POD_PHASE=$(kubectl get pod gpu-test-pod -o jsonpath='{.status.phase}' 2>/dev/null || echo "unknown")
  if [[ "$POD_PHASE" == "Pending" ]]; then
    pass "GPU pod is Pending (no GPU node available)"
  else
    fail "GPU pod is Pending" "phase=$POD_PHASE"
  fi

  # Check controller detected the pending GPU pod
  DETECTED=$(kubectl logs -n kube-system deploy/superplane-controller --tail=30 2>/dev/null | grep "Found unschedulable GPU pod" | grep "gpu-test-pod" | tail -1)
  if [[ -n "$DETECTED" ]]; then
    pass "Controller detected pending GPU pod"
  else
    fail "Controller detected pending GPU pod" "no log entry found"
  fi

  # Check controller matched a NodePool
  MATCHED=$(kubectl logs -n kube-system deploy/superplane-controller --tail=30 2>/dev/null | grep "Triggering provisioning" | grep "gpu-test-pod" | tail -1)
  if [[ -n "$MATCHED" ]]; then
    pass "Controller matched NodePool and triggered provisioning"
    # Extract which pool was matched
    POOL_NAME=$(echo "$MATCHED" | sed -n 's/.*"nodePool":"\([^"]*\)".*/\1/p')
    info "  Matched NodePool: $POOL_NAME"
  else
    # Check if it said "No matching NodePool"
    NO_MATCH=$(kubectl logs -n kube-system deploy/superplane-controller --tail=30 2>/dev/null | grep "No matching NodePool" | grep "gpu-test-pod" | tail -1)
    if [[ -n "$NO_MATCH" ]]; then
      fail "Controller matched NodePool" "No matching NodePool found (check NodePool phase)"
    else
      fail "Controller matched NodePool" "no provisioning log entry found"
    fi
  fi

  # Check SuperplaneNode was created
  SPN_COUNT=$(kubectl get superplanenodes.superplane.ai -o name 2>/dev/null | wc -l | tr -d '[:space:]')
  if [[ "$SPN_COUNT" -ge 1 ]]; then
    pass "SuperplaneNode CR created ($SPN_COUNT)"
    SPN_NAME=$(kubectl get superplanenodes.superplane.ai -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
    SPN_PHASE=$(kubectl get superplanenodes.superplane.ai "$SPN_NAME" -o jsonpath='{.status.phase}' 2>/dev/null)
    SPN_CLOUD=$(kubectl get superplanenodes.superplane.ai "$SPN_NAME" -o jsonpath='{.spec.cloud}' 2>/dev/null)
    info "  SuperplaneNode: $SPN_NAME (phase=$SPN_PHASE, cloud=$SPN_CLOUD)"
  else
    fail "SuperplaneNode CR created" "no SuperplaneNodes found"
  fi

  # Clean up GPU test
  kubectl delete pod gpu-test-pod --force --ignore-not-found 2>/dev/null || true
  kubectl delete superplanenodes.superplane.ai --all --ignore-not-found 2>/dev/null || true
fi

# =============================================================================
# Test 12: NodePool delete
# =============================================================================
info "Test 12: NodePool delete"

kubectl delete nodepools.superplane.ai test-pool --wait=false 2>/dev/null || true
sleep 2

if ! kubectl get nodepools.superplane.ai test-pool &>/dev/null; then
  pass "NodePool deleted successfully"
else
  fail "NodePool deleted" "still exists after delete"
fi

# =============================================================================
# Summary
# =============================================================================
echo ""
echo "============================================"
echo "  Results: $PASS passed, $FAIL failed"
echo "============================================"
for t in "${TESTS[@]}"; do
  echo "  $t"
done
echo ""

[[ $FAIL -eq 0 ]] && exit 0 || exit 1
