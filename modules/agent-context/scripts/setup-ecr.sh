#!/usr/bin/env bash
# Create ECR repositories for the Agent Context Intelligence Platform.
# Idempotent — safe to re-run.
#
# Usage:
#   bash scripts/setup-ecr.sh
#   bash scripts/setup-ecr.sh --region us-west-2
set -euo pipefail

REGION="${1:-us-east-1}"
# Accept --region flag
if [ "${1:-}" = "--region" ]; then
  REGION="${2:-us-east-1}"
fi

REPOS=(
  "agent-context/context-mcp-server"
  "agent-context/litellm-proxy"
  "agent-context/codegraph-context"
)

LIFECYCLE_POLICY='{
  "rules": [
    {
      "rulePriority": 1,
      "description": "Keep last 10 tagged images",
      "selection": {
        "tagStatus": "tagged",
        "tagPrefixList": ["latest"],
        "countType": "imageCountMoreThan",
        "countNumber": 10
      },
      "action": {
        "type": "expire"
      }
    },
    {
      "rulePriority": 2,
      "description": "Keep last 20 SHA-tagged images",
      "selection": {
        "tagStatus": "any",
        "countType": "imageCountMoreThan",
        "countNumber": 20
      },
      "action": {
        "type": "expire"
      }
    }
  ]
}'

echo "============================================"
echo "ECR Repository Setup"
echo "============================================"
echo "Region: ${REGION}"
echo "Repos:  ${REPOS[*]}"
echo "============================================"

for REPO in "${REPOS[@]}"; do
  echo ""
  echo "--- ${REPO} ---"

  # Create repository if it doesn't exist
  if aws ecr describe-repositories --repository-names "${REPO}" --region "${REGION}" &>/dev/null; then
    echo "  Repository already exists"
  else
    echo "  Creating repository..."
    aws ecr create-repository \
      --repository-name "${REPO}" \
      --image-scanning-configuration scanOnPush=true \
      --encryption-configuration encryptionType=AES256 \
      --region "${REGION}"
    echo "  Created"
  fi

  # Apply lifecycle policy
  echo "  Applying lifecycle policy..."
  aws ecr put-lifecycle-policy \
    --repository-name "${REPO}" \
    --lifecycle-policy-text "${LIFECYCLE_POLICY}" \
    --region "${REGION}" > /dev/null
  echo "  Lifecycle policy applied"
done

echo ""
echo "============================================"
echo "ECR setup complete!"
echo "============================================"
echo ""
echo "Registry: $(aws sts get-caller-identity --query Account --output text).dkr.ecr.${REGION}.amazonaws.com"
echo ""
for REPO in "${REPOS[@]}"; do
  echo "  ${REPO}"
done
