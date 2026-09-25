#!/usr/bin/env bash
#
# Apply the chat-agent ScaledJob to the dev cluster, wiring Terraform outputs
# into the REPLACE_WITH_* placeholders. Safe to re-run.
#
# Required env:
#   AWS_PROFILE         (e.g. embark2)
#   ENVIRONMENT         (e.g. dev)
#   AGENT_IMAGE         (e.g. <acct>.dkr.ecr.us-east-1.amazonaws.com/adp-chat-agent:<tag>)
#
# Optional env:
#   NAMESPACE           (default: adp-gateway-agents)
#
set -euo pipefail

NAMESPACE="${NAMESPACE:-adp-gateway-agents}"
ENVIRONMENT="${ENVIRONMENT:?ENVIRONMENT is required (e.g. dev)}"
AWS_REGION="${AWS_REGION:-us-east-1}"
ADP_CHAT_MODEL_POLICY_ENABLED="${ADP_CHAT_MODEL_POLICY_ENABLED:-false}"
[[ "$ADP_CHAT_MODEL_POLICY_ENABLED" == true || "$ADP_CHAT_MODEL_POLICY_ENABLED" == false ]] || exit 1
AGENT_IMAGE="${AGENT_IMAGE:?AGENT_IMAGE is required (full ECR URI with tag)}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST="${SCRIPT_DIR}/chat-scaledjob.yaml"
PREPULL_MANIFEST="${SCRIPT_DIR}/image-prepull-daemonset.yaml"
INFRA_DIR="${SCRIPT_DIR}/../../infra"

# Check account access before changing the ConfigMap or admitting new chat jobs.
bash "${SCRIPT_DIR}/../../../../platform/scripts/enable-bedrock-models.sh" \
  --prepare-and-verify --region "$AWS_REGION"

# Verify the selected release before any cluster mutation; both consumers use this digest.
AGENT_IMAGE=$(python3 "$SCRIPT_DIR/../../../../platform/scripts/resolve-ecr-image.py" "$AGENT_IMAGE")


echo "[deploy-chat] Reading Terraform outputs from ${INFRA_DIR}"
pushd "${INFRA_DIR}" > /dev/null

# The committed backend tfvars keeps a literal ACCOUNT_ID placeholder so the
# repo stays portable across accounts (it is rewritten by bootstrap for
# self-managed deploys, but this CI path runs from a clean checkout). Resolve
# the real state bucket at runtime and override the bucket on init — otherwise
# terraform inits against "adp-terraform-state-ACCOUNT_ID" and fails with
# NoSuchBucket (the cause of every chat-agent-deploy failure to date).
STATE_BUCKET="${STATE_BUCKET:-adp-terraform-state-$(aws sts get-caller-identity --query Account --output text)}"
echo "[deploy-chat] Using Terraform state bucket: ${STATE_BUCKET}"
terraform init \
  -backend-config="../../../environments/${ENVIRONMENT}/modules/agent-factory-backend.tfvars" \
  -backend-config="bucket=${STATE_BUCKET}" \
  -input=false -reconfigure > /dev/null

CHAT_TASKS_FIFO_URL=$(terraform output -raw chat_tasks_fifo_queue_url)
CONTEXT_TABLE=$(terraform output -raw chat_context_table_name)
ARTIFACTS_TABLE=$(terraform output -raw chat_artifacts_table_name)
MEMORY_TABLE=$(terraform output -raw agent_memory_table_name)
ARTIFACTS_BUCKET=$(terraform output -raw chat_artifacts_bucket)
RESPONSE_QUEUE_URL=$(terraform output -raw gateway_response_queue_url)
popd > /dev/null

# SIGV4_PROXY_TARGET: the chat agent routes Bedrock through the gateway's REST
# API (ADP_BEDROCK_VIA=gateway in the manifest), re-signing via a local
# sigv4-proxy. Without this substitution the manifest ships the literal
# placeholder, the proxy has no valid upstream, and the entrypoint's health check
# falls back to direct Bedrock — so chat keeps working and gateway routing is
# silently off, with nothing logged to say so.
#
# Read from SSM rather than a Terraform output because gateway-infra publishes it
# and this module does not own it.
APIGW_INVOKE_URL=$(aws ssm get-parameter \
  --name "/adp/${ENVIRONMENT}/gateway/apigw-invoke-url" \
  --region "$AWS_REGION" --query 'Parameter.Value' --output text)
if [ -n "${APIGW_INVOKE_URL}" ] && [ "${APIGW_INVOKE_URL}" != "None" ]; then
  SIGV4_PROXY_TARGET="${APIGW_INVOKE_URL}/agent"
else
  echo "[deploy-chat] Missing gateway API URL; refusing to deploy an unwired worker." >&2
  exit 1
fi

echo "[deploy-chat] Wiring manifest placeholders:"
echo "  CHAT_TASKS_FIFO_URL=${CHAT_TASKS_FIFO_URL}"
echo "  CONTEXT_TABLE=${CONTEXT_TABLE}"
echo "  ARTIFACTS_TABLE=${ARTIFACTS_TABLE}"
echo "  MEMORY_TABLE=${MEMORY_TABLE}"
echo "  ARTIFACTS_BUCKET=${ARTIFACTS_BUCKET}"
echo "  RESPONSE_QUEUE_URL=${RESPONSE_QUEUE_URL}"
echo "  AGENT_IMAGE=${AGENT_IMAGE}"
echo "  SIGV4_PROXY_TARGET=${SIGV4_PROXY_TARGET}"

kubectl create namespace "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -

if [[ "$ADP_CHAT_MODEL_POLICY_ENABLED" == true ]]; then
  kubectl apply -f "${SCRIPT_DIR}/chat-model-rbac.yaml"
fi

sed \
  -e "s|REPLACE_WITH_MODEL_CONTROL_ENDPOINT|${APIGW_INVOKE_URL}/agent/internal/v1/agent|g" \
  -e "s|REPLACE_WITH_CHAT_MODEL_POLICY_ENABLED|${ADP_CHAT_MODEL_POLICY_ENABLED}|g" \
  -e "s|REPLACE_WITH_AWS_REGION|${AWS_REGION}|g" \
  -e "s|REPLACE_WITH_CHAT_TASKS_FIFO_URL|${CHAT_TASKS_FIFO_URL}|g" \
  -e "s|REPLACE_WITH_CONTEXT_TABLE|${CONTEXT_TABLE}|g" \
  -e "s|REPLACE_WITH_ARTIFACTS_TABLE|${ARTIFACTS_TABLE}|g" \
  -e "s|REPLACE_WITH_MEMORY_TABLE|${MEMORY_TABLE}|g" \
  -e "s|REPLACE_WITH_ARTIFACTS_BUCKET|${ARTIFACTS_BUCKET}|g" \
  -e "s|REPLACE_WITH_RESPONSE_QUEUE_URL|${RESPONSE_QUEUE_URL}|g" \
  -e "s|REPLACE_WITH_GATEWAY_APIGW_INVOKE_URL|${SIGV4_PROXY_TARGET}|g" \
  -e "s|REPLACE_WITH_AGENT_IMAGE|${AGENT_IMAGE}|g" \
  "${MANIFEST}" | kubectl apply -f -

# Pre-pull DaemonSet: caches the chat-agent image on every node so KEDA pod
# spawn skips the ECR pull step (saves ~5-30s per cold pod).
echo "[deploy-chat] Applying pre-pull DaemonSet..."
sed \
  -e "s|REPLACE_WITH_AGENT_IMAGE|${AGENT_IMAGE}|g" \
  "${PREPULL_MANIFEST}" | kubectl apply -f -

echo "[deploy-chat] Applied. Verifying..."
kubectl get configmap chat-agent-config -n "${NAMESPACE}" -o name
kubectl get triggerauthentication chat-agent-aws-auth -n "${NAMESPACE}" -o name
kubectl get scaledjob chat-agent-worker -n "${NAMESPACE}" -o name
kubectl get daemonset chat-agent-image-prepull -n "${NAMESPACE}" -o name
kubectl wait --for=condition=Ready scaledjob/chat-agent-worker -n "$NAMESPACE" --timeout=300s
LIVE_IMAGE=$(kubectl get scaledjob chat-agent-worker -n "$NAMESPACE" \
  -o jsonpath='{.spec.jobTargetRef.template.spec.containers[0].image}')
[ "$LIVE_IMAGE" = "$AGENT_IMAGE" ] || { echo "[deploy-chat] Wrong release image" >&2; exit 1; }
kubectl rollout status daemonset/chat-agent-image-prepull -n "$NAMESPACE" --timeout=600s

echo "[deploy-chat] Done. Tail events with:"
echo "  kubectl get events -n ${NAMESPACE} --sort-by=.lastTimestamp | tail -20"
