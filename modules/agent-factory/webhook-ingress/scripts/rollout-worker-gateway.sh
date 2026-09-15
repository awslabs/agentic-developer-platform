#!/usr/bin/env bash
# Terraform invokes this only after its authority ConfigMap/signing resources
# are available. Environment variables come from the reviewed Terraform plan.
set -euo pipefail
scratch="$(mktemp -d "${TMPDIR:-/tmp}/adp-worker-gateway.XXXXXX")"
trap 'rm -rf "$scratch"' EXIT
export KUBECONFIG="$scratch/kubeconfig"
aws eks update-kubeconfig --name "${ADP_CLUSTER:?}" --region "${ADP_REGION:?}" --kubeconfig "$KUBECONFIG" >/dev/null
refs=$(kubectl --request-timeout=30s get deployment bedrockgateway -n "${ADP_NAMESPACE:?}" \
  -o 'jsonpath={.spec.template.spec.containers[?(@.name=="bedrockgateway")].envFrom[*].configMapRef.name}')
if [[ " $refs " != *" adp-worker-authority-config "* ]]; then
  if [[ "${ADP_AUTHORITY_ENABLED:?}" == true ]]; then
    echo "Gateway deployment must consume Terraform's worker authority ConfigMap before activation" >&2
    exit 1
  fi
  echo "Worker prerequisites prepared; deploy the compatible gateway template before activation."
  exit 0
fi
kubectl rollout restart deployment/bedrockgateway -n "$ADP_NAMESPACE"
kubectl rollout status deployment/bedrockgateway -n "$ADP_NAMESPACE" --timeout=300s
