#!/usr/bin/env bash
# Shared gateway rollout helpers. Source from deploy-all.sh.

# Keep the live desired count when rendering an upgrade manifest. Failing the
# read must stop the upgrade rather than silently scaling a live service down.
gateway_deployment_replicas() {
  local replicas=2
  if [[ "$1" == true ]]; then
    replicas=$(kubectl get deployment/bedrockgateway -n adp-gateway \
      --request-timeout=30s -o jsonpath='{.spec.replicas}') || return 1
  fi
  if [[ ! "$replicas" =~ ^[0-9]+$ ]]; then
    echo 'Cannot determine gateway desired replica count' >&2
    return 1
  fi
  printf '%s\n' "$replicas"
}

gateway_rollout_diagnostics() {
  # Avoid ConfigMaps, Secrets and full pod specs (which may contain credentials).
  kubectl get deployment,replicaset,pods -n adp-gateway -l app=bedrockgateway \
    -o wide --request-timeout=30s || true
  kubectl get deployment/bedrockgateway -n adp-gateway --request-timeout=30s \
    -o jsonpath='{.status.conditions}' || true
  kubectl get events -n adp-gateway --sort-by=.metadata.creationTimestamp \
    --request-timeout=30s || true
}

wait_for_gateway_rollout() {
  # 16 minutes of permitted stream draining plus provisioning/startup headroom.
  # This is a total wall-clock bound, not an unbounded retry on progress.
  local timeout="${ADP_GATEWAY_ROLLOUT_TIMEOUT_SECONDS:-2400}"
  if [[ ! "$timeout" =~ ^[1-9][0-9]{0,5}$ ]]; then
    echo 'ADP_GATEWAY_ROLLOUT_TIMEOUT_SECONDS must be a positive integer (seconds, at most 6 digits)' >&2
    return 1
  fi
  echo "Waiting for gateway rollout (timeout: ${timeout}s)..."
  local status=0
  kubectl rollout status deployment/bedrockgateway -n adp-gateway \
    --timeout="${timeout}s" || status=$?
  if (( status != 0 )); then
    echo "Gateway rollout failed (exit ${status}); collecting diagnostics..." >&2
    gateway_rollout_diagnostics
  fi
  return "$status"
}
