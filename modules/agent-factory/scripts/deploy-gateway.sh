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

# Validate publication or rollback selectors before Terraform or Kubernetes writes.
if [[ "$DRY_RUN" == false && ( "$SKIP_IMG" != true || "$SKIP_K8S" != true ) ]]; then
    [[ "$ECR_REPO_NAME" == adp-agent-gateway ]] || { echo 'Unsupported agent gateway repository' >&2; exit 1; }
    [[ "${PUBLISH_LATEST:-false}" == false ]] || { echo 'Mutable latest publication is unsupported' >&2; exit 1; }
    ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
    REGISTRY="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
    ECR_URI="$REGISTRY/$ECR_REPO_NAME"
    if [[ "$SKIP_IMG" == true ]]; then
        AGENT_IMAGE="${AGENT_IMAGE:-$ECR_URI:${AGENT_IMAGE_TAG:?Provide AGENT_IMAGE or a full source SHA when skipping the build}}"
        AGENT_IMAGE=$(python3 "$REPO_ROOT/platform/scripts/resolve-ecr-image.py" "$AGENT_IMAGE")
    else
        SOURCE_SHA=$(git -C "$REPO_ROOT" rev-parse HEAD)
        [[ "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo 'Expected a full source SHA' >&2; exit 1; }
        [[ "${AGENT_IMAGE_TAG:-$SOURCE_SHA}" == "$SOURCE_SHA" ]] || { echo 'AGENT_IMAGE_TAG must match source SHA' >&2; exit 1; }
    fi
fi

echo "=== Agent Gateway Deploy ==="

# Step 1: Terraform (enable_gateway = true)
if [[ "${SKIP_TF}" != "true" ]]; then
    echo "[1/3] Terraform apply (enable_gateway=true)..."
    if [[ "${DRY_RUN}" == "false" ]]; then
        bash "${REPO_ROOT}/platform/scripts/build-agent-factory-lambdas.sh"
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
        SOURCE_SHA="$SOURCE_SHA" IMAGE_TAG="$SOURCE_SHA" REGISTRY="$REGISTRY" AWS_REGION="$AWS_REGION" \
            bash "$REPO_ROOT/platform/scripts/publish-local-image.sh" adp-agent-gateway
        AGENT_IMAGE=$(python3 "$REPO_ROOT/platform/scripts/resolve-ecr-image.py" "$ECR_URI:$SOURCE_SHA")
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
