#!/usr/bin/env bash
# =============================================================================
# deploy-gateway.sh — Deploy the Agent Gateway sub-module
# =============================================================================
# Usage:
#   ./deploy-gateway.sh                    # Full deploy
#   ./deploy-gateway.sh --dry-run          # Preview
#   ./deploy-gateway.sh --skip-terraform   # Skip infra
#   ./deploy-gateway.sh --skip-image-build # Skip Docker
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODULE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${MODULE_ROOT}/../.." && pwd)"

source "${MODULE_ROOT}/gateway/config.env"

DRY_RUN=false; SKIP_TF=false; SKIP_IMG=false; SKIP_K8S=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)           DRY_RUN=true; shift ;;
        --skip-terraform)    SKIP_TF=true; shift ;;
        --skip-image-build)  SKIP_IMG=true; shift ;;
        --skip-k8s)          SKIP_K8S=true; shift ;;
        --help|-h)           echo "Usage: deploy-gateway.sh [--dry-run] [--skip-terraform] [--skip-image-build] [--skip-k8s]"; exit 0 ;;
        *) echo "Unknown: $1"; exit 1 ;;
    esac
done

echo "=== Agent Gateway Deploy ==="

# Step 1: Terraform (enable_gateway = true)
if [[ "${SKIP_TF}" != "true" ]]; then
    echo "[1/3] Terraform apply (enable_gateway=true)..."
    if [[ "${DRY_RUN}" == "false" ]]; then
        pushd "${MODULE_ROOT}/infra" > /dev/null
        terraform apply -input=false -auto-approve -var="enable_gateway=true"
        INPUT_QUEUE_URL=$(terraform output -raw gateway_input_queue_url 2>/dev/null || echo "")
        RESPONSE_QUEUE_URL=$(terraform output -raw gateway_response_queue_url 2>/dev/null || echo "")
        SESSIONS_TABLE=$(terraform output -raw gateway_sessions_table 2>/dev/null || echo "")
        WS_ENDPOINT=$(terraform output -raw gateway_ws_endpoint 2>/dev/null || echo "")
        popd > /dev/null
        echo "  Input Queue: ${INPUT_QUEUE_URL}"
        echo "  WS Endpoint: ${WS_ENDPOINT}"
    else
        echo "  [DRY RUN] terraform apply -var=enable_gateway=true"
    fi
else
    echo "[1/3] Skipping Terraform"
    if [[ "${DRY_RUN}" == "false" ]]; then
        pushd "${MODULE_ROOT}/infra" > /dev/null
        INPUT_QUEUE_URL=$(terraform output -raw gateway_input_queue_url 2>/dev/null || echo "")
        RESPONSE_QUEUE_URL=$(terraform output -raw gateway_response_queue_url 2>/dev/null || echo "")
        SESSIONS_TABLE=$(terraform output -raw gateway_sessions_table 2>/dev/null || echo "")
        popd > /dev/null
    fi
fi

# Step 2: Docker build + push
if [[ "${SKIP_IMG}" != "true" ]]; then
    echo "[2/3] Building Docker image..."
    if [[ "${DRY_RUN}" == "false" ]]; then
        ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
        ECR_URI="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO_NAME}"

        aws ecr describe-repositories --repository-names "${ECR_REPO_NAME}" --region "${AWS_REGION}" > /dev/null 2>&1 || \
            aws ecr create-repository --repository-name "${ECR_REPO_NAME}" --region "${AWS_REGION}" --no-cli-pager

        aws ecr get-login-password --region "${AWS_REGION}" | \
            docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

        BUILD_DIR="/tmp/agent-gateway-build"
        rm -rf "${BUILD_DIR}"; mkdir -p "${BUILD_DIR}"
        cp -r "${MODULE_ROOT}/gateway/app" "${BUILD_DIR}/app"
        cp -r "${MODULE_ROOT}/agent" "${BUILD_DIR}/agent"
        cp "${MODULE_ROOT}/gateway/Dockerfile" "${BUILD_DIR}/Dockerfile"
        cp "${MODULE_ROOT}/gateway/entrypoint.sh" "${BUILD_DIR}/entrypoint.sh"

        docker build -t "${ECR_URI}:${AGENT_IMAGE_TAG}" "${BUILD_DIR}"
        docker push "${ECR_URI}:${AGENT_IMAGE_TAG}"
        rm -rf "${BUILD_DIR}"
        AGENT_IMAGE="${ECR_URI}:${AGENT_IMAGE_TAG}"
        echo "  Pushed: ${AGENT_IMAGE}"
    else
        echo "  [DRY RUN] docker build + push"
    fi
else
    echo "[2/3] Skipping image build"
fi

# Step 3: K8s manifests
if [[ "${SKIP_K8S}" != "true" ]]; then
    echo "[3/3] Deploying K8s manifests..."
    if [[ "${DRY_RUN}" == "false" ]]; then
        # Namespace and service account are managed by Terraform (gateway-main.tf).
        # Only create namespace here as a fallback if Terraform hasn't run yet.
        kubectl get namespace "${GATEWAY_NAMESPACE}" > /dev/null 2>&1 || kubectl create namespace "${GATEWAY_NAMESPACE}"

        # Fetch the gateway agent role ARN from Terraform output for the SA annotation.
        # If Terraform has already created the SA, this is a no-op — the ScaledJob
        # references the SA by name, not by ARN.
        AGENT_ROLE_ARN=""
        if pushd "${MODULE_ROOT}/infra" > /dev/null 2>&1; then
            AGENT_ROLE_ARN=$(terraform output -raw gateway_agent_role_arn 2>/dev/null || echo "")
            popd > /dev/null
        fi

        RENDERED="/tmp/gateway-k8s-rendered.yaml"
        sed -e "s|REPLACE_WITH_INPUT_QUEUE_URL|${INPUT_QUEUE_URL:-PENDING}|g" \
            -e "s|REPLACE_WITH_RESPONSE_QUEUE_URL|${RESPONSE_QUEUE_URL:-PENDING}|g" \
            -e "s|REPLACE_WITH_SESSIONS_TABLE_NAME|${SESSIONS_TABLE:-PENDING}|g" \
            -e "s|REPLACE_WITH_AGENT_IMAGE|${AGENT_IMAGE:-PENDING}|g" \
            "${MODULE_ROOT}/gateway/k8s/keda-scaledjob.yaml" > "${RENDERED}"
        kubectl apply -f "${RENDERED}"
        rm -f "${RENDERED}"
        echo "  K8s deployed"
    else
        echo "  [DRY RUN] kubectl apply"
    fi
else
    echo "[3/3] Skipping K8s"
fi

echo "=== Gateway deploy complete ==="
