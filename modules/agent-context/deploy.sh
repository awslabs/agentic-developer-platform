#!/usr/bin/env bash
# Deploy the Agent Context Intelligence Platform
# Usage: ./deploy.sh [--config config.local.env]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source configuration
source "${SCRIPT_DIR}/config.env"
[[ -f "${SCRIPT_DIR}/config.local.env" ]] && source "${SCRIPT_DIR}/config.local.env"

# Parse arguments
PERSONAL_CONTEXT_ONLY="${PERSONAL_CONTEXT_ONLY:-false}"
while [[ $# -gt 0 ]]; do
  case $1 in
    --config)
      source "$2"
      shift 2
      ;;
    --personal-context-only)
      PERSONAL_CONTEXT_ONLY=true
      shift
      ;;
    --skip-validate)
      SKIP_VALIDATE=true
      shift
      ;;
    --help)
      echo "Usage: ./deploy.sh [--config config.local.env] [--personal-context-only] [--skip-validate]"
      echo ""
      echo "Options:"
      echo "  --config <file>           Source additional config overrides"
      echo "  --personal-context-only   Deploy lean stack only (LiteLLM + Context MCP +"
      echo "                            synthesis CronJob + S3 Vectors). Skips DeepWiki,"
      echo "                            ingestion pipeline, and OpenSearch."
      echo "                            Cost: ~\$80/mo vs ~\$800/mo full stack."
      echo "  --skip-validate           Skip post-deployment validation"
      exit 0
      ;;
    *)
      echo "Unknown option: $1"
      exit 1
      ;;
  esac
done

echo "============================================"
echo "Agent Context Intelligence Platform Deploy"
echo "============================================"
echo "Cluster:    ${CLUSTER_NAME}"
echo "Region:     ${AWS_REGION}"
echo "Namespace:  ${NAMESPACE}"
if [ "${PERSONAL_CONTEXT_ONLY}" = "true" ]; then
  echo "Mode:       PERSONAL-CONTEXT-ONLY (lean stack)"
  echo "            Deploys: LiteLLM, Context MCP, Synthesis CronJob, S3 Vectors"
  echo "            Skips:   DeepWiki, OpenSearch, Ingestion pipeline"
  echo "            Cost:    ~\$280/mo (vs ~\$800/mo full stack)"
fi
echo "============================================"

# Configure kubectl
echo ""
echo "Configuring kubectl..."
aws eks update-kubeconfig --name "${CLUSTER_NAME}" --region "${AWS_REGION}"

# Ensure namespace exists
echo "Ensuring namespace '${NAMESPACE}' exists..."
kubectl create namespace "${NAMESPACE}" 2>/dev/null || true

# Ensure ebs-gp3 StorageClass exists (EKS Auto Mode only provides gp2 by default)
echo "Ensuring ebs-gp3 StorageClass exists..."
if ! kubectl get storageclass ebs-gp3 >/dev/null 2>&1; then
  kubectl apply -f "${SCRIPT_DIR}/kubernetes/storageclass-ebs-gp3.yaml"
  echo "  Created ebs-gp3 StorageClass"
else
  echo "  ebs-gp3 StorageClass already exists"
fi

# Deploy S3 Files storage (S3-backed persistent volumes via EFS CSI driver)
# Creates S3 bucket, EFS file system, mount targets, and K8s PV/PVC.
# Idempotent — safe to run repeatedly. Requires Terraform + AWS credentials.
if [ "${S3_FILES_ENABLED:-true}" = "true" ] && [ -f "${SCRIPT_DIR}/scripts/deploy-s3-files.sh" ]; then
  echo ""
  echo "Deploying S3 Files storage infrastructure..."
  bash "${SCRIPT_DIR}/scripts/deploy-s3-files.sh" || {
    echo "WARNING: S3 Files deployment failed. Falling back to EBS PVCs."
    if [ -f "${SCRIPT_DIR}/kubernetes/pvcs.yaml" ]; then
      kubectl apply -f "${SCRIPT_DIR}/kubernetes/pvcs.yaml"
    fi
  }
else
  # Fallback: use EBS PVCs (legacy mode or S3 Files disabled)
  echo "Ensuring PVCs exist (EBS mode)..."
  if [ -f "${SCRIPT_DIR}/kubernetes/pvcs.yaml" ]; then
    kubectl apply -f "${SCRIPT_DIR}/kubernetes/pvcs.yaml"
  elif [ -f "${SCRIPT_DIR}/manifests/pvcs.yaml" ]; then
    kubectl apply -f "${SCRIPT_DIR}/manifests/pvcs.yaml"
  fi
fi

# Ensure service account exists (template IRSA role ARN)
if [ -f "${SCRIPT_DIR}/kubernetes/serviceaccount.yaml" ]; then
  # If IRSA_ROLE_ARN is not set but Terraform outputs are available, read it
  if [ -z "${IRSA_ROLE_ARN:-}" ] && [ -d "${SCRIPT_DIR}/terraform" ]; then
    IRSA_ROLE_ARN=$(cd "${SCRIPT_DIR}/terraform" && terraform output -raw irsa_role_arn 2>/dev/null || echo "")
  fi
  export IRSA_ROLE_ARN
  source "${SCRIPT_DIR}/scripts/_common.sh"
  template_file "${SCRIPT_DIR}/kubernetes/serviceaccount.yaml" | kubectl apply -f -
  if [ -n "${IRSA_ROLE_ARN:-}" ]; then
    echo "  Service account annotated with IRSA role: ${IRSA_ROLE_ARN}"
  else
    echo "  WARNING: IRSA_ROLE_ARN not set. Service account has no IRSA annotation."
  fi
fi

# Issue #2433: If NEPTUNE_ENDPOINT is still empty after config.env, try SSM
# (endpoint may have been published by a prior infra-apply).
if [ -z "${NEPTUNE_ENDPOINT:-}" ]; then
  NEPTUNE_ENDPOINT=$(aws ssm get-parameter \
    --name "/adp/${ENVIRONMENT:-dev}/agent-context/neptune-endpoint" \
    --query "Parameter.Value" --output text 2>/dev/null || echo "")
  NEPTUNE_PORT=$(aws ssm get-parameter \
    --name "/adp/${ENVIRONMENT:-dev}/agent-context/neptune-port" \
    --query "Parameter.Value" --output text 2>/dev/null || echo "${NEPTUNE_PORT:-8182}")
fi

# Deploy centralized ConfigMap (single source of truth for all non-secret config)
echo ""
echo "Deploying agent-context-config ConfigMap..."
source "${SCRIPT_DIR}/scripts/_common.sh"
export NAMESPACE AWS_REGION SQS_QUEUE_URL DYNAMO_TABLE DEEPWIKI_ENABLED
export GRAPHRAG_ENABLED NEPTUNE_ENDPOINT NEPTUNE_PORT OPENSEARCH_ENDPOINT
export WIKI_LLM_MODEL GITHUB_APP_ID_SECRET GITHUB_APP_KEY_SECRET GITHUB_APP_OWNER
export S3_VECTORS_BUCKET_NAME S3_VECTORS_REGION S3_FILES_BUCKET S3_CONTENT_PREFIX ZOEKT_URL
template_file "${SCRIPT_DIR}/manifests/agent-context-configmap.yaml" | kubectl apply -f -
echo "  ConfigMap agent-context-config deployed"

# Apply ingestion pipeline RBAC (cross-namespace access for runner SA)
echo ""
echo "Applying ingestion pipeline RBAC..."
export RUNNER_NAMESPACE RUNNER_SERVICE_ACCOUNT
template_file "${SCRIPT_DIR}/manifests/ingestion-rbac.yaml" | kubectl apply -f -
echo "  Role: ingestion-pipeline-role (ns: ${NAMESPACE})"
echo "  Binding: ${RUNNER_NAMESPACE}:${RUNNER_SERVICE_ACCOUNT} -> ingestion-pipeline-role"

# Set DEEPWIKI_ENABLED repo variable so the workflow can gate DeepWiki steps
if command -v gh &>/dev/null && [ "${DEEPWIKI_ENABLED:-true}" = "true" ]; then
  echo ""
  echo "Setting DEEPWIKI_ENABLED repo variable..."
  gh variable set DEEPWIKI_ENABLED --body "true" --repo "${GITHUB_REPO}" 2>/dev/null \
    && echo "  Set DEEPWIKI_ENABLED=true on ${GITHUB_REPO}" \
    || echo "  WARNING: Could not set repo variable (gh auth may not be configured)"
fi

# Set RUNNER_LABEL repo variable so the workflow uses the correct runner
if command -v gh &>/dev/null; then
  gh variable set RUNNER_LABEL --body "${RUNNER_LABEL}" --repo "${GITHUB_REPO}" 2>/dev/null \
    && echo "  Set RUNNER_LABEL=${RUNNER_LABEL} on ${GITHUB_REPO}" \
    || echo "  WARNING: Could not set RUNNER_LABEL repo variable"
fi

echo ""
echo "============================================"
echo "Deploying components..."
echo "============================================"

# Deploy LiteLLM Proxy (embeddings + VLM)
echo ""
bash "${SCRIPT_DIR}/scripts/deploy-litellm-proxy.sh"

# Deploy Context MCP Server (the Door — verb surface for the Knowledge Layer)
# Deployed in BOTH modes (full stack + personal-context-only) since it serves
# remember/experience verbs used by personal context, and all 6 verbs in full mode.
echo ""
echo "Deploying Context MCP Server (Door)..."
source "${SCRIPT_DIR}/scripts/_common.sh"

# Resolve image: use ECR-built image if available, else default from config.env
if [ -z "${CONTEXT_MCP_IMAGE:-}" ] || [ "${CONTEXT_MCP_IMAGE}" = "python:3.11-slim" ]; then
  CONTEXT_MCP_REPO="adp-${ENVIRONMENT:-dev}-agent-context-context-mcp"
  CONTEXT_MCP_ECR="${ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text 2>/dev/null || echo '')}.dkr.ecr.${AWS_REGION}.amazonaws.com/${CONTEXT_MCP_REPO}"
  LATEST_TAG=$(aws ecr describe-images --repository-name "${CONTEXT_MCP_REPO}" \
    --region "${AWS_REGION}" --query 'sort_by(imageDetails,&imagePushedAt)[-1].imageTags[0]' \
    --output text 2>/dev/null || echo "")
  if [ -n "${LATEST_TAG}" ] && [ "${LATEST_TAG}" != "None" ]; then
    CONTEXT_MCP_IMAGE="${CONTEXT_MCP_ECR}:${LATEST_TAG}"
    echo "  Using ECR image: ${CONTEXT_MCP_IMAGE}"
  else
    echo "  WARNING: No ECR image found for context-mcp. Using default: ${CONTEXT_MCP_IMAGE}"
    echo "  Run the images-build workflow first to build the context-mcp image."
  fi
fi
export CONTEXT_MCP_IMAGE NAMESPACE SERVICE_ACCOUNT
template_file "${SCRIPT_DIR}/manifests/context-mcp.yaml" | kubectl apply -f -
echo "  Context MCP Server deployed (image: ${CONTEXT_MCP_IMAGE})"
echo "  Endpoint: http://context-mcp.${NAMESPACE}.svc.cluster.local:5100"

# NOTE: OpenViking removed (Issue #1383). Semantic search now uses S3 Vectors.
# Personal context AGFS entries use S3 directly. No pod needed.

# NOTE: CodeGraphContext pod removed (Issue #105) — cgc now runs during ingestion.
# The ingestion pipeline (ingest-repo.py) handles structural analysis.

# NOTE: Sourcebot removed (Issue #1347). Code search now uses Zoekt directly.

# Deploy DeepWiki (skipped in personal-context-only mode)
if [ "${PERSONAL_CONTEXT_ONLY}" = "true" ]; then
  echo ""
  echo "Skipping DeepWiki (--personal-context-only mode)"
elif [ "${DEEPWIKI_ENABLED:-true}" = "true" ]; then
  # Ensure deepwiki-config ConfigMap exists BEFORE the Deployment is applied
  # (the Deployment mounts this ConfigMap as a volume — pod stays in
  # ContainerCreating if it's missing)
  echo ""
  echo "Creating DeepWiki config ConfigMap..."
  source "${SCRIPT_DIR}/scripts/_common.sh"
  export NAMESPACE
  template_file "${SCRIPT_DIR}/manifests/deepwiki-configmap.yaml" | kubectl apply -f -
  echo "  deepwiki-config ConfigMap ready"

  echo ""
  bash "${SCRIPT_DIR}/scripts/deploy-deepwiki.sh"
else
  echo ""
  echo "Skipping DeepWiki (DEEPWIKI_ENABLED=false)"
fi

# Deploy Terraform infrastructure (SQS + DynamoDB + optional GraphRAG)
# In personal-context-only mode, only deploy GraphRAG (Neptune) if its flag is on;
# skip SQS and ingestion-related infra.
if [ "${PERSONAL_CONTEXT_ONLY}" = "true" ]; then
  if [ "${GRAPHRAG_ENABLED:-false}" = "true" ] && [ -d "${SCRIPT_DIR}/terraform" ]; then
    echo ""
    echo "Deploying Terraform infrastructure (Neptune only, personal-context-only mode)..."
    cd "${SCRIPT_DIR}/terraform"
    terraform init -upgrade
    TF_VARS="-var=graphrag_enabled=true"
    terraform apply -auto-approve ${TF_VARS} || {
      echo "WARNING: Terraform deployment failed."
    }
    NEPTUNE_ENDPOINT=$(terraform output -raw neptune_endpoint 2>/dev/null || echo "")
    export NEPTUNE_ENDPOINT
    echo "  Neptune endpoint: ${NEPTUNE_ENDPOINT:-not set}"
    cd "${SCRIPT_DIR}"
  else
    echo ""
    echo "Skipping Terraform infrastructure (--personal-context-only, GraphRAG not enabled)"
  fi
else
  echo ""
  echo "Deploying Terraform infrastructure (SQS, DynamoDB, GraphRAG)..."
  if [ -d "${SCRIPT_DIR}/terraform" ]; then
    cd "${SCRIPT_DIR}/terraform"
    terraform init -upgrade

    TF_VARS="-var=graphrag_enabled=${GRAPHRAG_ENABLED:-false}"
    terraform apply -auto-approve ${TF_VARS} || {
      echo "WARNING: Terraform deployment failed."
    }

    # Export SQS queue URL and DynamoDB table name
    SQS_QUEUE_URL=$(terraform output -raw ingestion_queue_url 2>/dev/null || echo "")
    DYNAMO_TABLE=$(terraform output -raw dynamodb_table_name 2>/dev/null || echo "adp-context-service-state")
    export SQS_QUEUE_URL DYNAMO_TABLE
    echo "  SQS Queue URL: ${SQS_QUEUE_URL:-not set}"
    echo "  DynamoDB Table: ${DYNAMO_TABLE}"

    # Export GraphRAG endpoints if enabled
    if [ "${GRAPHRAG_ENABLED:-false}" = "true" ]; then
      NEPTUNE_ENDPOINT=$(terraform output -raw neptune_endpoint 2>/dev/null || echo "")
      OPENSEARCH_ENDPOINT=$(terraform output -raw opensearch_collection_endpoint 2>/dev/null || echo "")
      export NEPTUNE_ENDPOINT OPENSEARCH_ENDPOINT
      echo "  Neptune endpoint: ${NEPTUNE_ENDPOINT:-not set}"
      echo "  OpenSearch endpoint: ${OPENSEARCH_ENDPOINT:-not set}"
    fi
    cd "${SCRIPT_DIR}"
  fi
fi

# Deploy Ingestion Refresh CronJob (unified: repos + URLs + infra discovery)
# Skipped in personal-context-only mode (no code-intelligence backends to feed)
if [ "${PERSONAL_CONTEXT_ONLY}" = "true" ]; then
  echo ""
  echo "Skipping Ingestion Refresh CronJob (--personal-context-only mode)"
elif [ "${INGESTION_REFRESH_ENABLED:-true}" = "true" ]; then
  echo ""
  echo "Deploying Ingestion Refresh CronJob..."

  # Create ConfigMap from content files (repos.txt, urls.txt, docs.txt, accounts.txt)
  kubectl create configmap ingestion-content-config \
    --from-file=repos.txt="${SCRIPT_DIR}/index_content/repos.txt" \
    --from-file=urls.txt="${SCRIPT_DIR}/index_content/urls.txt" \
    --from-file=docs.txt="${SCRIPT_DIR}/index_content/docs.txt" \
    --from-file=accounts.txt="${SCRIPT_DIR}/index_content/accounts.txt" \
    -n "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -

  # Apply CronJob manifest (with variable substitution)
  source "${SCRIPT_DIR}/scripts/_common.sh"
  export NAMESPACE SERVICE_ACCOUNT INGESTION_IMAGE INGESTION_REFRESH_SCHEDULE
  export SQS_QUEUE_URL DYNAMO_TABLE
  template_file "${SCRIPT_DIR}/manifests/repo-refresh-cronjob.yaml" | kubectl apply -f -

  echo "  CronJob deployed: schedule='${INGESTION_REFRESH_SCHEDULE}'"
  echo "  Manual trigger: kubectl create job --from=cronjob/ingestion-refresh manual-refresh -n ${NAMESPACE}"

  # Deploy KEDA ScaledJob for parallel ingestion (if SQS is configured)
  if [ -n "${SQS_QUEUE_URL}" ] && [ -f "${SCRIPT_DIR}/manifests/ingestion-scaledjob.yaml" ]; then
    echo ""
    echo "Deploying KEDA ScaledJob for parallel ingestion..."
    export DEEPWIKI_URL="http://deepwiki.${NAMESPACE}.svc.cluster.local:8001"
    export LLM_BASE_URL="http://litellm-proxy.${NAMESPACE}.svc.cluster.local:4000/v1"
    export DEEPWIKI_ENABLED GRAPHRAG_ENABLED NEPTUNE_ENDPOINT NEPTUNE_PORT OPENSEARCH_ENDPOINT
    export WIKI_LLM_MODEL ECR_REGISTRY
    template_file "${SCRIPT_DIR}/manifests/ingestion-scaledjob.yaml" | kubectl apply -f -
    echo "  ScaledJob deployed: ingestion-worker (0-10 replicas)"
    echo "  Queue: ${SQS_QUEUE_URL}"
  fi

  # Clean up legacy CronJobs if they exist
  kubectl delete cronjob openviking-repo-refresh -n "${NAMESPACE}" 2>/dev/null && \
    echo "  Cleaned up legacy openviking-repo-refresh CronJob" || true
else
  echo ""
  echo "Skipping Ingestion Refresh CronJob (INGESTION_REFRESH_ENABLED=false)"
fi

# Deploy Personal Context Synthesis CronJob (daily 3am UTC)
if [ "${SYNTHESIS_ENABLED:-true}" = "true" ]; then
  echo ""
  echo "Deploying Personal Context Synthesis CronJob..."

  export SYNTHESIS_SCHEDULE="${SYNTHESIS_SCHEDULE:-0 3 * * *}"
  export SYNTHESIS_MODEL="${SYNTHESIS_MODEL:-bedrock/global.anthropic.claude-sonnet-4-6}"
  export MIN_LEARNINGS_THRESHOLD="${MIN_LEARNINGS_THRESHOLD:-5}"
  export MAX_UNSYNTHESIZED_AGE_DAYS="${MAX_UNSYNTHESIZED_AGE_DAYS:-7}"

  # Ensure _common.sh is sourced for template_file
  [[ "$(type -t template_file)" == "function" ]] || source "${SCRIPT_DIR}/scripts/_common.sh"

  template_file "${SCRIPT_DIR}/manifests/personal-context-synthesis-cronjob.yaml" | kubectl apply -f -

  echo "  CronJob deployed: schedule='${SYNTHESIS_SCHEDULE}'"
  echo "  Manual trigger: kubectl create job --from=cronjob/personal-context-synthesis manual-synthesis -n ${NAMESPACE}"
else
  echo ""
  echo "Skipping Personal Context Synthesis CronJob (SYNTHESIS_ENABLED=false)"
fi

# Validate
if [ "${SKIP_VALIDATE:-false}" != "true" ]; then
  echo ""
  bash "${SCRIPT_DIR}/scripts/validate.sh"
fi

echo ""
echo "============================================"
echo "Deployment complete!"
echo "============================================"
echo ""
if [ "${PERSONAL_CONTEXT_ONLY}" = "true" ]; then
  echo "Mode: PERSONAL-CONTEXT-ONLY (lean stack, ~\$280/mo)"
  echo ""
  echo "Services:"
  echo "  S3 Vectors:    adp-${ENVIRONMENT:-dev}-code-vectors (semantic search)"
  echo "  LiteLLM Proxy: http://litellm-proxy.${NAMESPACE}.svc.cluster.local:${LITELLM_PORT}"
  echo "  Context MCP:   http://context-mcp.${NAMESPACE}.svc.cluster.local:${CONTEXT_MCP_PORT:-5100}"
  if [ "${SYNTHESIS_ENABLED:-true}" = "true" ]; then
    echo "  Synthesis:     CronJob personal-context-synthesis (${SYNTHESIS_SCHEDULE:-0 3 * * *})"
  fi
  if [ "${GRAPHRAG_ENABLED:-false}" = "true" ]; then
    echo "  Neptune:       ${NEPTUNE_ENDPOINT:-not deployed}:${NEPTUNE_PORT:-8182}"
  fi
  echo ""
  echo "Skipped (not needed for personal context):"
  echo "  DeepWiki, Ingestion pipeline, OpenSearch, KEDA workers"
else
  echo "Mode: FULL STACK (~\$800/mo)"
  echo ""
  echo "Services:"
  echo "  S3 Vectors:    adp-${ENVIRONMENT:-dev}-code-vectors (semantic search)"
  echo "  LiteLLM Proxy: http://litellm-proxy.${NAMESPACE}.svc.cluster.local:${LITELLM_PORT}"
  echo "  Context MCP:   http://context-mcp.${NAMESPACE}.svc.cluster.local:${CONTEXT_MCP_PORT:-5100}"
  if [ "${DEEPWIKI_ENABLED:-true}" = "true" ]; then
    echo "  DeepWiki:      http://deepwiki.${NAMESPACE}.svc.cluster.local:${DEEPWIKI_PORT}"
  fi
  if [ "${INGESTION_REFRESH_ENABLED:-true}" = "true" ]; then
    echo "  Ingestion:     CronJob ingestion-refresh (${INGESTION_REFRESH_SCHEDULE})"
  fi
  if [ "${SYNTHESIS_ENABLED:-true}" = "true" ]; then
    echo "  Synthesis:     CronJob personal-context-synthesis (${SYNTHESIS_SCHEDULE:-0 3 * * *})"
  fi
  if [ "${GRAPHRAG_ENABLED:-false}" = "true" ]; then
    echo "  Neptune:       ${NEPTUNE_ENDPOINT:-not deployed}:${NEPTUNE_PORT:-8182}"
    echo "  OpenSearch:    ${OPENSEARCH_ENDPOINT:-not deployed}"
  fi
  if [ -n "${SQS_QUEUE_URL:-}" ]; then
    echo "  SQS Queue:     ${SQS_QUEUE_URL}"
    echo "  DynamoDB:      ${DYNAMO_TABLE}"
    echo "  KEDA Workers:  ScaledJob ingestion-worker (0-10 replicas)"
  fi
fi
echo ""
