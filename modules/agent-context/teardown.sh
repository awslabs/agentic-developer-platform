#!/usr/bin/env bash
# Teardown the Agent Context Intelligence Platform
# Usage: ./teardown.sh [--delete-namespace] [--delete-pvcs]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source configuration
source "${SCRIPT_DIR}/config.env"
[[ -f "${SCRIPT_DIR}/config.local.env" ]] && source "${SCRIPT_DIR}/config.local.env"

DELETE_NAMESPACE=false
DELETE_PVCS=false

while [[ $# -gt 0 ]]; do
  case $1 in
    --delete-namespace)
      DELETE_NAMESPACE=true
      shift
      ;;
    --delete-pvcs)
      DELETE_PVCS=true
      shift
      ;;
    --help)
      echo "Usage: ./teardown.sh [--delete-namespace] [--delete-pvcs]"
      echo ""
      echo "By default, only deployments/services/configmaps/cronjobs are removed."
      echo "  --delete-namespace  Delete the entire namespace (removes ALL resources including PVCs)"
      echo "  --delete-pvcs       Delete PVCs (data will be lost)"
      exit 0
      ;;
    *)
      echo "Unknown option: $1"
      exit 1
      ;;
  esac
done

echo "============================================"
echo "Agent Context Platform - Teardown"
echo "============================================"
echo "Namespace: ${NAMESPACE}"
echo "Delete namespace: ${DELETE_NAMESPACE}"
echo "Delete PVCs: ${DELETE_PVCS}"
echo "============================================"

if [ "${DELETE_NAMESPACE}" = "true" ]; then
  echo ""
  echo "Deleting entire namespace '${NAMESPACE}'..."
  kubectl delete namespace "${NAMESPACE}" --timeout=120s 2>/dev/null || true
  echo "Namespace deleted."
else
  echo ""
  echo "Removing deployments..."
  for DEPLOY in litellm-proxy sourcebot sourcebot-postgres sourcebot-redis openviking-server deepwiki; do
    kubectl delete deploy "${DEPLOY}" -n "${NAMESPACE}" --timeout=60s 2>/dev/null || true
  done
  # Legacy: remove CodeGraphContext pod if it still exists (removed in #105)
  kubectl delete deploy codegraph-context -n "${NAMESPACE}" --timeout=60s 2>/dev/null || true

  echo "Removing services..."
  for SVC in litellm-proxy sourcebot sourcebot-postgres sourcebot-redis openviking deepwiki; do
    kubectl delete svc "${SVC}" -n "${NAMESPACE}" --timeout=30s 2>/dev/null || true
  done
  # Legacy: remove CodeGraphContext service
  kubectl delete svc codegraph -n "${NAMESPACE}" --timeout=30s 2>/dev/null || true

  echo "Removing CronJobs and ScaledJobs..."
  kubectl delete cronjob sourcebot-token-refresh -n "${NAMESPACE}" 2>/dev/null || true
  kubectl delete cronjob ingestion-refresh -n "${NAMESPACE}" 2>/dev/null || true
  # Legacy CronJob
  kubectl delete cronjob openviking-repo-refresh -n "${NAMESPACE}" 2>/dev/null || true
  # KEDA ScaledJob for parallel ingestion
  kubectl delete scaledjob ingestion-worker -n "${NAMESPACE}" 2>/dev/null || true
  kubectl delete triggerauthentication keda-aws-auth -n "${NAMESPACE}" 2>/dev/null || true
  # Clean up any running ingestion worker jobs
  kubectl delete jobs -l app=ingestion-worker -n "${NAMESPACE}" 2>/dev/null || true

  echo "Removing RBAC..."
  kubectl delete role sourcebot-token-refresh -n "${NAMESPACE}" 2>/dev/null || true
  kubectl delete rolebinding sourcebot-token-refresh -n "${NAMESPACE}" 2>/dev/null || true

  echo "Removing ConfigMaps..."
  for CM in openviking-config sourcebot-config github-app-token-script deepwiki-config ingestion-content-config agent-context-refresh-scripts; do
    kubectl delete configmap "${CM}" -n "${NAMESPACE}" 2>/dev/null || true
  done

  echo "Removing DeepWiki indexing jobs..."
  kubectl delete jobs -l app=deepwiki-index -n "${NAMESPACE}" 2>/dev/null || true

  if [ "${DELETE_PVCS}" = "true" ]; then
    echo "Removing EBS PVCs (data will be lost)..."
    # NOTE: codegraph-data PVC is orphaned (CodeGraphContext removed in #105)
    for PVC in openviking-data sourcebot-data sourcebot-postgres-data sourcebot-redis-data codegraph-data; do
      kubectl delete pvc "${PVC}" -n "${NAMESPACE}" --timeout=60s 2>/dev/null || true
    done

    echo "Removing S3 Files PVC + PV (S3 data is preserved)..."
    kubectl delete pvc platform-data -n "${NAMESPACE}" --timeout=60s 2>/dev/null || true
    kubectl delete pv agent-context-s3-files --timeout=60s 2>/dev/null || true
    kubectl delete storageclass s3-files 2>/dev/null || true
    echo "  NOTE: S3 bucket data is preserved. To fully destroy, run:"
    echo "    cd context_management/agent-context/terraform && terraform destroy"
  fi

  # Destroy Terraform infrastructure (SQS, DynamoDB, Neptune, OpenSearch)
  echo ""
  echo "Destroying Terraform infrastructure..."
  if [ -d "${SCRIPT_DIR}/terraform" ]; then
    cd "${SCRIPT_DIR}/terraform"
    # Destroy SQS + DynamoDB (always created)
    terraform destroy -auto-approve \
      -target=module.sqs_ingestion \
      -target=module.dynamodb_state \
      2>/dev/null || echo "WARNING: SQS/DynamoDB Terraform destroy failed"
    # Destroy GraphRAG if enabled
    if [ "${GRAPHRAG_ENABLED:-false}" = "true" ]; then
      terraform destroy -auto-approve -var="graphrag_enabled=true" \
        -target=module.neptune_serverless \
        -target=module.opensearch_serverless \
        2>/dev/null || echo "WARNING: GraphRAG Terraform destroy failed"
    fi
    cd "${SCRIPT_DIR}"
  fi
fi

echo ""
echo "============================================"
echo "Teardown complete."
echo "============================================"
