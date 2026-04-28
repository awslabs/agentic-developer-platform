#!/usr/bin/env bash
# =============================================================================
# Deploy script — Cyber EKS + KEDA + SQS + DDB + ScaledJobs
# =============================================================================
# Issue #230: Deploys infrastructure via Terraform, configures kubectl for the
# cyber cluster, and applies K8s manifests with placeholder substitution.
#
# Usage: ./deploy.sh [--smoke-test]
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODULE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
INFRA_DIR="${MODULE_DIR}/infra"
K8S_DIR="${MODULE_DIR}/k8s"

AWS_REGION="${AWS_REGION:-us-east-1}"
ENVIRONMENT="${ENVIRONMENT:-dev}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

echo "=== Cyber Infrastructure Deploy ==="
echo "Account:     ${ACCOUNT_ID}"
echo "Region:      ${AWS_REGION}"
echo "Environment: ${ENVIRONMENT}"
echo ""

# ---------------------------------------------------------------------------
# Step 1: Terraform apply
# ---------------------------------------------------------------------------
echo "--- Step 1: Terraform apply ---"
cd "${INFRA_DIR}"

terraform init \
  -backend-config="bucket=adp-terraform-state-${ACCOUNT_ID}" \
  -backend-config="key=${ENVIRONMENT}/modules/cyber-sandbox/terraform.tfstate" \
  -backend-config="region=${AWS_REGION}" \
  -backend-config="encrypt=true" \
  -backend-config="dynamodb_table=adp-terraform-locks" \
  -input=false

terraform apply -var-file=terraform.tfvars -auto-approve

# Capture outputs
CLUSTER_NAME=$(terraform output -raw cyber_cluster_name)
TRIAGE_QUEUE_URL=$(terraform output -raw cyber_triage_tasks_queue_url)
STATIC_QUEUE_URL=$(terraform output -raw cyber_static_tasks_queue_url)
TRIAGE_RESP_URL=$(terraform output -raw cyber_triage_responses_queue_url)
STATIC_RESP_URL=$(terraform output -raw cyber_static_responses_queue_url)
RESULTS_TABLE=$(terraform output -raw cyber_analysis_results_table_name)

echo "Cluster:       ${CLUSTER_NAME}"
echo "Triage queue:  ${TRIAGE_QUEUE_URL}"
echo "Static queue:  ${STATIC_QUEUE_URL}"
echo "Results table: ${RESULTS_TABLE}"
echo ""

# ---------------------------------------------------------------------------
# Step 2: Configure kubectl for the cyber cluster
# ---------------------------------------------------------------------------
echo "--- Step 2: Configure kubectl ---"
aws eks update-kubeconfig --name "${CLUSTER_NAME}" --region "${AWS_REGION}"

echo "Waiting for nodes..."
for i in $(seq 1 20); do
  NODE_COUNT=$(kubectl get nodes --no-headers 2>/dev/null | wc -l)
  if [ "$NODE_COUNT" -gt 0 ]; then
    echo "Nodes ready: ${NODE_COUNT}"
    break
  fi
  echo "  Waiting for EKS Auto Mode nodes... (${i}/20)"
  sleep 15
done

# ---------------------------------------------------------------------------
# Step 3: Apply K8s manifests with substitutions
# ---------------------------------------------------------------------------
echo "--- Step 3: Apply K8s manifests ---"
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
IMAGE="${REGISTRY}/adp-cyber-worker:latest"

for manifest in "${K8S_DIR}"/*.yaml; do
  echo "Applying: $(basename "$manifest")"
  sed \
    -e "s|REPLACE_WITH_CYBER_WORKER_IMAGE|${IMAGE}|g" \
    -e "s|REPLACE_WITH_TRIAGE_TASKS_QUEUE_URL|${TRIAGE_QUEUE_URL}|g" \
    -e "s|REPLACE_WITH_TRIAGE_RESPONSES_QUEUE_URL|${TRIAGE_RESP_URL}|g" \
    -e "s|REPLACE_WITH_STATIC_TASKS_QUEUE_URL|${STATIC_QUEUE_URL}|g" \
    -e "s|REPLACE_WITH_STATIC_RESPONSES_QUEUE_URL|${STATIC_RESP_URL}|g" \
    -e "s|REPLACE_WITH_RESULTS_TABLE|${RESULTS_TABLE}|g" \
    "$manifest" | kubectl apply -f -
done

echo ""
echo "--- Verification ---"
echo "ScaledJobs:"
kubectl get scaledjobs -n cyber-workers 2>/dev/null || echo "  (none yet)"
echo ""
echo "KEDA pods:"
kubectl get pods -n keda 2>/dev/null || echo "  (none yet)"
echo ""

# ---------------------------------------------------------------------------
# Step 4: Smoke test (optional, requires worker image from #231)
# ---------------------------------------------------------------------------
if [ "${1:-}" = "--smoke-test" ]; then
  echo "--- Step 4: Smoke test ---"
  ARTIFACT_ID="smoke-test-$(date +%s)"

  echo "Sending test message to triage queue..."
  aws sqs send-message \
    --queue-url "${TRIAGE_QUEUE_URL}" \
    --message-body "{\"artifact_id\": \"${ARTIFACT_ID}\", \"org_id\": \"test-org\"}" \
    --message-group-id "${ARTIFACT_ID}" \
    --message-deduplication-id "${ARTIFACT_ID}" \
    --region "${AWS_REGION}"

  echo "Waiting for worker pod (30s timeout)..."
  for i in $(seq 1 6); do
    POD_COUNT=$(kubectl get pods -n cyber-workers --no-headers 2>/dev/null | grep -c "Running\|Completed" || true)
    if [ "$POD_COUNT" -gt 0 ]; then
      echo "Worker pod detected!"
      kubectl get pods -n cyber-workers
      break
    fi
    sleep 5
  done

  echo "Checking DynamoDB for result..."
  aws dynamodb get-item \
    --table-name "${RESULTS_TABLE}" \
    --key "{\"artifact_id\": {\"S\": \"${ARTIFACT_ID}\"}, \"stage_timestamp\": {\"S\": \"triage#\"}}" \
    --region "${AWS_REGION}" 2>/dev/null || echo "  (not found yet — expected if #231 not merged)"

  echo ""
  echo "Smoke test done. If no pod spawned, #231 (worker image) hasn't merged yet."
fi

echo ""
echo "=== Deploy complete ==="
