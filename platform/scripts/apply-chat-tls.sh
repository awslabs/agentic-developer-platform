#!/usr/bin/env bash
# =============================================================================
# apply-chat-tls.sh — provision the restricted chat sandbox gateway endpoint
# =============================================================================
# Idempotent operator script behind `deploy-all.sh --chat delegated` (#7899).
#   1. Issuer material comes from Secrets Manager
#      `adp/<env>/chat-sandbox-gateway/issuer` (JSON: ca_cert, ca_key, leaf_cert,
#      leaf_key). When absent, chat-tls-issuer.py generates a dev-only root CA
#      and leaf (SAN exactly chat-sandbox-gateway.adp-gateway.svc) and stores
#      them there, described as operator-owned and NOT for production.
#   2. The current bedrockgateway Deployment is captured as JSON.
#   3. render-chat-tls.py renders Role/RoleBinding + Secret + immutable CA
#      ConfigMap + Deployment/Service/NetworkPolicy to a private 0600 file.
#   4. The rendered objects are applied (server-side, so the TLS key is not
#      copied into a last-applied annotation) and the listener rollout awaited.
#   5. The CA ConfigMap name is published to SSM
#      /adp/<env>/gateway/chat-sandbox-ca-configmap for both gateway renderers.
#   6. The name is printed on stdout (progress goes to stderr).
# Key material only ever lives in a private temporary directory that is removed
# on exit; it is never printed or logged.
#
# Usage: apply-chat-tls.sh --env ENV --region REGION [--deployment-json FILE]
#                          [--skip-rollout-wait]
# =============================================================================
set -euo pipefail
set +x

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENVIRONMENT="${ENVIRONMENT:-${ADP_ENVIRONMENT:-}}"
AWS_REGION="${AWS_REGION:-${ADP_REGION:-}}"
DEPLOYMENT_JSON=""
WAIT_ROLLOUT=true
while [ "$#" -gt 0 ]; do
  case "$1" in
    --env) ENVIRONMENT="${2:?--env requires a value}"; shift ;;
    --region) AWS_REGION="${2:?--region requires a value}"; shift ;;
    --deployment-json) DEPLOYMENT_JSON="${2:?--deployment-json requires a file}"; shift ;;
    --skip-rollout-wait) WAIT_ROLLOUT=false ;;
    --help)
      sed -n '2,/^# ====*$/p' "$0" | sed -n '3,$p' | sed 's/^# \{0,1\}//' >&2
      exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
[[ "$ENVIRONMENT" =~ ^[a-z][a-z0-9-]{1,20}$ ]] || { echo "apply-chat-tls: --env must name an ADP environment" >&2; exit 2; }
[[ "$AWS_REGION" =~ ^[a-z]{2}(-gov)?-[a-z]+-[0-9]$ ]] || { echo "apply-chat-tls: --region must be an AWS region" >&2; exit 2; }
export AWS_REGION AWS_DEFAULT_REGION="$AWS_REGION"

GATEWAY_NS="adp-gateway"
SANDBOX_NS="adp-gateway-agents"
SECRET_ID="adp/${ENVIRONMENT}/chat-sandbox-gateway/issuer"
PARAMETER="/adp/${ENVIRONMENT}/gateway/chat-sandbox-ca-configmap"

log() { echo "[apply-chat-tls] $*" >&2; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/adp-chat-tls.XXXXXX")"
chmod 700 "$WORK"
trap 'rm -rf "$WORK"' EXIT

kubectl get namespace "$GATEWAY_NS" -o name >/dev/null
kubectl get namespace "$SANDBOX_NS" -o name >/dev/null

# 1. Issuer material (fetched or generated; validated by the renderer's rules).
ISSUER_INFO="$(python3 "$SCRIPT_DIR/chat-tls-issuer.py" ensure --secret-id "$SECRET_ID" \
  --region "$AWS_REGION" --environment "$ENVIRONMENT" --output-dir "$WORK")"
log "$ISSUER_INFO (secret $SECRET_ID)"

# 2. Current ordinary gateway Deployment as the rendering baseline.
if [ -n "$DEPLOYMENT_JSON" ]; then
  cp -f "$DEPLOYMENT_JSON" "$WORK/deployment.json"
else
  kubectl get deployment bedrockgateway -n "$GATEWAY_NS" -o json > "$WORK/deployment.json"
fi

# 3. Render (never applies; output is created exclusively with mode 0600).
python3 "$SCRIPT_DIR/render-chat-tls.py" --deployment-json "$WORK/deployment.json" \
  --certificate "$WORK/leaf.pem" --private-key "$WORK/key.pem" --ca "$WORK/ca.pem" \
  --output "$WORK/manifest.json" >&2
CA_CONFIGMAP="$(python3 - "$WORK/manifest.json" <<'PY'
import json, re, sys
items = json.load(open(sys.argv[1]))["items"]
names = [i["metadata"]["name"] for i in items if i["kind"] == "ConfigMap"
         and i["metadata"]["namespace"] == "adp-gateway-agents"]
if len(names) != 1 or not re.fullmatch(r"chat-sandbox-gateway-ca-[a-f0-9]{16}", names[0]):
    sys.exit("Rendered manifest must contain exactly one sandbox CA ConfigMap")
print(names[0])
PY
)"

# 4. Apply. Server-side apply keeps the Secret's data out of client annotations.
kubectl apply --server-side --force-conflicts -f "$WORK/manifest.json" >&2
if [ "$WAIT_ROLLOUT" = true ]; then
  kubectl rollout status deployment/chat-sandbox-gateway -n "$GATEWAY_NS" --timeout=300s >&2
fi
SERVICE_IP="$(kubectl get service chat-sandbox-gateway -n "$GATEWAY_NS" -o jsonpath='{.spec.clusterIP}')"
[ -n "$SERVICE_IP" ] || { echo "apply-chat-tls: Service chat-sandbox-gateway has no ClusterIP" >&2; exit 1; }

# 5. Publish the CA ConfigMap name for the gateway ConfigMap renderers.
CURRENT="$(aws ssm get-parameter --name "$PARAMETER" --query Parameter.Value --output text 2>/dev/null || echo "")"
if [ "$CURRENT" != "$CA_CONFIGMAP" ]; then
  aws ssm put-parameter --name "$PARAMETER" --type String --value "$CA_CONFIGMAP" --overwrite >/dev/null
  log "published $PARAMETER=$CA_CONFIGMAP"
else
  log "$PARAMETER already names $CA_CONFIGMAP"
fi

# 6. The only stdout line: the CA ConfigMap name.
echo "$CA_CONFIGMAP"
