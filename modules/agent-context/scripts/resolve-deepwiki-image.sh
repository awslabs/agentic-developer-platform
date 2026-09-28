#!/usr/bin/env bash
# Resolve only this environment's DeepWiki runtime repository to an immutable image.
set -euo pipefail
selector="${1:-latest}"
repository="adp-${ENVIRONMENT:?ENVIRONMENT is required}-agent-context-deepwiki"
if [[ "$selector" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  image_id="imageDigest=$selector"
elif [[ "$selector" =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}$ ]]; then
  image_id="imageTag=$selector"
else
  echo "::error::Invalid DeepWiki image selector; use an ECR tag or sha256 digest." >&2
  exit 1
fi
# latest is the final runtime tag; newer intermediate artifacts are not selected.
digest=$(aws ecr describe-images --repository-name "$repository" \
  --image-ids "$image_id" --region "${AWS_REGION:?AWS_REGION is required}" \
  --query 'imageDetails[0].imageDigest' --output text)
if [[ ! "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "::error::No built DeepWiki image found for the requested selector." >&2
  exit 1
fi
printf '%s/%s@%s\n' "${ECR_REGISTRY:?ECR_REGISTRY is required}" "$repository" "$digest"
