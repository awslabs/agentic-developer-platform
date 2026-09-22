#!/usr/bin/env bash
set -euo pipefail

kubectl_command="${KUBECTL:-kubectl}"
context="$("${kubectl_command}" config current-context)"
server="$("${kubectl_command}" config view --minify -o jsonpath='{.clusters[0].cluster.server}')"

case "${context}" in
  kind-*|k3d-*|minikube) ;;
  *)
    printf 'Refusing integration fixture: context %q is not a local test cluster.\n' "${context}" >&2
    exit 2
    ;;
esac

case "${server}" in
  https://127.0.0.1:*|https://localhost:*|https://\[::1\]:*) ;;
  *)
    printf 'Refusing integration fixture: API server is not loopback.\n' >&2
    exit 2
    ;;
esac

manifest="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/integration-test.yaml"
namespace="superplane-integration-test"

"${kubectl_command}" --context "${context}" apply -f "${manifest}"
"${kubectl_command}" --context "${context}" --namespace "${namespace}" wait \
  --for=condition=complete --timeout=180s job/superplane-integration-test-secret-init
"${kubectl_command}" --context "${context}" --namespace "${namespace}" wait \
  --for=condition=complete --timeout=300s job/superplane-integration-test-db-migrate
"${kubectl_command}" --context "${context}" --namespace "${namespace}" wait \
  --for=condition=complete --timeout=180s job/superplane-integration-test-db-seed
"${kubectl_command}" --context "${context}" --namespace "${namespace}" rollout status \
  --timeout=300s deployment/superplane-integration-test-api
