#!/usr/bin/env bash
set -euo pipefail

namespace="${1:-adp-gateway}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
gateway_dir="$(cd "$script_dir/.." && pwd)"
deployment="$gateway_dir/k8s/deployment.yaml"

kubectl apply --dry-run=server --validate=false -f "$deployment" >/dev/null
echo "Gateway Deployment is admissible in ${namespace}."

python3 "$script_dir/migrate-before-rollout.py" \
  --manifest "$deployment" \
  --image "adp-gateway:restricted-admission-check" \
  --namespace "$namespace" \
  --verify-admission

if output=$(kubectl create --dry-run=server -f - 2>&1 <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: restricted-admission-probe
  namespace: ${namespace}
spec:
  restartPolicy: Never
  containers:
    - name: privileged
      image: public.ecr.aws/docker/library/busybox:1.36
      securityContext:
        privileged: true
      command: ["sh", "-c", "true"]
YAML
); then
  echo "ERROR: privileged pod was admitted to ${namespace}" >&2
  exit 1
fi

if [[ "$output" != *"violates PodSecurity"* || "$output" != *"restricted"* ]]; then
  echo "ERROR: admission probe failed for a reason other than restricted Pod Security enforcement" >&2
  echo "$output" >&2
  exit 1
fi

echo "Restricted Pod Security Admission rejected the privileged probe in ${namespace}."
