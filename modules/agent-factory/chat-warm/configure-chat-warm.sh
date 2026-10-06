#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
NAMESPACE=adp-gateway-agents

cleanup_warm() {
  local status=0
  kubectl delete deployment/chat-sandbox-node-reserve daemonset/chat-sandbox-image-prepull \
    -n "$NAMESPACE" --ignore-not-found --wait=true --timeout=300s || status=1
  kubectl delete priorityclass/adp-chat-warm-reserve --ignore-not-found || status=1
  return "$status"
}

case "${CHAT_WARM_ACTION:-off}" in
  off)
    exit 0
    ;;
  enable)
    SANDBOX_IMAGE_DIGEST="$(kubectl get job adp-chat-supervisor-once -n "$NAMESPACE" -o json |
      node -e 'let data=""; process.stdin.on("data", chunk => data += chunk); process.stdin.on("end", () => { const job = JSON.parse(data); const supervisor = job.spec.template.spec.containers.find(container => container.name === "supervisor"); process.stdout.write(supervisor?.env?.find(entry => entry.name === "ADP_CHAT_SANDBOX_IMAGE")?.value ?? ""); });')"
    export SANDBOX_IMAGE_DIGEST
    rendered="$(mktemp)"
    applied=false
    cleanup_on_exit() {
      local status=$?
      if [ "$status" -ne 0 ] && [ "$applied" = true ]; then
        cleanup_warm || echo 'Chat warm cleanup incomplete; inspect warm resources before retrying' >&2
      fi
      rm -f "$rendered"
    }
    trap cleanup_on_exit EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    node "${SCRIPT_DIR}/render-chat-warm.mjs" > "$rendered"
    applied=true
    kubectl apply -f "$rendered"
    kubectl rollout status daemonset/chat-sandbox-image-prepull -n "$NAMESPACE" --timeout=600s
    kubectl rollout status deployment/chat-sandbox-node-reserve -n "$NAMESPACE" --timeout=600s
    ;;
  disable)
    cleanup_warm
    ;;
  *)
    echo 'CHAT_WARM_ACTION must be off, enable or disable' >&2
    exit 1
    ;;
esac
