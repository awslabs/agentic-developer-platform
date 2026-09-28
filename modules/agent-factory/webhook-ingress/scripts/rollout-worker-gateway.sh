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
if [[ "$ADP_AUTHORITY_ENABLED" == true ]]; then
  # Check references and nonempty key names without emitting secret values.
  # An old gateway template may consume
  # the ConfigMap but lack the keys needed by the mediated run services.
  secret_refs=$(kubectl --request-timeout=30s get deployment bedrockgateway -n "$ADP_NAMESPACE" \
    -o 'jsonpath={range .spec.template.spec.containers[?(@.name=="bedrockgateway")].env[*]}{.name}={.valueFrom.secretKeyRef.name}/{.valueFrom.secretKeyRef.key}{"\n"}{end}')
  for required in \
    'AGENT_RUN_CREDENTIAL_KEY=agent-authority-signing/run-credential-key' \
    'AGENT_CONTROL_ENVELOPE_SIGNING_KEY=agent-authority-signing/envelope-signing-key' \
    'ADP_MARKER_SIGNING_KEY=agent-run-services/marker-signing-key' \
    'ADP_DOOR_SERVICE_KEY=bedrockgateway-secrets/internal-api-key'; do
    if ! grep -Fqx -- "$required" <<< "$secret_refs"; then
      echo "Gateway deployment is missing run-service secret reference: ${required%%=*}" >&2
      exit 1
    fi
    secret_path="${required#*=}"
    secret_name="${secret_path%%/*}"
    secret_key="${secret_path#*/}"
    # Optional env references let a pod become Ready even when a key is absent.
    # The Kubernetes client formats the response to names of nonempty entries;
    # neither the shell nor its logs receive the Secret data values.
    available_keys=$(kubectl --request-timeout=30s get secret "$secret_name" -n "$ADP_NAMESPACE" \
      -o 'go-template={{range $key, $value := .data}}{{if $value}}{{$key}}{{"\n"}}{{end}}{{end}}')
    if ! grep -Fqx -- "$secret_key" <<< "$available_keys"; then
      echo "Gateway run-service secret key is missing or empty: $secret_name/$secret_key" >&2
      exit 1
    fi
  done
fi
kubectl rollout restart deployment/bedrockgateway -n "$ADP_NAMESPACE"
# A four-replica rollout can replace pods serially while EKS provisions new
# nodes and pulls the gateway plus instrumentation images. Keep the gate bounded,
# but allow the observed node-startup time without tainting Terraform mid-rollout.
kubectl rollout status deployment/bedrockgateway -n "$ADP_NAMESPACE" --timeout=900s
