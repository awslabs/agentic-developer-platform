#!/usr/bin/env bash
#
# Apply the chat-agent ScaledJob to the dev cluster, wiring Terraform outputs
# into the REPLACE_WITH_* placeholders. Safe to re-run.
#
# Required env:
#   AWS_PROFILE         (e.g. embark2)
#   ENVIRONMENT         (e.g. dev)
#   AGENT_IMAGE         (e.g. <acct>.dkr.ecr.us-east-1.amazonaws.com/adp-agent-gateway:<tag>)
#
# Optional env:
#   NAMESPACE           (default: adp-gateway-agents)
#
set -euo pipefail

NAMESPACE="${NAMESPACE:-adp-gateway-agents}"
ENVIRONMENT="${ENVIRONMENT:?ENVIRONMENT is required (e.g. dev)}"
AGENT_IMAGE="${AGENT_IMAGE:?AGENT_IMAGE is required (full ECR URI with tag)}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST="${SCRIPT_DIR}/chat-scaledjob.yaml"
INFRA_DIR="${SCRIPT_DIR}/../../infra"

echo "[deploy-chat] Reading Terraform outputs from ${INFRA_DIR}"
pushd "${INFRA_DIR}" > /dev/null
terraform init -backend-config="../../../environments/${ENVIRONMENT}/modules/agent-factory-backend.tfvars" -input=false -reconfigure > /dev/null

CHAT_TASKS_FIFO_URL=$(terraform output -raw chat_tasks_fifo_queue_url)
CONTEXT_TABLE=$(terraform output -raw chat_context_table_name)
ARTIFACTS_TABLE=$(terraform output -raw chat_artifacts_table_name)
MEMORY_TABLE=$(terraform output -raw agent_memory_table_name)
ARTIFACTS_BUCKET=$(terraform output -raw chat_artifacts_bucket)
RESPONSE_QUEUE_URL=$(terraform output -raw gateway_response_queue_url)
popd > /dev/null

echo "[deploy-chat] Wiring manifest placeholders:"
echo "  CHAT_TASKS_FIFO_URL=${CHAT_TASKS_FIFO_URL}"
echo "  CONTEXT_TABLE=${CONTEXT_TABLE}"
echo "  ARTIFACTS_TABLE=${ARTIFACTS_TABLE}"
echo "  MEMORY_TABLE=${MEMORY_TABLE}"
echo "  ARTIFACTS_BUCKET=${ARTIFACTS_BUCKET}"
echo "  RESPONSE_QUEUE_URL=${RESPONSE_QUEUE_URL}"
echo "  AGENT_IMAGE=${AGENT_IMAGE}"

kubectl create namespace "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -

sed \
  -e "s|REPLACE_WITH_CHAT_TASKS_FIFO_URL|${CHAT_TASKS_FIFO_URL}|g" \
  -e "s|REPLACE_WITH_CONTEXT_TABLE|${CONTEXT_TABLE}|g" \
  -e "s|REPLACE_WITH_ARTIFACTS_TABLE|${ARTIFACTS_TABLE}|g" \
  -e "s|REPLACE_WITH_MEMORY_TABLE|${MEMORY_TABLE}|g" \
  -e "s|REPLACE_WITH_ARTIFACTS_BUCKET|${ARTIFACTS_BUCKET}|g" \
  -e "s|REPLACE_WITH_RESPONSE_QUEUE_URL|${RESPONSE_QUEUE_URL}|g" \
  -e "s|REPLACE_WITH_AGENT_IMAGE|${AGENT_IMAGE}|g" \
  "${MANIFEST}" | kubectl apply -f -

echo "[deploy-chat] Applied. Verifying..."
kubectl get configmap chat-agent-config -n "${NAMESPACE}" -o name
kubectl get triggerauthentication chat-agent-aws-auth -n "${NAMESPACE}" -o name
kubectl get scaledjob chat-agent-worker -n "${NAMESPACE}" -o name

echo "[deploy-chat] Done. Tail events with:"
echo "  kubectl get events -n ${NAMESPACE} --sort-by=.lastTimestamp | tail -20"
