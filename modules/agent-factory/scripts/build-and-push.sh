#!/bin/bash
set -e

# Configuration
ECR_REGISTRY="${ECR_REGISTRY:-$(aws sts get-caller-identity --query Account --output text).dkr.ecr.us-east-1.amazonaws.com}"
IMAGE_NAME="mcp-agent-mail"
REGION="us-east-1"
DOCKERFILE_PATH="docker/agent-mail/Dockerfile"

# Get version tag from git or use 'latest'
if git rev-parse --git-dir > /dev/null 2>&1; then
    IMAGE_TAG=$(git rev-parse --short HEAD)
    # Also tag with latest
    TAG_LATEST=true
else
    IMAGE_TAG="latest"
    TAG_LATEST=false
fi

echo "Building MCP Agent Mail container..."
echo "Registry: $ECR_REGISTRY"
echo "Image: $IMAGE_NAME"
echo "Tag: $IMAGE_TAG"
echo "Dockerfile: $DOCKERFILE_PATH"

# Build from repository root with the correct Dockerfile
echo "Building Docker image..."
docker build -t "$IMAGE_NAME:$IMAGE_TAG" -f "$DOCKERFILE_PATH" docker/agent-mail/

if [ "$TAG_LATEST" = true ]; then
    docker tag "$IMAGE_NAME:$IMAGE_TAG" "$IMAGE_NAME:latest"
fi

# Tag for ECR
docker tag "$IMAGE_NAME:$IMAGE_TAG" "$ECR_REGISTRY/$IMAGE_NAME:$IMAGE_TAG"
if [ "$TAG_LATEST" = true ]; then
    docker tag "$IMAGE_NAME:$IMAGE_TAG" "$ECR_REGISTRY/$IMAGE_NAME:latest"
fi

echo "Authenticating with ECR..."
aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $ECR_REGISTRY

echo "Creating ECR repository if it doesn't exist..."
aws ecr describe-repositories --repository-names $IMAGE_NAME --region $REGION >/dev/null 2>&1 || \
    aws ecr create-repository \
        --repository-name $IMAGE_NAME \
        --region $REGION \
        --image-scanning-configuration scanOnPush=true \
        --encryption-configuration encryptionType=AES256

echo "Pushing image to ECR..."
docker push "$ECR_REGISTRY/$IMAGE_NAME:$IMAGE_TAG"
if [ "$TAG_LATEST" = true ]; then
    docker push "$ECR_REGISTRY/$IMAGE_NAME:latest"
fi

echo "Build and push complete!"
echo "Image: $ECR_REGISTRY/$IMAGE_NAME:$IMAGE_TAG"

# Export variables for subsequent scripts (e.g., envsubst for K8s manifests)
export ECR_REGISTRY
export IMAGE_TAG
export IMAGE_NAME

echo "Exported variables:"
echo "  ECR_REGISTRY=$ECR_REGISTRY"
echo "  IMAGE_TAG=$IMAGE_TAG"
echo "  IMAGE_NAME=$IMAGE_NAME"
